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

    def backups(self, path):
        return sorted(item.name for item in path.parent.iterdir() if item.name.startswith("config.toml.") and item.name.endswith(".bak"))

    def test_a_save_keeps_the_replaced_file_with_its_comments_as_a_timestamped_backup(self):
        path = self.root / "config.toml"
        original = b"# my notes\njob_retention_days = 3\nmy_own_key = 1\n\n[claude]\nmax_jobs_per_account = 2\n"
        path.write_bytes(original)
        config = load_config(path)
        config.job_retention_days = 4
        save_config(config, path)
        names = self.backups(path)
        self.assertEqual(len(names), 1, names)
        self.assertRegex(names[0], r"^config\.toml\.\d{8}-\d{6}-\d{6}\.bak$")
        self.assertEqual((self.root / names[0]).read_bytes(), original)
        self.assertNotIn(b"# my notes", path.read_bytes())
        self.assertEqual(load_config(path).job_retention_days, 4)

    def test_only_the_newest_backups_are_kept_and_unchanged_saves_add_none(self):
        from cli_subagents import config as config_module

        path = self.root / "config.toml"
        save_config(load_config(path), path)  # no file yet: nothing to back up
        self.assertEqual(self.backups(path), [])
        unrelated = self.root / "config.toml.mine.bak"
        unrelated.write_text("keep me", encoding="utf-8")
        written = []
        for days in range(1, config_module.CONFIG_BACKUPS + 4):
            config = load_config(path)
            config.job_retention_days = days
            save_config(config, path)
            written.append(path.read_bytes())
            save_config(load_config(path), path)  # the same text again
        names = [name for name in self.backups(path) if name != unrelated.name]
        self.assertEqual(len(names), config_module.CONFIG_BACKUPS, names)
        # The newest backup holds the file before the last change.
        self.assertEqual((self.root / names[-1]).read_bytes(), written[-2])
        self.assertTrue(unrelated.exists())


if __name__ == "__main__":
    unittest.main()


class FlagTests(TempFolder):
    def test_quoted_booleans_are_refused_instead_of_meaning_true(self):
        # bool("false") is True: a quoted value used to keep a disabled account in use.
        for section, line in (("[[claude.accounts]]\nname = \"a1\"\n", 'enabled = "false"'),
                              ("[[claude.accounts]]\nname = \"a1\"\n", 'fallback = "false"'),
                              ("[claude]\n", 'share_parent_mcp = "false"'),
                              ("[codex]\n", 'include_primary = "false"'),
                              ("[antigravity]\n", 'sandbox = "false"')):
            with self.subTest(line=line):
                path = self.root / "config.toml"
                path.write_text(section + line + "\n", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "true or false"):
                    load_config(path)

    def test_real_booleans_still_load(self):
        path = self.root / "config.toml"
        path.write_text('[claude]\nshare_parent_mcp = false\n\n[[claude.accounts]]\nname = "a1"\nenabled = false\n'
                        'fallback = true\n', encoding="utf-8")
        config = load_config(path)
        [account] = config.runtime("claude").accounts
        self.assertEqual((account.enabled, account.fallback), (False, True))
        self.assertFalse(config.runtime("claude").share_parent_mcp)
