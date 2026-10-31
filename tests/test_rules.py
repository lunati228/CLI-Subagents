import io
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from cli_subagents import cli, rules, setup


class RulesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.enterContext(patch.object(Path, "home", return_value=self.root))
        self.enterContext(patch.dict(os.environ, {
            "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
            "CODEX_HOME": str(self.root / "codex"),
            "CLI_SUBAGENTS_HOME": str(self.root / "csa"),
        }))
        self.path = self.root / "instructions.md"

    def args(self, *args):
        return cli.build_parser().parse_args(["rules", *args])

    def run_cli(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = cli.main(["rules", *args])
        return code, output.getvalue(), errors.getvalue()

    def backup(self, path=None):
        path = path or self.path
        return path.with_name(f"{path.name}.csa-rules-backup")

    def test_render_defaults(self):
        text = rules.render(self.args())
        self.assertTrue(text.startswith(rules.START + "\n"))
        self.assertTrue(text.endswith(rules.END))
        self.assertIn("managed by `csa rules`", text)
        for tier, default in zip(rules.TIERS, rules.DEFAULTS):
            row = next(line for line in text.splitlines() if line.startswith(f"| {tier} |"))
            self.assertIn(f"Claude {default} | none |", row)
        self.assertIn("at most 4 home vendor subagents", text)
        self.assertIn("[depth 1 of 2: you may start up to 3 helpers, cross-CLI agents included]", text)

    def test_render_all_values(self):
        flags = []
        for tier in rules.TIERS:
            flags.extend((f"--home-{tier}", f"Home {tier}", f"--try-{tier}", f"Other {tier}"))
        text = rules.render(self.args(*flags, "--work-folder", "work-one", "--work-folder", "work-two",
                                      "--main-limit", "2", "--helper-limit", "0"))
        for tier in rules.TIERS:
            self.assertIn(f"Home {tier} | Other {tier} |", text)
        self.assertIn("- work-one\n- work-two", text)
        self.assertIn("at most 2 home vendor subagents", text)
        self.assertIn("up to 0 helpers", text)

    def test_vendor_defaults(self):
        for host, vendor in rules.VENDORS.items():
            with self.subTest(host=host):
                self.assertIn(f"{vendor} cheapest model", rules.render(self.args("--target", host)))
        for filename, vendor in (("AGENTS.md", "OpenAI"), ("GEMINI.md", "Google"), ("custom.md", "Claude")):
            self.assertIn(f"{vendor} cheapest model",
                          rules.render(self.args("--target", str(self.root / filename))))

    def test_model_text_is_escaped_for_markdown(self):
        text = rules.render(self.args("--home-low", "Model <v> | `high`", "--try-max", "NONE"))
        self.assertIn("Model &lt;v&gt; &#124; &#96;high&#96;", text)
        self.assertIn("| none |", text)

    def test_invalid_inputs(self):
        for flag, value in (("--home-low", ""), ("--try-high", "one\ntwo"),
                            ("--work-folder", "\t"), ("--target", " "),
                            ("--home-max", rules.START), ("--main-limit", "0"),
                            ("--helper-limit", "-1"), ("--main-limit", "1.5"),
                            ("--helper-limit", "many")):
            with self.subTest(flag=flag, value=value), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    self.args(flag, value)

    def test_actions_are_exclusive(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.args("--install", "--remove")

    def test_show_never_writes(self):
        before = list(self.root.iterdir())
        default = self.run_cli()
        explicit = self.run_cli("--show")
        self.assertEqual(default, explicit)
        self.assertEqual(default[0], 0)
        self.assertIn(rules.START, default[1])
        self.assertEqual(list(self.root.iterdir()), before)

    def test_install_new_file_and_parent(self):
        path = self.root / "new" / "CLAUDE.md"
        code, out, errors = self.run_cli("--install", "--target", str(path))
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("Installed delegation rules", out)
        self.assertIn(rules.START.encode(), path.read_bytes())
        self.assertFalse(self.backup(path).exists())

    def test_named_host_targets(self):
        for host, path in (("claude", self.root / "claude" / "CLAUDE.md"),
                           ("codex", self.root / "codex" / "AGENTS.md"),
                           ("antigravity", self.root / ".gemini" / "GEMINI.md")):
            with self.subTest(host=host):
                self.assertEqual(self.run_cli("--install", "--target", host)[0], 0)
                self.assertTrue(path.exists())

    def test_host_targets_without_env_overrides(self):
        with patch.dict(os.environ):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            os.environ.pop("CODEX_HOME", None)
            self.assertEqual(rules.target_path("claude")[0], self.root / ".claude" / "CLAUDE.md")
            self.assertEqual(rules.target_path("codex")[0], self.root / ".codex" / "AGENTS.md")

    def test_append_preserves_existing_bytes(self):
        for original in (b"own text", b"own text\n", b"own text\r\n", b"\xef\xbb\xbftext\n\xff"):
            with self.subTest(original=original):
                self.path.write_bytes(original)
                rules.update_file(self.path, rules.render(self.args()))
                self.assertTrue(self.path.read_bytes().startswith(original))

    def test_replace_preserves_outside_bytes(self):
        prefix, suffix = b"\xef\xbb\xbf# Own rules\r\n\xff ", b"\r\nKeep this.\nNo final newline"
        self.path.write_bytes(prefix + rules.START.encode() + b"\nold\n" + rules.END.encode() + suffix)
        result = rules.update_file(self.path, rules.render(self.args("--home-low", "Chosen model")))
        updated = self.path.read_bytes()
        self.assertIn("Replaced", result)
        self.assertTrue(updated.startswith(prefix))
        self.assertTrue(updated.endswith(suffix))
        self.assertNotIn(b"\nold\n", updated)
        self.assertIn(b"Chosen model", updated)
        self.assertEqual(updated.count(rules.START.encode()), 1)

    def test_crlf_block_uses_only_crlf(self):
        self.path.write_bytes(b"Own rules\r\n")
        rules.update_file(self.path, rules.render(self.args()))
        content = self.path.read_bytes()
        self.assertNotIn(b"\n", content.replace(b"\r\n", b""))
        self.assertNotIn(b"\r\r\n", content)
        rules.update_file(self.path, rules.render(self.args("--main-limit", "2")))
        self.assertNotIn(b"\n", self.path.read_bytes().replace(b"\r\n", b""))

    def test_lf_file_keeps_lf_on_windows(self):
        self.path.write_bytes(b"Own rules\n")
        rules.update_file(self.path, rules.render(self.args()))
        self.assertNotIn(b"\r", self.path.read_bytes())

    def test_remove_preserves_outside_bytes(self):
        prefix, suffix = b"Own rules\r\n\r\n", b"\n\r\nTail\xff"
        original = prefix + rules.render(self.args()).encode() + suffix
        self.path.write_bytes(original)
        code, out, errors = self.run_cli("--remove", "--target", str(self.path))
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("Removed", out)
        self.assertEqual(self.path.read_bytes(), prefix + suffix)
        self.assertEqual(self.backup().read_bytes(), original)

    def test_remove_without_block_does_not_write(self):
        for exists in (False, True):
            if exists:
                self.path.write_bytes(b"Own text\r\n")
            result = rules.update_file(self.path, None)
            self.assertIn("nothing changed", result)
            self.assertEqual(self.path.exists(), exists)
            self.assertFalse(self.backup().exists())

    def test_dry_run_new_file_creates_nothing(self):
        path = self.root / "missing" / "rules.md"
        code, out, errors = self.run_cli("--install", "--dry-run", "--target", str(path))
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("Would install", out)
        self.assertFalse(path.parent.exists())

    def test_dry_run_replace_and_remove_write_nothing(self):
        original = rules.render(self.args()).encode()
        self.path.write_bytes(original)
        for action in ("--install", "--remove"):
            code, out, errors = self.run_cli(action, "--dry-run", "--target", str(self.path), "--main-limit", "1")
            self.assertEqual((code, errors), (0, ""))
            self.assertIn("Would", out)
            self.assertEqual(self.path.read_bytes(), original)
            self.assertFalse(self.backup().exists())

    def test_backup_is_made_only_once(self):
        original = b"Own text\r\n"
        self.path.write_bytes(original)
        rules.update_file(self.path, rules.render(self.args()))
        self.assertEqual(self.backup().read_bytes(), original)
        rules.update_file(self.path, rules.render(self.args("--home-high", "Changed model")))
        rules.update_file(self.path, None)
        self.assertEqual(self.backup().read_bytes(), original)
        self.assertEqual(len(list(self.root.glob("*.csa-rules-backup"))), 1)

    def test_symlinked_file_is_written_through(self):
        dotfiles = self.root / "dotfiles"
        dotfiles.mkdir()
        real = dotfiles / "CLAUDE.md"
        real.write_bytes(b"Own text\n")
        try:
            os.symlink(real, self.path)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"no symlinks here: {exc}")
        rules.update_file(self.path, rules.render(self.args()))
        self.assertTrue(self.path.is_symlink())
        self.assertTrue(real.read_bytes().startswith(b"Own text\n"))
        self.assertIn(rules.START.encode(), real.read_bytes())
        self.assertEqual(self.backup(real).read_bytes(), b"Own text\n")
        rules.update_file(self.path, None)
        self.assertTrue(self.path.is_symlink())
        self.assertNotIn(rules.START.encode(), real.read_bytes())

    def test_reinstall_same_values_is_unchanged(self):
        block = rules.render(self.args())
        rules.update_file(self.path, block)
        self.assertIn("unchanged", rules.update_file(self.path, block))
        self.assertFalse(self.backup().exists())

    def test_broken_markers_are_rejected_without_writes(self):
        for text in (rules.START, rules.END, rules.END + rules.START,
                     rules.START * 2 + rules.END, rules.START + rules.END * 2):
            with self.subTest(text=text):
                self.path.write_bytes(text.encode())
                for action in ("--install", "--remove"):
                    code, _, errors = self.run_cli(action, "--target", str(self.path))
                    self.assertEqual(code, 2)
                    self.assertIn("markers", errors)
                    self.assertEqual(self.path.read_bytes(), text.encode())
                    self.assertFalse(self.backup().exists())

    def test_utf16_is_rejected_without_writes(self):
        original = "Own text".encode("utf-16")
        self.path.write_bytes(original)
        code, _, errors = self.run_cli("--install", "--target", str(self.path))
        self.assertEqual(code, 2)
        self.assertIn("UTF-16", errors)
        self.assertEqual(self.path.read_bytes(), original)

    def test_file_errors_are_reported(self):
        code, _, errors = self.run_cli("--install", "--target", str(self.root))
        self.assertEqual(code, 2)
        self.assertIn("Cannot change delegation rules", errors)

    def test_setup_mentions_optional_rules_without_installing(self):
        report = setup.format_report(setup.Report())
        self.assertIn("Optional: delegation rules", report)
        self.assertIn("csa rules --install", report)
        self.assertFalse((self.root / "claude").exists())
        self.assertNotIn("Optional:", setup.format_report(setup.Report(), name="update"))


if __name__ == "__main__":
    unittest.main()
