#!/usr/bin/env python3
"""vllm-ascend-developer 本地与远程路径安全策略。"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
from pathlib import Path, PurePosixPath


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


PLUGIN_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR_NAME = ".dev"
RUN_DIR_NAME = "run"
RUN_SUBDIR_NAMES = ("generated", "downloads", "logs", "records")
MAX_RECENT_RECORDS = 20
CONFIG_FILE_NAMES = (
    "service.yaml",
    "test.yaml",
    "model.yaml",
    "aisbench.yaml",
    "proxy.yaml",
)
TRUSTED_SCRIPTS = {"path_policy.py", "generate_curl.py", "ssh_utils.py"}
INTERPRETERS = {
    "bash",
    "node",
    "perl",
    "powershell",
    "pwsh",
    "python",
    "python3",
    "ruby",
    "sh",
    "zsh",
}
CONTROL_TOKENS = {";", "&&", "||", "|", "&"}
DIRECT_WRITE_COMMANDS = {
    "chmod",
    "chown",
    "install",
    "ln",
    "mkdir",
    "mv",
    "rm",
    "rmdir",
    "touch",
    "truncate",
}
UNVALIDATED_WRITE_COMMANDS = {
    "make",
    "ninja",
    "npm",
    "pip",
    "pip3",
    "rsync",
    "scp",
    "unzip",
}
READ_ONLY_COMMANDS = {
    "[",
    "[[",
    "cat",
    "date",
    "echo",
    "false",
    "grep",
    "head",
    "jq",
    "ls",
    "printf",
    "pwd",
    "readlink",
    "realpath",
    "rg",
    "sleep",
    "stat",
    "test",
    "true",
    "type",
    "uname",
    "wc",
    "which",
}
READ_ONLY_GIT_SUBCOMMANDS = {
    "cat-file",
    "describe",
    "diff",
    "grep",
    "log",
    "ls-files",
    "name-rev",
    "rev-parse",
    "show",
    "status",
    "version",
}
PROTECTED_ENV_VARS = {
    "CLAUDE_PROJECT_DIR",
    "CLAUDE_PLUGIN_ROOT",
    "PATH",
    "PYTHONHOME",
    "PYTHONPATH",
    "VLLM_ASCEND_CONFIG_DIR",
    "VLLM_ASCEND_PROJECT_ROOT",
}


class PathPolicyError(ValueError):
    """路径或命令违反安全策略。"""


def get_project_root(explicit=None, hook_input=None):
    """解析用户项目根目录，不使用 Plugin 安装目录作为隐式输出目录。"""
    candidate = (
        explicit
        or os.environ.get("CLAUDE_PROJECT_DIR")
        or (hook_input or {}).get("cwd")
        or os.environ.get("VLLM_ASCEND_PROJECT_ROOT")
        or os.getcwd()
    )
    return _resolve_local_path(candidate, Path.cwd())


def get_config_dir(project_root=None, explicit=None):
    """优先使用项目私有配置，缺失时回退到 Plugin 内只读模板。"""
    project = get_project_root(project_root)
    if explicit or os.environ.get("VLLM_ASCEND_CONFIG_DIR"):
        candidate = _resolve_local_path(
            explicit or os.environ["VLLM_ASCEND_CONFIG_DIR"],
            project,
        )
        _validate_config_dir_arg(candidate, project, project)
        return candidate

    project_config = project / STATE_DIR_NAME / "config"
    if any((project_config / name).is_file() for name in CONFIG_FILE_NAMES):
        return project_config.resolve(strict=False)
    return (PLUGIN_ROOT / "config").resolve(strict=False)


def workspace_paths(project_root=None):
    project = get_project_root(project_root)
    state = project / STATE_DIR_NAME
    paths = {
        "project": project,
        "state": state,
        "config": state / "config",
        "run": state / RUN_DIR_NAME,
        "runtime": state / "runtime",
        "state_ignore_file": state / ".gitignore",
    }
    resolved_state = state.resolve(strict=False)
    if not is_within(resolved_state, project):
        raise PathPolicyError(
            f"{STATE_DIR_NAME} 通过符号链接越过 workspace: {resolved_state}"
        )
    for name in ("config", "run", "runtime"):
        resolved = paths[name].resolve(strict=False)
        if not is_within(resolved, resolved_state):
            raise PathPolicyError(
                f"{STATE_DIR_NAME}/{name} 通过符号链接越过状态目录: {resolved}"
            )
    resolved_ignore = paths["state_ignore_file"].resolve(strict=False)
    if not is_within(resolved_ignore, resolved_state):
        raise PathPolicyError(
            f"{paths['state_ignore_file'].name} 通过符号链接越过状态目录: "
            f"{resolved_ignore}"
        )
    return paths


def _validate_run_layout(paths, create=False):
    run_path = paths["run"]
    if run_path.is_symlink():
        raise PathPolicyError(f"固定运行目录禁止使用符号链接: {run_path}")
    if create:
        run_path.mkdir(parents=True, exist_ok=True)
    if not run_path.is_dir():
        raise PathPolicyError(
            f"固定运行目录不存在；请先执行 path_policy.py bootstrap: {run_path}"
        )

    run_dir = run_path.resolve(strict=False)
    if not is_within(run_dir, paths["state"]):
        raise PathPolicyError(f"固定运行目录越过 .dev: {run_dir}")
    for name in RUN_SUBDIR_NAMES:
        subdir_path = run_path / name
        if subdir_path.is_symlink():
            raise PathPolicyError(
                f"运行子目录禁止使用符号链接: {subdir_path}"
            )
        if create:
            subdir_path.mkdir(exist_ok=True)
        subdir = subdir_path.resolve(strict=False)
        if not is_within(subdir, run_dir):
            raise PathPolicyError(
                f"运行子目录越过固定 run: {subdir}"
            )
        if not subdir.is_dir():
            raise PathPolicyError(f"运行子目录不存在: {subdir}")
    return run_dir


def _recent_record_files(run_dir):
    records = (Path(run_dir) / "records").resolve(strict=False)
    candidates = []
    for candidate in records.rglob("*"):
        resolved = candidate.resolve(strict=False)
        if (
            candidate.is_file()
            and not candidate.is_symlink()
            and is_within(resolved, records)
        ):
            candidates.append((candidate.stat().st_mtime_ns, resolved))
    candidates.sort(
        key=lambda item: (item[0], item[1].as_posix()),
        reverse=True,
    )
    return [
        str(path)
        for _, path in candidates[:MAX_RECENT_RECORDS]
    ]


def initialize_workspace(project_root=None):
    """幂等创建项目唯一的固定运行工作区。"""
    paths = workspace_paths(project_root)
    paths["config"].mkdir(parents=True, exist_ok=True)
    paths["runtime"].mkdir(parents=True, exist_ok=True)
    run_dir = _validate_run_layout(paths, create=True)
    paths["state_ignore_file"].write_text(
        "# 由 vllm-ascend-developer 管理：配置、凭据和运行产物禁止提交\n"
        "*\n",
        encoding="utf-8",
    )
    return {
        "run_dir": run_dir,
        "generated": run_dir / "generated",
        "downloads": run_dir / "downloads",
        "logs": run_dir / "logs",
        "records": run_dir / "records",
        "latest_records": _recent_record_files(run_dir),
    }


def initialize_config_templates(project_root=None):
    """幂等复制缺失的配置模板，绝不覆盖项目已有配置。"""
    paths = workspace_paths(project_root)
    source_dir = (PLUGIN_ROOT / "config").resolve(strict=False)
    target_dir = paths["config"].resolve(strict=False)
    target_dir.mkdir(parents=True, exist_ok=True)
    paths["state_ignore_file"].write_text(
        "# 由 vllm-ascend-developer 管理：配置、凭据和运行产物禁止提交\n"
        "*\n",
        encoding="utf-8",
    )

    copied = []
    existing = []
    for name in CONFIG_FILE_NAMES:
        source = (source_dir / name).resolve(strict=False)
        if not is_within(source, source_dir) or not source.is_file():
            raise PathPolicyError(f"Plugin 配置模板不存在或不安全: {source}")

        target = target_dir / name
        resolved_target = target.resolve(strict=False)
        if not is_within(resolved_target, target_dir):
            raise PathPolicyError(
                f"配置目标通过符号链接越过项目配置目录: {resolved_target}"
            )
        if target.exists() or target.is_symlink():
            if not target.is_file():
                raise PathPolicyError(f"配置目标不是普通文件: {target}")
            existing.append(name)
            continue

        try:
            with target.open("xb") as output:
                output.write(source.read_bytes())
        except FileExistsError:
            existing.append(name)
        else:
            copied.append(name)

    return {
        "config_dir": target_dir,
        "copied": copied,
        "existing": existing,
        "new_templates_copied": bool(copied),
    }


def bootstrap_project(project_root=None):
    """幂等检查并补齐配置和项目唯一的固定运行工作区。"""
    project = get_project_root(project_root)
    run_result = initialize_workspace(project)
    config_result = initialize_config_templates(project)
    return {
        **config_result,
        **run_result,
    }


def get_run_dir(project_root=None, required=True):
    """返回项目唯一的固定运行目录。"""
    project = get_project_root(project_root)
    paths = workspace_paths(project)
    if not paths["run"].exists() and not paths["run"].is_symlink():
        if required:
            raise PathPolicyError(
                "尚未初始化固定运行目录；请先执行 path_policy.py bootstrap"
            )
        return None
    return _validate_run_layout(paths, create=False)


def validate_local_write(path, project_root=None, cwd=None, allow_runtime=False):
    """校验 Claude 或本地脚本即将写入的路径。"""
    project = get_project_root(project_root)
    target = _resolve_local_path(path, cwd or project)
    paths = workspace_paths(project)
    run_dir = get_run_dir(project, required=False)

    allowed_roots = [paths["config"]]
    if run_dir is not None:
        allowed_roots.append(run_dir)
    if allow_runtime:
        allowed_roots.append(paths["runtime"])

    if any(is_within(target, root) for root in allowed_roots):
        return target
    if is_within(target, PLUGIN_ROOT):
        raise PathPolicyError(
            f"拒绝写入 Plugin 源目录: {target}；运行结果必须写入 .dev/run"
        )
    if not is_within(target, project):
        raise PathPolicyError(f"拒绝写入 workspace 外路径: {target}")
    if is_within(target, paths["run"]) and run_dir is None:
        raise PathPolicyError(
            "拒绝写入未初始化的固定 run 目录；请先执行 path_policy.py bootstrap"
        )
    raise PathPolicyError(
        f"拒绝写入非运行目录: {target}；只允许 .dev/run 或 .dev/config"
    )


def validate_upload_source(path, project_root=None, cwd=None):
    project = get_project_root(project_root)
    source = _resolve_local_path(path, cwd or project)
    if not (is_within(source, project) or is_within(source, PLUGIN_ROOT)):
        raise PathPolicyError(f"拒绝上传 workspace 和 Plugin 之外的本地文件: {source}")
    if not source.is_file():
        raise PathPolicyError(f"上传源文件不存在或不是普通文件: {source}")
    return source


def validate_download_destination(path, project_root=None, cwd=None):
    project = get_project_root(project_root)
    target = _resolve_local_path(path, cwd or project)
    run_dir = get_run_dir(project)
    downloads = run_dir / "downloads"
    if not is_within(target, downloads):
        raise PathPolicyError(
            f"拒绝下载到 .dev/run/downloads 之外: {target}"
        )
    return target


def validate_remote_path(path, allowed_roots, operation, base_dir=None):
    """校验 SFTP 远程路径；相对路径按给定远程工作目录解析。"""
    raw = str(path)
    if not raw or "\x00" in raw or "\n" in raw or "\r" in raw:
        raise PathPolicyError(f"{operation}远程路径为空或包含控制字符")
    if any(char in raw for char in "*?[]{}"):
        raise PathPolicyError(f"{operation}远程路径禁止使用通配符: {raw}")

    candidate = PurePosixPath(raw)
    if ".." in candidate.parts:
        raise PathPolicyError(f"{operation}远程路径禁止包含 ..: {raw}")
    if not candidate.is_absolute():
        base_raw = str(base_dir or "")
        base_path = PurePosixPath(base_raw)
        if (
            not base_raw
            or not base_path.is_absolute()
            or ".." in base_path.parts
        ):
            raise PathPolicyError(
                f"{operation}相对远程路径缺少合法的 work_dir: {raw}"
            )
        candidate = base_path / candidate

    normalized_roots = []
    for root in allowed_roots:
        root_raw = str(root or "")
        root_path = PurePosixPath(root_raw)
        if (
            root_raw
            and not root_raw.startswith("<")
            and root_path.is_absolute()
            and root_path != PurePosixPath("/")
            and ".." not in root_path.parts
        ):
            normalized_roots.append(root_path)

    if not normalized_roots:
        raise PathPolicyError(f"{operation}没有配置可用的远程允许目录")
    if not any(_is_within_posix(candidate, root) for root in normalized_roots):
        roots_text = ", ".join(str(root) for root in normalized_roots)
        raise PathPolicyError(
            f"拒绝{operation}远程路径 {candidate}；允许目录: {roots_text}"
        )
    return str(candidate)


def validate_bash_command(command, project_root=None, cwd=None):
    """
    检查常见本地 Bash 写入形式。

    ssh_utils 的引号内远程命令不做通用 Shell 解析，仅阻止绕过
    service-stop 的手工进程破坏命令。
    """
    project = get_project_root(project_root)
    base = _resolve_local_path(cwd or project, project)
    if _has_unescaped_shell_substitution(command):
        raise PathPolicyError(
            "拒绝包含命令替换或进程替换的 Bash 命令；其写入目标无法可靠审计"
        )
    try:
        lexer = shlex.shlex(
            command,
            posix=True,
            punctuation_chars="|&;<>",
        )
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError as exc:
        raise PathPolicyError(f"Bash 命令无法安全解析，已拒绝: {exc}") from exc

    for segment in _split_shell_segments(tokens):
        _validate_shell_segment(segment, project, base)
    return True


def evaluate_hook(payload):
    """返回 Claude Code PreToolUse 所需的允许/拒绝 JSON。"""
    try:
        tool_name = payload.get("tool_name", "")
        tool_input = payload.get("tool_input") or {}
        project = get_project_root(hook_input=payload)
        cwd = payload.get("cwd") or project

        if tool_name in {"Write", "Edit"}:
            target = (
                tool_input.get("file_path")
                or tool_input.get("path")
                or tool_input.get("notebook_path")
            )
            if not target:
                raise PathPolicyError(f"{tool_name} 缺少文件路径字段")
            validate_local_write(target, project, cwd)
        elif tool_name == "Bash":
            command = tool_input.get("command", "")
            if not command:
                raise PathPolicyError("Bash 缺少 command")
            validate_bash_command(command, project, cwd)
        return {}
    except Exception as exc:
        reason = str(exc) if isinstance(exc, PathPolicyError) else f"路径策略异常: {exc}"
        return hook_denial(reason)


def hook_denial(reason):
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def is_within(path, root):
    path_text = os.path.normcase(str(Path(path).resolve(strict=False)))
    root_text = os.path.normcase(str(Path(root).resolve(strict=False)))
    try:
        return os.path.commonpath([path_text, root_text]) == root_text
    except ValueError:
        return False


def _resolve_local_path(path, base):
    raw = os.path.expandvars(os.path.expanduser(str(path)))
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = Path(base) / candidate
    return candidate.resolve(strict=False)


def _is_within_posix(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _split_shell_segments(tokens):
    segments = []
    current = []
    for token in tokens:
        if token in CONTROL_TOKENS:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def _has_unescaped_shell_substitution(command):
    quote = None
    index = 0
    while index < len(command):
        char = command[index]
        if char == "\\" and quote != "'":
            index += 2
            continue
        if char == "'" and quote != '"':
            quote = None if quote == "'" else "'"
            index += 1
            continue
        if char == '"' and quote != "'":
            quote = None if quote == '"' else '"'
            index += 1
            continue
        pair = command[index : index + 2]
        if quote != "'" and (pair == "$(" or char == "`"):
            return True
        if quote is None and pair in {"<(", ">("}:
            return True
        index += 1
    return False


def _validate_shell_segment(tokens, project, cwd):
    if not tokens:
        return

    _validate_redirections(tokens, project, cwd)
    command_index = _command_index(tokens)
    if command_index is None:
        return
    for token in tokens[:command_index]:
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)=.*", token)
        if match and match.group(1) in PROTECTED_ENV_VARS:
            raise PathPolicyError(
                f"拒绝在 Bash 命令中覆盖受保护变量 {match.group(1)}"
            )

    executable = Path(tokens[command_index]).name.lower()
    args = tokens[command_index + 1 :]

    if executable in INTERPRETERS:
        script, script_index = _trusted_python_invocation(executable, args)
        if script is not None:
            _validate_trusted_script_args(
                script,
                args[script_index + 1 :],
                project,
                cwd,
            )
            return
        raise PathPolicyError(
            f"拒绝无法审计本地写入的解释器命令: {executable}；请使用 Plugin 可信脚本"
        )

    if executable == "tee":
        targets = [arg for arg in args if not arg.startswith("-")]
        for target in targets:
            _validate_shell_write_path(target, project, cwd)
        return

    if executable in {"cp", "install"}:
        for target in _explicit_target_directories(args):
            _validate_shell_write_path(target, project, cwd)
        operands = _path_operands(args)
        if operands and not _explicit_target_directories(args):
            _validate_shell_write_path(operands[-1], project, cwd)
        return

    if executable == "mv":
        for target in _explicit_target_directories(args):
            _validate_shell_write_path(target, project, cwd)
        for operand in _path_operands(args):
            _validate_shell_write_path(operand, project, cwd)
        return

    if executable in DIRECT_WRITE_COMMANDS:
        for target in _explicit_target_directories(args):
            _validate_shell_write_path(target, project, cwd)
        for operand in _path_operands(args):
            _validate_shell_write_path(operand, project, cwd)
        return

    if executable == "sed" and any(
        arg == "-i" or arg.startswith("-i") for arg in args
    ):
        operands = _path_operands(args)
        if not operands:
            raise PathPolicyError("sed -i 未找到可校验的目标文件")
        _validate_shell_write_path(operands[-1], project, cwd)
        return

    if executable in {"curl", "wget"}:
        raise PathPolicyError(
            f"拒绝本地 {executable}：其输出选项可绕过单一路径检查；"
            "远程请求请通过 ssh_utils.py 执行"
        )

    if executable == "git":
        if not args or args[0] not in READ_ONLY_GIT_SUBCOMMANDS:
            subcommand = args[0] if args else "<缺失>"
            raise PathPolicyError(
                f"诊断 Skill 中拒绝非只读 git 子命令: {subcommand}"
            )
        return

    if executable in UNVALIDATED_WRITE_COMMANDS:
        raise PathPolicyError(
            f"拒绝无法确定写入边界的命令: {executable}；请改用受控脚本"
        )
    if executable not in READ_ONLY_COMMANDS:
        raise PathPolicyError(
            f"拒绝未列入只读白名单且无法审计写入边界的命令: {executable}"
        )


def _validate_redirections(tokens, project, cwd):
    for index, token in enumerate(tokens):
        if token not in {">", ">>", ">|", "&>", "&>>", "<>", ">&", ">>&"}:
            continue
        if index + 1 >= len(tokens):
            raise PathPolicyError("Bash 输出重定向缺少目标路径")
        target = tokens[index + 1]
        if target.startswith("&") or target.isdigit() or target == "-":
            continue
        if target in {"/dev/null", "NUL", "nul"}:
            continue
        if target.startswith("("):
            raise PathPolicyError("拒绝无法解析目标的 Bash 进程替换")
        _validate_shell_write_path(target, project, cwd)


def _command_index(tokens):
    for index, token in enumerate(tokens):
        if token in {">", ">>", ">|", "<", "<<", "<<<"}:
            continue
        if re.fullmatch(r"\d+", token):
            continue
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", token):
            continue
        return index
    return None


def _trusted_python_invocation(executable, args):
    if executable not in {"python", "python3"}:
        return None, None
    for index, arg in enumerate(args):
        if arg.startswith("-"):
            continue
        expanded = os.path.expandvars(os.path.expanduser(arg))
        candidate = Path(expanded)
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        candidate = candidate.resolve(strict=False)
        if (
            candidate.name in TRUSTED_SCRIPTS
            and is_within(candidate, PLUGIN_ROOT / "scripts")
        ):
            return candidate, index
        return None, None
    return None, None


def _validate_trusted_script_args(script, args, project, cwd):
    for index, arg in enumerate(args):
        if arg == "--project-root":
            if index + 1 >= len(args):
                raise PathPolicyError(
                    f"{script.name} 的 --project-root 缺少路径"
                )
            candidate = _resolve_local_path(args[index + 1], cwd)
            if not _same_local_path(candidate, project):
                raise PathPolicyError(
                    f"拒绝可信脚本切换到其他 project root: {candidate}"
                )
        elif arg.startswith("--project-root="):
            candidate = _resolve_local_path(arg.split("=", 1)[1], cwd)
            if not _same_local_path(candidate, project):
                raise PathPolicyError(
                    f"拒绝可信脚本切换到其他 project root: {candidate}"
                )
        elif arg == "--config-dir":
            if index + 1 >= len(args):
                raise PathPolicyError(
                    f"{script.name} 的 --config-dir 缺少路径"
                )
            _validate_config_dir_arg(args[index + 1], project, cwd)
        elif arg.startswith("--config-dir="):
            _validate_config_dir_arg(arg.split("=", 1)[1], project, cwd)

    if script.name == "ssh_utils.py":
        _validate_ssh_utils_process_control(args)


def _validate_ssh_utils_process_control(args):
    """拒绝绕过服务生命周期入口的手工远程杀进程。"""
    action_index = next(
        (
            index
            for index, arg in enumerate(args)
            if arg in {"exec", "docker-exec"}
        ),
        None,
    )
    if action_index is None or action_index + 2 >= len(args):
        return
    node_ref = str(args[action_index + 1])
    if node_ref == "eval":
        return

    remote_command = str(args[action_index + 2])
    for executable, command_args in _remote_shell_invocations(remote_command):
        if executable in {"pkill", "killall"}:
            raise PathPolicyError(
                "拒绝手工远程 pkill/killall；停止 vLLM 请使用 "
                "ssh_utils.py service-stop <node>"
            )
        if executable == "fuser" and any(
            arg == "--kill"
            or (
                arg.startswith("-")
                and not arg.startswith("--")
                and "k" in arg[1:]
            )
            for arg in command_args
        ):
            raise PathPolicyError(
                "拒绝手工远程 fuser -k；停止 vLLM 请使用 "
                "ssh_utils.py service-stop <node>"
            )
        if executable != "kill":
            continue
        signal_zero = bool(command_args) and (
            command_args[0] == "-0"
            or command_args[:2] == ["-s", "0"]
            or command_args[:2] == ["--signal", "0"]
            or command_args[0] == "--signal=0"
        )
        if signal_zero:
            continue
        raise PathPolicyError(
            "拒绝手工远程 kill；停止 vLLM 请使用 "
            "ssh_utils.py service-stop <node>"
        )


def _remote_shell_invocations(command):
    """提取远程 Shell 命令位置，避免把 `grep kill` 的参数当成命令。"""
    try:
        lexer = shlex.shlex(
            command,
            posix=True,
            punctuation_chars="|&;<>",
        )
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError as exc:
        raise PathPolicyError(
            f"ssh_utils 远程命令无法安全解析: {exc}"
        ) from exc

    for segment in _split_shell_segments(tokens):
        command_index = _command_index(segment)
        if command_index is None:
            continue
        argv = segment[command_index:]
        executable, command_args = _unwrap_remote_command(argv)
        if not executable:
            continue
        yield executable, command_args
        if executable in {"bash", "sh"} and "-c" in command_args:
            index = command_args.index("-c")
            if index + 1 < len(command_args):
                yield from _remote_shell_invocations(command_args[index + 1])


def _unwrap_remote_command(argv):
    """处理 sudo/command/env 等常见无害包装层。"""
    argv = list(argv)
    while argv:
        executable = PurePosixPath(argv[0]).name
        if executable == "command":
            argv = argv[1:]
            while argv and argv[0].startswith("-"):
                argv = argv[1:]
            continue
        if executable == "env":
            argv = argv[1:]
            while argv and (
                argv[0].startswith("-")
                or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", argv[0])
            ):
                argv = argv[1:]
            continue
        if executable == "sudo":
            argv = argv[1:]
            while argv and argv[0].startswith("-"):
                option = argv.pop(0)
                if option in {"-u", "--user", "-g", "--group"} and argv:
                    argv.pop(0)
            continue
        return executable, argv[1:]
    return "", []


def _same_local_path(left, right):
    return os.path.normcase(str(Path(left).resolve(strict=False))) == os.path.normcase(
        str(Path(right).resolve(strict=False))
    )


def _path_operands(args):
    operands = []
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg in {"-m", "--mode", "-o", "--owner", "-g", "--group", "-t", "--target-directory"}:
            skip_next = True
            continue
        if arg.startswith("-"):
            continue
        operands.append(arg)
    return operands


def _explicit_target_directories(args):
    targets = []
    for index, arg in enumerate(args):
        if arg in {"-t", "--target-directory"}:
            if index + 1 >= len(args):
                raise PathPolicyError(f"{arg} 缺少目标目录")
            targets.append(args[index + 1])
        elif arg.startswith("--target-directory="):
            targets.append(arg.split("=", 1)[1])
        elif arg.startswith("-t") and len(arg) > 2:
            targets.append(arg[2:])
    return targets


def _validate_shell_write_path(path, project, cwd):
    raw = str(path)
    variable_names = {
        braced or plain
        for braced, plain in re.findall(
            r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)",
            raw,
        )
    }
    if "`" in raw or (
        "$" in raw
        and (
            not variable_names
            or variable_names - {"CLAUDE_PROJECT_DIR"}
        )
    ):
        raise PathPolicyError(
            f"拒绝包含动态变量的 Bash 写入路径，必须使用解析后的绝对路径: {path}"
        )
    expanded = os.path.expandvars(raw)
    if "$" in expanded:
        raise PathPolicyError(f"Bash 写入路径包含未解析变量: {path}")
    if re.search(r"[*?\[\]{}]", expanded):
        raise PathPolicyError(
            f"拒绝包含 glob 或 brace expansion 的 Bash 写入路径: {path}"
        )
    return validate_local_write(expanded, project, cwd)


def _validate_config_dir_arg(path, project, cwd):
    candidate = _resolve_local_path(path, cwd)
    allowed = {
        str((project / STATE_DIR_NAME / "config").resolve(strict=False)),
        str((PLUGIN_ROOT / "config").resolve(strict=False)),
    }
    normalized = os.path.normcase(str(candidate))
    if normalized not in {os.path.normcase(item) for item in allowed}:
        raise PathPolicyError(
            f"拒绝可信脚本读取其他 config 目录: {candidate}"
        )


def _print_json(data):
    print(json.dumps(data, ensure_ascii=False))


def _json_ready(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {
            key: _json_ready(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    return value


def main():
    parser = argparse.ArgumentParser(description="vLLM-Ascend 路径安全策略")
    parser.add_argument("--project-root", help="用户项目根目录")
    sub = parser.add_subparsers(dest="action", required=True)

    sub.add_parser(
        "bootstrap",
        help="幂等检查并补齐配置和项目唯一的 .dev/run 工作区",
    )
    sub.add_parser("paths", help="显示当前安全路径")
    sub.add_parser("hook", help="处理 Claude Code PreToolUse JSON")
    p_check = sub.add_parser("check-write", help="检查本地写入路径")
    p_check.add_argument("path")

    args = parser.parse_args()

    if args.action == "hook":
        try:
            payload = json.load(sys.stdin)
            result = evaluate_hook(payload)
        except Exception as exc:
            result = hook_denial(f"路径策略无法解析 Hook 输入，已拒绝: {exc}")
        _print_json(result)
        return

    project = get_project_root(args.project_root)
    try:
        if args.action == "bootstrap":
            result = bootstrap_project(project)
            _print_json({"success": True, **_json_ready(result)})
        elif args.action == "paths":
            paths = workspace_paths(project)
            run_dir = get_run_dir(project, required=False)
            _print_json(
                {
                    "success": True,
                    **{key: str(value) for key, value in paths.items()},
                    "run_dir": str(run_dir) if run_dir else None,
                }
            )
        elif args.action == "check-write":
            target = validate_local_write(args.path, project)
            _print_json({"success": True, "path": str(target)})
    except (OSError, PathPolicyError, json.JSONDecodeError) as exc:
        _print_json({"success": False, "error": str(exc)})
        sys.exit(1)


if __name__ == "__main__":
    main()
