---
name: diagnose
description: vLLM/vLLM-Ascend 通用开发 skill，覆盖精度诊断、服务管理、测试验证等场景，支持单机/PD分离双模式
disable-model-invocation: true
hooks:
  PreToolUse:
    - matcher: "Write|Edit|Bash"
      hooks:
        - type: command
          command: 'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/path_policy.py" hook'
          timeout: 10
---

# vllm-ascend-developer — vLLM Ascend 开发 Skill

## 概述

本 skill 用于 vLLM + vLLM-Ascend 框架下的开发与调试工作，覆盖推理精度诊断、服务管理、自动化测试与代码修复等场景。采用模块化设计，支持单机和PD分离两种部署模式。

## 本地路径安全

Plugin 安装目录 `${CLAUDE_PLUGIN_ROOT}` 只读。所有本地配置和运行结果只能写入用户项目 `${CLAUDE_PROJECT_DIR}/.dev/`。

开始诊断时只执行一次 `bootstrap`，由脚本幂等复制缺失配置模板并初始化唯一运行目录：

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/path_policy.py" \
  --project-root "${CLAUDE_PROJECT_DIR}" bootstrap
```

不要预先用 `ls` 检查配置目录，不要手工执行 `mkdir` 或 `cp`。读取返回的 `copied`、`existing` 和 `new_templates_copied`：已有配置永不覆盖；当 `new_templates_copied` 为 `true` 时，告知用户填写新复制的配置并停止，等待用户确认后再继续。该字段只表示本次复制了新模板，不代表已有配置已经通过工作流校验。

保存命令返回的 `run_dir`，本轮生成脚本写入 `generated/`、下载文件写入 `downloads/`、日志写入 `logs/`、修复记录写入 `records/`。禁止在 Plugin 源目录、workspace 外或未激活的 run 中生成文件。Hook 拒绝操作时，用中文解释原因，不要通过其他工具或命令绕过。

初始化时会创建 `${CLAUDE_PROJECT_DIR}/.dev/.gitignore`，统一忽略项目私有配置、凭据、daemon 状态和全部运行产物。

## 目录结构

```
skills/vllm-ascend-developer/
├── SKILL.md                        # 本文件 — 主入口
├── config/                         # 配置文件（使用前需修改）
│   ├── service.yaml                # 服务配置
│   ├── test.yaml                   # 测试配置
│   ├── model.yaml                  # 模型/源码配置
│   ├── aisbench.yaml               # 精度评测配置
│   └── proxy.yaml                  # 网络代理配置
├── scripts/                        # 工具脚本
│   ├── ssh_utils.py                # SSH 远程执行（exec/docker-exec/wait/upload/download）
│   ├── generate_curl.py            # 从 config/test.yaml 生成 curl 测试脚本
│   └── path_policy.py              # 运行目录与 PreToolUse 路径安全策略
├── docs/                           # 经验文档（调试方法、定位案例）
│   ├── dcp2tp4-precision-fix.md    # DCP 精度调试：逐层对比、float64 merge
│   └── pcp-hybrid-nan-fix.md       # PCP NaN：bool mask fill_() 不写回
├── modules/                        # 核心模块
│   ├── service.md                  # 服务生命周期管理
│   ├── test-runner.md              # 测试执行器
│   ├── verifier.md                 # 结果验证器
│   ├── aisbench-evaluator.md       # 精度数据集评测（aisbench）
│   ├── log-analyzer.md            # 日志分析器
│   ├── auto-fixer.md              # 自动修复引擎
└── workflows/                      # 工作流
    └── precision-diagnosis.md      # 精度诊断工作流
```

## 快速开始

### 第零步：环境初始化

首次使用前，请用户在受控 Python 环境的终端中手动安装 `scripts/ssh_utils.py` 的依赖。诊断 Skill 激活期间的安全 Hook 会拒绝 `pip` 等会写入 workspace 外的包管理命令，不会代替用户修改 Python 环境。

```bash
pip install paramiko pyyaml -i https://pypi.tuna.tsinghua.edu.cn/simple
```

在用户终端中验证安装：

```bash
python -c "import paramiko, yaml; print('ok')"
# 输出 ok
```

> **说明**：`paramiko` 用于 SSH 远程连接，`pyyaml` 用于解析 `config/*.yaml` 配置文件。两者不在 Python 标准库中，需要在首次使用前安装。默认 PyPI 源可能超时，建议使用清华源（`-i https://pypi.tuna.tsinghua.edu.cn/simple`）。

### 第一步：配置环境

Plugin 中的 `${CLAUDE_PLUGIN_ROOT}/config/*.yaml` 是只读模板。`bootstrap` 会将缺失模板复制到项目私有目录，绝不覆盖已有文件。不要让模型自行拼接复制命令。真实密码和路径只写入项目私有配置。

根据实际环境修改：

1. **`${CLAUDE_PROJECT_DIR}/.dev/config/service.yaml`** — 部署模式、服务器连接、Docker 配置
2. **`${CLAUDE_PROJECT_DIR}/.dev/config/test.yaml`** — 测试用例、请求参数、prompt、预期输出
3. **`${CLAUDE_PROJECT_DIR}/.dev/config/model.yaml`** — 模型路径和源码路径
4. **`${CLAUDE_PROJECT_DIR}/.dev/config/aisbench.yaml`** — 【可选】精度数据集评测参数
5. **`${CLAUDE_PROJECT_DIR}/.dev/config/proxy.yaml`** — 【可选】网络代理

### 第二步：执行精度诊断工作流

读取 `${CLAUDE_PLUGIN_ROOT}/workflows/precision-diagnosis.md`，按照其中步骤执行：

1. 初始化配置检查
2. 启动服务 → 确保服务就绪
3. 执行测试 → 发送推理请求
4. 验证结果 → 对比预期输出
5. 如果不通过 → 分析日志 → 修复代码 → 重新启动 → 重新测试

### 第三步：查看修复记录

每次修复迭代记录在 `{run_dir}/records/fix_N.md`，N 从 1 递增。

---

## 选择工作模式

### 单机模式（standalone）

prefill 与 decode 在同一台机器上混合调度。需配置服务器 SSH 信息和 Docker 参数。

```yaml
mode: "standalone"
standalone:
  host: "<your-server-ip>"
  port: 22
  username: "root"
  password: "<your-password>"
  work_dir: "/path/to/scripts"
  env_vars:
    ASCEND_RT_VISIBLE_DEVICES: "0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15"
  docker:
    name: "vllm-ascend-dev"
    startup_script: "/path/to/start_server.sh"
    ...
```

`env_vars` 必须写成 `变量名: 标量值`，不要填写 `export KEY=...` 或 Shell 片段。它只由 `docker-exec` 注入对应节点的容器；宿主机 `exec` 不注入这些变量。

适用场景：单台 GPU/NPU 服务器，开发与部署在同一台机器

### PD分离模式（pd-separated）

P 节点（prefill）与 D 节点（decode）在不同机器上分离调度，通过 proxy 协调。各节点支持多机配置。

```yaml
mode: "pd-separated"
pd-separated:
  p_nodes:                    # Prefill 节点列表
    - host: "<p-node-ip>"
      username: "root"
      password: "<your-password>"
      ...
  d_nodes:                    # Decode 节点列表
    - host: "<d-node-ip>"
      username: "root"
      password: "<your-password>"
      ...
```

适用场景：大规模分布式推理，prefill 和 decode 需独立扩缩容

---

## 模块间关系

```
┌──────────────────────────────────────────────────────┐
│  SKILL.md                                            │
│  主入口 — 选择工作流                                   │
└───────────────────┬──────────────────────────────────┘
                    │ 引用
                    ▼
┌──────────────────────────────────────────────────────┐
│  workflows/precision-diagnosis.md                    │
│  端到端精度诊断工作流                                   │
└───┬──────┬──────┬──────┬──────┬──────┬───────────────┘
    │      │      │      │      │      │
    ▼      ▼      ▼      ▼      ▼      ▼
┌─────┐ ┌─────┐ ┌─────┐ ┌─────┐ ┌─────┐ ┌──────────┐
│service│ │test │ │verify│ │aisbench│ │log  │ │auto │ │config/   │
│.md   │ │runner│ │.md   │ │evaluat│ │analy│ │fixer│ │*.yaml    │
│      │ │.md   │ │      │ │or.md  │ │zer  │ │.md  │ │(配置数据) │
└─────┘ └─────┘ └─────┘ └─────┘ └─────┘ └──────────┘
```

---

## 注意事项

1. **只修改 vllm-ascend 代码** — 禁止修改 vLLM 源码
2. **区分宿主机与容器命令** — 宿主机命令使用 `ssh_utils.py exec`；容器命令必须使用 `ssh_utils.py docker-exec`，禁止手工拼接 `docker exec`。`docker-exec` 会合并全局/节点级 Docker 配置、注入当前节点的 `env_vars`，并默认使用 `docker.work_dir`。每次调用相互独立；同一容器命令内用 `;` 分隔步骤。
3. **SSH 远程操作** — 所有远程命令统一通过 `${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py` 执行，首次调用自动建立持久连接（Paramiko daemon），后续命令复用同一连接。宿主机参考：`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" exec standalone "..."`；容器参考：`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" docker-exec standalone "..."`
4. **禁止擅自重装** — vLLM 和 vLLM-Ascend 已在容器内预装，未经用户同意禁止执行 `pip install` 等安装/覆盖操作
5. **按端口杀进程** — 使用 `fuser -k {service_port}/tcp` 只杀占用服务端口的进程，不会误伤其他端口（如 8081）上的服务。杀完后用 `fuser {service_port}/tcp` 确认端口已释放
6. **服务就绪后执行 aisbench** — aisbench 精度评测必须在 vLLM 服务完全启动并通过健康检查（`curl http://host:port/v1/models` 返回 200）之后才能执行，严禁在服务未就绪时发起评测
7. **迭代记录** — 每次修改记录到当前 `{run_dir}/records/fix_N.md`
8. **启动耗时** — vLLM 服务启动通常需要 10 分钟以上
9. **服务器环境** — 需要目标服务器已安装 Docker
10. **配置一致性** — 启动脚本和测试脚本中的端口、模型名称必须一致
11. **密码保护** — 真实配置只放在项目私有 `.dev/config/`；初始化生成的嵌套 `.gitignore` 会统一忽略该状态目录内容
12. **从 "Application startup complete" 往后看报错** — 分析启动/运行时错误时，从该关键词出现在日志中的位置往后找第一个错误，后续报错通常是级联失败
13. **aisbench 服务端点配置在 config.py 中** — 不在命令行传递 `--host --port --model` 参数
14. **日志过多时只打一个 rank** — 使用 `dist.get_rank() == 0` 过滤
15. **pip 使用清华源** — 默认 PyPI 下载可能超时，安装 Python 包时使用 `pip install <pkg> -i https://pypi.tuna.tsinghua.edu.cn/simple`
16. **curl 前清理代理** — 容器内 curl 本地服务前需执行 `unset http_proxy; unset https_proxy`，否则代理可能导致 504

---

## 常见问题速查

| 现象 | 原因 | 解决 |
|------|------|------|
| curl 返回 504 | 代理未关 | `unset http_proxy; unset https_proxy` 后再 curl |
| curl 返回 000（连不上） | 服务绑定了 `--host` 外部 IP | curl 用实际 IP 而非 localhost |
| 杀进程失败 | 端口被占但 `fuser -k` 无效 | 用 `kill -9 $(fuser {port}/tcp)` 直接杀 |
| 启动时端口冲突 | 旧进程未清理 | `fuser {port}/tcp` 查占用，`fuser -k` 杀 |
| ssh_utils 报 ModuleNotFoundError | paramiko/pyyaml 未安装 | `pip install paramiko pyyaml -i https://pypi.tuna.tsinghua.edu.cn/simple` |
| pip install 超时 | 默认 PyPI 源慢 | `-i https://pypi.tuna.tsinghua.edu.cn/simple` |
| 健康检查返回 200 但推理超时 | 模型还在预热 | 等 30-60s 重试 |
| 两个配置输出不一致 | prompt 不同 或 精度 bug | 先用 `generate_curl.py` 确保 prompt 一致性 |
| DCP 推理 `<think>` token 泄漏 | `npu_attention_update` 精度不足 | 改用 float64 log-sum-exp merge |
