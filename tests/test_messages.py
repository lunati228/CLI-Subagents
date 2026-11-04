import io
import json
import os
import threading
import time
import unittest
from unittest.mock import patch

from helpers import TempHome

from cli_subagents import jobs, mcp_server, messages, tools


class MessageTests(TempHome):
    def start(self, prompt="task", **kwargs):
        return jobs.create_job(runtime="claude", prompt=prompt, cwd=str(self.project), **kwargs)["job_id"]

    def wait_running(self, job_id):
        deadline = time.time() + 30
        while jobs.read_status(job_id).get("state") != "running" and time.time() < deadline:
            time.sleep(0.2)

    def test_worker_message_reaches_the_main_session_once(self):
        self.write_config(accounts=("a1",))
        os.environ["FAKE_CLAUDE_DELAY"] = "3"
        job = self.start()
        tools.call("message_parent", {"text": "halfway there"}, worker_job=job)
        report = tools.call("subagent_wait", {"job_ids": [job], "timeout_seconds": 20})
        self.assertEqual(report["updates"][0]["messages"][0]["text"], "halfway there")
        self.assertNotIn("messages", tools.call("subagent_status", {"job_id": job}))
        report = tools.call("subagent_wait", {"job_ids": [job], "timeout_seconds": 30})
        self.assertEqual(report["finished"][0]["outcome"], "success")

    def test_question_and_answer(self):
        self.write_config(accounts=("a1",))
        os.environ["FAKE_CLAUDE_DELAY"] = "4"
        job = self.start()
        answers = []
        worker = threading.Thread(
            target=lambda: answers.append(tools.call("ask_parent", {"question": "Use tabs or spaces?", "timeout_seconds": 20}, worker_job=job))
        )
        worker.start()
        deadline = time.time() + 10
        while not messages.open_questions(job) and time.time() < deadline:
            time.sleep(0.1)
        question = tools.call("subagent_status", {"job_id": job})["open_questions"][0]
        self.assertEqual(question["text"], "Use tabs or spaces?")
        tools.call("subagent_message", {"job_id": job, "text": "Spaces.", "reply_to": question["id"]})
        worker.join(15)
        self.assertEqual(answers[0]["answer"], "Spaces.")
        jobs.wait_for(job, 30)

    def test_follow_up_sent_while_running_is_delivered(self):
        self.write_config(accounts=("a1",))
        os.environ["FAKE_CLAUDE_DELAY"] = "3"
        job = self.start("first task")
        self.wait_running(job)
        tools.call("subagent_message", {"job_id": job, "text": "also update the docs"})
        status = jobs.wait_for(job, 60)
        self.assertEqual((status["outcome"], status.get("follow_up_turns")), ("success", 1), status)
        last = self.records()[-1]
        self.assertIn("also update the docs", last["stdin"])
        self.assertIn("--resume", last["argv"])

    def test_message_to_a_finished_job_continues_it(self):
        self.write_config(accounts=("a1",))
        job = self.start()
        jobs.wait_for(job, 30)
        reply = tools.call("subagent_message", {"job_id": job, "text": "one more thing"})
        new_job = reply["new_job"]["job_id"]
        status = jobs.wait_for(new_job, 30)
        self.assertEqual(status["outcome"], "success")
        argv = self.records()[-1]["argv"]
        self.assertEqual(argv[argv.index("--resume") + 1], jobs.read_status(job)["session_id"])

    def test_finished_explicit_only_account_keeps_its_pin_and_job_budget(self):
        from cli_subagents.config import load_config, save_config

        self.write_config(accounts=("a1", "a2"))
        config = load_config()
        account = config.runtime("claude").find("a1")
        account.explicit_only = True
        account.spend_cap_usd = 10
        account.job_budget_usd = 1.5
        save_config(config)
        job = self.start(account="a1")
        self.assertEqual(jobs.wait_for(job, 30)["outcome"], "success")
        reply = tools.call("subagent_message", {"job_id": job, "text": "one more review"})
        new_job = reply["new_job"]["job_id"]
        status = jobs.wait_for(new_job, 30)
        self.assertEqual((status["outcome"], status["account"]), ("success", "a1"))
        self.assertEqual(jobs.read_spec(new_job).account, "a1")
        argv = self.records()[-1]["argv"]
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "1.500000")

    def test_worker_gets_the_parent_channel(self):
        self.write_config(accounts=("a1",))
        job = self.start()
        jobs.wait_for(job, 30)
        argv = self.records()[-1]["argv"]
        config = json.loads(open(argv[argv.index("--mcp-config") + 1], encoding="utf-8").read())
        self.assertEqual(config["mcpServers"]["csa-parent"]["args"][-1], job)
        self.assertIn("mcp__csa-parent", argv)

    def test_worker_server_lists_only_worker_tools_and_pings_progress(self):
        self.write_config(accounts=("a1",))
        os.environ["FAKE_CLAUDE_DELAY"] = "3"
        job = self.start()
        stdin = io.BytesIO(
            b"".join(
                (json.dumps(message) + "\n").encode()
                for message in (
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                )
            )
        )
        stdout = io.BytesIO()
        mcp_server.serve(stdin, stdout, worker_job=job)
        names = [tool["name"] for tool in json.loads(stdout.getvalue().decode().splitlines()[0])["result"]["tools"]]
        self.assertEqual(names, ["message_parent", "ask_parent", "check_parent_messages"])

        with patch.object(tools, "PING_SECONDS", 0.3):
            stdin = io.BytesIO((json.dumps({
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "subagent_wait", "arguments": {"job_ids": [job], "timeout_seconds": 30}, "_meta": {"progressToken": "p1"}},
            }) + "\n").encode())
            stdout = io.BytesIO()
            mcp_server.serve(stdin, stdout)
        lines = [json.loads(line) for line in stdout.getvalue().decode().splitlines()]
        self.assertTrue(any(line.get("method") == "notifications/progress" for line in lines))
        final = next(line for line in lines if line.get("id") == 2)
        self.assertIn("finished", json.loads(final["result"]["content"][0]["text"]))

    def tearDown(self):
        os.environ.pop("FAKE_CLAUDE_DELAY", None)
        super().tearDown()


if __name__ == "__main__":
    unittest.main()
