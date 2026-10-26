"""User-authorized cap overrides, with isolated records, leases and fake CLIs."""

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch

from helpers import TempHome
from cli_subagents import accounts, cli, jobs, limits, tools
from cli_subagents.config import QuotaPool, load_config, save_config
from cli_subagents.follow import follow
from cli_subagents.fsutil import unlock, write_json, write_text
from cli_subagents.runner import Runner

WORDS = "user asked for 6 Codex agents"
MODEL = "gpt-6-luna"
HERE = Path(__file__).resolve().parent


class OverrideTests(TempHome):
    def setUp(self):
        super().setUp()
        os.environ.update(
            CLAUDE_CONFIG_DIR=str(self.primary), CODEX_HOME=str(self.root / "codex"),
            CLI_SUBAGENTS_CODEX_COMMAND=json.dumps([sys.executable, str(HERE / "fake_codex.py")]),
            CLI_SUBAGENTS_AGY_COMMAND=json.dumps([sys.executable, str(HERE / "fake_agy.py")]),
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

    def record_job(self, *, runtime="codex", active=True):
        job_id = jobs.new_job_id()
        now = time.time()
        folder = jobs.job_dir(job_id)
        folder.mkdir(parents=True)
        write_json(folder / "spec.json", {"model": MODEL, "account": None})
        write_json(folder / "status.json", {
            "job_id": job_id, "runtime": runtime, "state": "running" if active else "done",
            "account": "primary", "created_at": now, "updated_at": now,
            "started_at": now - 10, "outcome": None if active else "success",
        })
        if active:
            write_text(folder / "runner.pid", str(os.getpid()))
            self.records_to_close.append(job_id)
        return job_id

    def assert_bypassed(self, cap_name, *, runtime="codex"):
        save_config(self.config)
        with self.assertRaises(limits.LimitError) as caught:
            limits.admit(self.config, runtime, model=MODEL)
        self.assertIn(cap_name, str(caught.exception))
        self.assertIn(limits.OVERRIDE_HINT, str(caught.exception))
        admitted = limits.admit(self.config, runtime, model=MODEL, override=WORDS)
        self.assertEqual(admitted.account, "primary")
        return admitted

    def test_global_cap(self):
        self.config.max_jobs = 1
        self.record_job(runtime="claude")
        self.assert_bypassed("global limit")

    def test_runtime_cap(self):
        self.runtime.max_jobs = 1
        self.record_job()
        self.assert_bypassed("max_jobs under [codex]")

    def test_explicit_account_cap(self):
        self.runtime.primary_max_jobs = 1
        self.record_job()
        self.assert_bypassed("max_jobs = 1 on the account")

    def test_plan_preset_caps(self):
        for name in ("codex", "claude", "antigravity"):
            with self.subTest(runtime=name):
                self.config.runtime(name).plan = "free"
                self.record_job(runtime=name)
                self.assert_bypassed("plan preset free", runtime=name)

    def test_quota_steps_including_zero_cap(self):
        self.runtime.max_jobs_by_room = [[0, 0]]
        self.assert_bypassed("max_jobs_by_room")

    def test_usage_ceiling(self):
        accounts.update_usage("codex", "primary", windows={
            "five_hour": {"utilization": 0.99, "resets_at": time.time() + 3600},
        })
        self.assert_bypassed("usage_ceiling")

    def test_pool_per_account(self):
        self.runtime.pools = [QuotaPool("sol", ["gpt-*"], per_account=1)]
        self.record_job()
        self.assert_bypassed("per_account in pool sol")

    def test_pool_total(self):
        self.runtime.pools = [QuotaPool("sol", ["gpt-*"], total=1)]
        self.record_job()
        self.assert_bypassed("total in pool sol")

    def test_pool_weekly_budget(self):
        self.runtime.pools = [QuotaPool("sol", ["gpt-*"], weekly_jobs=1)]
        self.record_job(active=False)
        self.assert_bypassed("weekly_jobs in pool sol")
        lease, _, _ = accounts.acquire(self.runtime, model=MODEL, override=WORDS)
        self.assertIsNotNone(lease)
        lease.release()

    def test_account_and_pool_cooldowns_still_refuse_in_queue_mode(self):
        self.runtime.when_full = "queue"
        self.runtime.pools = [QuotaPool("sol", ["gpt-*"], weekly_jobs=1)]
        for pool in (None, "sol"):
            with self.subTest(pool=pool):
                accounts.clear_cooldown("codex", "primary")
                if pool:
                    accounts.set_pool_cooldown("codex", "primary", pool, time.time() + 3600, "quota error")
                else:
                    accounts.set_cooldown("codex", "primary", time.time() + 3600, "quota error")
                with self.assertRaisesRegex(limits.LimitError, "usage limit until") as caught:
                    limits.admit(self.config, "codex", model=MODEL, override=WORDS)
                self.assertNotIn("override=", str(caught.exception))
                lease, until, busy = accounts.acquire(self.runtime, model=MODEL, override=WORDS)
                self.assertIsNone(lease)
                self.assertIsNotNone(until)
                self.assertFalse(busy)

    def test_models_list_still_filters_at_admission_and_execution(self):
        self.runtime.primary_models = [MODEL]
        self.runtime.default_model = "gpt-6-astra"
        for model in (None, "gpt-6-astra"):
            with self.subTest(model=model):
                with self.assertRaisesRegex(limits.LimitError, "No codex account allows model"):
                    limits.admit(self.config, "codex", model=model, override=WORDS)
                lease, _, _ = accounts.acquire(self.runtime, model=model, override=WORDS)
                self.assertIsNone(lease)

    def test_plan_and_explicit_effort_restrictions_still_apply(self):
        self.runtime.plan = "free"
        for explicit, expected in ((None, "high"), ("medium", "medium")):
            with self.subTest(explicit=explicit):
                self.runtime.primary_max_effort = explicit
                admission = limits.admit(self.config, "codex", effort="max", override=WORDS)
                self.assertEqual(admission.effort, expected)
                self.assertIn(f"effort lowered from max to {expected}", admission.warnings[0])

    def test_empty_and_non_string_values_are_rejected_without_a_record(self):
        for value in ("", " \n\t", False, 5, []):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "override must be a non-empty string"):
                    jobs.create_job(runtime="codex", cwd=str(self.project), prompt="x", override=value, start=False)
                with self.assertRaisesRegex(tools.ToolError, "override must be a non-empty string"):
                    tools.call("subagent_start", {"runtime": "codex", "cwd": str(self.project), "prompt": "x", "override": value})
        self.assertFalse(jobs.jobs_dir().exists())

    def test_depth_and_access_validation_still_apply(self):
        with patch.dict(os.environ, {"CLI_SUBAGENTS_DEPTH": str(self.config.max_depth)}):
            with self.assertRaisesRegex(ValueError, "nested delegation is disabled"):
                jobs.create_job(runtime="codex", prompt="x", cwd=str(self.project), override=WORDS)
        with self.assertRaisesRegex(ValueError, "access must be"):
            jobs.create_job(runtime="codex", prompt="x", cwd=str(self.project), access="invalid", override=WORDS)

    def test_account_overflow_counts_when_a_lower_slot_is_released(self):
        self.runtime.max_jobs_per_account = 1
        account = self.runtime.find("primary")
        first = accounts.try_lease(self.runtime, account)
        second = accounts.try_lease(self.runtime, account, override=WORDS)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        try:
            self.assertEqual(accounts.busy_slots(self.runtime, account), 2)
            first.release()
            first = None
            self.assertIsNone(accounts.try_lease(self.runtime, account))
        finally:
            if first:
                first.release()
            second.release()
        again = accounts.try_lease(self.runtime, account)
        self.assertIsNotNone(again)
        again.release()

    def test_pool_overflow_slots_count_after_other_jobs_finish(self):
        self.runtime.max_jobs_per_account = 10
        for field in ("per_account", "total"):
            with self.subTest(field=field):
                self.runtime.pools = [QuotaPool("sol", ["gpt-*"], **{field: 1})]
                first, _, _ = accounts.acquire(self.runtime, model=MODEL)
                second, _, _ = accounts.acquire(self.runtime, model=MODEL, override=WORDS)
                self.assertIsNotNone(first)
                self.assertIsNotNone(second)
                first.release()
                try:
                    blocked, _, busy = accounts.acquire(self.runtime, model=MODEL)
                    self.assertIsNone(blocked)
                    self.assertTrue(busy)
                finally:
                    second.release()

    def test_execution_overflow_counts_for_global_runtime_and_quota_caps(self):
        for kind in ("global", "runtime", "quota"):
            with self.subTest(kind=kind):
                self.config.max_jobs = 1 if kind == "global" else None
                self.runtime.max_jobs = 1 if kind == "runtime" else None
                self.runtime.max_jobs_by_room = [[0, 1]] if kind == "quota" else []
                first = limits.acquire_execution_slots(self.config, "codex", MODEL)
                second = limits.acquire_execution_slots(self.config, "codex", MODEL, override=WORDS)
                self.assertIsNotNone(first)
                self.assertIsNotNone(second)
                for handle, _ in first:
                    unlock(handle)
                try:
                    self.assertIsNone(limits.acquire_execution_slots(self.config, "codex", MODEL))
                finally:
                    for handle, _ in second:
                        unlock(handle)
        self.runtime.max_jobs_by_room = [[0, 0]]
        slots = limits.acquire_execution_slots(self.config, "codex", MODEL, override=WORDS)
        self.assertIsNotNone(slots)
        for handle, _ in slots:
            unlock(handle)

    def test_runner_skips_all_cap_queueing_but_rechecks_effort(self):
        self.config.max_jobs = self.runtime.max_jobs = self.runtime.primary_max_jobs = 1
        self.runtime.when_full = "queue"
        self.runtime.max_jobs_by_room = [[0, 0]]
        self.runtime.pools = [QuotaPool("sol", ["gpt-*"], per_account=1, total=1, weekly_jobs=1)]
        save_config(self.config)
        self.record_job()
        held, _, _ = accounts.acquire(self.runtime, model=MODEL, override=WORDS)
        status = jobs.create_job(runtime="codex", prompt="x", cwd=str(self.project), effort="max", override=WORDS, start=False)
        # A restriction introduced after admission must still apply at execution.
        self.runtime.primary_max_effort = "medium"
        save_config(self.config)
        try:
            with patch("cli_subagents.runner.time.sleep", side_effect=AssertionError("override queued")):
                self.assertEqual(Runner(status["job_id"]).run(), 0)
        finally:
            held.release()
        result = jobs.read_result(status["job_id"])
        self.assertEqual(result["outcome"], "success")
        self.assertEqual(result["override"], WORDS)
        self.assertIn("model_reasoning_effort=medium", self.records()[-1]["argv"])

    def test_login_error_is_preserved(self):
        status = jobs.create_job(runtime="claude", prompt="x", cwd=str(self.project), override=WORDS, start=False)
        with patch.dict(os.environ, {"FAKE_CLAUDE_SCENARIO": "auth"}):
            self.assertEqual(Runner(status["job_id"]).run(), 1)
        self.assertEqual(jobs.read_status(status["job_id"])["outcome"], "auth")

    def test_override_is_stored_displayed_and_counted_against_later_starts(self):
        self.runtime.primary_max_jobs = 1
        save_config(self.config)
        with patch.object(jobs, "spawn_detached", return_value=os.getpid()):
            status = tools.call("subagent_start", {
                "runtime": "codex", "prompt": "x", "cwd": str(self.project), "override": WORDS,
            })
        job_id = status["job_id"]
        self.records_to_close.append(job_id)
        self.assertEqual(status["override"], WORDS)
        self.assertEqual(jobs.read_spec(job_id).override, WORDS)
        self.assertEqual(jobs.read_status(job_id)["override"], WORDS)
        self.assertEqual(tools.call("subagent_status", {"job_id": job_id})["override"], WORDS)
        self.assertEqual(tools.call("subagent_result", {"job_id": job_id})["override"], WORDS)
        self.assertEqual(jobs.list_jobs()[0]["override"], WORDS)
        with self.assertRaises(limits.LimitError):
            jobs.create_job(runtime="codex", prompt="x", cwd=str(self.project))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli.cmd_jobs(argparse.Namespace(limit=20))
        self.assertIn(f'override="{WORDS}"', output.getvalue())
        output = io.StringIO()
        follow(job_id, max_wait=0, out=output)
        self.assertIn(f'override="{WORDS}"', output.getvalue().splitlines()[0])

    def test_cli_start_and_run_pass_override_and_follow_shows_it(self):
        for command in ("start", "run"):
            with self.subTest(command=command):
                args = cli.build_parser().parse_args([command, "--override", WORDS, "x", "--cwd", str(self.project)])
                with patch.object(jobs, "create_job", return_value={"job_id": "j"}) as create:
                    cli._start(args)
                self.assertEqual(create.call_args.kwargs["override"], WORDS)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.main(["run", "--follow", "--runtime", "codex", "--cwd", str(self.project), "--override", WORDS, "x"])
        self.assertEqual(code, 0)
        self.assertIn(f'override="{WORDS}"', output.getvalue().splitlines()[0])

    def test_finished_job_follow_up_does_not_inherit_override(self):
        status = jobs.create_job(runtime="codex", prompt="x", cwd=str(self.project), override=WORDS, start=False)
        jobs.update_status(status["job_id"], state="done", session_id="s", account="primary")
        with patch.object(jobs, "create_job", return_value={"job_id": "continued"}) as create:
            tools.call("subagent_message", {"job_id": status["job_id"], "text": "continue"})
        self.assertIsNone(create.call_args.kwargs.get("override"))

    def test_schema_and_all_relays_document_user_authorization(self):
        schema = next(item for item in tools.DEFINITIONS if item["name"] == "subagent_start")["inputSchema"]
        override = schema["properties"]["override"]
        self.assertEqual((override["type"], override["minLength"], override["pattern"]), ("string", 1, r"\S"))
        for path in (HERE.parent / "agents").glob("cli-subagents-*.md"):
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertIn("`override=...`", text)
                self.assertIn("--relay-file", text)
                self.assertIn("csa reads the complete value literally from the file", text)
                self.assertIn("Never set override on your own judgment", text)
                self.assertIn("how much of the 5-hour window is left", text)
