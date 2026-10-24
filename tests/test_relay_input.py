"""Relay options are stdin data, including shell metacharacters."""

import contextlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import unittest
from unittest.mock import patch

from helpers import TempHome
from cli_subagents import cli, jobs


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
