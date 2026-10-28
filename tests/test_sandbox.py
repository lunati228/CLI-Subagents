"""csa's MXC sandbox: policy, decisions, runner integration and the OpenCode view.

Everything runs against tests/fake_wxc.py and tests/fake_opencode.py; no real
MXC, OpenCode or agent CLI starts here.
"""

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from fake_wxc import split_windows
from helpers import TempHome
from test_opencode import OpenCodeHome

from cli_subagents import jobs, messages, sandbox
from cli_subagents.config import SandboxSettings, dump_config, load_config
from cli_subagents.fsutil import read_json
from cli_subagents.sandbox import mxc, policy
from cli_subagents.sandbox.policy import PolicyError
from cli_subagents.sandbox.backends.mxc import Grants
from cli_subagents.sandbox.backends import mxc as mxc_backend

FAKE_WXC = Path(__file__).with_name("fake_wxc.py")
WXC_COMMAND = json.dumps([sys.executable, str(FAKE_WXC)])


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._temp.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self._env = patch.dict(os.environ, {"CLI_SUBAGENTS_HOME": str(self.root / "csa")})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._temp.cleanup()

    def request(self, grants=None, env=None, argv=None):
        grants = grants or Grants(workspaces=[str(self.project)])
        return mxc_backend.request(argv or [sys.executable, "-c", "print(1)"], str(self.project),
                              env if env is not None else {"PATH": os.environ.get("PATH", "")}, grants)

    def test_request_shape(self):
        data = self.request()
        self.assertEqual((data["version"], data["containment"]), ("1.0.0", "processcontainer"))
        self.assertEqual(data["fallback"], {"allowDaclMutation": False})
        self.assertEqual(data["ui"], {"disable": False, "clipboard": "none", "injection": False})
        self.assertEqual(data["network"], {"egress": {"default": "allow"}, "ingress": {"default": "deny", "hostLoopback": "deny"}})
        self.assertNotIn("telemetry", data)
        self.assertEqual(data["process"]["timeout"], 0)
        self.assertTrue(data["process"]["inheritDefaultEnv"])
        self.assertEqual(data["process"]["cwd"], str(self.project))
        self.assertEqual(data["filesystem"]["readwritePaths"], [str(self.project)])
        roots = data["processContainer"]["filesystem"]["enumeratePaths"]
        self.assertEqual(roots, [str(Path(self.project.anchor))])
        # Never a read grant on a drive root: on BaseContainer it exposes the whole drive.
        for path in data["filesystem"]["readonlyPaths"]:
            self.assertNotEqual(Path(path), Path(Path(path).anchor))

    def test_command_line_round_trips_through_windows_splitting(self):
        argv = [sys.executable, "plain", "with space", 'quote"inside', "trailing\\", "a\\\\b c", "", "%PATH%&|<>"]
        data = self.request(argv=argv)
        self.assertEqual(split_windows(data["process"]["commandLine"]), [str(Path(sys.executable)), *argv[1:]])

    def test_relative_program_is_resolved_or_refused(self):
        folder = self.root / "bin"
        folder.mkdir()
        program = folder / ("tool.exe" if os.name == "nt" else "tool")
        program.write_text("", encoding="utf-8")
        program.chmod(0o755)
        data = self.request(argv=["tool", "--x"], env={"PATH": str(folder), "PATHEXT": ".EXE"})
        self.assertEqual(split_windows(data["process"]["commandLine"])[0].lower(), str(program).lower())
        with self.assertRaises(PolicyError):
            self.request(argv=["no-such-tool-anywhere"], env={"PATH": str(folder)})

    def test_environment_entries(self):
        data = self.request(env={"PATH": "p", "TMPDIR": "/tmp", "=C:": "C:\\x", "GOOD": "a=b", "BAD\0": "x"})
        self.assertIn("PATH=p", data["process"]["env"])
        self.assertIn("GOOD=a=b", data["process"]["env"])
        self.assertFalse(any(entry.startswith(("TMPDIR=", "=C:", "BAD")) for entry in data["process"]["env"]))
        shown = mxc_backend.redacted(data)
        self.assertEqual(shown["process"]["env"], ["GOOD", "PATH"])
        self.assertEqual(data["process"]["env"][0].split("=", 1)[1] if data["process"]["env"] else "", "p")

    def test_workspace_protections(self):
        (self.project / ".git").mkdir()
        (self.project / ".vscode").mkdir()
        (self.project / "opencode.json").write_text("{}", encoding="utf-8")
        data = self.request()
        readonly = data["filesystem"]["readonlyPaths"]
        denied = data["filesystem"]["deniedPaths"]
        for name in (".git", ".vscode", "opencode.json"):
            self.assertIn(str(self.project / name), readonly)
        for name in (".claude", ".opencode", ".codex", ".agents", ".husky", "opencode.jsonc"):
            self.assertIn(str(self.project / name), denied)

    def test_worktree_git_file_grants_its_git_dirs_read_only(self):
        main = self.root / "main" / ".git"
        gitdir = main / "worktrees" / "wt"
        gitdir.mkdir(parents=True)
        (gitdir / "commondir").write_text("../..\n", encoding="utf-8")
        (self.project / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
        data = self.request()
        readonly = data["filesystem"]["readonlyPaths"]
        self.assertIn(str(self.project / ".git"), readonly)
        self.assertIn(str(gitdir.resolve()), readonly)
        self.assertIn(str(main.resolve()), readonly)

    def test_enclosing_repository_git_dir(self):
        (self.root / ".git").mkdir()
        sub = self.project / "sub"
        sub.mkdir()
        self.assertEqual(policy.enclosing_git_dir(str(sub)), [str((self.root / ".git").resolve())])

    def test_refuses_to_make_profile_settings_or_csa_writable(self):
        home = Path.home().resolve()
        for path in (Path(home.anchor), home, home / ".claude", self.root / "csa", self.root / "csa" / "jobs"):
            with self.subTest(path=path):
                with self.assertRaises(PolicyError):
                    policy.check_workspace(str(path))
        agent = self.root / "agent-home"
        agent.mkdir()
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(agent)}):
            with self.assertRaises(PolicyError):
                policy.check_workspace(str(agent / "skills"))
        if os.name == "nt":
            with self.assertRaises(PolicyError):
                policy.check_workspace(str(home / "AppData"))
        self.assertEqual(policy.check_workspace(str(self.project)), str(self.project))

    def test_read_grants_inside_workspaces_are_dropped_except_pinned(self):
        inner = self.project / "venv" / "Scripts"
        inner.mkdir(parents=True)
        pinned = self.project / "src" / "cli_subagents"
        pinned.mkdir(parents=True)
        outside = self.root / "tools"
        outside.mkdir()
        data = self.request(Grants(workspaces=[str(self.project)], readonly=[str(inner), str(outside)], pinned=[str(pinned)]))
        readonly = data["filesystem"]["readonlyPaths"]
        self.assertNotIn(str(inner), readonly)
        self.assertIn(str(outside), readonly)
        self.assertIn(str(pinned), readonly)
        with self.assertRaises(PolicyError):
            self.request(Grants(workspaces=[str(self.project)], readonly=[str(Path(self.project.anchor))]))

    def test_toolchain_dirs_skip_home_ancestors_windows_and_agent_homes(self):
        home = Path.home().resolve()
        tools = self.root / "tools"
        tools.mkdir()
        agent = self.root / "agent"
        (agent / "bin").mkdir(parents=True)
        path = os.pathsep.join([str(tools), str(home), str(Path(home.anchor)), str(self.root / "missing"), str(agent / "bin")])
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(agent)}):
            found = policy.toolchain_dirs({"PATH": path})
        self.assertIn(str(tools), found)
        for blocked in (str(home), str(Path(home.anchor)), str(self.root / "missing"), str(agent / "bin")):
            self.assertNotIn(blocked, found)
        if os.name == "nt":
            windows = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
            self.assertNotIn(str(windows), policy.toolchain_dirs({"PATH": str(windows)}))

    def test_credential_folders_are_hidden_unless_granted(self):
        data = self.request()
        self.assertIn(str((Path.home() / ".ssh").resolve()), data["filesystem"]["deniedPaths"])

    def test_launch_error_reads_mxc_envelope(self):
        stderr = ('The target executable exited with STATUS_DLL_INIT_FAILED.{"error":{"code":"backend_error",'
                  '"message":"The target executable exited with STATUS_DLL_INIT_FAILED (0xC0000142)."}}\n')
        self.assertIn("STATUS_DLL_INIT_FAILED", sandbox.launch_error(stderr))
        self.assertIsNone(sandbox.launch_error("plain worker failure\n"))


class DecisionTests(unittest.TestCase):
    class Sandboxed:
        sandbox_integration = "opencode"

    class Plain:
        pass

    def test_state(self):
        with patch.object(mxc, "support", return_value=(False, "old Windows")):
            self.assertEqual(sandbox.state(SandboxSettings())[0], "off")
            self.assertEqual(sandbox.state(SandboxSettings(enabled=True))[0], "unavailable")
        with patch.object(mxc, "support", return_value=(True, "build ok")):
            self.assertEqual(sandbox.state(SandboxSettings())[0], "on")
            self.assertEqual(sandbox.state(SandboxSettings(enabled=False))[0], "off")

    def test_for_job_applies_only_to_edit_commands_of_integrated_runtimes(self):
        with patch.dict(os.environ, {mxc.COMMAND_ENV: WXC_COMMAND}):
            self.assertIsNone(sandbox.for_job(SandboxSettings(), self.Plain(), "edit"))
            self.assertIsNone(sandbox.for_job(SandboxSettings(), self.Sandboxed(), "full"))
            self.assertIsNone(sandbox.for_job(SandboxSettings(enabled=False), self.Sandboxed(), "edit"))
            self.assertIsNone(sandbox.for_job(SandboxSettings(), self.Sandboxed(), "read"))
            box = sandbox.for_job(SandboxSettings(), self.Sandboxed(), "edit")
            self.assertEqual(box.probe["tier"], "base-container")

    def test_missing_install_and_bad_probe_fail_closed(self):
        with patch.dict(os.environ, {mxc.COMMAND_ENV: ""}), patch.object(mxc, "support", return_value=(True, "ok")):
            missing = Path(tempfile.gettempdir()) / "csa-no-such" / "wxc-exec.exe"
            with self.assertRaises(mxc.MxcError) as caught:
                sandbox.for_job(SandboxSettings(path=missing), self.Sandboxed(), "edit")
            self.assertIn("csa setup", str(caught.exception))
            self.assertIn("enabled = false", str(caught.exception))
        bad = json.dumps({"tier": "appcontainer-dacl", "needsDaclAugmentation": True, "probes": {}})
        with patch.dict(os.environ, {mxc.COMMAND_ENV: WXC_COMMAND, "FAKE_WXC_PROBE": bad}):
            with self.assertRaises(mxc.MxcError) as caught:
                sandbox.for_job(SandboxSettings(), self.Sandboxed(), "edit")
            self.assertIn("base-container", str(caught.exception))
            self.assertIn("DACL", str(caught.exception))
        with patch.dict(os.environ, {mxc.COMMAND_ENV: WXC_COMMAND, "FAKE_WXC_PROBE_EXIT": "3"}):
            with self.assertRaises(mxc.MxcError):
                sandbox.for_job(SandboxSettings(), self.Sandboxed(), "edit")

    def test_support_table(self):
        with patch.dict(os.environ, {mxc.COMMAND_ENV: ""}):
            if os.name != "nt":
                self.assertFalse(mxc.support()[0])
                return
            with patch.object(mxc, "windows_version", return_value=(22631, 4000)):
                self.assertFalse(mxc.support()[0])
            with patch.object(mxc, "windows_version", return_value=(26100, 9000)):
                self.assertFalse(mxc.support()[0])
            with patch.object(mxc, "windows_version", return_value=(26200, 9278)):
                self.assertTrue(mxc.support()[0])
            with patch.object(mxc, "windows_version", return_value=(26300, 9549)):
                self.assertFalse(mxc.support()[0])
            with patch.object(mxc, "windows_version", return_value=(27000, 1)):
                self.assertTrue(mxc.support()[0])


class ConfigTests(TempHome):
    def test_sandbox_and_reviewer_chain_round_trip_together(self):
        (self.home / "config.toml").write_text(
            '[sandbox]\nenabled = false\npath = "C:/tools/wxc-exec.exe"\n'
            '[[review.reviewers]]\nprovider = "codex"\nmodel = "review-model"\n', encoding="utf-8")
        config = load_config()
        (self.home / "config.toml").write_text(dump_config(config), encoding="utf-8")
        loaded = load_config()
        self.assertIs(loaded.sandbox.enabled, False)
        self.assertEqual(loaded.sandbox.path, Path("C:/tools/wxc-exec.exe"))
        self.assertEqual(loaded.reviewers, config.reviewers)
        for runtime in ("opencode", "antigravity"):
            chain, defaults = loaded.runtime(runtime).review_chain()
            self.assertFalse(defaults)
            self.assertEqual([(entry.provider, entry.model) for entry in chain], [("codex", "review-model")])

    def test_sandbox_table_round_trips_and_validates(self):
        (self.home / "config.toml").write_text('[sandbox]\nenabled = false\npath = "C:/tools/wxc-exec.exe"\n', encoding="utf-8")
        config = load_config()
        self.assertIs(config.sandbox.enabled, False)
        self.assertEqual(config.sandbox.path, Path("C:/tools/wxc-exec.exe"))
        (self.home / "config.toml").write_text(dump_config(config), encoding="utf-8")
        self.assertIs(load_config().sandbox.enabled, False)
        self.assertIsNone(load_config(self.root / "missing.toml").sandbox.enabled)
        for text in ('[sandbox]\nenabled = "false"\n', "[sandbox]\nenable = false\n", '[sandbox]\npath = ""\n'):
            with self.subTest(text=text):
                (self.home / "config.toml").write_text(text, encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_config()


class TaskkillTests(unittest.TestCase):
    def test_arguments(self):
        from cli_subagents.sandbox import taskkill

        self.assertEqual(taskkill._parse(["/pid", "12", "/T", "/F"]), ([12], [], True))
        self.assertEqual(taskkill._parse(["/F", "/IM", "node.exe"]), ([], ["node.exe"], False))
        self.assertIsInstance(taskkill._parse(["/S", "remote", "/PID", "1"]), str)
        self.assertIsInstance(taskkill._parse(["/PID", "x"]), str)
        self.assertIsInstance(taskkill._parse([]), str)

    @unittest.skipUnless(os.name == "nt", "Windows process API")
    def test_kills_a_process_tree(self):
        from cli_subagents.sandbox import taskkill

        script = "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); time.sleep(60)"
        parent = subprocess.Popen([sys.executable, "-c", script])
        try:
            time.sleep(1.0)
            children = [pid for pid, ppid, _ in taskkill._processes() if ppid == parent.pid]
            self.assertTrue(children)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(taskkill.main(["/PID", str(parent.pid), "/T", "/F"]), 0)
            parent.wait(timeout=10)
            alive = {pid for pid, _ppid, _name in taskkill._processes()}
            self.assertFalse(set(children) & alive)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(taskkill.main(["/PID", "999999999"]), taskkill.NOT_FOUND)
        finally:
            if parent.poll() is None:
                parent.kill()


class OpenCodeLaunchTests(OpenCodeHome):
    def test_host_runtime_and_custom_executor_cannot_be_changed_from_workspace(self):
        runtime = self.project / "host-python"
        runtime.mkdir()
        library = runtime / "site.py"
        library.write_text("# fixture")
        backend = self.project / "host-mxc"
        backend.mkdir()
        executable = backend / "wxc-exec.exe"
        executable.write_text("fixture")
        with patch.object(policy, "python_dirs", return_value=[str(runtime)]):
            launch = self.launch(access="edit", sandbox=True,
                                 sandbox_backend={"name": "mxc", "command": [str(executable)]})
        _, settings = self.plugin_settings(launch)
        context = read_json(Path(settings["context"]))
        self.assertIn(str(runtime), context["command_sandbox"]["policy"]["pinned"])
        self.assertIn(str(backend), context["command_sandbox"]["policy"]["pinned"])
        from cli_subagents import opencode_review
        for target in (library, executable):
            answer = opencode_review.run(json.dumps({"tool": "write", "input": {"path": str(target)}}).encode(),
                                         Path(settings["context"]))
            self.assertEqual(answer["decision"], "deny")

    def test_command_sandbox_keeps_native_state_and_host_gate(self):
        instruction = self.root / "parent.md"
        instruction.write_text("rules", encoding="utf-8")
        original = {key: os.environ[f"XDG_{key}_HOME"] for key in ("DATA", "CONFIG", "STATE", "CACHE")}
        launch = self.launch(access="edit", parent={"instructions": [str(instruction)]}, sandbox=True)
        data, settings = self.plugin_settings(launch)
        self.assertTrue(settings["sandbox"])
        for key, value in original.items():
            self.assertEqual(launch.env[f"XDG_{key}_HOME"], value)
        self.assertIn(str(instruction), data["instructions"])
        context = read_json(Path(settings["context"]))
        self.assertEqual(context["sandbox_mode"], "command")
        self.assertFalse(context["sandbox_network"])
        self.assertEqual(Path(context["log"]).parent.name, "test-opencode")
        self.assertEqual(context["reviewers"][0]["kind"], "claude")
        self.assertEqual(context["review_budget"], 240)
        self.assertEqual(context["command_sandbox"]["policy"]["workspaces"], [str(self.project)])
        self.assertIn(str(self.home), context["command_sandbox"]["policy"]["denied"])
        self.assertFalse((self.home / "state" / "opencode-sandbox").exists())
        self.assertFalse((Path(settings["context"]).parent / "sandbox-view").exists())
        self.assertIn("shell commands run in csa's OS sandbox", launch.stdin)

    def test_unsandboxed_launch_is_unchanged(self):
        launch = self.launch(access="edit")
        data, settings = self.plugin_settings(launch)
        context = read_json(Path(settings["context"]))
        self.assertFalse(settings["sandbox"])
        self.assertFalse(context["sandbox"])
        self.assertNotIn("command_sandbox", context)
        self.assertIn("fixed rules first, then reviewer", launch.stdin)


class RunnerTests(OpenCodeHome):
    def setUp(self):
        super().setUp()
        self.wxc_record = self.root / "wxc.jsonl"
        os.environ[mxc.COMMAND_ENV] = WXC_COMMAND
        os.environ["FAKE_WXC_RECORD"] = str(self.wxc_record)
        for name in ("FAKE_WXC_FAIL", "FAKE_WXC_PROBE", "FAKE_WXC_PROBE_EXIT"):
            os.environ.pop(name, None)
        (self.project / "test_fixture.py").write_text(
            'import unittest\nclass Fixture(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\n', encoding="utf-8")

    def sandbox_config(self, sandbox_text=""):
        text = (self.home / "config.toml").read_text(encoding="utf-8").split("\n[sandbox]")[0]
        (self.home / "config.toml").write_text(text + ("\n[sandbox]\n" + sandbox_text if sandbox_text else ""), encoding="utf-8")

    def run_job(self, access="read", runtime="opencode"):
        status = jobs.create_job(runtime=runtime, prompt="look around", cwd=str(self.project), access=access)
        status = jobs.wait_for(status["job_id"], 90)
        self.assertEqual(status["state"], "done", status)
        return status

    def wxc_runs(self):
        if not self.wxc_record.exists():
            return []
        return [json.loads(line) for line in self.wxc_record.read_text(encoding="utf-8").splitlines() if line.strip()]

    def worker_runs(self):
        return [record for record in self.records() if record.get("argv", [None])[0] == "run"]

    def test_read_job_keeps_opencode_outside_and_runs_no_containers(self):
        status = self.run_job("read")
        self.assertEqual(status["outcome"], "success", status)
        self.assertNotIn("sandbox_scope", status["attempts"][0])
        self.assertEqual(self.wxc_runs(), [])
        folder = self.home / "jobs" / status["job_id"]
        context = read_json(folder / "review-context.json")
        self.assertFalse(context["sandbox"])
        self.assertFalse((folder / "sandbox-bin").exists())
        [worker] = self.worker_runs()
        self.assertEqual(worker["env"]["XDG_DATA_HOME"], os.environ["XDG_DATA_HOME"])

    def test_edit_command_uses_one_container_and_host_review_log(self):
        os.environ["FAKE_OPENCODE_SCENARIO"] = "sandbox_command"
        status = self.run_job("edit")
        self.assertEqual(status["outcome"], "success", status)
        [run] = self.wxc_runs()
        self.assertEqual(run["policy"]["network"]["egress"]["default"], "deny")
        self.assertEqual(run["policy"]["filesystem"]["readwritePaths"], [str(self.project)])
        folder = self.home / "jobs" / status["job_id"]
        self.assertTrue((folder / "review.jsonl").is_file())
        self.assertFalse((folder / "gate").exists())
        self.assertEqual(list((folder / "sandbox").glob("*.json*")), [])

    def test_full_jobs_and_other_runtimes_run_without_it(self):
        status = self.run_job("full")
        self.assertEqual(status["outcome"], "success", status)
        self.assertNotIn("sandbox", status["attempts"][0])
        self.assertEqual(self.wxc_runs(), [])

    def test_disabled_sandbox_runs_without_it(self):
        self.sandbox_config("enabled = false\n")
        status = self.run_job("edit")
        self.assertEqual(status["outcome"], "success", status)
        self.assertEqual(self.wxc_runs(), [])

    def test_missing_mxc_fails_closed(self):
        os.environ.pop(mxc.COMMAND_ENV)
        self.sandbox_config(f"enabled = true\npath = {json.dumps(str(self.root / 'nowhere' / 'wxc-exec.exe'))}\n")
        status = self.run_job("edit")
        self.assertEqual(status["outcome"], "error", status)
        self.assertTrue(status["message"].startswith("sandbox:"), status["message"])
        self.assertEqual(self.worker_runs(), [])

    def test_bad_probe_fails_closed(self):
        os.environ["FAKE_WXC_PROBE"] = json.dumps({"tier": "appcontainer", "needsDaclAugmentation": False, "probes": {}})
        status = self.run_job("edit")
        self.assertEqual(status["outcome"], "error", status)
        self.assertIn("base-container", status["message"])
        self.assertEqual(self.worker_runs(), [])

    def test_mxc_launch_error_is_reported(self):
        os.environ["FAKE_WXC_FAIL"] = "The target executable exited with STATUS_DLL_INIT_FAILED (0xC0000142)."
        os.environ["FAKE_OPENCODE_SCENARIO"] = "sandbox_command"
        status = self.run_job("edit")
        self.assertEqual(status["outcome"], "error", status)
        self.assertIn("STATUS_DLL_INIT_FAILED", (self.home / "jobs" / status["job_id"] / "stderr-1.log").read_text(encoding="utf-8"))

    def test_workspace_that_would_expose_csa_is_refused(self):
        self.project = self.home / "jobs"
        self.project.mkdir(exist_ok=True)
        status = self.run_job("edit")
        self.assertEqual(status["outcome"], "error", status)
        self.assertIn("won't make", status["message"])
        self.assertEqual(self.worker_runs(), [])

    def test_follow_shows_review_lines_from_the_host_gate(self):
        from cli_subagents import follow

        os.environ["FAKE_OPENCODE_SCENARIO"] = "denied_command"
        status = self.run_job("edit")
        lines = []
        with patch.object(follow.Follower, "emit_worker", lambda self, text: lines.append(text)):
            follower = follow.Follower(status["job_id"], None, 1)
            follower.review_lines()
        self.assertTrue(any(line.startswith("review: denied") for line in lines), lines)


class DocumentationTests(unittest.TestCase):
    def test_sandbox_is_documented_where_users_and_agents_look(self):
        root = Path(__file__).resolve().parents[1]
        for relative, phrases in (
            ("README.md", ("## Sandbox", "docs/sandbox.md", "--no-sandbox", "enabled = false")),
            ("AGENTS.md", ("docs/sandbox.md", "--no-sandbox", "src/cli_subagents/sandbox/")),
            ("docs/configuration.md", ("## Sandbox", "[sandbox]", "enabled", "path", "CLI_SUBAGENTS_SANDBOX_COMMAND")),
            ("docs/sandbox.md", ("enabled = false", "--no-sandbox", "microsoft/mxc#1061", "26100.9278", "taskkill")),
            ("docs/opencode.md", ("sandbox.md", '"sandbox": true')),
        ):
            text = (root / relative).read_text(encoding="utf-8")
            for phrase in phrases:
                with self.subTest(path=relative, phrase=phrase):
                    self.assertIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
