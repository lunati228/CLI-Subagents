"""csa's MXC installer (csa setup / update / doctor) against a small fake release zip.

Nothing here downloads the real release or runs the real wxc-exec: the asset
URL points at a local file, signatures are simulated and the probe runs
tests/fake_wxc.py through CLI_SUBAGENTS_SANDBOX_COMMAND.
"""

import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from cli_subagents import cli, setup, tools
from cli_subagents.config import SandboxSettings, load_config
from cli_subagents.sandbox import install, mxc

FAKE_WXC = Path(__file__).with_name("fake_wxc.py")
FAKE_HOST = Path(__file__).with_name("fake_host.py")

WINDOWS_X64 = {"wxc-exec.exe": b"x64 wxc-exec", "mxc.dll": b"x64 mxc dll", "Other.DLL": b"x64 other"}
MEMBERS = {
    **{f"x64/{name}": data for name, data in WINDOWS_X64.items()},
    "arm64/wxc-exec.exe": b"arm64 wxc-exec",
    "arm64/mxc.dll": b"arm64 mxc dll",
    # Non-Windows files and subfolders the installer must leave out.
    "x64/lxc-exec": b"linux", "x64/libmxc_ffi.so": b"so", "x64/mxc-exec-mac": b"mac", "x64/libmxc.dylib": b"dylib",
    "x64/unix-test-proxy": b"proxy", "x64/symbols/wxc-exec.pdb": b"pdb", "x64/symbols/extra.exe": b"deep exe",
    "x64/_manifest/spdx.json": b"{}", "x64/dotnet/Mxc.Net.dll": b"dotnet", "x64/sub/a.exe": b"deep",
    # Hostile names.
    "x64/../evil.exe": b"evil", "../evil2.exe": b"evil", "/abs/evil3.exe": b"evil", "/x64/evil4.exe": b"evil",
    "C:/evil5.exe": b"evil", "x64/C:evil6.exe": b"evil", "x64/..\\evil7.exe": b"evil", "x64/con.dll": b"evil",
    "x64/evil8.exe:stream": b"evil", "x64/.exe": b"evil",
}


def build_zip(path: Path, members: dict) -> tuple[int, str]:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(zipfile.ZipInfo("x64/"), b"")
        for name, data in members.items():
            archive.writestr(zipfile.ZipInfo(name), data)
    data = path.read_bytes()
    return len(data), hashlib.sha256(data).hexdigest()


class SandboxBase(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._temp.name).resolve()
        self.home = self.root / "csa"
        self.home.mkdir()
        self.claude_dir = self.root / "claude-main"
        self.claude_dir.mkdir()
        self.release = self.root / "release" / "mxc-release-binaries.zip"
        self.release.parent.mkdir()
        size, digest = build_zip(self.release, MEMBERS)
        fake_host = lambda kind: json.dumps([sys.executable, str(FAKE_HOST), kind])
        self._env = patch.dict(os.environ, {
            "CLI_SUBAGENTS_HOME": str(self.home),
            "CLAUDE_CONFIG_DIR": str(self.claude_dir),
            "CODEX_HOME": str(self.root / "codex-main"),
            "CLI_SUBAGENTS_CLAUDE_COMMAND": fake_host("claude"),
            "CLI_SUBAGENTS_CODEX_COMMAND": fake_host("codex"),
            "CLI_SUBAGENTS_AGY_COMMAND": fake_host("agy"),
            "FAKE_AGY_HOME": str(self.root / "agy-main"),
            "XDG_CONFIG_HOME": str(self.root / "xdg"),
            mxc.COMMAND_ENV: json.dumps([sys.executable, str(FAKE_WXC)]),
        })
        self._env.start()
        for key in ("FAKE_WXC_PROBE", "FAKE_WXC_PROBE_EXIT", "FAKE_WXC_FAIL", "FAKE_HOST_FAIL"):
            os.environ.pop(key, None)
        self.bad_signatures = set()
        self.signed = []
        self._patches = [
            patch.object(mxc, "arch", return_value="x64"),
            patch.object(mxc, "ASSET_SIZE", size),
            patch.object(mxc, "ASSET_SHA256", digest),
            patch.object(mxc, "ASSET_URL", self.release.as_uri()),
            patch.object(mxc, "signatures", side_effect=self.fake_signatures),
        ]
        for item in self._patches:
            item.start()

    def tearDown(self):
        for item in reversed(self._patches):
            item.stop()
        self._env.stop()
        self._temp.cleanup()

    def fake_signatures(self, paths, timeout=120):
        self.signed.append([Path(path).name for path in paths])
        result = {}
        for path in paths:
            bad = Path(path).name in self.bad_signatures
            result[str(path)] = {"status": "HashMismatch" if bad else "Valid", "subject": mxc.SIGNER, "ok": not bad}
        return result

    def ensure(self, settings=None, dry_run=False):
        report = setup.Report(dry_run=dry_run)
        with contextlib.redirect_stdout(io.StringIO()):
            install.ensure(settings or SandboxSettings(), report, dry_run=dry_run)
        return report

    def tree(self):
        return sorted(str(path.relative_to(self.home)) for path in self.home.rglob("*"))

    def installed(self):
        folder = mxc.install_dir(mxc.VERSION)
        return sorted(item.name for item in folder.iterdir()) if folder.is_dir() else []

    def assert_ok(self, report):
        self.assertEqual(report.failed, [], report)


class InstallTests(SandboxBase):
    def test_installs_only_the_top_level_windows_files_of_this_arch(self):
        report = self.ensure()
        self.assert_ok(report)
        self.assertEqual(self.installed(), sorted([*WINDOWS_X64, install.MARKER]))
        for name, data in WINDOWS_X64.items():
            self.assertEqual((mxc.install_dir(mxc.VERSION) / name).read_bytes(), data)
        marker = json.loads((mxc.install_dir(mxc.VERSION) / install.MARKER).read_text(encoding="utf-8"))
        self.assertEqual(marker["version"], mxc.VERSION)
        self.assertEqual(marker["asset_sha256"], mxc.ASSET_SHA256)
        self.assertEqual({entry["name"]: entry["sha256"] for entry in marker["files"]},
                         {name: hashlib.sha256(data).hexdigest() for name, data in WINDOWS_X64.items()})
        # Every extracted file was signature-checked before the swap.
        self.assertEqual(sorted(self.signed[0]), sorted(WINDOWS_X64))
        text = "\n".join(report.done)
        self.assertIn("downloaded mxc-release-binaries.zip", text)
        self.assertIn(f"installed MXC {mxc.VERSION} at {mxc.install_dir(mxc.VERSION)}: 3 files", text)
        self.assertIn("deleted the downloaded", text)
        self.assertIn("Sandbox: on. MXC's probe reports tier base-container", "\n".join(report.notes))

    def test_hostile_names_never_escape(self):
        self.assert_ok(self.ensure())
        everything = [path for path in self.root.rglob("*")]
        self.assertFalse([path for path in everything if "evil" in path.name], everything)
        self.assertFalse((mxc.install_root() / "con.dll").exists())
        # Only the release folder, csa's install and its lock remain.
        self.assertEqual(sorted(item.name for item in mxc.install_root().iterdir()), [install.LOCK, f"mxc-{mxc.VERSION}"])

    def test_member_name_rules(self):
        accept = {"x64/wxc-exec.exe": "wxc-exec.exe", "X64/Foo.DLL": "Foo.DLL", "x64/a-b_c.1.dll": "a-b_c.1.dll"}
        for name, expected in accept.items():
            self.assertEqual(install.member_name(name, "x64"), expected, name)
        reject = ["x64/../evil.exe", "../x64/a.exe", "/x64/a.exe", "C:/x64/a.exe", "x64/C:a.exe", "x64\\a.exe",
                  "x64/sub/a.exe", "x64/a.exe\x00.txt", "x64/a.exe:s", "x64/.exe", "x64/..", "x64/", "x64/lxc-exec",
                  "x64/libmxc_ffi.so", "arm64/a.exe", "x64/nul.exe", "x64/COM1.dll", "x64/a.exe ", "x64/a.exe.",
                  "x64/ a.exe"]
        for name in reject:
            self.assertIsNone(install.member_name(name, "x64"), name)

    def test_size_or_hash_mismatch_is_refused_before_extracting(self):
        for name, value in (("ASSET_SIZE", mxc.ASSET_SIZE + 1), ("ASSET_SHA256", "0" * 64)):
            with self.subTest(name), patch.object(mxc, name, value):
                report = self.ensure()
                self.assertTrue(report.failed, report)
                self.assertIn("wasn't installed", report.failed[0])
                self.assertFalse(mxc.install_dir(mxc.VERSION).exists())
                self.assertFalse(install.download_path().exists())
                self.assertFalse(list(mxc.install_root().glob(".staging-*")))
                self.assertFalse(list(install.download_dir().glob("*.part")))
                self.assertEqual(self.signed, [])
                self.assertIn("Run `csa setup` again", "\n".join(report.todo))

    def test_download_never_writes_more_than_the_pinned_size(self):
        with patch.object(mxc, "ASSET_SIZE", 10):
            report = self.ensure()
        self.assertIn("pinned release has 10", report.failed[0])
        self.assertFalse(list(install.download_dir().glob("*")))

    def test_reuses_a_verified_download_and_redownloads_a_bad_one(self):
        install.download_dir().mkdir(parents=True)
        install.download_path().write_bytes(self.release.read_bytes())
        with patch.object(mxc, "ASSET_URL", (self.root / "missing.zip").as_uri()):
            report = self.ensure()
        self.assert_ok(report)
        self.assertIn("reused the earlier download", "\n".join(report.done))
        self.assertFalse(install.download_path().exists())
        # Same size, wrong content: downloaded again.
        (mxc.install_dir(mxc.VERSION) / install.MARKER).unlink()
        install.download_dir().mkdir(parents=True)
        install.download_path().write_bytes(b"x" * mxc.ASSET_SIZE)
        report = self.ensure()
        self.assert_ok(report)
        self.assertIn("downloaded mxc-release-binaries.zip", "\n".join(report.done))

    def test_bad_signature_aborts_and_keeps_the_old_install(self):
        old = mxc.install_dir(mxc.VERSION)
        old.mkdir(parents=True)
        (old / "wxc-exec.exe").write_bytes(b"hand-extracted")
        (old / "lxc-exec").write_bytes(b"linux")
        self.bad_signatures = {"mxc.dll"}
        report = self.ensure()
        self.assertEqual(len(report.failed), 1, report)
        self.assertIn("valid Microsoft signature (mxc.dll: HashMismatch)", report.failed[0])
        self.assertIn("old install was left in place", report.failed[0])
        self.assertEqual(sorted(item.name for item in old.iterdir()), ["lxc-exec", "wxc-exec.exe"])
        self.assertEqual((old / "wxc-exec.exe").read_bytes(), b"hand-extracted")
        self.assertFalse(list(mxc.install_root().glob(".staging-*")))
        # The verified zip stays for the next try.
        self.assertTrue(install.download_path().is_file())
        self.bad_signatures = set()
        report = self.ensure()
        self.assert_ok(report)
        self.assertIn("replaced MXC", "\n".join(report.done))
        self.assertIn("no csa install record", "\n".join(report.done))
        self.assertEqual(self.installed(), sorted([*WINDOWS_X64, install.MARKER]))

    def test_second_run_is_unchanged_and_needs_no_download(self):
        self.assert_ok(self.ensure())
        with patch.object(mxc, "ASSET_URL", (self.root / "missing.zip").as_uri()):
            report = self.ensure()
        self.assert_ok(report)
        self.assertEqual(report.done, [])
        self.assertIn(f"MXC {mxc.VERSION} is installed at {mxc.install_dir(mxc.VERSION)} (3 files", report.unchanged[0])
        # The second run re-checked the installed files' signatures.
        self.assertEqual(sorted(self.signed[-1]), sorted(WINDOWS_X64))

    def test_changed_extra_or_unsigned_files_are_replaced(self):
        self.assert_ok(self.ensure())
        folder = mxc.install_dir(mxc.VERSION)
        cases = [
            ("was changed", lambda: (folder / "mxc.dll").write_bytes(b"patched")),
            ("files csa didn't install: version.dll", lambda: (folder / "version.dll").write_bytes(b"planted")),
            ("Other.DLL is missing", lambda: (folder / "Other.DLL").unlink()),
        ]
        for reason, damage in cases:
            with self.subTest(reason):
                damage()
                report = self.ensure()
                self.assert_ok(report)
                self.assertIn(reason, "\n".join(report.done))
                self.assertEqual(self.installed(), sorted([*WINDOWS_X64, install.MARKER]))
                self.assertEqual((folder / "mxc.dll").read_bytes(), WINDOWS_X64["mxc.dll"])
        # An installed file that no longer verifies (e.g. a revoked certificate) is reinstalled, which then fails.
        self.bad_signatures = {"wxc-exec.exe"}
        report = self.ensure()
        self.assertIn("valid Microsoft signature", report.failed[0])

    def test_other_csa_versions_are_removed_but_not_foreign_folders(self):
        ours = mxc.install_root() / "mxc-0.9.0" / "x64"
        ours.mkdir(parents=True)
        (ours / "wxc-exec.exe").write_bytes(b"old")
        (ours / install.MARKER).write_text(json.dumps({"version": "0.9.0"}), encoding="utf-8")
        foreign = mxc.install_root() / "mxc-0.8.0" / "x64"
        foreign.mkdir(parents=True)
        (foreign / "wxc-exec.exe").write_bytes(b"someone else's")
        report = self.ensure()
        self.assert_ok(report)
        self.assertFalse((mxc.install_root() / "mxc-0.9.0").exists())
        self.assertTrue((foreign / "wxc-exec.exe").is_file())
        self.assertIn(f"removed older MXC versions csa installed: {ours}", "\n".join(report.done))

    def test_dry_run_changes_nothing(self):
        before = self.tree()
        with patch.object(mxc, "ASSET_URL", (self.root / "missing.zip").as_uri()):
            report = self.ensure(dry_run=True)
        self.assert_ok(report)
        self.assertEqual(self.tree(), before)
        self.assertIn(f"would install MXC {mxc.VERSION}", report.done[0])
        self.assertIn("download file:", report.done[0])
        install.download_dir().mkdir(parents=True)
        install.download_path().write_bytes(self.release.read_bytes())
        before = self.tree()
        report = self.ensure(dry_run=True)
        self.assertEqual(self.tree(), before)
        self.assertIn("reuse the earlier download", report.done[0])
        self.assertIn("would delete the downloaded", "\n".join(report.done))
        self.assertEqual(self.signed, [])

    def test_probe_problems_are_reported_after_install(self):
        os.environ["FAKE_WXC_PROBE"] = json.dumps({"tier": "appcontainer", "needsDaclAugmentation": True, "probes": {}})
        report = self.ensure()
        self.assertTrue(mxc.default_executable(mxc.VERSION).is_file())
        self.assertEqual(len(report.failed), 1, report)
        self.assertIn("can't run csa's sandbox on this system", report.failed[0])
        self.assertIn("isolation tier 'appcontainer'", report.failed[0])
        self.assertIn("Update Windows", "\n".join(report.todo))
        self.assertIn("enabled = false", "\n".join(report.todo))
        os.environ["FAKE_WXC_PROBE_EXIT"] = "3"
        report = self.ensure()
        self.assertIn("its probe failed", report.failed[0])

    def test_member_size_cap(self):
        with patch.object(install, "MEMBER_LIMIT", 5):
            report = self.ensure()
        self.assertIn("larger than csa allows", report.failed[0])
        self.assertFalse(mxc.install_dir(mxc.VERSION).exists())


class SettingsTests(SandboxBase):
    def test_turned_off_does_nothing(self):
        report = self.ensure(SandboxSettings(enabled=False))
        self.assert_ok(report)
        self.assertEqual(report.done, [])
        self.assertIn("Sandbox: off, turned off with [sandbox] enabled = false", report.notes[0])
        self.assertEqual(self.tree(), [])

    def test_unsupported_system(self):
        os.environ.pop(mxc.COMMAND_ENV)
        with patch.object(mxc, "support", return_value=(False, "Windows build 22631.1 is too old")):
            report = self.ensure()
            self.assert_ok(report)
            self.assertIn("off, not available here (Windows build 22631.1 is too old)", report.notes[0])
            report = self.ensure(SandboxSettings(enabled=True))
            self.assertIn("enabled = true, but csa's sandbox can't run here: Windows build 22631.1", report.failed[0])
            self.assertIn("set it to false", report.todo[0])
        self.assertEqual(self.tree(), [])

    def test_configured_path(self):
        path = self.root / "own" / "wxc-exec.exe"
        report = self.ensure(SandboxSettings(path=path))
        self.assertIn("doesn't exist", report.failed[0])
        path.parent.mkdir()
        path.write_bytes(b"own build")
        report = self.ensure(SandboxSettings(path=path))
        self.assert_ok(report)
        self.assertIn(f"uses the configured {path}", report.unchanged[0])
        self.bad_signatures = {"wxc-exec.exe"}
        report = self.ensure(SandboxSettings(path=path))
        self.assertIn("isn't validly signed", report.failed[0])
        self.assertFalse(mxc.install_root().exists())


class WiringTests(SandboxBase):
    def test_setup_installs_and_no_sandbox_skips(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["setup", "--host", "claude", "--claude-route", "settings", "--no-sandbox"])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("Sandbox: skipped (--no-sandbox)", out.getvalue())
        self.assertFalse(mxc.install_root().exists())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["setup", "--host", "claude", "--claude-route", "settings"])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn(f"Sandbox: installed MXC {mxc.VERSION}", out.getvalue())
        self.assertTrue(mxc.default_executable(mxc.VERSION).is_file())

    def test_setup_dry_run_plans_only(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["setup", "--host", "claude", "--claude-route", "settings", "--dry-run"])
        self.assertIn(f"Sandbox: would install MXC {mxc.VERSION}", out.getvalue())
        self.assertFalse(mxc.install_root().exists())

    def test_update_keeps_the_sandbox_current(self):
        with patch.object(setup, "update_plugin", return_value=False), contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(["update", "--no-package", "--no-sandbox"])
            self.assertFalse(mxc.install_root().exists())
            code = cli.main(["update", "--no-package"])
        self.assertEqual(code, 0)
        self.assertTrue(mxc.default_executable(mxc.VERSION).is_file())
        # A newer pinned release is installed by the next update and the older one removed.
        old = mxc.install_dir(mxc.VERSION)
        size, digest = build_zip(self.release, {**MEMBERS, "x64/new.dll": b"new"})
        with patch.object(mxc, "VERSION", "1.0.2"), patch.object(mxc, "ASSET_SIZE", size), \
                patch.object(mxc, "ASSET_SHA256", digest), patch.object(setup, "update_plugin", return_value=False):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cli.main(["update", "--no-package"])
            self.assertEqual(code, 0, output.getvalue())
            self.assertIn("new.dll", self.installed())
            self.assertIn("mxc-1.0.2", str(mxc.install_dir(mxc.VERSION)))
        self.assertFalse(old.exists())
        self.assertIn("removed older MXC versions", output.getvalue())

    def test_run_setup_passes_the_config_settings(self):
        config = load_config()
        with patch.object(install, "ensure") as ensure, patch.object(setup, "install_plugin"), \
                patch.object(setup, "_check_primary_login"):
            setup.run_setup(config, ["claude"], [], dry_run=True)
        ensure.assert_called_once()
        self.assertIs(ensure.call_args.args[0], config.sandbox)
        self.assertTrue(ensure.call_args.kwargs["dry_run"])

    def test_unexpected_errors_are_reported(self):
        with patch.object(install, "ensure", side_effect=RuntimeError("boom")), \
                patch.object(setup, "update_plugin", return_value=False), contextlib.redirect_stdout(io.StringIO()):
            code, report = setup.run_update(load_config(), ["auto"], package=False)
        self.assertEqual(code, 1)
        self.assertIn("unexpected error: RuntimeError: boom", report.failed[0])


class DoctorTests(SandboxBase):
    def doctor(self, runtime="claude"):
        report = {"runtime": runtime, "binary": "x", "accounts": [{"name": "primary", "auth": {"loggedIn": True}}]}
        out = io.StringIO()
        with patch.object(tools, "account_report", return_value=report), contextlib.redirect_stdout(out):
            code = cli.cmd_doctor(argparse.Namespace(runtime=runtime, live=False))
        return code, out.getvalue()

    def write_config(self, text):
        (self.home / "config.toml").write_text(text, encoding="utf-8")

    def test_installed_and_working(self):
        self.assert_ok(self.ensure())
        code, out = self.doctor()
        self.assertEqual(code, 0, out)
        self.assertIn("Sandbox: on.", out)
        self.assertIn(f"MXC {mxc.VERSION}: {mxc.default_executable(mxc.VERSION)} (installed by csa; 3 files verified; signatures valid)", out)
        self.assertIn("Probe: tier base-container; denied paths yes; enumerate-only paths yes; DACL changes no", out)
        self.assertNotIn("Problem", out)
        info = install.status(SandboxSettings())
        self.assertTrue(info["installed"])
        self.assertEqual(info["integrity"], "verified")
        self.assertEqual(info["signatures"], {"ok": True, "problems": []})
        self.assertEqual(info["probe"]["tier"], "base-container")
        self.assertEqual(info["problems"], [])

    def test_problems_count(self):
        code, out = self.doctor()  # on, nothing installed; the test launcher still answers the probe
        self.assertEqual(code, 0, out)
        os.environ["FAKE_WXC_PROBE"] = json.dumps({"tier": "none", "probes": {}})
        code, out = self.doctor()
        self.assertEqual(code, 1, out)
        self.assertIn("Problem: sandbox: MXC reports isolation tier 'none'", out)
        os.environ.pop("FAKE_WXC_PROBE")
        self.assert_ok(self.ensure())
        (mxc.install_dir(mxc.VERSION) / "mxc.dll").write_bytes(b"patched")
        code, out = self.doctor()
        self.assertEqual(code, 1, out)
        self.assertIn("csa can't verify its MXC install", out)
        self.assertIn("mxc.dll was changed", out)

    def test_not_installed_without_a_test_launcher(self):
        os.environ.pop(mxc.COMMAND_ENV)
        with patch.object(mxc, "support", return_value=(True, "Windows build 26300.9711")):
            code, out = self.doctor("opencode")
        self.assertEqual(code, 1, out)
        self.assertIn(f"Problem: sandbox: MXC {mxc.VERSION} isn't installed", out)
        self.assertIn("isn't working here yet", out)

    def test_off_is_not_a_problem_and_skips_checks(self):
        self.write_config("[sandbox]\nenabled = false\n")
        with patch.object(mxc, "probe", side_effect=AssertionError("no probe when off")):
            code, out = self.doctor("opencode")
        self.assertEqual(code, 0, out)
        self.assertIn("Sandbox: off. Turned off with [sandbox] enabled = false.", out)
        self.assertIn("OpenCode read and edit workers use csa's review gate; csa's MXC sandbox is off here", out)
        self.assertEqual(self.signed, [])

    def test_unavailable_is_a_problem(self):
        os.environ.pop(mxc.COMMAND_ENV)
        self.write_config("[sandbox]\nenabled = true\n")
        with patch.object(mxc, "support", return_value=(False, "no MXC here")):
            code, out = self.doctor("opencode")
        self.assertEqual(code, 1, out)
        self.assertIn("Sandbox: unavailable. No MXC here.", out)
        self.assertIn("csa's sandbox is turned on but can't run here", out)

    def test_opencode_line_when_on(self):
        self.assert_ok(self.ensure())
        code, out = self.doctor("opencode")
        self.assertIn("OpenCode shell commands run in csa's MXC sandbox;", out)
        self.assertNotIn("OpenCode has no OS sandbox", out)


if __name__ == "__main__":
    unittest.main()
