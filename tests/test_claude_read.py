"""Claude read-only shell guard: no real vendor CLIs or credential files."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from helpers import TempHome
from cli_subagents import claude_read_hook, hooks
from cli_subagents.runtimes.claude import ClaudeRuntime


class ValidatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "src").mkdir()
        (self.root / "README.md").write_text("test", encoding="utf-8")

    def payload(self, command, tool="Bash"):
        return {"tool_name": tool, "tool_input": {"command": command}, "cwd": str(self.root),
                "permission_mode": "dontAsk", "hook_event_name": "PreToolUse"}

    def test_short_name_in_the_working_folder_itself_is_allowed(self):
        # GitHub's Windows runners keep temp folders under C:\Users\RUNNER~1.
        root = self.root / "RUNNER~1" / "proj"
        root.mkdir(parents=True)
        (root / "README.md").write_text("test", encoding="utf-8")
        for command in ("git diff main...HEAD", "git status", "cat README.md"):
            with self.subTest(command=command):
                self.assertIsNone(claude_read_hook.validate({**self.payload(command), "cwd": str(root)}))
        self.assertIsNotNone(claude_read_hook.validate({**self.payload("cat CREDEN~1.JSO"), "cwd": str(root)}))

    def test_plain_reads_leave_the_permission_decision_to_claude(self):
        for command in (
            "git status", "git diff", "git diff --stat", "git diff HEAD~1", "git diff main...HEAD",
            "git log --oneline -5", "git log --oneline -- src", "git show HEAD --stat",
            "git show HEAD:README.md", "git show HEAD~1:README.md", "git blame file",
            "rg pattern src", "ls", "wc -l README.md", "cat README.md", "head -n 5 README.md",
            "tail -n 5 README.md", "grep pattern README.md",
        ):
            with self.subTest(command=command):
                self.assertIsNone(claude_read_hook.validate(self.payload(command)))
        for command in ("Get-Content README.md", "Get-Content -LiteralPath README.md -Raw",
                        "Get-ChildItem -Force", "Measure-Object -Line"):
            with self.subTest(command=command):
                self.assertIsNone(claude_read_hook.validate(self.payload(command, "PowerShell")))
        if os.name == "nt":
            self.assertIsNone(claude_read_hook.validate(self.payload(r"Get-Content .\README.md", "PowerShell")))

    def test_write_execute_secret_and_unknown_commands_are_denied(self):
        for command in (
            "git diff --output=x", "git show --output=x", "git log --output=x", "git diff --ext-diff",
            "git diff --textconv", "git show --textconv", "git log --ext-diff", "rg --pre=cmd pattern src",
            "rg --hostname-bin=cmd pattern src", "find . -exec cmd", "sort -o file", "cat .env",
            "git show HEAD:.env", "git show HEAD~1:.env", "git diff -- .env", "git diff -- HEAD~1",
            "cat .env:stream", "cat .env.", "cat .env ", "cat .ssh/id_rsa", "git -c alias.x=cmd diff",
            "cat README.md > output", "ls; cmd", "ls && cmd", "rg --unknown pattern src", "python script.py",
            r"git diff \--output=x", r"sort \-oFILE", r"rg \--pre=cmd pattern src",
        ):
            with self.subTest(command=command):
                result = claude_read_hook.validate(self.payload(command))["hookSpecificOutput"]
                self.assertEqual(result["permissionDecision"], "deny")
                self.assertEqual(result["hookEventName"], "PreToolUse")
                self.assertTrue(result["permissionDecisionReason"].startswith("Read-only job:"))
        self.assertEqual(claude_read_hook.validate(self.payload("Get-Content .env", "PowerShell"))[
            "hookSpecificOutput"]["permissionDecision"], "deny")

    def test_symlink_to_secret_is_denied(self):
        secret = self.root / ".env"
        secret.touch()  # empty test file; the validator never reads its contents
        link = self.root / "readme-link"
        try:
            link.symlink_to(secret)
        except OSError:
            self.skipTest("symlinks unavailable")
        self.assertEqual(claude_read_hook.validate(self.payload("cat readme-link"))[
            "hookSpecificOutput"]["permissionDecision"], "deny")

    def test_unrelated_tool_has_no_decision(self):
        self.assertIsNone(claude_read_hook.validate(self.payload("anything", "Read")))

    def test_monitor_commands_are_denied_but_websocket_sources_are_not(self):
        # Monitor runs its command in Bash's shell, which would skip the read-only check.
        for tool_input in ({"command": "cat README.md", "description": "x"}, {"description": "x"}, "bad"):
            with self.subTest(tool_input=tool_input):
                result = claude_read_hook.validate({**self.payload("", "Monitor"), "tool_input": tool_input})
                self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIsNone(claude_read_hook.validate(
            {**self.payload("", "Monitor"), "tool_input": {"ws": {"url": "wss://example.test"}, "description": "x"}}))

    def test_bad_payload_or_validator_failure_denies(self):
        for value in ("not json", "[]", json.dumps(self.payload(None))):
            with self.subTest(value=value), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(claude_read_hook.main(io.StringIO(value)), 0)
                self.assertEqual(json.loads(output.getvalue())["hookSpecificOutput"]["permissionDecision"], "deny")
        with patch("cli_subagents.claude_read_hook.validate", side_effect=RuntimeError), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            claude_read_hook.main(io.StringIO(json.dumps(self.payload("ls"))))
        self.assertEqual(json.loads(output.getvalue())["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_entry_point_runs_in_isolation_from_any_working_folder(self):
        script = Path(claude_read_hook.__file__).resolve()
        # Neither cwd nor PYTHONPATH may shadow stdlib/package modules in the hook.
        (self.root / "json.py").write_text("raise RuntimeError('shadowed')", encoding="utf-8")
        (self.root / "cli_subagents.py").write_text("raise RuntimeError('shadowed')", encoding="utf-8")
        env = {**os.environ, "PYTHONPATH": str(self.root), "CLAUDE_CONFIG_DIR": str(self.root / "account")}
        for command in ("ls", "git diff --output=x"):
            completed = subprocess.run([sys.executable, "-I", str(script)], input=json.dumps(self.payload(command)),
                                       capture_output=True, text=True, cwd=self.root, env=env, timeout=30)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            if command == "ls":
                self.assertEqual(completed.stdout, "")
            else:
                self.assertEqual(json.loads(completed.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_hook_command_runs_through_available_shells(self):
        entry = hooks.worker_read_hook()["hooks"][0]
        shells = []
        if os.name == "nt":
            shells.append(["powershell", "-NoProfile", "-Command", entry["command"]])
            for candidate in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe"):
                if Path(candidate).is_file() and entry.get("shell") != "powershell":
                    shells.append([candidate, "-c", entry["command"]])
                    break
        else:
            shells.append(["/bin/sh", "-c", entry["command"]])
        for argv in shells:
            for command in ("Get-Content README.md", "git diff --output=x"):
                with self.subTest(shell=argv[0], command=command):
                    completed = subprocess.run(argv, input=json.dumps(self.payload(command, "PowerShell")),
                                               capture_output=True, text=True, cwd=self.root, timeout=30)
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    if "--output" in command:
                        self.assertEqual(json.loads(completed.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
                    else:
                        self.assertEqual(completed.stdout, "")


class SettingsTests(TempHome):
    def settings(self, overlay, access="read"):
        path = self.root / "overlay.json"
        path.write_text(json.dumps(overlay), encoding="utf-8")
        result = ClaudeRuntime()._job_settings("test-read", {"settings_path": str(path)}, [], access=access)
        return json.loads(result.read_text(encoding="utf-8"))

    def test_read_guard_preserves_user_and_inbox_hooks(self):
        own = {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo user"}]}
        stop = {"hooks": [{"type": "command", "command": "echo stop"}]}
        settings = self.settings({"hooks": {"PreToolUse": [own], "Stop": [stop]}})
        self.assertEqual(settings["hooks"]["PreToolUse"], [own, hooks.worker_read_hook()])
        self.assertEqual(settings["hooks"]["Stop"], [stop])
        self.assertEqual(len(settings["hooks"]["PostToolUse"]), 1)
        self.assertIs(settings["disableAllHooks"], False)

    def test_unusable_guard_settings_refuse_read_launch(self):
        for overlay in ({"hooks": []}, {"hooks": {"PreToolUse": "wrong shape"}}, {"disableAllHooks": True}):
            with self.subTest(overlay=overlay), self.assertRaisesRegex(ValueError, "read-only shell guard"):
                self.settings(overlay)

    def test_edit_settings_keep_user_pretooluse_and_disabled_hooks(self):
        overlay = {"disableAllHooks": True, "hooks": {"PreToolUse": []}}
        settings = self.settings(overlay, access="edit")
        self.assertIs(settings["disableAllHooks"], True)
        self.assertEqual(settings["hooks"]["PreToolUse"], [])
