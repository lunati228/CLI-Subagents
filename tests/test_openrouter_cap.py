"""The soft weekly cap on an account's OpenRouter key: overridable, never touches running jobs."""

import json
import os
from unittest.mock import patch

from test_spend import SpendHome
from cli_subagents import accounts, jobs, limits, openrouter, usage_view
from cli_subagents.config import Account, load_config, save_config
from cli_subagents.runner import Runner


class OpenRouterCapTests(SpendHome):
    def setUp(self):
        super().setUp()
        self.account.spend_cap_usd = None
        self.account.openrouter_weekly_cap_usd = 20
        save_config(self.config)
        self.account.home.mkdir(parents=True, exist_ok=True)
        (self.account.home / "settings.json").write_text(json.dumps({"env": {
            "ANTHROPIC_BASE_URL": "https://openrouter.ai/api", "ANTHROPIC_AUTH_TOKEN": "sk-or-test"}}), encoding="utf-8")
        self.calls = []

    def fake(self, usage):
        def fetch(key):
            self.calls.append(key)
            if isinstance(usage, Exception):
                raise usage
            return {"usage_weekly": usage}
        return patch.object(openrouter, "_fetch", fetch)

    def admit(self, override=None):
        return limits.admit(self.config, "claude", account="paid", override=override)

    def test_config_round_trip_and_validation(self):
        self.assertEqual(load_config().runtime("claude").find("paid").openrouter_weekly_cap_usd, 20)
        text = (self.home / "config.toml").read_text(encoding="utf-8")
        self.assertIn("openrouter_weekly_cap_usd = 20", text)
        for bad in ("0", "-1", '"20"'):
            with self.subTest(value=bad):
                (self.home / "config.toml").write_text(
                    f'[[claude.accounts]]\nname = "paid"\nopenrouter_weekly_cap_usd = {bad}\n', encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "openrouter_weekly_cap_usd"):
                    load_config()
        (self.home / "config.toml").write_text(
            '[[codex.accounts]]\nname = "x"\nopenrouter_weekly_cap_usd = 20\n', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Claude accounts only"):
            load_config()

    def test_under_the_cap_starts(self):
        with self.fake(19.5):
            self.assertEqual(self.admit().account, "paid")
        self.assertEqual(self.calls, ["sk-or-test"])

    def test_runner_skips_an_account_admission_rejected_for_its_soft_cap(self):
        self.runtime.accounts.append(Account("other", self.root / "other", max_jobs=4))
        save_config(self.config)
        with self.fake(20):
            self.assertEqual(limits.admit(self.config, "claude").account, "other")
            job = jobs.create_job(runtime="claude", prompt="task", cwd=str(self.project), start=False)
            self.assertEqual(Runner(job["job_id"]).run(), 0)
        status = jobs.read_status(job["job_id"])
        self.assertEqual([item["account"] for item in status["attempts"]], ["other"])

    def test_execution_rechecks_the_soft_cap_and_override_still_allows_it(self):
        with self.fake(20):
            with self.assertRaisesRegex(limits.LimitError, "soft cap"):
                accounts.acquire(self.runtime, pinned="paid")
            lease, _, _ = accounts.acquire(self.runtime, pinned="paid", override="user approved")
            self.assertIsNotNone(lease)
            lease.release()

    def test_at_the_cap_refuses_and_override_lets_one_through(self):
        with self.fake(20.0):
            with self.assertRaisesRegex(limits.LimitError, r"spent \$20.00 this week.*override=<their words>"):
                self.admit()
            self.assertEqual(self.admit(override="user said go over it this once").account, "paid")

    def test_unknown_spend_starts_with_a_warning(self):
        with self.fake(OSError("offline")):
            admission = self.admit()
        self.assertEqual(admission.account, "paid")
        self.assertTrue(any("without the soft cap check" in warning for warning in admission.warnings))

    def test_no_openrouter_settings_means_unknown_not_a_network_call(self):
        (self.account.home / "settings.json").write_text(json.dumps({"env": {}}), encoding="utf-8")
        with self.fake(99):
            self.assertEqual(self.admit().account, "paid")
        self.assertEqual(self.calls, [])

    def test_usage_is_cached_briefly(self):
        with self.fake(5):
            self.assertEqual(openrouter.weekly_usage(self.account, now=1000), 5)
            self.assertEqual(openrouter.weekly_usage(self.account, now=1000 + openrouter.CACHE_SECONDS - 1), 5)
            self.assertEqual(len(self.calls), 1)
            openrouter.weekly_usage(self.account, now=1000 + openrouter.CACHE_SECONDS)
            self.assertEqual(len(self.calls), 2)

    def test_a_finished_job_reports_openrouters_figure_not_claude_codes_estimate(self):
        from cli_subagents import jobs, tools
        from cli_subagents.runner import Runner

        with self.fake(2.72), patch.dict(os.environ, {"FAKE_CLAUDE_COST": "17.79"}):
            job = jobs.create_job(runtime="claude", prompt="task", cwd=str(self.project), account="paid",
                                  access="read", start=False)
            self.assertEqual(Runner(job["job_id"]).run(), 0)
            status = jobs.read_status(job["job_id"])
            report = tools.summarize(status)
        self.assertIsNone(status.get("cost_usd"))
        self.assertEqual(status["attempts"][0]["claude_code_estimate_usd"], 17.79)
        self.assertNotIn("cost_usd", report)
        self.assertIn("estimated $17.79", report["cost_note"])
        self.assertIn("$2.72 this week", report["cost_note"])

    def test_cost_note_uses_the_openrouter_attempts_account_after_failover(self):
        self.runtime.accounts.append(Account("other", self.root / "other"))
        save_config(self.config)
        job = jobs.create_job(runtime="claude", prompt="task", cwd=str(self.project), start=False)
        runner = Runner(job["job_id"])
        runner.attempts = [{"account": "paid", "claude_code_estimate_usd": 17.79},
                           {"account": "other", "cost_usd": 0.25}]
        with patch.object(openrouter, "weekly_usage", return_value=2.72) as usage:
            note = runner._openrouter_cost_note()
        self.assertEqual(usage.call_args.args[0].name, "paid")
        self.assertIn("paid", note)
        self.assertIn("$2.72 this week", note)

    def test_accounts_report_shows_the_keys_weekly_spend(self):
        from cli_subagents import tools

        with self.fake(2.72):
            report = tools.call("subagent_accounts", {"runtime": "claude"})
        entry = next(item for item in report["runtimes"][0]["accounts"] if item["name"] == "paid")
        self.assertEqual(entry["openrouter_key_week_usd"], 2.72)
        self.assertEqual(entry["openrouter_weekly_cap_usd"], 20)
        self.assertNotIn("spent_usd", entry)

    def test_room_line_shows_the_soft_cap(self):
        with self.fake(7.25):
            line = usage_view.room_line(self.config, ["claude"])
        self.assertIn("OpenRouter key $7.25 / $20.00 this week, soft cap", line)
