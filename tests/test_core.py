import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from helpers import TempHome

import re

from cli_subagents import accounts, jobs, mcp_server, runner, sync
from cli_subagents.config import load_config
from cli_subagents.fsutil import is_link, read_json, write_json
from cli_subagents.runtimes.base import clean_environment
from cli_subagents.runtimes.claude import ClaudeParser, ClaudeRuntime


class EnvironmentTests(unittest.TestCase):
    def test_host_session_variables_are_removed(self):
        source = {
            "PATH": "x",
            "HOME": "h",
            "ANTHROPIC_BASE_URL": "http://localhost:1",
            "ANTHROPIC_API_KEY": "secret",
            "CLAUDECODE": "1",
            "CLAUDE_CODE_OAUTH_TOKEN": "token",
            "CLAUDE_CODE_PLUGIN_DIRS": "dir",
            "CLAUDE_CONFIG_DIR": "host-account",
            "CLAUDE_CODE_GIT_BASH_PATH": "C:/git/bash.exe",
            "CODEX_COMPANION_SESSION_ID": "s",
            "OPENCODE_API_KEY": "opencode-secret",
            "OPENCODE_CONFIG_CONTENT": "host-config",
            "MCP_CONNECTION_NONBLOCKING": "1",
            "CLI_SUBAGENTS_DEPTH": "0",
            "CLI_SUBAGENTS_HOME": "keep",
        }
        cleaned = clean_environment(source)
        self.assertEqual(
            cleaned,
            {"PATH": "x", "HOME": "h", "CLAUDE_CODE_GIT_BASH_PATH": "C:/git/bash.exe", "CLI_SUBAGENTS_HOME": "keep"},
        )


REPLACE_LOOP = """
import sys, time
from pathlib import Path
from cli_subagents.fsutil import write_json
path, stop_at, count = Path(sys.argv[1]), float(sys.argv[2]), 0
while time.time() < stop_at:
    try:
        write_json(path, {"count": count})
    except OSError:
        pass  # like the runner's heartbeat: a busy file skips one write
    count += 1
"""


class ReplacedFileTests(unittest.TestCase):
    """status.json is read while the runner replaces it, so reads must ride out the swap."""

    def test_reads_while_other_processes_replace_the_file(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            path = Path(temp) / "status.json"
            write_json(path, {"count": -1})
            stop_at = time.time() + 2.0
            src = Path(__file__).resolve().parents[1] / "src"
            writers = [
                subprocess.Popen(
                    [sys.executable, "-c", REPLACE_LOOP, str(path), str(stop_at)],
                    env={**os.environ, "PYTHONPATH": str(src)},
                )
                for _ in range(2)
            ]
            values, errors = [], []

            def keep_reading():
                while time.time() < stop_at:
                    try:
                        values.append(read_json(path))
                    except OSError as exc:
                        errors.append(exc)
                    time.sleep(0.001)  # pollers pause between reads; nonstop readers would starve the writers

            readers = [threading.Thread(target=keep_reading) for _ in range(4)]
            for reader in readers:
                reader.start()
            for reader in readers:
                reader.join()
            self.assertEqual([writer.wait(timeout=60) for writer in writers], [0, 0])
        self.assertEqual(errors, [])
        self.assertTrue(values)
        self.assertTrue(all(isinstance(value, dict) for value in values))


class ParserTests(unittest.TestCase):
    def feed(self, *events):
        parser = ClaudeParser()
        for event in events:
            parser.feed(event)
        return parser

    def test_success(self):
        parser = self.feed(
            {"type": "system", "subtype": "init", "session_id": "s1", "model": "m", "mcp_servers": [], "plugins": []},
            {"type": "result", "subtype": "success", "is_error": False, "result": "OK", "total_cost_usd": 0.1},
        )
        result = parser.finish(0, "")
        self.assertEqual((result.outcome, result.text, result.session_id), ("success", "OK", "s1"))

    def test_usage_limit_from_rejected_event(self):
        parser = self.feed(
            {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": 123}},
            {"type": "result", "subtype": "success", "is_error": True, "api_error_status": 429, "result": "API Error: 429"},
        )
        result = parser.finish(1, "")
        self.assertEqual((result.outcome, result.resets_at), ("usage_limit", 123.0))

    def test_usage_limit_from_text(self):
        parser = self.feed({"type": "result", "is_error": True, "result": "You've hit your weekly limit · resets Mon"})
        self.assertEqual(parser.finish(1, "").outcome, "usage_limit")

    def test_server_throttle_is_not_a_usage_limit(self):
        parser = self.feed(
            {"type": "result", "is_error": True, "result": "Server is temporarily limiting requests (not your usage limit)"}
        )
        self.assertEqual(parser.finish(1, "").outcome, "error")

    def test_auth_error(self):
        parser = self.feed({"type": "result", "is_error": True, "result": "Not logged in · Please run /login"})
        self.assertEqual(parser.finish(1, "").outcome, "auth")

    def test_no_result_is_an_error(self):
        self.assertEqual(ClaudeParser().finish(3, "boom").outcome, "error")


class SharedSetupTests(TempHome):
    def make_primary(self):
        (self.primary / "skills" / "alpha").mkdir(parents=True)
        (self.primary / "skills" / "alpha" / "SKILL.md").write_text("alpha", encoding="utf-8")
        (self.primary / "skills" / "synced").mkdir()
        (self.primary / "agents").mkdir()
        (self.primary / "agents" / "helper.md").write_text("agent", encoding="utf-8")
        (self.primary / "CLAUDE.md").write_text("rules", encoding="utf-8")
        plugin = self.root / "plugin-cache" / "demo" / "1.0.0"
        plugin.mkdir(parents=True)
        (self.primary / "plugins").mkdir()
        (self.primary / "plugins" / "installed_plugins.json").write_text(
            json.dumps({"version": 2, "plugins": {"demo@market": [{"scope": "user", "installPath": str(plugin)}]}}),
            encoding="utf-8",
        )
        (self.primary / "settings.json").write_text(
            json.dumps(
                {
                    "apiKeyHelper": "get-key",
                    "enabledPlugins": {"demo@market": True, "off@market": False},
                    "env": {"ANTHROPIC_API_KEY": "secret", "MY_FLAG": "1"},
                    "hooks": {"Stop": []},
                }
            ),
            encoding="utf-8",
        )
        (self.primary / ".claude.json").write_text(
            json.dumps(
                {
                    "oauthAccount": {"emailAddress": "someone@example.com"},
                    "mcpServers": {"shared": {"command": "x"}},
                    "projects": {str(self.project).replace("\\", "/"): {"mcpServers": {"local-one": {"command": "y"}}}},
                }
            ),
            encoding="utf-8",
        )
        return plugin

    def prepare(self, name="a1"):
        config = load_config().runtime("claude")
        account = config.find(name)
        spec = jobs.JobSpec(job_id="t", runtime="claude", prompt="", cwd=str(self.project))
        return account, ClaudeRuntime().prepare(config, account, spec)

    def test_account_gets_primary_setup_without_credentials(self):
        self.write_config()
        plugin = self.make_primary()
        account, report = self.prepare()
        home = account.home
        self.assertTrue(is_link(home / "skills" / "alpha"))
        self.assertEqual((home / "skills" / "alpha" / "SKILL.md").read_text(), "alpha")
        self.assertFalse((home / "skills" / "synced").exists())
        self.assertEqual((home / "agents" / "helper.md").read_text(), "agent")
        self.assertEqual((home / "CLAUDE.md").read_text(), "rules")
        self.assertFalse((home / ".claude.json").exists())
        settings = json.loads(Path(report["settings_path"]).read_text())
        self.assertNotIn("apiKeyHelper", settings)
        self.assertNotIn("enabledPlugins", settings)
        self.assertEqual(settings["env"], {"MY_FLAG": "1"})
        self.assertIn("hooks", settings)
        servers = json.loads(Path(report["mcp_config_path"]).read_text())["mcpServers"]
        self.assertEqual(sorted(servers), ["local-one", "shared"])
        self.assertEqual(report["plugin_dirs"], [str(plugin)])
        self.assertFalse(report["conflicts"])

    def test_one_shared_pool_not_copies(self):
        self.write_config()
        self.make_primary()
        account, _ = self.prepare()
        home = account.home
        self.assertTrue(is_link(home / "agents"))
        self.assertTrue(os.path.samefile(home / "CLAUDE.md", self.primary / "CLAUDE.md"))
        # An edit through the account changes the one shared file.
        (home / "agents" / "new.md").write_text("added by a worker", encoding="utf-8")
        self.assertTrue((self.primary / "agents" / "new.md").exists())

    def test_project_memory_is_shared(self):
        self.write_config()
        self.make_primary()
        account, _ = self.prepare()
        name = re.sub(r"[^A-Za-z0-9]", "-", os.path.normpath(str(self.project)))
        (account.home / "projects" / name / "memory" / "note.md").write_text("remember", encoding="utf-8")
        self.assertEqual((self.primary / "projects" / name / "memory" / "note.md").read_text(), "remember")

    def test_old_per_file_copies_become_one_link(self):
        self.write_config()
        self.make_primary()
        home = self.root / "homes" / "a1"
        (home / "agents").mkdir(parents=True)
        (home / "agents" / "helper.md").write_text("agent", encoding="utf-8")
        from cli_subagents.fsutil import sha256_file
        (home / sync.MANIFEST).write_text(json.dumps({"items": {"agents/helper.md": {
            "kind": "copy", "sha256": sha256_file(home / "agents" / "helper.md")}}}), encoding="utf-8")
        self.prepare()
        self.assertTrue(is_link(home / "agents"))

    def test_removed_skill_is_unlinked_but_source_kept(self):
        self.write_config()
        self.make_primary()
        account, _ = self.prepare()
        # Remove the skill from the primary by moving it away, then sync again.
        moved = self.root / "moved-alpha"
        (self.primary / "skills" / "alpha").rename(moved)
        _, report = self.prepare()
        self.assertIn("skills/alpha", report["removed"])
        self.assertFalse((account.home / "skills" / "alpha").exists())
        self.assertEqual((moved / "SKILL.md").read_text(), "alpha")

    def test_account_owned_files_are_not_overwritten(self):
        self.write_config()
        self.make_primary()
        own = self.root / "homes" / "a1" / "skills" / "alpha"
        own.mkdir(parents=True)
        (own / "SKILL.md").write_text("mine", encoding="utf-8")
        (self.root / "homes" / "a1" / "CLAUDE.md").write_text("my rules", encoding="utf-8")
        _, report = self.prepare()
        self.assertEqual((own / "SKILL.md").read_text(), "mine")
        self.assertEqual((self.root / "homes" / "a1" / "CLAUDE.md").read_text(), "my rules")
        self.assertEqual(len(report["conflicts"]), 2)

    def test_updated_copy_follows_primary(self):
        self.write_config()
        self.make_primary()
        account, _ = self.prepare()
        (self.primary / "CLAUDE.md").write_text("new rules", encoding="utf-8")
        self.prepare()
        self.assertEqual((account.home / "CLAUDE.md").read_text(), "new rules")


class JobTests(TempHome):
    def wait(self, job_id, seconds=60):
        status = jobs.wait_for(job_id, seconds)
        self.assertEqual(status["state"], "done", status)
        return status

    def test_failover_moves_job_to_next_account(self):
        self.write_config(failover="on-limit")
        os.environ["FAKE_CLAUDE_LIMITED"] = "a1"
        status = jobs.create_job(runtime="claude", prompt="do it", cwd=str(self.project))
        status = self.wait(status["job_id"])
        self.assertEqual(status["outcome"], "success", status)
        self.assertEqual([attempt["account"] for attempt in status["attempts"]], ["a1", "a2"])
        result = jobs.read_result(status["job_id"])
        self.assertEqual(result["result"], "done on a2")
        usage = accounts.load_usage("claude")
        self.assertGreater(usage["a1"]["cooldown_until"], time.time())
        self.assertEqual(usage["a2"]["windows"]["seven_day"]["utilization"], 0.5)
        # The second account continues the same conversation from a copied transcript.
        first_argv, second = self.records()[0]["argv"], self.records()[1]
        session = first_argv[first_argv.index("--session-id") + 1]
        self.assertEqual(second["argv"][second["argv"].index("--resume") + 1], session)
        self.assertIn("usage limit", second["stdin"])
        self.assertEqual(status["session_id"], session)
        self.assertTrue(list((self.root / "homes" / "a2" / "projects").glob(f"*/{session}.jsonl")))
        self.assertTrue(list((self.root / "homes" / "a1" / "projects").glob(f"*/{session}.jsonl")))

    def test_without_failover_the_limit_is_reported(self):
        self.write_config(failover="off")
        os.environ["FAKE_CLAUDE_LIMITED"] = "a1"
        status = self.wait(jobs.create_job(runtime="claude", prompt="x", cwd=str(self.project))["job_id"])
        self.assertEqual(status["outcome"], "usage_limit")
        # The next job skips a1 while it cools down.
        status = self.wait(jobs.create_job(runtime="claude", prompt="y", cwd=str(self.project))["job_id"])
        self.assertEqual((status["outcome"], status["account"]), ("success", "a2"))

    def test_worker_environment_and_arguments(self):
        self.write_config(accounts=("a1",))
        with patch.dict(os.environ, {"ANTHROPIC_BASE_URL": "http://host-proxy", "CLAUDECODE": "1"}):
            job = jobs.create_job(runtime="claude", prompt="task text", cwd=str(self.project), access="read", model="haiku")
        self.wait(job["job_id"])
        record = self.records()[0]
        self.assertEqual(record["stdin"], "task text")
        self.assertNotIn("ANTHROPIC_BASE_URL", record["env"])
        self.assertNotIn("CLAUDECODE", record["env"])
        self.assertEqual(Path(record["env"]["CLAUDE_CONFIG_DIR"]), self.root / "homes" / "a1")
        self.assertEqual(record["env"]["CLI_SUBAGENTS_DEPTH"], "1")
        argv = record["argv"]
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(argv[argv.index("--model") + 1], "haiku")
        self.assertIn("--disallowedTools", argv)
        disallowed = argv[argv.index("--disallowedTools") + 1].split(",")
        self.assertNotIn("Bash", disallowed)
        self.assertNotIn("PowerShell", disallowed)
        allowed = argv[argv.index("--allowedTools") + 1].split(",")
        self.assertIn("Read", allowed)
        self.assertIn("Grep", allowed)
        for prefix in ("git status", "git diff", "git log", "git show", "git blame", "ls", "wc", "cat", "head", "tail", "grep", "rg"):
            self.assertIn(f"Bash({prefix}:*)", allowed)
        for prefix in ("Get-ChildItem", "Get-Content", "Measure-Object"):
            self.assertIn(f"PowerShell({prefix}:*)", allowed)
        settings = json.loads(Path(argv[argv.index("--settings") + 1]).read_text(encoding="utf-8"))
        guard = settings["hooks"]["PreToolUse"][-1]
        self.assertEqual(guard["matcher"], "Bash|PowerShell")
        self.assertIn("claude_read_hook.py", guard["hooks"][0]["command"])
        self.assertEqual(guard["hooks"][0]["onFailure"], "block")

    def test_edit_and_full_workers_keep_their_permissions_and_hooks(self):
        self.write_config(accounts=("a1",))
        for access, mode in (("edit", "auto"), ("full", "bypassPermissions")):
            with self.subTest(access=access):
                job = jobs.create_job(runtime="claude", prompt="task", cwd=str(self.project), access=access)
                self.assertEqual(self.wait(job["job_id"])["outcome"], "success")
                argv = self.records()[-1]["argv"]
                self.assertEqual(argv[argv.index("--permission-mode") + 1], mode)
                self.assertFalse(any(tool.startswith(("Bash", "PowerShell")) for tool in
                                     argv[argv.index("--allowedTools") + 1].split(",")))
                self.assertFalse(any(tool in ("Bash", "PowerShell") for tool in
                                     argv[argv.index("--disallowedTools") + 1].split(",")))
                settings = json.loads(Path(argv[argv.index("--settings") + 1]).read_text(encoding="utf-8"))
                self.assertNotIn("PreToolUse", settings["hooks"])
                self.assertNotIn("disableAllHooks", settings)

    def test_auth_error_does_not_fail_over(self):
        self.write_config(failover="on-limit")
        os.environ["FAKE_CLAUDE_SCENARIO"] = "auth"
        status = self.wait(jobs.create_job(runtime="claude", prompt="x", cwd=str(self.project))["job_id"])
        self.assertEqual((status["outcome"], len(status["attempts"])), ("auth", 1))

    def test_cancel_stops_the_worker(self):
        self.write_config(accounts=("a1",))
        os.environ["FAKE_CLAUDE_SCENARIO"] = "slow"
        job = jobs.create_job(runtime="claude", prompt="x", cwd=str(self.project))
        deadline = time.time() + 30
        while jobs.read_status(job["job_id"]).get("state") != "running" and time.time() < deadline:
            time.sleep(0.2)
        status = jobs.cancel(job["job_id"])
        self.assertEqual((status["state"], status["outcome"]), ("done", "cancelled"))

    def test_timeout(self):
        self.write_config(accounts=("a1",))
        os.environ["FAKE_CLAUDE_SCENARIO"] = "slow"
        job = jobs.create_job(runtime="claude", prompt="x", cwd=str(self.project), start=False)
        spec_path = jobs.job_dir(job["job_id"]) / "spec.json"
        spec = json.loads(spec_path.read_text())
        spec["timeout_seconds"] = 2
        spec_path.write_text(json.dumps(spec))
        runner.main(job["job_id"])
        self.assertEqual(jobs.read_status(job["job_id"])["outcome"], "timeout")

    def test_nested_delegation_is_refused(self):
        self.write_config()
        with patch.dict(os.environ, {"CLI_SUBAGENTS_DEPTH": "1"}):
            with self.assertRaises(ValueError):
                jobs.create_job(runtime="claude", prompt="x", cwd=str(self.project))

    def test_resume_moves_to_another_account_when_the_first_is_cooling_down(self):
        self.write_config(failover="off")
        first = self.wait(jobs.create_job(runtime="claude", prompt="x", cwd=str(self.project), account="a1")["job_id"])
        accounts.set_cooldown("claude", "a1", time.time() + 3600, "test")
        second = self.wait(jobs.create_job(runtime="claude", prompt="more", cwd=str(self.project), resume_job=first["job_id"])["job_id"])
        self.assertEqual((second["outcome"], second["account"]), ("success", "a2"), second)
        argv = self.records()[-1]["argv"]
        self.assertEqual(argv[argv.index("--resume") + 1], first["session_id"])

    def test_failover_is_on_by_default(self):
        (self.home / "config.toml").write_text("", encoding="utf-8")
        self.assertEqual(load_config().runtime("claude").failover, "on-limit")
        self.assertEqual(load_config().runtime("antigravity").failover, "on-limit")

    def test_resume_stays_on_the_same_account(self):
        self.write_config(failover="off")
        first = self.wait(jobs.create_job(runtime="claude", prompt="x", cwd=str(self.project), account="a2")["job_id"])
        second = jobs.create_job(runtime="claude", prompt="more", cwd=str(self.project), resume_job=first["job_id"])
        second = self.wait(second["job_id"])
        self.assertEqual(second["account"], "a2")
        argv = self.records()[-1]["argv"]
        self.assertEqual(argv[argv.index("--resume") + 1], first["session_id"])


class McpTests(TempHome):
    def exchange(self, *messages):
        stdin = io.BytesIO(b"".join((json.dumps(message) + "\n").encode() for message in messages))
        stdout = io.BytesIO()
        mcp_server.serve(stdin, stdout)
        replies = [json.loads(line) for line in stdout.getvalue().decode().splitlines()]
        return {reply["id"]: reply for reply in replies}

    def test_handshake_list_and_call(self):
        self.write_config()
        replies = self.exchange(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "subagent_list", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "subagent_status", "arguments": {"job_id": "nope"}}},
            {"jsonrpc": "2.0", "id": 5, "method": "unknown/method"},
        )
        self.assertEqual(replies[1]["result"]["protocolVersion"], "2025-06-18")
        names = [tool["name"] for tool in replies[2]["result"]["tools"]]
        self.assertIn("subagent_start", names)
        self.assertFalse(replies[3]["result"]["isError"])
        self.assertTrue(replies[4]["result"]["isError"])
        self.assertEqual(replies[5]["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main()
