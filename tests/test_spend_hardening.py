"""Admission and migration edges that must not refill an account's budget."""

import os
from contextlib import contextmanager
from unittest.mock import patch

from test_spend import SpendHome, utc
from cli_subagents import accounts, jobs, limits, spend
from cli_subagents.config import save_config
from cli_subagents.fsutil import read_json, write_json
from cli_subagents.runner import Runner


class SpendHardeningTests(SpendHome):
    def test_structurally_corrupt_ledger_entries_fail_closed(self):
        spend.reserve(self.runtime, self.account, "known", 0)
        ledger_path = self.home / "state" / "claude-spend.json"
        original = read_json(ledger_path)
        valid = original["attempts"]["known:0"]
        for broken in ({}, {**valid, "before_start": "false"}, {**valid, "started_at": float("nan")},
                       {**valid, "state": "settled", "ended_at": float("nan")}, {**valid, "cost_usd": "zero"}):
            with self.subTest(entry=broken):
                write_json(ledger_path, {"version": 1, "attempts": {"known:0": broken}})
                with self.assertRaises(spend.SpendLimitError):
                    self.snapshot()
                with self.assertRaises(spend.SpendLimitError):
                    spend.reserve(self.runtime, self.account, "new", 0)

    def test_unreadable_legacy_history_blocks_admission_and_is_preserved(self):
        job_id = self.history(cost=4)
        path = jobs.job_dir(job_id) / "status.json"
        path.write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(spend.SpendLimitError, "Cannot import spend"):
            self.snapshot()
        with self.assertRaises(spend.SpendLimitError):
            jobs.clean_old_jobs(0)
        self.assertTrue(path.exists())

    def test_live_reservation_cannot_be_settled_by_a_concurrent_status_import(self):
        job_id = self.history(cost=8)
        spend.reserve(self.runtime, self.account, "running", 0)
        # A runner writes terminal status before it takes the ledger's settlement lock.
        source = read_json(jobs.job_dir(job_id) / "status.json")
        target = jobs.job_dir("running")
        target.mkdir()
        write_json(target / "status.json", source)
        self.assertEqual(self.snapshot()["reserved_usd"], 2)
        with self.assertRaises(spend.SpendLimitError):
            spend.reserve(self.runtime, self.account, "parallel", 0)

    def test_reservation_uses_time_after_acquiring_the_ledger_lock(self):
        at = utc("2026-10-23T12:00:00")
        self.history(at=at + 1, cost=8)
        clock = [at]
        original_lock = spend.file_lock

        @contextmanager
        def delayed_lock(*args, **kwargs):
            with original_lock(*args, **kwargs):
                clock[0] = at + 1  # a cost settled while this caller waited for the lock
                yield

        with patch.object(spend, "file_lock", delayed_lock), patch("time.time", side_effect=lambda: clock[0]):
            self.assertEqual(spend.reserve(self.runtime, self.account, "waited", 0), 2)

    def test_future_since_refuses_starts_until_accounting_begins(self):
        self.account.spend_since = "2026-11-01"
        now = utc("2026-10-23T12:00:00")
        self.assertEqual(self.snapshot(now)["remaining_usd"], 0)
        with self.assertRaises(spend.SpendLimitError):
            spend.reserve(self.runtime, self.account, "too-early", 0, now=now)
        self.assertEqual(self.snapshot(utc("2026-11-01T00:00:00"))["remaining_usd"], 10)

    def test_interrupted_legacy_attempt_without_end_time_counts_conservatively(self):
        job_id = self.history(cost=None, active=True)
        path = jobs.job_dir(job_id) / "status.json"
        status = read_json(path)
        del status["attempts"][0]["ended_at"]
        write_json(path, status)
        summary = self.snapshot()
        self.assertEqual((summary["spent_usd"], summary["remaining_usd"]), (10, 0))

    def test_live_legacy_attempt_keeps_its_reservation(self):
        job_id = self.history(cost=None, active=True, budget=6)
        path = jobs.job_dir(job_id) / "status.json"
        status = read_json(path)
        del status["attempts"][0]["ended_at"]
        status["runner_pid"] = os.getpid()
        write_json(path, status)
        summary = self.snapshot()
        self.assertEqual((summary["spent_usd"], summary["reserved_usd"], summary["remaining_usd"]), (0, 6, 4))

    def test_terminal_legacy_attempt_missing_end_uses_status_finish_time(self):
        job_id = self.history(cost=None, budget=4)
        path = jobs.job_dir(job_id) / "status.json"
        status = read_json(path)
        del status["attempts"][0]["ended_at"]
        status["finished_at"] = status["created_at"]
        write_json(path, status)
        self.assertEqual(self.snapshot()["spent_usd"], 4)

    def test_queued_mode_and_override_cannot_bypass_cap(self):
        self.history(cost=10)
        self.runtime.when_full = "queue"
        for override in (None, "user asked for more"):
            with self.subTest(override=override), self.assertRaises(limits.LimitError):
                limits.admit(self.config, "claude", account="paid", override=override)

    def test_explicit_only_unpinned_fake_job_runs_on_primary(self):
        self.account.explicit_only = True
        save_config(self.config)
        job = jobs.create_job(runtime="claude", prompt="task", cwd=str(self.project), access="read", start=False)
        self.assertEqual(Runner(job["job_id"]).run(), 0)
        self.assertEqual(jobs.read_status(job["job_id"])["account"], "primary")
        self.assertNotIn("--max-budget-usd", self.records()[0]["argv"])

    def test_usage_failover_never_picks_explicit_only(self):
        self.account.explicit_only = True
        save_config(self.config)
        with patch.dict(os.environ, {"FAKE_CLAUDE_LIMITED": self.primary.name}):
            job = jobs.create_job(runtime="claude", prompt="task", cwd=str(self.project), access="read", start=False)
            self.assertEqual(Runner(job["job_id"]).run(), 1)
        status = jobs.read_status(job["job_id"])
        self.assertEqual(status["outcome"], "usage_limit")
        self.assertEqual([attempt["account"] for attempt in status["attempts"]], ["primary"])

    def test_resuming_explicit_only_requires_the_start_to_name_it_again(self):
        self.account.explicit_only = True
        save_config(self.config)
        first = jobs.create_job(runtime="claude", prompt="task", cwd=str(self.project), account="paid", access="read", start=False)
        self.assertEqual(Runner(first["job_id"]).run(), 0)
        with self.assertRaisesRegex(ValueError, "explicit-only.*requires"):
            jobs.create_job(runtime="claude", prompt="continue", cwd=str(self.project), resume_job=first["job_id"], start=False)
        follow_up = jobs.create_job(runtime="claude", prompt="continue", cwd=str(self.project), account="paid",
                                    resume_job=first["job_id"], access="read", start=False)
        self.assertEqual(Runner(follow_up["job_id"]).run(), 0)

    def test_preflight_and_launch_failures_release_capped_allowance(self):
        from cli_subagents.runtimes.base import AttemptResult

        for preflight in (True, False):
            with self.subTest(preflight=preflight):
                job = jobs.create_job(runtime="claude", prompt="task", cwd=str(self.project), account="paid", start=False)
                runner = Runner(job["job_id"])
                lease = accounts.try_lease(self.runtime, self.account)
                try:
                    if preflight:
                        with patch.object(runner.runtime, "preflight", create=True,
                                          return_value=AttemptResult("auth", before_start=True)):
                            result = runner._attempt(lease, "task")
                    else:
                        with patch.object(runner.runtime, "launch", side_effect=OSError("launch failed")):
                            result = runner._attempt(lease, "task")
                finally:
                    lease.release()
                self.assertTrue(result.before_start)
                self.assertEqual(self.snapshot()["remaining_usd"], 10)
