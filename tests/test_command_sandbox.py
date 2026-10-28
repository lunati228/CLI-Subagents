"""Each layer uses fake wxc/CLIs; these tests never launch a real sandbox."""

import base64
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from helpers import TempHome
from test_opencode import OpenCodeHome
from test_opencode_reviewer import GateBase
import test_opencode_reviewer as reviewer_tests
from cli_subagents import opencode_review
from cli_subagents.agy_review import paths, reviewer
from cli_subagents.sandbox import policy
from cli_subagents.sandbox.backends import get_backend
from cli_subagents.sandbox.integrations import opencode
from cli_subagents.sandbox.model import Command, Network, Policy, PolicyError, SandboxError
from cli_subagents.sandbox import launcher

FAKE = Path(__file__).with_name("fake_wxc.py")
ENTRY = Path(launcher.__file__).with_name("__main__.py")


class ModelAndBackendTests(TempHome):
    def test_nested_repositories_and_worktree_targets_are_read_only(self):
        nested = self.project / "vendor" / "lib"
        (nested / ".git").mkdir(parents=True)
        linked = self.project / "submodule"
        linked.mkdir()
        gitdir = self.project / "metadata" / "module"
        gitdir.mkdir(parents=True)
        common = self.project / "common"
        common.mkdir()
        (gitdir / "commondir").write_text("../../common", encoding="utf-8")
        (linked / ".git").write_text("gitdir: ../metadata/module", encoding="utf-8")
        value = policy.command_policy([str(self.project)], {})
        data = get_backend("mxc").request(value, Command([sys.executable], str(self.project), {}))
        for target in (nested / ".git", linked / ".git", gitdir, common):
            self.assertIn(str(target), data["filesystem"]["readonlyPaths"])

    def test_scan_skips_in_workspace_reparse_points_on_python_311(self):
        from types import SimpleNamespace
        link = self.project / "loop"
        link.mkdir()
        original = os.lstat
        walked = []

        def attributes(path, *args, **kwargs):
            if Path(path) == link:
                return SimpleNamespace(st_file_attributes=0x400)
            return original(path, *args, **kwargs)

        def walk(root, **kwargs):
            dirs = ["loop"]
            yield str(self.project), dirs, []
            if dirs:
                walked.append("followed junction")

        with patch.object(os, "lstat", side_effect=attributes), patch.object(os, "walk", side_effect=walk):
            policy.command_policy([str(self.project)], {})
        self.assertEqual(walked, [])

    def test_unreadable_inherited_tool_directory_is_skipped(self):
        blocked = self.root / "inaccessible-tools"
        original = Path.is_dir

        def is_dir(path):
            if path == blocked:
                raise PermissionError("fixture inaccessible host folder")
            return original(path)

        with patch.object(Path, "is_dir", is_dir):
            self.assertNotIn(str(blocked), policy.toolchain_dirs({"PSModulePath": str(blocked)}))

    def test_host_path_excludes_workspace_junctions_even_when_the_target_is_outside(self):
        outside = self.root / "trusted-tools"
        outside.mkdir()
        link = self.project / "linked-tools"
        original = policy._real

        def resolved(path):
            return str(outside) if str(path) == str(link) else original(path)

        with patch.object(policy, "_real", side_effect=resolved):
            env = policy.host_environment({"PATH": os.pathsep.join([str(link), str(outside)])}, [str(self.project)])
        self.assertEqual(env["PATH"], str(outside))

    def test_workspace_scan_errors_fail_closed(self):
        def walk(root, **kwargs):
            kwargs["onerror"](PermissionError("fixture unreadable workspace"))
            return iter(())

        with patch.object(os, "walk", side_effect=walk), self.assertRaisesRegex(PolicyError, "cannot scan workspace"):
            policy.command_policy([str(self.project)], {})

    @unittest.skipUnless(os.name == "nt", "case-insensitive Windows protected names")
    def test_nested_git_marker_case_does_not_bypass_protection(self):
        marker = self.project / "nested" / ".GIT"
        marker.mkdir(parents=True)
        value = policy.command_policy([str(self.project)], {})
        self.assertIn(str(marker), value.pinned)

    def test_policy_and_command_round_trip_without_backend_fields(self):
        value = Policy([str(self.project)], protected_names=[".git"])
        self.assertEqual(Policy.from_dict(value.to_dict()), value)
        command = Command([sys.executable, "-c", "pass"], str(self.project), {})
        self.assertEqual(Command.from_dict(command.to_dict()), command)
        for changed in ({"timeout": True}, {"timeout": float("nan")}, {"network": "unknown"}, {"argv": []}):
            with self.subTest(changed=changed), self.assertRaises(PolicyError):
                Command.from_dict({**command.to_dict(), **changed})
        with self.assertRaises(PolicyError):
            Policy.from_dict({**value.to_dict(), "readwrite": [str(self.root)]})

    def test_network_classes_are_explicit_and_proxy_is_refused(self):
        adapter = get_backend("mxc")
        value = Policy([str(self.project)])
        for network, egress, loopback in ((Network.NONE, "deny", "deny"), (Network.LOOPBACK, "deny", "allow"),
                                           (Network.EGRESS, "allow", "allow")):
            with self.subTest(network=network):
                data = adapter.request(value, Command([sys.executable], str(self.project), {}, network))
                self.assertEqual(data["network"], {"egress": {"default": egress},
                                                   "ingress": {"default": "deny", "hostLoopback": loopback}})
                self.assertFalse(data["process"]["inheritDefaultEnv"])
                self.assertEqual(data["filesystem"]["readwritePaths"], [str(self.project)])
        with self.assertRaises(PolicyError):
            adapter.request(value, Command([sys.executable], str(self.project), {}, Network.PROXY))
        with self.assertRaises(PolicyError):
            adapter.request(value, Command([sys.executable], str(self.root), {}))
        with self.assertRaises(SandboxError):
            get_backend("unknown")

    def test_missing_loopback_capability_is_not_reported_ready(self):
        from fake_wxc import GOOD_PROBE
        from cli_subagents.config import SandboxSettings
        from cli_subagents.sandbox import mxc

        probe = {**GOOD_PROBE, "probes": {**GOOD_PROBE["probes"], "baseContainerSupportsIngressHostLoopbackAllow": False}}
        with patch.dict(os.environ, {mxc.COMMAND_ENV: json.dumps([sys.executable, str(FAKE)])}), \
                patch.object(mxc, "probe", return_value=probe), self.assertRaises(SandboxError):
            get_backend("mxc").ready(SandboxSettings())

    def test_secret_files_and_agent_state_are_denied_without_reading_contents(self):
        (self.project / ".env").write_text("FIXTURE=1")
        nested = self.project / "nested"
        nested.mkdir()
        (nested / "secret.key").write_text("fixture")
        value = policy.command_policy([str(self.project)], dict(os.environ))
        self.assertIn(str(self.project / ".env"), value.denied)
        self.assertIn(str(nested / "secret.key"), value.denied)
        self.assertIn(str(self.home), value.denied)
        relatives = [".local/share/opencode/auth.json", ".config/opencode/service.json"]
        if os.name == "nt":  # OpenCode's AppData locations exist only on Windows
            relatives += ["AppData/Local/opencode/auth.json", "AppData/Roaming/opencode/service.json"]
        for relative in relatives:
            self.assertIsNotNone(paths.secret_reason(str(self.root / relative), [str(self.root)]))

    def test_missing_secret_parents_are_denied_at_the_first_missing_component(self):
        value = Policy([str(self.project)], denied=[str(self.root / "missing" / "nested" / "auth.json")])
        data = get_backend("mxc").request(value, Command([sys.executable], str(self.project), {}))
        self.assertIn(str(self.root / "missing"), data["filesystem"]["deniedPaths"])

    def test_host_parent_channel_cannot_import_a_workspace_package(self):
        from cli_subagents.runtimes.base import parent_server

        planted = self.project / "cli_subagents"
        planted.mkdir()
        marker = self.project / "host-imported"
        (planted / "__init__.py").write_text(f'from pathlib import Path; Path({str(marker)!r}).touch()\nraise RuntimeError("planted")')
        server = parent_server("fixture")
        env = {**os.environ, **server["env"], "PYTHONPATH": str(self.project)}
        done = subprocess.run([server["command"], *server["args"], "--help"], cwd=self.project,
                              env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse(marker.exists())


class LauncherTests(TempHome):
    def setUp(self):
        super().setUp()
        self.record = self.root / "wxc.jsonl"
        self.backend = {"name": "mxc", "command": [sys.executable, str(FAKE)]}
        self.folder = self.home / "requests"
        self.folder.mkdir()
        os.environ["FAKE_WXC_RECORD"] = str(self.record)

    def request(self, code, *, timeout=0, filesystem=None):
        request = self.folder / "command.json"
        command = Command([sys.executable, "-c", code], str(self.project), dict(os.environ), timeout=timeout)
        request.write_text(json.dumps({"version": 1, "policy": (filesystem or Policy([str(self.project)])).to_dict(),
                                       "command": command.to_dict(), "backend": self.backend}))
        return request

    def run_request(self, request, parent=None):
        return subprocess.run([sys.executable, "-I", str(ENTRY), "--request", str(request), "--parent-pid",
                               str(parent or os.getpid())], input="hello", capture_output=True, text=True, timeout=20)

    def test_stdio_exit_code_cleanup_and_one_use(self):
        request = self.request('import sys; print(sys.stdin.read()); sys.stderr.write("err"); sys.exit(7)')
        got = self.run_request(request)
        self.assertEqual((got.returncode, got.stdout.strip(), got.stderr), (7, "hello", "err"))
        self.assertEqual(list(self.folder.iterdir()), [])
        replay = self.run_request(request)
        self.assertEqual(replay.returncode, 125)
        self.assertIn("already consumed", replay.stderr)

    def test_backend_failure_never_runs_the_command(self):
        marker = self.project / "ran"
        request = self.request(f'from pathlib import Path; Path({str(marker)!r}).touch()')
        with patch.dict(os.environ, {"FAKE_WXC_FAIL": "fixture engine failure"}):
            got = self.run_request(request)
        self.assertNotEqual(got.returncode, 0)
        self.assertFalse(marker.exists())
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_backend_owns_its_policy_format_and_executor_arguments(self):
        from cli_subagents.sandbox.model import BackendRequest

        marker = self.project / "adapter-ran"
        expected_policy = "non-JSON backend policy\n"

        class DifferentBackend:
            def prepare(self, filesystem, command, executable, policy_path):
                code = ("from pathlib import Path; import sys; "
                        f"assert Path(sys.argv[1]).read_text() == {expected_policy!r}; "
                        f"Path({str(marker)!r}).touch()")
                return BackendRequest([*executable, "-c", code, str(policy_path)], expected_policy)

        with patch.object(launcher, "get_backend", return_value=DifferentBackend()):
            result = launcher.execute(Policy([str(self.project)]),
                                      Command(["unused-command"], str(self.project), {}),
                                      {"name": "different", "command": [sys.executable]}, self.folder)
        self.assertEqual(result, 0)
        self.assertTrue(marker.exists())
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_timeout_ends_child_and_removes_policies(self):
        request = self.request('import time; time.sleep(60)', timeout=.2)
        got = self.run_request(request)
        self.assertEqual(got.returncode, 124, got.stderr)
        self.assertEqual(list(self.folder.iterdir()), [])

    def plant(self, target):
        staging = target.with_name("staging")
        return (f"from pathlib import Path; p=Path({str(staging)!r}); p.mkdir(parents=True); "
                f"(p/'payload').write_text('preserved'); p.rename({str(target)!r})")

    def assert_quarantined(self, target, got):
        self.assertEqual(got.returncode, 125, got.stderr)
        self.assertIn(str(target), got.stderr)
        self.assertIn("quarantin", got.stderr)
        self.assertFalse(os.path.lexists(target))
        [saved] = target.parent.glob(target.name + ".csa-quarantine-*")
        self.assertEqual((saved / "item" / "payload").read_text(), "preserved")
        self.assertIn(str(saved), got.stderr)
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_rename_onto_each_protected_name_fails_and_preserves_the_item(self):
        # The backend's default protections apply even without protected_names.
        for name in policy.PROTECTED_NAMES:
            with self.subTest(name=name):
                target = self.project / name
                got = self.run_request(self.request(self.plant(target)))
                self.assert_quarantined(target, got)

    def test_new_protected_file_overrides_an_unsuccessful_command(self):
        target = self.project / "opencode.json"
        code = ("from pathlib import Path; import sys; "
                f"p=Path({str(self.project / 'staging')!r}); p.write_text('preserved'); "
                f"p.rename({str(target)!r}); sys.exit(7)")
        got = self.run_request(self.request(code))
        self.assertEqual(got.returncode, 125, got.stderr)
        self.assertFalse(target.exists())
        [saved] = self.project.glob("opencode.json.csa-quarantine-*")
        self.assertEqual((saved / "item").read_text(), "preserved")

    def test_existing_protected_directory_and_file_are_left_untouched(self):
        folder = self.project / ".claude"
        folder.mkdir()
        (folder / "settings.json").write_text("original")
        marker = self.project / ".git"
        marker.write_text("gitdir: fixture")
        got = self.run_request(self.request("pass"))
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual((folder / "settings.json").read_text(), "original")
        self.assertEqual(marker.read_text(), "gitdir: fixture")
        self.assertEqual(list(self.project.glob("*.csa-quarantine-*")), [])

    def test_snapshot_is_taken_at_launch_even_if_the_request_says_missing(self):
        target = self.project / ".agents"
        request = self.request("pass", filesystem=Policy([str(self.project)], denied=[str(target)]))
        target.mkdir()
        (target / "original").touch()
        got = self.run_request(request)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertTrue((target / "original").exists())
        self.assertEqual(list(self.project.glob("*.csa-quarantine-*")), [])

    def test_all_writable_roots_and_policy_specific_names_are_checked(self):
        extra = self.root / "additional"
        extra.mkdir()
        targets = [self.project / ".claude", extra / ".settings"]
        filesystem = Policy([str(self.project), str(extra), str(self.project)], protected_names=[".settings"])
        got = self.run_request(self.request("; ".join(self.plant(p) for p in targets), filesystem=filesystem))
        for target in targets:
            self.assert_quarantined(target, got)

    def test_explicit_deeper_protected_paths_are_checked_without_a_tree_walk(self):
        for field in ("readonly", "denied", "pinned"):
            with self.subTest(field=field):
                target = self.project / field / ".claude"
                # The policy can mark a child of the protected directory.
                filesystem = Policy([str(self.project)], **{field: [str(target / "settings.json")]})
                got = self.run_request(self.request(self.plant(target), filesystem=filesystem))
                self.assert_quarantined(target, got)

    def directory_link_code(self, link, target):
        if os.name == "nt":
            argv = ["cmd", "/d", "/c", "mklink", "/J", str(link), str(target)]
            return f"import subprocess; subprocess.run({argv!r}, check=True, stdout=subprocess.DEVNULL)"
        return f"from pathlib import Path; Path({str(link)!r}).symlink_to({str(target)!r}, target_is_directory=True)"

    def test_a_new_protected_link_is_moved_without_touching_its_target(self):
        outside = self.root / "link-target"
        outside.mkdir()
        (outside / "payload").write_text("preserved")
        target = self.project / ".claude"
        staging = self.project / "staging"
        code = self.directory_link_code(staging, outside) + f"; from pathlib import Path; Path({str(staging)!r}).rename({str(target)!r})"
        self.assert_quarantined(target, self.run_request(self.request(code)))
        [saved] = self.project.glob(".claude.csa-quarantine-*")
        self.assertEqual((saved / "item").resolve(), outside)
        self.assertEqual((outside / "payload").read_text(), "preserved")

    def test_an_existing_dangling_protected_link_is_left_untouched(self):
        target = self.project / ".claude"
        code = self.directory_link_code(target, self.root / "missing-target")
        subprocess.run([sys.executable, "-c", code], check=True, capture_output=True)
        got = self.run_request(self.request("pass"))
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertTrue(os.path.lexists(target))
        self.assertEqual(list(self.project.glob("*.csa-quarantine-*")), [])

    def test_a_new_nested_junction_cannot_move_an_item_outside_the_workspace(self):
        outside = self.root / "outside"
        outside.mkdir()
        protected = outside / ".claude"
        protected.mkdir()
        (protected / "original").touch()
        link = self.project / "nested"
        filesystem = Policy([str(self.project)], denied=[str(link / ".claude")])
        request = self.request(self.directory_link_code(link, outside), filesystem=filesystem)
        got = self.run_request(request)
        self.assertEqual(got.returncode, 125, got.stderr)
        self.assertIn("outside the writable root", got.stderr)
        self.assertTrue((protected / "original").exists())
        self.assertEqual(list(outside.glob("*.csa-quarantine-*")), [])
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_snapshot_errors_fail_before_starting_the_command(self):
        target = self.project / "ran"
        with patch.object(launcher, "_entry_exists", side_effect=PermissionError("fixture access error")), \
                self.assertRaisesRegex(SandboxError, "cannot snapshot protected path"):
            launcher.execute(Policy([str(self.project)]),
                             Command([sys.executable, "-c", f"from pathlib import Path; Path({str(target)!r}).touch()"],
                                     str(self.project), dict(os.environ)), self.backend, self.folder)
        self.assertFalse(target.exists())
        self.assertFalse(self.record.exists())
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_a_quarantine_error_fails_closed_and_still_moves_other_new_items(self):
        targets = [self.project / ".claude", self.project / ".codex"]
        rename = Path.rename

        def refuse_one(path, destination):
            if path == targets[0]:
                raise PermissionError("fixture locked path")
            return rename(path, destination)

        command = Command([sys.executable, "-c", "; ".join(self.plant(p) for p in targets)],
                          str(self.project), dict(os.environ))
        with patch.object(Path, "rename", autospec=True, side_effect=refuse_one), \
                self.assertRaisesRegex(SandboxError, "could not quarantine") as error:
            launcher.execute(Policy([str(self.project)]), command, self.backend, self.folder)
        self.assertEqual((targets[0] / "payload").read_text(), "preserved")
        self.assertIn("fixture locked path", str(error.exception))
        self.assertIn("move it aside", str(error.exception))
        self.assert_quarantined(targets[1], subprocess.CompletedProcess([], 125, "", str(error.exception)))

    def test_new_protected_paths_are_quarantined_on_timeout(self):
        target = self.project / ".codex"
        request = self.request(self.plant(target) + "; import time; time.sleep(60)", timeout=2)
        self.assert_quarantined(target, self.run_request(request))

    def test_new_protected_paths_are_quarantined_when_the_executor_is_killed(self):
        target = self.project / ".opencode"
        # Kill only the fake wxc, leaving its command for worker_process to stop.
        code = self.plant(target) + "; import os, signal, time; os.kill(os.getppid(), signal.SIGTERM); time.sleep(60)"
        self.assert_quarantined(target, self.run_request(self.request(code)))

    @unittest.skipUnless(os.name == "nt", "Windows parent lifetime and kill-on-close job")
    def test_new_protected_paths_are_quarantined_on_parent_death(self):
        target = self.project / ".git"
        request = self.request(self.plant(target) + "; import time; time.sleep(60)")
        parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        process = subprocess.Popen([sys.executable, "-I", str(ENTRY), "--request", str(request),
                                    "--parent-pid", str(parent.pid)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True)
        try:
            deadline = time.monotonic() + 10
            while not target.exists() and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue(target.exists())
            parent.kill(); parent.wait(timeout=5)
            stdout, stderr = process.communicate(timeout=10)
            self.assert_quarantined(target, subprocess.CompletedProcess([], process.returncode, stdout, stderr))
        finally:
            if parent.poll() is None:
                parent.kill(); parent.wait()
            if process.poll() is None:
                process.kill(); process.communicate(timeout=10)

    @unittest.skipUnless(os.name == "nt", "Windows parent lifetime and kill-on-close job")
    def test_parent_death_ends_descendants(self):
        marker = self.project / "beat"
        code = f'import time; from pathlib import Path; p=Path({str(marker)!r});\nwhile True:\n p.write_text(str(time.time())); time.sleep(.05)'
        request = self.request(code)
        parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        process = subprocess.Popen([sys.executable, "-I", str(ENTRY), "--request", str(request),
                                    "--parent-pid", str(parent.pid)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 10
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue(marker.exists())
            parent.kill(); parent.wait(timeout=5)
            process.communicate(timeout=10)
            self.assertEqual(process.returncode, 130)
            first = marker.read_text()
            time.sleep(.2)
            self.assertEqual(marker.read_text(), first)
            self.assertEqual(list(self.folder.iterdir()), [])
        finally:
            if parent.poll() is None:
                parent.kill(); parent.wait()
            if process.poll() is None:
                process.kill(); process.communicate(timeout=10)

    @unittest.skipUnless(os.name == "nt", "Windows kills the tree when the launcher's job handle closes")
    def test_killing_launcher_also_kills_its_contained_descendants(self):
        marker = self.project / "beat"
        code = f'import time; from pathlib import Path; p=Path({str(marker)!r});\nwhile True:\n p.write_text(str(time.time())); time.sleep(.05)'
        request = self.request(code)
        process = subprocess.Popen([sys.executable, "-I", str(ENTRY), "--request", str(request),
                                    "--parent-pid", str(os.getpid())], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 10
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue(marker.exists())
            process.kill(); process.communicate(timeout=10)
            time.sleep(.1)
            first = marker.read_text()
            time.sleep(.2)
            self.assertEqual(marker.read_text(), first)
        finally:
            if process.poll() is None:
                process.kill(); process.communicate(timeout=10)


class IntegrationTests(GateBase):
    def setUp(self):
        super().setUp()
        value = Policy([str(self.project)], denied=[str(self.home)])
        self.write(sandbox=True, sandbox_network=False, sandbox_mode="command", review_network="egress",
                   command_sandbox={"policy": value.to_dict(), "missing_protected": launcher.snapshot(value),
                                    "backend": {"name": "mxc", "command": [sys.executable, str(FAKE)]}})

    def test_job_snapshot_recovers_a_hard_killed_launcher_before_next_authorization(self):
        value = Policy.from_dict(self.data["command_sandbox"]["policy"])
        self.data["command_sandbox"]["missing_protected"] = launcher.snapshot(value)
        self.write()
        target = self.project / ".claude"
        target.mkdir()
        (target / "settings.json").write_text("fixture", encoding="utf-8")
        with patch.object(opencode_review, "run", side_effect=AssertionError("must check before review")), \
                self.assertRaisesRegex(SandboxError, "quarantine"):
            self.issue("npm test")
        self.assertFalse(target.exists())
        self.assertEqual(list((self.root / "sandbox").glob("command-*.json")), [])

    def test_snapshot_check_errors_fail_closed_before_review(self):
        value = Policy.from_dict(self.data["command_sandbox"]["policy"])
        self.data["command_sandbox"]["missing_protected"] = launcher.snapshot(value)
        self.write()
        with patch.object(launcher, "_entry_exists", side_effect=PermissionError("fixture")), \
                self.assertRaisesRegex(SandboxError, "could not quarantine"):
            self.issue("npm test")

    @unittest.skipUnless(os.name == "nt", "Windows command-line limit")
    def test_long_command_has_clear_error_and_no_launch_request(self):
        with self.assertRaisesRegex(PolicyError, "Windows command-line limit"):
            self.issue("npm test # " + "x" * 14000)
        self.assertEqual(list((self.root / "sandbox").glob("command-*.json")), [])

    @unittest.skipUnless(os.name == "nt", "Windows command-line limit")
    def test_long_command_error_reaches_the_shell_hook_without_executing_input(self):
        event = {"command": "npm test # " + "x" * 14000, "cwd": str(self.project), "env": dict(os.environ)}
        done = subprocess.run([sys.executable, str(Path(opencode.__file__)), str(self.context)],
                              input=json.dumps(event), capture_output=True, text=True, timeout=10)
        self.assertEqual(done.returncode, 0, done.stderr)
        failure = json.loads(done.stdout)
        self.assertIn("Windows command-line limit", failure["command"])
        self.assertNotIn("npm test", failure["command"])
        blocked = subprocess.run([failure["shell"], "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", failure["command"]],
                                 capture_output=True, text=True, timeout=10)
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("Windows command-line limit", blocked.stderr)

    def test_egress_approvals_are_reviewed_again_after_workspace_code_changes(self):
        self.write(reviewer=self.fake())
        for text in ("original", "changed lifecycle script"):
            (self.project / "package.json").write_text(text, encoding="utf-8")
            self.issue("npm install")
        self.assertEqual(self.calls(), 2)

    def test_egress_cannot_reuse_a_legacy_cached_approval(self):
        from cli_subagents.agy_review import store
        self.write(reviewer=self.fake())
        key = store.action_key("run_command", {"CommandLine": "npm install", "Cwd": str(self.project)})
        store.cache_put(Path(self.data["cache"]), key,
                        {"decision": "approve", "reason": "old", "risk": "low"})
        self.issue("npm install")
        self.assertEqual(self.calls(), 1)

    def issue(self, command, **values):
        event = {"command": command, "cwd": str(self.project), "timeout": 1000, "shell": "/bin/sh",
                 "env": {**os.environ, "OPENAI_API_KEY": "fixture-secret"}, **values}
        return opencode.prepare(event, self.context)

    def test_rule_and_review_network_are_bound_to_each_request(self):
        prepared = self.issue("npm test")
        [request] = list((self.root / "sandbox").glob("command-*.json"))
        data = json.loads(request.read_text())
        self.assertEqual(data["command"]["network"], "loopback")
        self.assertNotIn("OPENAI_API_KEY", data["command"]["env"])
        inner = base64.b64decode(data["command"]["argv"][-1]).decode("utf-16-le") if os.name == "nt" else data["command"]["argv"][-1]
        self.assertIn("npm test", inner)
        self.assertNotIn("npm test", prepared["command"])
        request.unlink()
        self.write(reviewer=self.fake())
        self.issue("npm install")
        [request] = list((self.root / "sandbox").glob("command-*.json"))
        self.assertEqual(json.loads(request.read_text())["command"]["network"], "egress")
        self.assertEqual(self.calls(), 1)

    def test_denials_read_jobs_and_external_cwd_issue_no_requests(self):
        for event in ({"command": "rm -rf /"}, {"command": "npm test", "cwd": str(self.root)}):
            with self.subTest(event=event), self.assertRaises(PolicyError):
                self.issue(**event)
        self.write(access="read")
        with self.assertRaises(PolicyError):
            self.issue("npm test")
        self.assertEqual(list((self.root / "sandbox").glob("command-*.json")), [])

    def test_reviewer_is_told_approval_grants_egress(self):
        self.write(reviewer=self.fake())
        seen = []

        def answer(config, request):
            seen.append(request)
            return {"decision": "allow", "risk": "low", "reason": "fixture"}

        with patch.object(reviewer, "review", side_effect=answer):
            self.issue("npm install")
        self.assertEqual(seen[0]["network_if_approved"], "egress")
        self.assertIn('"command network if approved": "egress"', reviewer.build_prompt(seen[0]))
        prompt = reviewer.build_prompt(seen[0])
        for name in ("~/.gitconfig", ".git/config", "http.extraHeader", "lifecycle"):
            self.assertIn(name, prompt)

    def test_external_file_writes_and_opencode_logins_never_reach_reviewer(self):
        self.write(reviewer=self.fake())
        self.assertEqual(self.review("write", {"path": str(self.root / "outside.txt")})["decision"], "deny")
        for name in (".local/share/opencode/auth.json", ".config/opencode/service.json"):
            self.assertEqual(self.review("read", {"path": str(self.root / name)})["decision"], "deny")
        self.assertEqual(self.calls(), 0)

    def test_custom_xdg_credentials_are_denied_before_review(self):
        auth = self.root / "custom-data" / "opencode" / "auth.json"
        self.write(reviewer=self.fake(), secret_paths=[str(auth)])
        self.assertEqual(self.review("read", {"path": str(auth)})["decision"], "deny")
        self.assertEqual(self.calls(), 0)
        folder = self.project / "custom-provider"
        folder.mkdir()
        auth = folder / "auth.json"
        auth.write_text('{"fixture":"not-a-real-key"}')
        self.write(secret_paths=[str(auth)])
        self.assertEqual(self.review("grep", {"path": str(folder), "pattern": "fixture"})["decision"], "deny")
        self.assertEqual(self.calls(), 0)


@unittest.skipUnless(shutil.which("node"), "Node executes the generated hook")
class ShellHookTests(OpenCodeHome):
    plugin = reviewer_tests.PluginCheckTests.plugin
    run_js = reviewer_tests.PluginCheckTests.run_js

    def test_host_gate_ignores_workspace_pythonpath(self):
        marker = self.project / "imported"
        (self.project / "json.py").write_text(f'from pathlib import Path; Path({str(marker)!r}).touch()\nraise RuntimeError("planted")')
        plugin = self.plugin('import json, sys\nsys.stdin.read()\nprint(json.dumps({"decision":"auto_approve"}))', review_seconds=2)
        with patch.dict(os.environ, {"PYTHONPATH": str(self.project)}):
            self.run_js(plugin, '''
const ctx = makeContext(); await plugin.setup(ctx);
await ctx.hooks["tool:execute.before"]({ tool:"read", input:{path:"fixture"} });
''')
        self.assertFalse(marker.exists())

    def test_hook_failure_replaces_original_before_await_and_blocks_model_unlock_on_missing_api(self):
        plugin = self.plugin('raise RuntimeError("fixture")', review_seconds=2)
        plugin.write_text(plugin.read_text().replace('"sandbox": false', '"sandbox": true'))
        lines = self.run_js(plugin, '''
const ctx = makeContext(); ctx.shell = { hook: async (name, callback) => { ctx.shellHook = callback; } };
await plugin.setup(ctx);
const invocation = { command: "ORIGINAL_MUST_NOT_RUN", cwd: process.cwd(), env: process.env, timeout: 1 };
let rejected = false;
try { await ctx.shellHook(invocation); } catch (error) { rejected = true; }
console.log(JSON.stringify(invocation));
console.log(JSON.stringify({ rejected }));
const missing = makeContext(); delete missing.shell; let failed = false;
try { await plugin.setup(missing); } catch (error) { failed = true; }
console.log(JSON.stringify({ failed, unlocked: !!missing.hooks["session:prompt"] }));
''')
        invocation = json.loads(lines[0])
        self.assertNotIn("ORIGINAL_MUST_NOT_RUN", invocation["command"])
        self.assertIn("failed closed", invocation["command"])
        self.assertEqual(json.loads(lines[1]), {"rejected": False})
        self.assertEqual(json.loads(lines[2]), {"failed": True, "unlocked": False})
