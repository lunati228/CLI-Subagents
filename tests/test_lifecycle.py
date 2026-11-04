"""Delivery, wake-up hooks, worker inbox, progress pings, Codex config merge, cooldowns and sync safety."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import tomllib
import unittest
from unittest.mock import patch

from claim_racer import race
from helpers import TempHome

from cli_subagents import accounts, cli, hooks, jobs, messages, sync, tools
from cli_subagents.config import load_config
from cli_subagents.fsutil import write_json
from cli_subagents.runtimes.base import JobSpec
from cli_subagents.runtimes.claude import DROP_SETTINGS, SYNC_PLAN, ClaudeRuntime, _project_dir_name
from cli_subagents.runtimes.codex import CodexRuntime, merge_config

SRC = Path(__file__).resolve().parents[1] / "src"
SHIM = SRC / "cli_subagents" / "hook_shim.py"
FAKE_CODEX = Path(__file__).with_name("fake_codex.py")
RACER = Path(__file__).with_name("claim_racer.py")


def tool_response(job_id):
    """A subagent_start result the way an MCP host hands it to a PostToolUse hook."""

    return [{"type": "text", "text": json.dumps({"job_id": job_id, "state": "queued"}, indent=2)}]


class LifecycleBase(TempHome):
    def setUp(self):
        super().setUp()
        os.environ.pop("FAKE_CLAUDE_DELAY", None)
        self.write_config(accounts=("a1",))

    def tearDown(self):
        os.environ.pop("FAKE_CLAUDE_DELAY", None)
        super().tearDown()

    def start(self, prompt="task", **kwargs):
        return jobs.create_job(runtime="claude", prompt=prompt, cwd=str(self.project), **kwargs)["job_id"]

    def finished(self, **kwargs):
        job = self.start(**kwargs)
        status = jobs.wait_for(job, 60)
        self.assertEqual(status["state"], "done", status)
        return job


class DeliveryTests(LifecycleBase):
    """Item 2: each finished result is delivered once."""

    def test_finished_job_is_pending_and_claimed_once(self):
        job = self.finished()
        self.assertTrue(jobs.is_pending(job))
        self.assertTrue(jobs.claim_delivery(job))
        self.assertFalse(jobs.claim_delivery(job))
        self.assertFalse(jobs.is_pending(job))

    def test_racing_claims_have_one_winner(self):
        job = self.finished()
        wins = []
        threads = [threading.Thread(target=lambda: wins.append(jobs.claim_delivery(job))) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(wins.count(True), 1)

    def test_claims_racing_across_threads_and_processes_have_one_winner(self):
        # Every racer claims each job at the same moment: 3 processes and this one, 4 threads each.
        # Odd jobs have no marker (their runner died), so they go through the exclusive create.
        job_ids = [f"race-{index:03d}" for index in range(40)]
        for index, job_id in enumerate(job_ids):
            write_json(jobs.job_dir(job_id) / "status.json", {"job_id": job_id, "state": "done", "updated_at": time.time()})
            if index % 2 == 0:
                jobs.mark_pending(job_id)
        slot = 0.02
        racers = [
            subprocess.Popen(
                [sys.executable, str(RACER), "4", str(slot), *job_ids],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8",
            )
            for _ in range(3)
        ]
        try:
            for racer in racers:
                self.assertEqual(racer.stdout.readline().strip(), "ready")
            start = time.time() + 0.1
            for racer in racers:
                racer.stdin.write(f"{start}\n")
                racer.stdin.flush()
            wins = race(job_ids, 4, start, slot)
            for racer in racers:
                output, _ = racer.communicate(timeout=60)
                wins += json.loads(output)
        finally:
            for racer in racers:
                if racer.poll() is None:
                    racer.kill()
        self.assertEqual({job_id: wins.count(job_id) for job_id in job_ids}, dict.fromkeys(job_ids, 1))
        self.assertFalse(any(jobs.is_pending(job_id) for job_id in job_ids))

    def test_result_and_wait_still_return_the_result_and_claim_it(self):
        job = self.finished()
        self.assertEqual(tools.call("subagent_result", {"job_id": job})["result"], "done on a1")
        self.assertFalse(jobs.is_pending(job))
        # Asking again still works.
        report = tools.call("subagent_wait", {"job_ids": [job], "timeout_seconds": 5})
        self.assertEqual(report["finished"][0]["result"], "done on a1")
        # The hook stays silent: the model already has the result.
        self.assertEqual(hooks.await_job({"tool_response": tool_response(job)}), (0, ""))

    def test_job_without_a_marker_is_claimed_once(self):
        job = self.finished()
        (jobs.job_dir(job) / jobs.NOTIFY_PENDING).unlink()  # e.g. its runner died before finishing
        self.assertTrue(jobs.claim_delivery(job))
        self.assertFalse(jobs.claim_delivery(job))

    def test_running_job_cannot_be_claimed(self):
        os.environ["FAKE_CLAUDE_DELAY"] = "3"
        job = self.start()
        self.assertFalse(jobs.claim_delivery(job))
        jobs.wait_for(job, 60)
        self.assertTrue(jobs.claim_delivery(job))


class AwaitHookTests(LifecycleBase):
    """Item 1: `csa hook await` wakes the main session."""

    def setUp(self):
        super().setUp()
        self._poll = patch.object(hooks, "POLL_SECONDS", 0.2)
        self._poll.start()

    def tearDown(self):
        self._poll.stop()
        super().tearDown()

    def test_finds_the_job_id_in_every_shape(self):
        self.assertEqual(hooks.find_job_id(tool_response("j-1")), "j-1")
        self.assertEqual(hooks.find_job_id({"content": tool_response("j-2")}), "j-2")
        self.assertEqual(hooks.find_job_id(json.dumps({"job_id": "j-3"})), "j-3")
        self.assertEqual(hooks.find_job_id({"structuredContent": {"job_id": "j-4"}}), "j-4")
        self.assertIsNone(hooks.find_job_id({"content": [{"type": "text", "text": "no id here"}]}))

    def test_wakes_with_the_result_when_the_job_finishes(self):
        os.environ["FAKE_CLAUDE_DELAY"] = "2"
        job = self.start()
        code, text = hooks.await_job({"tool_response": tool_response(job)})
        self.assertEqual(code, 2)
        self.assertIn(job, text)
        self.assertIn("success", text)
        self.assertIn("done on a1", text)
        self.assertFalse(jobs.claim_delivery(job))  # the hook claimed it

    def test_result_preview_is_cut(self):
        job = self.finished()
        (jobs.job_dir(job) / "result.md").write_text("x" * 5000, encoding="utf-8")
        code, text = hooks.await_job({"tool_response": tool_response(job)})
        self.assertEqual(code, 2)
        self.assertIn("x" * 1500, text)
        self.assertNotIn("x" * 1501, text)
        self.assertIn("subagent_result", text)

    def test_wakes_for_a_question(self):
        os.environ["FAKE_CLAUDE_DELAY"] = "4"
        job = self.start()
        question = messages.post_from_worker(job, "Tabs or spaces?", kind="question")
        code, text = hooks.await_job({"tool_response": tool_response(job)})
        self.assertEqual(code, 2)
        self.assertIn(question["id"], text)
        self.assertIn("Tabs or spaces?", text)
        self.assertIn("subagent_message", text)
        jobs.wait_for(job, 60)

    def test_wakes_for_a_message(self):
        os.environ["FAKE_CLAUDE_DELAY"] = "4"
        job = self.start()
        note = messages.post_from_worker(job, "found the bug")
        code, text = hooks.await_job({"tool_response": tool_response(job)})
        self.assertEqual(code, 2)
        self.assertIn(f"[{note['id']}]: found the bug", text)
        self.assertFalse(messages.has_new_for_host(job))
        jobs.wait_for(job, 60)

    def test_unknown_or_missing_job_is_silent(self):
        self.assertEqual(hooks.await_job({"tool_response": tool_response("20990101-000000-abcdef")}), (0, ""))
        self.assertEqual(hooks.await_job({"tool_response": tool_response("../bad")}), (0, ""))
        self.assertEqual(hooks.await_job({}), (0, ""))

    def test_deadline_wakes_with_a_note(self):
        os.environ["FAKE_CLAUDE_DELAY"] = "5"
        job = self.start()
        code, text = hooks.await_job({"tool_response": tool_response(job)}, deadline_seconds=0.5)
        self.assertEqual(code, 2)
        self.assertIn("subagent_wait", text)
        jobs.wait_for(job, 60)

    def test_command_exits_2_and_prints_to_stderr(self):
        job = self.finished()
        payload = {"hook_event_name": "PostToolUse", "tool_name": "mcp__cli-subagents__subagent_start",
                   "tool_response": tool_response(job)}
        completed = subprocess.run(
            [sys.executable, "-m", "cli_subagents", "hook", "await"], input=json.dumps(payload),
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertIn("done on a1", completed.stderr)
        self.assertEqual(completed.stdout, "")
        again = subprocess.run(
            [sys.executable, "-m", "cli_subagents", "hook", "await"], input=json.dumps(payload),
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        self.assertEqual((again.returncode, again.stderr), (0, ""))

    def test_plugin_hooks_file(self):
        data = json.loads((Path(__file__).resolve().parents[1] / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        post = data["hooks"]["PostToolUse"][0]
        self.assertEqual(
            set(post["matcher"].split("|")),
            {"mcp__plugin_cli-subagents_cli-subagents__subagent_start", "mcp__cli-subagents__subagent_start"},
        )
        handler = post["hooks"][0]
        self.assertEqual(
            (handler["command"], handler["async"], handler["asyncRewake"], handler["timeout"]),
            ("csa hook await", True, True, 7200),
        )
        self.assertGreater(handler["timeout"], hooks.AWAIT_DEADLINE_SECONDS)
        start = data["hooks"]["SessionStart"][0]
        self.assertEqual((start["matcher"], start["hooks"][0]["command"]), ("resume|compact", "csa hook pending"))


class PendingHookTests(LifecycleBase):
    """Item 3: SessionStart names uncollected results for this project."""

    def test_lists_unclaimed_jobs_of_this_project_newest_first(self):
        first = self.finished()
        second = self.finished()
        collected = self.finished()
        jobs.claim_delivery(collected)
        other = self.root / "other"
        other.mkdir()
        elsewhere = jobs.wait_for(jobs.create_job(runtime="claude", prompt="t", cwd=str(other))["job_id"], 60)["job_id"]
        output = hooks.pending_context({"cwd": str(self.project), "source": "resume"})
        self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "SessionStart")
        text = output["hookSpecificOutput"]["additionalContext"]
        self.assertLess(text.index(second), text.index(first))
        self.assertNotIn(collected, text)
        self.assertNotIn(elsewhere, text)
        self.assertIn("subagent_result", text)

    def test_at_most_ten(self):
        for _ in range(11):
            self.finished()
        self.assertEqual(len(hooks.pending_jobs(str(self.project))), 10)

    def test_command_prints_json_or_nothing(self):
        run = lambda: subprocess.run(  # noqa: E731
            [sys.executable, "-m", "cli_subagents", "hook", "pending"], input=json.dumps({"cwd": str(self.project)}),
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        empty = run()
        self.assertEqual((empty.returncode, empty.stdout), (0, ""))
        job = self.finished()
        completed = run()
        self.assertEqual(completed.returncode, 0)
        self.assertIn(job, json.loads(completed.stdout)["hookSpecificOutput"]["additionalContext"])


class WorkerInboxTests(LifecycleBase):
    """Item 4: a Claude worker gets follow-ups through a PostToolUse hook."""

    def job_settings(self, job):
        argv = self.records()[-1]["argv"]
        path = Path(argv[argv.index("--settings") + 1])
        self.assertEqual(path, jobs.job_dir(job) / "settings.json")
        return json.loads(path.read_text(encoding="utf-8"))

    def test_hook_is_added_next_to_the_users_hooks(self):
        own = {"matcher": "Write", "hooks": [{"type": "command", "command": "echo mine"}]}
        stop = {"hooks": [{"type": "command", "command": "echo stop"}]}
        (self.primary / "settings.json").write_text(
            json.dumps({"hooks": {"PostToolUse": [own], "Stop": [stop]}, "apiKeyHelper": "x"}), encoding="utf-8"
        )
        job = self.finished()
        settings = self.job_settings(job)
        self.assertNotIn("apiKeyHelper", settings)
        self.assertEqual(settings["hooks"]["Stop"], [stop])
        post = settings["hooks"]["PostToolUse"]
        self.assertEqual(post[0], own)
        command = post[1]["hooks"][0]["command"]
        self.assertIn("hook_shim.py", command)
        self.assertIn(job, command)
        self.assertIn(str(self.home).replace("\\", "/"), command)
        self.assertNotIn("\\", command)

    def test_primary_account_worker_gets_the_hook_too(self):
        self.write_config(accounts=())
        job = self.finished()
        self.assertEqual(len(self.job_settings(job)["hooks"]["PostToolUse"]), 1)

    def run_shim(self, job):
        env = {key: value for key, value in os.environ.items() if key not in ("PYTHONPATH", "CLI_SUBAGENTS_HOME")}
        return subprocess.run(
            [sys.executable, str(SHIM), "--home", str(self.home).replace("\\", "/"), "hook", "inbox", job],
            input=json.dumps({"hook_event_name": "PostToolUse", "tool_name": "Read"}),
            capture_output=True, text=True, encoding="utf-8", env=env, cwd=str(self.project), timeout=60,
        )

    def test_shim_delivers_follow_ups_once_without_pythonpath(self):
        job = self.finished()
        self.assertEqual((self.run_shim(job).returncode, self.run_shim(job).stdout), (0, ""))
        messages.post_to_worker(job, "also update the docs")
        completed = self.run_shim(job)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads(completed.stdout)["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "PostToolUse")
        self.assertEqual(output["additionalContext"], "New instruction from the main session, apply it to your current task: also update the docs")
        self.assertEqual(self.run_shim(job).stdout, "")
        # Shown by the hook but not read explicitly, so the runner still re-delivers it.
        self.assertEqual([item["text"] for item in messages.take_for_worker(job)], ["also update the docs"])

    def test_shim_never_fails(self):
        for job in ("not a job id", "20990101-000000-abcdef"):
            completed = self.run_shim(job)
            self.assertEqual((completed.returncode, completed.stdout), (0, ""), completed.stderr)

    def test_hook_command_runs_through_the_shell(self):
        job = self.finished()
        messages.post_to_worker(job, "ping")
        entry = hooks.worker_inbox_hook(job, self.home)["hooks"][0]
        if entry.get("shell") == "powershell":
            argv = ["powershell", "-NoProfile", "-Command", entry["command"]]
        else:
            bash = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH") or ("bash" if os.name != "nt" else None)
            if os.name == "nt" and not bash:
                for candidate in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe"):
                    if Path(candidate).is_file():
                        bash = candidate
                        break
            if not bash:
                self.skipTest("no bash to run the hook command with")
            argv = [bash, "-c", entry["command"]]
        completed = subprocess.run(argv, input="{}", capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("ping", completed.stdout)

    def test_shell_command_quoting(self):
        plain = hooks.shell_command(["C:/Python/python.exe", "C:/a b/hook_shim.py", "x"])
        spaced = hooks.shell_command(["C:/Program Files/Python/python.exe", "x"])
        dollar = hooks.shell_command(["C:/py/python.exe", "C:/a$b/x"])
        if os.name == "nt":
            self.assertEqual(plain, {"type": "command", "command": 'C:/Python/python.exe "C:/a b/hook_shim.py" "x"'})
            self.assertEqual(spaced["shell"], "powershell")
            self.assertTrue(spaced["command"].startswith('& "C:/Program Files/Python/python.exe"'))
            self.assertEqual(dollar["command"], "& 'C:/py/python.exe' 'C:/a$b/x'")
        else:
            self.assertEqual(spaced["command"], "'C:/Program Files/Python/python.exe' x")
            self.assertEqual(dollar["command"], "C:/py/python.exe 'C:/a$b/x'")


class ProgressPingTests(LifecycleBase):
    """Item 5: subagent_wait sends useful progress every 3 seconds."""

    def test_pings_name_state_tool_and_text(self):
        os.environ["FAKE_CLAUDE_DELAY"] = "5"
        job = self.start()
        pings = []
        with patch.object(tools, "WAIT_PING_SECONDS", 0.5):
            tools.wait_any([job], 60, lambda count, message: pings.append(message))
        self.assertTrue(pings)
        self.assertTrue(all(message.startswith(f"{job}: ") for message in pings), pings)
        self.assertTrue(any("running" in message for message in pings), pings)
        self.assertEqual(tools.WAIT_PING_SECONDS, 3)

    def test_message_includes_last_tool_and_text(self):
        job = self.finished()
        jobs.update_status(job, state="running", progress={"tool_uses": 4, "last_tool": "Grep", "last_text": "Looking at  the\nparser " + "y" * 300})
        message = tools.progress_message([job])
        self.assertIn("running", message)
        self.assertIn("last tool Grep", message)
        self.assertIn('"Looking at the parser y', message)
        self.assertLess(len(message), 300)


class CodexConfigMergeTests(TempHome):
    """Item 6: the account's own trust and notice tables survive the config sync."""

    PRIMARY = 'model = "gpt-new"\n\n[mcp_servers.docs]\nurl = "https://docs"\n\n[projects."/shared"]\ntrust_level = "trusted"\n'

    def setUp(self):
        super().setUp()
        os.environ["CLI_SUBAGENTS_CODEX_COMMAND"] = json.dumps([sys.executable, str(FAKE_CODEX)])
        (self.primary / "config.toml").write_text(self.PRIMARY, encoding="utf-8")
        self.account_home = self.root / "codex" / "c1"
        self.account_home.mkdir(parents=True)
        (self.home / "config.toml").write_text(
            f"[codex]\nprimary = {json.dumps(str(self.primary))}\n[[codex.accounts]]\nname = \"c1\"\n"
            f"home = {json.dumps(str(self.account_home))}\n",
            encoding="utf-8",
        )

    def tearDown(self):
        os.environ.pop("CLI_SUBAGENTS_CODEX_COMMAND", None)
        super().tearDown()

    def prepare(self):
        config = load_config().runtime("codex")
        spec = JobSpec(job_id="t", runtime="codex", prompt="", cwd=str(self.project))
        return CodexRuntime().prepare(config, config.find("c1"), spec)

    def test_account_tables_are_kept_and_the_rest_replaced(self):
        (self.account_home / "config.toml").write_text(
            'model = "gpt-old"\nnote = """\n[projects.fake]\n"""\n\n[mcp_servers.docs]\ncommand = "stale"\n\n'
            "[projects.'C:\\\\work']\ntrust_level = \"trusted\"\n\n[projects.\"/shared\"]\ntrust_level = \"untrusted\"\n\n"
            "[notice]\nhide_full_access_warning = true\n\n[hooks.state.abc]\nenabled = false\n",
            encoding="utf-8",
        )
        report = self.prepare()
        text = (self.account_home / "config.toml").read_text(encoding="utf-8")
        data = tomllib.loads(text)
        self.assertEqual(data["model"], "gpt-new")
        self.assertNotIn("note", data)
        self.assertEqual(data["mcp_servers"]["docs"], {"url": "https://docs"})  # replaced as a whole
        self.assertEqual(data["projects"]["C:\\\\work"]["trust_level"], "trusted")
        self.assertEqual(data["projects"]["/shared"]["trust_level"], "untrusted")  # the account's own wins
        self.assertTrue(data["notice"]["hide_full_access_warning"])
        self.assertFalse(data["hooks"]["state"]["abc"]["enabled"])
        self.assertTrue((self.account_home / "config.toml.before-cli-subagents").exists())
        self.assertFalse(any("could not keep" in item for item in report["warnings"]), report)
        self.assertEqual(self.prepare()["copied"], [])  # stable: nothing to rewrite on the next run

    def test_unparseable_merge_falls_back_to_the_primary(self):
        (self.primary / "config.toml").write_text(self.PRIMARY + '\n[notice.hide_full_access_warning]\nx = 1\n', encoding="utf-8")
        (self.account_home / "config.toml").write_text("[notice]\nhide_full_access_warning = true\n", encoding="utf-8")
        report = self.prepare()
        self.assertEqual((self.account_home / "config.toml").read_text(encoding="utf-8"), (self.primary / "config.toml").read_text(encoding="utf-8"))
        self.assertTrue(any("could not keep" in item for item in report["warnings"]), report)
        self.assertTrue((self.account_home / "config.toml.before-cli-subagents").exists())

    def test_merge_without_account_tables_is_the_primary(self):
        self.assertEqual(merge_config(self.PRIMARY, 'model = "x"\n[mcp_servers.y]\ncommand = "z"\n'), self.PRIMARY)


class SettingsTests(unittest.TestCase):
    """Item 7."""

    def test_more_auth_settings_are_dropped_and_workflows_shared(self):
        for key in ("proxyAuthHelper", "gcpAuthRefresh", "otelHeadersHelper", "forceLoginGatewayUrl", "apiKeyHelper"):
            self.assertIn(key, DROP_SETTINGS)
        self.assertIn("workflows", SYNC_PLAN.whole)


class CooldownTests(unittest.TestCase):
    """Item 8: Retry-After, then the reset time, then the configured cooldown, capped at 7 days."""

    NOW = 1_000_000.0

    def test_order_and_cap(self):
        end = lambda text, resets=None: accounts.cooldown_end(text, resets, 60, now=self.NOW) - self.NOW  # noqa: E731
        self.assertEqual(end("429 Too Many Requests\nRetry-After: 120", self.NOW + 9999), 120)
        self.assertEqual(end("Please retry after 30 seconds."), 30)
        self.assertEqual(end("retry after 5 minutes"), 300)
        self.assertEqual(end("usage limit reached", self.NOW + 500), 500)
        self.assertEqual(end("usage limit reached", self.NOW - 10), 3600)  # a reset time in the past is ignored
        self.assertEqual(end("usage limit reached"), 3600)
        self.assertEqual(end("Retry-After: 99999999"), 7 * 86400)
        self.assertEqual(end("", self.NOW + 30 * 86400), 7 * 86400)

    def test_the_time_the_message_names_wins_over_the_default(self):
        from datetime import datetime

        # Seen live: Codex said 5:34 PM at 17:31, and csa waited until 18:31.
        now = datetime(2026, 11, 4, 17, 31, 47).timestamp()
        text = "You've hit your usage limit. Upgrade to Pro or try again at 5:34 PM."
        self.assertEqual(accounts.cooldown_end(text, None, 60, now=now), datetime(2026, 11, 4, 17, 34).timestamp())
        weekly = "You've hit your usage limit. Try again at Nov 8th, 2026 9:10 PM."
        self.assertEqual(accounts.cooldown_end(weekly, None, 60, now=now), datetime(2026, 11, 8, 21, 10).timestamp())
        # A clock time already past is tomorrow's; a 24-hour time works too.
        self.assertEqual(accounts.stated_reset("try again at 9:05 AM", now), datetime(2026, 11, 5, 9, 5).timestamp())
        self.assertEqual(accounts.stated_reset("try again at 18:00", now), datetime(2026, 11, 4, 18, 0).timestamp())
        self.assertIsNone(accounts.stated_reset("try again later", now))
        self.assertIsNone(accounts.stated_reset("try again at 13:00 PM", now))

    def test_a_long_cooldown_lets_a_start_try_again_after_the_probe_time(self):
        record = {"cooldown_until": self.NOW + 3 * 86400, "cooldown_probe_at": self.NOW + 3600}
        self.assertEqual(accounts.cooldown_until(record, now=self.NOW + 60), self.NOW + 3 * 86400)
        self.assertIsNone(accounts.cooldown_until(record, now=self.NOW + 3600))
        self.assertIsNone(accounts.cooldown_until({"cooldown_until": self.NOW + 60}, now=self.NOW + 61))


class CooldownRunnerTests(TempHome):
    def test_a_probe_can_start_when_stale_usage_selects_a_zero_job_step(self):
        from cli_subagents import limits
        from cli_subagents.config import QuotaPool

        self.write_config(accounts=("a1", "a2"), extra="usage_ceiling = 1.0\nmax_jobs_by_room = [[10, 1], [0, 0]]")
        config = load_config()
        config.runtime("claude").pools = [QuotaPool("p", ["*"])]
        now = time.time()
        accounts.update_usage("claude", "a1", windows={"weekly": {
            "utilization": 1.0, "resets_at": now + 3 * 86400}})
        accounts.update_usage("claude", "a2", windows={"weekly": {
            "utilization": 1.0, "resets_at": now + 3 * 86400}})
        for pool in (None, "p"):
            with self.subTest(pool=pool):
                accounts.clear_cooldown("claude", "a1")
                if pool:
                    accounts.set_pool_cooldown("claude", "a1", pool, now + 3 * 86400, "limited", probe_at=now - 1)
                else:
                    accounts.set_cooldown("claude", "a1", now + 3 * 86400, "limited", probe_at=now - 1)
                self.assertEqual(limits.admit(config, "claude", account="a1", model="test-model").account, "a1")
                self.assertEqual(limits.admit(config, "claude", model="test-model").account, "a1")

    def test_a_probe_can_start_despite_the_last_full_usage_window(self):
        self.write_config(accounts=("a1",), failover="off", extra="usage_ceiling = 1.0\ncooldown_minutes = 1")
        now = time.time()
        accounts.update_usage("claude", "a1", windows={"weekly": {
            "utilization": 1.0, "resets_at": now + 3 * 86400}})
        accounts.set_cooldown("claude", "a1", now + 3 * 86400, "limited", probe_at=now - 1)
        config = load_config()
        from cli_subagents import limits

        self.assertEqual(limits.admit(config, "claude", account="a1").account, "a1")
        from cli_subagents.runner import Runner

        job = jobs.create_job(runtime="claude", prompt="probe", cwd=str(self.project), account="a1", start=False)
        with patch.dict(os.environ, {"FAKE_CLAUDE_LIMITED": "a1"}):
            self.assertEqual(Runner(job["job_id"]).run(), 1)
        record = accounts.load_usage("claude")["a1"]
        self.assertGreater(record["cooldown_probe_at"], now)
        with self.assertRaises(limits.LimitError):
            limits.admit(config, "claude", account="a1")

    def test_runner_uses_the_reset_time_of_the_event(self):
        self.write_config(accounts=("a1", "a2"))
        os.environ["FAKE_CLAUDE_LIMITED"] = "a1"
        try:
            before = time.time()
            status = jobs.wait_for(jobs.create_job(runtime="claude", prompt="t", cwd=str(self.project))["job_id"], 60)
        finally:
            os.environ.pop("FAKE_CLAUDE_LIMITED", None)
        self.assertEqual(status["outcome"], "success", status)
        until = accounts.load_usage("claude")["a1"]["cooldown_until"]
        self.assertAlmostEqual(until, before + 3600, delta=30)


class DoctorCopiesTests(LifecycleBase):
    """Item 9: doctor names shared entries that fell back to copies."""

    def test_lists_copies_per_account(self):
        account = self.root / "homes" / "a1"
        account.mkdir(parents=True)
        (account / sync.MANIFEST).write_text(json.dumps({"version": 2, "items": {
            "CLAUDE.md": {"kind": "copy", "sha256": "x"},
            "agents": {"kind": "link"},
            "skills/alpha": {"kind": "copydir"},
        }}), encoding="utf-8")
        self.assertEqual(sync.fallback_copies(account), ["CLAUDE.md", "skills/alpha"])
        output = io.StringIO()
        # No binary calls: `claude --version` runs without an account folder and would write into the cwd.
        report = {"runtime": "claude", "binary": "claude", "accounts": [{"name": "a1", "auth": {"loggedIn": True}}]}
        with contextlib.redirect_stdout(output), patch.object(tools, "account_report", return_value=report):
            cli.cmd_doctor(cli.build_parser().parse_args(["doctor"]))
        self.assertIn("account 'a1' has copies instead of links", output.getvalue())
        self.assertIn("CLAUDE.md, skills/alpha", output.getvalue())


class SyncKeepsAccountDataTests(TempHome):
    """Item 10: syncing never deletes an account's own project memory or session transcripts."""

    def test_memory_and_transcripts_survive_every_sync(self):
        self.write_config(accounts=("a1",))
        account = self.root / "homes" / "a1"
        name = _project_dir_name(str(self.project))
        memory = account / "projects" / name / "memory"
        memory.mkdir(parents=True)
        (memory / "MEMORY.md").write_text("account memory", encoding="utf-8")
        transcript = account / "projects" / name / "11111111-2222-3333-4444-555555555555.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")
        other = account / "projects" / "other-project" / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"
        other.parent.mkdir(parents=True)
        other.write_text("{}\n", encoding="utf-8")
        (self.primary / "projects" / name / "memory").mkdir(parents=True)
        (self.primary / "agents").mkdir()
        # A manifest that claims the account's projects as managed entries must be ignored.
        (account / sync.MANIFEST).write_text(json.dumps({"version": 2, "items": {
            "projects": {"kind": "link"},
            f"projects/{name}": {"kind": "copydir", "signature": "x"},
            f"projects/{name}/memory": {"kind": "copydir", "signature": "x"},
            "agents/../projects": {"kind": "link"},
        }}), encoding="utf-8")
        config = load_config().runtime("claude")
        runtime = ClaudeRuntime()
        spec = JobSpec(job_id="t", runtime="claude", prompt="", cwd=str(self.project))
        for _ in range(2):
            report = runtime.prepare(config, config.find("a1"), spec)
            self.assertTrue(any("own memory" in item for item in report["conflicts"]), report)
        for _ in range(2):  # the worker runs too, transcripts included
            status = jobs.wait_for(jobs.create_job(runtime="claude", prompt="t", cwd=str(self.project))["job_id"], 60)
            self.assertEqual(status["outcome"], "success", status)
        self.assertEqual((memory / "MEMORY.md").read_text(encoding="utf-8"), "account memory")
        self.assertTrue(transcript.is_file())
        self.assertTrue(other.is_file())
        self.assertEqual(len(list((account / "projects" / name).glob("*.jsonl"))), 3)


if __name__ == "__main__":
    unittest.main()
