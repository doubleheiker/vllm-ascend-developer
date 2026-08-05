# service.md — 服务生命周期管理

## 概述

统一管理 vLLM 服务的启动、状态查询和停止。支持单机与 PD 分离模式。不要手工拼接 `nohup`、`docker exec`、`kill`、`pkill` 或 `fuser -k`；这些执行域和顺序由 `scripts/ssh_utils.py` 统一控制。

## 配置约定

- `service_port` 是宿主机对外服务端口；`docker.proxy_port` 仅在 proxy 使用独立端口时填写。
- `docker.startup_script`、`docker.proxy_script`、`docker.log_file` 都是容器内可见的绝对路径。
- `docker.pid_file` 可选，默认为 `{docker.log_file}.pgid`。它记录服务 session 的进程组 ID，不是单个 API Server PID。
- PD 分离模式必须配置 `docker.proxy_script`。proxy 和 vLLM 由同一进程组管理。
- `docker-exec` 会注入节点 `env_vars`。启动脚本内显式 `export` 或变量赋值会按 Shell 规则覆盖同名配置，因此以启动脚本为准。

## 启动前处理

如果本轮前确实启动过服务，先调用 `service-stop`。这会先在容器内停止已跟踪的进程组，再在宿主机按配置端口兜底和复查。

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" service-stop standalone
```

仅清理当前节点的 plog：

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" docker-exec standalone "rm -rf /root/ascend/log/debug/plog/*; echo plog_cleared"
```

`service-stop` 返回 `SERVICE_STOP=untracked` 时，表示容器内存在本 Plugin 没有启动记录的 vLLM/EngineCore 进程。为避免误杀其他任务，脚本会拒绝按进程名广泛杀除；应先确认这些进程的归属。

## 单机模式流程（standalone）

### 1. 启动

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" service-start standalone
```

启动器自动执行：

1. 在宿主机只读检查 `service_port` 和可选 `proxy_port`。端口忙时不进入容器启动。
2. 在配置容器中检查旧 PGID 和未跟踪 vLLM 进程。
3. 使用 `setsid` 创建独立 session，记录真实 PGID，然后执行 `startup_script`。
4. 返回 `SERVICE_STARTED_PGID=<n>` 才表示启动命令已成功交付。这不代表 HTTP 服务已就绪。

### 2. 等待日志并做健康检查

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" wait standalone "{docker.log_file}" "Application startup complete" --scope container --timeout 3600 --interval 30
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" service-status standalone
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" docker-exec standalone "unset http_proxy; unset https_proxy; curl -s -o /dev/null -w \"%{http_code}\" --max-time 30 http://{standalone.host}:{standalone.service_port}/v1/models"
```

只有日志成功关键词出现且 `/v1/models` 返回 `200` 后，才可运行推理请求或 aisbench。

### 3. 停止

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" service-stop standalone --grace-period 30
```

停止器的固定顺序是：容器内进程组 `TERM` → 有界等待 → 必要时 `KILL` → 宿主机端口兜底 → 端口复查。`VLLM::EngineCore` 不监听 HTTP 端口，因此不能只依赖端口杀进程。

## PD分离模式流程（pd-separated）

对每个 P/D 节点使用同一组命令。下面以第一个 P/D 节点为例：

```bash
# 启动
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" service-start pd-separated.p[0]
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" service-start pd-separated.d[0]

# 状态
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" service-status pd-separated.p[0]
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" service-status pd-separated.d[0]

# 停止：逐节点执行，不要拼成一条命令
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" service-stop pd-separated.p[0]
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" service-stop pd-separated.d[0]
```

每个节点的 proxy 和 vLLM 同属该节点的一个 PGID。不要再分别用两条 `nohup` 命令启动。

## SSH daemon 与服务的区别

`service-stop` 停的是远程 vLLM 服务。`stop-daemon` 只停本地 Paramiko 持久连接，不会停远程服务：

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" daemon-status standalone
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" stop-daemon standalone
```

旧的 `status` / `stop` 仅作为 daemon 命令的临时兼容别名，新工作流不再使用。

## 状态解读

| 标记 | 含义 | 动作 |
|---|---|---|
| `SERVICE_STATUS=running` | pid file 指向活进程组 | 继续等待/健康检查 |
| `SERVICE_STATUS=stopped` | 无跟踪进程，也无可见 vLLM 残留 | 可启动 |
| `SERVICE_STATUS=stale-pidfile` | pid file 存在，但对应进程组已消失 | `service-stop` 幂等清理 |
| `SERVICE_STATUS=untracked` | 存在不属于当前 pid file 的 vLLM | 确认归属，不得盲目 `pkill` |
| `SERVICE_STOP=clean` | 进程组与端口均通过清理/复查 | 可重启 |
| `SERVICE_STOP=untracked` | 有未跟踪的活 vLLM 进程 | 停止自动重启，先人工确认 |
| `SERVICE_STOP=ownership-mismatch` | PGID 已被其他进程组复用，与当前启动脚本/vLLM 不匹配 | 拒绝 kill，检查 pid file 和容器状态 |

僵尸进程已经死亡，不能再被 `kill`。本轮把“非 zombie 的活进程”作为停止守卫；如容器长期累积 zombie，需要检查容器 init/reaper 配置，这不能靠反复 `kill -9` 解决。
