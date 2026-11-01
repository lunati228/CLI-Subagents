"""Quota steps, model pools and web search, using local records and fake CLIs."""

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

from helpers import TempHome
from cli_subagents import accounts, cli, jobs, limits, quota, tools, usage_view
from cli_subagents.config import QuotaPool, load_config, save_config
from cli_subagents.fsutil import write_json
from cli_subagents.runner import Runner
from cli_subagents.runtimes.antigravity import AntigravityParser
from cli_subagents.runtimes.base import AttemptResult, JobSpec, USAGE_LIMIT
from cli_subagents.runtimes.codex import CodexRuntime

STEPS = [[75, 4], [50, 3], [25, 2], [5, 1], [0, 0]]
ARGON = "gemini-4-argon-high"
FLASH = "gemini-3.8-flash-high"


class QuotaTests(TempHome):
    def setUp(self):
        super().setUp()
        self.now = time.time()
        self.codex_home = self.root / "codex"
        self.codex_home.mkdir()
        os.environ.update(
            CLAUDE_CONFIG_DIR=str(self.primary), CODEX_HOME=str(self.codex_home),
            CLI_SUBAGENTS_CODEX_COMMAND=json.dumps([sys.executable, str(Path(__file__).with_name("fake_codex.py"))]),
            CLI_SUBAGENTS_AGY_COMMAND=json.dumps([sys.executable, str(Path(__file__).with_name("fake_agy.py"))]),
        )
        for key in ("FAKE_AGY_SCENARIO", "FAKE_AGY_LIMITED", "FAKE_CODEX_LIMITED", "FAKE_CODEX_SCENARIO"):
            os.environ.pop(key, None)
        self.configure()

    def configure(self, steps=False, pools=False, runtime="antigravity", extra="", accounts_list=("a1", "a2")):
        config = load_config()
        for name, item in config.runtimes.items():
            item.primary = self.primary if name == "claude" else self.root / name
        if steps:
            config.runtime("codex").max_jobs_by_room = STEPS
        if pools:
            item = config.runtime(runtime)
            item.failover = "on-limit"
            item.max_jobs_per_account = 10
            item.include_primary = False
            from cli_subagents.config import Account
            item.accounts = [Account(name, self.root / "homes" / name) for name in accounts_list]
            item.pools = [QuotaPool("argon", ["gemini-4-argon-*"], per_account=1, total=2, weekly_jobs=3, cooldown_minutes=120),
                          QuotaPool("flash", ["gemini-3.8-flash-*"], total=6)]
        save_config(config)
        if extra:
            with (self.home / "config.toml").open("a", encoding="utf-8") as handle:
                handle.write(extra)
        return load_config()

    def job_record(self, account="a1", model=ARGON, runtime="antigravity", active=False, started=None,
               outcome="success", attempts=None, assigned=None):
        job_id = jobs.new_job_id()
        folder = jobs.job_dir(job_id)
        folder.mkdir(parents=True)
        spec = {"model": model, "account": None}
        status = {"job_id": job_id, "runtime": runtime, "account": account,
                  "state": "running" if active else "done", "outcome": None if active else outcome,
                  "created_at": self.now, "updated_at": self.now, "attempts": attempts or []}
        if started is not None:
            status["started_at"] = started
        if assigned is not None:
            status.update(account=None, assigned_account=assigned)
        write_json(folder / "status.json", status)
        write_json(folder / "spec.json", spec)
        if active:
            (folder / "runner.pid").write_text(str(os.getpid()), encoding="utf-8")
        return job_id

    def usage(self, name="primary", used=0.72, weekly=None, reset=None):
        windows = {"five_hour": {"utilization": used, "resets_at": reset or self.now + 1000}}
        if weekly is not None:
            windows["seven_day"] = {"utilization": weekly, "resets_at": self.now + 2000}
        accounts.update_usage("codex", name, windows=windows)

    def admit(self, runtime="antigravity", model=ARGON, **kwargs):
        with patch.object(limits, "_available_runtime", side_effect=lambda name: name == "claude"):
            return limits.admit(load_config(), runtime, model=model, now=self.now, **kwargs)

    def test_new_options_are_off_by_default(self):
        runtime = load_config().runtime("codex")
        self.assertEqual((runtime.max_jobs_by_room, runtime.pools, runtime.web_search), ([], [], None))

    def test_config_round_trip(self):
        config = self.configure(steps=True, pools=True)
        config.runtime("codex").web_search = "indexed"
        save_config(config)
        config = load_config()
        self.assertEqual(config.runtime("codex").max_jobs_by_room, STEPS)
        self.assertEqual(config.runtime("codex").web_search, "indexed")
        pool = config.runtime("antigravity").pools[0]
        self.assertEqual((pool.name, pool.models, pool.per_account, pool.total, pool.weekly_jobs, pool.cooldown_minutes),
                         ("argon", ["gemini-4-argon-*"], 1, 2, 3, 120))

    def test_step_validation(self):
        for value in ([], [[50, 3]], [[25, 2], [50, 3], [0, 0]], [[101, 2], [0, 0]],
                      [[50, -1], [0, 0]], [[50, 1.5], [0, 0]], [[50, True], [0, 0]],
                      [[50, 1], [0, 2]], [[50, 2], [50, 1], [0, 0]], "bad"):
            with self.subTest(value=value):
                (self.home / "config.toml").write_text('[codex]\nmax_jobs_by_room = ' + json.dumps(value), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "max_jobs_by_room"):
                    load_config()

    def test_pool_validation(self):
        for fields in ('name = "bad name"\nmodels = ["*"]', 'name = "p"\nmodels = []',
                       'name = "p"\nmodels = "*"', 'name = "p"\nmodels = [1]',
                       *('name = "p"\nmodels = ["*"]\n' + key + ' = 0' for key in ("per_account", "total", "weekly_jobs", "cooldown_minutes")),
                       'name = "p"\nmodels = ["*"]\nper_account = true',
                       'name = "p"\nmodels = ["*"]\n[[codex.pools]]\nname = "p"\nmodels = ["*"]'):
            with self.subTest(fields=fields):
                (self.home / "config.toml").write_text('[[codex.pools]]\n' + fields, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "pool"):
                    load_config()

    def test_web_search_validation(self):
        for value in ("disabled", "cached", "indexed", "live"):
            (self.home / "config.toml").write_text('[codex]\nweb_search = ' + json.dumps(value), encoding="utf-8")
            self.assertEqual(load_config().runtime("codex").web_search, value)
        for text in ('[codex]\nweb_search = "auto"', '[codex]\nweb_search = true', '[claude]\nweb_search = "live"'):
            (self.home / "config.toml").write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "web_search"):
                load_config()

    def test_each_quota_threshold_is_strict(self):
        config = self.configure(steps=True)
        for left, expected in ((100, 4), (75.01, 4), (75, 3), (50.01, 3), (50, 2),
                               (25.01, 2), (25, 1), (5.01, 1), (5, 0), (0.01, 0), (0, 0)):
            with self.subTest(left=left):
                self.usage(used=1-left/100)
                self.assertEqual(quota.room_cap(config.runtime("codex"), self.now), (expected, left))

    def test_best_regular_account_uses_its_tightest_open_window(self):
        config = self.configure(steps=True)
        from cli_subagents.config import Account
        item = config.runtime("codex")
        item.include_primary = True
        item.accounts = [Account("paid", self.root / "paid"), Account("spare", self.root / "spare", fallback=True)]
        self.usage(used=0.1, weekly=0.8)
        self.usage("paid", used=0.4, weekly=0.6)
        self.usage("spare", used=0)
        self.assertEqual(quota.room_cap(item, self.now), (2, 40))
        item.accounts[0].enabled = False
        self.assertEqual(quota.room_cap(item, self.now), (1, 20))

    def test_a_job_pinned_to_a_fallback_account_uses_that_accounts_room(self):
        config = self.configure(steps=True)
        from cli_subagents.config import Account
        item = config.runtime("codex")
        item.include_primary = True
        item.accounts = [Account("spare", self.root / "spare", fallback=True, models=["luna"])]
        self.usage(used=0.99)
        self.usage("spare", used=0.4)
        self.assertEqual(quota.room_cap(item, self.now, "luna"), (0, 1))
        self.assertEqual(quota.room_cap(item, self.now, "luna", pinned="spare"), (3, 60))
        self.assertEqual(quota.room_cap(item, self.now, "luna", pinned="primary"), (0, 1))
        self.assertEqual(quota.room_cap(item, self.now, "sol", pinned="spare"), (4, None))  # model not allowed there

    def test_unknown_or_reset_usage_uses_top_step(self):
        item = self.configure(steps=True).runtime("codex")
        self.assertEqual(quota.room_cap(item, self.now), (4, None))
        self.usage(used=1, reset=self.now - 1)
        self.assertEqual(quota.room_cap(item, self.now), (4, None))
        self.usage(used=1, weekly=0.4, reset=self.now)
        self.assertEqual(quota.room_cap(item, self.now), (3, 60))

    def test_scaled_refusal_and_running_jobs_are_untouched(self):
        self.configure(steps=True)
        self.usage()
        ids = [self.job_record(runtime="codex", account="primary", model="m", active=True) for _ in range(2)]
        before = [jobs.read_status(job) for job in ids]
        with self.assertRaises(limits.LimitError) as raised:
            self.admit("codex", model="m")
        self.assertIn("Codex has 28% of its quota left, so 2 jobs at a time (max_jobs_by_room); 2 are running. Run this on Claude, or wait for one to finish.", str(raised.exception))
        self.assertEqual([jobs.read_status(job) for job in ids], before)

    def test_no_data_still_enforces_the_top_step(self):
        self.configure(steps=True)
        for _ in range(4):
            self.job_record(runtime="codex", account="primary", model="m", active=True)
        with self.assertRaisesRegex(limits.LimitError, "no quota data, so 4 jobs"):
            self.admit("codex", model="m")

    def test_zero_step_refuses_without_running_jobs(self):
        self.configure(steps=True)
        self.usage(used=0.95)
        with self.assertRaisesRegex(limits.LimitError, "so 0 jobs at a time"):
            self.admit("codex", model="m")

    def test_existing_smaller_caps_still_win(self):
        config = self.configure(steps=True)
        config.runtime("codex").max_jobs = 1
        save_config(config)
        self.job_record(runtime="codex", account="primary", model="m", active=True)
        with self.assertRaisesRegex(limits.LimitError, "max_jobs under"):
            self.admit("codex", model="m")

    def test_matching_is_first_glob_and_uses_default_model(self):
        item = self.configure(pools=True).runtime("antigravity")
        item.default_model = "gemini-4-argon-medium"
        item.pools.append(QuotaPool("all", ["*"]))
        self.assertEqual(quota.pool_for(item, None).name, "argon")
        self.assertEqual(quota.pool_for(item, "gemini-4-argon-low").name, "argon")
        self.assertEqual(quota.pool_for(item, FLASH).name, "flash")
        self.assertEqual(quota.pool_for(item, "other").name, "all")
        item.pools.pop()
        self.assertIsNone(quota.pool_for(item, "other"))

    def test_pool_per_account_cap_routes_to_another_account(self):
        self.configure(pools=True)
        self.job_record(active=True)
        self.assertEqual(self.admit().account, "a2")
        with self.assertRaisesRegex(limits.LimitError, "per_account in pool argon"):
            self.admit(account="a1")
        self.assertEqual(self.admit(model=FLASH, account="a1").account, "a1")

    def test_pools_apply_to_every_runtime(self):
        for name in ("claude", "codex", "antigravity"):
            with self.subTest(runtime=name):
                self.configure(pools=True, runtime=name)
                self.job_record(runtime=name, active=True)
                self.assertEqual(self.admit(name).account, "a2")

    def test_lease_accounts_include_workers_started_before_pool_slots_existed(self):
        item = self.configure(pools=True).runtime("antigravity")
        self.job_record(active=True)
        lease, _, _ = accounts.acquire(item, model=ARGON)
        self.assertEqual(lease.account.name, "a2")
        lease.release()

    def test_waiting_jobs_do_not_deadlock_pool_leases(self):
        item = self.configure(pools=True).runtime("antigravity")
        ids = [self.job_record(assigned=account, active=True) for account in ("a1", "a1", "a2")]
        for job in ids:
            jobs.update_status(job, state="queued")
        lease, _, _ = accounts.acquire(item, model=ARGON, job_id=ids[0])
        self.assertIsNotNone(lease)
        lease.release()

    def test_pool_total_cap_is_across_accounts(self):
        self.configure(pools=True)
        self.job_record(active=True)
        self.job_record(account="a2", active=True)
        with self.assertRaisesRegex(limits.LimitError, "2 jobs across accounts .total in pool argon"):
            self.admit()
        self.assertEqual(self.admit(model=FLASH).account, "a1")

    def test_unmatched_jobs_keep_the_account_wide_cap(self):
        config = self.configure(pools=True)
        config.runtime("antigravity").accounts[0].max_jobs = 1
        save_config(config)
        self.job_record(active=True)
        with self.assertRaisesRegex(limits.LimitError, "Antigravity account 'a1' is at its limit"):
            self.admit(model="other", account="a1")

    def test_weekly_budget_is_a_rolling_seven_days_and_counts_failed_starts(self):
        item = self.configure(pools=True).runtime("antigravity")
        for outcome in ("success", "error", "cancelled", "usage_limit"):
            self.job_record(started=self.now-1, outcome=outcome)
        for when, outcome in ((self.now-quota.WEEK, "success"), (self.now-quota.WEEK-1, "success"),
                              (self.now-1, "refused"), (self.now-1, "limit"), (self.now+1, "success")):
            self.job_record(started=when, outcome=outcome)
        self.job_record()  # a record that never started
        state = quota.pool_state(item, limits.count_active(self.now), self.now)
        self.assertEqual(state.weekly[("a1", "argon")], 4)
        self.assertEqual(self.admit().account, "a2")

    def test_failover_and_followups_count_once_per_job_on_each_account(self):
        item = self.configure(pools=True).runtime("antigravity")
        self.job_record(attempts=[{"account": account, "started_at": self.now-2} for account in ("a1", "a1", "a2")])
        state = quota.pool_state(item, limits.count_active(self.now), self.now)
        self.assertEqual(state.weekly, {("a1", "argon"): 1, ("a2", "argon"): 1})

    def test_cleanup_preserves_the_weekly_budget_history(self):
        self.configure(pools=True)
        recent = self.job_record(started=self.now-2*86400)
        old = self.job_record(started=self.now-8*86400)
        for job, age in ((recent, 2), (old, 8)):
            status = jobs.read_status(job)
            status["updated_at"] = self.now-age*86400
            write_json(jobs.job_dir(job) / "status.json", status)
        self.assertEqual(jobs.clean_old_jobs(1), 1)
        self.assertTrue(jobs.job_dir(recent).exists())
        self.assertFalse(jobs.job_dir(old).exists())

    def test_weekly_refusal_has_the_budget_and_alternative(self):
        self.configure(pools=True)
        for account in ("a1", "a2"):
            for _ in range(3):
                self.job_record(account=account, started=self.now-2)
        with self.assertRaises(limits.LimitError) as raised:
            self.admit()
        self.assertIn("Argon budget used up: 3 of 3 jobs this week on every account (weekly_jobs in pool argon). Use Opus instead.", str(raised.exception))

    def test_queued_starts_reserve_the_weekly_budget(self):
        self.configure(pools=True)
        for _ in range(2):
            self.job_record(started=self.now-2)
        self.job_record(account=None, assigned="a1", active=True)
        self.assertEqual(self.admit().account, "a2")
        with self.assertRaisesRegex(limits.LimitError, "weekly_jobs in pool argon"):
            self.admit(account="a1")

    def test_leases_enforce_per_account_and_total_caps(self):
        item = self.configure(pools=True, accounts_list=("a1", "a2", "a3")).runtime("antigravity")
        first, _, _ = accounts.acquire(item, model=ARGON, pinned="a1")
        self.assertIsNotNone(first)
        try:
            blocked, _, busy = accounts.acquire(item, model=ARGON, pinned="a1")
            self.assertIsNone(blocked)
            self.assertTrue(busy)
            second, _, _ = accounts.acquire(item, model=ARGON)
            self.assertEqual(second.account.name, "a2")
            try:
                blocked, _, busy = accounts.acquire(item, model=ARGON, pinned="a3")
                self.assertIsNone(blocked)
                self.assertTrue(busy)
                flash, _, _ = accounts.acquire(item, model=FLASH, pinned="a1")
                self.assertIsNotNone(flash)
                flash.release()
            finally:
                second.release()
        finally:
            first.release()
        again, _, _ = accounts.acquire(item, model=ARGON, pinned="a1")
        self.assertIsNotNone(again)
        again.release()

    def test_leases_skip_accounts_with_an_exhausted_weekly_budget(self):
        item = self.configure(pools=True).runtime("antigravity")
        for _ in range(3):
            self.job_record(started=self.now-2)
        lease, _, _ = accounts.acquire(item, model=ARGON)
        self.assertEqual(lease.account.name, "a2")
        lease.release()

    def test_pool_cooldown_leaves_other_models_available(self):
        item = self.configure(pools=True).runtime("antigravity")
        accounts.set_pool_cooldown("antigravity", "a1", "argon", self.now+1000, "quota")
        self.assertEqual(self.admit().account, "a2")
        self.assertEqual(self.admit(model=FLASH, account="a1").account, "a1")
        with self.assertRaisesRegex(limits.LimitError, "usage limit until"):
            self.admit(account="a1")
        lease, _, _ = accounts.acquire(item, model=ARGON)
        self.assertEqual(lease.account.name, "a2")
        lease.release()
        self.assertTrue(accounts.clear_cooldown("antigravity", "a1"))
        self.assertEqual(self.admit(account="a1").account, "a1")

    def test_pooled_cooldown_is_set_even_with_failover_off(self):
        config = self.configure(pools=True)
        config.runtime("antigravity").failover = "off"
        save_config(config)
        job = jobs.create_job(runtime="antigravity", prompt="x", cwd=str(self.project), model=ARGON, account="a1", start=False)
        runner = Runner(job["job_id"])
        before = time.time()
        with patch.object(runner, "_attempt", return_value=AttemptResult(USAGE_LIMIT, message="quota")):
            runner.run()
        record = accounts.load_usage("antigravity")["a1"]
        self.assertIsNone(accounts.cooldown_until(record))
        self.assertAlmostEqual(accounts.pool_cooldown_until(record, "argon"), before+120*60, delta=3)

    def test_pool_reset_time_beats_default_cooldown_and_retry_after(self):
        self.configure(pools=True)
        job = jobs.create_job(runtime="antigravity", prompt="x", cwd=str(self.project), model=ARGON, account="a1", start=False)
        runner = Runner(job["job_id"])
        reset = time.time()+quota.WEEK-100
        with patch.object(runner, "_attempt", return_value=AttemptResult(USAGE_LIMIT, message="Retry-After: 10", resets_at=reset)):
            runner.run()
        self.assertEqual(accounts.pool_cooldown_until(accounts.load_usage("antigravity")["a1"], "argon"), reset)

    def test_pool_cooldown_falls_back_to_runtime_setting(self):
        config = self.configure(pools=True)
        config.runtime("antigravity").pools[0].cooldown_minutes = None
        config.runtime("antigravity").cooldown_minutes = 15
        save_config(config)
        job = jobs.create_job(runtime="antigravity", prompt="x", cwd=str(self.project), model=ARGON, account="a1", start=False)
        runner = Runner(job["job_id"])
        before = time.time()
        with patch.object(runner, "_attempt", return_value=AttemptResult(USAGE_LIMIT, message="quota")):
            runner.run()
        until = accounts.pool_cooldown_until(accounts.load_usage("antigravity")["a1"], "argon")
        self.assertAlmostEqual(until, before+15*60, delta=3)

    def test_fake_agy_fails_over_to_an_account_with_pool_budget(self):
        config = self.configure(pools=True, accounts_list=("a1", "a2", "a3"))
        for account in config.runtime("antigravity").accounts:
            token = account.home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
            token.parent.mkdir(parents=True)
            token.write_text("fake", encoding="utf-8")
        for _ in range(3):
            self.job_record(account="a2", started=self.now-2)
        os.environ["FAKE_AGY_LIMITED"] = "a1"
        job = jobs.create_job(runtime="antigravity", prompt="x", cwd=str(self.project), model=ARGON, access="read")
        status = jobs.wait_for(job["job_id"], 60)
        self.assertEqual(status["outcome"], "success", status)
        self.assertEqual([a["account"] for a in status["attempts"]], ["a1", "a3"])
        record = accounts.load_usage("antigravity")["a1"]
        self.assertIsNone(accounts.cooldown_until(record))
        self.assertIsNotNone(accounts.pool_cooldown_until(record, "argon"))
        self.assertEqual(self.admit(model=FLASH, account="a1").account, "a1")

    def test_usage_errors_preserve_reset_times(self):
        for fields in ({"reset_time": "2026-10-20T12:00:00Z"}, {"resets_at": 1792497600}):
            parser = AntigravityParser()
            parser.feed({"event": "result", "result": {"status": "ERROR", "error": {"message": "quota reached", **fields}}})
            self.assertEqual(parser.finish(1, "").resets_at, 1792497600)
        parser = AntigravityParser()
        self.assertEqual(parser.finish(1, "Quota resets at 2026-10-20T12:00:00Z").resets_at, 1792497600)

    def test_web_search_argv_for_new_runs_and_resume(self):
        item = load_config().runtime("codex")
        item.windows_sandbox = "off"
        item.share_parent_mcp = False
        runtime = CodexRuntime()
        for mode in (None, "disabled", "cached", "indexed", "live"):
            item.web_search = mode
            for session in (None, "thread-1"):
                spec = JobSpec(job_id="test", runtime="codex", cwd=str(self.project), prompt="x", resume_session=session)
                argv = runtime.launch(item, item.primary_account(), spec, "x", {}).argv
                settings = [argv[i+1] for i, value in enumerate(argv[:-1]) if value == "-c"]
                search = [setting for setting in settings if setting.startswith("web_search=")]
                self.assertEqual(search, [] if mode is None else [f'web_search="{mode}"'])
                self.assertEqual("resume" in argv, session is not None)

    def test_default_model_is_saved_for_later_pool_counts(self):
        config = self.configure(pools=True)
        config.runtime("antigravity").default_model = ARGON
        save_config(config)
        job = jobs.create_job(runtime="antigravity", prompt="x", cwd=str(self.project), start=False)
        self.assertEqual(jobs.read_spec(job["job_id"]).model, ARGON)
        self.assertEqual(job["pool"], "argon")

    def test_reports_and_room_line_show_scaled_cap_and_pool_state(self):
        config = self.configure(steps=True, pools=True)
        self.usage()
        self.job_record(active=True, started=self.now-2)
        self.job_record(started=self.now-2)
        accounts.set_pool_cooldown("antigravity", "a1", "argon", self.now+1000, "quota")
        codex = tools.account_report("codex")
        self.assertEqual(codex["limits"]["room_cap"], 2)
        report = tools.call("subagent_accounts", {"runtime": "antigravity"})
        pool = report["runtimes"][0]["accounts"][1]["pools"][0]
        self.assertIn("Argon: 1 running of 1, 2 of 3 this week, cooling down until", pool["state"])
        line = usage_view.room_line(config, ["codex", "antigravity"], self.now)
        self.assertNotIn("\n", line)
        self.assertIn("2 jobs at a time (max_jobs_by_room, 28% left", line)
        self.assertIn("Argon: 1 running of 1, 2 of 3 this week", line)
        self.assertIn("Flash:", line)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli.cmd_doctor(argparse.Namespace(runtime="antigravity", live=False))
        self.assertIn("Argon: 1 running of 1, 2 of 3 this week", output.getvalue())

    def test_follow_header_shows_room_when_quota_controls_are_configured(self):
        from cli_subagents.follow import Follower

        self.configure(steps=True, pools=True)
        self.usage()
        job = jobs.create_job(runtime="codex", prompt="x", cwd=str(self.project), start=False)
        output = io.StringIO()
        follower = Follower(job["job_id"], None, 0, output)
        follower.header()
        lines = output.getvalue().splitlines()
        self.assertTrue(lines[0].startswith("[csa] CLI Subagents"))
        self.assertTrue(lines[1].startswith("| Room:"))
        self.assertIn("max_jobs_by_room", lines[1])
        self.assertIn("Argon:", lines[1])


if __name__ == "__main__":
    unittest.main()
