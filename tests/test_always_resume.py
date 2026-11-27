"""A stopped job always continues as the same worker conversation, never as a fresh job.

Covers the 2026-11-21 incident: continuations of override jobs were refused at the pool
cap, so the manager started fresh jobs that re-read everything. Fake CLIs only.
"""

import contextlib
import io
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch

from helpers import TempHome
from cli_subagents import cli, jobs, limits, tools
from cli_subagents.config import QuotaPool, load_config, save_config
from cli_subagents.fsutil import kill_tree, read_json, write_json, write_text

WORDS = "user asked for 4 Codex Sol agents"
NEW_WORDS = "user said: resume all four now"
MODEL = "gpt-6-luna"
HERE = Path(__file__).resolve().parent
DEAD_PID = 999_999_99  # no process has this PID; read_status sees a dead runner


class ResumeTests(TempHome):
    def setUp(self):
        super().setUp()
        os.environ.update(
            CLAUDE_CONFIG_DIR=str(self.primary), CODEX_HOME=str(self.root / "codex"),
            CLI_SUBAGENTS_CODEX_COMMAND=json.dumps([sys.executable, str(HERE / "fake_codex.py")]),
        )
        self.config = load_config()
        for name, runtime in self.config.runtimes.items():
            runtime.primary = self.primary if name == "claude" else self.root / name
            runtime.primary.mkdir(exist_ok=True)
            runtime.accounts = []
            runtime.include_primary = True
        self.runtime = self.config.runtime("codex")
        self.runtime.default_model = MODEL
        self.records_to_close = []
        self.unavailable = patch.object(limits, "_available_runtime", return_value=False)
        self.unavailable.start()
        save_config(self.config)

    def tearDown(self):
        for job_id in self.records_to_close:
            jobs.update_status(job_id, state="done", outcome="success")
        self.unavailable.stop()
        super().tearDown()

    # Helpers -----------------------------------------------------------------

    def occupy_slot(self):
        """A running Codex job that holds the account's one slot."""

        job_id = jobs.new_job_id()
        now = time.time()
        folder = jobs.job_dir(job_id)
        folder.mkdir(parents=True)
        write_json(folder / "spec.json", {"model": MODEL, "account": None})
        write_json(folder / "status.json", {
            "job_id": job_id, "runtime": "codex", "state": "running", "account": "primary",
            "created_at": now, "updated_at": now, "started_at": now - 10, "outcome": None,
        })
        write_text(folder / "runner.pid", str(os.getpid()))
        self.records_to_close.append(job_id)
        return job_id

    def stopped_job(self, override=None, outcome="usage_limit", session="thread-primary"):
        """A Codex job that stopped (here on a usage limit) with a saved session."""

        status = jobs.create_job(runtime="codex", prompt="the original task", cwd=str(self.project),
                                 override=override, start=False)
        jobs.update_status(status["job_id"], state="done", outcome=outcome, session_id=session,
                           account="primary", finished_at=time.time())
        return status["job_id"]

    def full_cap(self):
        self.runtime.primary_max_jobs = 1
        save_config(self.config)
        self.occupy_slot()

    def message(self, arguments):
        with patch.object(jobs, "spawn_detached", return_value=os.getpid()):
            reply = tools.call("subagent_message", arguments)
        new_job = reply["new_job"]["job_id"]
        self.records_to_close.append(new_job)
        return new_job

    # 1. Override on continuations -------------------------------------------

    def test_continuation_keeps_the_original_override_at_a_full_cap(self):
        job = self.stopped_job(override=WORDS)
        self.full_cap()
        new_job = self.message({"job_id": job, "text": "continue"})
        spec = jobs.read_spec(new_job)
        self.assertEqual((spec.override, spec.resume_session), (WORDS, "thread-primary"))
        self.assertEqual(jobs.read_status(new_job)["override"], WORDS)

    def test_follow_up_can_carry_a_new_override(self):
        job = self.stopped_job()
        self.full_cap()
        new_job = self.message({"job_id": job, "text": "continue", "override": NEW_WORDS})
        self.assertEqual(jobs.read_spec(new_job).override, NEW_WORDS)
        schema = next(item for item in tools.DEFINITIONS if item["name"] == "subagent_message")["inputSchema"]
        self.assertIn("override", schema["properties"])

    def test_override_never_bypasses_a_cooldown_on_a_continuation(self):
        from cli_subagents import accounts

        self.runtime.pools = [QuotaPool("sol", ["gpt-*"], per_account=1)]
        save_config(self.config)
        job = self.stopped_job(override=WORDS)
        accounts.set_pool_cooldown("codex", "primary", "sol", time.time() + 3600, "usage limit")
        with self.assertRaises(tools.ToolError) as caught:
            self.message({"job_id": job, "text": "continue"})
        self.assertIn("usage limit", str(caught.exception))
        self.assertIn(f"resume={job}", str(caught.exception))

    def test_cli_message_override_flag_and_relay_override_line(self):
        job = self.stopped_job()
        args = cli.parse_args(["message", job, "--override", NEW_WORDS, "go on"])
        with patch("cli_subagents.tools.call", return_value={"job_id": job}) as call, \
                contextlib.redirect_stdout(io.StringIO()):
            cli._message(args)
        self.assertEqual(call.call_args.args[1]["override"], NEW_WORDS)
        from cli_subagents import relay_input

        path = relay_input.fresh_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"override={NEW_WORDS}\n\ngo on", encoding="utf-8")
        args = cli.parse_args(["message", job, "--relay-file", str(path)])
        with patch("cli_subagents.tools.call", return_value={"job_id": job}) as call, \
                contextlib.redirect_stdout(io.StringIO()):
            cli._message(args)
        self.assertEqual(call.call_args.args[1]["override"], NEW_WORDS)
        self.assertEqual(call.call_args.args[1]["text"], "go on")

    # 2. A continuation refused only by a cap waits instead -------------------

    def test_continuation_without_override_queues_at_a_full_cap(self):
        job = self.stopped_job()
        self.full_cap()
        with self.assertRaises(limits.LimitError), patch.object(jobs, "spawn_detached", return_value=os.getpid()):
            jobs.create_job(runtime="codex", prompt="a fresh job", cwd=str(self.project))
        new_job = self.message({"job_id": job, "text": "continue"})
        spec = jobs.read_spec(new_job)
        self.assertIsNone(spec.override)
        self.assertEqual(spec.resume_session, "thread-primary")

    def test_continuation_refusal_says_continue_later_never_fresh(self):
        self.runtime.pools = [QuotaPool("sol", ["gpt-*"], weekly_jobs=1)]
        save_config(self.config)
        job = self.stopped_job()
        jobs.update_status(job, started_at=time.time() - 60)  # it used this week's one Sol start
        with self.assertRaises(tools.ToolError) as caught:
            self.message({"job_id": job, "text": "continue"})
        text = str(caught.exception)
        self.assertIn(f"resume={job}", text)
        self.assertIn("never start a new job", text)

    def test_continuation_that_never_ran_keeps_the_session_for_the_next_try(self):
        job = self.stopped_job()
        status = jobs.create_job(runtime="codex", prompt="continue", cwd=str(self.project), resume_job=job, start=False)
        # It waited at the cap and gave up without running: no attempt, no session of its own.
        jobs.update_status(status["job_id"], state="done", outcome="no_account", session_id=None)
        again = jobs.create_job(runtime="codex", prompt="continue", cwd=str(self.project),
                                resume_job=status["job_id"], start=False)
        self.assertEqual(jobs.read_spec(again["job_id"]).resume_session, "thread-primary")

    # 3. Resuming after the relay is gone ------------------------------------

    def relay_run(self, text, runtime="codex"):
        from cli_subagents import relay_input

        path = relay_input.fresh_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.main(["run", "--follow", "--window", "100", "--max-wait", "60", "--runtime", runtime,
                             "--relay-file", str(path)])
        return code, output.getvalue()

    def test_new_relay_resumes_a_finished_job_with_resume_option(self):
        job = self.stopped_job(override=WORDS)
        self.full_cap()
        code, output = self.relay_run(f"resume={job}\n\nThe limit reset. Continue where you stopped.")
        header = output.splitlines()[0]
        self.assertNotIn(job, header, output)
        new_job = header.split(" · job ")[1].split(" ")[0]
        spec = jobs.read_spec(new_job)
        self.assertEqual((spec.resume_session, spec.override), ("thread-primary", WORDS))
        self.assertIn("Continue where you stopped.", spec.prompt)
        self.assertEqual(code, 0, output)
        argv = self.records()[-1]["argv"]
        self.assertEqual(argv[argv.index("resume") + 1], "thread-primary")

    def test_resume_option_rejects_another_runtime_and_job_settings(self):
        job = self.stopped_job()
        code, output = self.relay_run(f"resume={job}\n\ngo on", runtime="claude")
        self.assertEqual(code, 1)
        self.assertIn("cli-subagents-codex", output)
        code, output = self.relay_run(f"resume={job}\nmodel=other\n\ngo on")
        self.assertEqual(code, 1)
        self.assertIn("model", output)

    def test_list_marks_resumable_jobs_and_their_continuations(self):
        job = self.stopped_job()
        new_job = self.message({"job_id": job, "text": "continue"})
        listed = {item["job_id"]: item for item in tools.call("subagent_list", {})["jobs"]}
        self.assertEqual(listed[job]["continued_by"], new_job)
        self.assertEqual(listed[new_job]["continues"], job)
        self.assertTrue(listed[job]["resumable"])
        self.assertIn("the original task", listed[job]["task"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli.main(["jobs", "--resumable"])
        lines = output.getvalue()
        self.assertNotIn(job, lines)  # already continued: resume the newest job of the chain

    # 4. The PC was switched off mid-job --------------------------------------

    def test_killed_runner_leaves_a_continuable_codex_job(self):
        os.environ["FAKE_CODEX_SCENARIO"] = "success_pause"  # the worker keeps running after its turn
        status = jobs.create_job(runtime="codex", prompt="long task", cwd=str(self.project))
        job = status["job_id"]
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            current = read_json(jobs.job_dir(job) / "status.json", {}) or {}
            if current.get("state") == "running" and current.get("session_id"):
                break
            time.sleep(0.2)
        current = read_json(jobs.job_dir(job) / "status.json", {}) or {}
        self.assertEqual(current.get("state"), "running", current)
        # Machine off: runner and worker die at once, nothing writes a final status.
        for pid in (current.get("runner_pid"), current.get("child_pid")):
            if pid:
                kill_tree(int(pid))
        current = read_json(jobs.job_dir(job) / "status.json", {}) or {}
        current["updated_at"] = time.time() - 120
        write_json(jobs.job_dir(job) / "status.json", current)
        os.environ.pop("FAKE_CODEX_SCENARIO")
        self.assertEqual(jobs.read_status(job)["outcome"], "lost")
        reply = tools.call("subagent_message", {"job_id": job, "text": "The PC restarted. Continue."})
        new_job = reply["new_job"]["job_id"]
        final = jobs.wait_for(new_job, 60)
        self.assertEqual(final["outcome"], "success", final)
        argv = self.records()[-1]["argv"]
        self.assertEqual(argv[argv.index("resume") + 1], "thread-codex")

    def test_lost_job_recovers_its_session_from_the_event_log(self):
        # Records written before the runner saved session ids mid-run: the id is only in events.jsonl.
        cases = {
            "codex": ({"type": "thread.started", "thread_id": "thread-x"}, "thread-x"),
            "opencode": ({"type": "step_start", "timestamp": 1, "sessionID": "ses_x", "part": {}}, "ses_x"),
            "antigravity": ({"event": "init", "conversation_id": "conv-x", "init": {}}, "conv-x"),
        }
        for runtime, (event, session) in cases.items():
            with self.subTest(runtime=runtime):
                job = jobs.new_job_id()
                folder = jobs.job_dir(job)
                folder.mkdir(parents=True)
                write_json(folder / "spec.json", {"job_id": job, "runtime": runtime, "prompt": "x",
                                                  "cwd": str(self.project), "access": "read"})
                now = time.time()
                write_json(folder / "status.json", {
                    "job_id": job, "runtime": runtime, "state": "running", "account": "primary",
                    "runner_pid": DEAD_PID, "created_at": now - 600, "updated_at": now - 300,
                    "attempts": [{"account": "primary", "session_id": None}],
                })
                write_text(folder / "events.jsonl", json.dumps(event) + "\n")
                self.assertEqual(jobs.read_status(job)["outcome"], "lost")
                with patch.dict(os.environ, {"CLI_SUBAGENTS_OPENCODE_COMMAND": json.dumps(
                        [sys.executable, str(HERE / "fake_opencode.py")])}):
                    status = jobs.create_job(runtime=runtime, prompt="continue", cwd=str(self.project),
                                             resume_job=job, start=False)
                self.assertEqual(jobs.read_spec(status["job_id"]).resume_session, session)

    def test_session_start_lists_a_lost_job_with_how_to_continue_it(self):
        from cli_subagents import hooks

        job = jobs.new_job_id()
        folder = jobs.job_dir(job)
        folder.mkdir(parents=True)
        write_json(folder / "spec.json", {"job_id": job, "runtime": "codex", "prompt": "x", "cwd": str(self.project)})
        now = time.time()
        write_json(folder / "status.json", {
            "job_id": job, "runtime": "codex", "state": "running", "account": "primary", "cwd": str(self.project),
            "runner_pid": DEAD_PID, "created_at": now - 600, "updated_at": now - 300, "session_id": "thread-primary",
        })
        context = hooks.pending_context({"cwd": str(self.project)})
        text = context["hookSpecificOutput"]["additionalContext"]
        self.assertIn(f"{job}: lost", text)
        self.assertIn(f"resume={job}", text)
        self.message({"job_id": job, "text": "continue"})
        self.assertIsNone(hooks.pending_context({"cwd": str(self.project)}))  # continued: no longer listed

    def test_job_that_never_started_says_to_start_again(self):
        status = jobs.create_job(runtime="codex", prompt="x", cwd=str(self.project), start=False)
        jobs.update_status(status["job_id"], state="done", outcome="cancelled")
        with self.assertRaises(ValueError) as caught:
            jobs.create_job(runtime="codex", prompt="go", cwd=str(self.project), resume_job=status["job_id"], start=False)
        self.assertIn("never started", str(caught.exception))

    # 5. Docs say: continue, never start fresh --------------------------------

    def test_shipped_docs_say_to_continue_not_restart(self):
        root = HERE.parent
        skill = (root / "skills" / "cli-subagents" / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue("resume=" in skill, "SKILL.md does not document resume=")
        self.assertTrue("never start a new job for the same work" in skill.lower(), "SKILL.md lacks the rule")
        for path in (root / "agents").glob("cli-subagents-*.md"):
            with self.subTest(path=path.name):
                self.assertIn("resume=JOB", path.read_text(encoding="utf-8"))
