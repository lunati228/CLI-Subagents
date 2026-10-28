import contextlib
import io
import json
import os
import re
import sys
import time
import unittest
from unittest.mock import patch

from helpers import TempHome

from cli_subagents import cli, hooks, jobs, messages
from cli_subagents.follow import (
    AntigravityDescriber,
    ClaudeDescriber,
    CodexDescriber,
    OpenCodeDescriber,
    RUNTIME_LABELS,
    describer_for,
    follow,
    read_lines,
)

FOLLOW_RE = re.compile(r"^csa follow (\S+) --from (\d+) # csa-token ([0-9a-f]{32})$", re.MULTILINE)
FRAME_RE = re.compile(r" · job ([a-z0-9-]+) · token ([0-9a-f]{32})$")


def result_body(text):
    lines = text.split("\n")
    start, end = lines.index("===== RESULT ====="), lines.index("===== END RESULT =====")
    assert all(line.startswith("| ") for line in lines[start + 1:end])
    return "\n".join(line[2:] for line in lines[start + 1:end])


def describe(describer, *events):
    lines = []
    for event in events:
        lines += describer.describe(event)
    return lines + describer.flush()


class ClaudeDescribeTests(unittest.TestCase):
    def test_lines(self):
        lines = describe(
            ClaudeDescriber(),
            {"type": "system", "subtype": "init", "model": "claude-opus-5-5", "claude_code_version": "2.1.293",
             "mcp_servers": [{"name": "a"}, {"name": "b"}], "permissionMode": "auto"},
            {"type": "assistant", "message": {"content": [
                {"type": "thinking", "thinking": "Look at the tests first.\nThen fix."},
                {"type": "text", "text": "I'll run them."},
                {"type": "tool_use", "name": "Bash", "input": {"command": "python -m unittest\n  -v"}},
                {"type": "tool_use", "name": "Read", "input": {"file_path": "C:/x/a.py"}},
                {"type": "tool_use", "name": "Grep", "input": {"pattern": "def main", "path": "src"}},
                {"type": "tool_use", "name": "WebFetch", "input": {"url": "https://example.com"}},
                {"type": "tool_use", "name": "mcp__docs__search", "input": {"query": "follow"}},
                {"type": "tool_use", "name": "Write", "input": {"file_path": "b" * 400}},
            ]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "is_error": True, "content": [{"type": "text", "text": "\nExit code 1\nmore"}]},
                {"type": "tool_result", "content": "fine"},
            ]}},
            {"type": "system", "subtype": "api_retry", "attempt": 2, "error": "overloaded"},
            {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}},
            {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}},
            {"type": "result", "subtype": "success", "result": "done"},
            {"type": "something_new"},
        )
        self.assertEqual(lines[:9], [
            "started: Claude Code 2.1.293 · claude-opus-5-5 · 2 MCP servers · auto",
            "thinking: Look at the tests first.",
            "  Then fix.",
            "says: I'll run them.",
            "tool: Bash · python -m unittest -v",
            "tool: Read · C:/x/a.py",
            "tool: Grep · def main in src",
            "tool: WebFetch · https://example.com",
            "tool: mcp__docs__search · follow",
        ])
        self.assertTrue(lines[9].startswith("tool: Write · bbb") and lines[9].endswith("..."))
        self.assertLessEqual(len(lines[9]), len("tool: Write · ") + 200)
        self.assertEqual(lines[10:], [
            "tool error: Exit code 1",
            "retrying the API call (attempt 2, overloaded)",
            "usage limit reached on this account",
        ])

    def test_tool_count_and_attempt_start(self):
        describer = ClaudeDescriber()
        describer.describe({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "X"}] * 3}})
        self.assertEqual(describer.tool_calls, 3)
        self.assertTrue(describer.starts_attempt({"type": "system", "subtype": "init"}))
        self.assertFalse(describer.starts_attempt({"type": "system", "subtype": "api_retry"}))


class CodexDescribeTests(unittest.TestCase):
    def test_lines(self):
        describer = CodexDescriber()
        lines = describe(
            describer,
            {"type": "thread.started", "thread_id": "t1"},
            {"type": "turn.started"},
            {"type": "item.started", "item": {"type": "command_execution", "command": "pytest -q"}},
            {"type": "item.completed", "item": {"type": "command_execution", "command": "pytest -q", "exit_code": 1}},
            {"type": "item.started", "item": {"type": "command_execution", "command": "ls"}},
            {"type": "item.completed", "item": {"type": "command_execution", "command": "ls", "exit_code": 0}},
            {"type": "item.completed", "item": {"type": "reasoning", "text": "**Checking** the failure"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Fixed it.\nAll green."}},
            {"type": "item.completed", "item": {"type": "file_change", "changes": [
                {"path": "a.py", "kind": "update"}, {"path": "b.py", "kind": "add"}]}},
            {"type": "item.started", "item": {"type": "mcp_tool_call", "server": "docs", "tool": "search"}},
            {"type": "item.completed", "item": {"type": "web_search", "query": "codex json events"}},
            {"type": "item.started", "item": {"type": "todo_list", "items": [
                {"text": "read", "completed": True}, {"text": "fix", "completed": False}]}},
            {"type": "item.completed", "item": {"type": "error", "message": "tool hiccup"}},
            {"type": "turn.failed", "error": {"message": "usage limit"}},
            {"type": "unknown.thing"},
        )
        self.assertEqual(lines, [
            "started: Codex thread",
            "$ pytest -q",
            "  exit 1",
            "$ ls",
            "thinking: Checking: the failure",
            "says: Fixed it.",
            "  All green.",
            "edited: a.py, b.py (new)",
            "tool: docs/search",
            "search: codex json events",
            "plan: [x] read",
            "  [ ] fix",
            "warning: tool hiccup",
            "turn failed: usage limit",
        ])
        self.assertEqual(describer.tool_calls, 3)


class AntigravityDescribeTests(unittest.TestCase):
    def test_text_is_buffered_per_step(self):
        describer = AntigravityDescriber()
        events = [
            {"event": "init", "init": {"model": "gemini-test"}},
            {"event": "step_update", "step_update": {"step_index": 0, "step_type": "user_input"}},
            {"event": "step_update", "step_update": {"step_index": 1, "step_type": "tool_call", "state": "ACTIVE", "command": "dir"}},
            {"event": "step_update", "step_update": {"step_index": 1, "step_type": "tool_call", "state": "DONE"}},
            {"event": "step_update", "step_update": {"step_index": 2, "step_type": "agent_response", "text_delta": "gemini "}},
            {"event": "step_update", "step_update": {"step_index": 2, "step_type": "agent_response", "text_delta": "done"}},
            {"event": "step_update", "step_update": {"step_index": 3, "step_type": "agent_response", "text_delta": "second"}},
        ]
        lines = []
        for offset, event in enumerate(events):
            lines += describer.describe(event, offset * 10)
        self.assertEqual(lines, ["started: Antigravity · gemini-test", "step: tool_call · dir", "says: gemini done"])
        self.assertEqual(describer.pending_offset, 60)
        self.assertEqual(describer.describe({"event": "result", "result": {}}), ["says: second"])
        self.assertIsNone(describer.pending_offset)
        self.assertEqual(describer.tool_calls, 1)


class OpenCodeDescribeTests(unittest.TestCase):
    def test_finished_parts_and_usage(self):
        describer = OpenCodeDescriber()
        lines = describe(
            describer,
            {"type": "step_start", "sessionID": "ses_1", "part": {"type": "step-start"}},
            {"type": "reasoning", "part": {"text": "**Checking** Read the tests first. Then fix them."}},
            {"type": "tool_use", "sessionID": "ses_1", "part": {
                "id": "tool_1", "tool": "shell", "state": {"status": "completed",
                    "input": {"command": "python -m unittest\n -v", "environment": {"TOKEN": "secret"}},
                    "output": "private command output"}}},
            {"type": "tool_use", "part": {"tool": "read", "state": {
                "status": "completed", "input": {"filePath": "src/main.py"}, "output": "private file body"}}},
            {"type": "step_finish", "part": {"reason": "tool-calls", "cost": 0,
                "tokens": {"input": 12, "output": 4, "reasoning": 1, "cache": {"read": 3, "write": 0}}}},
            {"type": "step_start", "sessionID": "ses_1", "part": {"type": "step-start"}},
            {"type": "text", "part": {"text": "Fixed it.\nAll tests pass."}},
            {"type": "step_finish", "part": {"reason": "stop", "cost": 0.0002,
                "tokens": {"input": 20, "output": 6}}},
        )
        self.assertEqual(lines, [
            "started: OpenCode session",
            "thinking: Checking: Read the tests first.",
            "tool: shell · python -m unittest -v",
            "tool: read · src/main.py",
            "step finished: tool-calls · 12 input tokens · 4 output tokens · 1 reasoning token · 3 cache read tokens · 0 cache write tokens · $0",
            "says: Fixed it.",
            "  All tests pass.",
            "step finished: stop · 20 input tokens · 6 output tokens · $0.0002",
        ])
        self.assertEqual(describer.tool_calls, 2)
        self.assertIsInstance(describer_for("opencode"), OpenCodeDescriber)
        self.assertEqual(RUNTIME_LABELS["opencode"], "OpenCode")

    def test_tool_errors_duplicates_and_safe_summaries(self):
        describer = OpenCodeDescriber()
        failed = {"type": "tool_use", "sessionID": "ses_1", "part": {
            "id": "tool_1", "tool": "shell", "state": {"status": "error",
                "input": {"command": "git status"}, "error": "Denied by review gate\nprivate details"}}}
        lines = describe(
            describer,
            failed,
            failed,
            {"type": "tool_use", "part": {"tool": "mcp_example", "state": {
                "status": "completed", "input": {"apiKey": "secret"}, "output": "private result"}}},
            {"type": "tool_use", "part": {"tool": "read", "state": {
                "status": "completed", "input": {"filePath": "x" * 400}}}},
            {"type": "tool_use", "part": {"tool": "shell", "state": {
                "status": "running", "input": {"command": "not finished"}}}},
        )
        self.assertEqual(lines[:3], ["tool: shell · git status", "tool error: Denied by review gate", "tool: mcp_example"])
        self.assertTrue(lines[3].endswith("..."))
        self.assertLessEqual(len(lines[3]), len("tool: read · ") + 200)
        self.assertEqual(describer.tool_calls, 3)

    def test_errors_and_attempt_boundaries(self):
        describer = OpenCodeDescriber()
        start = {"type": "step_start", "sessionID": "ses_1"}
        self.assertTrue(describer.starts_attempt(start))
        self.assertEqual(describer.describe(start), ["started: OpenCode session"])
        self.assertFalse(describer.starts_attempt(start))
        self.assertEqual(describer.describe(start), [])
        self.assertEqual(describer.describe({"type": "error", "error": {
            "type": "provider.free-limit", "message": "Free usage limit reached.\nprivate response body",
            "response": {"body": "secret"}}}), ["error: provider.free-limit · Free usage limit reached."])
        self.assertTrue(describer.starts_attempt(start))
        describer.describe(start)
        self.assertTrue(describer.starts_attempt({"type": "step_start", "sessionID": "ses_2"}))
        self.assertEqual(describer.describe({"type": "error", "error": {
            "name": "APIError", "data": {"message": "Rate limit exceeded.", "body": "secret"}}}),
            ["error: APIError · Rate limit exceeded."])

    def test_unknown_and_malformed_parts(self):
        self.assertEqual(describe(
            OpenCodeDescriber(),
            {"type": "unknown"}, {"type": "text", "part": None},
            {"type": "reasoning", "part": {"text": "  "}},
            {"type": "tool_use", "part": {"state": "bad"}},
            {"type": "step_finish", "part": {"tokens": {"input": "bad", "output": True}, "cost": "bad"}},
            {"type": "error", "error": None},
        ), ["step finished", "error: no details"])


class ReadLinesTests(unittest.TestCase):
    def test_partial_last_line_waits(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "events.jsonl"
            path.write_bytes(b'{"a": 1}\n{"b": 2}\n{"c"')
            lines, end = read_lines(path, 0)
            self.assertEqual([offset for offset, _ in lines], [0, 9])
            self.assertEqual(end, 18)
            self.assertEqual(read_lines(path, end), ([], 18))
            lines, end = read_lines(path, 18, final=True)
            self.assertEqual((lines, end), ([(18, b'{"c"')], 22))
            self.assertEqual(read_lines(Path(folder) / "missing", 5), ([], 5))


class FollowTests(TempHome):
    def csa(self, *argv, stdin=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch("sys.stdin", io.StringIO(stdin or "")):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def start(self, prompt="task", **kwargs):
        return jobs.create_job(runtime="claude", prompt=prompt, cwd=str(self.project), **kwargs)["job_id"]

    def body(self, text):
        """Event and account lines: everything but the header and the end marker."""

        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("[csa] CLI Subagents · Claude Code"), lines)
        end = next((index for index, line in enumerate(lines) if line in
                    ("===== RESULT =====", "===== QUESTION =====")), len(lines) - 1)
        return [line[2:] for line in lines[1:end] if line.startswith("| ")]

    def test_run_follow_ends_with_done_and_the_result(self):
        self.write_config(accounts=("a1",))
        code, text = self.csa("run", "--follow", "--cwd", str(self.project), "--model", "haiku", "do the task")
        self.assertEqual(code, 0, text)
        lines = text.splitlines()
        self.assertRegex(lines[0], r"^\[csa\] CLI Subagents · Claude Code · model haiku · access edit · (account a1 · )?job \S+ · token [0-9a-f]{32}$")
        self.assertIn("| tool: Read", lines)
        self.assertIn("| says: done on a1", lines)
        self.assertTrue(any(line.startswith("| started: Claude Code 9.9.9 · claude-haiku-5-5 · 1 MCP server") for line in lines))
        self.assertRegex(lines[-1], r"^\[csa\] DONE · success · \d+s · 1 tool call · account a1 · job \S+ · token [0-9a-f]{32}$")
        self.assertEqual(lines[-5:-2], ["===== RESULT =====", "| done on a1", "===== END RESULT ====="])
        self.assertEqual(lines[-2], "[csa] title: Claude Code: Read")
        job_id, token = FRAME_RE.search(lines[0]).groups()
        self.assertEqual(FRAME_RE.search(lines[-1]).groups(), (job_id, token))
        # The follower delivered the result: nothing is left for the wake-up or SessionStart hooks.
        self.assertFalse(jobs.is_pending(job_id))
        self.assertFalse(jobs.claim_delivery(job_id))
        self.assertEqual(hooks.pending_jobs(str(self.project)), [])
        # A plain follow of the finished job prints the whole history again.
        code, again = self.csa("follow", job_id)
        # The replay names the account in its header instead of in a line of its own.
        self.assertIn(" · account a1 · ", again.splitlines()[0])
        live = [line for line in self.body(text) if line != "running on account a1"]
        self.assertEqual((code, self.body(again)), (0, live))
        self.assertEqual(result_body(again), "done on a1")

    def test_message_follow_continues_finished_conversation_and_follows_to_done(self):
        self.write_config(accounts=("a1",))
        code, text = self.csa("run", "--follow", "--cwd", str(self.project), "first task")
        self.assertEqual(code, 0, text)
        previous, previous_token = FRAME_RE.search(text.splitlines()[0]).groups()
        session = jobs.read_status(previous)["session_id"]

        for prompt in ("one more thing\nkeep the context", "and another follow-up"):
            # Even an offset beyond the new log must be discarded on continuation.
            code, text = self.csa("message", previous, "--follow", "--from", "9999999", "-", stdin=prompt)
            self.assertEqual(code, 0, text)
            lines = text.splitlines()
            self.assertTrue(lines[0].startswith("[csa] CLI Subagents"), text)
            current, token = FRAME_RE.search(lines[0]).groups()
            self.assertNotEqual(current, previous)
            self.assertNotEqual(token, previous_token)
            self.assertEqual(FRAME_RE.search(lines[-1]).groups(), (current, token))
            self.assertTrue(lines[-1].startswith("[csa] DONE"), text)
            self.assertTrue(lines[-2].startswith("[csa] title: Claude Code:"), text)
            self.assertIn("| tool: Read", lines)
            self.assertIn("| says: done on a1", lines)
            self.assertEqual(result_body(text), "done on a1")
            self.assertEqual(jobs.read_spec(current).prompt, prompt)
            self.assertEqual(jobs.read_spec(current).resume_session, session)
            last = self.records()[-1]
            self.assertEqual(last["argv"][last["argv"].index("--resume") + 1], session)
            self.assertIn(prompt, last["stdin"])
            self.assertFalse(jobs.is_pending(current))
            previous, previous_token = current, token

    def test_message_follow_returns_continuation_failure(self):
        self.write_config(accounts=("a1",))
        job_id = self.start()
        self.assertEqual(jobs.wait_for(job_id, 30)["outcome"], "success")
        os.environ["FAKE_CLAUDE_SCENARIO"] = "auth"
        code, text = self.csa("message", job_id, "--follow", "-", stdin="follow-up")
        self.assertEqual(code, 1, text)
        lines = text.splitlines()
        current, token = FRAME_RE.search(lines[0]).groups()
        self.assertNotEqual(current, job_id)
        self.assertEqual(FRAME_RE.search(lines[-1]).groups(), (current, token))
        self.assertTrue(lines[-1].startswith("[csa] FAILED"), text)
        self.assertIn("Invalid API key", lines[-1])
        self.assertEqual(result_body(text), "")

    def test_message_follow_running_job_keeps_offset_and_queues_follow_up(self):
        self.write_config(accounts=("a1",))
        job_id = self.start(start=False)
        events = jobs.job_dir(job_id) / "events.jsonl"
        event = {"type": "assistant", "message": {"content": [{"type": "text", "text": "already shown"}]}}
        events.write_bytes((json.dumps(event) + "\n").encode())
        jobs.update_status(job_id, state="running")
        _, first = self.csa("follow", job_id, "--max-wait", "0")
        offset = FOLLOW_RE.search(first).group(2)
        self.assertIn("| says: already shown", first)

        code, text = self.csa("message", job_id, "--follow", "--from", offset, "--max-wait", "0",
                              "-", stdin="also update the docs")
        self.assertEqual(code, 0, text)
        self.assertEqual(FRAME_RE.search(text.splitlines()[0]).group(1), job_id)
        self.assertTrue(text.splitlines()[-1].startswith("[csa] STILL RUNNING"), text)
        self.assertEqual(FOLLOW_RE.search(text).group(2), offset)
        self.assertNotIn("says: already shown", self.body(text))
        self.assertEqual([item["text"] for item in messages.take_for_worker(job_id)], ["also update the docs"])
        self.assertEqual(len(list((self.home / "jobs").iterdir())), 1)
        jobs.update_status(job_id, state="done", outcome="success")

    def test_message_rejects_invalid_offset_before_delivery(self):
        self.write_config(accounts=("a1",))
        job_id = self.start(start=False)
        for options, error in ((("--from", "5"), "--from requires --follow"),
                               (("--follow", "--from", "-1"), "--from must be 0 or more")):
            with self.subTest(options=options), contextlib.redirect_stderr(io.StringIO()) as err:
                code, _ = self.csa("message", job_id, *options, "-", stdin="do not deliver")
                self.assertEqual(code, 2)
                self.assertIn(error, err.getvalue())
                self.assertEqual(messages.take_for_worker(job_id), [])
        jobs.update_status(job_id, state="done", outcome="success")

    def test_failed_job(self):
        self.write_config(accounts=("a1",))
        os.environ["FAKE_CLAUDE_SCENARIO"] = "auth"
        code, text = self.csa("run", "--follow", "--cwd", str(self.project), "x")
        self.assertEqual(code, 1, text)
        self.assertTrue(text.splitlines()[-1].startswith("[csa] FAILED · auth · Invalid API key · Please run /login · job "))

    def test_failover_prints_the_account_switch(self):
        self.write_config(accounts=("a1", "a2"))
        os.environ["FAKE_CLAUDE_LIMITED"] = "a1"
        code, text = self.csa("run", "--follow", "--cwd", str(self.project), "x")
        self.assertEqual(code, 0, text)
        body = self.body(text)
        limit = body.index("usage limit reached on this account")
        switch = body.index("switched to account a2 after usage_limit on a1")
        self.assertLess(limit, switch)
        self.assertLess(switch, body.index("says: done on a2"))
        self.assertEqual(sum(line.startswith("started: Claude Code") for line in body), 2)
        self.assertIn("· account a2", text.splitlines()[-1])

    def test_still_running_then_continue_without_repeats(self):
        self.write_config(accounts=("a1",))
        os.environ["FAKE_CLAUDE_DELAY"] = "2"
        job_id = self.start()
        seen, calls, offset = [], 0, None
        while True:
            argv = ["follow", job_id, "--max-wait", "0.3"] + (["--from", str(offset)] if offset is not None else [])
            code, text = self.csa(*argv)
            calls += 1
            self.assertEqual(code, 0, text)
            seen += self.body(text)
            if "[csa] DONE" in text:
                break
            self.assertIn("[csa] STILL RUNNING after", text)
            match = FOLLOW_RE.search(text)
            self.assertEqual(match.group(1), job_id)
            self.assertEqual(text.splitlines()[-3], match.group(0))
            self.assertEqual(FRAME_RE.search(text.splitlines()[-1]).groups(), (job_id, match.group(3)))
            offset = int(match.group(2))
            self.assertLess(calls, 100)
        self.assertGreater(calls, 1)
        _, full = self.csa("follow", job_id)
        # A live follow may announce the first account before a replay's header does.
        self.assertEqual([line for line in seen if line != "running on account a1"], self.body(full))
        self.assertEqual(sum(line.startswith("started:") for line in seen), 1)

    def test_question_stops_the_follow_and_message_answers_from_stdin(self):
        self.write_config(accounts=("a1",))
        os.environ["FAKE_CLAUDE_DELAY"] = "3"
        job_id = self.start()
        question = messages.post_from_worker(job_id, "Tabs or spaces?", kind="question")
        messages.post_from_worker(job_id, "halfway there")
        code, text = self.csa("follow", job_id)
        self.assertEqual(code, 0, text)
        lines = text.splitlines()
        marker = lines.index("===== QUESTION =====")
        self.assertEqual(lines[marker + 1 : marker + 4], ["| Tabs or spaces?", "===== END QUESTION =====", "Answer with:"])
        token = FRAME_RE.search(lines[0]).group(2)
        self.assertIn(f"csa message {job_id} --reply-to {question['id']} - # csa-token {token}", lines)
        self.assertTrue(lines[-1].startswith(f"[csa] QUESTION from the worker (id {question['id']}) · job {job_id}"))
        self.assertEqual(lines[-4], "Then keep watching with:")
        offset = int(FOLLOW_RE.search(text).group(2))
        # The note after the question waits for the next follow.
        self.assertNotIn("| note for the main session: halfway there", lines)

        code, reply = self.csa("message", job_id, "--reply-to", question["id"], "-", stdin="Spaces, four of them.\n")
        self.assertEqual(code, 0, reply)
        self.assertEqual(messages.answers(job_id)[question["id"]], "Spaces, four of them.")
        messages.take_for_worker(job_id)  # the worker's ask_parent picks the answer up

        code, text = self.csa("follow", job_id, "--from", str(offset))
        self.assertEqual(code, 0, text)
        self.assertNotIn("QUESTION", text)
        self.assertIn("| note for the main session: halfway there", text.splitlines())
        self.assertIn("[csa] DONE · success", text)
        # Both worker messages reached the main session through the follower.
        self.assertFalse(messages.has_new_for_host(job_id))

    def test_question_seen_once_does_not_stop_again(self):
        self.write_config(accounts=("a1",))
        os.environ["FAKE_CLAUDE_DELAY"] = "2"
        job_id = self.start()
        messages.post_from_worker(job_id, "Which branch?", kind="question")
        _, text = self.csa("follow", job_id)
        self.assertIn("[csa] QUESTION", text)
        offset = FOLLOW_RE.search(text).group(2)
        # Not answered, but already handed on: keep watching instead of stopping again.
        code, text = self.csa("follow", job_id, "--from", offset)
        self.assertEqual(code, 0, text)
        self.assertNotIn("QUESTION", text)
        self.assertIn("[csa] DONE · success", text)
        # The full history shows the question as a plain line.
        _, full = self.csa("follow", job_id)
        self.assertIn("| asked the main session: Which branch?", full.splitlines())

    def test_partial_lines_wait_and_offsets_resume(self):
        self.write_config(accounts=("a1",))
        job_id = jobs.create_job(runtime="claude", prompt="x", cwd=str(self.project), start=False)["job_id"]
        events = jobs.job_dir(job_id) / "events.jsonl"
        init = json.dumps({"type": "system", "subtype": "init", "model": "m"}) + "\n"
        said = json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}}) + "\n"
        events.write_bytes((init + "not json\n" + said[:10]).encode())
        out = io.StringIO()
        self.assertEqual(follow(job_id, None, 0, out), 0)
        lines = out.getvalue().splitlines()
        self.assertIn("| started: Claude Code · m", lines)
        self.assertNotIn("| says: hello", lines)
        offset = len((init + "not json\n").encode())
        self.assertEqual(FOLLOW_RE.search(out.getvalue()).groups()[:2], (job_id, str(offset)))
        self.assertTrue(lines[-1].startswith("[csa] STILL RUNNING"))

        with open(events, "ab") as handle:
            handle.write(said[10:].encode())
        out = io.StringIO()
        follow(job_id, offset, 0, out)
        lines = out.getvalue().splitlines()
        self.assertNotIn("| started: Claude Code · m", lines)
        self.assertIn("| says: hello", lines)
        offset = int(FOLLOW_RE.search(out.getvalue()).group(2))
        self.assertEqual(offset, events.stat().st_size)

        jobs.write_text(jobs.job_dir(job_id) / "result.md", "hello")
        jobs.mark_pending(job_id)
        jobs.update_status(job_id, state="done", outcome="success", account="a1", attempts=[{"account": "a1"}],
                           started_at=time.time() - 75, finished_at=time.time())
        out = io.StringIO()
        self.assertEqual(follow(job_id, offset, 0, out), 0)
        lines = out.getvalue().splitlines()
        self.assertNotIn("| says: hello", lines)
        self.assertTrue(lines[-1].startswith("[csa] DONE · success · 1m 15s · 0 tool calls · account a1 · job "))
        self.assertFalse(jobs.is_pending(job_id))

    def test_antigravity_run(self):
        fake = os.path.join(os.path.dirname(__file__), "fake_agy.py")
        os.environ["CLI_SUBAGENTS_AGY_COMMAND"] = json.dumps([sys.executable, fake])
        code, text = self.csa("run", "--follow", "--runtime", "antigravity", "--cwd", str(self.project), "--model", "gemini-test", "x")
        self.assertEqual(code, 0, text)
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("[csa] CLI Subagents · Antigravity · model gemini-test · access edit"), lines)
        self.assertEqual(
            [line for line in lines[1:lines.index("===== RESULT =====")] if not line.startswith("| running")],
            ["| started: Antigravity · gemini-test", "| step: tool_call", "| says: gemini done"],
        )
        self.assertRegex(lines[-1], r"^\[csa\] DONE · success · \d+s · 1 tool call · account primary · job \S+ · token [0-9a-f]{32}$")
        self.assertEqual(result_body(text), "gemini done")

    def test_explicit_offset_zero_does_not_replay_messages(self):
        self.write_config(accounts=("a1",))
        os.environ["FAKE_CLAUDE_DELAY"] = "2"
        job_id = self.start()
        messages.post_from_worker(job_id, "first note")
        _, text = self.csa("follow", job_id, "--max-wait", "0")
        self.assertIn("| note for the main session: first note", text.splitlines())
        _, text = self.csa("follow", job_id, "--from", "0")
        self.assertNotIn("first note", text)
        self.assertIn("[csa] DONE", text)

    def test_antigravity_text_split_across_follows(self):
        self.write_config(accounts=("a1",))
        job_id = jobs.create_job(runtime="claude", prompt="x", cwd=str(self.project), start=False)["job_id"]
        spec_path = jobs.job_dir(job_id) / "spec.json"
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        spec["runtime"] = "antigravity"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        events = jobs.job_dir(job_id) / "events.jsonl"

        def step(index, kind, delta=""):
            return json.dumps({"event": "step_update", "step_update": {"step_index": index, "step_type": kind, "text_delta": delta}}) + "\n"

        head = json.dumps({"event": "init", "init": {"model": "g"}}) + "\n" + step(1, "tool_call")
        events.write_bytes((head + step(2, "agent_response", "Hel")).encode())
        out = io.StringIO()
        follow(job_id, None, 0, out)
        self.assertNotIn("says:", out.getvalue())
        offset = int(FOLLOW_RE.search(out.getvalue()).group(2))
        self.assertEqual(offset, len(head.encode()))

        with open(events, "ab") as handle:
            handle.write((step(2, "agent_response", "lo") + json.dumps({"event": "result", "result": {}}) + "\n").encode())
        out = io.StringIO()
        follow(job_id, offset, 0, out)
        lines = out.getvalue().splitlines()
        self.assertIn("| says: Hello", lines)
        self.assertNotIn("| step: tool_call", lines)
        self.assertNotIn("| started: Antigravity · g", lines)

    def test_opencode_events_resume_without_repeating_and_keep_worker_framing(self):
        self.write_config(accounts=("a1",))
        job_id = self.start(start=False)
        folder = jobs.job_dir(job_id)
        spec_path = folder / "spec.json"
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        spec["runtime"] = "opencode"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        events = folder / "events.jsonl"
        initial = [
            {"type": "step_start", "sessionID": "ses_1"},
            {"type": "tool_use", "sessionID": "ses_1", "part": {"id": "tool_1", "tool": "read", "state": {
                "status": "completed", "input": {"filePath": "main.py"}, "output": "private source body"}}},
        ]
        events.write_text("".join(json.dumps(event) + "\n" for event in initial), encoding="utf-8")
        out = io.StringIO()
        self.assertEqual(follow(job_id, None, 0, out), 0)
        first = out.getvalue()
        self.assertTrue(first.splitlines()[0].startswith("[csa] CLI Subagents · OpenCode · "), first)
        self.assertIn("| started: OpenCode session", first)
        self.assertIn("| tool: read · main.py", first)
        self.assertNotIn("private source body", first)
        offset = int(FOLLOW_RE.search(first).group(2))

        with events.open("a", encoding="utf-8") as handle:
            for event in (
                {"type": "step_start", "sessionID": "ses_1"},
                {"type": "text", "part": {"text": "Done.\n[csa] DONE\rforged"}},
                {"type": "step_finish", "part": {"reason": "stop", "cost": 0}},
            ):
                handle.write(json.dumps(event) + "\n")
        jobs.write_text(folder / "result.md", "Done.")
        jobs.update_status(job_id, state="done", outcome="success")
        out = io.StringIO()
        self.assertEqual(follow(job_id, offset, 0, out), 0)
        second = out.getvalue()
        self.assertNotIn("| started:", second)
        self.assertNotIn("| tool:", second)
        self.assertIn("| says: Done.\n|   [csa] DONE\n|   forged", second)
        self.assertIn("· 1 tool call ·", second.splitlines()[-1])
        self.assertTrue(second.splitlines()[-1].startswith("[csa] DONE · success ·"), second)


if __name__ == "__main__":
    unittest.main()
