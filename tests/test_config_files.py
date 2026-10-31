"""Reading files users edit by hand (config.toml, settings.json)."""

import json
from pathlib import Path
import tempfile
import unittest

from cli_subagents import setup
from cli_subagents.config import load_config
from cli_subagents.fsutil import read_json

BOM = b"\xef\xbb\xbf"


class TempFolder(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()


class ByteOrderMarkTests(TempFolder):
    """PowerShell 5.1 (`Set-Content -Encoding utf8`, `Out-File`) saves UTF-8 with a byte order mark."""

    def test_config_toml_with_a_byte_order_mark_loads(self):
        path = self.root / "config.toml"
        path.write_bytes(BOM + b'job_retention_days = 3\n\n[claude]\nmax_jobs_per_account = 2\n')
        config = load_config(path)
        self.assertEqual(config.job_retention_days, 3)
        self.assertEqual(config.runtime("claude").max_jobs_per_account, 2)

    def test_read_json_accepts_a_byte_order_mark(self):
        path = self.root / "settings.json"
        path.write_bytes(BOM + json.dumps({"env": {"A": "1"}}).encode("utf-8"))
        self.assertEqual(read_json(path, {}), {"env": {"A": "1"}})

    def test_settings_route_keeps_a_settings_file_with_a_byte_order_mark(self):
        primary = self.root / "claude"
        primary.mkdir()
        (primary / "settings.json").write_bytes(BOM + json.dumps({"model": "opus"}).encode("utf-8"))
        report = setup.Report()
        setup._declare_in_settings(primary, report, why=None)
        self.assertEqual(report.failed, [])
        data = json.loads((primary / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(data["model"], "opus")
        self.assertIs(data["enabledPlugins"][setup.PLUGIN_ID], True)
        self.assertTrue(setup._settings_declare_plugin(primary))


if __name__ == "__main__":
    unittest.main()
