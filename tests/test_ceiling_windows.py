"""`usage_ceiling_windows`: which usage windows count toward the ceiling."""

import unittest

from helpers import TempHome

from cli_subagents import accounts, limits
from cli_subagents.config import load_config


RECORD = {"windows": {"five_hour": {"utilization": 0.2}, "seven_day": {"utilization": 0.98}}}


class UtilizationTests(unittest.TestCase):
    def test_utilization_without_windows_counts_every_window(self):
        self.assertEqual(limits.utilization(RECORD, 1000.0), 0.98)

    def test_utilization_with_windows_counts_only_those(self):
        self.assertEqual(limits.utilization(RECORD, 1000.0, windows=("five_hour",)), 0.2)
        self.assertEqual(limits.utilization(RECORD, 1000.0, windows=("seven_day",)), 0.98)
        self.assertEqual(limits.utilization(RECORD, 1000.0, windows=("five_hour", "seven_day")), 0.98)
        self.assertIsNone(limits.utilization(RECORD, 1000.0, windows=("unknown",)))

    def test_utilization_without_a_record(self):
        self.assertIsNone(limits.utilization(None, 1000.0))
        self.assertIsNone(limits.utilization(None, 1000.0, windows=("five_hour",)))

    def test_max_utilization_without_windows_counts_every_window(self):
        self.assertEqual(accounts.max_utilization(RECORD), 0.98)

    def test_max_utilization_with_windows_counts_only_those(self):
        self.assertEqual(accounts.max_utilization(RECORD, windows=("five_hour",)), 0.2)
        self.assertEqual(accounts.max_utilization(RECORD, windows=("seven_day",)), 0.98)
        self.assertIsNone(accounts.max_utilization(RECORD, windows=("unknown",)))
        self.assertIsNone(accounts.max_utilization(None, windows=("five_hour",)))


class ConfigTests(TempHome):
    def load_runtime(self, value: str):
        (self.home / "config.toml").write_text(f"[codex]\nusage_ceiling_windows = {value}\n", encoding="utf-8")
        return load_config().runtime("codex")

    def test_unset_counts_every_window(self):
        (self.home / "config.toml").write_text("[codex]\n", encoding="utf-8")
        self.assertIsNone(load_config().runtime("codex").usage_ceiling_windows)

    def test_accepts_a_list_of_window_names(self):
        self.assertEqual(self.load_runtime('["five_hour"]').usage_ceiling_windows, ("five_hour",))
        self.assertEqual(self.load_runtime('["five_hour", "seven_day"]').usage_ceiling_windows, ("five_hour", "seven_day"))

    def test_rejects_bad_values(self):
        for value in ("[]", '[""]', '"five_hour"'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as raised:
                    self.load_runtime(value)
                self.assertIn("usage_ceiling_windows must be a non-empty list of window names", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
