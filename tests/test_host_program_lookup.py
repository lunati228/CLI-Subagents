"""Host-side program lookups never use the current folder (a workspace a sandboxed command can write)."""

import os
from pathlib import Path
import stat
import subprocess
import sys
import unittest
from unittest.mock import patch

from helpers import TempHome
from cli_subagents import fsutil
from cli_subagents.config import load_config
from cli_subagents.runtimes import get_runtime
from cli_subagents.sandbox import policy


def plant(folder: Path, name: str) -> Path:
    """An executable `name` in `folder`: a .cmd on Windows, a shell script elsewhere."""

    path = folder / (name + ".cmd" if os.name == "nt" else name)
    path.write_text("@echo planted\r\n" if os.name == "nt" else "#!/bin/sh\necho planted\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class HostProgramLookupTests(TempHome):
    def setUp(self):
        super().setUp()
        self.cwd = os.getcwd()
        self.addCleanup(os.chdir, self.cwd)
        os.chdir(self.project)
        variables = {key: value for key, value in os.environ.items() if key.upper() != "NODEFAULTCURRENTDIRECTORYINEXEPATH"}
        environment = patch.dict(os.environ, variables, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def test_a_program_in_the_current_folder_is_never_found(self):
        plant(self.project, "csa-fixture-tool")
        tools = self.root / "tools"
        tools.mkdir()
        os.environ["PATH"] = os.pathsep.join([".", "", "relative-tools"])
        self.assertIsNone(fsutil.find_program("csa-fixture-tool"))
        expected = plant(tools, "csa-fixture-tool")
        os.environ["PATH"] = os.pathsep.join([".", f'"{tools}"'])
        self.assertEqual(Path(fsutil.find_program("csa-fixture-tool")), expected)
        self.assertEqual(Path(fsutil.find_program("csa-fixture-tool", str(tools))), expected)

    def test_runtimes_ignore_a_shim_planted_in_the_current_folder(self):
        for name, variable in (("opencode", "CLI_SUBAGENTS_OPENCODE_COMMAND"), ("claude", "CLI_SUBAGENTS_CLAUDE_COMMAND"),
                               ("codex", "CLI_SUBAGENTS_CODEX_COMMAND"), ("agy", "CLI_SUBAGENTS_AGY_COMMAND")):
            plant(self.project, name)
            os.environ.pop(variable, None)
        os.environ["PATH"] = str(self.root / "empty")
        os.environ.pop("AGY_CMD", None)
        os.environ["LOCALAPPDATA"] = str(self.root / "no-local-app-data")
        config = load_config()
        with patch.object(Path, "home", return_value=self.root / "no-home"), \
                patch.dict(os.environ, {"CLAUDE_CODE_EXECPATH": ""}):
            for runtime in ("opencode", "claude", "codex", "antigravity"):
                with self.subTest(runtime=runtime):
                    found = get_runtime(runtime).find_binary(config.runtime(runtime))
                    self.assertFalse(found and any(str(self.project) in str(Path(item).resolve()) for item in found), found)

    @unittest.skipUnless(os.name == "nt", "Windows process lookup")
    def test_kill_tree_runs_the_system_taskkill(self):
        with patch.object(fsutil.subprocess, "run") as run:
            fsutil.kill_tree(12345)
        program = run.call_args.args[0][0]
        self.assertTrue(os.path.isabs(program), program)
        self.assertEqual(Path(program).name.lower(), "taskkill.exe")
        self.assertEqual(Path(program).parent.name.lower(), "system32")

    @unittest.skipUnless(os.name == "nt", "Windows process lookup")
    def test_opencode_host_environment_never_searches_the_workspace_for_programs(self):
        env = policy.host_environment({"PATH": str(self.root), "nodefaultcurrentdirectoryinexepath": ""}, [str(self.project)])
        self.assertEqual([key for key in env if key.upper() == "NODEFAULTCURRENTDIRECTORYINEXEPATH"],
                         ["NoDefaultCurrentDirectoryInExePath"])
        self.assertEqual(env["NoDefaultCurrentDirectoryInExePath"], "1")
        planted = self.project / "csa-fixture-probe.exe"
        planted.write_bytes(Path(sys.executable).read_bytes())
        try:
            done = subprocess.run(["csa-fixture-probe", "-c", "print('planted')"], cwd=self.project, env=env,
                                  capture_output=True, text=True, timeout=30)
        except FileNotFoundError:
            return  # CreateProcess skipped the current folder
        self.assertNotIn("planted", done.stdout)


if __name__ == "__main__":
    unittest.main()
