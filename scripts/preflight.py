#!/usr/bin/env python3
"""在已配置容器中执行只读的 Python 导入检查。"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path

import ssh_utils


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


PROBE_CODE = r'''
import contextlib
import importlib
import io
import json
import os
import sys

expected = json.loads(sys.argv[1])


def is_within(path, root):
    if not path or not root:
        return None
    try:
        return os.path.commonpath(
            (os.path.realpath(path), os.path.realpath(root))
        ) == os.path.realpath(root)
    except (OSError, ValueError):
        return False


result = {
    "python": sys.executable,
    "python_version": sys.version.split()[0],
    "pythonpath": os.environ.get("PYTHONPATH", ""),
    "sys_path": sys.path,
    "expected_sources": {
        name: {"path": path, "exists": bool(path and os.path.isdir(path))}
        for name, path in expected.items()
    },
    "imports": {},
}

for name in ("vllm", "vllm_ascend"):
    output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            module = importlib.import_module(name)
        module_file = getattr(module, "__file__", None)
        module_version = getattr(module, "__version__", None)
        result["imports"][name] = {
            "ok": True,
            "file": module_file,
            "version": str(module_version) if module_version is not None else None,
            "source_match": is_within(module_file, expected.get(name)),
            "import_output": output.getvalue()[-4000:],
        }
    except Exception as exc:
        result["imports"][name] = {
            "ok": False,
            "file": None,
            "version": None,
            "source_match": False if expected.get(name) else None,
            "error": "%s: %s" % (type(exc).__name__, exc),
            "import_output": output.getvalue()[-4000:],
        }

result["import_ready"] = all(
    item["ok"] for item in result["imports"].values()
)
print(json.dumps(result, ensure_ascii=False))
'''.strip()


def build_probe_command(container_python, vllm_source, vllm_ascend_source):
    """构造固定、只读且经过 Shell 转义的容器检查命令。"""
    expected_sources = json.dumps(
        {
            "vllm": vllm_source or "",
            "vllm_ascend": vllm_ascend_source or "",
        },
        ensure_ascii=False,
    )
    return " ".join(
        shlex.quote(part)
        for part in (container_python or "python3", "-c", PROBE_CODE, expected_sources)
    )


def run_preflight(node_ref, timeout=120):
    """复用 ssh_utils 的 docker-exec，在目标容器中执行一次导入检查。"""
    node = ssh_utils.resolve_node(node_ref)
    docker = node.get("docker") or {}
    model = ssh_utils.load_model_config() or {}
    command = build_probe_command(
        docker.get("container_python") or "python3",
        model.get("container_vllm_source") or model.get("vllm_source"),
        model.get("container_vllm_ascend_source")
        or model.get("vllm_ascend_source"),
    )
    response = dict(
        ssh_utils.docker_exec_command(
            node_ref,
            command,
            timeout=timeout,
            source_pythonpath=True,
        )
    )
    response["preflight"] = None
    if not response.get("success"):
        return response

    raw_output = response.get("stdout", "").strip()
    try:
        preflight = json.loads(raw_output)
        if not isinstance(preflight, dict) or not isinstance(
            preflight.get("import_ready"), bool
        ):
            raise ValueError("输出缺少布尔字段 import_ready")
        response["preflight"] = preflight
        response["stdout"] = ""
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        response["success"] = False
        response["stderr"] = (
            f"无法解析 preflight 输出: {exc}; 原始输出: {raw_output[-4000:]!r}"
        )
    return response


def main():
    parser = argparse.ArgumentParser(description="检查容器 Python 与包导入来源")
    parser.add_argument("--project-root", help="用户项目根目录")
    parser.add_argument("--config-dir", help="配置目录，默认优先项目私有配置")
    parser.add_argument(
        "node_ref",
        help="节点引用: standalone, pd-separated.p[0], pd-separated.d[0]",
    )
    parser.add_argument("--timeout", type=int, default=120, help="超时秒数")
    args = parser.parse_args()

    if args.project_root:
        os.environ["VLLM_ASCEND_PROJECT_ROOT"] = str(
            Path(args.project_root).resolve(strict=False)
        )
    if args.config_dir:
        os.environ["VLLM_ASCEND_CONFIG_DIR"] = str(
            Path(args.config_dir).resolve(strict=False)
        )

    result = run_preflight(args.node_ref, timeout=args.timeout)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
