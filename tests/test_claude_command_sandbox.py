"""Claude main-session containment with fake MXC; no real agent or user state."""

import base64
import io
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

from helpers import TempHome
from cli_subagents import hooks
from cli_subagents.config import ClaudeSandboxSettings, SandboxSettings, dump_config, load_config
from cli_subagents.sandbox.install import describe
from cli_subagents.sandbox.integrations import claude
from cli_subagents.sandbox.launcher import consume
from cli_subagents.sandbox.model import BackendInfo, PolicyError

FAKE = Path(__file__).with_name("fake_wxc.py")


class ClaudeCommandSandboxTests(TempHome):
    def setUp(self):
        super().setUp()
        self.write_config()
        self.config = load_config()
        self.config.sandbox.claude.mode = "block-bash"
        self.env = patch.dict(os.environ, {"CLI_SUBAGENTS_DEPTH": "0", "OPENAI_API_KEY": "fixture-secret"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.settings = patch.object(claude, "load_config", return_value=self.config)
        self.settings.start()
        self.addCleanup(self.settings.stop)
        self.platform = patch.object(claude, "supported_windows", return_value=True)
        self.platform.start()
        self.addCleanup(self.platform.stop)
        self.backend = patch.object(claude, "get_backend")
        self.ready = self.backend.start()
        self.addCleanup(self.backend.stop)
        self.ready.return_value.ready.return_value = BackendInfo("mxc", [sys.executable, str(FAKE)], "fixture")
        self.event = {"session_id": "fixture-session", "cwd": str(self.project), "tool_name": "PowerShell",
                      "tool_input": {"command": "Write-Output 'original'", "description": "fixture", "run_in_background": True}}

    def start(self):
        return claude.handle(self.event, "claude-sandbox-start")

    def requests(self):
        return list(claude._folder(self.event).glob("command-*.json"))

    def test_off_by_default_and_workers_keep_their_existing_hooks(self):
        self.config.sandbox = SandboxSettings()
        self.assertIsNone(claude.handle({}, "claude-sandbox"))
        self.config.sandbox.claude.mode = "block-bash"
        with patch.dict(os.environ, {"CLI_SUBAGENTS_DEPTH": "1"}):
            self.assertIsNone(claude.handle({}, "claude-sandbox-start"))
        self.ready.assert_not_called()

    def test_modes_only_block_bash_when_requested(self):
        event = {**self.event, "tool_name": "Bash"}
        self.assertEqual(claude.handle(event, "claude-sandbox")["hookSpecificOutput"]["permissionDecision"], "deny")
        self.config.sandbox.claude.mode = "powershell"
        self.assertIsNone(claude.handle(event, "claude-sandbox"))
        self.config.sandbox.enabled = False
        self.assertIsNone(claude.handle(event, "claude-sandbox"))
        self.assertIsNone(claude.handle({"tool_name": "Read"}, "claude-sandbox"))

    def test_command_is_encoded_as_data_and_permissions_are_preserved(self):
        self.start()
        line = "Write-Output 'quotes; $(evil) `new';\nWrite-Output \"next\""
        self.event["tool_input"]["command"] = line
        answer = claude.handle(self.event, "claude-sandbox")["hookSpecificOutput"]
        self.assertNotIn("permissionDecision", answer)
        self.assertNotIn(line, answer["updatedInput"]["command"])
        self.assertTrue(answer["updatedInput"]["run_in_background"])
        self.assertEqual(answer["updatedInput"]["description"], "fixture")
        [request] = self.requests()
        data = json.loads(request.read_text(encoding="utf-8"))
        command = data["command"]
        self.assertIn(line, base64.b64decode(command["argv"][-1]).decode("utf-16-le"))
        self.assertEqual(command["network"], "loopback")
        self.assertNotIn("OPENAI_API_KEY", command["env"])
        self.assertIn(str(self.home), data["policy"]["denied"])
        self.assertIn(str(claude._folder(self.event) / "sandbox-bin"), data["policy"]["readonly"])
        self.assertIn("--parent-pid' $PID", answer["updatedInput"]["command"])

    def test_only_initial_cwd_and_configured_add_dirs_are_writable(self):
        extra = self.root / "additional"
        extra.mkdir()
        self.config.sandbox.claude.additional_dirs = [extra]
        self.start()
        self.event["cwd"] = str(extra)
        claude.handle(self.event, "claude-sandbox")
        [request] = self.requests()
        self.assertEqual(json.loads(request.read_text())["policy"]["workspaces"], [str(self.project), str(extra)])
        self.event["cwd"] = str(self.root)
        with self.assertRaisesRegex(PolicyError, "outside"):
            claude.handle(self.event, "claude-sandbox")

    def test_resume_does_not_widen_roots_from_current_cwd(self):
        self.start()
        self.event["cwd"] = str(self.root)
        self.start()
        with self.assertRaisesRegex(PolicyError, "outside"):
            claude.handle(self.event, "claude-sandbox")

    def test_corrupt_resume_baseline_is_never_silently_replaced(self):
        self.start()
        path = claude._folder(self.event) / "session.json"
        path.write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(PolicyError, "baseline is invalid"):
            self.start()
        self.assertEqual(path.read_text(), "{broken")

    def test_timeout_is_bounded_and_invalid_values_issue_no_request(self):
        self.start()
        self.event["tool_input"]["timeout"] = 999999
        answer = claude.handle(self.event, "claude-sandbox")["hookSpecificOutput"]["updatedInput"]
        [request] = self.requests()
        self.assertEqual(json.loads(request.read_text())["command"]["timeout"], 120)
        self.assertEqual(answer["timeout"], 120000)
        request.unlink()
        for value in (True, -1, 0, "100", float("nan")):
            with self.subTest(value=value), self.assertRaisesRegex(PolicyError, "timeout"):
                self.event["tool_input"]["timeout"] = value
                claude.handle(self.event, "claude-sandbox")
        self.assertEqual(self.requests(), [])

    def test_missing_baseline_unsupported_host_and_disabled_backend_fail_closed(self):
        with self.assertRaisesRegex(PolicyError, "SessionStart"):
            claude.handle(self.event, "claude-sandbox")
        with patch.object(claude, "supported_windows", return_value=False), self.assertRaisesRegex(PolicyError, "Windows"):
            self.start()
        self.config.sandbox.enabled = False
        with self.assertRaisesRegex(PolicyError, "enabled = false"):
            self.start()
        self.assertEqual(self.requests(), [])

    def test_bad_roots_and_backend_failures_do_not_create_baseline(self):
        self.config.sandbox.claude.additional_dirs = [self.user_home]
        with self.assertRaises(PolicyError):
            self.start()
        self.config.sandbox.claude.additional_dirs = []
        self.ready.return_value.ready.side_effect = PolicyError("fixture broken backend")
        with self.assertRaisesRegex(PolicyError, "broken backend"):
            self.start()
        self.assertFalse((claude._folder(self.event) / "session.json").exists())

    def test_killed_launcher_baseline_is_checked_before_next_command_and_at_end(self):
        for action in ("claude-sandbox", "claude-sandbox-end"):
            self.start()
            planted = self.project / ".claude"
            planted.mkdir()
            (planted / "fixture").touch()
            with self.assertRaisesRegex(Exception, "quarantine"):
                claude.handle(self.event, action)
            self.assertFalse(planted.exists())
        self.assertEqual(self.requests(), [])

    def test_hook_entry_denies_validation_errors_without_notification_swallow(self):
        with patch("sys.stdin", io.StringIO("{}")), patch("sys.stdout", new_callable=io.StringIO) as output:
            # No tool name is an unrelated event; malformed JSON fails closed.
            self.assertEqual(hooks.main("claude-sandbox"), 0)
        with patch("sys.stdin", io.StringIO("{")), patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(hooks.main("claude-sandbox"), 0)
            self.assertEqual(json.loads(output.getvalue())["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_session_end_cleans_hard_killed_requests_even_after_quarantine(self):
        self.start()
        folder = claude._folder(self.event)
        paths = [folder / "command-fixture.json", folder / "command-fixture.json.running-fixture",
                 folder / "policy-fixture.json"]
        for path in paths:
            path.write_text("fixture", encoding="utf-8")
        (self.project / ".claude").mkdir()
        with self.assertRaisesRegex(Exception, "quarantine"):
            claude.handle(self.event, "claude-sandbox-end")
        self.assertTrue(all(not path.exists() for path in paths))
        self.assertTrue((folder / "session.json").exists())

    def test_rewritten_request_runs_once_through_shared_launcher_and_fake_mxc(self):
        self.start()
        claude.handle(self.event, "claude-sandbox")
        [request] = self.requests()
        # Exercise containment with a portable fake command rather than PowerShell.
        data = json.loads(request.read_text())
        marker = self.project / "ran"
        data["command"]["argv"] = [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"]
        request.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(consume(request), 0)
        self.assertTrue(marker.exists())
        self.assertEqual(self.requests(), [])

    def test_plugin_hooks_fail_closed_and_cover_session_lifetime(self):
        root = Path(__file__).resolve().parents[1]
        definitions = json.loads((root / "hooks/hooks.json").read_text())["hooks"]
        shell = [h for group in definitions["PreToolUse"] if group.get("matcher") == "Bash|PowerShell" for h in group["hooks"]]
        self.assertEqual(len(shell), 1)
        self.assertEqual(shell[0]["onFailure"], "block")
        self.assertNotIn("async", shell[0])
        for event, action in (("SessionStart", "claude-sandbox-start"), ("SessionEnd", "claude-sandbox-end")):
            self.assertTrue(any(action in h.get("args", []) for group in definitions[event] for h in group["hooks"]))


class ClaudeSandboxConfigTests(TempHome):
    def test_config_round_trip_and_defaults(self):
        self.write_config()
        config = load_config()
        self.assertEqual(config.sandbox.claude.mode, "off")
        config.sandbox.claude = ClaudeSandboxSettings("powershell", [self.project], 240)
        (self.home / "config.toml").write_text(dump_config(config), encoding="utf-8")
        self.assertEqual(load_config().sandbox.claude, config.sandbox.claude)

    def test_invalid_config_is_rejected(self):
        for value in ('mode = "unknown"', 'additional_dirs = ["relative"]', 'timeout_seconds = 0',
                      'timeout_seconds = true', 'egress = true'):
            with self.subTest(value=value):
                self.write_config()
                with (self.home / "config.toml").open("a", encoding="utf-8") as stream:
                    stream.write("\n[sandbox.claude]\n" + value)
                with self.assertRaises(ValueError):
                    load_config()

    def test_doctor_warns_about_unsandboxed_bash(self):
        lines = describe({"state": "off", "reason": "fixture", "problems": [], "claude_mode": "powershell"})
        self.assertTrue(any("Bash" in line and "outside" in line for line in lines))
