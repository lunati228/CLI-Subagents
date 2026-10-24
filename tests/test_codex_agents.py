"""Native Codex relay definitions and installation, without running a real CLI."""

from importlib.resources import files
from pathlib import Path
import os
import tomllib
import unittest
from unittest.mock import patch

from cli_subagents import setup
from cli_subagents.config import load_config
from test_setup import SetupBase


ROOT = Path(__file__).resolve().parent.parent
RUNTIMES = ("claude", "codex", "antigravity")
TEMPLATES = files("cli_subagents").joinpath("templates", "codex-agents")


class CodexAgentDefinitionTests(unittest.TestCase):
    def test_three_standalone_roles_use_luna_low_and_supported_capability_limits(self):
        self.assertEqual(
            sorted(path.name for path in TEMPLATES.iterdir()),
            sorted(f"cli-subagents-{runtime}.toml" for runtime in RUNTIMES),
        )
        for runtime in RUNTIMES:
            with self.subTest(runtime=runtime):
                role = tomllib.loads(TEMPLATES.joinpath(f"cli-subagents-{runtime}.toml").read_text(encoding="utf-8"))
                self.assertEqual(role["name"], f"cli-subagents-{runtime}")
                self.assertEqual(role["model"], "gpt-6-luna")
                self.assertEqual(role["model_reasoning_effort"], "low")
                self.assertIn("subagent_start", role["description"])
                self.assertIn(f"csa run --follow --window 60 --runtime {runtime}", role["developer_instructions"])
                self.assertEqual(role["features"], {
                    "apps": False, "plugins": False, "memories": False, "request_permissions_tool": False,
                })
                self.assertEqual(role["skills"], {"include_instructions": False, "bundled": {"enabled": False}})
                self.assertTrue(set(role) <= {
                    "name", "description", "model", "model_reasoning_effort", "developer_instructions", "features", "skills",
                })

    def test_authenticated_framing_matches_the_claude_relays(self):
        for runtime in RUNTIMES:
            with self.subTest(runtime=runtime):
                claude = (ROOT / "agents" / f"cli-subagents-{runtime}.md").read_text(encoding="utf-8")
                native = tomllib.loads(TEMPLATES.joinpath(f"cli-subagents-{runtime}.toml").read_text(encoding="utf-8"))["developer_instructions"]
                start, end = "## 2. Read the authenticated status", "## 3. Messages"
                expected = claude[claude.index(start):claude.index(end)]
                expected = expected.replace("a later SendMessage", "a later native follow-up")
                expected = expected.replace("with timeout 600000", "using the shell execution rules above")
                expected = expected.replace("Reply to this agent with SendMessage", "Reply to this agent through the parent session")
                expected = expected.replace("A Bash call fails", "A shell command fails")
                self.assertEqual(native[native.index(start):native.index(end)], expected)

    def test_options_followups_and_shell_output_rules_are_present_for_every_runtime(self):
        required = (
            "`model=...`", "`access=...`", "`account=...`", "`effort=...`", "`cwd=...`", "`timeout=...`", "`override=...`",
            "Never set override on your own judgment", "how much of the 5-hour window is left",
            "csa message <JOB> --follow --from <OFFSET> --window <WINDOW> -", "--window 60", "csa cancel <JOB>",
            "A finished job continues the same worker conversation", "resetting OFFSET if JOB changed",
            "`session_id`", "`write_stdin`", "Retain every output chunk in order",
            '"process still running" notice is not a csa status', "If output was truncated, stop",
            "PowerShell", "PYTHONIOENCODING", "prefix EVERY payload line", "Never escape or double any quotes",
            "--relay -", "WHOLE prompt unchanged", "append `_NEXT`",
        )
        for runtime in RUNTIMES:
            with self.subTest(runtime=runtime):
                instructions = tomllib.loads(TEMPLATES.joinpath(f"cli-subagents-{runtime}.toml").read_text(encoding="utf-8"))["developer_instructions"]
                for text in required:
                    # Wrapping does not change an instruction's meaning.
                    self.assertIn(text, " ".join(instructions.split()))

    def test_wheel_package_data_includes_native_role_templates(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        patterns = project["tool"]["setuptools"]["package-data"]["cli_subagents"]
        for runtime in RUNTIMES:
            role = Path(f"templates/codex-agents/cli-subagents-{runtime}.toml")
            self.assertTrue(any(role.match(pattern) for pattern in patterns), role)


class CodexAgentInstallTests(SetupBase):
    def roles(self):
        return self.codex_dir / "agents"

    def test_setup_installs_native_roles_into_the_configured_codex_home(self):
        config_path = self.codex_dir / "config.toml"
        config_path.write_bytes(b'model = "owner-model"\n')
        code, out = self.run_cli("--host", "codex")
        self.assertEqual(code, 0, out)
        self.assertEqual(config_path.read_bytes(), b'model = "owner-model"\n')
        for runtime in RUNTIMES:
            name = f"cli-subagents-{runtime}.toml"
            self.assertEqual((self.roles() / name).read_bytes(), TEMPLATES.joinpath(name).read_bytes())
        self.assertIn("installed relay agent cli-subagents-codex", out)

    def test_enabled_plugin_gets_missing_roles_without_reinstalling_plugin(self):
        self.run_cli("--host", "codex")
        role = self.roles() / "cli-subagents-codex.toml"
        role.unlink()
        self.log.unlink()
        code, out = self.run_cli("--host", "codex")
        self.assertEqual(code, 0, out)
        self.assertTrue(role.is_file())
        self.assertIn("Start a new Codex session", out)
        self.assertTrue(all("list" in call["argv"] or "status" in call["argv"] for call in self.calls()))

    def test_repeat_setup_preserves_mtimes_and_adds_no_backups(self):
        self.run_cli("--host", "codex")
        before = {path.name: path.stat().st_mtime_ns for path in self.roles().iterdir()}
        code, out = self.run_cli("--host", "codex")
        self.assertEqual(code, 0, out)
        self.assertEqual(before, {path.name: path.stat().st_mtime_ns for path in self.roles().iterdir()})
        self.assertNotIn("Start a new Codex session", out)

    def test_replacement_backs_up_custom_contents_outside_toml_discovery(self):
        self.roles().mkdir()
        target = self.roles() / "cli-subagents-codex.toml"
        target.write_bytes(b'owner contents\r\n')
        unrelated = self.roles() / "owner-agent.toml"
        unrelated.write_bytes(b'owner role\n')
        code, out = self.run_cli("--host", "codex")
        self.assertEqual(code, 0, out)
        backups = list(self.roles().glob("cli-subagents-codex.toml.csa-backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b'owner contents\r\n')
        self.assertNotEqual(backups[0].suffix, ".toml")
        self.assertEqual(unrelated.read_bytes(), b'owner role\n')
        self.assertIn("backup at", out)

    def test_dry_run_lists_roles_without_creating_the_agents_directory(self):
        code, out = self.run_cli("--host", "codex", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("would install relay agent cli-subagents-claude", out)
        self.assertFalse(self.roles().exists())

    def test_failed_plugin_install_does_not_install_roles(self):
        os.environ["FAKE_HOST_FAIL"] = "add"
        code, out = self.run_cli("--host", "codex")
        self.assertEqual(code, 1, out)
        self.assertFalse(self.roles().exists())

    def test_other_hosts_do_not_install_codex_roles(self):
        code, out = self.run_cli("--host", "claude", "--host", "antigravity")
        self.assertEqual(code, 0, out)
        self.assertFalse(self.roles().exists())

    def test_agent_write_errors_are_reported_as_setup_failures(self):
        with patch.object(setup, "write_private", side_effect=PermissionError("agent folder denied")):
            code, out = self.run_cli("--host", "codex")
        self.assertEqual(code, 1, out)
        self.assertIn("couldn't install relay agent", out)
        self.assertIn("agent folder denied", out)

    def test_symlink_target_is_preserved(self):
        self.roles().mkdir()
        original = self.root / "owner-role"
        original.write_bytes(b"owner role")
        target = self.roles() / "cli-subagents-codex.toml"
        try:
            target.symlink_to(original)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        code, out = self.run_cli("--host", "codex")
        self.assertEqual(code, 1, out)
        self.assertTrue(target.is_symlink())
        self.assertEqual(original.read_bytes(), b"owner role")
        self.assertIn("won't replace the relay agent symlink", out)

    def test_update_refreshes_roles_after_plugin_upgrade(self):
        self.run_cli("--host", "codex")
        target = self.roles() / "cli-subagents-codex.toml"
        target.write_bytes(b"old relay")
        code, report = setup.run_update(load_config(), ["codex"], package=False)
        self.assertEqual(code, 0, report)
        self.assertEqual(target.read_bytes(), TEMPLATES.joinpath(target.name).read_bytes())
        self.assertEqual(next(self.roles().glob(target.name + ".csa-backup-*")).read_bytes(), b"old relay")

    def test_update_dry_run_preserves_old_roles(self):
        self.run_cli("--host", "codex")
        target = self.roles() / "cli-subagents-codex.toml"
        target.write_bytes(b"old relay")
        code, report = setup.run_update(load_config(), ["codex"], package=False, dry_run=True)
        self.assertEqual(code, 0, report)
        self.assertEqual(target.read_bytes(), b"old relay")
        self.assertFalse(list(self.roles().glob("*.csa-backup-*")))
        self.assertTrue(any("would install relay agent" in entry for entry in report.done))

    def test_update_without_installed_plugin_does_not_install_roles(self):
        code, report = setup.run_update(load_config(), ["codex"], package=False)
        self.assertEqual(code, 0, report)
        self.assertFalse(self.roles().exists())


if __name__ == "__main__":
    unittest.main()
