import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def split_frontmatter(source):
    match = re.fullmatch(r"---\r?\n(.*?)\r?\n---\r?\n(.*)", source, re.DOTALL)
    if match is None:
        raise AssertionError("SKILL.md must contain YAML frontmatter")

    frontmatter = {}
    for line in match.group(1).splitlines():
        key, separator, value = line.partition(":")
        if not separator:
            raise AssertionError(f"Invalid frontmatter line: {line!r}")
        frontmatter[key.strip()] = value.strip()
    return frontmatter, match.group(2)


class PluginSkeletonTests(unittest.TestCase):
    def test_plugin_manifest_is_minimal_and_valid(self):
        manifest_path = ROOT / ".claude-plugin" / "plugin.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(
            manifest,
            {
                "name": "vllm-ascend-developer",
                "version": "0.1.0",
                "description": (
                    "Develop and diagnose vLLM and vLLM-Ascend inference "
                    "services and precision issues on Ascend NPUs."
                ),
            },
        )
        self.assertEqual(
            sorted(path.name for path in manifest_path.parent.iterdir()),
            ["plugin.json"],
        )

    def test_namespaced_diagnose_skill_exists(self):
        plugin_skill = ROOT / "skills" / "diagnose" / "SKILL.md"
        frontmatter, _ = split_frontmatter(
            plugin_skill.read_text(encoding="utf-8")
        )

        self.assertEqual(frontmatter["name"], "diagnose")
        self.assertTrue(frontmatter["description"])

    def test_legacy_root_skill_is_preserved(self):
        legacy_skill = ROOT / "SKILL.md"
        frontmatter, _ = split_frontmatter(
            legacy_skill.read_text(encoding="utf-8")
        )

        self.assertEqual(frontmatter["name"], "vllm-ascend-developer")

    def test_plugin_entry_has_no_round_one_behavior_drift(self):
        _, legacy_body = split_frontmatter(
            (ROOT / "SKILL.md").read_text(encoding="utf-8")
        )
        _, plugin_body = split_frontmatter(
            (ROOT / "skills" / "diagnose" / "SKILL.md").read_text(
                encoding="utf-8"
            )
        )

        self.assertEqual(plugin_body, legacy_body)

    def test_hooks_file_is_intentionally_inert(self):
        hooks = json.loads(
            (ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8")
        )

        self.assertEqual(hooks, {"hooks": {}})
        self.assertFalse((ROOT / "scripts" / "hook_guard.py").exists())


if __name__ == "__main__":
    unittest.main()
