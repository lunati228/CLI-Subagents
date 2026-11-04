"""csa re-asks once when a Claude Code relay's transcript shows it skipped a message block."""

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


def tool_result(text, as_list=False):
    content = [{"type": "text", "text": text}] if as_list else text
    return {"type": "user", "message": {"content": [{"type": "tool_result", "content": content}]}}


def wrote(text):
    return {"type": "assistant", "message": {"model": "claude-haiku-5-5", "content": [{"type": "text", "text": text}]}}


def calls():
    return {"type": "assistant", "message": {"model": "claude-haiku-5-5", "content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": "csa follow"}}]}}


class CopiedTests(unittest.TestCase):
    def test_copied_allows_quote_style_and_spacing_only(self):
        self.assertTrue(identity.copied(MESSAGE, "Checking the parser's tests now."))
        self.assertTrue(identity.copied("a\n\nb  c", "a b c"))
        self.assertTrue(identity.copied(MESSAGE, "Here: " + MESSAGE))
        self.assertFalse(identity.copied(MESSAGE, "I'm writing the worker's message as shown."))
        self.assertFalse(identity.copied(MESSAGE, ""))


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
        events = [{"type": "item.completed", "item": {"type": "agent_message", "text": MESSAGE}},
                  {"type": "item.started", "item": {"type": "command_execution", "command": "pytest"}}]
        path = jobs.job_dir(self.job) / "events.jsonl"
        path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
        self.offset = path.stat().st_size

    def watch(self, start=None):
        out = io.StringIO()
        follow(self.job, start=start, max_wait=540, out=out, window=1)
        return out.getvalue()

    def relay_saw(self, output, *entries, as_list=False):
        """Append what a relay would record: the tool result, then its own next turn."""

        with self.transcript.open("a", encoding="utf-8") as stream:
            for entry in (tool_result(output, as_list), *entries):
                stream.write(json.dumps(entry) + "\n")

    def test_a_written_block_is_not_asked_again(self):
        first = self.watch()
        self.assertIn("| " + MESSAGE, first)
        self.relay_saw(first, wrote("Checking the parser's tests now."), calls(), as_list=True)
        second = self.watch(start=self.offset)
        self.assertNotIn(REASK_LINE, second)
        self.assertNotIn(MESSAGES_RULE, second)
        self.assertFalse((jobs.job_dir(self.job) / RELAY_PENDING).exists())

    def test_a_skipped_block_is_asked_again_once_at_the_same_offset(self):
        first = self.watch()
        self.relay_saw(first, calls())
        second = self.watch(start=self.offset)
        lines = second.splitlines()
        self.assertIn(REASK_LINE, lines)
        self.assertEqual(lines[lines.index(MESSAGES_RULE) + 1:lines.index(MESSAGES_END_RULE)], ["| " + MESSAGE])
        self.assertIn(f"--from {self.offset} --window 1 #", lines[lines.index(REASK_LINE) - 1])
        self.assertTrue(lines[-1].startswith("[csa] STILL RUNNING"))
        # Skipped again: csa moves on instead of looping.
        self.relay_saw(second, wrote("I'm writing the worker's message as shown."), calls())
        third = self.watch(start=self.offset)
        self.assertNotIn(REASK_LINE, third)

    def test_a_turn_not_yet_on_disk_is_not_read_as_skipped(self):
        # Seen live: the relay wrote the block, but its turn reached the transcript
        # only after csa had already looked, and every block came out twice.
        first = self.watch()
        self.relay_saw(first)
        second = self.watch(start=self.offset)
        self.assertNotIn(REASK_LINE, second)
        self.assertNotIn(MESSAGES_RULE, second)

    def test_a_reasked_block_written_the_second_time_is_accepted(self):
        first = self.watch()
        self.relay_saw(first, calls())
        second = self.watch(start=self.offset)
        self.relay_saw(second, wrote(MESSAGE), calls())
        self.assertNotIn(REASK_LINE, self.watch(start=self.offset))

    def test_no_reask_without_a_relay_transcript_or_after_the_job_ended(self):
        first = self.watch()
        self.relay_saw(first, calls())
        os.environ.pop("CLAUDE_CODE_SESSION_ID")  # another host: nothing to check
        self.assertNotIn(REASK_LINE, self.watch(start=self.offset))

        os.environ["CLAUDE_CODE_SESSION_ID"] = SESSION
        first = self.watch(start=0)
        self.relay_saw(first, calls())
        jobs.write_text(jobs.job_dir(self.job) / "result.md", "done")
        jobs.update_status(self.job, state="done", outcome="success")
        last = self.watch(start=self.offset)
        self.assertNotIn(REASK_LINE, last)
        self.assertTrue(last.splitlines()[-1].startswith("[csa] DONE"))

    def test_a_call_at_another_offset_is_not_reasked(self):
        first = self.watch()
        self.relay_saw(first, calls())
        replay = self.watch(start=0)  # not the pending block's offset
        self.assertNotIn(REASK_LINE, replay)
        self.assertIn("| " + MESSAGE, replay)  # the replay delivers the block itself
