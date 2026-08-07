import importlib.util
import json
import shlex
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def _load_preflight(fake_ssh_utils):
    spec = importlib.util.spec_from_file_location(
        "preflight_test",
        SCRIPTS / "preflight.py",
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"ssh_utils": fake_ssh_utils}):
        spec.loader.exec_module(module)
    return module


class PythonImportPreflightTests(unittest.TestCase):
    def setUp(self):
        self.ssh_utils = types.ModuleType("ssh_utils")
        self.preflight = _load_preflight(self.ssh_utils)

    def test_probe_command_is_read_only_and_uses_configured_python(self):
        command = self.preflight.build_probe_command(
            "/usr/bin/python3",
            "/source/vllm",
            "/source/vllm-ascend",
        )
        tokens = shlex.split(command)

        self.assertEqual(tokens[0], "/usr/bin/python3")
        self.assertEqual(tokens[1], "-c")
        self.assertIn("importlib.import_module", tokens[2])
        self.assertEqual(
            json.loads(tokens[3]),
            {
                "vllm": "/source/vllm",
                "vllm_ascend": "/source/vllm-ascend",
            },
        )
        self.assertNotIn("pip install", command)

    def test_run_preflight_uses_existing_docker_exec_and_parses_probe(self):
        probe = {
            "python": "/usr/bin/python3",
            "python_version": "3.11.9",
            "pythonpath": "/source/vllm:/source/vllm-ascend",
            "sys_path": ["/source/vllm", "/source/vllm-ascend"],
            "expected_sources": {},
            "imports": {
                "vllm": {"ok": True, "file": "/source/vllm/vllm/__init__.py"},
                "vllm_ascend": {
                    "ok": True,
                    "file": "/source/vllm-ascend/vllm_ascend/__init__.py",
                },
            },
            "import_ready": True,
        }
        self.ssh_utils.resolve_node = Mock(
            return_value={"docker": {"container_python": "/usr/bin/python3"}}
        )
        self.ssh_utils.load_model_config = Mock(
            return_value={
                "vllm_source": "/source/vllm",
                "vllm_ascend_source": "/source/vllm-ascend",
            }
        )
        self.ssh_utils.docker_exec_command = Mock(
            return_value={
                "success": True,
                "stdout": json.dumps(probe, ensure_ascii=False) + "\n",
                "stderr": "",
                "exit_code": 0,
                "node_ref": "standalone",
                "scope": "container",
                "cwd": "/workspace",
            }
        )

        result = self.preflight.run_preflight("standalone")

        self.assertTrue(result["success"])
        self.assertEqual(result["preflight"], probe)
        command = self.ssh_utils.docker_exec_command.call_args.args[1]
        self.assertIn("/usr/bin/python3", command)
        self.assertIn("/source/vllm-ascend", command)
        self.assertTrue(
            self.ssh_utils.docker_exec_command.call_args.kwargs[
                "source_pythonpath"
            ]
        )

    def test_malformed_probe_output_fails_without_install_or_retry(self):
        self.ssh_utils.resolve_node = Mock(
            return_value={"docker": {"container_python": "python3"}}
        )
        self.ssh_utils.load_model_config = Mock(return_value={})
        self.ssh_utils.docker_exec_command = Mock(
            return_value={
                "success": True,
                "stdout": "not-json\n",
                "stderr": "",
                "exit_code": 0,
            }
        )

        result = self.preflight.run_preflight("standalone")

        self.assertFalse(result["success"])
        self.assertIn("无法解析 preflight 输出", result["stderr"])
        self.ssh_utils.docker_exec_command.assert_called_once()

    def test_preflight_is_trusted_and_workflow_replaces_local_pip_probe(self):
        path_policy = (SCRIPTS / "path_policy.py").read_text(encoding="utf-8")
        workflow = (ROOT / "workflows" / "precision-diagnosis.md").read_text(
            encoding="utf-8"
        )
        service = (ROOT / "modules" / "service.md").read_text(encoding="utf-8")
        skill = (ROOT / "skills" / "diagnose" / "SKILL.md").read_text(
            encoding="utf-8"
        )

        self.assertIn('"preflight.py"', path_policy)
        self.assertIn("scripts/preflight.py", workflow)
        self.assertNotIn("pip show {model.pip_package}", workflow)
        self.assertIn("preflight.import_ready", service)
        self.assertIn("--source-pythonpath", service)
        self.assertIn("脚本内部显式设置的同名环境变量仍然优先", service)
        self.assertIn("preflight.py", skill)

        spec = importlib.util.spec_from_file_location(
            "phase4_path_policy",
            SCRIPTS / "path_policy.py",
        )
        path_policy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(path_policy)
        path_policy.validate_bash_command(
            (
                f'python3 "{(SCRIPTS / "preflight.py").as_posix()}" '
                f'--project-root "{ROOT.as_posix()}" standalone'
            ),
            ROOT,
            ROOT,
        )


if __name__ == "__main__":
    unittest.main()
