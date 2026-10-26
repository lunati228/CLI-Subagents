"""Keep OpenCode's discovery, data and safety limits visible to users and agents."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class OpenCodeDocumentationTests(unittest.TestCase):
    def test_user_and_agent_entrypoints_link_to_the_runtime_guide(self):
        for relative in (
            "README.md", "docs/hosts.md", "docs/configuration.md",
            "skills/cli-subagents/SKILL.md",
            "skills/cli-subagents/hosts/claude-code.md",
            "skills/cli-subagents/hosts/codex.md",
            "skills/cli-subagents/hosts/antigravity.md",
        ):
            with self.subTest(path=relative):
                text = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn("opencode.md", text)
                self.assertIn("OpenCode", text)

    def test_guide_covers_required_discovery_data_and_sandbox_limits(self):
        text = (ROOT / "docs/opencode.md").read_text(encoding="utf-8")
        for requirement in (
            "@opencode/cli", "enabled = false", "--standalone",
            "opencode/deepseek-v4.1-flash-free", "personal projects only",
            "UTC midnight", "provider.free-limit", "does not rotate accounts",
            "Big Pickle", "MiMo", "unpaid-account", "2026-08-15",
            "## Sandbox", "https://github.com/fencesandbox/fence",
            "@fencesandbox/opencode-fence", "Apache-2.0", "macOS and Linux",
            "native Windows", "no OS sandbox", "tool.execute.before",
            "codemode: false", "OPENCODE_CONFIG_CONTENT",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, text)


if __name__ == "__main__":
    unittest.main()
