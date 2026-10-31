"""The review gates decide on network and device paths without opening them.

Opening \\\\server\\share\\x on Windows (realpath, stat, listing) resolves the server
name and connects to it over SMB with the user's NTLM login. The gates run on the
host, outside every sandbox, and look at paths before they decide, so a denied call
could still leak credentials or data in a host name. The server below is in the
reserved .invalid domain; the recorders fail the test if anything opens it.
"""

from __future__ import annotations

import contextlib
import glob
import json
import os
import pathlib
import sys
import unittest
from unittest import mock

from test_agy_review import TempCase

from cli_subagents import claude_read_hook, opencode_review
from cli_subagents.agy_review import context, paths

# Every spelling that reaches a server, on any host. Win32 hands \??\ paths to the
# NT namespace unchanged, so \??\UNC\ reaches a server, and so does \??\ followed by
# \\server\share. The tests below patch paths.IS_WINDOWS, so the Linux and macOS CI
# runs check the same rule as Windows.
NETWORK_SPELLINGS = (
    r"\\attacker.invalid\share\x.txt",
    "//attacker.invalid/share/x.txt",
    r"\\?\UNC\attacker.invalid\share\x.txt",
    r"\\.\UNC\attacker.invalid\share\x.txt",
    r"\\.\pipe\attacker",
    r"\??\UNC\attacker.invalid\share\x.txt",
    r"\??\\\attacker.invalid\share\x.txt",
    r"/??/UNC/attacker.invalid/share/x.txt",
)

SERVER_PATHS = NETWORK_SPELLINGS


def _network(value) -> bool:
    """A plain string check, like paths.network_path: ntpath must not decide it."""

    try:
        text = os.fspath(value)
    except TypeError:
        return False
    if not isinstance(text, str):
        return False
    text = text.strip().strip("\"'").replace("/", "\\").casefold()
    return text.startswith(("\\\\", "\\??\\"))


class NetworkPathStringTests(unittest.TestCase):
    """The rule itself: a plain string check that answers the same on every host."""

    def test_every_spelling_of_a_server_path_is_recognized(self) -> None:
        for value in NETWORK_SPELLINGS:
            with self.subTest(value=value), mock.patch.object(paths, "IS_WINDOWS", True):
                self.assertTrue(paths.network_path(value))

    def test_case_slashes_and_quotes_do_not_hide_a_server_path(self) -> None:
        for value in NETWORK_SPELLINGS:
            for spelling in (value.upper(), value.lower(), value.replace("\\", "/"), value.replace("/", "\\"),
                             f'"{value}"', f"'{value}'", f"  {value}  "):
                with self.subTest(value=value, spelling=spelling), \
                        mock.patch.object(paths, "IS_WINDOWS", True):
                    self.assertTrue(paths.network_path(spelling), spelling)

    def test_ordinary_paths_are_not_server_paths(self) -> None:
        for value in ("", "README.md", "sub/dir/x.txt", r"sub\dir\x.txt", r"C:\project\x.txt", "c:/project/x.txt",
                      "/home/user/x.txt", r"\server\share\x.txt", "./x", "../x", "~"):
            with self.subTest(value=value), mock.patch.object(paths, "IS_WINDOWS", True):
                self.assertFalse(paths.network_path(value))

    def test_a_posix_host_leaves_double_slash_paths_alone(self) -> None:
        for value in NETWORK_SPELLINGS:
            with self.subTest(value=value), mock.patch.object(paths, "IS_WINDOWS", False):
                self.assertFalse(paths.network_path(value))

    def test_the_rule_opens_and_normalizes_nothing(self) -> None:
        def forbidden(*args, **kwargs):
            raise AssertionError("network_path must only read the string")

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(paths, "IS_WINDOWS", True))
            for owner, name in ((os.path, "normpath"), (os.path, "isabs"), (os.path, "realpath"),
                                (os, "stat"), (os, "lstat"), (os, "listdir"), (os, "scandir"),
                                (os, "walk"), (os.path, "exists"), (pathlib.Path, "exists")):
                stack.enter_context(mock.patch.object(owner, name, forbidden))
            for value in NETWORK_SPELLINGS:
                self.assertTrue(paths.network_path(value), value)


class NormalizeJoinTests(TempCase):
    """Python 3.13's ntpath doesn't call a \\??\\ path absolute, so normalize() joins it to the base."""

    VALUE = r"\??\UNC\attacker.invalid\share\x.txt"

    def isabs_313(self):
        original = os.path.isabs

        def isabs(text):
            text = os.fspath(text)
            return not text.startswith("\\??\\") and original(text)

        return mock.patch.object(os.path, "isabs", isabs)

    @unittest.skipUnless(os.name == "nt", "network_path applies only on Windows")
    def test_check_denies_it_after_the_join(self) -> None:
        with mock.patch.object(paths, "IS_WINDOWS", True), self.isabs_313():
            normal, _resolved, reason = paths.check(self.VALUE, base=str(self.ws), homes=[])
        self.assertEqual(reason, paths.NETWORK_REASON)
        # 3.12 normalizes it to C:\??\..., 3.13 joins it to the workspace: either way
        # the written form is the only one that still shows the prefix.
        self.assertFalse(paths.network_path(normal), normal)
        self.assertTrue(paths.network_path(self.VALUE))

    def test_the_agy_rules_deny_it_after_the_join(self) -> None:
        with mock.patch.object(paths, "IS_WINDOWS", True), self.isabs_313():
            for name, args in (("view_file", {"AbsolutePath": self.VALUE}),
                               ("list_dir", {"DirectoryPath": self.VALUE}),
                               ("grep_search", {"SearchPath": self.VALUE}),
                               ("write_to_file", {"TargetFile": self.VALUE, "CodeContent": "x"})):
                with self.subTest(name=name):
                    self.assertEqual(self.decide(name, args).action, "deny")

    def test_the_opencode_gate_denies_it_after_the_join(self) -> None:
        path = self.root / "review-context.json"
        context.write_context(path, self.ctx(sandbox=False))
        with mock.patch.object(paths, "IS_WINDOWS", True), self.isabs_313():
            answer = opencode_review.run(json.dumps({"tool": "read", "input": {"filePath": self.VALUE}}).encode(), path)
        self.assertEqual(answer["decision"], "deny")
        self.assertIn(paths.NETWORK_REASON, answer["reason"])

    def test_a_workspace_on_a_share_still_works_after_the_join(self) -> None:
        share = r"\\attacker.invalid\share\project"
        with mock.patch.object(paths, "IS_WINDOWS", True), self.isabs_313():
            inside, _resolved, reason = paths.check(share + r"\README.md", base=share, homes=[])
            outside, _resolved, denied = paths.check(r"\\attacker.invalid\other\x.txt", base=share, homes=[])
        self.assertIsNone(reason, inside)
        self.assertEqual(denied, paths.NETWORK_REASON, outside)


@unittest.skipUnless(os.name == "nt", "UNC and device paths are Windows paths")
class NetworkPathTests(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.opened: list[str] = []

        def missing(*args, **kwargs):
            raise FileNotFoundError("not opened in tests")

        # What each function returns for a network path instead of opening it, so a
        # regression shows up as a recorded call and never reaches the network.
        stand_ins = {"realpath": lambda value, *a, **k: value, "isfile": lambda *a, **k: False,
                     "isdir": lambda *a, **k: False, "exists": lambda *a, **k: False, "stat": missing,
                     "lstat": missing, "listdir": missing, "scandir": missing, "walk": lambda *a, **k: iter(()),
                     "iglob": lambda *a, **k: iter(()), "resolve": lambda self, *a, **k: self,
                     "is_dir": lambda *a, **k: False, "is_file": lambda *a, **k: False}

        def recorder(name, original):
            def wrapper(*args, **kwargs):
                network = [value for value in (*args, *kwargs.values()) if _network(value)]
                if network:
                    self.opened.append(f"{name}({os.fspath(network[0])})")
                    return stand_ins[name](*args, **kwargs)
                return original(*args, **kwargs)
            return wrapper

        targets = [(os.path, "realpath"), (os.path, "isfile"), (os.path, "isdir"), (os.path, "exists"),
                   (os, "stat"), (os, "lstat"), (os, "listdir"), (os, "scandir"), (os, "walk"), (glob, "iglob"),
                   (pathlib.Path, "resolve"), (pathlib.Path, "is_dir"), (pathlib.Path, "is_file"),
                   (pathlib.Path, "exists")]
        (self.ws / "README.md").write_text("public", encoding="utf-8")
        for owner, name in targets:
            patcher = mock.patch.object(owner, name, recorder(name, getattr(owner, name)))
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_agy_rules_deny_network_paths_without_opening_them(self) -> None:
        for value in SERVER_PATHS:
            for name, args in (
                ("view_file", {"AbsolutePath": value}),
                ("list_dir", {"DirectoryPath": value}),
                ("grep_search", {"SearchPath": value}),
                ("write_to_file", {"TargetFile": value, "CodeContent": "x"}),
                ("run_command", {"CommandLine": f"cat {value}", "Cwd": str(self.ws)}),
                ("run_command", {"CommandLine": f"Get-Content README.md,{value}", "Cwd": str(self.ws)}),
                ("run_command", {"CommandLine": f"Get-ChildItem -Path:{value}", "Cwd": str(self.ws)}),
                ("run_command", {"CommandLine": "dir", "Cwd": value}),
            ):
                for access, sandbox in (("read", False), ("edit", True), ("edit", False)):
                    with self.subTest(value=value, name=name, access=access, sandbox=sandbox):
                        self.assertEqual(self.decide(name, args, access=access, sandbox=sandbox).action, "deny")
        self.assertEqual(self.opened, [])

    def test_a_workspace_on_a_share_still_works(self) -> None:
        share = r"\\attacker.invalid\share\project"
        # Paths inside the user's own workspace may be opened: the user chose that folder.
        verdict = self.decide("view_file", {"AbsolutePath": share + r"\README.md"}, workspaces=[share])
        self.assertNotEqual(verdict.action, "deny")
        self.assertEqual(self.decide("view_file", {"AbsolutePath": r"\\attacker.invalid\other\x.txt"},
                                     workspaces=[share]).action, "deny")

    def test_opencode_gate_denies_network_paths_without_opening_them(self) -> None:
        path = self.root / "review-context.json"
        context.write_context(path, self.ctx(sandbox=False))
        for value in SERVER_PATHS:
            for tool, args in (
                ("read", {"filePath": value}),
                ("grep", {"pattern": "x", "path": value}),
                ("glob", {"pattern": "*", "path": value}),
                ("write", {"filePath": value, "content": "x"}),
                ("edit", {"filePath": value, "oldString": "a", "newString": "b"}),
                ("patch", {"patchText": f"*** Begin Patch\n*** Add File: {value}\n+x\n*** End Patch"}),
                ("shell", {"command": f"cat {value}"}),
                ("shell", {"command": "dir", "workdir": value}),
            ):
                with self.subTest(value=value, tool=tool):
                    answer = opencode_review.run(json.dumps({"tool": tool, "input": args}).encode(), path)
                    self.assertEqual(answer["decision"], "deny")
        self.assertEqual(self.opened, [])

    def test_claude_read_guard_denies_network_paths_without_opening_them(self) -> None:
        for value in SERVER_PATHS:
            for line in (f"Get-Content {value}", f"Get-Content README.md,{value}"):
                with self.subTest(line=line):
                    answer = claude_read_hook.validate({"tool_name": "PowerShell", "tool_input": {"command": line},
                                                        "cwd": str(self.ws)})
                    self.assertIsNotNone(answer)
                    self.assertEqual(answer["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(self.opened, [])


if __name__ == "__main__":
    unittest.main()
