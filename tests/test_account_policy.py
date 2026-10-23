"""Account opt-in policy and spend settings shown without making model calls."""

import contextlib
import io
import json
from types import SimpleNamespace
from unittest.mock import patch

from helpers import TempHome

from cli_subagents import accounts, cli, spend, tools, usage_view
from cli_subagents.config import Account, RuntimeConfig, SpendCap, dump_config, load_config, save_config


class _AccountPolicyHome(TempHome):
    def runtime(self, *extra: Account) -> RuntimeConfig:
        return RuntimeConfig(runtime="claude", primary=self.primary, accounts=list(extra))

    def account(self, name="api", **kwargs) -> Account:
        return Account(name=name, home=self.root / name, **kwargs)

    def write_account(self, settings: str, runtime="claude") -> None:
        (self.home / "config.toml").write_text(
            f"[{runtime}]\nprimary = {json.dumps(str(self.primary))}\n"
            f'[[{runtime}.accounts]]\nname = "api"\nhome = {json.dumps(str(self.root / "api"))}\n'
            + settings + "\n",
            encoding="utf-8",
        )


class AccountPolicyTests(_AccountPolicyHome):
    def test_explicit_only_is_not_auto_picked_or_used_after_exclusion(self):
        runtime = self.runtime(self.account("regular"), self.account(explicit_only=True))
        ranked, _ = accounts.ranked_accounts(runtime, prefer="api")
        self.assertEqual([account.name for account in ranked], ["regular"])
        ranked, _ = accounts.ranked_accounts(runtime, exclude={"regular"})
        self.assertEqual(ranked, [])
        ranked, _ = accounts.ranked_accounts(runtime, pinned="api")
        self.assertEqual([account.name for account in ranked], ["api"])

    def test_an_explicit_only_pool_falls_back_to_primary(self):
        runtime = self.runtime(self.account(explicit_only=True))
        self.assertFalse(runtime.include_primary)
        ranked, _ = accounts.ranked_accounts(runtime)
        self.assertEqual([account.name for account in ranked], ["primary"])
        self.assertEqual([account.name for account in runtime.pool_accounts()], ["primary", "api"])
        self.assertEqual([account.name for account in runtime.listed_accounts()], ["primary", "api"])

    def test_disabled_and_explicit_only_accounts_remain_listed(self):
        runtime = self.runtime(
            self.account("regular"), self.account(explicit_only=True), self.account("off", enabled=False),
        )
        self.assertEqual([account.name for account in runtime.worker_accounts()], ["regular"])
        self.assertEqual([account.name for account in runtime.pool_accounts()], ["regular", "api"])
        self.assertEqual([account.name for account in runtime.listed_accounts()], ["regular", "api", "off"])

    def test_flags_and_spend_settings_survive_round_trip(self):
        self.write_account('explicit_only = true\nspend_cap_usd = 500.25\nspend_period = "week"\nspend_since = "2026-10-23T12:30:00Z"')
        config = load_config()
        text = dump_config(config)
        self.assertIn("spend_cap_usd = 500.25", text)
        self.assertIn("explicit_only = true", text)
        save_config(config)
        account = load_config().runtime("claude").find("api")
        self.assertTrue(account.explicit_only)
        self.assertEqual(account.spend_cap_usd, 500.25)
        self.assertEqual(account.spend_period, "week")
        self.assertEqual(account.spend_since, "2026-10-23T12:30:00Z")

    def test_defaults_and_supported_periods(self):
        self.write_account("")
        account = load_config().runtime("claude").find("api")
        self.assertFalse(account.explicit_only)
        self.assertIsNone(account.spend_cap_usd)
        self.assertEqual(account.spend_period, "month")
        self.assertEqual(account.spend_caps, [])
        for period in ("day", "week", "month", "total"):
            with self.subTest(period=period):
                self.write_account(f'spend_cap_usd = 500\nspend_period = "{period}"')
                self.assertEqual(load_config().runtime("claude").find("api").spend_period, period)

    def test_simultaneous_rolling_caps_round_trip_as_inline_tables(self):
        self.write_account(
            'spend_caps = [{ amount_usd = 300, window = "24h" }, { amount_usd = 500.25, window = "7d" }]\n'
            'spend_since = "2026-10-23"'
        )
        config = load_config()
        caps = [SpendCap(300, "24h"), SpendCap(500.25, "7d")]
        account = config.runtime("claude").find("api")
        self.assertEqual(account.spend_caps, caps)
        self.assertIsNone(account.spend_cap_usd)
        text = dump_config(config)
        self.assertIn('spend_caps = [{ amount_usd = 300.0, window = "24h" }, { amount_usd = 500.25, window = "7d" }]', text)
        save_config(config)
        again = load_config().runtime("claude").find("api")
        self.assertEqual(again.spend_caps, caps)
        self.assertEqual(again.spend_since, "2026-10-23")

    def test_rolling_caps_reject_malformed_lists_and_entries(self):
        for value in (
            "[]", "300", '"24h"', "[300]",
            '[{ amount_usd = 300 }]', '[{ window = "24h" }]',
            '[{ amount_usd = 300, window = "24h", typo = true }]',
        ):
            with self.subTest(value=value):
                self.write_account(f"spend_caps = {value}")
                with self.assertRaisesRegex(ValueError, "spend_caps"):
                    load_config()

    def test_rolling_caps_reject_invalid_amounts_and_windows(self):
        for value in ("0", "-1", "nan", "inf", '"300"', "true"):
            with self.subTest(amount=value):
                self.write_account(f'spend_caps = [{{ amount_usd = {value}, window = "24h" }}]')
                with self.assertRaisesRegex(ValueError, "spend_caps amount_usd must be a positive finite number"):
                    load_config()
        for value in ('"0h"', '"1.5h"', '"-7d"', '"week"', '"24H"', '"36501d"', '"876001h"', "24"):
            with self.subTest(window=value):
                self.write_account(f"spend_caps = [{{ amount_usd = 300, window = {value} }}]")
                with self.assertRaisesRegex(ValueError, "spend_caps window"):
                    load_config()
        for window in ("1h", "1d", "36500d", "876000h"):
            with self.subTest(window=window):
                self.write_account(f'spend_caps = [{{ amount_usd = 300, window = "{window}" }}]')
                self.assertEqual(load_config().runtime("claude").find("api").spend_caps, [SpendCap(300, window)])

    def test_scalar_and_rolling_cap_cannot_be_configured_together(self):
        self.write_account('spend_cap_usd = 500\nspend_caps = [{ amount_usd = 300, window = "24h" }]')
        with self.assertRaisesRegex(ValueError, "use spend_caps or spend_cap_usd, not both"):
            load_config()

    def test_spend_cap_rejects_invalid_values(self):
        for value in ("0", "-1", "nan", "inf", "-inf", '"500"', "true", "9" * 400):
            with self.subTest(value=value):
                self.write_account(f"spend_cap_usd = {value}")
                with self.assertRaisesRegex(ValueError, "spend_cap_usd must be a positive finite number"):
                    load_config()

    def test_spend_dates_and_periods_have_clear_errors(self):
        for value in ('"2026-02-30"', '"tomorrow"', '"20261023"', '"2026-10-23T12:00:00+02:00"', "2026-10-23"):
            with self.subTest(value=value):
                self.write_account(f"spend_since = {value}")
                with self.assertRaisesRegex(ValueError, "spend_since must be an ISO UTC date or datetime"):
                    load_config()
        self.write_account('spend_period = "year"')
        with self.assertRaisesRegex(ValueError, "spend_period must be one of"):
            load_config()
        for value in ("2026-10-23", "2026-10-23T12:00:00", "2026-10-23T12:00:00+00:00"):
            with self.subTest(value=value):
                self.write_account(f"spend_since = {json.dumps(value)}")
                self.assertEqual(load_config().runtime("claude").find("api").spend_since, value)

    def test_spend_settings_fail_closed_for_other_runtimes(self):
        for runtime in ("codex", "antigravity"):
            for setting in ("spend_cap_usd = 500", 'spend_period = "month"', 'spend_since = "2026-10-23"',
                            'spend_caps = [{ amount_usd = 300, window = "24h" }]'):
                with self.subTest(runtime=runtime, setting=setting):
                    self.write_account(setting, runtime)
                    with self.assertRaisesRegex(ValueError, "spend caps are supported for Claude accounts only"):
                        load_config()

    def test_explicit_only_requires_a_boolean(self):
        self.write_account('explicit_only = "true"')
        with self.assertRaisesRegex(ValueError, "explicit_only must be true or false"):
            load_config()


class AccountPolicyReportTests(_AccountPolicyHome):
    def setUp(self):
        super().setUp()
        self.write_account('enabled = false\nexplicit_only = true\nspend_cap_usd = 500\nspend_since = "2026-10-23"')

    def fake_snapshot(self, runtime, account, now=None):
        if account.spend_caps:
            caps = [
                {
                    "spent_usd": 80.0 if cap.window == "24h" else 450.0,
                    "reserved_usd": 5.0, "spend_cap_usd": cap.amount_usd,
                    "remaining_usd": 215.0 if cap.window == "24h" else 45.0,
                    "spend_period": cap.window, "window": cap.window, "spend_since": account.spend_since,
                    "period_start": 0.0, "period_end": None,
                }
                for cap in account.spend_caps
            ]
            return {**caps[1], "spend_caps": caps}
        return {
            "spent_usd": 20.0, "reserved_usd": 5.0, "spend_cap_usd": account.spend_cap_usd,
            "remaining_usd": 475.0 if account.spend_cap_usd is not None else None,
            "spend_period": account.spend_period, "spend_since": account.spend_since,
            "period_start": "2026-10-23T00:00:00Z", "period_end": "2026-11-01T00:00:00Z",
        }

    def test_accounts_list_and_mcp_include_disabled_explicit_account_and_spend(self):
        runtime = SimpleNamespace(find_binary=lambda config: None)
        with patch.object(tools, "get_runtime", return_value=runtime), patch.object(spend, "snapshot", side_effect=self.fake_snapshot), patch.object(tools, "room_note", return_value=""):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(cli.cmd_accounts(cli.build_parser().parse_args(["accounts", "list"])), 0)
            report = json.loads(output.getvalue())
            mcp_report = tools.call("subagent_accounts", {"runtime": "claude"})
        self.assertEqual([entry["name"] for entry in report["accounts"]], ["primary", "api"])
        entry = report["accounts"][1]
        self.assertFalse(entry["enabled"])
        self.assertTrue(entry["explicit_only"])
        self.assertEqual((entry["spent_usd"], entry["reserved_usd"], entry["spend_cap_usd"], entry["remaining_usd"]), (20, 5, 500, 475))
        self.assertEqual(entry["spend_since"], "2026-10-23")
        self.assertEqual(mcp_report["runtimes"][0]["accounts"][1], entry)

    def test_room_shows_explicit_only_disabled_and_spend_even_without_usage(self):
        with patch.object(spend, "snapshot", side_effect=self.fake_snapshot):
            line = usage_view.room_line(load_config(), ["claude"])
        self.assertIn("api explicit-only disabled", line)
        self.assertIn(spend.describe(self.fake_snapshot(None, load_config().runtime("claude").find("api"))), line)

    def test_doctor_prints_explicit_only_and_spend(self):
        runtime = SimpleNamespace(find_binary=lambda config: None)
        output = io.StringIO()
        with patch.object(tools, "get_runtime", return_value=runtime), patch.object(spend, "snapshot", side_effect=self.fake_snapshot), contextlib.redirect_stdout(output):
            cli.cmd_doctor(cli.build_parser().parse_args(["doctor"]))
        text = output.getvalue()
        self.assertIn("; explicit-only; disabled; ", text)
        self.assertIn(spend.describe(self.fake_snapshot(None, load_config().runtime("claude").find("api"))), text)

    def test_rolling_caps_are_reported_in_json_doctor_and_room(self):
        self.write_account(
            'enabled = false\nexplicit_only = true\n'
            'spend_caps = [{ amount_usd = 300, window = "24h" }, { amount_usd = 500, window = "7d" }]'
        )
        runtime = SimpleNamespace(find_binary=lambda config: None)
        output = io.StringIO()
        with patch.object(tools, "get_runtime", return_value=runtime), patch.object(spend, "snapshot", side_effect=self.fake_snapshot):
            report = tools.account_report("claude")
            room = usage_view.room_line(load_config(), ["claude"])
            with contextlib.redirect_stdout(output):
                cli.cmd_doctor(cli.build_parser().parse_args(["doctor"]))
        account = report["accounts"][1]
        self.assertEqual(account["spend_cap_usd"], 500)
        self.assertEqual(account["remaining_usd"], 45)
        self.assertEqual([cap["window"] for cap in account["spend_caps"]], ["24h", "7d"])
        for text in (room, output.getvalue()):
            for detail in ("24h", "7d", "$300.00", "$500.00", "$215.00", "$45.00"):
                self.assertIn(detail, text)
