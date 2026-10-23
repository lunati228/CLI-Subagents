"""Spend caps use isolated state, fake workers and UTC calendar periods."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import os
import threading
import time
from unittest.mock import patch

from helpers import TempHome
from cli_subagents import accounts, jobs, spend
from cli_subagents.config import Account, SpendCap, load_config, save_config
from cli_subagents.fsutil import write_json
from cli_subagents.runner import Runner
from cli_subagents.runtimes.base import AttemptResult
from cli_subagents.runtimes.claude import ClaudeParser


def utc(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()


class SpendHome(TempHome):
    def setUp(self):
        super().setUp()
        os.environ.update(CLAUDE_CONFIG_DIR=str(self.primary), CODEX_HOME=str(self.root / "codex"))
        for key in ("FAKE_CLAUDE_COST", "FAKE_CLAUDE_DELAY"):
            os.environ.pop(key, None)
        self.config = load_config()
        for name, runtime in self.config.runtimes.items():
            runtime.primary = self.primary if name == "claude" else self.root / name
            runtime.accounts = []
        self.runtime = self.config.runtime("claude")
        self.runtime.include_primary = False
        self.runtime.max_jobs_per_account = 4
        self.account = Account("paid", self.root / "paid", max_jobs=4, spend_cap_usd=10)
        self.runtime.accounts = [self.account]
        save_config(self.config)

    def snapshot(self, now=None):
        return spend.snapshot(self.runtime, self.account, now=now)

    def history(self, *, at=None, cost=2.5, account="paid", runtime="claude", before_start=False,
                budget=None, outcome="success", active=False):
        """Import a job written by an older runner, without going through the ledger."""
        at = time.time() if at is None else at
        job_id = jobs.new_job_id()
        attempt = {"account": account, "attempted_at": at - 1, "started_at": at - 1,
                   "ended_at": at, "outcome": outcome, "cost_usd": cost,
                   "before_start": before_start}
        if budget is not None:
            attempt["budget_usd"] = budget
        folder = jobs.job_dir(job_id)
        folder.mkdir(parents=True)
        write_json(folder / "spec.json", {"runtime": runtime, "account": account})
        write_json(folder / "status.json", {
            "job_id": job_id, "runtime": runtime, "account": account,
            "state": "running" if active else "done", "outcome": None if active else outcome,
            "attempts": [attempt], "created_at": at, "updated_at": at + 1,
        })
        return job_id


class SpendAccountingTests(SpendHome):
    def test_uncapped_accounts_do_not_reserve_a_budget(self):
        self.account.spend_cap_usd = None
        self.assertIsNone(spend.reserve("claude", self.account, "uncapped", 0))
        state = self.snapshot()
        self.assertIsNone(state["spend_cap_usd"])
        self.assertIsNone(state["remaining_usd"])

    def test_import_is_idempotent_and_only_counts_the_matching_account_runtime(self):
        self.history(cost=2.5)
        self.history(cost=8, account="other")
        self.history(cost=9, runtime="codex")
        for _ in range(2):
            state = self.snapshot()
            self.assertEqual(state["spent_usd"], 2.5)
            self.assertEqual(state["reserved_usd"], 0)
            self.assertEqual(state["remaining_usd"], 7.5)

    def test_reserve_all_available_then_settle_actual_cost_and_release_difference(self):
        self.history(cost=2.5)
        self.assertEqual(spend.reserve("claude", self.account, "first", 0), 7.5)
        reserved = self.snapshot()
        self.assertEqual(reserved["spent_usd"], 2.5)
        self.assertEqual(reserved["reserved_usd"], 7.5)
        self.assertEqual(reserved["remaining_usd"], 0)
        spend.settle("claude", self.account, "first", 0, AttemptResult("success", cost_usd=1.25))
        settled = self.snapshot()
        self.assertEqual(settled["spent_usd"], 3.75)
        self.assertEqual(settled["reserved_usd"], 0)
        self.assertEqual(settled["remaining_usd"], 6.25)
        # Re-reading and settling the same terminal record never doubles its cost.
        spend.settle("claude", self.account, "first", 0, AttemptResult("success", cost_usd=1.25))
        self.assertEqual(self.snapshot()["spent_usd"], 3.75)

    def test_two_callers_cannot_reserve_the_same_remainder(self):
        barrier = threading.Barrier(2)

        def reserve(job_id):
            barrier.wait(timeout=10)
            try:
                return spend.reserve("claude", self.account, job_id, 0)
            except spend.SpendLimitError:
                return "refused"

        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(reserve, ("first", "second")))
        self.assertCountEqual(results, [10, "refused"])
        self.assertEqual(self.snapshot()["reserved_usd"], 10)

    def test_costless_started_attempts_charge_the_entire_reserved_budget(self):
        for outcome in ("error", "cancelled", "timeout", "lost"):
            with self.subTest(outcome=outcome):
                account = Account(outcome, self.root / outcome, spend_cap_usd=10)
                self.assertEqual(spend.reserve("claude", account, outcome, 0), 10)
                spend.settle("claude", account, outcome, 0, AttemptResult(outcome))
                state = spend.snapshot("claude", account)
                self.assertEqual((state["spent_usd"], state["reserved_usd"], state["remaining_usd"]), (10, 0, 0))

    def test_worker_not_started_releases_reservation_without_debit(self):
        spend.reserve("claude", self.account, "preflight", 0)
        spend.settle("claude", self.account, "preflight", 0, AttemptResult("auth", before_start=True))
        state = self.snapshot()
        self.assertEqual((state["spent_usd"], state["reserved_usd"], state["remaining_usd"]), (0, 0, 10))

    def test_legacy_costless_attempt_without_budget_charges_current_cap(self):
        self.history(cost=None, outcome="error")
        self.assertEqual(self.snapshot()["spent_usd"], 10)

    def test_legacy_costless_attempt_with_saved_budget_charges_that_budget(self):
        self.history(cost=None, budget=3.5, outcome="cancelled")
        self.assertEqual(self.snapshot()["spent_usd"], 3.5)

    def test_legacy_preflight_failure_does_not_charge_spend(self):
        self.history(cost=None, before_start=True, outcome="auth")
        self.assertEqual(self.snapshot()["spent_usd"], 0)

    def test_less_than_one_dollar_remaining_is_refused_with_actionable_details(self):
        self.history(cost=9.01)
        with self.assertRaises(spend.SpendLimitError) as raised:
            spend.reserve("claude", self.account, "too-little", 0)
        message = str(raised.exception)
        for detail in ("paid", "9.01", "10", "month", "spend_cap_usd"):
            self.assertIn(detail, message)
        self.assertEqual(self.snapshot()["reserved_usd"], 0)

    def test_exactly_one_dollar_remaining_can_be_reserved(self):
        self.history(cost=9)
        self.assertEqual(spend.reserve("claude", self.account, "minimum", 0), 1)

    def test_cleaning_job_folders_does_not_reset_account_spend(self):
        job_id = self.history(at=time.time() - 3 * 86400, cost=4)
        self.account.spend_period = "total"
        save_config(self.config)
        self.assertEqual(self.snapshot()["spent_usd"], 4)
        self.assertEqual(jobs.clean_old_jobs(1), 1)
        self.assertFalse(jobs.job_dir(job_id).exists())
        self.assertEqual(self.snapshot()["spent_usd"], 4)

    def test_cleanup_imports_costs_even_if_no_report_was_requested_first(self):
        job_id = self.history(at=time.time() - 3 * 86400, cost=4)
        self.account.spend_period = "total"
        save_config(self.config)
        self.assertEqual(jobs.clean_old_jobs(1), 1)
        self.assertFalse(jobs.job_dir(job_id).exists())
        self.assertEqual(self.snapshot()["spent_usd"], 4)

    def test_accounting_periods_are_utc_calendar_boundaries(self):
        now = utc("2026-11-01T00:00:00")  # Sunday, a month boundary and a day boundary.
        self.history(at=utc("2026-10-25T23:59:59"), cost=1)
        self.history(at=utc("2026-10-26T00:00:00"), cost=2)
        self.history(at=utc("2026-10-31T23:59:59"), cost=3)
        self.history(at=now, cost=4)
        self.history(at=now + 86400, cost=8)
        for period, expected, start, end in (
            ("day", 4, now, now + 86400),
            ("week", 9, utc("2026-10-26T00:00:00"), utc("2026-11-02T00:00:00")),
            ("month", 4, now, utc("2026-12-01T00:00:00")),
            ("total", 10, 0, None),
        ):
            with self.subTest(period=period):
                self.account.spend_period = period
                state = self.snapshot(now)
                self.assertEqual(state["spent_usd"], expected)
                self.assertEqual(state["period_start"], start)
                self.assertEqual(state["period_end"], end)

    def test_since_is_a_lower_bound_within_the_calendar_period(self):
        self.history(at=utc("2026-10-01T00:00:00"), cost=1)
        self.history(at=utc("2026-10-22T23:59:59"), cost=2)
        self.history(at=utc("2026-10-23T00:00:00"), cost=3)
        self.account.spend_since = "2026-10-23"
        state = self.snapshot(utc("2026-10-24T12:00:00"))
        self.assertEqual(state["spent_usd"], 3)
        self.assertEqual(state["period_start"], utc("2026-10-23T00:00:00"))
        self.account.spend_since = "2026-09-01"
        state = self.snapshot(utc("2026-10-24T12:00:00"))
        self.assertEqual(state["spent_usd"], 6)
        self.assertEqual(state["period_start"], utc("2026-10-01T00:00:00"))

    def test_running_reservations_survive_a_new_period_and_since_reset(self):
        before = utc("2026-10-31T23:59:59")
        after = utc("2026-11-01T00:00:01")
        spend.reserve("claude", self.account, "overnight", 0, now=before)
        self.account.spend_since = "2026-11-01"
        state = self.snapshot(after)
        self.assertEqual((state["spent_usd"], state["reserved_usd"], state["remaining_usd"]), (0, 10, 0))
        with self.assertRaises(spend.SpendLimitError):
            spend.reserve("claude", self.account, "after-reset", 0, now=after)

    def test_dead_runner_and_worker_conservatively_settle_a_stale_reservation(self):
        spend.reserve("claude", self.account, "abandoned", 0)
        with patch.object(spend, "same_process", return_value=False):
            state = self.snapshot()
        self.assertEqual((state["spent_usd"], state["reserved_usd"], state["remaining_usd"]), (10, 0, 0))
        self.assertEqual(self.snapshot()["spent_usd"], 10)

    def test_corrupt_ledger_refuses_snapshot_and_new_reservations(self):
        ledger = self.home / "state" / "claude-spend.json"
        ledger.parent.mkdir(parents=True)
        ledger.write_text('{"version": 1, "attempts": ', encoding="utf-8")
        with self.assertRaisesRegex(spend.SpendLimitError, "ledger"):
            self.snapshot()
        with self.assertRaisesRegex(spend.SpendLimitError, "ledger"):
            spend.reserve("claude", self.account, "corrupt", 0)


class SpendRunnerTests(SpendHome):
    def run_job(self, *, pinned="paid"):
        job = jobs.create_job(runtime="claude", prompt="task", cwd=str(self.project), access="read",
                              account=pinned, start=False)
        Runner(job["job_id"]).run()
        return jobs.read_status(job["job_id"])

    def budget_argv(self, index=-1):
        argv = self.records()[index]["argv"]
        self.assertIn("--max-budget-usd", argv)
        return float(argv[argv.index("--max-budget-usd") + 1])

    def test_worker_receives_remaining_budget_and_next_job_uses_settled_cost(self):
        self.history(cost=2.5)
        with patch.dict(os.environ, {"FAKE_CLAUDE_COST": "1.25"}):
            first = self.run_job()
        self.assertEqual(first["outcome"], "success", first)
        self.assertEqual(self.budget_argv(), 7.5)
        self.assertEqual(self.snapshot()["spent_usd"], 3.75)
        second = self.run_job()
        self.assertEqual(second["outcome"], "success", second)
        self.assertEqual(self.budget_argv(), 6.25)

    def test_uncapped_worker_has_no_budget_flag(self):
        self.account.spend_cap_usd = None
        save_config(self.config)
        status = self.run_job()
        self.assertEqual(status["outcome"], "success", status)
        self.assertNotIn("--max-budget-usd", self.records()[0]["argv"])

    def test_spend_cap_refuses_a_start_even_with_override(self):
        self.history(cost=10)
        for override in (None, "user asked for more parallel jobs"):
            with self.subTest(override=override):
                with self.assertRaises(ValueError) as raised:
                    jobs.create_job(runtime="claude", prompt="task", cwd=str(self.project), account="paid", override=override)
                for detail in ("paid", "10", "spend_cap_usd"):
                    self.assertIn(detail, str(raised.exception))
        self.assertEqual(self.records(), [])

    def test_parallel_running_job_reserves_remainder_and_blocks_second_start(self):
        with patch.dict(os.environ, {"FAKE_CLAUDE_SCENARIO": "slow"}):
            first = jobs.create_job(runtime="claude", prompt="first", cwd=str(self.project), account="paid")
        deadline = time.monotonic() + 30
        while jobs.read_status(first["job_id"]).get("state") != "running" or not self.records():
            self.assertLess(time.monotonic(), deadline, jobs.read_status(first["job_id"]))
            time.sleep(0.05)
        try:
            self.assertEqual(self.snapshot()["reserved_usd"], 10)
            # max_jobs=4 leaves execution slots; this refusal must come from dollars.
            with self.assertRaises(ValueError) as raised:
                jobs.create_job(runtime="claude", prompt="second", cwd=str(self.project), account="paid")
            self.assertIn("spend", str(raised.exception).lower())
            self.assertEqual(len(self.records()), 1)
        finally:
            status = jobs.cancel(first["job_id"])
            self.assertEqual(status["outcome"], "cancelled", status)
        self.assertEqual(self.snapshot()["spent_usd"], 10)
        self.assertEqual(self.snapshot()["reserved_usd"], 0)

    def test_budget_result_stops_without_retry_failover_or_quota_cooldown(self):
        self.runtime.accounts.append(Account("spare", self.root / "spare"))
        self.runtime.capacity_retries = 3
        save_config(self.config)
        with patch.dict(os.environ, {"FAKE_CLAUDE_SCENARIO": "budget"}):
            status = self.run_job(pinned=None)
        self.assertEqual(status["outcome"], "budget", status)
        self.assertIn("budget", status["message"].lower())
        self.assertEqual([attempt["account"] for attempt in status["attempts"]], ["paid"])
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.snapshot()["spent_usd"], 10)
        self.assertTrue(all(accounts.cooldown_until(record) is None for record in accounts.load_usage("claude").values()))

    def test_crash_without_final_cost_charges_reserved_budget(self):
        with patch.dict(os.environ, {"FAKE_CLAUDE_SCENARIO": "crash"}):
            status = self.run_job()
        self.assertEqual(status["outcome"], "error", status)
        self.assertEqual(self.snapshot()["spent_usd"], 10)
        self.assertEqual(self.snapshot()["reserved_usd"], 0)

    def test_partial_usage_without_final_cost_stays_conservative(self):
        with patch.dict(os.environ, {"FAKE_CLAUDE_SCENARIO": "usage_no_result"}):
            status = self.run_job()
        self.assertEqual(status["outcome"], "error", status)
        self.assertEqual(self.snapshot()["spent_usd"], 10)

    def test_budget_parser_preserves_cost_and_has_its_own_outcome(self):
        parser = ClaudeParser()
        parser.feed({"type": "result", "subtype": "error_max_budget_usd", "is_error": True,
                     "total_cost_usd": 3.25, "errors": ["Maximum dollar budget exceeded"]})
        result = parser.finish(1, "")
        self.assertEqual(result.outcome, "budget")
        self.assertEqual(result.cost_usd, 3.25)
        self.assertFalse(result.at_capacity)

    def test_synthetic_crash_result_with_zeroed_cost_does_not_prove_a_free_run(self):
        parser = ClaudeParser()
        parser.feed({"type": "result", "subtype": "error_during_execution", "is_error": True,
                     "total_cost_usd": 0, "errors": ["worker process crashed"]})
        result = parser.finish(1, "")
        self.assertEqual(result.outcome, "error")
        self.assertIsNone(result.cost_usd)


class RollingSpendTests(SpendHome):
    def setUp(self):
        super().setUp()
        self.now = utc("2026-10-23T12:00:00")
        self.account.spend_cap_usd = None
        self.account.spend_caps = [SpendCap(300, "24h"), SpendCap(500, "7d")]
        save_config(self.config)

    def windows(self, now=None):
        state = self.snapshot(self.now if now is None else now)
        return {cap["window"]: cap for cap in state["spend_caps"]}

    def test_daily_cap_binds_while_weekly_cap_has_more_room(self):
        self.history(at=self.now - 2 * 86400, cost=100)
        self.history(at=self.now, cost=50)
        state = self.snapshot(self.now)
        windows = self.windows()
        self.assertEqual((windows["24h"]["spent_usd"], windows["24h"]["remaining_usd"]), (50, 250))
        self.assertEqual((windows["7d"]["spent_usd"], windows["7d"]["remaining_usd"]), (150, 350))
        self.assertEqual((state["spend_cap_usd"], state["spent_usd"], state["remaining_usd"]), (300, 50, 250))
        self.assertEqual(spend.reserve("claude", self.account, "daily", 0, now=self.now), 250)

    def test_weekly_cap_can_bind_despite_unused_daily_allowance(self):
        self.history(at=self.now - 2 * 86400, cost=400)
        self.history(at=self.now, cost=50)
        state = self.snapshot(self.now)
        windows = self.windows()
        self.assertEqual((windows["24h"]["spent_usd"], windows["24h"]["remaining_usd"]), (50, 250))
        self.assertEqual((windows["7d"]["spent_usd"], windows["7d"]["remaining_usd"]), (450, 50))
        self.assertEqual((state["spend_cap_usd"], state["spent_usd"], state["remaining_usd"]), (500, 450, 50))
        self.assertEqual(spend.reserve("claude", self.account, "weekly", 0, now=self.now), 50)

    def test_rolling_windows_exclude_lower_boundary_and_include_now(self):
        self.history(at=self.now - 7 * 86400, cost=1)
        self.history(at=self.now - 7 * 86400 + 0.001, cost=2)
        self.history(at=self.now - 86400, cost=4)
        self.history(at=self.now - 86400 + 0.001, cost=8)
        self.history(at=self.now, cost=16)
        self.history(at=self.now + 0.001, cost=32)
        windows = self.windows()
        self.assertEqual(windows["24h"]["spent_usd"], 24)
        self.assertEqual(windows["7d"]["spent_usd"], 30)
        self.assertEqual(windows["24h"]["period_start"], self.now - 86400)
        self.assertEqual(windows["7d"]["period_start"], self.now - 7 * 86400)

    def test_reservation_counts_against_every_window_and_settlement_releases_it(self):
        self.history(at=self.now - 2 * 86400, cost=100)
        self.history(at=self.now, cost=50)
        spend.reserve("claude", self.account, "reserved", 0, now=self.now)
        windows = self.windows()
        self.assertEqual((windows["24h"]["reserved_usd"], windows["24h"]["remaining_usd"]), (250, 0))
        self.assertEqual((windows["7d"]["reserved_usd"], windows["7d"]["remaining_usd"]), (250, 100))
        with self.assertRaises(spend.SpendLimitError):
            spend.reserve("claude", self.account, "second", 0, now=self.now)
        spend.settle("claude", self.account, "reserved", 0,
                     AttemptResult("success", cost_usd=25), now=self.now)
        windows = self.windows()
        self.assertEqual((windows["24h"]["spent_usd"], windows["24h"]["reserved_usd"], windows["24h"]["remaining_usd"]),
                         (75, 0, 225))
        self.assertEqual((windows["7d"]["spent_usd"], windows["7d"]["reserved_usd"], windows["7d"]["remaining_usd"]),
                         (175, 0, 325))

    def test_costless_attempt_debits_its_grant_in_both_windows(self):
        self.history(at=self.now - 2 * 86400, cost=100)
        self.history(at=self.now, cost=50)
        self.assertEqual(spend.reserve("claude", self.account, "costless", 0, now=self.now), 250)
        spend.settle("claude", self.account, "costless", 0, AttemptResult("cancelled"), now=self.now)
        windows = self.windows()
        self.assertEqual((windows["24h"]["spent_usd"], windows["24h"]["remaining_usd"]), (300, 0))
        self.assertEqual((windows["7d"]["spent_usd"], windows["7d"]["remaining_usd"]), (400, 100))

    def test_since_reset_is_inclusive_for_every_rolling_window(self):
        since = utc("2026-10-23T00:00:00")
        self.history(at=since - 1, cost=100)
        self.history(at=since, cost=50)
        self.history(at=self.now, cost=25)
        self.account.spend_since = "2026-10-23"
        windows = self.windows()
        self.assertEqual((windows["24h"]["spent_usd"], windows["24h"]["remaining_usd"]), (75, 225))
        self.assertEqual((windows["7d"]["spent_usd"], windows["7d"]["remaining_usd"]), (75, 425))
        self.assertEqual(windows["24h"]["period_start"], since)
        self.assertEqual(windows["7d"]["period_start"], since)

    def test_running_reservation_survives_rolling_expiry_and_since_reset(self):
        self.assertEqual(spend.reserve("claude", self.account, "long-running", 0, now=self.now), 300)
        self.account.spend_since = "2026-10-24"
        windows = self.windows(self.now + 8 * 86400)
        self.assertEqual((windows["24h"]["reserved_usd"], windows["24h"]["remaining_usd"]), (300, 0))
        self.assertEqual((windows["7d"]["reserved_usd"], windows["7d"]["remaining_usd"]), (300, 200))
        with self.assertRaises(spend.SpendLimitError):
            spend.reserve("claude", self.account, "after-expiry", 0, now=self.now + 8 * 86400)

    def test_refusal_names_the_binding_rolling_window(self):
        for window, old_cost, recent_cost in (("24h", 0, 300), ("7d", 300, 200)):
            with self.subTest(window=window):
                self.account = Account(window, self.root / window,
                                       spend_caps=[SpendCap(300, "24h"), SpendCap(500, "7d")])
                if old_cost:
                    self.history(account=window, at=self.now - 2 * 86400, cost=old_cost)
                self.history(account=window, at=self.now, cost=recent_cost)
                with self.assertRaises(spend.SpendLimitError) as raised:
                    spend.reserve("claude", self.account, "refused-" + window, 0, now=self.now)
                self.assertIn(window, str(raised.exception))
                self.assertIn("remaining", str(raised.exception))

    def test_fake_worker_budget_uses_the_minimum_of_rolling_allowances(self):
        now = time.time()
        self.history(at=now - 2 * 86400, cost=400)
        self.history(at=now - 1, cost=50)
        job = jobs.create_job(runtime="claude", prompt="task", cwd=str(self.project), access="read",
                              account="paid", start=False)
        with patch.dict(os.environ, {"FAKE_CLAUDE_COST": "5"}):
            Runner(job["job_id"]).run()
        status = jobs.read_status(job["job_id"])
        self.assertEqual(status["outcome"], "success", status)
        argv = self.records()[0]["argv"]
        self.assertIn("--max-budget-usd", argv)
        self.assertEqual(float(argv[argv.index("--max-budget-usd") + 1]), 50)
        windows = self.windows(time.time())
        self.assertEqual((windows["24h"]["spent_usd"], windows["24h"]["remaining_usd"]), (55, 245))
        self.assertEqual((windows["7d"]["spent_usd"], windows["7d"]["remaining_usd"]), (455, 45))
