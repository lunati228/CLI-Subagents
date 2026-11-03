"""The relay's identity line comes from logs: the relay's transcript and the worker's own records."""

import io
import json
import os
from pathlib import Path
import sys
import unittest

from helpers import TempHome
from cli_subagents import identity, jobs
from cli_subagents.follow import IDENTITY_SHOWN, MESSAGES_END_RULE, MESSAGES_RULE, follow

SESSION = "0f5b1c2d-aaaa-bbbb-cccc-123456789abc"
WORKER_SESSION = "452ae516-c1ea-482c-8635-ec786ad4af76"
THREAD = "01a1a299-d195-7862-a820-aae2415b44ee"
INBOX = "relay-38f1c6411d189a95d85ea06851f834d5.txt"


def write_lines(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


def assistant(model, effort):
    return {"type": "assistant", "effort": effort, "message": {"model": model, "content": []}}


class IdentityUnitTests(unittest.TestCase):
    def test_display_names_and_clean_tokens(self):
        self.assertEqual(identity.display_model("claude-haiku-5-5"), "Haiku 5.5")
        self.assertEqual(identity.display_model("claude-opus-4-1-20250805"), "Opus 4.1")
        self.assertEqual(identity.display_model("claude-opus-5"), "Opus 5")
        self.assertEqual(identity.display_model("gpt-6.1-sol"), "gpt-6.1-sol")
        self.assertEqual(identity.clean("opus[1m]"), "opus")
        self.assertIsNone(identity.clean("<synthetic>"))
        self.assertEqual(identity.clean("gpt\n6 [csa] DONE"), "gpt6")

    def test_sentences(self):
        worker = identity.Worker("gpt-6.1-sol", "high", "Codex's session log")
        self.assertEqual(
            identity.sentence(("claude-haiku-5-5", "low"), worker, "codex", "gpt-6.1-sol", "high"),
            "I'm Haiku 5.5 (effort low), the relay agent for Codex gpt-6.1-sol (effort high). "
            "Read from my transcript and Codex's session log.")
        mismatch = identity.Worker("deepseek/deepseek-v4.1-flash-1017", None, "the worker's start event")
        self.assertIn("Asked for opus, but the worker reports deepseek/deepseek-v4.1-flash-1017.",
                      identity.sentence(None, mismatch, "claude", "opus", None))
        self.assertNotIn("Asked", identity.sentence(None, identity.Worker("claude-opus-5-5", "max", "x"),
                                                    "claude", "opus", "max"))
        self.assertIn("Asked for effort high, but the worker reports medium.",
                      identity.sentence(None, identity.Worker("gpt-6.1-sol", "medium", "x"), "codex", None, "high"))
        opencode = identity.sentence(("claude-haiku-5-5", "low"), identity.Worker(None, None, None), "opencode",
                                     "opencode/deepseek-v4.1-flash-free", None)
        self.assertEqual(opencode, "I'm Haiku 5.5 (effort low), the relay agent for OpenCode. Read from my transcript. "
                                   "OpenCode didn't report which model it runs; asked for opencode/deepseek-v4.1-flash-free.")
        self.assertTrue(identity.sentence(None, worker, "codex", None, None).endswith("My own model isn't visible from here."))


class IdentityFollowTests(TempHome):
    def setUp(self):
        super().setUp()
        self.write_config(accounts=())
        os.environ["CLI_SUBAGENTS_OPENCODE_COMMAND"] = json.dumps(
            [sys.executable, str(Path(__file__).with_name("fake_opencode.py"))])
        os.environ["CLAUDE_CODE_SESSION_ID"] = SESSION
        relay = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / "C--work" / SESSION / "subagents"
        write_lines(relay / "agent-a1.jsonl", [
            {"type": "user", "message": {"content": "csa relay-path"}},
            {"type": "assistant", "effort": "low", "message": {"model": "claude-haiku-5-5", "content": [
                {"type": "tool_use", "name": "Write", "input": {"file_path": "C:\\inbox\\" + INBOX}}]}}])
        (relay / "agent-a1.meta.json").write_text(json.dumps({"agentType": "cli-subagents:cli-subagents-claude"}))
        # Another agent of the same session that never saw this relay's inbox.
        write_lines(relay / "agent-a2.jsonl", [assistant("claude-opus-5-5", "xhigh")])

    def job(self, runtime, records, model=None, effort=None):
        job = jobs.create_job(runtime=runtime, prompt="task", cwd=str(self.project), start=False,
                              model=model, effort=effort)["job_id"]
        write_lines(jobs.job_dir(job) / "events.jsonl", records)
        return job

    def first_block(self, job, start=None, marker=INBOX):
        out = io.StringIO()
        follow(job, start=start, max_wait=540, out=out, window=1, marker=marker)
        lines = out.getvalue().splitlines()
        if MESSAGES_RULE not in lines:
            return None
        return lines[lines.index(MESSAGES_RULE) + 1:lines.index(MESSAGES_END_RULE)]

    def test_claude_worker_from_its_transcript_and_relay_from_its_own(self):
        write_lines(self.primary / "projects" / "C--work" / f"{WORKER_SESSION}.jsonl", [assistant("claude-opus-5-5", "high")])
        job = self.job("claude", [
            {"type": "system", "subtype": "init", "model": "claude-opus-5-5", "session_id": WORKER_SESSION},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {}}]}}],
            model="opus", effort="high")
        self.assertEqual(self.first_block(job), [
            "| I'm Haiku 5.5 (effort low), the relay agent for Claude Code Opus 5.5 (effort high). "
            "Read from my transcript and the worker's transcript."])
        self.assertTrue((jobs.job_dir(job) / IDENTITY_SHOWN).exists())
        self.assertIsNone(self.first_block(job, start=0))  # shown once per job

    def test_claude_model_mismatch_from_the_start_event(self):
        job = self.job("claude", [
            {"type": "system", "subtype": "init", "model": "deepseek/deepseek-v4.1-flash-1017", "session_id": WORKER_SESSION},
            {"type": "assistant", "message": {"content": []}}], model="opus")
        [line] = self.first_block(job)
        self.assertIn("the relay agent for Claude Code deepseek/deepseek-v4.1-flash-1017.", line)
        self.assertIn("Asked for opus, but the worker reports deepseek/deepseek-v4.1-flash-1017.", line)

    def test_claude_identity_waits_for_the_first_answer(self):
        job = self.job("claude", [{"type": "system", "subtype": "init", "model": "claude-opus-5-5", "session_id": WORKER_SESSION}])
        self.assertIsNone(self.first_block(job))
        self.assertFalse((jobs.job_dir(job) / IDENTITY_SHOWN).exists())

    def test_codex_worker_from_the_rollout_file(self):
        codex_home = Path(os.environ["CODEX_HOME"])
        write_lines(codex_home / "sessions" / "2026" / "11" / "03" / f"rollout-2026-11-03T21-12-16-{THREAD}.jsonl", [
            {"type": "session_meta", "payload": {"id": THREAD}},
            {"type": "turn_context", "payload": {"model": "gpt-6.1-sol", "effort": "high", "summary": "none"}}])
        job = self.job("codex", [{"type": "thread.started", "thread_id": THREAD}], model="gpt-6.1-sol", effort="high")
        self.assertEqual(self.first_block(job), [
            "| I'm Haiku 5.5 (effort low), the relay agent for Codex gpt-6.1-sol (effort high). "
            "Read from my transcript and Codex's session log."])

    def test_opencode_says_it_does_not_report_its_model(self):
        job = self.job("opencode", [], model="opencode/deepseek-v4.1-flash-free")
        [line] = self.first_block(job)
        self.assertTrue(line.startswith("| I'm Haiku 5.5 (effort low), the relay agent for OpenCode."))
        self.assertIn("OpenCode didn't report which model it runs; asked for opencode/deepseek-v4.1-flash-free.", line)

    def test_relay_found_by_job_id_on_later_calls_and_unknown_elsewhere(self):
        job = self.job("antigravity", [{"event": "init", "init": {"model": "gemini-3.8-flash-high"}}])
        [line] = self.first_block(job, marker=None)
        self.assertEqual(line, "| I'm the relay agent for Antigravity gemini-3.8-flash-high. Read from the worker's "
                               "start event. My own model isn't visible from here.")
        os.environ.pop("CLAUDE_CODE_SESSION_ID")
        self.assertIsNone(identity.relay_identity([INBOX]))
