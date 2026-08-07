import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import path_policy  # noqa: E402


class PathPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="路径安全-")
        self.project = Path(self.temp_dir.name) / "中文 workspace"
        self.project.mkdir()
        self.run = path_policy.initialize_workspace(self.project)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_run_layout_is_single_and_utf8_safe(self):
        self.assertEqual(
            path_policy.get_run_dir(self.project),
            self.run["run_dir"],
        )
        self.assertEqual(
            self.run["run_dir"],
            (self.project / ".dev" / "run").resolve(),
        )
        for name in ("generated", "downloads", "logs", "records"):
            self.assertTrue((self.run["run_dir"] / name).is_dir())
        self.assertFalse((self.project / ".dev" / "runs").exists())
        self.assertFalse(
            (self.project / ".dev" / "active-run.json").exists()
        )
        self.assertTrue(
            (self.project / ".dev" / "config").is_dir()
        )
        self.assertTrue(
            (self.project / ".dev" / "runtime").is_dir()
        )
        state_ignore = (
            self.project / ".dev" / ".gitignore"
        ).read_text(encoding="utf-8")
        self.assertIn("*\n", state_ignore)
        self.assertNotIn("!.gitignore", state_ignore)
        repeated = path_policy.initialize_workspace(self.project)
        self.assertEqual(repeated["run_dir"], self.run["run_dir"])

    def test_local_state_name_does_not_overlap_product_name(self):
        self.assertEqual(path_policy.STATE_DIR_NAME, ".dev")
        for path in (
            SCRIPTS / "path_policy.py",
            ROOT / "skills" / "diagnose" / "SKILL.md",
            ROOT / "AGENTS.md",
            ROOT / "CLAUDE.md",
        ):
            with self.subTest(path=path):
                self.assertNotIn(
                    ".vllm-ascend",
                    path.read_text(encoding="utf-8"),
                )

    def test_config_dir_override_is_restricted_in_script_core(self):
        project_config = (
            self.project / ".dev" / "config"
        ).resolve()
        self.assertEqual(
            path_policy.get_config_dir(
                self.project,
                project_config,
            ),
            project_config,
        )
        with self.assertRaisesRegex(
            path_policy.PathPolicyError,
            "其他 config 目录",
        ):
            path_policy.get_config_dir(
                self.project,
                self.project.parent,
            )

    def test_bootstrap_copies_templates_once_without_overwrite(self):
        first = path_policy.bootstrap_project(self.project)
        self.assertEqual(
            set(first["copied"]),
            set(path_policy.CONFIG_FILE_NAMES),
        )
        self.assertEqual(first["existing"], [])
        self.assertTrue(first["new_templates_copied"])
        self.assertEqual(first["run_dir"], self.run["run_dir"])

        config_dir = first["config_dir"]
        service_yaml = config_dir / "service.yaml"
        service_yaml.write_text(
            "# 用户配置，禁止覆盖\nmode: standalone\n",
            encoding="utf-8",
        )

        second = path_policy.bootstrap_project(self.project)
        self.assertEqual(second["copied"], [])
        self.assertEqual(
            set(second["existing"]),
            set(path_policy.CONFIG_FILE_NAMES),
        )
        self.assertFalse(second["new_templates_copied"])
        self.assertEqual(second["run_dir"], self.run["run_dir"])
        self.assertEqual(
            service_yaml.read_text(encoding="utf-8"),
            "# 用户配置，禁止覆盖\nmode: standalone\n",
        )

    def test_bootstrap_is_idempotent_across_sessions(self):
        project = Path(self.temp_dir.name) / "跨会话 workspace"
        project.mkdir()

        first = path_policy.bootstrap_project(project)
        record = first["records"] / "需求进度.md"
        record.write_text("已经完成第一阶段\n", encoding="utf-8")
        second = path_policy.bootstrap_project(project)

        self.assertEqual(first["run_dir"], second["run_dir"])
        self.assertEqual(
            first["run_dir"],
            (project / ".dev" / "run").resolve(),
        )
        self.assertIn(
            str(record.resolve()),
            second["latest_records"],
        )

    def test_local_writes_are_limited_to_config_and_fixed_run(self):
        allowed_record = self.run["records"] / "修复 1.md"
        allowed_config = (
            self.project
            / ".dev"
            / "config"
            / "service.local.yaml"
        )
        self.assertEqual(
            path_policy.validate_local_write(
                allowed_record,
                self.project,
            ),
            allowed_record.resolve(strict=False),
        )
        self.assertEqual(
            path_policy.validate_local_write(
                allowed_config,
                self.project,
            ),
            allowed_config.resolve(strict=False),
        )

        with self.assertRaisesRegex(
            path_policy.PathPolicyError,
            "非运行目录",
        ):
            path_policy.validate_local_write(
                self.project / "unexpected.md",
                self.project,
            )

        with self.assertRaisesRegex(
            path_policy.PathPolicyError,
            "workspace 外",
        ):
            path_policy.validate_local_write(
                self.project.parent / "outside.md",
                self.project,
            )

        with self.assertRaisesRegex(
            path_policy.PathPolicyError,
            "Plugin 源目录",
        ):
            path_policy.validate_local_write(
                ROOT / "unexpected.md",
                self.project,
            )

    def test_path_traversal_cannot_escape_fixed_run(self):
        escaped = self.run["run_dir"] / ".." / ".." / "escaped.md"
        with self.assertRaises(path_policy.PathPolicyError):
            path_policy.validate_local_write(
                escaped,
                self.project,
            )

    def test_state_symlink_cannot_escape_workspace(self):
        symlink_project = Path(self.temp_dir.name) / "符号链接 workspace"
        symlink_project.mkdir()
        outside = Path(self.temp_dir.name) / "外部状态目录"
        outside.mkdir()
        try:
            (symlink_project / ".dev").symlink_to(
                outside,
                target_is_directory=True,
            )
        except OSError as exc:
            self.skipTest(f"当前系统不允许创建目录符号链接: {exc}")

        with self.assertRaisesRegex(
            path_policy.PathPolicyError,
            "符号链接越过 workspace",
        ):
            path_policy.initialize_workspace(symlink_project)

    def test_write_and_edit_hooks_return_structured_chinese_denial(self):
        outside = self.project.parent / "outside.md"
        payload = {
            "cwd": str(self.project),
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": str(outside)},
        }
        with patch.dict(
            os.environ,
            {"VLLM_ASCEND_PROJECT_ROOT": str(self.project)},
        ):
            denied = path_policy.evaluate_hook(payload)
            allowed = path_policy.evaluate_hook(
                {
                    **payload,
                    "tool_name": "Edit",
                    "tool_input": {
                        "file_path": str(
                            self.run["records"] / "fix_1.md"
                        )
                    },
                }
            )

        decision = denied["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("拒绝写入", decision["permissionDecisionReason"])
        self.assertEqual(allowed, {})

    def test_bash_redirection_and_tee_are_checked(self):
        allowed_log = self.run["logs"] / "输出.log"
        outside = self.project.parent / "outside.log"

        path_policy.validate_bash_command(
            f'echo ok > "{allowed_log.as_posix()}"',
            self.project,
        )
        path_policy.validate_bash_command(
            f'echo ok 2>> "{allowed_log.as_posix()}"',
            self.project,
        )
        path_policy.validate_bash_command(
            "rg error . 2>/dev/null",
            self.project,
        )
        with patch.dict(
            os.environ,
            {"CLAUDE_PROJECT_DIR": str(self.project)},
        ):
            path_policy.validate_bash_command(
                (
                    'cp -n "${CLAUDE_PLUGIN_ROOT}/config/"*.yaml '
                    '"${CLAUDE_PROJECT_DIR}/.dev/config/"'
                ),
                self.project,
            )
        for command in (
            f'echo blocked > "{outside.as_posix()}"',
            f'echo blocked | tee "{outside.as_posix()}"',
            f'curl -o "{outside.as_posix()}" https://example.invalid',
            f'touch "{outside.as_posix()}"',
        ):
            with self.subTest(command=command):
                with self.assertRaises(path_policy.PathPolicyError):
                    path_policy.validate_bash_command(
                        command,
                        self.project,
                    )

    def test_bash_dynamic_writers_fail_closed(self):
        outside = self.project.parent / "outside.log"
        allowed_log = self.run["logs"] / "allowed.log"
        commands = (
            f'echo "$(touch {outside.as_posix()})"',
            f"cat <(touch {outside.as_posix()})",
            'echo blocked > "$OUTPUT_PATH"',
            f'echo blocked &> "{outside.as_posix()}"',
            f'exec 3<> "{outside.as_posix()}"',
            f'rm "{self.run["logs"].as_posix()}"/*.log',
            f'touch "{self.run["logs"].as_posix()}/"{{a,b}}.log',
            f'cp source -t "{outside.as_posix()}"',
            f'cp source --target-directory="{outside.as_posix()}"',
            f'git clone example.invalid "{allowed_log.as_posix()}"',
            f'curl -o "{allowed_log.as_posix()}" https://example.invalid',
            f'dd if=/dev/zero of="{outside.as_posix()}"',
        )
        for command in commands:
            with self.subTest(command=command):
                with self.assertRaises(path_policy.PathPolicyError):
                    path_policy.validate_bash_command(
                        command,
                        self.project,
                    )

    def test_plugin_root_assignment_denial_gives_resolved_retry_path(self):
        plugin_root = path_policy.PLUGIN_ROOT.as_posix()
        command = (
            f'CLAUDE_PLUGIN_ROOT="{plugin_root}" '
            'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" '
            'exec standalone "true"'
        )
        with self.assertRaisesRegex(
            path_policy.PathPolicyError,
            "直接使用 Skill 中已解析的 Plugin 脚本绝对路径",
        ) as denied:
            path_policy.validate_bash_command(command, self.project)
        self.assertIn(
            str(path_policy.PLUGIN_ROOT / "scripts"),
            str(denied.exception),
        )

    def test_service_commands_keep_fuser_on_host_and_fail_closed(self):
        service = (ROOT / "modules" / "service.md").read_text(
            encoding="utf-8"
        )
        workflow = (
            ROOT / "workflows" / "precision-diagnosis.md"
        ).read_text(encoding="utf-8")
        skill = (
            ROOT / "skills" / "diagnose" / "SKILL.md"
        ).read_text(encoding="utf-8")

        for line in service.splitlines():
            if "fuser" not in line or "ssh_utils.py" not in line:
                continue
            with self.subTest(line=line):
                self.assertIn(" exec ", line)
                self.assertNotIn(" docker-exec ", line)
                self.assertIn("command -v fuser", line)
                self.assertNotRegex(line, r"fuser [^;]+\|\| echo")

        self.assertIn("不得在容器内执行 `fuser`", workflow)
        self.assertIn("不要先做额外端口探测", workflow)
        self.assertIn(
            'docker-exec standalone --source-pythonpath "setsid bash {docker.startup_script}',
            workflow,
        )
        self.assertIn(
            "禁止在 Bash 命令前添加 `CLAUDE_PLUGIN_ROOT=...`",
            skill,
        )

    def test_standalone_service_tracks_and_stops_its_process_group(self):
        service = (ROOT / "modules" / "service.md").read_text(
            encoding="utf-8"
        )
        workflow = (
            ROOT / "workflows" / "precision-diagnosis.md"
        ).read_text(encoding="utf-8")
        auto_fixer = (ROOT / "modules" / "auto-fixer.md").read_text(
            encoding="utf-8"
        )
        skill = (
            ROOT / "skills" / "diagnose" / "SKILL.md"
        ).read_text(encoding="utf-8")

        for path, source in (
            ("modules/service.md", service),
            ("workflows/precision-diagnosis.md", workflow),
        ):
            with self.subTest(path=path):
                self.assertIn("setsid bash {docker.startup_script}", source)
                self.assertIn(
                    r"echo \$! > {docker.work_dir}/vllm.pid",
                    source,
                )
                self.assertNotIn(
                    'docker-exec standalone "nohup bash {docker.startup_script}',
                    source,
                )

        stop_section = service.split("## 停止服务", 1)[1]
        pid_stop = r"kill -9 -\$(cat {docker.work_dir}/vllm.pid)"
        self.assertIn(pid_stop, stop_section)
        self.assertIn(
            "rm -f {docker.work_dir}/vllm.pid",
            stop_section,
        )
        self.assertLess(
            stop_section.index(pid_stop),
            stop_section.index('ssh_utils.py" exec standalone'),
        )
        self.assertIn("严格执行 **service.md**", auto_fixer)
        self.assertIn("进程组", skill)

    def test_untrusted_interpreter_is_fail_closed(self):
        with self.assertRaisesRegex(
            path_policy.PathPolicyError,
            "解释器命令",
        ):
            path_policy.validate_bash_command(
                'python3 -c "open(\'/tmp/out\', \'w\').write(\'x\')"',
                self.project,
            )
        with self.assertRaisesRegex(
            path_policy.PathPolicyError,
            "未列入只读白名单",
        ):
            path_policy.validate_bash_command(
                'env python3 -c "print(1)"',
                self.project,
            )

    def test_trusted_script_cannot_override_project_root(self):
        policy_script = (SCRIPTS / "path_policy.py").as_posix()
        outside = self.project.parent / "other-project"
        with self.assertRaisesRegex(
            path_policy.PathPolicyError,
            "其他 project root",
        ):
            path_policy.validate_bash_command(
                (
                    f'python3 "{policy_script}" '
                    f'--project-root "{outside.as_posix()}" bootstrap'
                ),
                self.project,
            )
        with self.assertRaisesRegex(
            path_policy.PathPolicyError,
            "其他 config 目录",
        ):
            path_policy.validate_bash_command(
                (
                    f'python3 "{policy_script}" '
                    f'--config-dir "{outside.as_posix()}" paths'
                ),
                self.project,
            )
        with self.assertRaisesRegex(
            path_policy.PathPolicyError,
            "受保护变量",
        ):
            path_policy.validate_bash_command(
                (
                    f'VLLM_ASCEND_PROJECT_ROOT="{outside.as_posix()}" '
                    f'python3 "{policy_script}" bootstrap'
                ),
                self.project,
            )

    def test_trusted_ssh_script_does_not_misclassify_remote_redirect(self):
        ssh_script = (SCRIPTS / "ssh_utils.py").as_posix()
        redirect_command = (
            f'python3 "{ssh_script}" exec standalone '
            '"echo remote > /tmp/remote.log"'
        )
        path_policy.validate_bash_command(
            redirect_command,
            self.project,
        )
        remote_substitution_command = (
            f'python3 "{ssh_script}" docker-exec standalone '
            '"tail \\$(ls -t /tmp | head -1)"'
        )
        path_policy.validate_bash_command(
            remote_substitution_command,
            self.project,
        )

    def test_upload_and_download_local_boundaries(self):
        upload_source = self.run["generated"] / "patch.py"
        upload_source.write_text("print('ok')\n", encoding="utf-8")
        self.assertEqual(
            path_policy.validate_upload_source(
                upload_source,
                self.project,
            ),
            upload_source.resolve(),
        )
        with self.assertRaises(path_policy.PathPolicyError):
            path_policy.validate_upload_source(
                self.project.parent / "outside.py",
                self.project,
            )

        download_target = self.run["downloads"] / "服务 日志.txt"
        self.assertEqual(
            path_policy.validate_download_destination(
                download_target,
                self.project,
            ),
            download_target.resolve(strict=False),
        )
        with self.assertRaises(path_policy.PathPolicyError):
            path_policy.validate_download_destination(
                self.run["logs"] / "wrong.log",
                self.project,
            )

    def test_remote_paths_resolve_relative_to_allowlisted_work_dir(self):
        roots = ["/home/user/vllm-ascend", "/home/user/workspace"]
        self.assertEqual(
            path_policy.validate_remote_path(
                "/home/user/vllm-ascend/vllm_ascend/file.py",
                roots,
                "上传",
            ),
            "/home/user/vllm-ascend/vllm_ascend/file.py",
        )
        self.assertEqual(
            path_policy.validate_remote_path(
                "debug/output.log",
                roots,
                "上传",
                base_dir="/home/user/workspace",
            ),
            "/home/user/workspace/debug/output.log",
        )
        for target in (
            "/home/user/vllm/file.py",
            "../vllm-ascend/file.py",
            "/home/user/vllm-ascend/*.py",
        ):
            with self.subTest(target=target):
                with self.assertRaises(path_policy.PathPolicyError):
                    path_policy.validate_remote_path(
                        target,
                        roots,
                        "上传",
                    )
        with self.assertRaisesRegex(
            path_policy.PathPolicyError,
            "缺少合法的 work_dir",
        ):
            path_policy.validate_remote_path(
                "relative.log",
                roots,
                "下载",
            )
        with self.assertRaisesRegex(
            path_policy.PathPolicyError,
            "没有配置",
        ):
            path_policy.validate_remote_path(
                "/etc/passwd",
                ["/"],
                "下载",
            )


class PathSafetyIntegrationTests(unittest.TestCase):
    def test_bootstrap_cli_returns_machine_readable_result(self):
        with tempfile.TemporaryDirectory(prefix="bootstrap-") as raw:
            project = Path(raw) / "中文 project with space"
            project.mkdir()
            completed = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(SCRIPTS / "path_policy.py"),
                    "--project-root",
                    str(project),
                    "bootstrap",
                ],
                cwd=project,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            output = json.loads(completed.stdout)
            self.assertTrue(output["success"])
            self.assertEqual(
                set(output["copied"]),
                set(path_policy.CONFIG_FILE_NAMES),
            )
            self.assertEqual(output["existing"], [])
            self.assertTrue(output["new_templates_copied"])
            self.assertTrue(Path(output["run_dir"]).is_dir())
            self.assertTrue(Path(output["config_dir"]).is_dir())
            record = Path(output["records"]) / "跨会话记录.md"
            record.write_text("继续处理同一需求", encoding="utf-8")

            resumed = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(SCRIPTS / "path_policy.py"),
                    "--project-root",
                    str(project),
                    "bootstrap",
                ],
                cwd=project,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(
                resumed.returncode,
                0,
                resumed.stderr,
            )
            resumed_output = json.loads(resumed.stdout)
            self.assertEqual(
                resumed_output["run_dir"],
                output["run_dir"],
            )
            self.assertIn(
                str(record.resolve()),
                resumed_output["latest_records"],
            )

    def test_generate_curl_writes_only_to_fixed_run(self):
        with tempfile.TemporaryDirectory(prefix="curl生成-") as raw:
            project = Path(raw) / "项目 with space"
            project.mkdir()
            run = path_policy.initialize_workspace(project)
            config = project / ".dev" / "config"
            (config / "test.yaml").write_text(
                """
tests:
  - endpoint: http://127.0.0.1:8000/v1/completions
    params:
      model: test-model
      max_tokens: 8
      temperature: 0
    prompts:
      - 你好，Ascend
""".lstrip(),
                encoding="utf-8",
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(SCRIPTS / "generate_curl.py"),
                    "--project-root",
                    str(project),
                ],
                cwd=project,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            output = run["generated"] / "curl_test.sh"
            self.assertTrue(output.is_file())
            self.assertIn("你好，Ascend", output.read_text(encoding="utf-8"))
            self.assertFalse((SCRIPTS / "curl_test.sh").exists())

    def test_skill_registers_scoped_pretooluse_hook(self):
        source = (
            ROOT / "skills" / "diagnose" / "SKILL.md"
        ).read_text(encoding="utf-8")
        frontmatter = source.split("---", 2)[1]
        metadata = yaml.safe_load(frontmatter)

        self.assertTrue(metadata["disable-model-invocation"])
        hook = metadata["hooks"]["PreToolUse"][0]
        self.assertEqual(hook["matcher"], "Write|Edit|Bash")
        command = hook["hooks"][0]["command"]
        self.assertIn("${CLAUDE_PLUGIN_ROOT}", command)
        self.assertIn("path_policy.py", command)

    def test_hook_cli_fails_closed_on_malformed_json(self):
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(SCRIPTS / "path_policy.py"),
                "--project-root",
                str(ROOT),
                "hook",
            ],
            input="{not-json",
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads(completed.stdout)
        decision = output["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("无法解析", decision["permissionDecisionReason"])

    def test_trusted_scripts_do_not_write_to_plugin_source(self):
        generate_source = (
            SCRIPTS / "generate_curl.py"
        ).read_text(encoding="utf-8")
        ssh_source = (
            SCRIPTS / "ssh_utils.py"
        ).read_text(encoding="utf-8")

        self.assertNotIn('SKILL_ROOT / "scripts" / "curl_test.sh"', generate_source)
        self.assertIn('run_dir / "generated" / "curl_test.sh"', generate_source)
        self.assertIn("validate_local_write", generate_source)
        self.assertNotIn("/tmp/vllm-ssh-daemon", ssh_source)
        self.assertIn('runtime / "ssh-daemon"', ssh_source)
        self.assertIn("validate_upload_source", ssh_source)
        self.assertIn("validate_download_destination", ssh_source)

    def test_sftp_success_responses_use_zero_exit_code(self):
        source = (SCRIPTS / "ssh_utils.py").read_text(encoding="utf-8")
        upload_block = source.split(
            'elif action == "upload":',
            1,
        )[1].split('elif action == "download":', 1)[0]
        download_block = source.split(
            'elif action == "download":',
            1,
        )[1].split('elif action == "shutdown":', 1)[0]

        for action, block in (
            ("upload", upload_block),
            ("download", download_block),
        ):
            with self.subTest(action=action):
                self.assertIn('resp["success"] = True', block)
                self.assertIn('resp["exit_code"] = 0', block)

    def test_execute_success_is_derived_from_remote_exit_code(self):
        source = (SCRIPTS / "ssh_utils.py").read_text(encoding="utf-8")
        execute_block = source.split(
            'if action == "execute":',
            1,
        )[1].split('elif action == "upload":', 1)[0]

        self.assertIn(
            'resp["success"] = resp["exit_code"] == 0',
            execute_block,
        )
        self.assertNotIn('resp["success"] = True', execute_block)

    def test_curl_upload_and_container_execution_use_same_shared_path(self):
        shared_script = "{standalone.work_dir}/curl_test.sh"
        wrong_script = "{docker.work_dir}/curl_test.sh"
        test_runner = (ROOT / "modules" / "test-runner.md").read_text(
            encoding="utf-8"
        )
        verifier = (ROOT / "modules" / "verifier.md").read_text(
            encoding="utf-8"
        )
        workflow = (
            ROOT / "workflows" / "precision-diagnosis.md"
        ).read_text(encoding="utf-8")

        self.assertIn(
            f'upload standalone "{{run_dir}}/generated/curl_test.sh" "{shared_script}"',
            test_runner,
        )
        for path, source in (
            ("modules/test-runner.md", test_runner),
            ("modules/verifier.md", verifier),
            ("workflows/precision-diagnosis.md", workflow),
        ):
            with self.subTest(path=path):
                self.assertIn(
                    f'docker-exec standalone "bash {shared_script}"',
                    source,
                )
                self.assertNotIn(f"bash {wrong_script}", source)

    def test_runtime_docs_use_plugin_root_and_run_directory(self):
        runtime_docs = [
            ROOT / "skills" / "diagnose" / "SKILL.md",
            ROOT / "workflows" / "precision-diagnosis.md",
            *sorted((ROOT / "modules").glob("*.md")),
        ]
        combined = "\n".join(
            path.read_text(encoding="utf-8") for path in runtime_docs
        )

        self.assertIsNone(
            re.search(
                r"python3?\s+scripts/(?:ssh_utils|generate_curl|path_policy)\.py",
                combined,
            )
        )
        self.assertNotIn("scripts/curl_test.sh", combined)
        self.assertIn("${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py", combined)
        self.assertIn("{run_dir}/records/fix_N.md", combined)
        skill_source = runtime_docs[0].read_text(encoding="utf-8")
        self.assertIn("path_policy.py", skill_source)
        self.assertIn("bootstrap", skill_source)
        self.assertIn("latest_records", skill_source)
        self.assertIn(".dev/run/", skill_source)
        self.assertIn("最多执行一次 `bootstrap`", skill_source)
        self.assertIn("后续模块直接复用", skill_source)
        self.assertIn("不要按会话创建目录", skill_source)
        self.assertNotIn("run_action", skill_source)
        self.assertNotIn("run_id", skill_source)
        self.assertNotIn("active-run", skill_source)
        self.assertNotIn("resume-run", skill_source)
        self.assertNotIn(".dev/runs", skill_source)
        self.assertNotIn("cp -n", skill_source)
        self.assertIn("不要预先用 `ls`", skill_source)

    def test_runtime_artifacts_and_secret_configs_are_ignored(self):
        ignore_rules = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for marker in (
            ".dev/",
            "config/*.local.yaml",
            "config/*.local.yml",
            "config/*.secret.yaml",
            "config/*.secret.yml",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, ignore_rules)


if __name__ == "__main__":
    unittest.main()
