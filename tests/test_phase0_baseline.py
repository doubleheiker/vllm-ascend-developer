import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


class BaselineCapabilityTests(unittest.TestCase):
    def test_required_skill_files_exist(self):
        required = [
            "SKILL.md",
            "workflows/precision-diagnosis.md",
            "modules/service.md",
            "modules/test-runner.md",
            "modules/verifier.md",
            "modules/aisbench-evaluator.md",
            "modules/log-analyzer.md",
            "modules/auto-fixer.md",
            "scripts/ssh_utils.py",
            "scripts/generate_curl.py",
        ]
        for relative_path in required:
            with self.subTest(path=relative_path):
                self.assertTrue((ROOT / relative_path).is_file())

    def test_baseline_capability_markers_are_preserved(self):
        manifest = self._load_json(FIXTURES / "baseline" / "capabilities.json")
        self.assertGreaterEqual(len(manifest["capabilities"]), 8)

        for capability in manifest["capabilities"]:
            for evidence in capability["evidence"]:
                source_path = ROOT / evidence["path"]
                source = source_path.read_text(encoding="utf-8")
                for marker in evidence["markers"]:
                    with self.subTest(capability=capability["id"], marker=marker):
                        self.assertIn(marker, source)

    def test_legacy_trace_covers_full_diagnosis_loop(self):
        trace = self._load_json(FIXTURES / "baseline" / "legacy-run-trace.json")
        step_ids = [step["id"] for step in trace["steps"]]
        self.assertEqual(
            step_ids,
            [
                "initialize",
                "release-port",
                "start-service",
                "health-check",
                "smoke-test",
                "verify",
                "diagnose",
                "patch",
            ],
        )

    def test_raw_success_fixtures_are_parseable(self):
        service_log = (
            FIXTURES / "raw" / "service-startup-success.log"
        ).read_text(encoding="utf-8")
        self.assertIn("Started server process", service_log)
        self.assertIn("Application startup complete.", service_log)

        completion = self._load_json(FIXTURES / "raw" / "completion-response.json")
        self.assertEqual(completion["choices"][0]["text"], " expected continuation")

        aisbench_log = (
            FIXTURES / "raw" / "aisbench-output.log"
        ).read_text(encoding="utf-8")
        self.assertIn("'accuracy': 83.0", aisbench_log)

    def test_all_json_fixtures_are_valid(self):
        json_files = sorted(FIXTURES.rglob("*.json"))
        self.assertGreaterEqual(len(json_files), 7)
        for path in json_files:
            with self.subTest(path=path):
                self._load_json(path)

    def test_generated_artifacts_are_not_tracked_as_sources(self):
        self.assertFalse((ROOT / "scripts" / "curl_test.sh").exists())
        self.assertFalse(
            (
                ROOT
                / "scripts"
                / "__pycache__"
                / "ssh_utils.cpython-311.pyc"
            ).exists()
        )

        ignore_rules = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("scripts/curl_test.sh", ignore_rules)
        self.assertIn("__pycache__/", ignore_rules)
        self.assertIn(".dev/", ignore_rules)

    @staticmethod
    def _load_json(path):
        return json.loads(path.read_text(encoding="utf-8"))


class KnownGapContractTests(unittest.TestCase):
    """Expected failures that later rounds must replace with passing tests."""

    def test_python_import_path_preflight_is_implemented(self):
        service_config = (
            ROOT / "config" / "service.yaml"
        ).read_text(encoding="utf-8")
        service_module = (
            ROOT / "modules" / "service.md"
        ).read_text(encoding="utf-8")
        self.assertIn("container_python", service_config)
        self.assertIn("PYTHONPATH", service_module)
        self.assertTrue((ROOT / "scripts" / "preflight.py").is_file())

    @unittest.expectedFailure
    def test_host_only_port_release_is_machine_enforced(self):
        controller = ROOT / "scripts" / "service_controller.py"
        self.assertTrue(controller.is_file())
        source = controller.read_text(encoding="utf-8")
        self.assertIn("release_service_port", source)
        self.assertIn("remote-host", source)

    def test_workspace_write_boundary_is_machine_enforced(self):
        path_policy = ROOT / "scripts" / "path_policy.py"
        plugin_skill = ROOT / "skills" / "diagnose" / "SKILL.md"
        self.assertTrue(path_policy.is_file())
        policy_source = path_policy.read_text(encoding="utf-8")
        skill_source = plugin_skill.read_text(encoding="utf-8")
        self.assertIn('STATE_DIR_NAME = ".dev"', policy_source)
        self.assertIn("PreToolUse", skill_source)
        self.assertIn('matcher: "Write|Edit|Bash"', skill_source)

    @unittest.expectedFailure
    def test_persistent_workflow_runner_is_implemented(self):
        runner = ROOT / "scripts" / "workflow_runner.py"
        schema = ROOT / "schemas" / "run-state.schema.json"
        self.assertTrue(runner.is_file())
        self.assertTrue(schema.is_file())


if __name__ == "__main__":
    unittest.main()
