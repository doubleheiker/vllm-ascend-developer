import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def _load_ssh_utils():
    fake_paramiko = types.ModuleType("paramiko")
    fake_paramiko.SSHClient = object
    fake_paramiko.AutoAddPolicy = object
    spec = importlib.util.spec_from_file_location(
        "ssh_utils_service_lifecycle_test",
        SCRIPTS / "ssh_utils.py",
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"paramiko": fake_paramiko}):
        spec.loader.exec_module(module)
    return module


if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

ssh_utils = _load_ssh_utils()
import path_policy


def _node(**overrides):
    node = {
        "host": "192.0.2.10",
        "work_dir": "/host/scripts",
        "service_port": 8088,
        "deployment_mode": "standalone",
        "env_vars": {"ASCEND_RT_VISIBLE_DEVICES": "0,1"},
        "docker": {
            "name": "vllm-container",
            "work_dir": "/container/work",
            "startup_script": "/host/scripts/test.sh",
            "proxy_script": "",
            "proxy_port": "",
            "log_file": "/container/work/service.log",
        },
    }
    node.update(overrides)
    return node


class ServiceLifecycleFixtureTests(unittest.TestCase):
    def test_port_kill_fixture_preserves_non_listening_engine_core(self):
        fixture = json.loads(
            (
                ROOT
                / "tests"
                / "fixtures"
                / "known-gaps"
                / "service-process-group-residual.json"
            ).read_text(encoding="utf-8")
        )

        remaining = fixture["processes_after_fuser"]
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["name"], "VLLM::EngineCore")
        self.assertFalse(remaining[0]["listens_on_service_port"])
        self.assertEqual(
            fixture["expected_stop"]["signal_order"],
            ["TERM", "KILL"],
        )

    def test_kill_scope_fixture_uses_container_group_then_host_port(self):
        fixture = json.loads(
            (
                ROOT
                / "tests"
                / "fixtures"
                / "known-gaps"
                / "host-kill-scope.json"
            ).read_text(encoding="utf-8")
        )
        cases = {case["id"]: case for case in fixture["cases"]}

        self.assertEqual(
            cases["tracked-container-process-group"]["execution_scope"],
            "container",
        )
        self.assertEqual(
            cases["host-port-verification"]["expected_decision"],
            "allow-after-group-stop",
        )
        self.assertEqual(
            cases["ambiguous-process-kill"]["expected_decision"],
            "deny",
        )


class ServiceLifecycleCommandTests(unittest.TestCase):
    def test_runtime_config_derives_pid_file_from_log_file(self):
        runtime = ssh_utils.resolve_service_runtime(_node())

        self.assertEqual(runtime["service_port"], 8088)
        self.assertEqual(
            runtime["pid_file"],
            "/container/work/service.log.pgid",
        )

    def test_start_command_records_inner_session_group(self):
        command = ssh_utils.build_service_start_command(_node())

        self.assertIn("setsid bash -c", command)
        self.assertIn('printf "%s\\n" "$$"', command)
        self.assertIn("/host/scripts/test.sh", command)
        self.assertIn("/container/work/service.log.pgid", command)
        self.assertNotIn("nohup", command)
        self.assertNotIn("pkill", command)

    def test_pd_start_command_keeps_proxy_in_the_same_group(self):
        node = _node(deployment_mode="pd-separated")
        node["docker"] = dict(
            node["docker"],
            proxy_script="/host/scripts/proxy.sh",
        )

        command = ssh_utils.build_service_start_command(node)

        self.assertIn("/host/scripts/proxy.sh", command)
        self.assertIn('bash "$2" &', command)
        self.assertIn('exec bash "$3"', command)

    def test_stop_command_uses_term_then_kill_for_valid_group(self):
        command = ssh_utils.build_service_stop_command(
            _node(),
            grace_period=3,
        )

        term_at = command.index('kill -TERM -- "-$pgid"')
        kill_at = command.index('kill -KILL -- "-$pgid"')
        self.assertLess(term_at, kill_at)
        self.assertIn("SERVICE_STOP=clean", command)
        self.assertIn("SERVICE_STOP=untracked", command)
        self.assertIn("SERVICE_STOP=ownership-mismatch", command)
        self.assertIn("/host/scripts/test.sh", command)
        self.assertIn("^[0-9]+$", command)
        self.assertNotIn("pkill", command)
        self.assertNotIn("fuser", command)

    def test_service_ports_are_resolved_for_standalone_and_pd(self):
        config = {
            "docker": {
                "name": "service",
                "work_dir": "/work",
                "startup_script": "/scripts/start.sh",
                "log_file": "/work/service.log",
            },
            "standalone": {
                "host": "192.0.2.10",
                "password": "secret",
                "work_dir": "/host",
                "service_port": 8088,
            },
            "pd-separated": {
                "service_port": 9000,
                "p_nodes": [
                    {
                        "host": "192.0.2.11",
                        "password": "secret",
                        "work_dir": "/host",
                        "docker": {"proxy_port": 9001},
                    }
                ],
                "d_nodes": [],
            },
        }
        with patch.object(
            ssh_utils,
            "load_service_config",
            return_value=config,
        ):
            standalone = ssh_utils.resolve_node("standalone")
            prefill = ssh_utils.resolve_node("pd-separated.p[0]")

        self.assertEqual(standalone["service_port"], 8088)
        self.assertEqual(standalone["deployment_mode"], "standalone")
        self.assertEqual(prefill["service_port"], 9000)
        self.assertEqual(prefill["deployment_mode"], "pd-separated")


class ServiceLifecycleOrchestrationTests(unittest.TestCase):
    def test_start_requires_clean_host_ports_before_container_launch(self):
        node = _node()
        with (
            patch.object(ssh_utils, "resolve_node", return_value=node),
            patch.object(
                ssh_utils,
                "inspect_service_ports",
                return_value={
                    "success": True,
                    "stdout": "SERVICE_PORT_8088=clean\n",
                },
            ) as inspect_ports,
            patch.object(
                ssh_utils,
                "docker_exec_command",
                return_value={"success": True, "stdout": "SERVICE_STARTED_PGID=410\n"},
            ) as container_exec,
        ):
            result = ssh_utils.start_service("standalone")

        inspect_ports.assert_called_once_with("standalone", node=node)
        container_exec.assert_called_once()
        self.assertTrue(result["success"])
        self.assertEqual(result["execution_scopes"], ["host", "container"])

    def test_start_refuses_busy_port_without_container_launch(self):
        node = _node()
        with (
            patch.object(ssh_utils, "resolve_node", return_value=node),
            patch.object(
                ssh_utils,
                "inspect_service_ports",
                return_value={
                    "success": True,
                    "stdout": "SERVICE_PORT_8088=busy\n",
                },
            ),
            patch.object(ssh_utils, "docker_exec_command") as container_exec,
        ):
            result = ssh_utils.start_service("standalone")

        container_exec.assert_not_called()
        self.assertFalse(result["success"])

    def test_stop_orders_container_group_before_host_port_fallback(self):
        node = _node()
        calls = []

        def container(*args, **kwargs):
            calls.append("container")
            return {"success": True, "stdout": "SERVICE_STOP=clean\n"}

        def release(*args, **kwargs):
            calls.append("host")
            return {"success": True, "stdout": "SERVICE_PORT_8088=clean\n"}

        with (
            patch.object(ssh_utils, "resolve_node", return_value=node),
            patch.object(ssh_utils, "docker_exec_command", side_effect=container),
            patch.object(ssh_utils, "release_service_port", side_effect=release),
        ):
            result = ssh_utils.stop_service("standalone", grace_period=2)

        self.assertEqual(calls, ["container", "host"])
        self.assertTrue(result["success"])
        self.assertEqual(result["execution_scopes"], ["container", "host"])

    def test_stop_does_not_kill_host_port_for_untracked_container_process(self):
        node = _node()
        with (
            patch.object(ssh_utils, "resolve_node", return_value=node),
            patch.object(
                ssh_utils,
                "docker_exec_command",
                return_value={
                    "success": False,
                    "stdout": "",
                    "stderr": "SERVICE_STOP=untracked\n",
                },
            ),
            patch.object(
                ssh_utils,
                "inspect_service_ports",
                return_value={
                    "success": True,
                    "stdout": "SERVICE_PORT_8088=busy\n",
                },
            ) as inspect_ports,
            patch.object(ssh_utils, "release_service_port") as release_port,
        ):
            result = ssh_utils.stop_service("standalone")

        inspect_ports.assert_called_once_with("standalone", node=node)
        release_port.assert_not_called()
        self.assertFalse(result["success"])
        self.assertEqual(result["host_action"], "inspect-only")

    def test_hook_blocks_manual_remote_process_mutation_but_allows_status(self):
        ssh_script = (SCRIPTS / "ssh_utils.py").as_posix()
        denied_commands = [
            f'python3 "{ssh_script}" exec standalone "fuser -k 8088/tcp"',
            f'python3 "{ssh_script}" exec standalone "fuser --kill 8088/tcp"',
            f'python3 "{ssh_script}" docker-exec standalone "kill -9 123"',
            f'python3 "{ssh_script}" docker-exec standalone "sudo kill -TERM 123"',
            f'python3 "{ssh_script}" docker-exec standalone "bash -c \'kill -9 123\'"',
            f'python3 "{ssh_script}" docker-exec standalone "pkill python"',
        ]
        for command in denied_commands:
            with self.subTest(command=command):
                with self.assertRaisesRegex(
                    path_policy.PathPolicyError,
                    "service-stop",
                ):
                    path_policy.validate_bash_command(command, ROOT)

        allowed_commands = [
            f'python3 "{ssh_script}" exec standalone "fuser 8088/tcp"',
            f'python3 "{ssh_script}" docker-exec standalone "kill -0 123"',
            f'python3 "{ssh_script}" docker-exec standalone "grep kill service.log"',
            f'python3 "{ssh_script}" docker-exec eval "kill -TERM 123"',
            f'python3 "{ssh_script}" service-stop standalone',
        ]
        for command in allowed_commands:
            with self.subTest(command=command):
                path_policy.validate_bash_command(command, ROOT)

    def test_runtime_docs_use_service_lifecycle_commands(self):
        runtime_docs = [
            ROOT / "skills" / "diagnose" / "SKILL.md",
            ROOT / "workflows" / "precision-diagnosis.md",
            *sorted((ROOT / "modules").glob("*.md")),
        ]
        combined = "\n".join(
            path.read_text(encoding="utf-8") for path in runtime_docs
        )

        self.assertIn('ssh_utils.py" service-start standalone', combined)
        self.assertIn('ssh_utils.py" service-stop standalone', combined)
        self.assertIn('ssh_utils.py" stop-daemon standalone', combined)
        self.assertNotIn("nohup bash {docker.startup_script}", combined)
        self.assertNotIn("fuser -k {standalone.service_port}", combined)
        self.assertNotIn("kill -9 $(fuser", combined)


if __name__ == "__main__":
    unittest.main()
