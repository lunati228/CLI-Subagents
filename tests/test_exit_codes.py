"""Process completion must agree with the worker's success event."""

import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

from helpers import TempHome
from cli_subagents import jobs
from cli_subagents.config import Account, load_config, save_config
from cli_subagents.runner import Runner
from cli_subagents.runtimes.antigravity import AntigravityParser
from cli_subagents.runtimes.claude import ClaudeParser
from cli_subagents.runtimes.codex import CodexParser


PARSERS = {"claude": ClaudeParser, "codex": CodexParser, "antigravity": AntigravityParser}
SCENARIOS = {"claude": "FAKE_CLAUDE_SCENARIO", "codex": "FAKE_CODEX_SCENARIO",
             "antigravity": "FAKE_AGY_SCENARIO"}
SUCCESS_EVENTS = {
    "claude": {"type": "result", "subtype": "success", "is_error": False, "result": "final answer"},
    "codex": {"type": "turn.completed", "usage": {"input_tokens": 10}},
    "antigravity": {"event": "result", "result": {"status": "SUCCESS", "response": "final answer"}},
}


def completed_parser(runtime):
    parser = PARSERS[runtime]()
    if runtime == "codex":
        parser.feed({"type": "item.completed", "item": {"type": "agent_message", "text": "final answer"}})
    parser.feed(SUCCESS_EVENTS[runtime])
    return parser


class ExitCodeParserTests(unittest.TestCase):
    def test_success_requires_exit_zero(self):
        for runtime in PARSERS:
            for code in (0, 1, -9, None):
                with self.subTest(runtime=runtime, code=code):
                    result = completed_parser(runtime).finish(code, "late failure" if code else "")
                    self.assertEqual(result.outcome, "success" if code == 0 else "error")
                    self.assertEqual(result.text, "final answer")
                    if code != 0:
                        self.assertIn(f"status {code}", result.message)

    def test_exit_zero_without_success_is_an_error(self):
        for runtime, parser_type in PARSERS.items():
            with self.subTest(runtime=runtime):
                self.assertEqual(parser_type().finish(0, "").outcome, "error")

    def test_late_stderr_classifies_limit_and_auth(self):
        for runtime in PARSERS:
            for text, outcome in (("usage limit reached; quota reached", "usage_limit"),
                                  ("not logged in; Authentication expired. Please log back in.", "auth")):
                with self.subTest(runtime=runtime, outcome=outcome):
                    result = completed_parser(runtime).finish(1, text)
                    self.assertEqual((result.outcome, result.text), (outcome, "final answer"))

    def test_answer_words_do_not_classify_a_process_failure(self):
        for runtime in PARSERS:
            with self.subTest(runtime=runtime):
                parser = completed_parser(runtime)
                parser.last_text = "usage limit reached; not logged in"
                self.assertEqual(parser.finish(1, "crashed").outcome, "error")

    def test_codex_late_stream_errors_classify_failure(self):
        for kind in ("error", "turn.failed"):
            for message, outcome in (("usage limit reached", "usage_limit"), ("401 Unauthorized", "auth")):
                with self.subTest(kind=kind, outcome=outcome):
                    parser = completed_parser("codex")
                    payload = {"message": message} if kind == "error" else {"error": {"message": message}}
                    parser.feed({"type": kind, **payload})
                    result = parser.finish(1, "")
                    self.assertEqual((result.outcome, result.text), (outcome, "final answer"))

    def test_codex_failed_turn_prevents_success_even_on_exit_zero(self):
        parser = completed_parser("codex")
        parser.feed({"type": "turn.failed", "error": {"message": "late failure"}})
        result = parser.finish(0, "")
        self.assertEqual((result.outcome, result.text), ("error", "final answer"))

    def test_codex_recovered_errors_stay_warnings_on_failure(self):
        parser = CodexParser()
        parser.feed({"type": "error", "message": "429 Too Many Requests; retrying"})
        parser.feed({"type": "item.completed", "item": {"type": "error", "message": "model rerouted"}})
        parser.feed(SUCCESS_EVENTS["codex"])
        warnings = parser.progress()["warnings"]
        result = parser.finish(1, "late shutdown failure")
        self.assertEqual(result.outcome, "error")
        self.assertEqual(parser.progress()["warnings"], warnings)
        self.assertEqual(len(warnings), 2)
        self.assertIn("status 1", result.message)

    def test_claude_error_result_is_not_success_even_with_success_subtype(self):
        for code in (0, 1):
            parser = ClaudeParser()
            parser.feed({"type": "assistant", "message": {"content": [{"type": "text", "text": "partial answer"}]}})
            parser.feed({"type": "result", "subtype": "success", "is_error": True,
                         "api_error_status": 401, "result": "Invalid API key"})
            result = parser.finish(code, "")
            self.assertEqual((result.outcome, result.text), ("auth", "partial answer"))

    def test_result_only_text_is_available_to_runner_stop_handling(self):
        for runtime in ("claude", "antigravity"):
            with self.subTest(runtime=runtime):
                self.assertEqual(completed_parser(runtime).last_text, "final answer")


class ExitCodeJobTests(TempHome):
    def setUp(self):
        super().setUp()
        here = Path(__file__).resolve().parent
        os.environ.update(
            CLI_SUBAGENTS_CODEX_COMMAND=json.dumps([sys.executable, str(here / "fake_codex.py")]),
            CLI_SUBAGENTS_AGY_COMMAND=json.dumps([sys.executable, str(here / "fake_agy.py")]),
            CLAUDE_CONFIG_DIR=str(self.primary), CODEX_HOME=str(self.root / "codex"),
            HOME=str(self.root), USERPROFILE=str(self.root),
        )
        for key in (*SCENARIOS.values(), "FAKE_CLAUDE_LIMITED", "FAKE_CODEX_LIMITED", "FAKE_AGY_LIMITED",
                    "FAKE_CLAUDE_DELAY", "FAKE_AGY_TOOLS"):
            os.environ.pop(key, None)
        config = load_config()
        for name, runtime in config.runtimes.items():
            if name == "opencode":  # no extra account folders for this runtime
                continue
            runtime.primary = self.root / name / "primary"
            runtime.primary.mkdir(parents=True)
            runtime.include_primary = False
            runtime.accounts = [Account("a1", self.root / name / "a1")]
            runtime.failover = "off"
        save_config(config)
        # An isolated fake login, as used by the other Antigravity tests.
        token = self.root / "antigravity" / "a1" / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
        token.parent.mkdir(parents=True)
        token.write_text("fake", encoding="utf-8")

    def run_scenario(self, runtime, scenario, *, stop=None):
        os.environ[SCENARIOS[runtime]] = scenario
        job = jobs.create_job(runtime=runtime, prompt="task", cwd=str(self.project), account="a1",
                              access="read", timeout_seconds=30, start=False)
        runner = Runner(job["job_id"])
        if stop == "timeout":
            runner.spec.timeout_seconds = 2
        handle_line = runner._handle_line
        success_seen = []

        def handle(raw, parser, account):
            handle_line(raw, parser, account)
            event = json.loads(raw)
            if (event.get("type") == "turn.completed"
                    or (event.get("type") == "result" and event.get("subtype") == "success")
                    or (event.get("event") == "result" and (event.get("result") or {}).get("status") == "SUCCESS")):
                success_seen.append(True)
                if stop == "cancelled":
                    (runner.folder / "cancel").touch()

        with patch.object(runner, "_handle_line", side_effect=handle):
            runner.run()
        status = jobs.read_status(job["job_id"])
        text = (runner.folder / "result.md").read_text(encoding="utf-8")
        if scenario.startswith("success"):
            self.assertTrue(success_seen, status)
        self.assertIn("done", text)
        self.assertTrue(status["session_id"])
        return status

    def test_normal_success_for_every_runtime(self):
        for runtime in PARSERS:
            with self.subTest(runtime=runtime):
                status = self.run_scenario(runtime, "success")
                self.assertEqual(status["outcome"], "success", status)
                self.assertEqual(status["attempts"][0]["exit_code"], 0)

    def test_success_then_exit_one_for_every_runtime(self):
        for runtime in PARSERS:
            with self.subTest(runtime=runtime):
                status = self.run_scenario(runtime, "success_exit_1")
                self.assertEqual(status["outcome"], "error", status)
                self.assertEqual(status["attempts"][0]["exit_code"], 1)
                self.assertIn("status 1", status["message"])
                self.assertIn("late CLI failure", status["message"])

    def test_timeout_after_success_for_every_runtime(self):
        for runtime in PARSERS:
            with self.subTest(runtime=runtime):
                status = self.run_scenario(runtime, "success_pause", stop="timeout")
                self.assertEqual(status["outcome"], "timeout", status)
                self.assertNotEqual(status["attempts"][0]["exit_code"], 0)

    def test_cancel_after_success_for_every_runtime(self):
        for runtime in PARSERS:
            with self.subTest(runtime=runtime):
                status = self.run_scenario(runtime, "success_pause", stop="cancelled")
                self.assertEqual(status["outcome"], "cancelled", status)
                self.assertNotEqual(status["attempts"][0]["exit_code"], 0)

    def test_late_stderr_limit_for_every_runtime(self):
        for runtime in PARSERS:
            with self.subTest(runtime=runtime):
                status = self.run_scenario(runtime, "success_quota")
                self.assertEqual(status["outcome"], "usage_limit", status)

    def test_late_stderr_auth_for_every_runtime(self):
        for runtime in PARSERS:
            with self.subTest(runtime=runtime):
                status = self.run_scenario(runtime, "success_auth")
                self.assertEqual(status["outcome"], "auth", status)

    def test_early_auth_stop_keeps_output_already_written(self):
        for scenario in ("early_auth",):
            with self.subTest(scenario=scenario):
                started = time.monotonic()
                status = self.run_scenario("antigravity", scenario)
                self.assertEqual(status["outcome"], "auth", status)
                self.assertEqual(status["session_id"], "conv-early")
                self.assertEqual(status["progress"]["last_text"], "partial done")
                # The worker keeps running (60 s) after the stop; the drain must not wait for it.
                self.assertLess(time.monotonic() - started, 30)

    def test_drain_is_bounded_when_the_pipe_stays_open(self):
        runner = Runner(jobs.create_job(runtime="antigravity", prompt="t", cwd=str(self.project), account="a1",
                                        access="read", timeout_seconds=30, start=False)["job_id"])
        parser = AntigravityParser()
        import io, queue
        lines = queue.Queue()
        lines.put(json.dumps({"event": "step_update", "step_update": {"step_index": 5, "step_type": "agent_response", "text_delta": "kept"}}).encode() + b"\n")
        with patch("cli_subagents.runner.EARLY_DRAIN_SECONDS", 0.5):
            started = time.monotonic()
            runner._drain(lines, [], parser, Account("a1", self.root), io.BytesIO())
            self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(parser.last_text, "kept")

    def test_claude_max_turns_exit_one_keeps_its_outcome(self):
        status = self.run_scenario("claude", "max_turns")
        self.assertEqual(status["outcome"], "max_turns", status)
        self.assertEqual(status["attempts"][0]["exit_code"], 1)

    def test_codex_recovered_errors_allow_success_and_keep_warnings(self):
        status = self.run_scenario("codex", "recovered_errors")
        self.assertEqual(status["outcome"], "success", status)
        self.assertEqual(len(status["progress"]["warnings"]), 2)

    def test_codex_failed_turn_after_completion(self):
        status = self.run_scenario("codex", "success_failed")
        self.assertEqual(status["outcome"], "error", status)
        self.assertIn("status 1", status["message"])


if __name__ == "__main__":
    unittest.main()
