"""Relay files and stdin preserve literal options and message text."""

import contextlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock, patch

from helpers import TempHome
from cli_subagents import cli, hooks, jobs, relay_input


SPECIAL = "user's \"words\" C:\\Users\\O'Brien\\ $HOME $(echo nope) `echo nope` café 日本語 😀\u2028end"


class RelayPromptTests(unittest.TestCase):
    def test_values_are_literal_data(self):
        values = {"model": SPECIAL, "effort": SPECIAL, "cwd": SPECIAL, "override": SPECIAL,
                  "account": "paid-account", "access": "full", "window": "120", "timeout": "300"}
        text = "\n".join(f"{key}={value}" for key, value in values.items()) + "\n\nTask\n  spaces  \n"
        options, task = cli._relay_prompt(text)
        self.assertEqual(options, {**values, "window": 120, "timeout": 300})
        self.assertEqual(task, "Task\n  spaces  \n")

    def test_no_option_lines_preserves_the_whole_task(self):
        for task in (SPECIAL, "Task: inspect this\nmodel=part of task\n", "\nFirst line\n", "```\naccess=edit\n```\n"):
            with self.subTest(task=task):
                self.assertEqual(cli._relay_prompt(task), ({}, task))

    def test_only_leading_options_are_parsed(self):
        task = "Do this\nmodel=task text\nunknown=value\n\n" + SPECIAL
        self.assertEqual(cli._relay_prompt("access=read\n" + task), ({"access": "read"}, task))
        self.assertEqual(cli._relay_prompt("access=read\n\n\n" + task), ({"access": "read"}, "\n" + task))

    def test_stdin_byte_order_mark_is_dropped_once(self):
        with patch.object(sys, "stdin", io.StringIO("﻿access=read\n\n﻿Task")):
            self.assertEqual(cli._read_stdin(), "access=read\n\n﻿Task")

    def test_colon_compatibility_and_crlf(self):
        self.assertEqual(cli._relay_prompt("model: gpt-test\r\naccess: read\r\n\r\nTask\r\n"),
                         ({"model": "gpt-test", "access": "read"}, "Task\r\n"))

    def test_empty_unknown_duplicate_and_invalid_options_are_rejected(self):
        invalid = [f"{key}={empty}" for key in cli._RELAY_KEYS for empty in ("", "   ")]
        invalid += ["unknown=value", "runtime=claude", "access=write", "account=bad name",
                    "model=one\nmodel=two", "override=bad\x00value",
                    "window=0", "window=-1", "window=1; echo nope", "window=١", "window=1.5",
                    "timeout=29", "timeout=21601", "timeout=nan"]
        for option in invalid:
            with self.subTest(option=option), self.assertRaises(ValueError):
                cli._relay_prompt(option + "\n\nTask")
        for text in ("", "  \n", "model=test\n", "access=edit\n\n"):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "task is empty"):
                cli._relay_prompt(text)


class RelayRunTests(TempHome):
    def setUp(self):
        super().setUp()
        # TempHome's outer environment patch restores these before cleanup ends.
        os.environ.update({
            "CLAUDE_CONFIG_DIR": str(self.primary), "CODEX_HOME": str(self.primary),
            "PYTHONIOENCODING": "utf-8",
            "CLI_SUBAGENTS_CODEX_COMMAND": json.dumps([sys.executable, str(Path(__file__).with_name("fake_codex.py"))]),
        })
        (self.home / "config.toml").write_text(
            '[codex]\nprimary = ' + json.dumps(str(self.primary)) + '\n', encoding="utf-8")

    def csa(self, payload, *extra):
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.stdin", io.StringIO(payload)), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["run", "--follow", "--window", "60", "--runtime", "codex", "--relay", "-", *extra])
        return code, out.getvalue(), err.getvalue()

    def test_subprocess_run_reaches_fake_codex_with_exact_task_and_values(self):
        project = self.project / "O'Brien café"
        project.mkdir()
        task = "Do this unchanged: " + SPECIAL + "\n\nmodel=task text\n  final spaces  \n"
        payload = f"cwd={project}\nmodel=gpt-test\neffort=high\naccount=primary\noverride={SPECIAL}\nwindow=120\n\n" + task
        result = subprocess.run(
            [sys.executable, "-m", "cli_subagents", "run", "--follow", "--window", "60", "--runtime", "codex", "--relay", "-"],
            input=payload, text=True, encoding="utf-8", capture_output=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        job_id = re.search(r" · job ([a-z0-9-]+) · token", result.stdout).group(1)
        spec = jobs.read_spec(job_id)
        self.assertEqual((spec.prompt, spec.override, spec.account, spec.access), (task, SPECIAL, "primary", "read"))
        record = self.records()[-1]
        self.assertTrue(record["stdin"].endswith(task), repr(record["stdin"]))
        self.assertNotIn("cwd=", record["stdin"])
        self.assertEqual(record["env"]["CLI_SUBAGENTS_JOB_ID"], job_id)
        self.assertIn("[csa] DONE", result.stdout.splitlines()[-1])

    def test_relay_defaults_access_and_overrides_window_without_shell_arguments(self):
        for runtime, access in (("claude", "edit"), ("codex", "read"), ("antigravity", "read")):
            with self.subTest(runtime=runtime), patch("cli_subagents.jobs.create_job", return_value={"job_id": "test-job"}) as create, \
                    patch("cli_subagents.follow.follow", return_value=0) as watching, \
                    patch("sys.stdin", io.StringIO("window=120\ntimeout=300\nTask")):
                cli.main(["run", "--follow", "--window", "60", "--runtime", runtime, "--relay", "-"])
                self.assertEqual(create.call_args.kwargs["access"], access)
                self.assertEqual(create.call_args.kwargs["timeout_seconds"], 300)
                self.assertEqual(watching.call_args.kwargs["window"], 120)

    def test_invalid_relay_returns_authenticated_failed_without_starting(self):
        for payload in ("unknown=oops\nTask", "access=edit\nwindow=0\nTask", "override=\nTask", "cwd=nonexistent\nTask"):
            with self.subTest(payload=payload), patch("cli_subagents.jobs.create_job") as create:
                if payload.startswith("cwd="):
                    create.side_effect = ValueError("cwd must be an existing absolute directory")
                code, out, err = self.csa(payload)
                self.assertEqual(code, 1, out + err)
                lines = out.splitlines()
                frame = re.search(r" · job ([a-z0-9-]+) · token ([0-9a-f]{32})$", lines[0])
                self.assertIsNotNone(frame, out)
                self.assertTrue(lines[-1].startswith("[csa] FAILED"), out)
                self.assertTrue(lines[-1].endswith(frame[0]), out)
                self.assertEqual(lines[1], "===== RESULT =====")
                self.assertTrue(all(line.startswith("| ") for line in lines[2:-2]), out)
                self.assertEqual(lines[-2], "===== END RESULT =====")
                self.assertEqual(err, "")
                if not payload.startswith("cwd="):
                    create.assert_not_called()
        self.assertFalse((self.home / "jobs").exists())

    def test_options_require_stdin_and_admission_failures_keep_framing(self):
        out = io.StringIO()
        with patch("cli_subagents.jobs.create_job") as create, contextlib.redirect_stdout(out):
            code = cli.main(["run", "--relay", "not stdin"])
        self.assertEqual(code, 1)
        create.assert_not_called()
        self.assertIn("[csa] FAILED", out.getvalue())
        with patch("cli_subagents.jobs.create_job", side_effect=ValueError("spend cap reached; override does not bypass it")):
            code, out, err = self.csa("account=primary\noverride=" + SPECIAL + "\nTask")
        self.assertEqual(code, 1)
        self.assertIn("| spend cap reached; override does not bypass it", out)

    def test_message_stdin_is_literal_and_never_parsed_as_relay_options(self):
        payload = "model=task text\noverride=user's follow-up\n" + SPECIAL
        with patch("cli_subagents.tools.call", return_value={}) as call, patch("sys.stdin", io.StringIO(payload)), \
                contextlib.redirect_stdout(io.StringIO()):
            cli.main(["message", "saved-job", "--reply-to", "deadbeef", "-"])
        self.assertEqual(call.call_args.args[1], {"job_id": "saved-job", "text": payload, "reply_to": "deadbeef"})

    def test_large_inbox_prompt_reaches_fake_codex_and_is_deleted(self):
        task = (SPECIAL + "\n'@\nCSA_RELAY_PAYLOAD_8c85aaf97de2456c9a544d2c09b16eb1\n") * 130 + "  no final newline"
        self.assertGreater(len(task), 10_000)
        path = relay_input.fresh_path()
        path.write_bytes((f"cwd={self.project}\nmodel=gpt-test\naccess=read\n\n" + task).encode("utf-8"))
        result = subprocess.run(
            [sys.executable, "-m", "cli_subagents", "run", "--follow", "--window", "60", "--runtime", "codex",
             "--relay-file", str(path)], text=True, encoding="utf-8", capture_output=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(path.exists())
        job_id = re.search(r" · job ([a-z0-9-]+) · token", result.stdout).group(1)
        self.assertEqual(jobs.read_spec(job_id).prompt, task)
        self.assertTrue(self.records()[-1]["stdin"].endswith(task))
        self.assertIn("[csa] DONE", result.stdout.splitlines()[-1])

    def test_file_options_use_existing_defaults_window_and_admission(self):
        for runtime, access in (("claude", "edit"), ("codex", "read"), ("antigravity", "read")):
            path = relay_input.fresh_path()
            path.write_bytes(("window=120\ntimeout=300\noverride=" + SPECIAL + "\n\nTask").encode("utf-8"))
            with self.subTest(runtime=runtime), patch("cli_subagents.jobs.create_job", return_value={"job_id": "test-job"}) as create, \
                    patch("cli_subagents.follow.follow", return_value=0) as follow:
                cli.main(["run", "--follow", "--runtime", runtime, "--relay-file", str(path)])
                self.assertEqual(create.call_args.kwargs["access"], access)
                self.assertEqual(create.call_args.kwargs["override"], SPECIAL)
                self.assertEqual(create.call_args.kwargs["timeout_seconds"], 300)
                self.assertEqual(follow.call_args.kwargs["window"], 120)
                self.assertFalse(path.exists())


class RelayInboxBase(TempHome):
    def setUp(self):
        super().setUp()
        # TempHome restores its outer environment in tearDown.
        os.environ.update({"CLAUDE_CONFIG_DIR": str(self.primary), "CODEX_HOME": str(self.primary)})
        self._cwd = patch("cli_subagents.relay_input.Path.cwd", return_value=self.project)
        self._cwd.start()

    def tearDown(self):
        self._cwd.stop()
        super().tearDown()

    def payload(self, text="Task", *, workspace=False):
        path = relay_input.fresh_path(workspace=workspace)
        path.write_bytes(text.encode("utf-8"))
        return path

    def invoke(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def assert_failed(self, result):
        code, out, err = result
        self.assertEqual(code, 1, out + err)
        lines = out.splitlines()
        frame = re.search(r" · job ([a-z0-9-]+) · token ([0-9a-f]{32})$", lines[0])
        self.assertIsNotNone(frame, out)
        self.assertTrue(lines[-1].startswith("[csa] FAILED"), out)
        self.assertTrue(lines[-1].endswith(frame[0]), out)
        self.assertEqual(lines[1], "===== RESULT =====")
        self.assertEqual(lines[-2], "===== END RESULT =====")
        self.assertTrue(all(line.startswith("| ") for line in lines[2:-2]), out)
        self.assertEqual(err, "")


class RelayInboxTests(RelayInboxBase):
    def test_path_command_returns_new_absolute_paths_and_workspace_stays_local(self):
        for flags, parent in (([], self.home / "relay-inbox"), (["--workspace"], self.project / ".csa-relay-inbox")):
            with self.subTest(flags=flags):
                code, out, err = self.invoke("relay-path", *flags)
                path = Path(out.strip())
                self.assertEqual((code, err), (0, ""))
                self.assertTrue(path.is_absolute())
                self.assertEqual(path.parent, parent)
                self.assertRegex(path.name, relay_input.NAME)
                self.assertFalse(path.exists())
                self.assertNotEqual(path, relay_input.fresh_path(workspace=bool(flags)))

    def test_plain_files_preserve_crlf_whitespace_paths_and_missing_final_newline(self):
        for text in (SPECIAL, "\r\n  text  \r\n\r\n", "no final newline", "", '*** End Patch\n+data\n"\\',
                     '"C:\\new\\tools"', '{}', '[]'):
            for workspace in (False, True):
                with self.subTest(text=text, workspace=workspace):
                    path = self.payload(text, workspace=workspace)
                    self.assertEqual(relay_input.consume(str(path)), text)
                    self.assertFalse(path.exists())

    def test_file_answer_and_followup_are_literal_and_follow_the_continuation(self):
        text = "model=message text\r\noverride=still message\n" + SPECIAL * 130
        for reply, options in ((None, ["--follow", "--from", "99", "--window", "120"]),
                               ("deadbeef", ["--reply-to", "deadbeef"])):
            path = self.payload(text)
            with self.subTest(reply=reply), \
                    patch("cli_subagents.tools.call", return_value={"new_job": {"job_id": "continued"}}) as call, \
                    patch("cli_subagents.follow.follow", return_value=0) as follow:
                result = self.invoke("message", "saved-job", *options, "--relay-file", str(path))
                self.assertEqual(result[0], 0, result)
                self.assertEqual(call.call_args.args[1], {"job_id": "saved-job", "text": text, "reply_to": reply})
                if reply is None:
                    self.assertEqual(follow.call_args.args[:2], ("continued", None))
                    self.assertEqual(follow.call_args.kwargs["window"], 120)
                else:
                    follow.assert_not_called()
                self.assertFalse(path.exists())

    def test_outside_traversal_nested_and_bad_names_are_not_opened(self):
        valid = self.payload()
        outside = self.project / valid.name
        outside.write_text("private data", encoding="utf-8")
        nested = valid.parent / "nested"
        nested.mkdir()
        (nested / valid.name).write_text("Task", encoding="utf-8")
        bad_paths = [str(outside), str(valid.parent / "nested" / valid.name),
                     str(valid.parent / ".." / "relay-inbox" / valid.name), "relay-" + "a" * 32 + ".txt",
                     str(valid.parent / "config.toml"), str(valid) + ":stream", str(valid.parent / (valid.stem + ".TXT"))]
        for value in bad_paths:
            for command in (["run"], ["message", "saved-job"]):
                with self.subTest(value=value, command=command), patch("cli_subagents.relay_input.os.open") as opening, \
                        patch("cli_subagents.jobs.create_job") as create, patch("cli_subagents.tools.call") as call:
                    self.assert_failed(self.invoke(*command, "--relay-file", value))
                    opening.assert_not_called()
                    create.assert_not_called()
                    call.assert_not_called()
        self.assertEqual(outside.read_text(encoding="utf-8"), "private data")
        self.assertTrue(valid.exists())

    def test_directories_hardlinks_oversized_and_missing_files_are_not_opened(self):
        folder = relay_input.fresh_path()
        folder.mkdir()
        large = self.payload()
        with large.open("wb") as stream:
            stream.truncate(relay_input.MAX_BYTES + 1)
        original = self.project / "private.txt"
        original.write_text("private data", encoding="utf-8")
        linked = relay_input.fresh_path()
        os.link(original, linked)
        for value in (folder, large, linked, relay_input.fresh_path()):
            with self.subTest(path=value), patch("cli_subagents.relay_input.os.open") as opening:
                self.assert_failed(self.invoke("run", "--relay-file", str(value)))
                opening.assert_not_called()
        self.assertTrue(large.exists())
        self.assertTrue(linked.exists())
        self.assertEqual(original.read_text(encoding="utf-8"), "private data")

    def test_symlink_and_junction_escapes_are_not_opened(self):
        from cli_subagents.fsutil import make_dir_link, remove_link

        outside = self.project / "outside"
        outside.mkdir()
        original = outside / ("relay-" + "f" * 32 + ".txt")
        original.write_text("private data", encoding="utf-8")
        inbox = relay_input.fresh_path().parent
        inbox.rmdir()
        kind = make_dir_link(outside, inbox)
        if kind == "copy":
            self.skipTest("directory links unavailable")
        self.addCleanup(lambda: remove_link(inbox) if os.path.lexists(inbox) else None)
        with patch("cli_subagents.relay_input.os.open") as opening:
            self.assert_failed(self.invoke("run", "--relay-file", str(inbox / original.name)))
            opening.assert_not_called()
        with self.assertRaisesRegex(ValueError, "links or junctions"):
            relay_input.fresh_path()
        self.assertIsNone(hooks.relay_write_permission({
            "hook_event_name": "PreToolUse", "tool_name": "Write",
            "agent_type": "cli-subagents:cli-subagents-codex",
            "tool_input": {"file_path": str(inbox / ("relay-" + "a" * 32 + ".txt")), "content": "Task"},
        }))
        self.assertEqual(original.read_text(encoding="utf-8"), "private data")

    def test_file_symlink_escape_is_not_opened(self):
        original = self.project / "private.txt"
        original.write_text("private data", encoding="utf-8")
        path = relay_input.fresh_path()
        try:
            path.symlink_to(original)
        except OSError:
            self.skipTest("file symlinks unavailable")
        with patch("cli_subagents.relay_input.os.open") as opening:
            self.assert_failed(self.invoke("message", "saved-job", "--relay-file", str(path)))
            opening.assert_not_called()
        self.assertTrue(path.is_symlink())

    def test_invalid_encoding_options_and_admission_delete_valid_files_and_fail_authenticated(self):
        for contents in (b"\xff", b"unknown=value\nTask", b"window=0\nTask", b"access=read\n\n"):
            path = self.payload()
            path.write_bytes(contents)
            with patch("cli_subagents.jobs.create_job") as create:
                self.assert_failed(self.invoke("run", "--relay-file", str(path)))
                create.assert_not_called()
            self.assertFalse(path.exists())
        path = self.payload("account=primary\nTask")
        with patch("cli_subagents.jobs.create_job", side_effect=ValueError("spend cap reached")):
            self.assert_failed(self.invoke("run", "--relay-file", str(path)))
        self.assertFalse(path.exists())

    def test_file_input_cannot_be_combined_with_stdin_or_inline_text(self):
        for command in (["run"], ["message", "saved-job"]):
            path = self.payload()
            self.assert_failed(self.invoke(*command, "-", "--relay-file", str(path)))
            self.assertTrue(path.exists())

    def test_replacement_during_open_is_rejected_before_reading(self):
        path = self.payload()
        replacement = self.project / "replacement.txt"
        replacement.write_text("private data", encoding="utf-8")
        real_open = os.open

        def swap_and_open(value, flags):
            os.replace(replacement, path)
            return real_open(value, flags)

        real_fdopen = os.fdopen
        reads = []

        @contextlib.contextmanager
        def tracked_fdopen(descriptor, mode):
            with real_fdopen(descriptor, mode) as stream:
                tracked = Mock(wraps=stream)
                reads.append(tracked.read)
                yield tracked

        with patch("cli_subagents.relay_input.os.open", side_effect=swap_and_open), \
                patch("cli_subagents.relay_input.os.fdopen", side_effect=tracked_fdopen):
            with self.assertRaisesRegex(ValueError, "changed while opening"):
                relay_input.consume(str(path))
        self.assertEqual(len(reads), 1)
        reads[0].assert_not_called()
        self.assertEqual(path.read_text(encoding="utf-8"), "private data")

    def test_fresh_path_cleans_only_stale_regular_payloads(self):
        stale = self.payload()
        fresh = self.payload()
        unrelated = stale.parent / "notes.txt"
        unrelated.write_text("keep", encoding="utf-8")
        nested = stale.parent / "nested"
        nested.mkdir()
        nested_file = nested / stale.name
        nested_file.write_text("keep", encoding="utf-8")
        old = time.time() - relay_input.STALE_SECONDS - 60
        for path in (stale, unrelated, nested_file):
            os.utime(path, (old, old))
        relay_input.fresh_path()
        self.assertFalse(stale.exists())
        for path in (fresh, unrelated, nested_file):
            self.assertTrue(path.exists())

    def test_workspace_payloads_are_git_ignored_and_cleanup_keeps_ignore_file(self):
        git = shutil.which("git")
        if git is None:
            self.skipTest("Git is not installed")
        subprocess.run([git, "init", "--quiet", str(self.project)], check=True, capture_output=True, timeout=20)
        path = self.payload("left after a failed run", workspace=True)
        ignore = path.parent / ".gitignore"
        self.assertEqual(ignore.read_bytes(), b"*\n")
        ignored = subprocess.run([git, "-C", str(self.project), "check-ignore", "--", str(path)],
                                 capture_output=True, text=True, timeout=20)
        self.assertEqual(ignored.returncode, 0, ignored.stdout + ignored.stderr)
        old = time.time() - relay_input.STALE_SECONDS - 60
        for file in (path, ignore):
            os.utime(file, (old, old))
        relay_input.cleanup(path.parent)
        self.assertFalse(path.exists())
        self.assertEqual(ignore.read_bytes(), b"*\n")

    def test_workspace_ignore_is_repaired_without_writing_through_a_hardlink(self):
        path = relay_input.fresh_path(workspace=True)
        ignore = path.parent / ".gitignore"
        original = self.project / "original.txt"
        original.write_text("keep this", encoding="utf-8")
        ignore.unlink()
        os.link(original, ignore)
        relay_input.fresh_path(workspace=True)
        self.assertEqual(ignore.read_bytes(), b"*\n")
        self.assertEqual(original.read_text(encoding="utf-8"), "keep this")


class RelayWriteHookTests(RelayInboxBase):
    def request(self, path=None, **updates):
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Write",
                   "agent_type": "cli-subagents:cli-subagents-codex",
                   "tool_input": {"file_path": str(path or relay_input.fresh_path()), "content": SPECIAL}}
        payload.update(updates)
        return payload

    def test_hook_allows_only_new_home_inbox_writes_by_known_relays(self):
        for runtime in ("claude", "codex", "antigravity"):
            for prefix in ("", "cli-subagents:"):
                request = self.request(agent_type=prefix + "cli-subagents-" + runtime)
                output = hooks.relay_write_permission(request)
                self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "allow")
        invalid = [self.request(self.payload()), self.request(relay_input.fresh_path(workspace=True)),
                   self.request(self.project / ("relay-" + "a" * 32 + ".txt")),
                   self.request(tool_name="Edit"), self.request(hook_event_name="PostToolUse"),
                   self.request(agent_type="Explore"), self.request(agent_type=None),
                   self.request(agent_type=["cli-subagents-codex"]),
                   self.request(tool_input=None), self.request(tool_input={"file_path": 123, "content": "Task"}),
                   self.request(tool_input={"file_path": str(relay_input.fresh_path()), "content": "x" * (relay_input.MAX_BYTES + 1)})]
        for request in invalid:
            with self.subTest(request=str(request)[:120]):
                self.assertIsNone(hooks.relay_write_permission(request))

    def test_hook_command_emits_documented_allow_and_stays_silent_otherwise(self):
        for request, allowed in ((self.request(), True), (self.request(agent_type="other"), False), ({}, False)):
            with patch("sys.stdin", io.StringIO(json.dumps(request))):
                code, out, err = self.invoke("hook", "relay-write")
            self.assertEqual((code, err), (0, ""))
            if allowed:
                self.assertEqual(json.loads(out)["hookSpecificOutput"], {
                    "hookEventName": "PreToolUse", "permissionDecision": "allow",
                    "permissionDecisionReason": "New CLI Subagents relay payload in the csa inbox"})
            else:
                self.assertEqual(out, "")
