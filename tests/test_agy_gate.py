"""The review gate inside real jobs: fake agy runs the hooks from the worker folder like agy does."""

import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from test_antigravity import AgyHome

from cli_subagents import follow, jobs, mcp_translate
from cli_subagents.config import load_config
from cli_subagents.runtimes.antigravity import AntigravityParser, AntigravityRuntime
from cli_subagents.runtimes.base import JobSpec


class GateJobTests(AgyHome):
    def setUp(self):
        super().setUp()
        for key in ("FAKE_AGY_TOOLS", "FAKE_AGY_REVIEW"):
            os.environ.pop(key, None)
        self.write_agy_config()
        self.managed = self.home / "homes" / "antigravity" / "primary"

    def tearDown(self):
        for key in ("FAKE_AGY_TOOLS", "FAKE_AGY_REVIEW"):
            os.environ.pop(key, None)
        super().tearDown()

    def write_agy_config(self, accounts=("a1",), failover="off", extra=""):
        # These tests are about the agy reviewer; the default chain (Claude Haiku first) has its own test.
        if "reviewer" not in extra:
            extra += '\nreviewer = "agy"'
        super().write_agy_config(accounts=accounts, failover=failover, extra=extra)

    def tools(self, *calls):
        os.environ["FAKE_AGY_TOOLS"] = json.dumps(list(calls))

    def answer_lines(self, status):
        return jobs.read_result(status["job_id"])["result"].splitlines()[1:]

    def worker_record(self):
        return [record for record in self.records() if record.get("reviewer") is False][-1]

    def reviewer_records(self):
        return [record for record in self.records() if record.get("reviewer")]

    def test_default_chain_falls_back_from_claude_to_agy(self):
        # The fake claude prints no reviewer JSON (no verdict), so the fake agy decides.
        call = {"name": "run_command", "args": {"CommandLine": "npm install", "Cwd": str(self.project), "BypassSandbox": True}}
        self.tools(call)
        super().write_agy_config()
        status = self.run_job(access="edit")
        self.assertEqual(self.answer_lines(status), ["run_command: auto_approve"])
        claude_calls = [record for record in self.records() if "reviewer" not in record]
        self.assertEqual(len(claude_calls), 1)
        self.assertIn("--tools", claude_calls[0]["argv"])
        self.assertEqual(len(self.reviewer_records()), 1)
        entry = self.review_log(status)[0]
        self.assertEqual((entry["source"], entry["reviewer"]), ("reviewer", "agy/gemini-3.8-flash-low"))
        self.assertEqual(entry["failed"][0]["reviewer"], "claude/haiku")

    def review_log(self, status):
        path = jobs.job_dir(status["job_id"]) / "review.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def test_edit_job_runs_sandboxed_behind_the_gate(self):
        secret = self.root / "gemini-home-secret"
        self.tools(
            {"name": "view_file", "args": {"AbsolutePath": str(self.project / "README.md")}},
            {"name": "run_command", "args": {"CommandLine": "npm test", "Cwd": str(self.project)}},
            {"name": "run_command", "args": {"CommandLine": "npm install", "Cwd": str(self.project), "BypassSandbox": True}},
            {"name": "write_to_file", "args": {"TargetFile": str(self.project / ".git" / "config"), "CodeContent": "x"}},
            {"name": "view_file", "args": {"AbsolutePath": str(self.root / ".ssh" / "id_rsa")}},
            {"name": "ask_permission", "args": {}},
        )
        status = self.run_job(access="edit")
        self.assertEqual(status["outcome"], "success", status)
        self.assertEqual(self.answer_lines(status), [
            "view_file: auto_approve", "run_command: auto_approve", "run_command: auto_approve",
            "write_to_file: deny", "view_file: deny", "ask_permission: deny",
        ])
        record = self.worker_record()
        argv = record["argv"]
        self.assertEqual(argv[argv.index("--mode") + 1], "accept-edits")
        self.assertIn("--sandbox", argv)
        self.assertEqual(Path(record["env"]["HOME"]), self.managed)
        self.assertTrue(record["env"]["AGY_REVIEW_CONTEXT"].endswith("review-context.json"))
        self.assertIn("automatic reviewer", argv[-1])
        # One reviewer call (the command that leaves the sandbox), tool-less and marked as the reviewer.
        reviews = self.reviewer_records()
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["env"].get("AGY_REVIEW_ROLE"), "reviewer")
        self.assertNotIn("AGY_REVIEW_CONTEXT", reviews[0]["env"])
        self.assertEqual(reviews[0]["argv"][reviews[0]["argv"].index("--mode") + 1], "plan")
        self.assertIn("gemini-3.8-flash-low", reviews[0]["argv"])
        # The reviewer runs from its own bare folder: gate-only hooks, no MCP servers.
        reviewer_home = self.home / "homes" / "antigravity" / ".reviewer" / "primary"
        self.assertEqual(Path(reviews[0]["env"]["HOME"]), reviewer_home)
        mcp = json.loads((reviewer_home / ".gemini" / "config" / "mcp_config.json").read_text(encoding="utf-8"))
        self.assertEqual(mcp, {"mcpServers": {}})
        self.assertEqual(sorted(json.loads((reviewer_home / ".gemini" / "config" / "hooks.json").read_text(encoding="utf-8"))),
                         ["cli-subagents-review"])
        log = self.review_log(status)
        self.assertEqual([item["source"] for item in log], ["rule", "rule", "reviewer", "rule", "rule", "rule"])
        settings = json.loads((self.managed / ".gemini" / "antigravity-cli" / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["toolPermission"], "request-review")

    def test_read_job_uses_plan_mode_and_denies_edits(self):
        self.tools(
            {"name": "write_to_file", "args": {"TargetFile": str(self.project / "a.txt"), "CodeContent": "x"}},
            {"name": "run_command", "args": {"CommandLine": "git status", "Cwd": str(self.project)}},
            {"name": "run_command", "args": {"CommandLine": "npm test", "Cwd": str(self.project)}},
        )
        status = self.run_job(access="read")
        self.assertEqual(self.answer_lines(status), ["write_to_file: deny", "run_command: auto_approve", "run_command: auto_approve"])
        argv = self.worker_record()["argv"]
        self.assertEqual(argv[argv.index("--mode") + 1], "plan")
        self.assertNotIn("--sandbox", argv)
        self.assertEqual(len(self.reviewer_records()), 1)  # npm test in a read job goes to the reviewer

    def test_full_job_has_no_gate(self):
        self.tools({"name": "run_command", "args": {"CommandLine": "npm install", "Cwd": str(self.project)}})
        status = self.run_job(access="full")
        self.assertEqual(self.answer_lines(status), ["run_command: allow"])  # the hook defers to agy
        record = self.worker_record()
        self.assertIn("--dangerously-skip-permissions", record["argv"])
        self.assertNotIn("AGY_REVIEW_CONTEXT", record["env"])
        self.assertEqual(self.reviewer_records(), [])

    def test_reviewer_off_and_bad_answers_deny(self):
        call = {"name": "run_command", "args": {"CommandLine": "npm install", "Cwd": str(self.project), "BypassSandbox": "true"}}
        self.tools(call)
        self.write_agy_config(extra='reviewer = "off"')
        status = self.run_job(access="edit")
        self.assertEqual(self.answer_lines(status), ["run_command: deny"])
        self.assertEqual(self.reviewer_records(), [])
        self.write_agy_config()
        os.environ["FAKE_AGY_REVIEW"] = "Sure, looks fine to me!"
        status = self.run_job(access="edit")
        self.assertEqual(self.answer_lines(status), ["run_command: deny"])
        self.assertEqual(self.review_log(status)[0]["source"], "error")
        os.environ["FAKE_AGY_REVIEW"] = '{"decision":"allow","risk":"high","reason":"pushes to a shared system"}'
        status = self.run_job(access="edit")
        self.assertEqual(self.answer_lines(status), ["run_command: deny"])

    def test_same_action_is_reviewed_once_per_job(self):
        call = {"name": "read_url_content", "args": {"Url": "https://example.com/docs", "toolSummary": "a"}}
        again = {"name": "read_url_content", "args": {"Url": "https://example.com/docs", "toolSummary": "b"}}
        self.tools(call, again)
        status = self.run_job(access="read")
        self.assertEqual(self.answer_lines(status), ["read_url_content: auto_approve", "read_url_content: auto_approve"])
        self.assertEqual([item["source"] for item in self.review_log(status)], ["reviewer", "cache"])
        self.assertEqual(len(self.reviewer_records()), 1)

    def test_parent_files_and_channel_are_allowed(self):
        instructions = self.root / "claude-config" / "CLAUDE.md"
        instructions.parent.mkdir()
        instructions.write_text("rules", encoding="utf-8")
        self.tools(
            {"name": "view_file", "args": {"AbsolutePath": str(instructions)}},
            {"name": "call_mcp_tool", "args": {"ServerName": "csa-parent", "ToolName": "message_parent", "Arguments": {"text": "hi"}}},
            {"name": "view_file", "args": {"AbsolutePath": str(self.home / "config.toml")}},
        )
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(instructions.parent)}):
            status = jobs.create_job(runtime="antigravity", prompt="task", cwd=str(self.project), access="read", host="claude")
            status = jobs.wait_for(status["job_id"], 90)
        self.assertEqual(self.answer_lines(status), ["view_file: auto_approve", "call_mcp_tool: auto_approve", "view_file: deny"])

    def test_user_hooks_are_kept_next_to_the_gate(self):
        (self.gemini / "config").mkdir(exist_ok=True)
        (self.gemini / "config" / "hooks.json").write_text(json.dumps({"mine": {"PostToolUse": []}}), encoding="utf-8")
        self.run_job(access="edit")
        hooks = json.loads((self.managed / ".gemini" / "config" / "hooks.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(hooks), ["cli-subagents-review", "mine"])

    def test_extra_account_runs_behind_the_gate_in_its_folder(self):
        self.write_agy_config(accounts=("a1",))
        self.log_in("a1")
        self.tools(
            {"name": "run_command", "args": {"CommandLine": "npm test", "Cwd": str(self.project)}},
            {"name": "read_url_content", "args": {"Url": "https://example.com"}},
        )
        status = self.run_job(account="a1", access="edit")
        self.assertEqual((status["outcome"], status["account"]), ("success", "a1"), status)
        self.assertEqual(self.answer_lines(status), ["run_command: auto_approve", "read_url_content: auto_approve"])
        env = self.worker_record()["env"]
        self.assertEqual(Path(env["HOME"]), self.homes / "a1")
        self.assertTrue(env.get("SSH_CONNECTION"))
        # The reviewer folder links only antigravity-cli (where the account's login file lives).
        review = self.reviewer_records()[0]["env"]
        reviewer_home = self.home / "homes" / "antigravity" / ".reviewer" / "a1"
        self.assertEqual(Path(review["HOME"]), reviewer_home)
        self.assertTrue(review.get("SSH_CONNECTION"))
        self.assertTrue((reviewer_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token").is_file())
        self.assertFalse((reviewer_home / ".gemini" / "config" / "mcp_config.json").read_text(encoding="utf-8").count("csa-parent"))

    def test_primary_with_a_file_login_keeps_the_real_home(self):
        token = self.root / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
        token.parent.mkdir(parents=True)
        token.write_text("x", encoding="utf-8")
        self.write_agy_config()
        # gemini_root(config) is <root>/gemini here, so put the token where agy would: <its parent>/.gemini
        runtime = AntigravityRuntime()
        config = load_config().runtime("antigravity")
        self.assertIsNone(runtime.worker_home(config, config.primary_account()))
        status = self.run_job(access="edit")
        self.assertEqual(status["outcome"], "success", status)
        record = self.worker_record()
        self.assertEqual(record["env"].get("HOME"), os.environ.get("HOME"))
        self.assertNotIn("AGY_REVIEW_CONTEXT", record["env"])
        self.assertTrue(any("without the review gate" in item for item in status["warnings"]), status)

    def test_live_view_prints_gate_decisions(self):
        self.tools(
            {"name": "run_command", "args": {"CommandLine": "npm test", "Cwd": str(self.project)}},
            {"name": "write_to_file", "args": {"TargetFile": str(self.project / ".git" / "HEAD"), "CodeContent": "x"}},
            {"name": "view_file", "args": {"AbsolutePath": str(self.project / "README.md")}},
        )
        status = self.run_job(access="edit")
        out = io.StringIO()
        follow.follow(status["job_id"], None, 5, out)
        text = out.getvalue()
        self.assertIn("tool: run_command · npm test", text)
        self.assertIn("review: approved run_command · npm test (runs in agy's sandbox", text)
        self.assertIn("review: denied write_to_file", text)
        self.assertNotIn("review: approved view_file", text)  # routine reads stay quiet
        # A second follow with an offset prints nothing new from the review log.
        out = io.StringIO()
        follow.follow(status["job_id"], 10**9, 5, out)
        self.assertNotIn("review:", out.getvalue())


class DeniedRunTests(AgyHome):
    def test_empty_success_with_denied_actions_is_an_error(self):
        self.write_agy_config()
        os.environ["FAKE_AGY_SCENARIO"] = "denied"
        status = self.run_job(access="full")
        self.assertEqual(status["outcome"], "error", status)
        self.assertIn("RunCommand", status["message"])
        self.assertIn("headless", status["message"])

    def test_parser_names_a_missing_gate(self):
        result = {"status": "SUCCESS", "response": "", "denied_actions": [{"action": "mcp", "display_name": "CallMcpTool"}]}
        parser = AntigravityParser(review_log=self.root / "none.jsonl")
        parser.feed({"event": "result", "result": result})
        finished = parser.finish(0, 'jetski: no output produced - a tool required the "mcp" permission')
        self.assertEqual(finished.outcome, "error")
        self.assertIn("never answered", finished.message)
        self.assertIn("CallMcpTool", finished.message)


class ParentMcpTests(AgyHome):
    def test_claude_servers_are_shared_and_yours_win(self):
        (self.gemini / "config").mkdir(exist_ok=True)
        mine = {"mcpServers": {"docs": {"command": "mine"}, "cli-subagents": {"command": "csa", "args": ["mcp"]}}}
        (self.gemini / "config" / "mcp_config.json").write_text(json.dumps(mine), encoding="utf-8")
        self.write_agy_config()
        collected = mcp_translate.Collected(servers={
            "docs": {"command": "theirs"}, "search": {"command": "node", "args": ["s.js"], "cwd": str(self.project)},
            "remote": {"type": "sse", "url": "https://example.com/sse"},
        })
        config = load_config().runtime("antigravity")
        spec = JobSpec(job_id="job-9", runtime="antigravity", prompt="", cwd=str(self.project), parent={"host": "claude"})
        with patch.object(mcp_translate, "collect_servers", return_value=collected):
            report = AntigravityRuntime().prepare(config, config.primary_account(), spec)
        path = self.home / "homes" / "antigravity" / "primary" / ".gemini" / "config" / "mcp_config.json"
        servers = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]
        self.assertEqual(sorted(servers), ["csa-parent", "docs", "remote", "search"])
        self.assertEqual(servers["docs"]["command"], "mine")
        self.assertEqual(servers["search"]["cwd"], str(self.project))
        self.assertEqual(servers["remote"], {"serverUrl": "https://example.com/sse"})
        self.assertTrue(report["parent_channel"])
        self.write_agy_config(extra="share_parent_mcp = false")
        config = load_config().runtime("antigravity")
        with patch.object(mcp_translate, "collect_servers", return_value=collected):
            AntigravityRuntime().prepare(config, config.primary_account(), spec)
        servers = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]
        self.assertEqual(sorted(servers), ["csa-parent", "docs"])


if __name__ == "__main__":
    unittest.main()
