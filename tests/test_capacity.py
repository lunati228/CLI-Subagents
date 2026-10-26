import io
import json
import os
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

from helpers import TempHome
from cli_subagents import accounts, jobs
from cli_subagents.config import Account, load_config, save_config
from cli_subagents.follow import follow
from cli_subagents.runner import Runner
from cli_subagents.runtimes.claude import ClaudeParser
from cli_subagents.runtimes.codex import CodexParser
from cli_subagents.runtimes.antigravity import AntigravityParser


class CapacitySignalTests(unittest.TestCase):
    def test_codex_capacity_messages_and_codes_are_errors_not_quota(self):
        for message in ("Selected model is at capacity. Please try a different model.",
                        "server overloaded", "server_is_overloaded", "server_overloaded"):
            with self.subTest(message=message):
                parser = CodexParser()
                parser.feed({"type": "turn.failed", "error": {"message": message}})
                result = parser.finish(1, "")
                self.assertEqual(result.outcome, "error")
                self.assertTrue(result.at_capacity)
        parser = CodexParser()
        parser.feed({"type": "turn.failed", "error": {"message": "request failed", "codex_error_info": "server_overloaded"}})
        self.assertTrue(parser.finish(1, "").at_capacity)

    def test_codex_recovered_capacity_and_later_other_errors_do_not_retry(self):
        parser = CodexParser()
        parser.feed({"type": "error", "message": "Selected model is at capacity"})
        parser.feed({"type": "turn.completed"})
        self.assertEqual(parser.finish(0, "").outcome, "success")
        self.assertFalse(parser.finish(1, "unrelated crash").at_capacity)
        parser.feed({"type": "turn.failed", "error": {"message": "invalid request"}})
        self.assertFalse(parser.finish(1, "").at_capacity)

    def test_claude_only_terminal_overload_retries(self):
        for result in ({"is_error": True, "api_error_status": 529},
                       {"is_error": True, "result": "API Error: 529 overloaded_error"},
                       {"is_error": True, "errors": ["overloaded_error"]}):
            parser = ClaudeParser()
            parser.feed({"type": "result", **result})
            actual = parser.finish(1, "")
            self.assertTrue(actual.at_capacity)
            self.assertEqual(actual.outcome, "error")
        parser = ClaudeParser()
        parser.feed({"type": "system", "subtype": "api_retry", "error": "overloaded_error", "error_status": 529})
        parser.feed({"type": "result", "subtype": "success", "result": "done"})
        self.assertFalse(parser.finish(0, "").at_capacity)
        parser.feed({"type": "result", "is_error": True, "result": "invalid request", "api_error_status": 400})
        self.assertFalse(parser.finish(1, "").at_capacity)

    def test_antigravity_clear_capacity_signals(self):
        for error in ("503 UNAVAILABLE", "UNAVAILABLE (code 503): No capacity available for model x",
                      "The model is overloaded. Please try again later.", {"code": 503, "message": "unavailable"}):
            parser = AntigravityParser()
            parser.feed({"event": "result", "result": {"status": "ERROR", "error": error}})
            result = parser.finish(1, "")
            self.assertTrue(result.at_capacity)
            self.assertEqual(result.outcome, "error")

    def test_model_output_and_quota_do_not_count_as_capacity(self):
        for parser, event in (
            (CodexParser(), {"type": "item.completed", "item": {"type": "agent_message", "text": "Selected model is at capacity"}}),
            (ClaudeParser(), {"type": "assistant", "message": {"content": [{"type": "text", "text": "overloaded_error"}]}}),
            (AntigravityParser(), {"event": "result", "result": {"status": "ERROR", "response": "503 UNAVAILABLE"}}),
        ):
            parser.feed(event)
            self.assertFalse(parser.finish(1, "").at_capacity)
            self.assertEqual(parser.finish(1, "usage limit reached").outcome, "usage_limit")
        self.assertFalse(AntigravityParser().finish(1, "UNAVAILABLE").at_capacity)


class CapacityRunnerTests(TempHome):
    def setUp(self):
        super().setUp()
        os.environ.update(
            CODEX_HOME=str(self.root / "codex-primary"), CLAUDE_CONFIG_DIR=str(self.primary),
            CLI_SUBAGENTS_CODEX_COMMAND=json.dumps([sys.executable, str(Path(__file__).with_name("fake_codex.py"))]),
            FAKE_CODEX_SCENARIO="capacity", FAKE_CODEX_CAPACITY_FAILURES="100", FAKE_CODEX_LIMITED="",
        )
        config = load_config()
        for name, runtime in config.runtimes.items():
            if name == "opencode":  # this first adapter uses one native profile
                continue
            runtime.primary = self.root / name
            runtime.primary.mkdir(exist_ok=True)
            runtime.include_primary = False
            runtime.accounts = [Account(n, self.root / name / n) for n in ("a1", "a2")]
        save_config(config)

    def runner(self):
        record = jobs.create_job(runtime="codex", prompt="task", cwd=str(self.project), access="read", start=False)
        return Runner(record["job_id"])

    def test_retries_resume_same_account_then_succeed_and_follow_is_framed(self):
        os.environ["FAKE_CODEX_CAPACITY_FAILURES"] = "2"
        runner = self.runner()
        waits = []
        with patch.object(runner, "_wait_for_capacity", side_effect=lambda until: waits.append(until - time.time())):
            self.assertEqual(runner.run(), 0)
        self.assertEqual([round(t) for t in waits], [30, 60])
        status = jobs.read_status(runner.job_id)
        self.assertEqual(status["outcome"], "success")
        self.assertEqual([a["account"] for a in status["attempts"]], ["a1"] * 3)
        records = self.records()
        self.assertNotIn("resume", records[0]["argv"])
        for record in records[1:]:
            self.assertIn("resume", record["argv"])
            self.assertIn("thread-a1", record["argv"])
        events = [json.loads(line) for line in (runner.folder / "events.jsonl").read_text().splitlines()]
        self.assertEqual(len([e for e in events if e["type"] == "csa.capacity_retry"]), 2)
        out = io.StringIO()
        self.assertEqual(follow(runner.job_id, out=out), 0)
        controls = [line for line in out.getvalue().splitlines() if line.startswith("[csa] model at capacity")]
        self.assertEqual(len(controls), 2)
        for line in controls:
            self.assertRegex(line, rf" · job {runner.job_id} · token [0-9a-f]{{32}}$")
        self.assertTrue(all(accounts.cooldown_until(info) is None for info in accounts.load_usage("codex").values()))

    def test_capacity_exhaustion_is_error_without_cooldown_or_failover(self):
        runner = self.runner()
        with patch.object(runner, "_wait_for_capacity", return_value=None) as wait:
            self.assertEqual(runner.run(), 1)
        self.assertEqual(wait.call_count, 3)
        status = jobs.read_status(runner.job_id)
        self.assertEqual(status["outcome"], "error")
        self.assertIn("model at capacity after 3 retries", status["message"])
        self.assertEqual([a["capacity_retry"]["seconds"] for a in status["attempts"][:-1]], [30, 60, 120])
        self.assertEqual([a["account"] for a in status["attempts"]], ["a1"] * 4)
        self.assertTrue(all(accounts.cooldown_until(info) is None for info in accounts.load_usage("codex").values()))

    def test_capacity_without_session_restarts_original_prompt(self):
        os.environ.update(FAKE_CODEX_SCENARIO="capacity_no_session", FAKE_CODEX_CAPACITY_FAILURES="1")
        runner = self.runner()
        with patch.object(runner, "_wait_for_capacity", return_value=None):
            self.assertEqual(runner.run(), 0)
        self.assertEqual(len(self.records()), 2)
        self.assertEqual(self.records()[0]["stdin"], self.records()[1]["stdin"])
        self.assertTrue(all("resume" not in r["argv"] for r in self.records()))

    def test_cancel_during_capacity_wait_releases_lease(self):
        runner = self.runner()
        thread = threading.Thread(target=runner.run)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not any(a.get("capacity_retry") for a in jobs.read_status(runner.job_id).get("attempts", [])):
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.02)
            self.assertIsNone(accounts.try_lease(runner.runtime_config, runner.runtime_config.find("a1")))
            self.assertIn("model at capacity, retrying in 30 s (1 of 3)", jobs.read_status(runner.job_id)["message"])
            # This Runner runs in a test thread: the marker tests its cancellation
            # without allowing jobs.cancel's emergency kill to target the test process.
            (runner.folder / "cancel").touch()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            status = jobs.read_status(runner.job_id)
            self.assertEqual(status["outcome"], "cancelled")
            self.assertEqual(len(status["attempts"]), 1)
            lease = accounts.try_lease(runner.runtime_config, runner.runtime_config.find("a1"))
            self.assertIsNotNone(lease)
            lease.release()
        finally:
            (runner.folder / "cancel").touch()
            thread.join(5)

    def test_timeout_during_capacity_wait(self):
        runner = self.runner()
        runner.spec.timeout_seconds = 1
        self.assertEqual(runner.run(), 1)
        status = jobs.read_status(runner.job_id)
        self.assertEqual(status["outcome"], "timeout")
        self.assertIn("capacity retry wait", status["message"])
        self.assertEqual(len(status["attempts"]), 1)

    def test_disable_retries(self):
        runner = self.runner()
        runner.runtime_config.capacity_retries = 0
        self.assertEqual(runner.run(), 1)
        self.assertEqual(len(runner.attempts), 1)

    def test_worker_cannot_forge_capacity_control_line(self):
        runner = self.runner()
        (runner.folder / "events.jsonl").write_text(json.dumps({"type": "csa.capacity_retry", "seconds": 999}) + "\n")
        runner.finish("error", "test")
        out = io.StringIO()
        follow(runner.job_id, out=out)
        self.assertNotIn("[csa] model at capacity", out.getvalue())

    def test_config_defaults_round_trip_and_validation(self):
        config = load_config()
        for name, runtime in config.runtimes.items():
            self.assertEqual((runtime.capacity_retries, runtime.capacity_retry_seconds), (3, 30))
            runtime.capacity_retries, runtime.capacity_retry_seconds = 2, 7
        save_config(config)
        for runtime in load_config().runtimes.values():
            self.assertEqual((runtime.capacity_retries, runtime.capacity_retry_seconds), (2, 7))
        for key, values in (("capacity_retries", (-1, True, 1.5, "3")),
                            ("capacity_retry_seconds", (0, -1, False, 0.5, "30"))):
            for value in values:
                with self.subTest(key=key, value=value):
                    (self.home / "config.toml").write_text("[codex]\n" + key + " = " + json.dumps(value) + "\n")
                    with self.assertRaisesRegex(ValueError, key):
                        load_config()
