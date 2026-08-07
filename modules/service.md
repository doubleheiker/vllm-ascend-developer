# service.md — 服务生命周期管理模块

## 概述

管理 vLLM 推理服务的启动、监控、健康检查和停止。支持单机（standalone）和 PD分离（pd-separated）两种模式，所有远程操作统一通过 `scripts/ssh_utils.py` 执行。

## 使用方式

该模块由工作流自动调用，或者可独立使用：
```
在当前会话中，遵循以下步骤启动服务。
```

本模块中的命令是低自由度执行模板：替换配置占位符后逐条直接执行，不增加端口探测，不交换 `exec` 与 `docker-exec`，也不临时选择替代工具。`fuser` 只允许通过宿主机 `exec` 执行；容器中是否安装 `fuser` 与本流程无关。任一命令返回 `success: false` 或非零 `exit_code` 时停止并报告原始结果，不用后续 `echo` 掩盖错误。

## 前置条件

- 已正确配置 `config/service.yaml`
- 目标服务器已安装 Docker
- 如需下载依赖或 clone 代码，配置 `config/proxy.yaml`

## 步骤 0：容器 Python 导入检查

每个新诊断工作流在启动服务前，对本次使用的每个节点执行一次只读检查。该脚本复用 `ssh_utils.py` 的 `docker-exec --source-pythonpath`，先将配置的容器源码路径置于现有 `PYTHONPATH` 前方，再检查实际导入来源；它不会安装包、修改源码或写入远程文件：

```bash
# 单机模式
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/preflight.py" --project-root "${CLAUDE_PROJECT_DIR}" standalone

# PD 分离模式：按实际配置对每个 P/D 节点分别执行
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/preflight.py" --project-root "${CLAUDE_PROJECT_DIR}" pd-separated.p[0]
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/preflight.py" --project-root "${CLAUDE_PROJECT_DIR}" pd-separated.d[0]
```

结果处理规则：

- 外层 `success: false` 或缺少 `preflight`：停止并报告原始结果，不重试、不读取工具源码猜测原因。
- `preflight.import_ready: false`：停止启动，直接报告 `imports` 中的导入错误；禁止自动执行 `pip install`。
- 任一 `source_match: false` 或预期源码目录 `exists: false`：停止启动，报告实际 `__file__`、`PYTHONPATH` 和 `sys.path`；不要退回 site-packages 继续运行。
- 已在当前工作流成功检查过的节点直接复用结果，不要在同一轮启动/重启时重复检查。

---

## 单机模式流程（standalone）

宿主机命令通过 `ssh_utils.py exec` 执行并默认从节点 `work_dir` 开始；容器命令通过 `ssh_utils.py docker-exec` 执行并默认从 `docker.work_dir` 开始，同时注入节点 `env_vars`。绝对路径和显式 `cd` 仍可使用；禁止手工拼接 `docker exec`。

### 步骤 1：停止已有进程并清空 plog

**1a. 在容器内停止已记录的 standalone 进程组**：

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" docker-exec standalone "if [ -s {docker.work_dir}/vllm.pid ]; then kill -9 -\$(cat {docker.work_dir}/vllm.pid) 2>/dev/null; fi; rm -f {docker.work_dir}/vllm.pid"
```

**1b. 在宿主机按端口清理残留进程及 proxy**：

```bash
# 杀服务端口（vllm）
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" exec standalone "if ! command -v fuser >/dev/null 2>&1; then echo fuser_not_found_on_host >&2; exit 127; fi; fuser -k {standalone.service_port}/tcp >/dev/null 2>&1 || true; sleep 2; if fuser {standalone.service_port}/tcp >/dev/null 2>&1; then echo service_port_still_busy >&2; exit 1; fi; echo service_port_clean"

# 如果配置了 proxy_port，也杀掉
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" exec standalone "if ! command -v fuser >/dev/null 2>&1; then echo fuser_not_found_on_host >&2; exit 127; fi; fuser -k {docker.proxy_port}/tcp >/dev/null 2>&1 || true; sleep 1; if fuser {docker.proxy_port}/tcp >/dev/null 2>&1; then echo proxy_port_still_busy >&2; exit 1; fi; echo proxy_port_clean"
```

> **说明**：第二条命令只在 `proxy_port` 已配置且与服务端口不同时执行。proxy 和 vllm 共享端口时只执行第一条；`proxy_port` 为空时跳过第二条，不执行带空端口的命令。

**1c. 清空 plog**（防止旧日志干扰本次定位）：

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" docker-exec standalone "rm -rf /root/ascend/log/debug/plog/*; echo plog_cleared"
```

注意：每个命令单独执行，不可用 && 链式连接。

### 步骤 2：启动服务

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" docker-exec standalone --source-pythonpath "setsid bash {docker.startup_script} > {docker.log_file} 2>&1 & echo \$! > {docker.work_dir}/vllm.pid"
```

`--source-pythonpath` 根据 `model.yaml` 自动把 vLLM、vLLM-Ascend 容器源码目录放在原有 `PYTHONPATH` 前方，不需要 Agent 拼接路径。随后才执行启动脚本，因此脚本内部显式设置的同名环境变量仍然优先。`setsid` 和 PID 文件行为保持不变。

### 步骤 3：监控日志等待启动

启动过程通常需要 10 分钟以上。在容器内轮询日志，直到出现成功关键词：

```bash
# 等待启动完成（每 30s 检查一次，最多等 15 分钟）
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" wait standalone "{docker.log_file}" "Application startup complete" --scope container --timeout 900 --interval 30
```

> **注意**：`--scope container` 会在配置的容器及其 `docker.work_dir` 中读取日志；宿主机日志改用 `--scope host`。

也可手动查看进度：

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" docker-exec standalone "tail -n 50 {docker.log_file}"
```

### 步骤 4：检查启动成功

日志中是否包含所有 `success_keywords`：
- `Application startup complete.`
- `Started server process`

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" docker-exec standalone "grep -E 'Application startup complete|Started server process' {docker.log_file}"
```

### 步骤 5：健康检查

服务启动后使用实际 IP 做健康检查（注意：`--host` 绑定的 IP 不是 `localhost`）：

```bash
# 用服务实际绑定的 IP（service.yaml 中的 standalone.host）
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" docker-exec standalone "unset http_proxy; unset https_proxy; curl -s -o /dev/null -w \"%{http_code}\" --max-time 30 http://{standalone.host}:{standalone.service_port}/v1/models"
# 必须返回 200
```

> **重要**：curl 前必须 `unset http_proxy; unset https_proxy`，否则代理可能导致 504。

---

## PD分离模式流程（pd-separated）

PD分离模式下 P 节点（prefill）和 D 节点（decode）分别在不同机器上。节点通过 `pd-separated.p[N]` / `pd-separated.d[N]` 引用。

### 步骤 1：停止已有进程并清空 plog

对每个 P 节点和 D 节点，按端口杀：

```bash
# P 节点
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" exec pd-separated.p[0] "if ! command -v fuser >/dev/null 2>&1; then echo fuser_not_found_on_host >&2; exit 127; fi; fuser -k {pd-separated.service_port}/tcp >/dev/null 2>&1 || true; sleep 2; if fuser {pd-separated.service_port}/tcp >/dev/null 2>&1; then echo p_service_port_still_busy >&2; exit 1; fi; echo p_service_port_clean"
# D 节点
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" exec pd-separated.d[0] "if ! command -v fuser >/dev/null 2>&1; then echo fuser_not_found_on_host >&2; exit 127; fi; fuser -k {pd-separated.service_port}/tcp >/dev/null 2>&1 || true; sleep 2; if fuser {pd-separated.service_port}/tcp >/dev/null 2>&1; then echo d_service_port_still_busy >&2; exit 1; fi; echo d_service_port_clean"

# 清空 plog
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" docker-exec pd-separated.p[0] "rm -rf /root/ascend/log/debug/plog/*; echo plog_cleared"
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" docker-exec pd-separated.d[0] "rm -rf /root/ascend/log/debug/plog/*; echo plog_cleared"
```

### 步骤 2：启动服务

P 节点和 D 节点分别启动各自的 proxy + vLLM 服务：

```bash
# P 节点启动
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" docker-exec pd-separated.p[0] "nohup bash {docker.proxy_script} > {docker.log_file} 2>&1 &"
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" docker-exec pd-separated.p[0] --source-pythonpath "nohup bash {docker.startup_script} > {docker.log_file} 2>&1 &"

# D 节点同理
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" docker-exec pd-separated.d[0] "nohup bash {docker.proxy_script} > {docker.log_file} 2>&1 &"
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" docker-exec pd-separated.d[0] --source-pythonpath "nohup bash {docker.startup_script} > {docker.log_file} 2>&1 &"
```

### 步骤 3-5：监控、检查、健康检查

与单机模式相同，分别对 P 和 D 节点执行。健康检查端口使用 `{pd-separated.service_port}`。

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" docker-exec pd-separated.p[0] "tail -n 50 {docker.log_file}"
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" docker-exec pd-separated.d[0] "tail -n 50 {docker.log_file}"
```

---

## 停止服务

### 单机模式（standalone）

先在容器内按已记录 PID 杀整个进程组并删除 PID 文件，再由宿主机 `fuser` 清理端口残留：

```bash
# 1. 容器内停止 vLLM 进程组
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" docker-exec standalone "if [ -s {docker.work_dir}/vllm.pid ]; then kill -9 -\$(cat {docker.work_dir}/vllm.pid) 2>/dev/null; fi; rm -f {docker.work_dir}/vllm.pid"

# 2. 宿主机清理服务端口 + proxy端口（如配置）
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" exec standalone "if ! command -v fuser >/dev/null 2>&1; then echo fuser_not_found_on_host >&2; exit 127; fi; fuser -k {standalone.service_port}/tcp >/dev/null 2>&1 || true; sleep 2; if fuser {standalone.service_port}/tcp >/dev/null 2>&1; then echo service_port_still_busy >&2; exit 1; fi; echo service_port_clean"
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" exec standalone "if ! command -v fuser >/dev/null 2>&1; then echo fuser_not_found_on_host >&2; exit 127; fi; fuser -k {docker.proxy_port}/tcp >/dev/null 2>&1 || true; sleep 1; if fuser {docker.proxy_port}/tcp >/dev/null 2>&1; then echo proxy_port_still_busy >&2; exit 1; fi; echo proxy_port_clean"
```

### PD分离模式

PD 分离暂时保留原有的宿主机端口清理方式，不套用未经真实环境验证的 PID 规则：

```bash
# PD分离模式 — 分别停各节点
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" exec pd-separated.p[0] "if ! command -v fuser >/dev/null 2>&1; then echo fuser_not_found_on_host >&2; exit 127; fi; fuser -k {pd-separated.service_port}/tcp >/dev/null 2>&1 || true; sleep 2; if fuser {pd-separated.service_port}/tcp >/dev/null 2>&1; then echo p_service_port_still_busy >&2; exit 1; fi; echo p_service_port_clean"
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" exec pd-separated.d[0] "if ! command -v fuser >/dev/null 2>&1; then echo fuser_not_found_on_host >&2; exit 127; fi; fuser -k {pd-separated.service_port}/tcp >/dev/null 2>&1 || true; sleep 2; if fuser {pd-separated.service_port}/tcp >/dev/null 2>&1; then echo d_service_port_still_busy >&2; exit 1; fi; echo d_service_port_clean"
```

> **说明**：`fuser -k {port}/tcp` 只杀占用指定端口的进程，不影响其他服务。proxy 和 vllm 共用端口则只执行服务端口命令；proxy 独立端口且已配置时才额外执行 proxy 命令。宿主机没有 `fuser`、清理后端口仍被占用或配置为空时，不得用成功输出掩盖，应停止并报告。

---

## 常见问题

1. **启动超时** — 检查 `startup_timeout` 是否足够，查看日志是否有明显错误
2. **端口冲突** — 检查端口是否已被占用，修改对应模式下的 `service_port`
3. **模型路径错误** — 检查 `model.yaml` 中的 `model_path` 是否正确
4. **Ascend 设备初始化失败** — 检查是否正确挂载了 `/dev/davinci*` 设备
5. **OOM 错误** — 模型过大或 batch size 过高，检查显存：`npu-smi info`
6. **ssh_utils.py 连接失败** — 检查 `service.yaml` 中节点的 `host/port/username/password` 是否正确
7. **daemon 异常** — `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" stop <node-ref>` 停止后重试
