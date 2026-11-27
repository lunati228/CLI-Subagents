"""DeepSeek workers think out loud in their messages; csa keeps what relays copy to status lines.

A relay that copied DeepSeek's reasoning word for word was refused by Anthropic's
reasoning_extraction classifier (2026-11-22/23). Claude and Codex workers are unchanged.
"""

import io
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

from helpers import TempHome
from cli_subagents import follow as follow_module, jobs
from cli_subagents.follow import (
    COPY_LINE, IDENTITY_SHOWN, MESSAGES_END_RULE, MESSAGES_RULE, RELAY_PENDING, STATUS_COPY_LINE,
    STATUS_MESSAGE_CHARS, follow, status_text,
)
from cli_subagents.runtimes.base import STATUS_NOTE, JobSpec, thinks_aloud
from cli_subagents.runtimes.opencode import OpenCodeRuntime
from cli_subagents.config import RuntimeConfig

DEEPSEEK = "deepseek/deepseek-v4.1-flash-1017"


def claude_text(text):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def claude_tool(command):
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": command}}]}}


class ThinksAloudTests(unittest.TestCase):
    def test_deepseek_models_paid_and_free_think_aloud(self):
        for model in (DEEPSEEK, "opencode/deepseek-v4.1-flash-free", "DeepSeek-V4"):
            self.assertTrue(thinks_aloud(model), model)
        for model in (None, "", "claude-opus-5-5", "gpt-6.2-sol", "gemini-4-hydrogen-high"):
            self.assertFalse(thinks_aloud(model), model)

    def test_status_text_collapses_a_repetition_loop(self):
        loop = "Round 4. Waiting for the run.\n\n" + "Let me check.\n\nI'll check.\n\nLet me check the run status.\n\n" * 500
        self.assertEqual(status_text(loop), "Round 4. Waiting for the run.\n\nLet me check the run status.")

    def test_status_text_drops_bare_filler_and_repeats_but_keeps_real_status(self):
        self.assertEqual(status_text("Let me check."), "")
        self.assertEqual(status_text("Hmm, wait."), "")
        text = "Found the bug in parser.py.\nFound the bug in parser.py.\nNow running the date tests to confirm the fix."
        self.assertEqual(status_text(text), "Found the bug in parser.py.\nNow running the date tests to confirm the fix.")

    def test_status_text_is_capped(self):
        text = status_text("word " * 1000)
        self.assertEqual(len(text), STATUS_MESSAGE_CHARS)
        self.assertTrue(text.endswith("..."))


class ClaudeLaunchTests(TempHome):
    def run_job(self, model):
        self.write_config(accounts=("a1",))
        job = jobs.create_job(runtime="claude", prompt="task", cwd=str(self.project), model=model)
        status = jobs.wait_for(job["job_id"], 60)
        self.assertEqual(status["state"], "done", status)
        argv = self.records()[-1]["argv"]
        note = argv[argv.index("--append-system-prompt") + 1]
        settings = json.loads(Path(argv[argv.index("--settings") + 1]).read_text(encoding="utf-8"))
        return note, settings

    def test_a_deepseek_worker_gets_the_status_note_and_no_output_style(self):
        (self.primary / "settings.json").write_text(json.dumps({"outputStyle": "Explanatory"}), encoding="utf-8")
        note, settings = self.run_job(DEEPSEEK)
        self.assertIn(STATUS_NOTE, note)
        self.assertEqual(settings["outputStyle"], "default")

    def test_a_claude_worker_keeps_the_users_output_style_and_no_status_note(self):
        (self.primary / "settings.json").write_text(json.dumps({"outputStyle": "Explanatory"}), encoding="utf-8")
        note, settings = self.run_job("claude-opus-5-5")
        self.assertNotIn(STATUS_NOTE, note)
        self.assertEqual(settings.get("outputStyle"), "Explanatory")


class OpenCodeLaunchTests(TempHome):
    def setUp(self):
        super().setUp()
        for name in list(os.environ):
            if name.startswith(("OPENCODE_", "FAKE_OPENCODE_")):
                os.environ.pop(name)
        os.environ["CLI_SUBAGENTS_OPENCODE_COMMAND"] = json.dumps(
            [sys.executable, str(Path(__file__).with_name("fake_opencode.py"))])
        for name in ("DATA", "CONFIG", "STATE", "CACHE"):
            os.environ[f"XDG_{name}_HOME"] = str(self.root / name.lower())
        self.config = RuntimeConfig(runtime="opencode", primary=self.root / "config" / "opencode")

    def note(self, model):
        spec = JobSpec(job_id="test-think-aloud", runtime="opencode", prompt="task", cwd=str(self.project), model=model)
        launch = OpenCodeRuntime().launch(self.config, self.config.primary_account(), spec, "task", {})
        return launch.stdin.split("]\n\n", 1)[0]

    def test_only_a_deepseek_worker_gets_the_status_note(self):
        self.assertIn(STATUS_NOTE, self.note("opencode/deepseek-v4.1-flash-free"))
        self.assertNotIn(STATUS_NOTE, self.note("opencode/some-other-model"))


class RelayStatusTests(TempHome):
    def setUp(self):
        super().setUp()
        self.write_config()
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)

    def make_job(self, model, records):
        job = jobs.create_job(runtime="claude", prompt="task", cwd=str(self.project), model=model, start=False)["job_id"]
        (jobs.job_dir(job) / IDENTITY_SHOWN).write_text("", encoding="utf-8")
        with (jobs.job_dir(job) / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write("".join(json.dumps(event) + "\n" for event in records))
        return job

    def watch(self, job, window=60):
        out = io.StringIO()
        follow(job, max_wait=540, out=out, window=window)
        return out.getvalue().splitlines()

    @staticmethod
    def block(lines):
        start, end = lines.index(MESSAGES_RULE), lines.index(MESSAGES_END_RULE)
        return lines[start + 1:end]

    def test_a_deepseek_block_is_cleaned_and_posted_without_word_for_word(self):
        job = self.make_job(DEEPSEEK, [claude_text("Let me check.\n\nFound the bug in parser.py.\n\nLet me check."),
                                       claude_tool("pytest")])
        with patch.object(follow_module, "STATUS_MIN_CALL_SECONDS", 0.0):
            lines = self.watch(job)
        self.assertEqual(self.block(lines), ["| Found the bug in parser.py."])
        self.assertIn(STATUS_COPY_LINE, lines)
        self.assertNotIn(COPY_LINE, lines)
        self.assertFalse((jobs.job_dir(job) / RELAY_PENDING).exists())  # never re-asked

    def test_deepseek_messages_are_batched_until_the_call_has_run_a_while(self):
        job = self.make_job(DEEPSEEK, [claude_text("Reading the parser."), claude_tool("cat parser.py")])
        with patch.object(follow_module, "STATUS_MIN_CALL_SECONDS", 3.0):
            start = time.monotonic()
            lines = self.watch(job)
        self.assertGreaterEqual(time.monotonic() - start, 3.0)
        self.assertEqual(self.block(lines), ["| Reading the parser."])

    def test_a_claude_block_is_unchanged(self):
        job = self.make_job("claude-opus-5-5", [claude_text("Let me check.\n\nFound it."), claude_tool("pytest")])
        start = time.monotonic()
        lines = self.watch(job)
        self.assertLess(time.monotonic() - start, 10)
        self.assertEqual(self.block(lines), ["| Let me check.", "| ", "| Found it."])
        self.assertIn(COPY_LINE, lines)


if __name__ == "__main__":
    unittest.main()
