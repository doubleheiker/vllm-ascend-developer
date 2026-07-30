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

        self.assertEqual(result, {"success": True})
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


if __name__ == "__main__":
    unittest.main()
