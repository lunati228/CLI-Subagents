import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from cli_subagents import cli, setup
from cli_subagents.config import default_primary, load_config, save_config
from cli_subagents.runtimes.claude import ClaudeRuntime

FAKE_HOST = Path(__file__).with_name("fake_host.py")


class SetupBase(unittest.TestCase):
    """Throwaway CLI Subagents home and host config folders, with fake claude, codex and agy."""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name).resolve()
        self.claude_dir = self.root / "claude-main"
        self.codex_dir = self.root / "codex-main"
        self.agy_dir = self.root / "agy-main"
        for folder in (self.claude_dir, self.codex_dir, self.agy_dir):
            folder.mkdir()
        self.log = self.root / "host-log.jsonl"
        fake = lambda kind: json.dumps([sys.executable, str(FAKE_HOST), kind])
        self._env = patch.dict(os.environ, {
            "CLI_SUBAGENTS_HOME": str(self.root / "csa"),
            "CLAUDE_CONFIG_DIR": str(self.claude_dir),
            "CODEX_HOME": str(self.codex_dir),
            "FAKE_AGY_HOME": str(self.agy_dir),
            "FAKE_HOST_LOG": str(self.log),
            "CLI_SUBAGENTS_CLAUDE_COMMAND": fake("claude"),
            "CLI_SUBAGENTS_CODEX_COMMAND": fake("codex"),
            "CLI_SUBAGENTS_AGY_COMMAND": fake("agy"),
        })
        self._env.start()
        # Antigravity has no CODEX_HOME-style override; keep setup's file writes temporary too.
        self._primary = patch("cli_subagents.config.default_primary", side_effect=lambda runtime:
                              self.agy_dir if runtime == "antigravity" else default_primary(runtime))
        self._primary.start()
        # The real sandbox install downloads about 540 MB; tests/test_sandbox_install.py covers it with a fake release.
        self._sandbox = patch("cli_subagents.sandbox.install.state", return_value=("off", "turned off in these tests"))
        self._sandbox.start()
        os.environ.pop("FAKE_HOST_FAIL", None)

    def tearDown(self):
        self._sandbox.stop()
        self._primary.stop()
        self._env.stop()
        self._temp.cleanup()

    def run_cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["setup", *args])
        return code, out.getvalue()

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def plugins(self, folder):
        path = folder / "fake_plugins.json"
        return json.loads(path.read_text(encoding="utf-8"))["plugins"] if path.exists() else {}


class PluginInstallTests(SetupBase):
    def test_claude_installs_through_its_terminal_commands_into_the_config_dir(self):
        code, out = self.run_cli("--host", "claude")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.plugins(self.claude_dir), {"cli-subagents@cli-subagents": True})
        changing = [c["argv"] for c in self.calls() if c["kind"] == "claude" and "list" not in c["argv"] and "auth" not in c["argv"]]
        self.assertEqual(changing, [
            ["plugin", "marketplace", "add", "lunati228/CLI-Subagents"],
            ["plugin", "install", "cli-subagents@cli-subagents"],
        ])
        self.assertTrue(all(c["folder"] == str(self.claude_dir) for c in self.calls()))
        self.assertIn("Restart Claude Code", out)
        self.assertIn("Claude Code reports no login", out)
        self.assertFalse((self.claude_dir / "settings.json").exists())

    def test_second_run_is_a_no_op(self):
        self.run_cli("--host", "claude", "--host", "codex")
        self.log.unlink()
        (self.claude_dir / "logged_in").touch()
        (self.codex_dir / "logged_in").touch()
        code, out = self.run_cli("--host", "claude", "--host", "codex")
        self.assertEqual(code, 0, out)
        self.assertIn("already installed and enabled", out)
        self.assertNotIn("Restart", out)
        self.assertTrue(all("list" in c["argv"] or "status" in c["argv"] for c in self.calls()))

    def test_disabled_claude_plugin_is_enabled(self):
        state = {"marketplaces": ["cli-subagents"], "plugins": {"cli-subagents@cli-subagents": False}}
        (self.claude_dir / "fake_plugins.json").write_text(json.dumps(state), encoding="utf-8")
        code, out = self.run_cli("--host", "claude")
        self.assertEqual(code, 0, out)
        self.assertIn("claude plugin enable cli-subagents@cli-subagents", out)
        self.assertEqual(self.plugins(self.claude_dir), {"cli-subagents@cli-subagents": True})

    def test_codex_skips_marketplace_add_when_it_exists(self):
        state = {"marketplaces": ["cli-subagents"], "plugins": {}}
        (self.codex_dir / "fake_plugins.json").write_text(json.dumps(state), encoding="utf-8")
        code, out = self.run_cli("--host", "codex")
        self.assertEqual(code, 0, out)
        changing = [c["argv"] for c in self.calls() if c["kind"] == "codex" and "list" not in c["argv"] and "status" not in c["argv"]]
        self.assertEqual(changing, [["plugin", "add", "cli-subagents@cli-subagents"]])
        self.assertIn("Start a new Codex session", out)

    def test_antigravity_installs_from_the_repository_url(self):
        code, out = self.run_cli("--host", "antigravity")
        self.assertEqual(code, 0, out)
        self.assertIn("agy plugin install https://github.com/lunati228/CLI-Subagents", out)
        self.assertEqual(self.plugins(self.agy_dir), {"CLI-Subagents": True})
        code, out = self.run_cli("--host", "antigravity")
        self.assertIn("already installed", out)

    def test_dry_run_changes_nothing(self):
        (self.root / "acct").mkdir()
        code, out = self.run_cli("--dry-run", "--account", f"max-2={self.root / 'acct'}")
        self.assertEqual(code, 0, out)
        self.assertIn("dry run", out)
        for text in ("would run `claude plugin marketplace add lunati228/CLI-Subagents`",
                     "would run `codex plugin add cli-subagents@cli-subagents`",
                     "would run `agy plugin install https://github.com/lunati228/CLI-Subagents`",
                     "would register and sync"):
            self.assertIn(text, out)
        for folder in (self.claude_dir, self.codex_dir, self.agy_dir):
            self.assertFalse((folder / "fake_plugins.json").exists())
        self.assertFalse((self.root / "csa" / "config.toml").exists())
        self.assertEqual(list((self.root / "acct").iterdir()), [])

    def test_failed_codex_install_is_reported_with_the_mcp_fallback(self):
        os.environ["FAKE_HOST_FAIL"] = "add"
        code, out = self.run_cli("--host", "codex")
        self.assertEqual(code, 1)
        self.assertIn("failed on purpose", out)
        self.assertIn("codex mcp add cli-subagents -- csa mcp", out)

    def test_auto_uses_only_installed_hosts(self):
        with patch("cli_subagents.runtimes.antigravity.AntigravityRuntime.find_binary", return_value=None), \
             patch("cli_subagents.runtimes.codex.CodexRuntime.find_binary", return_value=None):
            code, out = self.run_cli()
        self.assertEqual(code, 0, out)
        self.assertEqual({c["kind"] for c in self.calls()}, {"claude"})


class SettingsRouteTests(SetupBase):
    def settings(self):
        return json.loads((self.claude_dir / "settings.json").read_text(encoding="utf-8"))

    def test_missing_claude_binary_declares_the_plugin_in_settings(self):
        original = {"model": "opus", "enabledPlugins": {"other@market": True},
                    "extraKnownMarketplaces": {"market": {"source": {"source": "github", "repo": "o/r"}}}, "hooks": {"x": []}}
        (self.claude_dir / "settings.json").write_text(json.dumps(original), encoding="utf-8")
        with patch.object(ClaudeRuntime, "find_binary", return_value=None):
            code, out = self.run_cli("--host", "claude")
        self.assertEqual(code, 0, out)
        data = self.settings()
        self.assertEqual(data["model"], "opus")
        self.assertEqual(data["hooks"], {"x": []})
        self.assertEqual(data["enabledPlugins"], {"other@market": True, "cli-subagents@cli-subagents": True})
        self.assertEqual(data["extraKnownMarketplaces"]["cli-subagents"],
                         {"source": {"source": "github", "repo": "lunati228/CLI-Subagents"}})
        self.assertIn("market", data["extraKnownMarketplaces"])
        backups = list(self.claude_dir.glob("settings.json.csa-backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text(encoding="utf-8")), original)
        self.assertIn("Restart Claude Code so it installs", out)
        self.assertEqual(self.calls(), [])
        with patch.object(ClaudeRuntime, "find_binary", return_value=None):
            code, out = self.run_cli("--host", "claude")
        self.assertIn("already declares", out)
        self.assertEqual(len(list(self.claude_dir.glob("settings.json.csa-backup-*"))), 1)

    def test_settings_route_creates_the_file_when_missing(self):
        code, out = self.run_cli("--host", "claude", "--claude-route", "settings")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.settings()["enabledPlugins"], {"cli-subagents@cli-subagents": True})

    def test_failed_cli_install_falls_back_to_settings(self):
        os.environ["FAKE_HOST_FAIL"] = "install"
        code, out = self.run_cli("--host", "claude")
        self.assertEqual(code, 0, out)
        self.assertIn("failed on purpose", out)
        self.assertTrue(self.settings()["enabledPlugins"]["cli-subagents@cli-subagents"])

    def test_cli_route_does_not_fall_back(self):
        os.environ["FAKE_HOST_FAIL"] = "install"
        code, out = self.run_cli("--host", "claude", "--claude-route", "cli")
        self.assertEqual(code, 1)
        self.assertFalse((self.claude_dir / "settings.json").exists())

    def test_invalid_settings_are_left_alone(self):
        (self.claude_dir / "settings.json").write_text("{ not json", encoding="utf-8")
        code, out = self.run_cli("--host", "claude", "--claude-route", "settings")
        self.assertEqual(code, 1)
        self.assertEqual((self.claude_dir / "settings.json").read_text(encoding="utf-8"), "{ not json")
        self.assertEqual(list(self.claude_dir.glob("settings.json.csa-backup-*")), [])

    def test_dry_run_leaves_settings_alone(self):
        code, out = self.run_cli("--host", "claude", "--claude-route", "settings", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("would add the cli-subagents marketplace", out)
        self.assertFalse((self.claude_dir / "settings.json").exists())


class SandboxHookSettingsTests(SetupBase):
    def set_mode(self, mode):
        config = load_config()
        config.sandbox.claude.mode = mode
        save_config(config)
        return config

    def settings(self):
        return json.loads((self.claude_dir / "settings.json").read_text(encoding="utf-8"))

    def write_settings(self, data):
        (self.claude_dir / "settings.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_setup_installs_hooks_on_both_routes_and_for_both_enabled_modes(self):
        for mode in ("powershell", "block-bash"):
            for route in ("cli", "settings"):
                with self.subTest(mode=mode, route=route):
                    self.set_mode(mode)
                    self.write_settings({"model": "fixture"})
                    code, out = self.run_cli("--host", "claude", "--claude-route", route)
                    self.assertEqual(code, 0, out)
                    data = self.settings()
                    self.assertEqual(data["model"], "fixture")
                    self.assertEqual(data["hooks"], setup.claude_sandbox_hooks())
                    self.assertIn("Restart Claude Code so the sandbox hook settings take effect", out)
                    for event in ("PreToolUse", "SessionStart"):
                        hook = data["hooks"][event][0]["hooks"][0]
                        self.assertEqual(hook["onFailure"], "block")
                        self.assertNotIn("async", hook)
                    self.assertNotIn("matcher", data["hooks"]["SessionStart"][0])

    def test_off_mode_does_not_create_settings(self):
        self.set_mode("off")
        code, out = self.run_cli("--host", "claude", "--claude-route", "cli")
        self.assertEqual(code, 0, out)
        self.assertFalse((self.claude_dir / "settings.json").exists())
        self.assertIn("not installed; matches mode off", out)

    def test_merge_and_removal_preserve_other_settings_and_shared_hook_groups(self):
        config = self.set_mode("powershell")
        custom = {"type": "command", "command": "csa", "args": ["hook", "pending"]}
        group = {"matcher": "Bash", "hooks": [custom]}
        original = {"model": "éxample", "hooks": {"PreToolUse": [group], "OtherEvent": [{"untouched": True}]}}
        self.write_settings(original)
        report = setup.Report()
        setup.sync_claude_sandbox_hooks(config, report)
        self.assertFalse(report.failed)
        data = self.settings()
        self.assertEqual(data["hooks"]["PreToolUse"][0], group)
        backups = list(self.claude_dir.glob("settings.json.csa-backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text(encoding="utf-8")), original)
        before = (self.claude_dir / "settings.json").read_bytes()
        setup.sync_claude_sandbox_hooks(config, setup.Report())
        self.assertEqual((self.claude_dir / "settings.json").read_bytes(), before)
        self.assertEqual(len(list(self.claude_dir.glob("settings.json.csa-backup-*"))), 1)
        # A user can put another command in the managed group: keep it and its matcher.
        data["hooks"]["SessionStart"][0]["hooks"].append(custom)
        self.write_settings(data)
        config.sandbox.claude.mode = "off"
        setup.sync_claude_sandbox_hooks(config, setup.Report())
        expected = {**original, "hooks": {**original["hooks"], "SessionStart": [{"hooks": [custom]}]}}
        self.assertEqual(self.settings(), expected)
        # Fast consecutive writes must not overwrite the original backup.
        self.assertEqual(len(list(self.claude_dir.glob("settings.json.csa-backup-*"))), 2)
        self.assertEqual(json.loads(backups[0].read_text(encoding="utf-8")), original)
        setup.sync_claude_sandbox_hooks(config, setup.Report())
        self.assertEqual(len(list(self.claude_dir.glob("settings.json.csa-backup-*"))), 2)

    def test_modified_or_duplicate_managed_hooks_are_repaired(self):
        config = self.set_mode("block-bash")
        hooks = setup.claude_sandbox_hooks()
        hooks["PreToolUse"][0]["hooks"][0]["onFailure"] = "ignore"
        hooks["SessionStart"] *= 2
        self.write_settings({"hooks": hooks})
        status, matches = setup.claude_sandbox_hook_status(config)
        self.assertFalse(matches)
        self.assertIn("partially installed or modified", status)
        setup.sync_claude_sandbox_hooks(config, setup.Report())
        self.assertEqual(self.settings()["hooks"], setup.claude_sandbox_hooks())
        self.assertTrue(setup.claude_sandbox_hook_status(config)[1])

    def test_dry_run_enable_and_disable_leave_files_and_backups_alone(self):
        self.set_mode("powershell")
        code, out = self.run_cli("--host", "claude", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("would install csa's sandbox hooks", out)
        self.assertFalse((self.claude_dir / "settings.json").exists())
        self.write_settings({"hooks": setup.claude_sandbox_hooks()})
        self.set_mode("off")
        before = (self.claude_dir / "settings.json").read_bytes()
        code, out = self.run_cli("--host", "claude", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("would remove csa's sandbox hooks", out)
        self.assertEqual((self.claude_dir / "settings.json").read_bytes(), before)
        self.assertEqual(list(self.claude_dir.glob("settings.json.csa-backup-*")), [])

    def test_update_installs_and_removes_hooks_including_dry_run(self):
        self.run_cli("--host", "claude", "--claude-route", "settings")
        for mode in ("block-bash", "off"):
            config = self.set_mode(mode)
            before = (self.claude_dir / "settings.json").read_bytes()
            code, report = setup.run_update(config, ["claude"], package=False, dry_run=True, sandbox=False)
            self.assertEqual(code, 0, setup.format_report(report))
            self.assertIn("would install" if mode != "off" else "would remove", setup.format_report(report))
            self.assertEqual((self.claude_dir / "settings.json").read_bytes(), before)
            # Cover the update route where Claude has no command and is declared in settings.
            with patch.object(ClaudeRuntime, "find_binary", return_value=None):
                code, report = setup.run_update(config, ["claude"], package=False, sandbox=False)
            self.assertEqual(code, 0, setup.format_report(report))
            self.assertEqual(self.settings().get("hooks", {}), setup.claude_sandbox_hooks() if mode != "off" else {})

    def test_setup_and_update_respect_selected_hosts(self):
        config = self.set_mode("powershell")
        self.run_cli("--host", "codex")
        setup.run_update(config, ["codex"], package=False, sandbox=False)
        self.assertFalse((self.claude_dir / "settings.json").exists())

    def test_invalid_settings_are_reported_without_changes(self):
        config = self.set_mode("powershell")
        path = self.claude_dir / "settings.json"
        for value in ("not json", "[]", '{"hooks": []}', '{"hooks": {"PreToolUse": {}}}',
                      '{"hooks": {"SessionStart": [{}]}}'):
            with self.subTest(value=value):
                path.write_text(value, encoding="utf-8")
                report = setup.Report()
                setup.sync_claude_sandbox_hooks(config, report)
                self.assertTrue(report.failed)
                self.assertEqual(path.read_text(encoding="utf-8"), value)
                self.assertFalse(setup.claude_sandbox_hook_status(config)[1])
        self.assertEqual(list(self.claude_dir.glob("settings.json.csa-backup-*")), [])

    def test_doctor_reports_missing_installed_and_stale_hooks(self):
        for mode, installed, matches in (("off", False, True), ("powershell", False, False),
                                         ("block-bash", True, True), ("off", True, False)):
            with self.subTest(mode=mode, installed=installed):
                self.set_mode(mode)
                self.write_settings({"hooks": setup.claude_sandbox_hooks()} if installed else {})
                accounts = {"runtime": "claude", "binary": "fake", "accounts": [
                    {"name": "primary", "auth": {"loggedIn": True}}]}
                sandbox = {"state": "off", "reason": "fixture", "problems": [], "claude_mode": mode}
                out = io.StringIO()
                before = (self.claude_dir / "settings.json").read_bytes()
                with patch.object(cli.tools, "account_report", return_value=accounts), \
                     patch("cli_subagents.sandbox.install.status", return_value=sandbox), redirect_stdout(out):
                    code = cli.main(["doctor"])
                self.assertEqual(code, 0 if matches else 1, out.getvalue())
                self.assertIn("sandbox hooks: installed" if installed else "sandbox hooks: not installed", out.getvalue())
                self.assertIn(f"{'matches' if matches else 'does not match'} mode {mode}", out.getvalue())
                if not matches:
                    self.assertIn("Run `csa setup` or `csa update`, then restart Claude Code", out.getvalue())
                self.assertEqual((self.claude_dir / "settings.json").read_bytes(), before)


class AccountTests(SetupBase):
    def test_existing_folders_are_registered_synced_and_checked(self):
        claude_acct = self.root / ".claude-2"
        codex_acct = self.root / "work"
        claude_acct.mkdir()
        codex_acct.mkdir()
        (claude_acct / "logged_in").touch()
        (codex_acct / "config.toml").write_text("", encoding="utf-8")
        code, out = self.run_cli("--host", "claude", "--account", f"max-2={claude_acct}", "--account", f"plus-2={codex_acct}")
        self.assertEqual(code, 0, out)
        config = load_config()
        self.assertEqual([(a.name, a.home) for a in config.runtime("claude").accounts], [("max-2", claude_acct)])
        self.assertEqual([(a.name, a.home) for a in config.runtime("codex").accounts], [("plus-2", codex_acct)])
        self.assertIn("csa accounts login plus-2 --runtime codex", out)
        self.assertNotIn("csa accounts login max-2", out)
        self.assertIn("shared setup synced", out)
        # Running again keeps one entry per folder.
        code, out = self.run_cli("--host", "claude", "--account", f"max-2={claude_acct}", "--account", f"codex:other={codex_acct}")
        self.assertEqual(code, 0, out)
        self.assertIn("already registered as 'plus-2'", out)
        self.assertEqual(len(load_config().runtime("codex").accounts), 1)

    def test_bad_accounts_are_reported_not_created(self):
        code, out = self.run_cli("--host", "claude", "--account", f"x={self.root / 'missing'}",
                                 "--account", f"main={self.claude_dir}", "--account", "nonsense")
        self.assertEqual(code, 1)
        self.assertIn("doesn't exist", out)
        self.assertIn("primary claude setup", out)
        self.assertIn("NAME=FOLDER", out)
        self.assertFalse((self.root / "missing").exists())
        self.assertEqual(load_config().runtime("claude").accounts, [])

    def test_parse_account_keeps_windows_drive_paths(self):
        runtime, name, folder = setup.parse_account(r"codex:plus-2=C:\Users\me\.codex-2")
        self.assertEqual((runtime, name), ("codex", "plus-2"))
        self.assertTrue(str(folder).lower().endswith(".codex-2"))
        self.assertEqual(setup.parse_account("max-2=~/x")[0], None)
        with self.assertRaises(ValueError):
            setup.parse_account("gemini:a=b")

    def test_guess_runtime(self):
        folder = self.root / "plain"
        folder.mkdir()
        self.assertEqual(setup.guess_runtime(folder), "claude")
        (folder / "sessions").mkdir()
        self.assertEqual(setup.guess_runtime(folder), "codex")
        named = self.root / ".codex-3"
        named.mkdir()
        self.assertEqual(setup.guess_runtime(named), "codex")

    def test_accounts_add_refuses_a_folder_that_another_account_uses(self):
        folder = self.root / "shared"
        folder.mkdir()
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["accounts", "add", "a", "--home", str(folder), "--no-login"]), 0)
            self.assertEqual(cli.main(["accounts", "add", "b", "--home", str(folder), "--no-login"]), 1)
        self.assertIn("already belongs to claude account 'a'", out.getvalue())
        self.assertEqual([a.name for a in load_config().runtime("claude").accounts], ["a"])


if __name__ == "__main__":
    unittest.main()


class UpdateTests(SetupBase):
    def run_update(self, *args, info=("pip-user", None)):
        method, argv = info
        if method.startswith("pip") and argv is None:
            argv = [sys.executable, "-c", "print('reinstalled')"]
        out = io.StringIO()
        with patch.object(setup, "_install_info", return_value=(method, argv)), \
             patch.object(setup, "_replaces_running_program", return_value=False), redirect_stdout(out):
            code = cli.main(["update", *args])
        return code, out.getvalue()

    def test_updates_the_package_and_every_host_that_has_the_plugin(self):
        self.run_cli("--host", "claude", "--host", "codex")
        self.log.unlink()
        code, out = self.run_update()
        self.assertEqual(code, 0, out)
        self.assertIn("csa: reinstalled from GitHub", out)
        changing = [(c["kind"], c["argv"]) for c in self.calls() if "list" not in c["argv"]]
        self.assertEqual(changing, [
            ("claude", ["plugin", "marketplace", "update", "cli-subagents"]),
            ("claude", ["plugin", "update", "cli-subagents@cli-subagents"]),
            ("codex", ["plugin", "marketplace", "upgrade", "cli-subagents"]),
            ("codex", ["plugin", "add", "cli-subagents@cli-subagents"]),
        ])
        self.assertIn("Restart Claude Code", out)
        self.assertIn("Start a new Codex session", out)
        self.assertNotIn("Antigravity", out)

    def test_dry_run_and_no_package(self):
        self.run_cli("--host", "antigravity")
        self.log.unlink()
        code, out = self.run_update("--dry-run")
        self.assertIn("would run `python -c print('reinstalled')`", out.replace(Path(sys.executable).stem, "python"))
        self.assertIn("would run `agy plugin install https://github.com/lunati228/CLI-Subagents`", out)
        self.assertTrue(all("list" in c["argv"] for c in self.calls()))
        code, out = self.run_update("--no-package", "--host", "antigravity")
        self.assertEqual(code, 0, out)
        self.assertNotIn("csa:", out)
        self.assertIn("ran `agy plugin install", out)

    def test_nothing_installed_and_unknown_package(self):
        code, out = self.run_update(info=("unknown", None))
        self.assertEqual(code, 1)
        self.assertIn("couldn't tell how the package was installed", out)
        self.assertIn("pipx install --force git+https://github.com/lunati228/CLI-Subagents", out)
        self.assertIn("Run `csa setup`", out)

    def test_settings_route_needs_a_restart(self):
        self.run_cli("--host", "claude", "--claude-route", "settings")
        with patch.object(ClaudeRuntime, "find_binary", return_value=None):
            code, out = self.run_update("--no-package", "--host", "claude")
        self.assertEqual(code, 0, out)
        self.assertIn("a restart refreshes it", out)
        self.assertIn("Restart Claude Code", out)

    def test_detect_install(self):
        url = "git+https://github.com/lunati228/CLI-Subagents"
        method, argv = setup.detect_install("/home/u/.local/pipx/venvs/cli-subagents", "/x/site-packages", None, None, "py")
        self.assertEqual((method, argv[1:]), ("pipx", ["install", "--force", url]))
        method, argv = setup.detect_install("/home/u/.local/share/uv/tools/cli-subagents", "/x", None, None, "py")
        self.assertEqual((method, argv[1:]), ("uv", ["tool", "install", "--force", url]))
        user_site = str(self.root / "user-site")
        method, argv = setup.detect_install("/usr", str(self.root / "user-site"), None, user_site, "py")
        self.assertEqual(method, "pip-user")
        self.assertIn("--user", argv)
        self.assertEqual(setup.detect_install("/usr", "/usr/lib/site", None, user_site, "py")[0], "pip")
        editable = json.dumps({"url": "file:///src", "dir_info": {"editable": True}})
        self.assertEqual(setup.detect_install("/usr", "/usr/lib/site", editable, None, "py"), ("editable", None))
        self.assertEqual(setup.detect_install("/usr", None, None, None, "py"), ("unknown", None))

    def test_a_checkout_is_never_reinstalled(self):
        # The tests run from src/ in this repository, so this must say "checkout" and run nothing.
        self.assertEqual(setup._install_info(), ("checkout", None))
