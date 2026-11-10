"""csa re-asks once when a Claude Code relay's transcript shows it skipped a message block.

The relay's newest turn is usually not in its transcript yet when its next
`csa follow` runs (seen live on 2026-11-04), so the tests write the transcript in
that order: csa looks, then the turn lands, then the next tool result.
"""

import io
import json
import os
from pathlib import Path
import unittest

from helpers import TempHome
from cli_subagents import identity, jobs
from cli_subagents.follow import IDENTITY_SHOWN, MESSAGES_END_RULE, MESSAGES_RULE, REASK_LINE, RELAY_PENDING, follow

SESSION = "0f5b1c2d-aaaa-bbbb-cccc-123456789abc"
MESSAGE = "Checking the parser’s tests now."
LATER = "Running the full suite next."


def tool_result(text, as_list=False):
    content = [{"type": "text", "text": text}] if as_list else text
    return {"type": "user", "message": {"content": [{"type": "tool_result", "content": content}]}}


def wrote(text):
    return {"type": "assistant", "message": {"model": "claude-haiku-5-5", "content": [{"type": "text", "text": text}]}}


def calls():
    return {"type": "assistant", "message": {"model": "claude-haiku-5-5", "content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": "csa follow"}}]}}


def says(text):
    return [{"type": "item.completed", "item": {"type": "agent_message", "text": text}},
            {"type": "item.started", "item": {"type": "command_execution", "command": "pytest"}}]


class CopiedTests(unittest.TestCase):
    def test_copied_allows_quote_style_and_spacing_only(self):
        self.assertTrue(identity.copied(MESSAGE, "Checking the parser's tests now."))
        self.assertTrue(identity.copied("a\n\nb  c", "a b c"))
        self.assertTrue(identity.copied(MESSAGE, "Here: " + MESSAGE))
        self.assertFalse(identity.copied(MESSAGE, "I'm writing the worker's message as shown."))
        self.assertFalse(identity.copied(MESSAGE, ""))

    def test_written_after_only_judges_a_turn_closed_by_the_next_result(self):
        import tempfile

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "t.jsonl"
            path.write_text("\n".join(json.dumps(entry) for entry in (
                tool_result("| " + MESSAGE), wrote(MESSAGE), calls())) + "\n", encoding="utf-8")
            self.assertIsNone(identity.written_after(path, "| " + MESSAGE))  # turn may be incomplete
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(tool_result("next output", as_list=True)) + "\n")
            self.assertEqual(identity.written_after(path, "| " + MESSAGE), MESSAGE)
            self.assertIsNone(identity.written_after(path, "| never shown"))


class ReaskTests(TempHome):
    def setUp(self):
        super().setUp()
        self.write_config()
        os.environ["CLAUDE_CODE_SESSION_ID"] = SESSION
        folder = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / "C--work" / SESSION / "subagents"
        folder.mkdir(parents=True)
        (folder / "agent-r1.meta.json").write_text(json.dumps({"agentType": "cli-subagents:cli-subagents-codex"}))
        self.transcript = folder / "agent-r1.jsonl"
        self.job = jobs.create_job(runtime="codex", prompt="task", cwd=str(self.project), start=False)["job_id"]
        (jobs.job_dir(self.job) / IDENTITY_SHOWN).write_text("", encoding="utf-8")
        self.events = jobs.job_dir(self.job) / "events.jsonl"
        self.add(says(MESSAGE))

    def add(self, events):
        with self.events.open("a", encoding="utf-8") as stream:
            stream.write("".join(json.dumps(event) + "\n" for event in events))
        return self.events.stat().st_size

    def watch(self, start=None):
        out = io.StringIO()
        follow(self.job, start=start, max_wait=540, out=out, window=1)
        return out.getvalue()

    def transcribe(self, *entries):
        with self.transcript.open("a", encoding="utf-8") as stream:
            for entry in entries:
                stream.write(json.dumps(entry) + "\n")

    def skipped_once(self):
        """Deliver the block, let the relay skip it, and return the call that re-asks it."""

        end = self.events.stat().st_size
        self.transcribe(tool_result(self.watch()))
        second = self.watch(start=end)  # the relay's turn isn't on disk yet: nothing is known
        self.assertNotIn(REASK_LINE, second)
        self.transcribe(calls(), tool_result(second))  # its turn: only the next call
        return self.watch(start=end), end

    def test_a_written_block_is_not_asked_again(self):
        end = self.events.stat().st_size
        first = self.watch()
        self.assertIn("| " + MESSAGE, first)
        self.transcribe(tool_result(first, as_list=True))
        second = self.watch(start=end)
        self.transcribe(wrote("Checking the parser's tests now."), calls(), tool_result(second, as_list=True))
        third = self.watch(start=end)
        self.assertNotIn(REASK_LINE, third)
        self.assertFalse((jobs.job_dir(self.job) / RELAY_PENDING).exists())

    def test_a_turn_not_yet_on_disk_is_not_read_as_skipped(self):
        # Seen live with 0.2.34: the relay wrote every block, but its turn reached the
        # transcript only after csa had looked, and every block came out twice.
        end = self.events.stat().st_size
        self.transcribe(tool_result(self.watch()))
        second = self.watch(start=end)
        self.assertNotIn(REASK_LINE, second)
        self.assertNotIn(MESSAGES_RULE, second)

    def test_a_skipped_block_is_asked_again_one_call_later_and_only_once(self):
        third, end = self.skipped_once()
        lines = third.splitlines()
        self.assertIn(REASK_LINE, lines)
        self.assertEqual(lines[lines.index(MESSAGES_RULE) + 1:lines.index(MESSAGES_END_RULE)], ["| " + MESSAGE])
        self.assertIn(f"--from {end} --window 1 #", lines[lines.index(REASK_LINE) - 1])
        self.assertTrue(lines[-1].startswith("[csa] STILL RUNNING"))
        # Skipped again (with a sentence about it instead): csa moves on instead of looping.
        self.transcribe(tool_result(third))
        fourth = self.watch(start=end)
        self.transcribe(wrote("I'm writing the worker's message as shown."), calls(), tool_result(fourth))
        self.assertNotIn(REASK_LINE, self.watch(start=end))
        self.assertFalse((jobs.job_dir(self.job) / RELAY_PENDING).exists())

    def test_a_reasked_block_written_the_second_time_is_accepted(self):
        third, end = self.skipped_once()
        self.transcribe(tool_result(third))
        fourth = self.watch(start=end)
        self.transcribe(wrote(MESSAGE), calls(), tool_result(fourth))
        self.assertNotIn(REASK_LINE, self.watch(start=end))
        self.assertFalse((jobs.job_dir(self.job) / RELAY_PENDING).exists())

    def test_a_skip_found_after_the_next_block_was_delivered_reasks_only_the_skipped_one(self):
        first_end = self.events.stat().st_size
        first = self.watch()
        self.transcribe(tool_result(first))
        second_end = self.add(says(LATER))
        second = self.watch(start=first_end)  # delivers the later block; the first is undecided
        self.assertIn("| " + LATER, second)
        self.assertNotIn(REASK_LINE, second)
        self.transcribe(calls(), tool_result(second))  # the first block was skipped
        third = self.watch(start=second_end)
        lines = third.splitlines()
        self.assertEqual(lines[lines.index(MESSAGES_RULE) + 1:lines.index(MESSAGES_END_RULE)], ["| " + MESSAGE])
        self.assertIn(f"--from {second_end} --window 1 #", lines[lines.index(REASK_LINE) - 1])

    def test_blocks_sharing_a_first_line_are_told_apart(self):
        # Found by a Gemini review of 0.2.39: matching on the first line alone bound an older
        # block to a newer block's tool result.
        first_text, second_text = "```\nfirst code\n```", "```\nsecond code\n```"
        first_end = self.add(says(first_text))
        self.watch(start=self.events.stat().st_size)  # nothing new yet
        (jobs.job_dir(self.job) / RELAY_PENDING).unlink(missing_ok=True)
        first = self.watch(start=self.offset_before(first_text))
        self.transcribe(tool_result(first))
        second_end = self.add(says(second_text))
        second = self.watch(start=first_end)
        self.assertIn("| second code", second)
        self.transcribe(wrote(first_text), calls(), tool_result(second))  # the first was written
        third = self.watch(start=second_end)
        self.assertNotIn(REASK_LINE, third)  # not confused with the second block's result

    def offset_before(self, text):
        """The byte offset of the event that carries `text`."""

        data = self.events.read_bytes()
        return data.rfind(b"\n", 0, data.find(json.dumps(text).encode())) + 1

    def test_no_reask_without_a_relay_transcript_or_after_the_job_ended(self):
        end = self.events.stat().st_size
        self.transcribe(tool_result(self.watch()))
        second = self.watch(start=end)
        self.transcribe(calls(), tool_result(second))
        os.environ.pop("CLAUDE_CODE_SESSION_ID")  # another host: nothing to check
        self.assertNotIn(REASK_LINE, self.watch(start=end))

        os.environ["CLAUDE_CODE_SESSION_ID"] = SESSION
        first = self.watch(start=0)
        self.transcribe(tool_result(first))
        second = self.watch(start=end)
        self.transcribe(calls(), tool_result(second))
        jobs.write_text(jobs.job_dir(self.job) / "result.md", "done")
        jobs.update_status(self.job, state="done", outcome="success")
        last = self.watch(start=end)
        self.assertNotIn(REASK_LINE, last)
        self.assertTrue(last.splitlines()[-1].startswith("[csa] DONE"))
