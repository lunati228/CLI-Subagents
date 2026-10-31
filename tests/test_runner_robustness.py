"""Runner regressions: process trees, continuations, caps, budgets and live events."""

import io
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time
import tomllib
import unittest
from unittest.mock import patch

from helpers import TempHome
from cli_subagents import accounts, jobs, limits, messages, quota, tools
from cli_subagents.config import Account, QuotaPool, load_config, save_config
from cli_subagents.fsutil import kill_tree, process_start_time, same_process
from cli_subagents.runner import Runner
from cli_subagents.runtimes.base import AUTH, SUCCESS, USAGE_LIMIT, AttemptResult, JobSpec
from cli_subagents.runtimes.codex import CodexRuntime

HERE = Path(__file__).resolve().parent


class RobustnessBase(TempHome):
    def setUp(self):
        super().setUp()
        os.environ.update(
            CLAUDE_CONFIG_DIR=str(self.primary), CODEX_HOME=str(self.root / "codex"),
            CLI_SUBAGENTS_CODEX_COMMAND=json.dumps([sys.executable, str(HERE / "fake_codex.py")]),
            CLI_SUBAGENTS_AGY_COMMAND=json.dumps([sys.executable, str(HERE / "fake_agy.py")]),
        )
        for key in ("FAKE_CODEX_LIMITED", "FAKE_CLAUDE_DELAY", "FAKE_AGY_SCENARIO"):
            os.environ.pop(key, None)
        config = load_config()
        for name, runtime in config.runtimes.items():
            if name == "opencode":  # the native profile has its own adapter tests
                continue
            runtime.primary = self.root / name
            runtime.primary.mkdir(exist_ok=True)
            runtime.include_primary = False
            runtime.accounts = [Account(account, self.root / name / account) for account in ("a1", "a2")]
            runtime.failover = "on-limit"
            runtime.when_full = "queue"
        save_config(config)

    def runner(self, runtime="codex", account=None, model=None):
        record = jobs.create_job(runtime=runtime, prompt="task", cwd=str(self.project), account=account,
                                 model=model, access="read", start=False)
        return Runner(record["job_id"])

    def wait_until(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("condition did not become true before timeout")


class DescendantCleanupTests(RobustnessBase):
    def executing(self, pid):
        if sys.platform.startswith("linux"):
            try:
                raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
                if raw[raw.rindex(")") + 2:].split()[0] == "Z":
                    return False  # killed orphan waiting for init to reap it
            except FileNotFoundError:
                return False
        return same_process(pid, process_start_time(pid))

    def check_descendant(self, mode):
        child_file = self.root / "child.pid"
        os.environ.update(
            CLI_SUBAGENTS_CODEX_COMMAND=json.dumps([sys.executable, str(HERE / "fake_robust_worker.py")]),
            FAKE_ROBUST_MODE=mode, FAKE_ROBUST_CHILD_PID=str(child_file),
        )
        runner = self.runner(account="a1")
        lease = runner._wait_for_account(set(), pinned="a1")
        try:
            before = time.monotonic()
            result = runner._attempt(lease, "task")
            self.assertEqual(result.outcome, SUCCESS, result)
            self.assertLess(time.monotonic() - before, 10)
            pid = int(child_file.read_text(encoding="ascii"))
            self.assertFalse(self.executing(pid), "descendant survived while its lease was still held")
            self.assertIsNone(accounts.try_lease(runner.runtime_config, lease.account))
        finally:
            lease.release()
            if child_file.exists():
                pid = int(child_file.read_text(encoding="ascii"))
                if self.executing(pid):
                    if os.name == "nt":
                        kill_tree(pid)
                    else:
                        os.kill(pid, 9)

    def test_redirected_descendant_is_dead_before_lease_release(self):
        self.check_descendant("tree")

    def test_descendant_inheriting_stdout_does_not_hold_the_job_open(self):
        self.check_descendant("pipe_tree")

    @unittest.skipUnless(os.name == "nt", "Windows Job Object lifetime")
    def test_runner_death_closes_the_job_object_and_kills_descendants(self):
        child_file = self.root / "child.pid"
        os.environ.update(
            CLI_SUBAGENTS_CODEX_COMMAND=json.dumps([sys.executable, str(HERE / "fake_robust_worker.py")]),
            FAKE_ROBUST_MODE="tree_wait", FAKE_ROBUST_CHILD_PID=str(child_file),
            FAKE_ROBUST_RELEASE=str(self.root / "release"),
        )
        status = jobs.create_job(runtime="codex", prompt="task", cwd=str(self.project), account="a1", access="read")
        try:
            self.wait_until(child_file.exists)
            status = jobs.read_status(status["job_id"])
            child = int(child_file.read_text(encoding="ascii"))
            self.assertTrue(self.executing(child))
            os.kill(status["runner_pid"], signal.SIGTERM)
            self.wait_until(lambda: not self.executing(child))
        finally:
            jobs.update_status(status["job_id"], state="done")

    @unittest.skipUnless(os.name == "nt", "Windows suspended process assignment")
    def test_failed_job_assignment_cannot_run_the_worker(self):
        from cli_subagents.fsutil import _WindowsJob

        os.environ.update(
            CLI_SUBAGENTS_CODEX_COMMAND=json.dumps([sys.executable, str(HERE / "fake_robust_worker.py")]),
            FAKE_ROBUST_MODE="tree", FAKE_ROBUST_CHILD_PID=str(self.root / "child.pid"),
        )
        runner = self.runner(account="a1")
        with patch.object(_WindowsJob, "start", side_effect=OSError("assignment failed")):
            self.assertEqual(runner.run(), 1)
        self.assertFalse((self.root / "child.pid").exists())
        lease = accounts.try_lease(runner.runtime_config, runner.runtime_config.find("a1"))
        self.assertIsNotNone(lease)
        lease.release()


class ContinuationAccountTests(RobustnessBase):
    def test_internal_continuation_waits_for_its_owner_on_nontransferable_runtimes(self):
        for runtime in ("codex", "antigravity"):
            with self.subTest(runtime=runtime):
                runner = self.runner(runtime=runtime)
                held = []
                calls = []

                def attempt(lease, prompt, session, owner, fallback):
                    calls.append((lease.account.name, session, owner))
                    if len(calls) == 1:
                        messages.post_to_worker(runner.job_id, "continue")
                    return AttemptResult(SUCCESS, text="done", session_id="session-a1")

                original_acquire = accounts.acquire

                def acquire(config, **kwargs):
                    if len(calls) == 1 and not held:
                        competitor = accounts.try_lease(config, config.find("a1"))
                        self.assertIsNotNone(competitor)
                        held.append(competitor)
                        threading.Timer(0.15, competitor.release).start()
                    return original_acquire(config, **kwargs)

                with patch.object(runner, "_attempt", side_effect=attempt), patch.object(accounts, "acquire", side_effect=acquire):
                    self.assertEqual(runner.run(), 0)
                self.assertEqual(calls, [("a1", None, None), ("a1", "session-a1", "a1")])

    def test_follow_up_survives_a_usage_limit_failover(self):
        # The follow-up was taken from the inbox for its own turn; when that turn hits a
        # usage limit, the restart on the next account must still carry it.
        for runtime in ("codex", "claude"):
            with self.subTest(runtime=runtime):
                runner = self.runner(runtime=runtime)
                calls = []

                def attempt(lease, prompt, session, owner, fallback):
                    calls.append((lease.account.name, prompt, fallback))
                    if len(calls) == 1:
                        messages.post_to_worker(runner.job_id, "also update the changelog")
                        return AttemptResult(SUCCESS, text="first answer", session_id="s1")
                    if len(calls) == 2:
                        return AttemptResult(USAGE_LIMIT, message="usage limit reached", session_id="s1")
                    return AttemptResult(SUCCESS, text="done")

                with patch.object(runner, "_attempt", side_effect=attempt):
                    self.assertEqual(runner.run(), 0)
                self.assertEqual([call[0] for call in calls], ["a1", "a1", "a2"], calls)
                self.assertIn("also update the changelog", calls[1][1])
                # Codex can't move the session, so the restart gets the task again, follow-up included;
                # Claude continues the session and needs the follow-up only if the move fails.
                restart = calls[2][1] if runtime == "codex" else calls[2][2]
                self.assertIn("task", restart)
                self.assertIn("also update the changelog", restart)


class InboxFinalizationTests(RobustnessBase):
    def test_message_after_final_drain_starts_a_continuation_before_done_is_written(self):
        config = load_config()
        config.max_jobs = 1
        config.runtime("codex").when_full = "refuse"
        save_config(config)
        runner = self.runner(account="a1")
        runner.status(state="running", account="a1", session_id="stale-session")
        (runner.folder / "runner.pid").write_text(str(os.getpid()), encoding="ascii")
        self.assertEqual(messages.close_or_continue(runner.job_id, "final-session", "a1", True), [])
        self.assertEqual(jobs.read_status(runner.job_id)["state"], "running")
        with patch.object(jobs, "spawn_detached", return_value=os.getpid()):
            report = tools.call("subagent_message", {"job_id": runner.job_id, "text": "late follow-up"})
        continued = report["new_job"]["job_id"]
        try:
            spec = jobs.read_spec(continued)
            self.assertEqual((spec.resume_session, spec.resume_account, spec.account), ("final-session", "a1", "a1"))
            self.assertEqual(spec.prompt, "late follow-up")
            self.assertEqual(messages.take_for_worker(runner.job_id), [])
        finally:
            jobs.update_status(continued, state="done")
            jobs.update_status(runner.job_id, state="done")

    def test_message_accepted_before_closure_becomes_an_internal_turn(self):
        runner = self.runner()
        report = tools.call("subagent_message", {"job_id": runner.job_id, "text": "follow-up"})
        self.assertEqual(report["delivered"], "queued for the running subagent")
        pending = messages.close_or_continue(runner.job_id, "session", "a1", True)
        self.assertEqual([item["text"] for item in pending], ["follow-up"])
        self.assertFalse((runner.folder / "inbox-closed.json").exists())
        self.assertEqual(messages.close_or_continue(runner.job_id, "session", "a1", True), [])
        self.assertIsNone(messages.queue_for_worker(runner.job_id, "late"))

    def test_acceptance_racing_closure_is_drained_or_rejected(self):
        for _ in range(12):
            runner = self.runner()
            barrier = threading.Barrier(2)
            accepted, drained = [], []

            def send():
                barrier.wait()
                accepted.append(messages.queue_for_worker(runner.job_id, "follow-up"))

            thread = threading.Thread(target=send)
            thread.start()
            barrier.wait()
            drained.extend(messages.close_or_continue(runner.job_id, "session", "a1", True))
            thread.join(5)
            self.assertFalse(thread.is_alive())
            if accepted[0] is not None:
                self.assertEqual(drained, accepted)
            else:
                self.assertEqual(drained, [])


class ExecutionCapTests(RobustnessBase):
    def check_cap(self, global_cap):
        config = load_config()
        if global_cap:
            config.max_jobs = 1
        else:
            config.runtime("codex").max_jobs = 1
        save_config(config)
        first = self.runner(account="a1")
        second = self.runner(runtime="claude" if global_cap else "codex", account="a2")
        # Queueing still permits admission while all execution slots are occupied.
        limits.admit(config, second.spec.runtime, account="a2")
        lease = first._wait_for_account(set())
        self.assertIsNotNone(lease)
        acquired = []
        thread = threading.Thread(target=lambda: acquired.append(second._wait_for_account(set())))
        thread.start()
        try:
            self.wait_until(lambda: jobs.read_status(second.job_id).get("message") == "waiting for a free account slot")
            self.assertEqual(acquired, [])
        finally:
            lease.release()
            thread.join(10)
            if thread.is_alive():
                (second.folder / "cancel").touch()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(acquired), 1)
        self.assertIsNotNone(acquired[0])
        acquired[0].release()

    def test_queued_jobs_obey_the_global_cap_across_runtimes(self):
        self.check_cap(True)

    def test_queued_jobs_obey_the_runtime_cap_across_accounts(self):
        self.check_cap(False)

    def test_runtime_cap_failure_releases_its_partial_global_lease(self):
        config = load_config()
        config.max_jobs = 2
        config.runtime("codex").max_jobs = 1
        save_config(config)
        first = self.runner(account="a1")
        lease = first._wait_for_account(set())
        try:
            self.assertIsNone(limits.acquire_execution_slots(config, "codex"))
            other = self.runner(runtime="claude", account="a1")
            other_lease = other._wait_for_account(set())
            self.assertIsNotNone(other_lease)
            other_lease.release()
        finally:
            lease.release()


class StartedBudgetTests(RobustnessBase):
    def setUp(self):
        super().setUp()
        config = load_config()
        config.runtime("codex").pools = [QuotaPool("test", ["gpt-test"], weekly_jobs=1)]
        save_config(config)

    def budget(self):
        return quota.pool_state(load_config().runtime("codex"), limits.count_active(), time.time()).weekly.get(("a1", "test"), 0)

    def test_failed_preflight_does_not_spend_weekly_budget(self):
        runner = self.runner(account="a1", model="gpt-test")
        with patch.object(runner.runtime, "preflight", create=True, return_value=AttemptResult(AUTH, message="login needed")):
            self.assertEqual(runner.run(), 1)
        self.assertTrue(runner.attempts[0]["before_start"])
        self.assertNotIn("started_at", runner.attempts[0])
        self.assertEqual(self.budget(), 0)

    def test_launch_configuration_failure_does_not_spend_weekly_budget(self):
        runner = self.runner(account="a1", model="gpt-test")
        with patch.object(runner.runtime, "launch", side_effect=RuntimeError("bad configuration")):
            self.assertEqual(runner.run(), 1)
        self.assertNotIn("started_at", runner.attempts[0])
        self.assertEqual(self.budget(), 0)

    def test_process_creation_failure_does_not_spend_weekly_budget(self):
        runner = self.runner(account="a1", model="gpt-test")
        with patch("cli_subagents.fsutil.subprocess.Popen", side_effect=OSError("cannot create process")):
            self.assertEqual(runner.run(), 1)
        self.assertNotIn("started_at", runner.attempts[0])
        self.assertEqual(self.budget(), 0)

    def test_launched_process_spends_weekly_budget(self):
        runner = self.runner(account="a1", model="gpt-test")
        self.assertEqual(runner.run(), 0)
        self.assertFalse(runner.attempts[0]["before_start"])
        self.assertIn("started_at", runner.attempts[0])
        self.assertEqual(self.budget(), 1)

    def test_explicit_before_start_marker_excludes_a_legacy_timestamp(self):
        runner = self.runner(account="a1", model="gpt-test")
        runner.attempts.append({"account": "a1", "pool": "test", "started_at": time.time(),
                                "before_start": True, "outcome": AUTH})
        runner.finish(AUTH, "login needed")
        self.assertEqual(self.budget(), 0)


class LiveEventTests(RobustnessBase):
    def test_complete_events_are_readable_while_the_worker_is_running(self):
        gate = self.root / "release"
        os.environ.update(
            CLI_SUBAGENTS_CODEX_COMMAND=json.dumps([sys.executable, str(HERE / "fake_robust_worker.py")]),
            FAKE_ROBUST_MODE="stream", FAKE_ROBUST_RELEASE=str(gate),
        )
        status = jobs.create_job(runtime="codex", prompt="task", cwd=str(self.project), account="a1", access="read")
        path = jobs.job_dir(status["job_id"]) / "events.jsonl"
        try:
            self.wait_until(lambda: path.exists() and path.stat().st_size > 0, timeout=8)
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(json.loads(lines[0])["type"], "thread.started")
            self.assertEqual(jobs.read_status(status["job_id"])["state"], "running")
        finally:
            gate.touch()
            final = jobs.wait_for(status["job_id"], 20)
        self.assertEqual(final["outcome"], SUCCESS, final)

    def test_output_read_just_after_the_worker_exited_still_reaches_the_parser(self):
        # The reader thread can still be putting the CLI's last line when the main loop
        # sees the process gone: that line holds the final answer and must not be dropped.
        class LateStdout:
            def __iter__(self):
                time.sleep(0.8)  # longer than one queue wait
                yield b'{"type":"item.completed","item":{"type":"agent_message","text":"final answer"}}\n'

        class ExitedProcess:
            pid = 0
            stdin = io.BytesIO()
            stdout = LateStdout()

            def poll(self):
                return 0

        from cli_subagents.runtimes.codex import CodexParser

        runner = self.runner()
        parser = CodexParser()
        events = io.BytesIO()
        account = load_config().runtime("codex").find("a1")
        self.assertIsNone(runner._pump(ExitedProcess(), "task", parser, account, events, time_limit=30))
        self.assertEqual(parser.last_text, "final answer")


class ParentApprovalTests(RobustnessBase):
    def test_only_parent_server_tools_are_autoapproved_for_read_edit_and_resume(self):
        config = load_config().runtime("codex")
        account = config.find("a1")
        account.home.mkdir(parents=True)
        for access in ("read", "edit"):
            for resume in (None, "existing-thread"):
                with self.subTest(access=access, resume=resume):
                    spec = JobSpec(job_id="test", runtime="codex", prompt="task", cwd=str(self.project),
                                   access=access, resume_session=resume)
                    launch = CodexRuntime().launch(config, account, spec, "task", {})
                    overrides = [launch.argv[i + 1] for i, arg in enumerate(launch.argv[:-1]) if arg == "-c"]
                    approval = [value for value in overrides if "approval_mode=" in value]
                    self.assertEqual(len(approval), 4)
                    parsed = tomllib.loads("\n".join(approval))["mcp_servers"]
                    self.assertEqual(list(parsed), ["csa_parent"])
                    parent = parsed["csa_parent"]
                    self.assertEqual(parent["default_tools_approval_mode"], "approve")
                    self.assertEqual(parent["tools"], {name: {"approval_mode": "approve"} for name in
                                                     ("message_parent", "ask_parent", "check_parent_messages")})
                    self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", launch.argv)


if __name__ == "__main__":
    unittest.main()
