import json
import os
from pathlib import Path
import time
from unittest.mock import Mock, patch

from helpers import TempHome

from cli_subagents import accounts, jobs, mcp_translate, tools
from cli_subagents.config import default_primary, dump_config, load_config
from cli_subagents.runner import Runner
from cli_subagents.runtimes import offered_runtimes
from cli_subagents.runtimes.base import AttemptResult, USAGE_LIMIT


class OpenCodeRegistrationTests(TempHome):
    def test_discovery_is_dynamic_and_the_off_switch_round_trips(self):
        with patch("cli_subagents.runtimes.opencode.find_program", return_value=None), patch.dict(
            os.environ, {"CLI_SUBAGENTS_OPENCODE_COMMAND": ""}
        ):
            self.assertNotIn("opencode", offered_runtimes())
            with patch("cli_subagents.runtimes.opencode.find_program", return_value="opencode.exe"):
                self.assertIn("opencode", offered_runtimes())
                (self.home / "config.toml").write_text("[opencode]\nenabled = false\n", encoding="utf-8")
                config = load_config()
                self.assertFalse(config.runtime("opencode").enabled)
                self.assertNotIn("opencode", offered_runtimes())
                (self.home / "config.toml").write_text(dump_config(config), encoding="utf-8")
                self.assertFalse(load_config().runtime("opencode").enabled)

    def test_only_available_optional_runtime_is_advertised_to_mcp(self):
        with patch("cli_subagents.tools.offered_runtimes", return_value=("claude", "codex", "antigravity")):
            definitions = tools.definitions()
        start = next(item for item in definitions if item["name"] == "subagent_start")
        self.assertNotIn("opencode", start["inputSchema"]["properties"]["runtime"]["enum"])
        with patch("cli_subagents.tools.offered_runtimes", return_value=("claude", "codex", "antigravity", "opencode")):
            definitions = tools.definitions()
        start = next(item for item in definitions if item["name"] == "subagent_start")
        self.assertIn("opencode", start["inputSchema"]["properties"]["runtime"]["enum"])

    def test_default_profile_uses_xdg_config_root_and_no_rotation(self):
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "xdg")}):
            self.assertEqual(default_primary("opencode"), self.root / "xdg" / "opencode")
        self.assertEqual(load_config().runtime("opencode").failover, "off")
        (self.home / "config.toml").write_text('[opencode]\nenabled = "false"\n', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "enabled must be true or false"):
            load_config()

    def test_extra_accounts_are_explicitly_deferred(self):
        (self.home / "config.toml").write_text('[[opencode.accounts]]\nname = "extra"\n', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "extra accounts are not supported yet"):
            load_config()

    def test_shared_limit_stops_runner_without_trying_another_account(self):
        self.write_config()
        status = jobs.create_job(runtime="claude", prompt="fake", cwd=str(self.project), start=False)
        runner = Runner(status["job_id"])
        lease = Mock(account=runner.runtime_config.find("a1"))
        reset = time.time() + 3600
        result = AttemptResult(USAGE_LIMIT, "Free usage limit reached; resets at UTC midnight", resets_at=reset,
                               no_rotation=True)
        with patch.object(runner, "_wait_for_account", side_effect=[lease, None]) as wait, patch.object(
            runner, "_attempt_with_capacity_retries", return_value=result
        ), patch.object(accounts, "set_cooldown") as cooldown:
            self.assertEqual(runner.run(), 1)
        self.assertEqual(wait.call_count, 1)
        lease.release.assert_called_once()
        self.assertEqual(cooldown.call_args.args[2], reset)
        self.assertEqual(jobs.read_status(status["job_id"])["outcome"], USAGE_LIMIT)


class OpenCodeMcpTests(TempHome):
    def test_codex_parent_config_and_header_environment(self):
        (self.primary / "config.toml").write_text('''
[mcp_servers.local]
command = "python"
args = ["server.py"]
env_vars = ["LOCAL_TOKEN"]
[mcp_servers.remote]
url = "https://example.test/mcp"
bearer_token_env_var = "REMOTE_TOKEN"
env_http_headers = {X-Team = "TEAM"}
[mcp_servers.disabled]
command = "skip"
enabled = false
[mcp_servers.cli-subagents]
command = "csa"
''', encoding="utf-8")
        got = mcp_translate.collect_native_servers("codex", self.primary, str(self.project),
            {"LOCAL_TOKEN": "local-secret", "REMOTE_TOKEN": "remote-secret", "TEAM": "my-team"})
        self.assertEqual(set(got.servers), {"local", "remote"})
        self.assertEqual(got.servers["local"]["env"], {"LOCAL_TOKEN": "local-secret"})
        self.assertEqual(got.servers["remote"]["headers"], {"X-Team": "my-team", "Authorization": "Bearer remote-secret"})
        missing = mcp_translate.collect_native_servers("codex", self.primary, str(self.project), {})
        self.assertFalse(missing.servers)
        self.assertEqual(len(missing.warnings), 2)

    def test_antigravity_parent_config_translates_remote_and_skips_self(self):
        (self.primary / "config").mkdir()
        (self.primary / "config" / "mcp_config.json").write_text(json.dumps({"mcpServers": {
            "remote": {"serverUrl": "https://example.test/mcp", "headers": {"X-Team": "team"}},
            "local": {"command": "python", "args": ["server.py"]},
            "cli-subagents": {"command": "csa"},
            "disabled": {"command": "skip", "disabled": True},
        }}), encoding="utf-8")
        got = mcp_translate.collect_native_servers("antigravity", self.primary / "antigravity-cli", str(self.project))
        self.assertEqual(set(got.servers), {"remote", "local"})
        self.assertEqual(got.servers["remote"]["url"], "https://example.test/mcp")
        translated = mcp_translate.to_opencode(got.servers)
        self.assertEqual(translated.servers["remote"]["type"], "remote")

    def test_native_mcp_shape_and_private_value_references(self):
        shared = mcp_translate.to_opencode({
            "csa-parent": {"command": "python", "args": ["-m", "cli_subagents"],
                           "env": {"CLI_SUBAGENTS_HOME": str(self.home)}, "cwd": str(self.project)},
            "remote": {"type": "http", "url": "https://example.test/mcp",
                       "headers": {"Authorization": "Bearer private-test-token"}},
            "sse": {"type": "sse", "url": "https://example.test/sse"},
            "unsupported": {"type": "socket"},
        })
        self.assertEqual(set(shared.servers), {"csa-parent", "remote", "sse"})
        self.assertTrue(all(entry["codemode"] is False for entry in shared.servers.values()))
        parent = shared.servers["csa-parent"]
        self.assertEqual(parent["type"], "local")
        self.assertEqual(parent["cwd"], str(self.project))
        self.assertIn("CLI_SUBAGENTS_HOME", parent["environment"])
        config = json.dumps(shared.servers)
        self.assertNotIn("private-test-token", config)
        self.assertIn("Bearer private-test-token", shared.env.values())
        self.assertIn("{env:CSA_OPENCODE_MCP_", config)
        self.assertEqual(len(shared.warnings), 1)

    def test_headers_helper_is_skipped_without_disclosing_its_value(self):
        shared = mcp_translate.to_opencode({
            "helper": {"type": "http", "url": "https://example.test", "headersHelper": "private-helper"}
        })
        self.assertFalse(shared.servers)
        self.assertNotIn("private-helper", str(shared.warnings))

