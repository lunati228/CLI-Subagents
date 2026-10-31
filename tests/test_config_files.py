"""Reading files users edit by hand (config.toml, settings.json)."""

import json
from pathlib import Path
import tempfile
import unittest

from cli_subagents import setup
from cli_subagents.config import load_config, save_config
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


class SaveConfigTests(TempFolder):
    def test_paths_and_names_outside_the_basic_plane_survive_a_save(self):
        # json.dumps escapes U+1F600 as a pair of surrogate escapes and leaves U+007F raw; TOML rejects both.
        path = self.root / "config.toml"
        home = str(self.root / "Jörg 😀" / "max-2")
        quote = lambda text: json.dumps(text, ensure_ascii=False)
        path.write_text(
            "[sandbox.claude]\nmode = \"powershell\"\n"
            f"additional_dirs = [{quote(str(self.root / 'проект 😀'))}]\n\n"
            "[claude]\n"
            f"primary = {quote(str(self.root / 'claude 😀'))}\n"
            'primary_models = ["model\\u007F\\U00020000"]\n\n'
            "[[claude.accounts]]\nname = \"max-2\"\n"
            f"home = {quote(home)}\n",
            encoding="utf-8",
        )
        model = "model\x7f\U00020000"
        config = load_config(path)
        save_config(config, path)
        again = load_config(path)
        self.assertEqual(again, config)
        self.assertEqual(str(again.runtime("claude").accounts[0].home), home)
        self.assertEqual(again.runtime("claude").primary_models, [model])


if __name__ == "__main__":
    unittest.main()
