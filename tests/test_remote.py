"""The remote runtime end to end: csa's runner, the relay and bridge on the "PC", fake ssh, the
real server module and a fake Claude Code on the "server", all on this machine."""

import json
import os
from pathlib import Path
import sys
import threading
import time
import unittest

from helpers import TempHome

from cli_subagents import accounts, jobs, messages, tools
from cli_subagents.config import load_config
from cli_subagents.runtimes import get_runtime, offered_runtimes

HERE = Path(__file__).resolve().parent
FAKE_SSH = HERE / "fake_ssh.py"
FAKE_REMOTE_CLAUDE = HERE / "fake_remote_claude.py"


def mcp(server: str, tool: str, arguments: dict) -> str:
    return f"MCP: {server} {tool} {json.dumps(arguments)}"


class RemoteHome(TempHome):
    def setUp(self):
        super().setUp()
        self.server_state = self.root / "server-state"
        self.server_accounts = self.root / "server-accounts"
        self.server_config = self.root / "server.toml"
        lines = [
            "[server]",
            f"state_dir = {json.dumps(str(self.server_state))}",
            f"claude = {json.dumps([sys.executable, str(FAKE_REMOTE_CLAUDE)])}",
            "transcript_days = 7",
        ]
        for name, owner in (("a1", "alice"), ("a2", "alice"), ("b1", "bob")):
            lines += ["", "[[account]]", f'name = "{name}"', f'owner = "{owner}"',
                      f"home = {json.dumps(str(self.server_accounts / name))}"]
            # The server refuses account folders other users could read (POSIX).
            (self.server_accounts / name).mkdir(parents=True)
            os.chmod(self.server_accounts / name, 0o700)
        self.server_config.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.ssh_record = self.root / "ssh.jsonl"
        os.environ["CLI_SUBAGENTS_SSH_COMMAND"] = json.dumps(
            [sys.executable, str(FAKE_SSH), "--server-config", str(self.server_config), "--user", "alice"])
        os.environ["FAKE_SSH_RECORD"] = str(self.ssh_record)
        (self.primary / "skills" / "helper").mkdir(parents=True)
        (self.primary / "skills" / "helper" / "SKILL.md").write_text("---\nname: helper\n---\nhelp", encoding="utf-8")
        (self.project / "notes.txt").write_text("hello from the PC\n", encoding="utf-8")
        self.write_remote_config()

    def write_remote_config(self, accounts=("a1", "a2", "b1"), server="alice@test-server", extra=""):
        lines = ["[claude]", f"primary = {json.dumps(str(self.primary))}", "", "[remote]",
                 f"primary = {json.dumps(str(self.primary))}"]
        if server:
            lines.append(f"server = {json.dumps(server)}")
        lines.append(extra)
        for name in accounts:
            lines += ["[[remote.accounts]]", f'name = "{name}"']
        (self.home / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def start(self, prompt, **kwargs):
        kwargs.setdefault("account", "a1")
        return jobs.create_job(runtime="remote", prompt=prompt, cwd=str(self.project), **kwargs)["job_id"]

    def wait(self, job_id, seconds=60):
        status = jobs.wait_for(job_id, seconds)
        self.assertEqual(status.get("state"), "done", status)
        return status

    def events(self, job_id):
        path = jobs.job_dir(job_id) / "events.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def relay_events(self, job_id, what):
        return [event for event in self.events(job_id) if event.get("type") == "csa.remote" and event.get("event") == what]

    def server_calls(self, account):
        path = self.server_accounts / account / "fake-calls.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def server_prompts(self, account):
        path = self.server_accounts / account / "fake-prompts.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def result(self, job_id):
        return jobs.read_result(job_id)["result"] or ""


class RemoteJobTests(RemoteHome):
    def test_job_runs_on_the_named_account_with_pc_tools_and_the_setup(self):
        job = self.start(mcp("pc", "pc_read", {"path": "notes.txt"}) + "\n" + mcp("pc", "pc_write", {"path": "out.txt", "content": "made remotely"}))
        status = self.wait(job)
        self.assertEqual(status["outcome"], "success", status)
        self.assertEqual(status["account"], "a1")
        self.assertIn("hello from the PC", self.result(job))
        self.assertEqual((self.project / "out.txt").read_text(encoding="utf-8"), "made remotely")
        accepted = self.relay_events(job, "accepted")
        self.assertEqual(accepted[0]["account"], "a1")
        self.assertEqual(status["session_id"], accepted[0]["session_id"])
        argv = [call["argv"] for call in self.server_calls("a1") if "--input-format" in call["argv"]][0]
        self.assertIn("--disallowedTools", argv)
        self.assertIn("--strict-mcp-config", argv)
        self.assertFalse(any(call["oauth_env"] for call in self.server_calls("a1")))
        # The setup bundle went to the server and its skill reached the account folder.
        self.assertTrue((self.server_accounts / "a1" / "skills" / "helper" / "SKILL.md").is_file())
        # The worker note went in front of the task, once.
        self.assertIn("<csa_worker_note>", self.server_prompts("a1")[0]["text"])
        # The ssh call carried the reverse forward to the bridge.
        ssh = [json.loads(line)["argv"] for line in self.ssh_record.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any("-R" in argv and argv[-1] == "job" for argv in ssh))
        self.assertFalse((jobs.job_dir(job) / "remote" / "bundle.zip").exists())

    def test_read_only_job_reads_but_cannot_write_or_run(self):
        job = self.start("\n".join([
            mcp("pc", "pc_read", {"path": "notes.txt"}),
            mcp("pc", "pc_write", {"path": "out.txt", "content": "nope"}),
            mcp("pc", "pc_run", {"command": "echo hi"}),
        ]), access="read")
        status = self.wait(job)
        self.assertEqual(status["outcome"], "success", status)
        parts = self.result(job).split(" | ")
        self.assertIn("hello from the PC", parts[0])
        self.assertTrue(parts[1].startswith("ERROR"), parts[1])
        self.assertTrue(parts[2].startswith("ERROR"), parts[2])
        self.assertFalse((self.project / "out.txt").exists())

    def test_worker_messages_and_asks_the_main_session(self):
        job = self.start("\n".join([
            mcp("csa-parent", "message_parent", {"text": "halfway there"}),
            mcp("csa-parent", "ask_parent", {"question": "Which colour?", "timeout_seconds": 30}),
        ]))
        deadline = time.monotonic() + 40
        question = None
        while time.monotonic() < deadline and question is None:
            questions = messages.open_questions(job)
            question = questions[0] if questions else None
            time.sleep(0.2)
        self.assertIsNotNone(question, "the worker's question never reached the main session")
        tools.call("subagent_message", {"job_id": job, "text": "blue", "reply_to": question["id"]})
        status = self.wait(job)
        self.assertEqual(status["outcome"], "success", status)
        self.assertIn("halfway there", [item["text"] for item in messages.outbox(job)])
        self.assertIn('"answer": "blue"', self.result(job))

    def test_follow_up_goes_into_the_running_conversation(self):
        job = self.start("SLOW 4\nfirst task")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not (jobs.job_dir(job) / "events.jsonl").exists():
            time.sleep(0.1)
        while time.monotonic() < deadline and not self.relay_events(job, "accepted"):
            time.sleep(0.1)
        record = messages.queue_for_worker(job, "also say banana")
        self.assertIsNotNone(record)
        status = self.wait(job)
        self.assertEqual(status["outcome"], "success", status)
        self.assertEqual(len(status["attempts"]), 1)
        self.assertIn("banana", self.result(job))
        self.assertEqual([event["id"] for event in self.relay_events(job, "delivered")], [record["id"]])
        self.assertEqual(messages.undelivered(job), [])
        self.assertEqual(len(self.server_prompts("a1")), 2)

    def test_resume_continues_the_session_on_the_same_account(self):
        first = self.start("remember the word kiwi", account="a2")
        self.assertEqual(self.wait(first)["outcome"], "success")
        session = jobs.read_status(first)["session_id"]
        reply = tools.call("subagent_message", {"job_id": first, "text": "what was the word?"})
        second = reply["new_job"]["job_id"]
        status = self.wait(second)
        self.assertEqual(status["outcome"], "success", status)
        self.assertEqual(status["account"], "a2")
        self.assertTrue(self.relay_events(second, "accepted")[0]["resumed"])
        self.assertEqual(status["session_id"], session)
        prompts = self.server_prompts("a2")
        self.assertEqual({item["session_id"] for item in prompts}, {session})
        self.assertNotIn("<csa_worker_note>", prompts[-1]["text"])

    def test_resume_after_the_server_deleted_its_copy_sends_the_transcript(self):
        first = self.start("remember the word kiwi")
        self.wait(first)
        for path in (self.server_accounts / "a1" / "projects").glob("*/*.jsonl"):
            path.unlink()
        second = tools.call("subagent_message", {"job_id": first, "text": "what was the word?"})["new_job"]["job_id"]
        status = self.wait(second)
        self.assertEqual(status["outcome"], "success", status)
        self.assertEqual(status["account"], "a1")
        self.assertFalse(self.relay_events(second, "accepted")[0]["resumed"])
        last = self.server_prompts("a1")[-1]["text"]
        self.assertIn("Earlier conversation", last)
        self.assertIn("remember the word kiwi", last)
        self.assertIn("what was the word?", last)

    def test_an_account_of_another_person_is_refused(self):
        job = self.start("hello", account="b1")
        status = self.wait(job)
        self.assertEqual(status["outcome"], "auth", status)
        self.assertIn("not one of your accounts", status["message"])
        self.assertEqual(self.server_calls("b1"), [])

    def test_a_usage_limit_ends_the_job_on_its_account(self):
        job = self.start("LIMIT", account=None)
        status = self.wait(job)
        self.assertEqual(status["outcome"], "usage_limit", status)
        self.assertEqual(len(status["attempts"]), 1)  # never moved to a2
        limited = status["attempts"][0]["account"]
        self.assertIsNotNone(accounts.cooldown_until(accounts.load_usage("remote").get(limited)))

    def test_the_servers_tier_limits_reach_the_job_outcome(self):
        import datetime

        with open(self.server_config, "a", encoding="utf-8") as handle:
            handle.write('\n[tier.default]\nmodels = ["fake-small"]\nweekly_cost_usd = 1\n')
        refused = self.wait(self.start("hello", model="fake-big"))
        self.assertEqual(refused["outcome"], "error", refused)
        self.assertIn("model_not_allowed", refused["message"])
        year, week, _ = datetime.date.today().isocalendar()
        usage = self.server_state / "users" / "alice" / "usage.jsonl"
        usage.parent.mkdir(parents=True, exist_ok=True)
        usage.write_text(json.dumps({"week": f"{year}-W{week:02d}", "cost_usd": 2.0}) + "\n", encoding="utf-8")
        capped = self.wait(self.start("hello", model="fake-small"))
        self.assertEqual(capped["outcome"], "budget", capped)
        self.assertIn("weekly cap", capped["message"])

    def test_a_logged_out_account_is_an_auth_problem(self):
        job = self.start("LOGIN")
        status = self.wait(job)
        self.assertEqual(status["outcome"], "auth", status)


class RemoteAccountTests(RemoteHome):
    def test_status_reports_login_and_usage_from_the_server(self):
        (self.server_accounts / "a2" / "fake-usage.txt").write_text("42", encoding="utf-8")
        config = load_config().runtime("remote")
        runtime = get_runtime("remote")
        a1 = runtime.auth_status(config, config.find("a1"))
        a2 = runtime.auth_status(config, config.find("a2"))
        b1 = runtime.auth_status(config, config.find("b1"))
        self.assertTrue(a1["loggedIn"])
        self.assertEqual(a2["usagePercent"], 42)
        self.assertFalse(b1["loggedIn"])
        self.assertIn("no account 'b1'", b1["error"])
        self.assertAlmostEqual(accounts.load_usage("remote")["a2"]["windows"]["server"]["utilization"], 0.42)

    def test_login_runs_claude_code_on_the_server(self):
        config = load_config().runtime("remote")
        argv, env = get_runtime("remote").login_command(config, config.find("a1"))
        self.assertEqual(argv[-4:], ["-t", "alice@test-server", "login", "a1"])
        self.assertNotIn("BatchMode=yes", argv)  # ssh may ask about the host key or a passphrase
        self.assertNotIn("CLAUDE_CONFIG_DIR", env)

    def test_csa_accounts_and_doctor_work_for_server_accounts(self):
        import contextlib
        import io

        from cli_subagents import cli

        def run(*argv):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = cli.main(list(argv))
            return code, out.getvalue()

        code, out = run("accounts", "add", "a3", "--runtime", "remote", "--no-login")
        self.assertEqual(code, 0, out)
        self.assertIn("a3", [account.name for account in load_config().runtime("remote").accounts])
        self.assertFalse((self.home / "homes" / "remote" / "a3").exists())  # nothing is made on the PC
        self.assertEqual(run("accounts", "add", "a3", "--runtime", "remote", "--no-login")[0], 1)
        self.assertEqual(run("accounts", "add", "a4", "--runtime", "remote", "--home", str(self.root))[0], 2)
        report = {entry["name"]: entry["auth"] for entry in tools.account_report("remote")["accounts"]}
        self.assertTrue(report["a1"]["loggedIn"])
        self.assertFalse(report["a3"]["loggedIn"])
        code, out = run("doctor", "--runtime", "remote")
        self.assertIn("account 'a3' is not logged in", out)
        self.assertIn("csa accounts login a3 --runtime remote", out)
        self.assertEqual(run("launch", "--runtime", "remote")[0], 2)

    def test_follow_shows_the_relays_events_and_claude_codes(self):
        from cli_subagents.follow import describer_for

        describer = describer_for("remote")
        self.assertEqual(describer.describe({"type": "csa.remote", "event": "accepted", "account": "a1", "resumed": True}),
                         ["server took the job on account a1 (continues its session)"])
        self.assertIn("account_not_owned", describer.describe(
            {"type": "csa.remote", "event": "error", "code": "account_not_owned", "message": "not yours"})[0])
        self.assertEqual(describer.describe({"type": "csa.remote", "event": "prompt", "text": "x"}), [])
        self.assertTrue(describer.describe({"type": "system", "subtype": "init", "model": "fake-model"})[0].startswith("started"))

    def test_remote_is_offered_only_with_a_server_and_accounts(self):
        self.assertIn("remote", offered_runtimes())
        self.write_remote_config(server=None)
        self.assertNotIn("remote", offered_runtimes())
        with self.assertRaisesRegex(ValueError, "needs a server"):
            self.start("hello")

    def test_config_keeps_remote_jobs_on_one_account(self):
        config = load_config().runtime("remote")
        self.assertEqual(config.failover, "off")
        self.assertEqual([account.name for account in config.worker_accounts()], ["a1", "a2", "b1"])
        self.assertIsNone(config.find("primary"))
        for extra, problem in (('failover = "on-limit"', "failover"), ("include_primary = true", "primary"),
                               ("", None)):
            with self.subTest(extra=extra):
                self.write_remote_config(extra=extra)
                if problem:
                    with self.assertRaisesRegex(ValueError, problem):
                        load_config()
                else:
                    load_config()
        for server in ("-oProxyCommand=calc", "alice@host name", "-x@host"):
            with self.subTest(server=server):
                self.write_remote_config(server=server)
                with self.assertRaisesRegex(ValueError, "ssh destination"):
                    load_config()

    def test_server_settings_belong_under_remote_and_survive_a_save(self):
        from cli_subagents.config import dump_config, save_config

        self.write_remote_config(extra='ssh_key = "~/.ssh/team_ed25519"')
        config = load_config()
        save_config(config)
        again = load_config().runtime("remote")
        self.assertEqual(again.server, "alice@test-server")
        self.assertTrue(again.ssh_key.endswith("team_ed25519"))
        self.assertIn('server = "alice@test-server"', dump_config(config))
        (self.home / "config.toml").write_text('[claude]\nserver = "x@y"\n', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "belong under"):
            load_config()


if __name__ == "__main__":
    unittest.main()
