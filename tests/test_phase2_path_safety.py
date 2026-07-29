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
        self.run = path_policy.initialize_run(
            self.project,
            "run-001",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_run_layout_is_single_and_utf8_safe(self):
        self.assertEqual(
            path_policy.get_active_run_dir(self.project),
            self.run["run_dir"],
        )
        for name in ("generated", "downloads", "logs", "records"):
            self.assertTrue((self.run["run_dir"] / name).is_dir())
        self.assertTrue(
            (self.project / ".vllm-ascend" / "config").is_dir()
        )
        self.assertTrue(
            (self.project / ".vllm-ascend" / "runtime").is_dir()
        )
        state_ignore = (
            self.project / ".vllm-ascend" / ".gitignore"
        ).read_text(encoding="utf-8")
        self.assertIn("*\n", state_ignore)
        self.assertNotIn("!.gitignore", state_ignore)
        with self.assertRaisesRegex(
            path_policy.PathPolicyError,
            "拒绝复用旧运行目录",
        ):
            path_policy.initialize_run(self.project, "run-001")

    def test_config_dir_override_is_restricted_in_script_core(self):
        project_config = (
            self.project / ".vllm-ascend" / "config"
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

    def test_local_writes_are_limited_to_config_and_active_run(self):
        allowed_record = self.run["records"] / "修复 1.md"
        allowed_config = (
            self.project
            / ".vllm-ascend"
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

    def test_path_traversal_cannot_escape_active_run(self):
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
            (symlink_project / ".vllm-ascend").symlink_to(
                outside,
                target_is_directory=True,
            )
        except OSError as exc:
            self.skipTest(f"当前系统不允许创建目录符号链接: {exc}")

        with self.assertRaisesRegex(
            path_policy.PathPolicyError,
            "符号链接越过 workspace",
        ):
            path_policy.initialize_run(symlink_project, "run-escape")

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
                    '"${CLAUDE_PROJECT_DIR}/.vllm-ascend/config/"'
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
                    f'--project-root "{outside.as_posix()}" init-run'
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
                    f'python3 "{policy_script}" init-run'
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
            f'python3 "{ssh_script}" exec standalone '
            '"docker exec container bash -c \'tail \\$(ls -t /tmp | head -1)\'"'
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

    def test_remote_paths_require_absolute_allowlisted_locations(self):
        roots = ["/home/user/vllm-ascend", "/home/user/workspace"]
        self.assertEqual(
            path_policy.validate_remote_path(
                "/home/user/vllm-ascend/vllm_ascend/file.py",
                roots,
                "上传",
            ),
            "/home/user/vllm-ascend/vllm_ascend/file.py",
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
            "没有配置",
        ):
            path_policy.validate_remote_path(
                "/etc/passwd",
                ["/"],
                "下载",
            )


class PathSafetyIntegrationTests(unittest.TestCase):
    def test_generate_curl_writes_only_to_active_run(self):
        with tempfile.TemporaryDirectory(prefix="curl生成-") as raw:
            project = Path(raw) / "项目 with space"
            project.mkdir()
            run = path_policy.initialize_run(project, "run-curl")
            config = project / ".vllm-ascend" / "config"
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

    def test_runtime_artifacts_and_secret_configs_are_ignored(self):
        ignore_rules = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for marker in (
            ".vllm-ascend/",
            "config/*.local.yaml",
            "config/*.local.yml",
            "config/*.secret.yaml",
            "config/*.secret.yml",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, ignore_rules)


if __name__ == "__main__":
    unittest.main()
