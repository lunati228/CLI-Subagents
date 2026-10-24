"""Antigravity native relay packaging and setup, using temporary homes and fake CLIs."""

from importlib.resources import files
import json
import os
from pathlib import Path
import tomllib
import unittest
from unittest.mock import patch

from cli_subagents import setup
from cli_subagents.config import load_config
from test_setup import SetupBase


ROOT = Path(__file__).resolve().parent.parent
RUNTIMES = ("claude", "codex", "antigravity")
TEMPLATES = files("cli_subagents").joinpath("templates", "antigravity-plugin")


def definition(runtime):
    text = TEMPLATES.joinpath("agents", f"cli-subagents-{runtime}.md").read_text(encoding="utf-8")
    frontmatter, body = text.removeprefix("---\n").split("\n---\n", 1)
    metadata = dict(line.split(": ", 1) for line in frontmatter.splitlines() if not line.startswith("#"))
    return metadata, body


class AntigravityAgentDefinitionTests(unittest.TestCase):
    def test_companion_plugin_contains_three_shell_only_flash_agents(self):
        manifest = json.loads(TEMPLATES.joinpath("plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "cli-subagents-relays")
        self.assertEqual(set(manifest), {"name", "description"})
        self.assertEqual(sorted(path.name for path in TEMPLATES.joinpath("agents").iterdir()),
                         sorted(f"cli-subagents-{runtime}.md" for runtime in RUNTIMES))
        for runtime in RUNTIMES:
            with self.subTest(runtime=runtime):
                metadata, body = definition(runtime)
                self.assertEqual(metadata["name"], f"cli-subagents-{runtime}")
                self.assertEqual(metadata["model"], "flash")
                self.assertEqual(metadata["tools"], "[run_command, command_status]")
                self.assertEqual(metadata["mainAgent"], "false")
                self.assertEqual(metadata["subagent"], "true")
                self.assertEqual(metadata["commandExecutionPolicy"], "sandbox")
                self.assertEqual(set(metadata), {
                    "name", "description", "tools", "mainAgent", "subagent", "model", "commandExecutionPolicy",
                })
                self.assertIn("subagent_start", json.loads(metadata["description"]))
                self.assertIn(f"csa run --follow --window 60 --runtime {runtime}", body)

    def test_authenticated_framing_matches_the_claude_relays(self):
        for runtime in RUNTIMES:
            with self.subTest(runtime=runtime):
                claude = (ROOT / "agents" / f"cli-subagents-{runtime}.md").read_text(encoding="utf-8")
                _, native = definition(runtime)
                start, end = "## 2. Read the authenticated status", "## 3. Messages"
                expected = claude[claude.index(start):claude.index(end)]
                expected = expected.replace("a later SendMessage", "a later native follow-up")
                expected = expected.replace("with timeout 600000", "using the shell execution rules above")
                expected = expected.replace("Reply to this agent with SendMessage", "Reply to this agent through the parent session")
                expected = expected.replace("A Bash call fails", "A shell command fails")
                self.assertEqual(native[native.index(start):native.index(end)], expected)

    def test_options_followups_and_complete_shell_output_are_required(self):
        required = (
            "`model=...`", "`access=...`", "`account=...`", "`cwd=...`", "`timeout=...`", "`window=...`", "`override=...`",
            "Never set override on your own judgment", "how much of the 5-hour window is left",
            "csa message <JOB> --follow --from <OFFSET> --window <WINDOW> -", "csa cancel <JOB>",
            "A finished job continues the same worker conversation", "resetting OFFSET if JOB changed",
            "`run_command`", "`command_status`", "until that same command exits",
            "A running shell handle is not a csa status", "If output was truncated",
            "Never deduplicate repeated worker lines", "Never restart the initial task",
            "PowerShell", "PYTHONIOENCODING", "prefix EVERY payload line", "Never escape or double any quotes",
            "--relay -", "WHOLE prompt unchanged", "append `_NEXT`",
            "Do not add commentary or a summary", "WINDOW and OFFSET across native follow-ups",
        )
        for runtime in RUNTIMES:
            with self.subTest(runtime=runtime):
                _, body = definition(runtime)
                for text in required:
                    self.assertIn(text, " ".join(body.split()))
                self.assertIn("`effort=", body)

    def test_wheel_package_data_includes_the_companion_plugin(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        patterns = project["tool"]["setuptools"]["package-data"]["cli_subagents"]
        paths = ["plugin.json"] + [f"agents/cli-subagents-{runtime}.md" for runtime in RUNTIMES]
        for name in paths:
            path = Path("templates/antigravity-plugin") / name
            self.assertTrue(any(path.match(pattern) for pattern in patterns), path)


class AntigravityAgentInstallTests(SetupBase):
    def plugin(self):
        return self.agy_dir / "config" / "plugins" / "cli-subagents-relays"

    def roles(self):
        return self.plugin() / "agents"

    def test_setup_installs_the_companion_and_keeps_main_plugin_commands(self):
        code, out = self.run_cli("--host", "antigravity")
        self.assertEqual(code, 0, out)
        self.assertIn("agy plugin install https://github.com/lunati228/CLI-Subagents", out)
        self.assertEqual((self.plugin() / "plugin.json").read_bytes(), TEMPLATES.joinpath("plugin.json").read_bytes())
        for runtime in RUNTIMES:
            name = f"cli-subagents-{runtime}.md"
            self.assertEqual((self.roles() / name).read_bytes(), TEMPLATES.joinpath("agents", name).read_bytes())
        self.assertIn("Start a new Antigravity session", out)

    def test_default_primary_cli_directory_uses_its_gemini_parent(self):
        config = load_config()
        config.runtime("antigravity").primary = self.agy_dir / "antigravity-cli"
        report = setup.Report()
        setup._install_antigravity_agents(config, report)
        self.assertFalse(report.failed, report)
        self.assertTrue((self.plugin() / "plugin.json").is_file())
        self.assertFalse((self.agy_dir / "antigravity-cli" / "config").exists())

    def test_enabled_main_plugin_gets_missing_agents_without_reinstall(self):
        self.run_cli("--host", "antigravity")
        target = self.roles() / "cli-subagents-codex.md"
        target.unlink()
        self.log.unlink()
        code, out = self.run_cli("--host", "antigravity")
        self.assertEqual(code, 0, out)
        self.assertTrue(target.is_file())
        self.assertTrue(all("list" in call["argv"] for call in self.calls()))

    def test_repeat_setup_preserves_mtimes_and_creates_no_backups(self):
        self.run_cli("--host", "antigravity")
        before = {p.relative_to(self.plugin()): p.stat().st_mtime_ns for p in self.plugin().rglob("*") if p.is_file()}
        code, out = self.run_cli("--host", "antigravity")
        self.assertEqual(code, 0, out)
        self.assertEqual(before, {p.relative_to(self.plugin()): p.stat().st_mtime_ns for p in self.plugin().rglob("*") if p.is_file()})
        self.assertNotIn("Start a new Antigravity session", out)

    def test_custom_contents_are_backed_up_without_markdown_extension(self):
        self.roles().mkdir(parents=True)
        target = self.roles() / "cli-subagents-codex.md"
        target.write_bytes(b"---\nname: owner-customization\n---\nkeep this\r\n")
        code, out = self.run_cli("--host", "antigravity")
        self.assertEqual(code, 0, out)
        backup, = self.roles().glob(target.name + ".csa-backup-*")
        self.assertEqual(backup.read_bytes(), b"---\nname: owner-customization\n---\nkeep this\r\n")
        self.assertNotEqual(backup.suffix, ".md")

    def test_managed_earlier_version_is_replaced_without_backup(self):
        self.run_cli("--host", "antigravity")
        target = self.roles() / "cli-subagents-codex.md"
        prefix = b"\n".join(target.read_bytes().splitlines()[:2]) + b"\n"
        target.write_bytes(prefix + b"old csa version\n")
        code, out = self.run_cli("--host", "antigravity")
        self.assertEqual(code, 0, out)
        self.assertEqual(list(self.roles().glob("*.csa-backup-*")), [])
        self.assertEqual(target.read_bytes(), TEMPLATES.joinpath("agents", target.name).read_bytes())

    def test_dry_run_creates_no_companion_files(self):
        code, out = self.run_cli("--host", "antigravity", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("would install relay agent cli-subagents-claude", out)
        self.assertFalse(self.plugin().exists())

    def test_failed_main_plugin_install_does_not_install_companion(self):
        os.environ["FAKE_HOST_FAIL"] = "install"
        code, out = self.run_cli("--host", "antigravity")
        self.assertEqual(code, 1, out)
        self.assertFalse(self.plugin().exists())

    def test_only_the_companion_does_not_count_as_the_main_plugin(self):
        data = [{"name": "cli-subagents-relays", "enabled": True}]
        self.assertIsNone(setup._find_plugin(data, "antigravity"))
        main = {"name": "cli-subagents", "enabled": True}
        self.assertIs(setup._find_plugin([*data, main], "antigravity"), main)

    def test_linked_file_and_plugin_directory_are_left_untouched(self):
        for relative in (Path("agents/cli-subagents-claude.md"), Path(".")):
            with self.subTest(relative=relative):
                target = self.plugin() / relative
                report = setup.Report()
                with patch.object(setup, "is_link", side_effect=lambda path: path == target):
                    setup._install_antigravity_agents(load_config(), report)
                self.assertTrue(report.failed, report)
                self.assertFalse(self.plugin().exists())

    def test_write_error_is_reported_without_publishing_manifest(self):
        with patch.object(setup, "write_private", side_effect=PermissionError("agent folder denied")):
            code, out = self.run_cli("--host", "antigravity")
        self.assertEqual(code, 1, out)
        self.assertIn("couldn't install relay agents", out)
        self.assertIn("agent folder denied", out)
        self.assertFalse((self.plugin() / "plugin.json").exists())

    def test_update_refreshes_agents_and_dry_run_preserves_custom_contents(self):
        self.run_cli("--host", "antigravity")
        target = self.roles() / "cli-subagents-codex.md"
        target.write_bytes(b"owner version")
        code, report = setup.run_update(load_config(), ["antigravity"], package=False, dry_run=True)
        self.assertEqual(code, 0, report)
        self.assertEqual(target.read_bytes(), b"owner version")
        self.assertFalse(list(self.roles().glob("*.csa-backup-*")))
        code, report = setup.run_update(load_config(), ["antigravity"], package=False)
        self.assertEqual(code, 0, report)
        self.assertEqual(target.read_bytes(), TEMPLATES.joinpath("agents", target.name).read_bytes())
        self.assertEqual(next(self.roles().glob(target.name + ".csa-backup-*")).read_bytes(), b"owner version")

    def test_update_without_main_plugin_does_not_install_companion(self):
        code, report = setup.run_update(load_config(), ["antigravity"], package=False)
        self.assertEqual(code, 0, report)
        self.assertFalse(self.plugin().exists())

    def test_other_hosts_do_not_install_the_companion(self):
        code, out = self.run_cli("--host", "claude", "--host", "codex")
        self.assertEqual(code, 0, out)
        self.assertFalse(self.plugin().exists())


if __name__ == "__main__":
    unittest.main()
