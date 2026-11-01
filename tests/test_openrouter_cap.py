"""The soft weekly cap on an account's OpenRouter key: overridable, never touches running jobs."""

import json
from unittest.mock import patch

from test_spend import SpendHome
from cli_subagents import limits, openrouter, usage_view
from cli_subagents.config import load_config, save_config


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

    def test_room_line_shows_the_soft_cap(self):
        with self.fake(7.25):
            line = usage_view.room_line(self.config, ["claude"])
        self.assertIn("OpenRouter key $7.25 / $20.00 this week, soft cap", line)
