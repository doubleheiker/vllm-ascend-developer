import importlib.util
import io
import shlex
import sys
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def _load_ssh_utils():
    fake_paramiko = types.ModuleType("paramiko")
    fake_paramiko.SSHClient = object
    fake_paramiko.AutoAddPolicy = object
    spec = importlib.util.spec_from_file_location(
        "ssh_utils_env_test",
        SCRIPTS / "ssh_utils.py",
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"paramiko": fake_paramiko}):
        spec.loader.exec_module(module)
    return module


if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

ssh_utils = _load_ssh_utils()


class ServiceEnvironmentTests(unittest.TestCase):
    def test_source_pythonpath_is_prepended_before_startup_script(self):
        command = ssh_utils.build_source_pythonpath_command(
            "setsid bash /scripts/test.sh",
            {
                "vllm_source": "/host/vllm",
                "vllm_ascend_source": "/host/vllm-ascend",
                "container_vllm_source": "/container/vllm source",
                "container_vllm_ascend_source": "/container/vllm-ascend",
            },
        )

        self.assertEqual(
            command,
            (
                "export PYTHONPATH='/container/vllm source:"
                "/container/vllm-ascend'\"${PYTHONPATH:+:${PYTHONPATH}}\"; "
                "setsid bash /scripts/test.sh"
            ),
        )
        self.assertLess(command.index("export PYTHONPATH"), command.index("setsid"))

    def test_source_pythonpath_falls_back_to_existing_model_paths(self):
        command = ssh_utils.build_source_pythonpath_command(
            "python3 -c 'import vllm'",
            {
                "vllm_source": "/source/vllm",
                "vllm_ascend_source": "/source/vllm-ascend",
            },
        )

        self.assertIn(
            'export PYTHONPATH=/source/vllm:/source/vllm-ascend"',
            command,
        )

    def test_resolve_node_keeps_env_and_merges_docker_defaults(self):
        config = {
            "docker": {
                "name": "global-container",
                "work_dir": "/global/work",
                "startup_script": "/global/start.sh",
                "log_file": "/global/service.log",
            },
            "standalone": {
                "host": "192.0.2.10",
                "password": "secret",
                "work_dir": "/host/work",
                "env_vars": {
                    "ASCEND_RT_VISIBLE_DEVICES": "0,1",
                    "WORKER_COUNT": 2,
                },
                "docker": {
                    "name": "node-container",
                    "work_dir": "/node/work",
                },
            },
        }

        with patch.object(
            ssh_utils,
            "load_service_config",
            return_value=config,
        ):
            node = ssh_utils.resolve_node("standalone")

        self.assertEqual(
            node["env_vars"],
            {
                "ASCEND_RT_VISIBLE_DEVICES": "0,1",
                "WORKER_COUNT": "2",
            },
        )
        self.assertEqual(node["docker"]["name"], "node-container")
        self.assertEqual(node["docker"]["work_dir"], "/node/work")
        self.assertEqual(
            node["docker"]["startup_script"],
            "/global/start.sh",
        )
        self.assertEqual(
            node["docker"]["log_file"],
            "/global/service.log",
        )

    def test_docker_command_injects_each_env_value_without_shell_reparse(self):
        node = {
            "env_vars": {
                "ASCEND_RT_VISIBLE_DEVICES": "0,1",
                "PYTHONPATH": "/source/vllm:/source/vllm ascend",
                "MESSAGE": "a; echo must-not-run",
            },
            "docker": {
                "name": "vllm container",
                "work_dir": "/container/work dir",
            },
        }
        container_command = 'printf "%s" "$ASCEND_RT_VISIBLE_DEVICES"'

        rendered = ssh_utils.build_docker_exec_command(
            node,
            container_command,
        )
        tokens = shlex.split(rendered)

        self.assertEqual(
            tokens,
            [
                "docker",
                "exec",
                "--env",
                "ASCEND_RT_VISIBLE_DEVICES=0,1",
                "--env",
                "PYTHONPATH=/source/vllm:/source/vllm ascend",
                "--env",
                "MESSAGE=a; echo must-not-run",
                "--workdir",
                "/container/work dir",
                "vllm container",
                "bash",
                "-c",
                container_command,
            ],
        )

    def test_invalid_env_shapes_fail_closed_with_chinese_message(self):
        cases = (
            (["NOT_A_MAPPING"], "env_vars 必须是 YAML 映射"),
            ({"BAD-NAME": "value"}, "env_vars 包含无效变量名"),
            ({"NESTED": {"key": "value"}}, "必须是字符串、数字或布尔值"),
        )
        for raw, marker in cases:
            with self.subTest(raw=raw):
                output = io.StringIO()
                with redirect_stdout(output), self.assertRaises(SystemExit):
                    ssh_utils._normalize_env_vars(raw)
                self.assertIn(marker, output.getvalue())

    def test_invalid_docker_shape_fails_closed_with_chinese_message(self):
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit):
            ssh_utils._merge_docker_config(
                {"name": "valid"},
                ["not", "a", "mapping"],
            )
        self.assertIn("节点 docker 必须是 YAML 映射", output.getvalue())

    def test_docker_exec_resolves_config_before_remote_execution(self):
        node = {
            "env_vars": {"DEVICE": "3"},
            "docker": {
                "name": "service-container",
                "work_dir": "/service",
            },
        }
        with (
            patch.object(
                ssh_utils,
                "resolve_node",
                return_value=node,
            ),
            patch.object(
                ssh_utils,
                "exec_command",
                return_value={"success": True},
            ) as execute,
        ):
            result = ssh_utils.docker_exec_command(
                "standalone",
                "env",
                timeout=45,
            )

        self.assertEqual(
            result,
            {
                "success": True,
                "node_ref": "standalone",
                "scope": "container",
                "cwd": "/service",
            },
        )
        args = execute.call_args.args
        self.assertEqual(args[0], "standalone")
        self.assertEqual(args[2], 45)
        self.assertEqual(
            shlex.split(args[1]),
            [
                "docker",
                "exec",
                "--env",
                "DEVICE=3",
                "--workdir",
                "/service",
                "service-container",
                "bash",
                "-c",
                "env",
            ],
        )

    def test_docker_exec_can_inject_source_pythonpath_on_request(self):
        node = {
            "env_vars": {},
            "docker": {
                "name": "service-container",
                "work_dir": "/service",
            },
        }
        model = {
            "vllm_source": "/source/vllm",
            "vllm_ascend_source": "/source/vllm-ascend",
        }
        with (
            patch.object(ssh_utils, "resolve_node", return_value=node),
            patch.object(ssh_utils, "load_model_config", return_value=model),
            patch.object(
                ssh_utils,
                "exec_command",
                return_value={"success": True},
            ) as execute,
        ):
            ssh_utils.docker_exec_command(
                "standalone",
                "setsid bash /scripts/test.sh",
                source_pythonpath=True,
            )

        container_command = shlex.split(execute.call_args.args[1])[-1]
        self.assertTrue(container_command.startswith("export PYTHONPATH="))
        self.assertTrue(container_command.endswith("setsid bash /scripts/test.sh"))

    def test_docker_exec_cli_accepts_source_pythonpath_before_command(self):
        argv = [
            "ssh_utils.py",
            "docker-exec",
            "standalone",
            "--source-pythonpath",
            "setsid bash /scripts/test.sh",
        ]
        with (
            patch.object(sys, "argv", argv),
            patch.object(
                ssh_utils,
                "docker_exec_command",
                return_value={"success": True},
            ) as execute,
            redirect_stdout(io.StringIO()),
        ):
            ssh_utils.main()

        execute.assert_called_once_with(
            "standalone",
            "setsid bash /scripts/test.sh",
            600,
            source_pythonpath=True,
        )

    def test_host_exec_starts_in_node_work_dir_and_reports_context(self):
        node = {
            "work_dir": "/host/work dir",
            "docker": {},
        }
        with (
            patch.object(
                ssh_utils,
                "resolve_node",
                return_value=node,
            ),
            patch.object(
                ssh_utils,
                "exec_command",
                return_value={"success": True, "stdout": "/host/work dir\n"},
            ) as execute,
        ):
            result = ssh_utils.host_exec_command(
                "standalone",
                "pwd",
                timeout=12,
            )

        self.assertEqual(
            execute.call_args.args,
            (
                "standalone",
                "cd '/host/work dir' || exit 1; pwd",
                12,
            ),
        )
        self.assertEqual(result["node_ref"], "standalone")
        self.assertEqual(result["scope"], "host")
        self.assertEqual(result["cwd"], "/host/work dir")

    def test_exec_work_dirs_fail_closed_only_when_used(self):
        cases = (
            (None, "节点 work_dir"),
            ("relative/path", "节点 work_dir"),
            ("/", "节点 work_dir"),
        )
        for value, marker in cases:
            with self.subTest(value=value):
                output = io.StringIO()
                with redirect_stdout(output), self.assertRaises(SystemExit):
                    ssh_utils.build_host_exec_command(
                        {"work_dir": value},
                        "pwd",
                    )
                self.assertIn(marker, output.getvalue())

        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit):
            ssh_utils.build_docker_exec_command(
                {"docker": {"name": "service", "work_dir": ""}},
                "pwd",
            )
        self.assertIn("docker.work_dir", output.getvalue())

    def test_eval_node_keeps_host_and_container_work_dirs_separate(self):
        config = {
            "eval_machine": {
                "host": "192.0.2.20",
                "password": "secret",
                "work_dir": "/eval/host",
                "docker": {
                    "name": "eval-container",
                    "work_dir": "/eval/container",
                },
            }
        }
        with patch.object(
            ssh_utils,
            "load_aisbench_config",
            return_value=config,
        ):
            node = ssh_utils.resolve_node("eval")

        self.assertEqual(node["work_dir"], "/eval/host")
        self.assertEqual(node["docker"]["work_dir"], "/eval/container")

    def test_wait_uses_explicit_host_or_container_scope(self):
        node = {
            "work_dir": "/host/work",
            "docker": {
                "name": "service",
                "work_dir": "/container/work",
            },
        }
        for scope, expected_cwd, runner_name, other_name in (
            ("host", "/host/work", "host_exec_command", "docker_exec_command"),
            (
                "container",
                "/container/work",
                "docker_exec_command",
                "host_exec_command",
            ),
        ):
            with self.subTest(scope=scope):
                with (
                    patch.object(
                        ssh_utils,
                        "resolve_node",
                        return_value=node,
                    ),
                    patch.object(
                        ssh_utils,
                        runner_name,
                        return_value={
                            "success": True,
                            "stdout": "Application startup complete\n",
                        },
                    ) as selected,
                    patch.object(ssh_utils, other_name) as unselected,
                ):
                    result = ssh_utils.wait_for_keyword(
                        "standalone",
                        "service log.txt",
                        "Application startup complete",
                        interval=0,
                        timeout=1,
                        scope=scope,
                    )

                selected.assert_called_once()
                unselected.assert_not_called()
                command = selected.call_args.args[1]
                self.assertIn("'service log.txt'", command)
                self.assertEqual(result["scope"], scope)
                self.assertEqual(result["cwd"], expected_cwd)

    def test_host_exec_rejects_handwritten_docker_exec(self):
        commands = (
            "sudo docker exec service-container env",
            "/usr/bin/docker exec service-container env",
            "bash -c 'docker container exec service-container env'",
        )
        for command in commands:
            with self.subTest(command=command):
                output = io.StringIO()
                with (
                    redirect_stdout(output),
                    patch.object(ssh_utils, "exec_command") as execute,
                    self.assertRaises(SystemExit),
                ):
                    ssh_utils.host_exec_command(
                        "standalone",
                        command,
                    )

                execute.assert_not_called()
                self.assertIn(
                    "exec 只允许宿主机命令",
                    output.getvalue(),
                )
                self.assertIn("docker-exec", output.getvalue())

    def test_runtime_instructions_use_controlled_container_entrypoint(self):
        runtime_docs = [
            ROOT / "skills" / "diagnose" / "SKILL.md",
            ROOT / "workflows" / "precision-diagnosis.md",
            *sorted((ROOT / "modules").glob("*.md")),
        ]
        combined = "\n".join(
            path.read_text(encoding="utf-8") for path in runtime_docs
        )

        self.assertNotRegex(
            combined,
            r'ssh_utils\.py"\s+exec[^\n]*"docker\s+exec',
        )
        self.assertIn(
            'ssh_utils.py" docker-exec standalone',
            combined,
        )
        self.assertIn("注入节点 `env_vars`", combined)
        wait_lines = [
            line
            for line in combined.splitlines()
            if 'ssh_utils.py" wait ' in line
        ]
        self.assertTrue(wait_lines)
        for line in wait_lines:
            self.assertIn("--scope container", line)


if __name__ == "__main__":
    unittest.main()
