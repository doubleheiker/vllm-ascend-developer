#!/usr/bin/env python3
"""
vllm-ascend-developer SSH 工具 — 基于 service.yaml 的远程执行模块。

特性：
- 从 config/service.yaml 读取节点连接信息，无需 ~/.ssh/config
- 持久连接 daemon：首次连接后复用，避免反复 SSH 握手
- 密码通过 Paramiko 内存传递，不暴露在命令行
- 支持 exec / docker-exec / upload / download / wait
- 支持 service-start / service-status / service-stop 服务生命周期
- 支持 daemon-status / stop-daemon 本地 SSH daemon 管理
- docker-exec 从节点配置注入 env_vars，并合并节点级/全局 Docker 配置
- exec 与 docker-exec 分别从节点 work_dir 与 docker.work_dir 开始执行

用法：
    python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" exec standalone "hostname"
    python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" docker-exec standalone "env"
    python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" exec pd-separated.p[0] "npu-smi info"
    python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" service-start standalone
    python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" service-status standalone
    python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" service-stop standalone
    python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" daemon-status standalone
    python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" stop-daemon standalone
"""

import argparse
import json
import os
import re
import shlex
import signal
import socket
import struct
import sys
import time
import traceback
from hashlib import md5
from pathlib import Path, PurePosixPath
from threading import Thread

from path_policy import (
    PathPolicyError,
    get_config_dir,
    get_project_root,
    is_within,
    validate_download_destination,
    validate_remote_path,
    validate_upload_source,
    workspace_paths,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# 依赖检查：第三方包不在标准库中，首次使用需安装
try:
    import yaml
except ImportError:
    sys.exit(
        "ssh_utils.py 缺少依赖 pyyaml，请先安装:\n"
        "  pip install pyyaml paramiko -i https://pypi.tuna.tsinghua.edu.cn/simple"
    )
try:
    import paramiko
except ImportError:
    sys.exit(
        "ssh_utils.py 缺少依赖 paramiko，请先安装:\n"
        "  pip install pyyaml paramiko -i https://pypi.tuna.tsinghua.edu.cn/simple"
    )

# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------


def _config_file(name):
    return get_config_dir() / name


def _daemon_dir(create=False):
    runtime = workspace_paths()["runtime"]
    directory = (runtime / "ssh-daemon").resolve(strict=False)
    if not is_within(directory, runtime):
        raise PathPolicyError(
            f"ssh-daemon 目录通过符号链接越过 runtime: {directory}"
        )
    if create:
        directory.mkdir(parents=True, exist_ok=True)
    return directory

# daemon 配置
IDLE_TIMEOUT = 3600       # 空闲超时 60 分钟
HEARTBEAT_INTERVAL = 60   # 心跳间隔 60 秒
MAX_RECONNECT = 3         # 最大重连次数


# ============================================================================
# 配置加载
# ============================================================================

def load_service_config():
    """加载 service.yaml，返回 dict。"""
    path = _config_file("service.yaml")
    if not path.exists():
        die(f"找不到配置文件: {path}")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_aisbench_config():
    """加载 aisbench.yaml，返回 dict。"""
    path = _config_file("aisbench.yaml")
    if not path.exists():
        die(f"找不到配置文件: {path}")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_model_config():
    """加载 model.yaml，用于远程 SFTP 允许目录判定。"""
    path = _config_file("model.yaml")
    if not path.exists():
        die(f"找不到配置文件: {path}")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def resolve_node(node_ref):
    """解析节点引用，返回连接、宿主机目录、环境变量和 Docker 配置。

    node_ref 格式：
      - "standalone"                     → service.yaml standalone 节点
      - "pd-separated.p[0]"              → p_nodes[0]
      - "pd-separated.d[1]"              → d_nodes[1]
      - "eval"                           → aisbench.yaml eval_machine
    """
    if node_ref == "eval":
        cfg = load_aisbench_config()
        section = cfg.get("eval_machine")
        if not section:
            die("aisbench.yaml 中没有 eval_machine 配置")
        return _extract_eval_node(section)

    cfg = load_service_config()

    if node_ref == "standalone":
        section = cfg.get("standalone")
        if not section:
            die("配置中没有 standalone 节点")
        return _extract_node(
            section,
            cfg.get("docker"),
            service_port=section.get("service_port"),
            deployment_mode="standalone",
        )

    if node_ref.startswith("pd-separated."):
        rest = node_ref[len("pd-separated."):]
        if rest.startswith("p["):
            idx = _parse_index(rest, "p[")
            nodes = cfg.get("pd-separated", {}).get("p_nodes", [])
        elif rest.startswith("d["):
            idx = _parse_index(rest, "d[")
            nodes = cfg.get("pd-separated", {}).get("d_nodes", [])
        else:
            die(f"无效节点引用: {node_ref}")
        if idx >= len(nodes):
            die(f"节点索引超出范围: {node_ref} (共 {len(nodes)} 个)")
        pd_section = cfg.get("pd-separated", {})
        return _extract_node(
            nodes[idx],
            cfg.get("docker"),
            service_port=pd_section.get("service_port"),
            deployment_mode="pd-separated",
        )

    die(f"无效节点引用: {node_ref}")


def _parse_index(s, prefix):
    """从 'p[0]' 或 'd[1]' 中提取索引数字。"""
    try:
        return int(s[len(prefix):].split("]")[0])
    except (ValueError, IndexError):
        die(f"无效节点引用格式")


def _extract_node(
    n,
    docker_defaults=None,
    service_port=None,
    deployment_mode=None,
):
    """从 service.yaml 节点配置中提取连接信息，验证必填字段。"""
    docker = _merge_docker_config(docker_defaults, n.get("docker"))
    info = {
        "host": n.get("host", ""),
        "port": n.get("port", 22),
        "username": n.get("username", "root"),
        "password": n.get("password", ""),
        "work_dir": n.get("work_dir", ""),
        "env_vars": _normalize_env_vars(n.get("env_vars")),
        "docker": docker,
        "service_port": service_port,
        "deployment_mode": deployment_mode,
    }
    if not info["host"] or info["host"].startswith("<"):
        die(f"节点 host 未配置或为占位符: {info['host']}")
    if not info["password"] or info["password"].startswith("<"):
        die(f"节点 password 未配置或为占位符")
    return info


def _extract_eval_node(n):
    """从 aisbench.yaml eval_machine 提取连接信息。"""
    docker = _merge_docker_config(None, n.get("docker"))
    info = {
        "host": n.get("host", ""),
        "port": n.get("port", 22),
        "username": n.get("username", "root"),
        "password": n.get("password", ""),
        "work_dir": n.get("work_dir", ""),
        "env_vars": _normalize_env_vars(n.get("env_vars")),
        "docker": docker,
    }
    if not info["host"] or info["host"].startswith("<"):
        die(f"评测机器 host 未配置: {info['host']}")
    if not info["password"] or info["password"].startswith("<"):
        die(f"评测机器 password 未配置")
    return info


def _merge_docker_config(defaults, override):
    """合并全局与节点级 Docker 配置，并对错误形状给出中文提示。"""
    for label, value in (
        ("全局 docker", defaults),
        ("节点 docker", override),
    ):
        if value is not None and not isinstance(value, dict):
            die(f"{label} 必须是 YAML 映射")
    merged = dict(defaults or {})
    merged.update(override or {})
    return merged


def _normalize_env_vars(raw):
    """校验并规范化容器环境变量，避免把任意 Shell 片段当作变量名。"""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        die("env_vars 必须是 YAML 映射，例如 KEY: \"value\"")

    normalized = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*",
            name,
        ):
            die(f"env_vars 包含无效变量名: {name!r}")
        if value is None or isinstance(value, (dict, list)):
            die(f"env_vars.{name} 必须是字符串、数字或布尔值")
        normalized[name] = str(value)
    return normalized


def _require_remote_work_dir(value, label):
    """校验用于默认 cwd 的远程 POSIX 绝对目录。"""
    raw = str(value or "").strip()
    path = PurePosixPath(raw)
    if (
        not raw
        or raw.startswith("<")
        or any(char in raw for char in "\x00\n\r")
        or not path.is_absolute()
        or ".." in path.parts
        or path == PurePosixPath("/")
    ):
        die(f"{label} 必须是非根目录的远程绝对路径，当前值: {raw!r}")
    return str(path)


# ============================================================================
# Daemon 信息文件
# ============================================================================

def _daemon_key(node_ref):
    """安全的文件名 key（用 md5 避免特殊字符问题）。"""
    return md5(node_ref.encode()).hexdigest()


def _daemon_info_file(node_ref):
    daemon_dir = _daemon_dir()
    candidate = (
        daemon_dir / f"{_daemon_key(node_ref)}.json"
    ).resolve(strict=False)
    if not is_within(candidate, daemon_dir):
        raise PathPolicyError(
            f"daemon 信息文件通过符号链接越过 runtime: {candidate}"
        )
    return candidate


def read_daemon_info(node_ref):
    """读取 daemon 的 pid 和端口，不存在则返回 None。"""
    f = _daemon_info_file(node_ref)
    if not f.exists():
        return None
    try:
        with open(f) as fh:
            return json.load(fh)
    except (json.JSONDecodeError, IOError):
        return None


def write_daemon_info(node_ref, pid, port):
    _daemon_dir(create=True)
    with open(_daemon_info_file(node_ref), "w") as f:
        json.dump({"pid": pid, "port": port, "node_ref": node_ref}, f)


def remove_daemon_info(node_ref):
    f = _daemon_info_file(node_ref)
    if f.exists():
        f.unlink(missing_ok=True)


# ============================================================================
# Socket 通信协议（4 字节大端长度前缀 + JSON 负载）
# ============================================================================

def _recv_exactly(sock, n):
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError("连接断开")
        data += chunk
    return data


def _send_msg(sock, msg):
    payload = json.dumps(msg).encode("utf-8")
    sock.sendall(struct.pack(">I", len(payload)) + payload)


def _recv_msg(sock):
    header = _recv_exactly(sock, 4)
    length = struct.unpack(">I", header)[0]
    payload = _recv_exactly(sock, length)
    return json.loads(payload.decode("utf-8"))


# ============================================================================
# Daemon 子进程
# ============================================================================

def daemon_main(node_ref):
    """子进程入口：建立 Paramiko 长连接，监听本地端口，处理请求。"""
    node = resolve_node(node_ref)
    port = node["port"]

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=node["host"],
        port=node["port"],
        username=node["username"],
        password=node["password"],
        timeout=30,
    )

    # 监听随机端口
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.bind(("127.0.0.1", 0))
    server_sock.listen(5)
    listen_port = server_sock.getsockname()[1]
    write_daemon_info(node_ref, os.getpid(), listen_port)

    last_activity = time.time()

    def heartbeat():
        nonlocal last_activity
        while True:
            time.sleep(HEARTBEAT_INTERVAL)
            try:
                transport = client.get_transport()
                if transport and transport.is_active():
                    transport.send_ignore()
                    if time.time() - last_activity > IDLE_TIMEOUT:
                        server_sock.close()
                        client.close()
                        remove_daemon_info(node_ref)
                        os._exit(0)
                else:
                    # 尝试重连
                    for attempt in range(MAX_RECONNECT):
                        try:
                            client.connect(
                                hostname=node["host"],
                                port=port,
                                username=node["username"],
                                password=node["password"],
                                timeout=30,
                            )
                            break
                        except Exception:
                            if attempt == MAX_RECONNECT - 1:
                                remove_daemon_info(node_ref)
                                os._exit(1)
                            time.sleep(5)
            except Exception:
                pass

    hb = Thread(target=heartbeat, daemon=True)
    hb.start()

    try:
        while True:
            server_sock.settimeout(IDLE_TIMEOUT)
            try:
                conn, _ = server_sock.accept()
            except socket.timeout:
                server_sock.close()
                client.close()
                remove_daemon_info(node_ref)
                os._exit(0)

            last_activity = time.time()
            try:
                msg = _recv_msg(conn)
            except Exception:
                conn.close()
                continue

            action = msg.get("action", "")
            resp = {"success": False, "stdout": "", "stderr": "", "exit_code": -1}

            if action == "execute":
                cmd = msg.get("command", "")
                try:
                    _, stdout, stderr = client.exec_command(cmd, timeout=msg.get("timeout", 120))
                    resp["stdout"] = stdout.read().decode("utf-8", errors="replace")
                    resp["stderr"] = stderr.read().decode("utf-8", errors="replace")
                    resp["exit_code"] = stdout.channel.recv_exit_status()
                    resp["success"] = resp["exit_code"] == 0
                except Exception as e:
                    resp["stderr"] = str(e)

            elif action == "upload":
                try:
                    sftp = client.open_sftp()
                    sftp.put(msg["local_path"], msg["remote_path"])
                    sftp.close()
                    resp["success"] = True
                    resp["exit_code"] = 0
                except Exception as e:
                    resp["stderr"] = str(e)

            elif action == "download":
                try:
                    sftp = client.open_sftp()
                    sftp.get(msg["remote_path"], msg["local_path"])
                    sftp.close()
                    resp["success"] = True
                    resp["exit_code"] = 0
                except Exception as e:
                    resp["stderr"] = str(e)

            elif action == "shutdown":
                resp["success"] = True
                _send_msg(conn, resp)
                conn.close()
                server_sock.close()
                client.close()
                remove_daemon_info(node_ref)
                os._exit(0)

            else:
                resp["stderr"] = f"未知 action: {action}"

            try:
                _send_msg(conn, resp)
            except Exception:
                pass
            finally:
                conn.close()

    except KeyboardInterrupt:
        pass
    finally:
        try:
            server_sock.close()
        except Exception:
            pass
        try:
            client.close()
        except Exception:
            pass
        remove_daemon_info(node_ref)


# ============================================================================
# Daemon 管理
# ============================================================================

def start_daemon(node_ref):
    """启动 daemon 子进程并等待就绪。"""
    pid = os.fork()
    if pid == 0:
        # 子进程
        os.setsid()
        daemon_main(node_ref)
        os._exit(0)
    else:
        # 父进程等待 daemon 就绪
        for _ in range(30):
            info = read_daemon_info(node_ref)
            if info and info.get("pid"):
                time.sleep(0.2)
                return
            time.sleep(0.5)
        die("daemon 启动超时")


def ensure_daemon(node_ref):
    """确保 daemon 进程存活，否则启动。"""
    info = read_daemon_info(node_ref)
    if info:
        pid = info.get("pid", 0)
        try:
            os.kill(pid, 0)
            return  # 存活
        except OSError:
            remove_daemon_info(node_ref)
    start_daemon(node_ref)


def stop_daemon(node_ref):
    """发送 shutdown 信号关闭 daemon。"""
    info = read_daemon_info(node_ref)
    if not info:
        print_json({"success": True, "message": "daemon 未运行"})
        return
    try:
        sock = socket.create_connection(("127.0.0.1", info["port"]), timeout=5)
        _send_msg(sock, {"action": "shutdown"})
        resp = _recv_msg(sock)
        sock.close()
        print_json({"success": resp.get("success", False), "message": "daemon 已关闭"})
    except Exception as e:
        # 强制清理
        try:
            os.kill(info["pid"], signal.SIGTERM)
        except OSError:
            pass
        remove_daemon_info(node_ref)
        print_json({"success": True, "message": f"daemon 强制终止: {e}"})


def daemon_status(node_ref):
    info = read_daemon_info(node_ref)
    if not info:
        print_json({"running": False, "message": "daemon 未运行"})
        return
    pid = info.get("pid", 0)
    try:
        os.kill(pid, 0)
        print_json({"running": True, "pid": pid, "port": info.get("port")})
    except OSError:
        remove_daemon_info(node_ref)
        print_json({"running": False, "message": "daemon 已退出"})


# ============================================================================
# 远程操作接口
# ============================================================================


def _remote_transfer_roots(node, operation):
    """按节点与模型配置生成 SFTP 允许目录。"""
    model = load_model_config()
    work_dir = node.get("work_dir", "")
    ascend_source = model.get("vllm_ascend_source", "")

    if operation == "上传":
        # 上传允许写 vllm-ascend 源码或节点工作目录，绝不写 vLLM 上游。
        return [ascend_source, work_dir]
    return [
        work_dir,
        ascend_source,
        model.get("vllm_source", ""),
        model.get("model_path", ""),
    ]


def exec_command(node_ref, command, timeout=600):
    """通过 daemon 执行远程命令。"""
    ensure_daemon(node_ref)
    info = read_daemon_info(node_ref)
    if not info:
        die("无法获取 daemon 信息")

    sock = socket.create_connection(("127.0.0.1", info["port"]), timeout=30)
    sock.settimeout(timeout + 300)  # 等待命令完成 + 额外缓冲
    try:
        _send_msg(sock, {"action": "execute", "command": command, "timeout": timeout})
        resp = _recv_msg(sock)
        return resp
    finally:
        sock.close()


def _with_execution_context(response, node_ref, scope, cwd):
    """为远程操作响应增加非敏感执行上下文。"""
    result = dict(response or {})
    result.update({"node_ref": node_ref, "scope": scope, "cwd": cwd})
    return result


def build_host_exec_command(node, command):
    """将宿主机命令固定从节点 work_dir 开始执行。"""
    if not isinstance(command, str) or not command.strip():
        die("exec 的宿主机命令不能为空")
    work_dir = _require_remote_work_dir(
        node.get("work_dir"),
        "节点 work_dir",
    )
    return f"cd {shlex.quote(work_dir)} || exit 1; {command}"


def host_exec_command(node_ref, command, timeout=600):
    """从节点 work_dir 执行宿主机命令；容器命令改走 docker-exec。"""
    if re.search(
        r"(?:^|[\s;&|()'\"`])(?:[^\s;&|()'\"`]*/)?"
        r"docker\s+(?:container\s+)?exec(?:\s|$)",
        command,
    ):
        die(
            "exec 只允许宿主机命令；检测到 docker exec，"
            "请改用 ssh_utils.py docker-exec <node> <command>"
        )
    node = resolve_node(node_ref)
    work_dir = _require_remote_work_dir(
        node.get("work_dir"),
        "节点 work_dir",
    )
    host_command = build_host_exec_command(node, command)
    response = exec_command(node_ref, host_command, timeout)
    return _with_execution_context(
        response,
        node_ref,
        "host",
        work_dir,
    )


def build_docker_exec_command(node, command):
    """根据已解析节点构造宿主机 docker exec 命令。

    参数通过 shlex.quote 逐项转义，env_vars 不经过模型或远程 Shell 拼接。
    """
    docker = node.get("docker") or {}
    container_name = docker.get("name", "")
    if not container_name or str(container_name).startswith("<"):
        die("当前节点的 docker.name 未配置或为占位符")
    if not isinstance(command, str) or not command.strip():
        die("docker-exec 的容器命令不能为空")

    argv = ["docker", "exec"]
    for name, value in (node.get("env_vars") or {}).items():
        argv.extend(["--env", f"{name}={value}"])

    work_dir = _require_remote_work_dir(
        docker.get("work_dir"),
        "docker.work_dir",
    )
    argv.extend(["--workdir", work_dir])

    argv.extend([str(container_name), "bash", "-c", command])
    return " ".join(shlex.quote(part) for part in argv)


def docker_exec_command(node_ref, command, timeout=600):
    """在节点容器中执行命令，并注入 service.yaml 的 env_vars。"""
    node = resolve_node(node_ref)
    work_dir = _require_remote_work_dir(
        (node.get("docker") or {}).get("work_dir"),
        "docker.work_dir",
    )
    host_command = build_docker_exec_command(node, command)
    response = exec_command(node_ref, host_command, timeout)
    return _with_execution_context(
        response,
        node_ref,
        "container",
        work_dir,
    )


# ============================================================================
# vLLM 服务生命周期
# ============================================================================


def _require_remote_file_path(value, label):
    """校验服务生命周期使用的容器内绝对文件路径。"""
    raw = str(value or "").strip()
    path = PurePosixPath(raw)
    if (
        not raw
        or raw.startswith("<")
        or any(char in raw for char in "\x00\n\r")
        or not path.is_absolute()
        or ".." in path.parts
        or path == PurePosixPath("/")
    ):
        die(f"{label} 必须是远程绝对文件路径，当前值: {raw!r}")
    return str(path)


def _normalize_service_port(value, label, required=True):
    """校验端口配置；可选端口为空时返回 None。"""
    if value in (None, ""):
        if required:
            die(f"{label} 未配置")
        return None
    if isinstance(value, bool):
        die(f"{label} 必须是 1-65535 的整数")
    try:
        port = int(value)
    except (TypeError, ValueError):
        die(f"{label} 必须是 1-65535 的整数，当前值: {value!r}")
    if not 1 <= port <= 65535:
        die(f"{label} 必须是 1-65535 的整数，当前值: {value!r}")
    return port


def resolve_service_runtime(node):
    """将节点配置解析为可执行的服务生命周期配置。"""
    if node.get("deployment_mode") not in {"standalone", "pd-separated"}:
        die("当前节点不是 vLLM 服务节点")

    docker = node.get("docker") or {}
    work_dir = _require_remote_work_dir(
        docker.get("work_dir"),
        "docker.work_dir",
    )
    startup_script = _require_remote_file_path(
        docker.get("startup_script"),
        "docker.startup_script",
    )
    log_file = _require_remote_file_path(
        docker.get("log_file"),
        "docker.log_file",
    )
    pid_file = _require_remote_file_path(
        docker.get("pid_file") or f"{log_file}.pgid",
        "docker.pid_file",
    )

    proxy_script_raw = str(docker.get("proxy_script") or "").strip()
    if node["deployment_mode"] == "pd-separated" and not proxy_script_raw:
        die("PD 分离模式必须配置 docker.proxy_script")
    proxy_script = (
        _require_remote_file_path(proxy_script_raw, "docker.proxy_script")
        if proxy_script_raw
        else ""
    )

    return {
        "deployment_mode": node["deployment_mode"],
        "container_name": docker.get("name", ""),
        "work_dir": work_dir,
        "startup_script": startup_script,
        "proxy_script": proxy_script,
        "log_file": log_file,
        "pid_file": pid_file,
        "service_port": _normalize_service_port(
            node.get("service_port"),
            "service_port",
        ),
        "proxy_port": _normalize_service_port(
            docker.get("proxy_port"),
            "docker.proxy_port",
            required=False,
        ),
    }


def _service_process_probe():
    """返回不会匹配自身 grep 的 vLLM 活进程检查命令。"""
    return (
        "ps -eo stat=,args= | "
        "grep -E '[V]LLM::|[v]llm[[:space:]]+serve' | "
        "grep -Ev '^[[:space:]]*Z'"
    )


def _service_group_owned_probe(runtime):
    """确认 pid file 指向的活进程组仍属于当前服务。"""
    startup = shlex.quote(runtime["startup_script"])
    proxy = shlex.quote(runtime["proxy_script"])
    return (
        "ps -eo pgid=,stat=,args= | "
        f"awk -v g=\"$pgid\" -v startup={startup} -v proxy={proxy} "
        "'$1 == g && $2 !~ /^Z/ { "
        "line=$0; if (index(line, startup) || "
        "(proxy != \"\" && index(line, proxy)) || "
        "line ~ /VLLM::|vllm[[:space:]]+serve/) found=1 "
        "} END { exit !found }'"
    )


def build_service_start_command(node):
    """构造容器内服务启动命令，由独立 session/PGID 管理整棵进程。"""
    runtime = resolve_service_runtime(node)
    pid_file = shlex.quote(runtime["pid_file"])
    log_file = shlex.quote(runtime["log_file"])
    startup_script = shlex.quote(runtime["startup_script"])
    proxy_script = shlex.quote(runtime["proxy_script"])
    probe = _service_process_probe()
    group_owned = _service_group_owned_probe(runtime)

    # 位置参数避免在多层 shell 中重复拼接用户配置路径。
    inner = (
        'printf "%s\\n" "$$" > "$1" || exit 70; '
        'if [ -n "$2" ]; then bash "$2" & fi; '
        'exec bash "$3"'
    )
    return (
        "command -v setsid >/dev/null 2>&1 || "
        "{ echo 'SERVICE_START=missing-setsid' >&2; exit 127; }; "
        f"test -f {startup_script} || "
        "{ echo 'SERVICE_START=missing-startup-script' >&2; exit 2; }; "
        f"if [ -n {proxy_script} ]; then test -f {proxy_script} || "
        "{ echo 'SERVICE_START=missing-proxy-script' >&2; exit 2; }; fi; "
        f"if [ -f {pid_file} ]; then "
        f"pgid=$(sed -n '1p' {pid_file}); "
        "if printf '%s' \"$pgid\" | grep -Eq '^[0-9]+$' && "
        "[ \"$pgid\" -gt 1 ] && "
        "ps -eo pgid=,stat= | awk -v g=\"$pgid\" "
        "'$1 == g && $2 !~ /^Z/ { found=1 } END { exit !found }' && "
        f"{group_owned}; then "
        "echo \"SERVICE_START=already-running PGID=$pgid\" >&2; exit 3; "
        f"else rm -f -- {pid_file}; fi; fi; "
        f"if {probe} >/dev/null 2>&1; then "
        "echo 'SERVICE_START=untracked-vllm-process' >&2; exit 4; fi; "
        f": > {log_file} || "
        "{ echo 'SERVICE_START=log-not-writable' >&2; exit 2; }; "
        f"setsid bash -c {shlex.quote(inner)} service-wrapper "
        f"{pid_file} {proxy_script} {startup_script} "
        f">> {log_file} 2>&1 < /dev/null & "
        "launcher_pid=$!; pgid=''; "
        "i=0; while [ \"$i\" -lt 50 ]; do "
        f"if [ -s {pid_file} ]; then pgid=$(sed -n '1p' {pid_file}); break; fi; "
        "kill -0 \"$launcher_pid\" 2>/dev/null || break; "
        "sleep 0.1; i=$((i + 1)); done; "
        "if ! printf '%s' \"$pgid\" | grep -Eq '^[0-9]+$' || "
        "[ \"$pgid\" -le 1 ]; then "
        "echo 'SERVICE_START=pidfile-not-created' >&2; exit 5; fi; "
        "if ! kill -0 \"-$pgid\" 2>/dev/null; then "
        "echo \"SERVICE_START=exited-early PGID=$pgid\" >&2; exit 6; fi; "
        "echo \"SERVICE_STARTED_PGID=$pgid\""
    )


def build_service_stop_command(node, grace_period=30):
    """构造容器内服务停止命令：TERM -> 有界等待 -> KILL。"""
    runtime = resolve_service_runtime(node)
    if isinstance(grace_period, bool):
        die("grace_period 必须是非负整数")
    try:
        grace_period = int(grace_period)
    except (TypeError, ValueError):
        die("grace_period 必须是非负整数")
    if not 0 <= grace_period <= 300:
        die("grace_period 必须在 0-300 秒之间")

    pid_file = shlex.quote(runtime["pid_file"])
    probe = _service_process_probe()
    group_owned = _service_group_owned_probe(runtime)
    group_active = (
        "ps -eo pgid=,stat= | awk -v g=\"$pgid\" "
        "'$1 == g && $2 !~ /^Z/ { found=1 } END { exit !found }'"
    )
    return (
        "group_active() { " + group_active + "; }; "
        f"if [ ! -f {pid_file} ]; then "
        f"if {probe} >/dev/null 2>&1; then "
        "echo 'SERVICE_STOP=untracked' >&2; exit 4; "
        "else echo 'SERVICE_STOP=already-stopped'; exit 0; fi; fi; "
        f"pgid=$(sed -n '1p' {pid_file}); "
        "if ! printf '%s' \"$pgid\" | grep -Eq '^[0-9]+$' || "
        "[ \"$pgid\" -le 1 ]; then "
        "echo 'SERVICE_STOP=invalid-pgid' >&2; exit 2; fi; "
        f"if group_active && ! {group_owned}; then "
        "echo 'SERVICE_STOP=ownership-mismatch' >&2; exit 6; fi; "
        "if group_active; then "
        'kill -TERM -- "-$pgid" 2>/dev/null || true; '
        f"remaining={grace_period}; while group_active && "
        "[ \"$remaining\" -gt 0 ]; do "
        "sleep 1; remaining=$((remaining - 1)); done; "
        "if group_active; then "
        'kill -KILL -- "-$pgid" 2>/dev/null || true; '
        "sleep 1; fi; fi; "
        "if group_active; then "
        "echo \"SERVICE_STOP=residual PGID=$pgid\" >&2; "
        "ps -eo pid=,ppid=,pgid=,sid=,stat=,args= | "
        "awk -v g=\"$pgid\" '$3 == g && $5 !~ /^Z/'; exit 5; fi; "
        "zombie_count=$(ps -eo pgid=,stat= | awk -v g=\"$pgid\" "
        "'$1 == g && $2 ~ /^Z/ { count++ } END { print count+0 }'); "
        "if [ \"$zombie_count\" -gt 0 ]; then "
        "echo \"SERVICE_ZOMBIES=$zombie_count PGID=$pgid\"; fi; "
        f"rm -f -- {pid_file}; "
        f"if {probe} >/dev/null 2>&1; then "
        "echo 'SERVICE_STOP=untracked' >&2; exit 4; fi; "
        "echo \"SERVICE_STOP=clean PGID=$pgid\""
    )


def build_service_status_command(node):
    """构造只读服务状态命令，区分跟踪、残留与已停止。"""
    runtime = resolve_service_runtime(node)
    pid_file = shlex.quote(runtime["pid_file"])
    probe = _service_process_probe()
    group_owned = _service_group_owned_probe(runtime)
    return (
        f"if [ -f {pid_file} ]; then pgid=$(sed -n '1p' {pid_file}); "
        "if printf '%s' \"$pgid\" | grep -Eq '^[0-9]+$' && "
        "[ \"$pgid\" -gt 1 ] && "
        "ps -eo pgid=,stat= | awk -v g=\"$pgid\" "
        "'$1 == g && $2 !~ /^Z/ { found=1 } END { exit !found }' && "
        f"{group_owned}; then "
        "echo \"SERVICE_STATUS=running PGID=$pgid\"; "
        "else echo \"SERVICE_STATUS=stale-pidfile PGID=$pgid\"; fi; "
        f"elif {probe} >/dev/null 2>&1; then "
        "echo 'SERVICE_STATUS=untracked'; "
        "else echo 'SERVICE_STATUS=stopped'; fi"
    )


def _service_ports(runtime):
    """返回去重后的服务/代理端口与标签。"""
    ports = [("SERVICE", runtime["service_port"])]
    if (
        runtime.get("proxy_port") is not None
        and runtime["proxy_port"] != runtime["service_port"]
    ):
        ports.append(("PROXY", runtime["proxy_port"]))
    return ports


def _build_service_port_command(runtime, release=False):
    """构造宿主机端口检查；停服务时才允许 fuser 作为兜底。"""
    parts = [
        "command -v fuser >/dev/null 2>&1 || "
        "{ echo 'SERVICE_PORT_CHECK=missing-fuser' >&2; exit 127; }"
    ]
    for label, port in _service_ports(runtime):
        marker = f"{label}_PORT_{port}"
        if release:
            parts.append(
                f"if fuser -s {port}/tcp; then "
                f"fuser -k {port}/tcp >/dev/null 2>&1 || true; sleep 1; fi"
            )
        parts.append(
            f"if fuser -s {port}/tcp; then echo '{marker}=busy'; "
            f"else echo '{marker}=clean'; fi"
        )
    return "; ".join(parts)


def _ports_are_clean(response):
    return bool(response.get("success")) and not re.search(
        r"(?:SERVICE|PROXY)_PORT_[0-9]+=busy",
        response.get("stdout", ""),
    )


def inspect_service_ports(node_ref, node=None):
    """在宿主机检查服务端口，不修改进程。"""
    node = node or resolve_node(node_ref)
    runtime = resolve_service_runtime(node)
    return host_exec_command(
        node_ref,
        _build_service_port_command(runtime, release=False),
        timeout=60,
    )


def release_service_port(node_ref, node=None):
    """进程组停止后，在宿主机释放并复查端口。"""
    node = node or resolve_node(node_ref)
    runtime = resolve_service_runtime(node)
    return host_exec_command(
        node_ref,
        _build_service_port_command(runtime, release=True),
        timeout=90,
    )


def start_service(node_ref):
    """先检查宿主机端口，再在容器内启动可跟踪的进程组。"""
    node = resolve_node(node_ref)
    runtime = resolve_service_runtime(node)
    port_result = inspect_service_ports(node_ref, node=node)
    result = {
        "action": "service-start",
        "node_ref": node_ref,
        "execution_scopes": ["host"],
        "pid_file": runtime["pid_file"],
        "ports": port_result,
    }
    if not _ports_are_clean(port_result):
        result.update(
            {
                "success": False,
                "stderr": "服务端口未通过启动前检查，未执行容器启动",
            }
        )
        return result

    container_result = docker_exec_command(
        node_ref,
        build_service_start_command(node),
        timeout=120,
    )
    result.update(
        {
            "success": bool(container_result.get("success")),
            "execution_scopes": ["host", "container"],
            "container": container_result,
        }
    )
    return result


def service_status(node_ref):
    """同时读取容器进程组与宿主机端口状态。"""
    node = resolve_node(node_ref)
    runtime = resolve_service_runtime(node)
    container_result = docker_exec_command(
        node_ref,
        build_service_status_command(node),
        timeout=60,
    )
    port_result = inspect_service_ports(node_ref, node=node)
    return {
        "success": bool(
            container_result.get("success") and port_result.get("success")
        ),
        "action": "service-status",
        "node_ref": node_ref,
        "execution_scopes": ["container", "host"],
        "pid_file": runtime["pid_file"],
        "container": container_result,
        "ports": port_result,
    }


def stop_service(node_ref, grace_period=30):
    """先在容器内停止跟踪的进程组，再从宿主机按端口兜底并复查。"""
    node = resolve_node(node_ref)
    runtime = resolve_service_runtime(node)
    container_result = docker_exec_command(
        node_ref,
        build_service_stop_command(node, grace_period=grace_period),
        timeout=int(grace_period) + 120,
    )
    container_output = (
        container_result.get("stdout", "")
        + "\n"
        + container_result.get("stderr", "")
    )
    container_clean = bool(container_result.get("success")) and not re.search(
        r"SERVICE_STOP=(?:untracked|invalid-pgid|ownership-mismatch|residual)",
        container_output,
    )
    # 只有容器内跟踪进程组已干净时，才允许宿主机端口杀进程兜底。
    # untracked 或 residual 时改为只读检查，避免隐藏归属不明的服务。
    if container_clean:
        port_result = release_service_port(node_ref, node=node)
        host_action = "release-and-verify"
    else:
        port_result = inspect_service_ports(node_ref, node=node)
        host_action = "inspect-only"
    return {
        "success": bool(container_clean and _ports_are_clean(port_result)),
        "action": "service-stop",
        "node_ref": node_ref,
        "execution_scopes": ["container", "host"],
        "pid_file": runtime["pid_file"],
        "container": container_result,
        "ports": port_result,
        "host_action": host_action,
    }


def upload_file(node_ref, local_path, remote_path):
    """上传文件到远程节点。"""
    node = resolve_node(node_ref)
    work_dir = _require_remote_work_dir(
        node.get("work_dir"),
        "节点 work_dir",
    )
    local_path = str(
        validate_upload_source(
            local_path,
            project_root=get_project_root(),
        )
    )
    remote_path = validate_remote_path(
        remote_path,
        _remote_transfer_roots(node, "上传"),
        "上传",
        base_dir=work_dir,
    )
    ensure_daemon(node_ref)
    info = read_daemon_info(node_ref)
    sock = socket.create_connection(("127.0.0.1", info["port"]), timeout=30)
    try:
        _send_msg(sock, {"action": "upload", "local_path": local_path, "remote_path": remote_path})
        return _with_execution_context(
            _recv_msg(sock),
            node_ref,
            "host",
            work_dir,
        )
    finally:
        sock.close()


def download_file(node_ref, remote_path, local_path):
    """从远程节点下载文件。"""
    node = resolve_node(node_ref)
    work_dir = _require_remote_work_dir(
        node.get("work_dir"),
        "节点 work_dir",
    )
    remote_path = validate_remote_path(
        remote_path,
        _remote_transfer_roots(node, "下载"),
        "下载",
        base_dir=work_dir,
    )
    local_path = validate_download_destination(
        local_path,
        project_root=get_project_root(),
    )
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path = str(local_path)
    ensure_daemon(node_ref)
    info = read_daemon_info(node_ref)
    sock = socket.create_connection(("127.0.0.1", info["port"]), timeout=30)
    try:
        _send_msg(sock, {"action": "download", "remote_path": remote_path, "local_path": local_path})
        return _with_execution_context(
            _recv_msg(sock),
            node_ref,
            "host",
            work_dir,
        )
    finally:
        sock.close()


def wait_for_keyword(
    node_ref,
    log_file,
    keyword,
    interval=30,
    timeout=3600,
    scope="host",
):
    """轮询远程日志文件，等待关键词出现或超时。

    返回 {"matched": true/false, "line": "匹配行", "elapsed": 秒数}
    """
    if scope not in {"host", "container"}:
        die("wait scope 只能是 host 或 container")
    node = resolve_node(node_ref)
    if scope == "host":
        cwd = _require_remote_work_dir(
            node.get("work_dir"),
            "节点 work_dir",
        )
        run_command = host_exec_command
    else:
        cwd = _require_remote_work_dir(
            (node.get("docker") or {}).get("work_dir"),
            "docker.work_dir",
        )
        run_command = docker_exec_command

    started = time.time()
    while True:
        elapsed = time.time() - started
        if elapsed > timeout:
            return _with_execution_context(
                {
                    "success": True,
                    "matched": False,
                    "line": "",
                    "elapsed": elapsed,
                },
                node_ref,
                scope,
                cwd,
            )

        resp = run_command(
            node_ref,
            "grep -m 1 -- "
            f"{shlex.quote(keyword)} {shlex.quote(log_file)} "
            "2>/dev/null || echo __NOT_FOUND__",
            timeout=60,
        )
        if resp.get("success") and "__NOT_FOUND__" not in resp.get("stdout", ""):
            line = resp["stdout"].strip()
            if line:
                return _with_execution_context(
                    {
                        "success": True,
                        "matched": True,
                        "line": line,
                        "elapsed": elapsed,
                    },
                    node_ref,
                    scope,
                    cwd,
                )

        time.sleep(interval)


# ============================================================================
# 输出辅助
# ============================================================================

def print_json(data):
    print(json.dumps(data, ensure_ascii=False))


def die(msg):
    print_json({"success": False, "stderr": msg})
    sys.exit(1)


# ============================================================================
# CLI
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="vllm-ascend-developer SSH 工具")
    parser.add_argument("--project-root", help="用户项目根目录")
    parser.add_argument("--config-dir", help="配置目录，默认优先项目私有配置")
    sub = parser.add_subparsers(dest="action", required=True)

    # exec
    p_exec = sub.add_parser(
        "exec",
        help="从节点 work_dir 执行远程宿主机命令",
    )
    p_exec.add_argument("node_ref", help="节点引用: standalone, pd-separated.p[0], pd-separated.d[0]")
    p_exec.add_argument("command", help="要执行的命令")
    p_exec.add_argument("--timeout", type=int, default=600, help="超时秒数，默认 600（10分钟）")

    # docker-exec
    p_docker_exec = sub.add_parser(
        "docker-exec",
        help="从 docker.work_dir 执行容器命令，并注入 env_vars",
    )
    p_docker_exec.add_argument(
        "node_ref",
        help="节点引用: standalone, pd-separated.p[0], pd-separated.d[0], eval",
    )
    p_docker_exec.add_argument("command", help="要在容器内执行的命令")
    p_docker_exec.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="超时秒数，默认 600（10分钟）",
    )

    # vLLM service lifecycle
    p_service_start = sub.add_parser(
        "service-start",
        help="检查宿主机端口后，在容器内启动并记录服务进程组",
    )
    p_service_start.add_argument("node_ref")

    p_service_status = sub.add_parser(
        "service-status",
        help="查看容器服务进程组和宿主机端口状态",
    )
    p_service_status.add_argument("node_ref")

    p_service_stop = sub.add_parser(
        "service-stop",
        help="停止容器服务进程组，再从宿主机释放并复查端口",
    )
    p_service_stop.add_argument("node_ref")
    p_service_stop.add_argument(
        "--grace-period",
        type=int,
        default=30,
        help="TERM 后等待秒数，默认 30，最大 300",
    )

    # SSH daemon lifecycle. status/stop 保留一个兼容周期。
    p_daemon_status = sub.add_parser("daemon-status", help="查看 SSH daemon 状态")
    p_daemon_status.add_argument("node_ref")
    p_stop_daemon = sub.add_parser("stop-daemon", help="停止 SSH daemon")
    p_stop_daemon.add_argument("node_ref")
    p_status = sub.add_parser("status", help="daemon-status 的兼容别名")
    p_status.add_argument("node_ref")
    p_stop = sub.add_parser("stop", help="stop-daemon 的兼容别名")
    p_stop.add_argument("node_ref")

    # upload
    p_up = sub.add_parser(
        "upload",
        help="上传文件；相对远程路径基于节点 work_dir",
    )
    p_up.add_argument("node_ref")
    p_up.add_argument("local_path")
    p_up.add_argument("remote_path")

    # download
    p_down = sub.add_parser(
        "download",
        help="下载文件；相对远程路径基于节点 work_dir",
    )
    p_down.add_argument("node_ref")
    p_down.add_argument("remote_path")
    p_down.add_argument("local_path")

    # wait
    p_wait = sub.add_parser("wait", help="轮询日志等关键词")
    p_wait.add_argument("node_ref")
    p_wait.add_argument("log_file", help="远程日志文件路径")
    p_wait.add_argument("keyword", help="完成关键词")
    p_wait.add_argument(
        "--scope",
        choices=("host", "container"),
        default="host",
        help="日志所在范围，默认 host",
    )
    p_wait.add_argument("--interval", type=int, default=30, help="轮询间隔秒数，默认 30")
    p_wait.add_argument("--timeout", type=int, default=3600, help="最大等待秒数，默认 3600")

    args = parser.parse_args()

    if args.project_root:
        os.environ["VLLM_ASCEND_PROJECT_ROOT"] = str(
            Path(args.project_root).resolve(strict=False)
        )
    if args.config_dir:
        os.environ["VLLM_ASCEND_CONFIG_DIR"] = str(
            Path(args.config_dir).resolve(strict=False)
        )

    try:
        if args.action == "exec":
            print_json(
                host_exec_command(
                    args.node_ref,
                    args.command,
                    args.timeout,
                )
            )
        elif args.action == "docker-exec":
            print_json(
                docker_exec_command(
                    args.node_ref,
                    args.command,
                    args.timeout,
                )
            )
        elif args.action == "service-start":
            print_json(start_service(args.node_ref))
        elif args.action == "service-status":
            print_json(service_status(args.node_ref))
        elif args.action == "service-stop":
            print_json(
                stop_service(
                    args.node_ref,
                    grace_period=args.grace_period,
                )
            )
        elif args.action in {"daemon-status", "status"}:
            daemon_status(args.node_ref)
        elif args.action in {"stop-daemon", "stop"}:
            stop_daemon(args.node_ref)
        elif args.action == "upload":
            print_json(upload_file(args.node_ref, args.local_path, args.remote_path))
        elif args.action == "download":
            print_json(download_file(args.node_ref, args.remote_path, args.local_path))
        elif args.action == "wait":
            print_json(
                wait_for_keyword(
                    args.node_ref,
                    args.log_file,
                    args.keyword,
                    args.interval,
                    args.timeout,
                    args.scope,
                )
            )
    except PathPolicyError as exc:
        die(str(exc))


if __name__ == "__main__":
    main()
