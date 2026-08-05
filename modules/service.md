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

---

## 单机模式流程（standalone）

宿主机命令通过 `ssh_utils.py exec` 执行并默认从节点 `work_dir` 开始；容器命令通过 `ssh_utils.py docker-exec` 执行并默认从 `docker.work_dir` 开始，同时注入节点 `env_vars`。绝对路径和显式 `cd` 仍可使用；禁止手工拼接 `docker exec`。

### 步骤 1：停止已有进程并清空 plog

**1a. 按端口杀 vLLM 进程及 proxy**：

```bash
# 杀服务端口（vllm）
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" exec standalone "if ! command -v fuser >/dev/null 2>&1; then echo fuser_not_found_on_host >&2; exit 127; fi; fuser -k {standalone.service_port}/tcp >/dev/null 2>&1 || true; sleep 2; if fuser {standalone.service_port}/tcp >/dev/null 2>&1; then echo service_port_still_busy >&2; exit 1; fi; echo service_port_clean"

# 如果配置了 proxy_port，也杀掉
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" exec standalone "if ! command -v fuser >/dev/null 2>&1; then echo fuser_not_found_on_host >&2; exit 127; fi; fuser -k {docker.proxy_port}/tcp >/dev/null 2>&1 || true; sleep 1; if fuser {docker.proxy_port}/tcp >/dev/null 2>&1; then echo proxy_port_still_busy >&2; exit 1; fi; echo proxy_port_clean"
```

> **说明**：第二条命令只在 `proxy_port` 已配置且与服务端口不同时执行。proxy 和 vllm 共享端口时只执行第一条；`proxy_port` 为空时跳过第二条，不执行带空端口的命令。

**1b. 清空 plog**（防止旧日志干扰本次定位）：

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" docker-exec standalone "rm -rf /root/ascend/log/debug/plog/*; echo plog_cleared"
```

注意：每个命令单独执行，不可用 && 链式连接。

### 步骤 2：启动服务

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" docker-exec standalone "nohup bash {docker.startup_script} > {docker.log_file} 2>&1 &"
```

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
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" docker-exec pd-separated.p[0] "nohup bash {docker.startup_script} > {docker.log_file} 2>&1 &"

# D 节点同理
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" docker-exec pd-separated.d[0] "nohup bash {docker.proxy_script} > {docker.log_file} 2>&1 &"
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" docker-exec pd-separated.d[0] "nohup bash {docker.startup_script} > {docker.log_file} 2>&1 &"
```

### 步骤 3-5：监控、检查、健康检查

与单机模式相同，分别对 P 和 D 节点执行。健康检查端口使用 `{pd-separated.service_port}`。

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" docker-exec pd-separated.p[0] "tail -n 50 {docker.log_file}"
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" docker-exec pd-separated.d[0] "tail -n 50 {docker.log_file}"
```

---

## 停止服务

**按端口杀**（`fuser -k` 精准不误伤）：

```bash
# 单机模式 — 杀服务端口 + proxy端口（如配置）
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" exec standalone "if ! command -v fuser >/dev/null 2>&1; then echo fuser_not_found_on_host >&2; exit 127; fi; fuser -k {standalone.service_port}/tcp >/dev/null 2>&1 || true; sleep 2; if fuser {standalone.service_port}/tcp >/dev/null 2>&1; then echo service_port_still_busy >&2; exit 1; fi; echo service_port_clean"
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" exec standalone "if ! command -v fuser >/dev/null 2>&1; then echo fuser_not_found_on_host >&2; exit 127; fi; fuser -k {docker.proxy_port}/tcp >/dev/null 2>&1 || true; sleep 1; if fuser {docker.proxy_port}/tcp >/dev/null 2>&1; then echo proxy_port_still_busy >&2; exit 1; fi; echo proxy_port_clean"

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
