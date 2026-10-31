"""The review gates decide on network and device paths without opening them.

Opening \\\\server\\share\\x on Windows (realpath, stat, listing) resolves the server
name and connects to it over SMB with the user's NTLM login. The gates run on the
host, outside every sandbox, and look at paths before they decide, so a denied call
could still leak credentials or data in a host name. The server below is in the
reserved .invalid domain; the recorders fail the test if anything opens it.
"""

from __future__ import annotations

import glob
import json
import os
import pathlib
import sys
import unittest
from unittest import mock

from test_agy_review import TempCase

from cli_subagents import claude_read_hook, opencode_review
from cli_subagents.agy_review import context

SERVER_PATHS = (
    r"\\attacker.invalid\share\x.txt",
    "//attacker.invalid/share/x.txt",
    r"\\?\UNC\attacker.invalid\share\x.txt",
    r"\\.\pipe\attacker",
    # Win32 hands \??\ paths to the NT namespace unchanged: \??\UNC\ reaches a server.
    r"\??\UNC\attacker.invalid\share\x.txt",
    "/??/UNC/attacker.invalid/share/x.txt",
)


def _network(value) -> bool:
    try:
        text = os.fspath(value)
    except TypeError:
        return False
    return isinstance(text, str) and os.path.normpath(text).startswith(("\\\\", "\\??\\"))


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
