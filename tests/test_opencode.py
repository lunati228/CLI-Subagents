import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

from helpers import TempHome

from cli_subagents import opencode_review
from cli_subagents.agy_review.context import write_context
from cli_subagents.config import RuntimeConfig
from cli_subagents.runtimes.base import JobSpec
import cli_subagents
from cli_subagents.runtimes.opencode import (DEFAULT_MODEL, FORCE_ESCAPE_VARIABLE, GATE_MODEL, OpenCodeParser,
                                             OpenCodeRuntime, project_context)


FAKE = Path(__file__).with_name("fake_opencode.py")


class ParserTests(unittest.TestCase):
    def error(self, value, exit_code=1):
        parser = OpenCodeParser()
        parser.feed({"type": "text", "sessionID": "ses_test", "part": {"text": "partial answer"}})
        parser.feed({"type": "error", "error": value})
        return parser.finish(exit_code, "")

    def test_text_session_progress_and_usage_sum(self):
        parser = OpenCodeParser()
        parser.feed({"type": "step_start", "sessionID": "ses_test"})
        parser.feed({"type": "text", "part": {"text": "first"}})
        parser.feed({"type": "reasoning", "part": {"text": "thinking"}})
        tool = {"type": "tool_use", "part": {"id": "t1", "tool": "read"}}
        parser.feed(tool)
        parser.feed(tool)
        for index in range(2):
            parser.feed({"type": "step_finish", "part": {"id": str(index), "cost": 0.1,
                        "tokens": {"input": 10, "output": 3, "cache": {"read": 5}}}})
        parser.feed({"type": "text", "part": {"text": "last"}})
        result = parser.finish(0, "")
        self.assertEqual((result.outcome, result.text, result.session_id), ("success", "last", "ses_test"))
        self.assertEqual(result.usage, {"input": 20, "output": 6, "cache": {"read": 10}})
        self.assertAlmostEqual(result.cost_usd, 0.2)
        self.assertEqual(result.num_turns, 2)
        self.assertEqual(parser.progress()["tool_uses"], 1)
        self.assertEqual(parser.progress()["last_reasoning"], "thinking")

    def test_free_limit_has_utc_midnight_reset_and_no_rotation(self):
        from datetime import datetime, timezone

        result = self.error({"type": "provider.free-limit", "message": "Free usage limit reached"})
        self.assertEqual(result.outcome, "usage_limit")
        self.assertTrue(result.no_rotation)
        reset = datetime.fromtimestamp(result.resets_at, timezone.utc)
        self.assertEqual((reset.hour, reset.minute, reset.second), (0, 0, 0))
        self.assertIn("UTC", result.message)
        self.assertIn("Changing accounts", result.message)
        self.assertFalse(result.account_cooldown)

    def test_rate_limit_and_quota_use_existing_outcome(self):
        import time

        before = time.time()
        rate = self.error({"type": "provider.rate-limit", "message": "limited", "retryAfterMs": 30000})
        self.assertEqual(rate.outcome, "usage_limit")
        self.assertFalse(rate.no_rotation)
        self.assertGreaterEqual(rate.resets_at, before + 30)
        self.assertEqual(self.error({"type": "provider.quota", "message": "no credits"}).outcome, "usage_limit")

    def test_auth_and_clear_capacity(self):
        self.assertEqual(self.error({"type": "provider.auth", "message": "failed"}).outcome, "auth")
        result = self.error({"type": "provider.internal", "message": "Model is overloaded"})
        self.assertEqual(result.outcome, "error")
        self.assertTrue(result.at_capacity)
        self.assertFalse(self.error({"type": "provider.internal", "message": "internal failure"}).at_capacity)

    def test_v1_error_shape_still_gives_clear_failure(self):
        self.assertEqual(self.error({"name": "APIError", "data": {"message": "Rate limit exceeded", "statusCode": 429}}).outcome,
                         "usage_limit")
        free = self.error({"name": "APIError", "data": {"message": "Rate limit exceeded", "responseBody": "FreeUsageLimitError"}})
        self.assertTrue(free.no_rotation)

    def test_error_event_is_failure_even_with_zero_exit(self):
        result = self.error({"type": "permission.rejected", "message": "denied"}, 0)
        self.assertEqual(result.outcome, "error")
        self.assertEqual(result.text, "partial answer")

    def test_no_route_errors_name_the_plugin_model_or_variant(self):
        gate = self.error({"type": "provider.no-route", "message": f"Model unavailable: {GATE_MODEL}"})
        self.assertEqual(gate.outcome, "error")
        self.assertIn("plugin did not load", gate.message)
        missing = self.error({"type": "provider.no-route", "message": f"Model unavailable: {DEFAULT_MODEL}"})
        self.assertIn(f"{DEFAULT_MODEL} is not available", missing.message)
        self.assertIn("does not fall back", missing.message)
        variant = self.error({"type": "provider.no-route",
                              "message": f"Variant unavailable for {DEFAULT_MODEL}: medium"})
        self.assertEqual(variant.outcome, "error")
        self.assertIn("no 'medium' variant", variant.message)
        self.assertFalse(variant.at_capacity)

    def test_no_events_and_nonzero_exit_fail(self):
        self.assertEqual(OpenCodeParser().finish(0, "").outcome, "error")
        parser = OpenCodeParser()
        parser.feed({"type": "text", "part": {"text": "answer"}})
        self.assertEqual(parser.finish(1, "late CLI failure").outcome, "error")


class OpenCodeHome(TempHome):
    def setUp(self):
        super().setUp()
        for name in list(os.environ):
            if name.startswith(("OPENCODE_", "FAKE_OPENCODE_")):
                os.environ.pop(name)
        os.environ["CLI_SUBAGENTS_OPENCODE_COMMAND"] = json.dumps([sys.executable, str(FAKE)])
        for name in ("DATA", "CONFIG", "STATE", "CACHE"):
            os.environ[f"XDG_{name}_HOME"] = str(self.root / name.lower())
        # A parent-context test may collect native MCP settings. Every source
        # points into this test's temporary tree, never a real CLI profile.
        lines = []
        for runtime in ("claude", "codex", "antigravity"):
            lines += [f"[{runtime}]", f"primary = {json.dumps(str(self.root / ('parent-' + runtime)))}"]
        (self.home / "config.toml").write_text("\n".join(lines), encoding="utf-8")
        self.config = RuntimeConfig(runtime="opencode", primary=self.root / "config" / "opencode")
        self.account = self.config.primary_account()
        self.runtime = OpenCodeRuntime()

    def spec(self, **values):
        return JobSpec(job_id="test-opencode", runtime="opencode", prompt="task", cwd=str(self.project), **values)

    def launch(self, **values):
        spec = self.spec(**values)
        return self.runtime.launch(self.config, self.account, spec, "task", {})

    @staticmethod
    def plugin_settings(launch):
        data = json.loads(launch.env["OPENCODE_CONFIG_CONTENT"])
        source = (Path(data["plugins"][-1]) / "server.js").read_text(encoding="utf-8")
        return data, json.loads(source.split("const settings = ", 1)[1].split(";\n", 1)[0])

    def run_fake(self, **values):
        launch = self.launch(**values)
        result = subprocess.run(launch.argv, cwd=self.project, env=launch.env, input=launch.stdin,
                                capture_output=True, encoding="utf-8", timeout=20)
        parser = OpenCodeParser()
        for line in result.stdout.splitlines():
            parser.feed(json.loads(line))
        return parser.finish(result.returncode, result.stderr), launch


class LaunchTests(OpenCodeHome):
    def test_binary_is_optional_and_disable_wins_over_override(self):
        self.assertEqual(self.runtime.find_binary(self.config), [sys.executable, str(FAKE)])
        self.config.enabled = False
        self.assertIsNone(self.runtime.find_binary(self.config))
        self.config.enabled = True
        with patch.dict(os.environ, {"CLI_SUBAGENTS_OPENCODE_COMMAND": ""}), patch("cli_subagents.runtimes.opencode.shutil.which", return_value=None):
            self.assertIsNone(self.runtime.find_binary(self.config))

    def test_windows_npm_shim_uses_node_directly(self):
        shim = self.root / "npm" / "opencode.cmd"
        launcher = shim.parent / "node_modules" / "@opencode" / "cli" / "bin" / "opencode.cjs"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("fake", encoding="utf-8")
        shim.write_text("fake", encoding="utf-8")
        node = shim.parent / "node.exe"
        node.write_text("fake", encoding="utf-8")
        with patch.dict(os.environ, {"CLI_SUBAGENTS_OPENCODE_COMMAND": ""}), patch("cli_subagents.runtimes.opencode.shutil.which", return_value=str(shim)):
            self.assertEqual(self.runtime.find_binary(self.config), [str(node), str(launcher)])

    def test_windows_npm_shim_prefers_the_published_native_binary(self):
        shim = self.root / "npm" / "opencode.cmd"
        native = shim.parent / "node_modules" / "@opencode" / "cli" / "bin" / "opencode.exe"
        native.parent.mkdir(parents=True)
        native.write_text("fake", encoding="utf-8")
        (native.parent / "opencode.cjs").write_text("fake", encoding="utf-8")
        shim.write_text("fake", encoding="utf-8")
        with patch.dict(os.environ, {"CLI_SUBAGENTS_OPENCODE_COMMAND": ""}), patch("cli_subagents.runtimes.opencode.shutil.which", return_value=str(shim)):
            self.assertEqual(self.runtime.find_binary(self.config), [str(native)])

    def test_real_v2_version_output_is_accepted(self):
        for output in ("opencode v2.1.4", "2.1.4", "v2.0"):
            with self.subTest(output=output):
                os.environ["FAKE_OPENCODE_VERSION"] = output
                self.runtime._checked = None
                self.assertIsNone(self.runtime.preflight(self.config, self.account))
        for output in ("opencode v1.19.4", "1.19.4"):
            with self.subTest(output=output):
                os.environ["FAKE_OPENCODE_VERSION"] = output
                self.runtime._checked = None
                self.assertEqual(self.runtime.preflight(self.config, self.account).outcome, "error")

    def test_v1_version_refused_before_run(self):
        os.environ["FAKE_OPENCODE_VERSION"] = "1.19.4"
        blocked = self.runtime.preflight(self.config, self.account)
        self.assertEqual(blocked.outcome, "error")
        self.assertTrue(blocked.before_start)
        self.assertIn("@opencode/cli", blocked.message)
        with self.assertRaisesRegex(RuntimeError, "v2"):
            self.launch()
        self.assertEqual(self.records(), [])

    def test_read_permissions_and_gate(self):
        launch = self.launch(access="read")
        data, settings = self.plugin_settings(launch)
        rules = {item["action"]: item["effect"] for item in settings["permissions"]}
        self.assertEqual([rules[name] for name in ("edit", "shell", "subagent")], ["deny"] * 3)
        # Until the plugin copies the build agent, the worker agent asks (= rejects) everything.
        self.assertEqual(data["agents"]["csa-worker"]["permissions"], [{"action": "*", "resource": "*", "effect": "ask"}])
        self.assertNotIn("--auto", launch.argv)
        self.assertIn("--standalone", launch.argv)
        self.assertEqual(launch.argv[launch.argv.index("-m") + 1], GATE_MODEL)
        plugin = Path(data["plugins"][-1])
        self.assertTrue((plugin / "server.js").is_file())
        context = json.loads((plugin.parent / "review-context.json").read_text(encoding="utf-8"))
        self.assertEqual((context["access"], context["sandbox"], context["reviewers"][0]["kind"]), ("read", False, "claude"))
        self.assertEqual(settings["context"], str(plugin.parent / "review-context.json"))

    def test_edit_defaults_external_directory_asks_and_add_dirs(self):
        extra = self.root / "extra"
        extra.mkdir()
        launch = self.launch(access="edit", add_dirs=[str(extra)])
        data, settings = self.plugin_settings(launch)
        self.assertEqual(settings["permissions"][0], {"action": "external_directory", "resource": "*", "effect": "ask"})
        self.assertIn({"action": "external_directory", "resource": extra.resolve().as_posix() + "/*", "effect": "allow"},
                      settings["permissions"])
        self.assertNotIn("edit", {item["action"] for item in settings["permissions"]})
        self.assertNotIn("--auto", launch.argv)
        self.assertFalse(data["autoupdate"])
        self.assertTrue(settings["context"])

    def test_full_auto_skips_review_hook_but_keeps_model_gate(self):
        launch = self.launch(access="full")
        _data, settings = self.plugin_settings(launch)
        self.assertIn("--auto", launch.argv)
        self.assertEqual(launch.argv[launch.argv.index("-m") + 1], GATE_MODEL)
        self.assertEqual((settings["context"], settings["permissions"]), (None, []))
        self.assertEqual((settings["model"], settings["variant"], settings["fallback"]), (None, None, DEFAULT_MODEL))
        self.assertIn("big-pickle", settings["restricted"])
        self.assertIn("mimo-", settings["restricted"])

    def test_explicit_model_effort_and_resume(self):
        launch = self.launch(model="provider/model#low", effort="high", resume_session="ses_earlier", max_turns=4)
        _data, settings = self.plugin_settings(launch)
        self.assertEqual((settings["model"], settings["variant"], settings["steps"]), ("provider/model", "high", 4))
        self.assertEqual(launch.argv[launch.argv.index("-m") + 1], GATE_MODEL)
        self.assertEqual(launch.argv[launch.argv.index("-s") + 1], "ses_earlier")
        self.assertEqual(launch.session_id, "ses_earlier")
        _data, settings = self.plugin_settings(self.launch(model="opencode/deepseek-v4.1-flash-free#max"))
        self.assertEqual((settings["model"], settings["variant"]), (DEFAULT_MODEL, "max"))

    def test_unspecified_model_preserves_native_default_and_applies_effort(self):
        os.environ["OPENCODE_CONFIG_CONTENT"] = json.dumps({"model": "provider/private-model", "instructions": ["own.md"]})
        launch = self.launch(effort="high")
        data, settings = self.plugin_settings(launch)
        self.assertEqual(data["model"], "provider/private-model")
        self.assertEqual((settings["model"], settings["variant"]), (None, "high"))
        self.assertEqual(launch.warnings, [])
        self.assertEqual(data["instructions"], ["own.md"])
        self.config.default_model = "opencode/glm-5.3-flash-free"
        _data, settings = self.plugin_settings(self.launch())
        self.assertEqual((settings["model"], settings["variant"]), ("opencode/glm-5.3-flash-free", None))

    def test_invalid_model_or_variant_is_refused_before_start(self):
        for values in ({"model": "no-provider"}, {"model": "provider/model#"}, {"model": "/model"},
                       {"effort": "high; rm"}, {"model": "provider/model#a#b"}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.launch(**values)

    def test_environment_and_parent_instructions_and_mcp(self):
        instruction = self.root / "parent.md"
        instruction.write_text("rules", encoding="utf-8")
        with patch.dict(os.environ, {"OPENCODE_API_KEY": "test-provider-key", "CLAUDECODE": "host-session", "PWD": str(self.root)}):
            launch = self.launch(parent={"host": "codex", "instructions": [str(instruction)], "skills": [str(self.root / "skills")]})
        self.assertNotIn("CLAUDECODE", launch.env)
        self.assertEqual(launch.env["OPENCODE_API_KEY"], "test-provider-key")
        self.assertEqual(launch.env["PWD"], str(self.project.resolve()))
        for name in ("DATA", "CONFIG", "STATE", "CACHE"):
            self.assertEqual(launch.env[f"XDG_{name}_HOME"], str(self.root / name.lower()))
        data = json.loads(launch.env["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(data["instructions"], [str(instruction)])
        self.assertFalse(data["mcp"]["servers"]["csa-parent"]["codemode"])
        self.assertIn("csa-parent tools", launch.stdin)
        # Instructions and skills come through config; no note pointing outside the scope.
        self.assertNotIn(str(instruction), launch.stdin)

    def test_guarded_jobs_ignore_project_config_formatters_and_parent_servers(self):
        parent = {"host": "codex", "instructions": [], "skills": []}
        with patch.object(OpenCodeRuntime, "_parent_servers", return_value={"other": {"command": "tool"}}):
            for access in ("read", "edit", "full"):
                with self.subTest(access=access):
                    launch = self.launch(access=access, parent=parent)
                    data, settings = self.plugin_settings(launch)
                    guarded = access != "full"
                    for name in ("OPENCODE_DISABLE_PROJECT_CONFIG", "OPENCODE_CONFIG_PROJECT_DISABLE"):
                        self.assertEqual(launch.env.get(name), "1" if guarded else None)
                    self.assertEqual(("formatter" in data, "lsp" in data), (guarded, guarded))
                    if guarded:
                        self.assertEqual((data["formatter"], data["lsp"]), (False, False))
                    self.assertEqual("other" in data["mcp"]["servers"], not guarded)
                    self.assertIn("csa-parent", data["mcp"]["servers"])
                    self.assertEqual(settings["guarded"], guarded)

    def test_plugin_id_is_random_and_not_in_the_environment(self):
        _data, first = self.plugin_settings(launch := self.launch())
        _data, second = self.plugin_settings(self.launch())
        self.assertTrue(first["id"].startswith("cli-subagents.worker."))
        self.assertNotEqual(first["id"], second["id"])
        self.assertFalse(any(first["id"] in value for value in launch.env.values()))
        context = json.loads((self.home / "jobs" / "test-opencode" / "review-context.json").read_text(encoding="utf-8"))
        # Reads of csa's own package stay allowed; opencode_review refuses edits there.
        self.assertNotIn(str(Path(cli_subagents.__file__).resolve().parent), context["protected_paths"])

    def test_project_agents_files_and_skill_folders_are_listed_explicitly(self):
        repo = self.root / "repo"
        sub = repo / "pkg" / "sub"
        sub.mkdir(parents=True)
        (repo / ".git").mkdir()
        for folder in (repo, sub):
            (folder / "AGENTS.md").write_text("rules", encoding="utf-8")
        (self.root / "AGENTS.md").write_text("outside the repository", encoding="utf-8")
        for name in (".claude", ".agents"):
            (repo / name / "skills").mkdir(parents=True)
        (sub / ".claude" / "skills").mkdir(parents=True)
        fake_home = self.root / "elsewhere"
        fake_home.mkdir()
        with patch.object(Path, "home", return_value=fake_home):
            instructions, skills = project_context(str(sub))
            self.assertEqual(instructions, [str(sub / "AGENTS.md"), str(repo / "AGENTS.md")])
            for folder in (sub / ".claude" / "skills", repo / ".claude" / "skills", repo / ".agents" / "skills"):
                self.assertIn(str(folder), skills)
            launch = self.runtime.launch(self.config, self.account, JobSpec(
                job_id="test-opencode", runtime="opencode", prompt="task", cwd=str(sub), access="read"), "task", {})
            full = self.runtime.launch(self.config, self.account, JobSpec(
                job_id="test-opencode", runtime="opencode", prompt="task", cwd=str(sub), access="full"), "task", {})
        data = json.loads(launch.env["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(data["instructions"][:2], [str(sub / "AGENTS.md"), str(repo / "AGENTS.md")])
        self.assertIn(str(repo / ".agents" / "skills"), data["skills"])
        # Full jobs keep OpenCode's own project discovery.
        self.assertNotIn("instructions", json.loads(full.env["OPENCODE_CONFIG_CONTENT"]))
        # Inside the home folder the walk goes up to it, as OpenCode's does.
        with patch.object(Path, "home", return_value=self.root):
            instructions, skills = project_context(str(sub))
        self.assertEqual(instructions, [str(sub / "AGENTS.md"), str(repo / "AGENTS.md"), str(self.root / "AGENTS.md")])

    def test_env_references_always_take_the_escaped_substitution_pass(self):
        import re

        server = {"command": "D:\\repos\\tools\\run.exe", "args": ["--name", "line\\none"]}
        with patch.object(OpenCodeRuntime, "_parent_servers", return_value={"tool": server}):
            launch = self.launch(access="full", parent={"host": "codex"})
        text = launch.env["OPENCODE_CONFIG_CONTENT"]

        def substitute(escape):
            def value(match):
                raw = launch.env.get(match.group(1), "")
                return json.dumps(raw)[1:-1] if escape else raw
            return re.sub(r"\{env:([^}]+)\}", value, text)

        # What OpenCode v2.1.4 does: keep the raw pass only if it parses.
        with self.assertRaises(ValueError):
            json.loads(substitute(False))
        data = json.loads(substitute(True))
        self.assertEqual(data["mcp"]["servers"]["tool"]["command"], ["D:\\repos\\tools\\run.exe", "--name", "line\\none"])
        self.assertEqual(data["mcp"]["servers"]["csa-parent"]["environment"][FORCE_ESCAPE_VARIABLE], '"')

    def test_fake_success_resume_outcomes_and_denied_command(self):
        result, _ = self.run_fake(resume_session="ses_earlier")
        self.assertEqual((result.outcome, result.session_id, result.text), ("success", "ses_earlier", "opencode done after resume"))
        self.assertEqual(result.usage["input"], 10)
        for scenario, outcome in (("free_limit", "usage_limit"), ("rate_limit", "usage_limit"), ("auth", "auth"),
                                  ("capacity", "error"), ("v1_error", "usage_limit"), ("success_exit_1", "error"),
                                  ("plugin_missing", "error"), ("bad_variant", "error")):
            with self.subTest(scenario=scenario), patch.dict(os.environ, {"FAKE_OPENCODE_SCENARIO": scenario}):
                result, _ = self.run_fake()
                self.assertEqual(result.outcome, outcome)
        with patch.dict(os.environ, {"FAKE_OPENCODE_SCENARIO": "plugin_missing"}):
            result, _ = self.run_fake()
        self.assertIn("plugin did not load", result.message)
        with patch.dict(os.environ, {"FAKE_OPENCODE_SCENARIO": "denied_command"}):
            result, _ = self.run_fake()
        self.assertEqual((result.outcome, result.text), ("success", "denied command was blocked"))


class ReviewTests(OpenCodeHome):
    def setUp(self):
        super().setUp()
        self.file = self.project / "source.py"
        self.file.write_text("print('example')\n", encoding="utf-8")
        self.context = self.root / "review-context.json"
        self.context_data = {"version": 1, "access": "edit", "sandbox": False, "workspaces": [str(self.project)],
                             "user_home": str(self.root), "read_paths": [], "protected_paths": [],
                             "trusted_mcp_servers": ["csa-parent"], "reviewer": {"kind": "off"}}
        write_context(self.context, self.context_data)

    def review(self, tool, args):
        return opencode_review.run(json.dumps({"tool": tool, "input": args}).encode(), self.context)

    def test_gate_blocks_denied_command_and_unknown_shell_writes(self):
        self.assertEqual(self.review("shell", {"command": "rm -rf /"})["decision"], "deny")
        self.assertEqual(self.review("shell", {"command": "python build.py"})["decision"], "deny")
        self.assertEqual(self.review("shell", {"command": "git status"})["decision"], "auto_approve")
        self.assertEqual(self.review("shell", {"command": "git status", "workdir": str(self.root)})["decision"], "deny")

    def test_read_edit_patch_and_external_paths(self):
        self.assertEqual(self.review("read", {"path": str(self.file)})["decision"], "auto_approve")
        self.assertEqual(self.review("edit", {"path": str(self.file)})["decision"], "auto_approve")
        patch_text = "*** Begin Patch\n*** Update File: source.py\n*** Move to: ../outside.py\n*** End Patch"
        self.assertEqual(self.review("patch", {"patchText": patch_text})["decision"], "deny")
        self.assertEqual(self.review("write", {"path": str(self.root / "outside.txt")})["decision"], "deny")
        self.context_data["access"] = "read"
        write_context(self.context, self.context_data)
        self.assertEqual(self.review("edit", {"path": str(self.file)})["decision"], "deny")
        self.assertEqual(self.review("shell", {"command": "git status"})["decision"], "deny")

    def test_patch_paths_are_read_like_opencode_reads_them(self):
        hook = "*** Begin Patch\n*** Add File: x{}/../.git/hooks/pre-commit\n+echo\n*** End Patch"
        for separator in ("\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"):
            with self.subTest(separator=repr(separator)):
                self.assertEqual(self.review("patch", {"patchText": hook.format(separator)})["decision"], "deny")
        crlf = "*** Begin Patch\r\n*** Update File: source.py\r\n@@\r\n-a\r\n+b\r\n*** End Patch\r\n"
        self.assertEqual(self.review("patch", {"patchText": crlf})["decision"], "auto_approve")
        moved = "*** Begin Patch\n*** Update File: source.py\n*** Move to: .git/config  \r\n*** End Patch"
        self.assertEqual(self.review("patch", {"patchText": moved})["decision"], "deny")
        self.assertEqual(opencode_review.patch_targets("*** Add File: \u3000a.py\x1f\n"), ["a.py\x1f"])

    def test_folder_grep_is_refused_when_it_could_read_secrets(self):
        (self.project / ".env").write_text("API_KEY=secret\n", encoding="utf-8")
        (self.project / "node_modules").mkdir()
        (self.project / "node_modules" / "key.pem").write_text("skipped", encoding="utf-8")
        for access in ("read", "edit"):
            self.context_data["access"] = access
            write_context(self.context, self.context_data)
            with self.subTest(access=access):
                self.assertEqual(self.review("read", {"path": ".env"})["decision"], "deny")
                denied = self.review("grep", {"pattern": "API_KEY"})
                self.assertEqual(denied["decision"], "deny")
                self.assertIn(".env", denied["reason"])
                self.assertIn("include", denied["reason"])
                for include in (".env", ".env*", "*", "**/.env", "*.{py,env}", "!*.py"):
                    with self.subTest(include=include):
                        self.assertEqual(self.review("grep", {"pattern": ".", "include": include})["decision"], "deny")
                self.assertEqual(self.review("grep", {"pattern": "print", "include": "*.py"})["decision"], "auto_approve")
                self.assertEqual(self.review("grep", {"pattern": "API", "path": str(self.file)})["decision"], "auto_approve")
        with patch.object(opencode_review, "GREP_FILE_LIMIT", 1):
            (self.project / "other.py").write_text("x", encoding="utf-8")
            limited = self.review("grep", {"pattern": "x", "include": "*.py"})
        self.assertEqual(limited["decision"], "deny")
        self.assertIn("too many", limited["reason"])

    def test_node_modules_is_skipped_only_when_git_ignores_it(self):
        # rg searches node_modules unless .gitignore covers it, so csa must check it too.
        (self.project / "node_modules").mkdir()
        (self.project / "node_modules" / ".env").write_text("API_KEY=secret\n", encoding="utf-8")
        denied = self.review("grep", {"pattern": "API_KEY"})
        self.assertEqual(denied["decision"], "deny")
        self.assertIn("node_modules", denied["reason"])
        if not shutil.which("git"):
            self.skipTest("git is needed to check whether .gitignore covers node_modules")
        subprocess.run(["git", "init", "-q"], cwd=str(self.project), capture_output=True, check=True, timeout=60)
        (self.project / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
        self.assertEqual(self.review("grep", {"pattern": "API_KEY"})["decision"], "auto_approve")

    def test_csa_package_can_be_read_but_not_edited(self):
        package = opencode_review.PACKAGE_DIR
        self.context_data.update(workspaces=[str(package.parent)])
        for access in ("read", "edit"):
            self.context_data["access"] = access
            write_context(self.context, self.context_data)
            with self.subTest(access=access):
                self.assertEqual(self.review("read", {"path": str(package / "hooks.py")})["decision"], "auto_approve")
        denied = self.review("edit", {"path": str(package / "opencode_review" / "__init__.py")})
        self.assertEqual(denied["decision"], "deny")
        self.assertIn("csa's own package", denied["reason"])
        shadow = self.review("write", {"path": str(package.parent / "stdlib_shadow.py")})
        self.assertEqual(shadow["decision"], "deny")
        patch_text = f"*** Begin Patch\n*** Update File: {package.name}/hooks.py\n*** End Patch"
        self.assertEqual(self.review("patch", {"patchText": patch_text})["decision"], "deny")

    def test_folder_search_inside_the_workspace_is_allowed_in_read_jobs(self):
        self.context_data["access"] = "read"
        write_context(self.context, self.context_data)
        self.assertEqual(self.review("grep", {"pattern": "print"})["decision"], "auto_approve")
        self.assertEqual(self.review("grep", {"pattern": "print", "path": str(self.project)})["decision"], "auto_approve")
        self.assertEqual(self.review("glob", {"pattern": "*.py"})["decision"], "auto_approve")
        self.assertEqual(self.review("read", {"path": str(self.project)})["decision"], "auto_approve")
        self.assertEqual(self.review("grep", {"pattern": "x", "path": str(self.root)})["decision"], "deny")

    def test_opencode_project_config_is_write_protected(self):
        for target in ("opencode.json", "opencode.jsonc", "sub/opencode.json", ".opencode/plugins/run.js",
                       ".opencode/agents/a.md"):
            with self.subTest(target=target):
                self.assertEqual(self.review("write", {"path": str(self.project / target)})["decision"], "deny")

    def test_malformed_and_unknown_tools_fail_closed(self):
        self.assertEqual(self.review("read", {})["decision"], "deny")
        self.assertEqual(self.review("grep", {})["decision"], "deny")
        self.assertEqual(self.review("glob", {})["decision"], "deny")
        self.assertEqual(self.review("unfamiliar", {})["decision"], "deny")
        self.assertEqual(opencode_review.run(b"invalid JSON", self.context)["decision"], "deny")

    def test_csa_parent_tools_approved_and_other_mcp_denied(self):
        self.assertEqual(self.review("csa-parent_message_parent", {"message": "progress"})["decision"], "auto_approve")
        self.assertEqual(self.review("mail_send", {"body": "example"})["decision"], "deny")

    def run_plugin(self, launch, script):
        """Run the generated plugin in Node against a small stand-in for OpenCode v2's Promise plugin API."""

        plugin = Path(json.loads(launch.env["OPENCODE_CONFIG_CONTENT"])["plugins"][-1]) / "server.js"
        harness = self.root / "test-plugin.mjs"
        harness.write_text(PLUGIN_HARNESS + script, encoding="utf-8")
        result = subprocess.run([shutil.which("node"), str(harness), str(plugin)],
                                cwd=self.project, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip().splitlines()

    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute the generated JS plugin")
    def test_plugin_registers_gate_before_model_and_blocks_denied_calls(self):
        lines = self.run_plugin(self.launch(access="read"), '''
const ctx = makeContext({ nativeDefault: { providerID: "private", modelID: "own" } });
await plugin.setup(ctx);
if (ctx.order.join() !== "tool:execute.before,shell:create.before,session:model.request,session:prompt") throw Error(ctx.order.join());
try { await ctx.hooks["tool:execute.before"]({ tool: "shell", input: { command: "rm -rf /" } }); throw Error("not blocked"); }
catch (error) { if (!error.message.includes("CLI Subagents review")) throw error; }
await ctx.hooks["session:prompt"]({ sessionID: "ses_1" });
console.log(JSON.stringify(ctx.switched));
console.log(JSON.stringify(ctx.worker.permissions.at(-1)));
''')
        self.assertEqual(json.loads(lines[0]), [{"sessionID": "ses_1", "model": {"providerID": "private", "id": "own"}}])
        self.assertEqual(json.loads(lines[1])["effect"], "deny")

    MODEL_SCRIPT = '''
const ctx = makeContext(JSON.parse(process.env.CASE || "{}"));
await plugin.setup(ctx);
await ctx.hooks["session:prompt"]({ sessionID: "ses_1" });
await ctx.hooks["session:prompt"]({ sessionID: "ses_child" });
const model = ctx.switched[0].model;
const tried = ["big-pickle", "mimo-v2.6-flash-free", "nemotron-3-ultra-free", "flint-40b-free",
  "glm-5.3-flash-free", "muse-spark-1.3-contributor-free", "deepseek-v4.1-flash-free"];
const allowed = [];
for (const id of tried) {
  try { await ctx.hooks["session:model.request"]({ model: { providerID: "opencode", id } }); allowed.push(id); }
  catch (error) {}
}
console.log(JSON.stringify({ model, allowed, switched: ctx.switched.map(item => item.sessionID) }));
'''

    def plugin_case(self, native, **values):
        launch = self.launch(**values)
        with patch.dict(os.environ, {"CASE": json.dumps(native)}):
            return json.loads(self.run_plugin(launch, self.MODEL_SCRIPT)[0])

    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute the generated JS plugin")
    def test_plugin_model_choice_and_variant(self):
        cases = (
            ({}, {}, {"providerID": "opencode", "id": "deepseek-v4.1-flash-free"}),
            ({"effort": "max"}, {}, {"providerID": "opencode", "id": "deepseek-v4.1-flash-free", "variant": "max"}),
            ({"effort": "low"}, {"nativeDefault": {"providerID": "private", "modelID": "own"}},
             {"providerID": "private", "id": "own", "variant": "low"}),
            ({}, {"buildModel": {"providerID": "private", "id": "own", "variant": "x"}},
             {"providerID": "private", "id": "own", "variant": "x"}),
            ({"model": "opencode/glm-5.3-flash-free"}, {"buildModel": {"providerID": "private", "id": "own"}},
             {"providerID": "opencode", "id": "glm-5.3-flash-free"}),
        )
        for values, native, expected in cases:
            with self.subTest(values=values, native=native):
                self.assertEqual(self.plugin_case(native, **values)["model"], expected)

    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute the generated JS plugin")
    def test_guarded_jobs_reach_only_the_selected_model(self):
        got = self.plugin_case({}, access="read")
        self.assertEqual(got["allowed"], ["deepseek-v4.1-flash-free"])
        self.assertEqual(got["switched"], ["ses_1", "ses_child"])
        # A restricted model from OpenCode or project settings is refused even when selected...
        got = self.plugin_case({"buildModel": {"providerID": "opencode", "id": "big-pickle"}}, access="edit")
        self.assertEqual(got["allowed"], [])
        # ...and runs only when the csa job names it.
        got = self.plugin_case({}, access="edit", model="opencode/big-pickle")
        self.assertEqual(got["allowed"], ["big-pickle"])

    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute the generated JS plugin")
    def test_guarded_jobs_allow_the_configured_compaction_model(self):
        script = '''
const ctx = makeContext(JSON.parse(process.env.CASE));
await plugin.setup(ctx);
await ctx.hooks["session:prompt"]({ sessionID: "ses_1" });
const results = [];
for (const model of [{ providerID: "private", id: "summary" }, { providerID: "opencode", id: "big-pickle" },
                     { providerID: "private", id: "other" }]) {
  try { await ctx.hooks["session:model.request"]({ model }); results.push("allowed"); }
  catch (error) { results.push(error.message); }
}
console.log(JSON.stringify(results));
'''
        for compaction, expected in (({"providerID": "private", "id": "summary"}, "allowed"),
                                     ({"providerID": "opencode", "id": "big-pickle"}, "blocked")):
            with self.subTest(compaction=compaction):
                launch = self.launch(access="edit")
                with patch.dict(os.environ, {"CASE": json.dumps({"compactionModel": compaction})}):
                    results = json.loads(self.run_plugin(launch, script)[0])
                if expected == "allowed":
                    self.assertEqual(results[0], "allowed")
                self.assertIn("data-use terms", results[1])
                self.assertIn("compaction agent's model", results[2])
                self.assertNotIn("free models", results[2])

    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute the generated JS plugin")
    def test_full_jobs_block_restricted_models_and_leave_child_sessions_alone(self):
        got = self.plugin_case({}, access="full")
        self.assertEqual(got["allowed"], ["glm-5.3-flash-free", "muse-spark-1.3-contributor-free", "deepseek-v4.1-flash-free"])
        self.assertEqual(got["switched"], ["ses_1"])
        got = self.plugin_case({}, access="full", model="opencode/mimo-v2.6-flash-free")
        self.assertIn("mimo-v2.6-flash-free", got["allowed"])
        self.assertNotIn("big-pickle", got["allowed"])

    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute the generated JS plugin")
    def test_plugin_setup_failure_never_unlocks_the_model(self):
        lines = self.run_plugin(self.launch(access="edit"), '''
const ctx = makeContext({ failTool: true });
try { await plugin.setup(ctx); console.log("setup passed"); } catch (error) { console.log("setup failed"); }
console.log(Object.keys(ctx.hooks).join() || "none");
''')
        self.assertEqual(lines, ["setup failed", "none"])


# A stand-in for the parts of OpenCode v2.1.4's Promise plugin context the generated
# plugin uses (packages/plugin/src/promise/{plugin,tool,session,agent,model}.ts).
PLUGIN_HARNESS = '''import { pathToFileURL } from "node:url";
const plugin = (await import(pathToFileURL(process.argv[2]).href)).default;
function makeContext(options = {}) {
  const ctx = { hooks: {}, order: [], switched: [] };
  const register = domain => async (name, callback) => {
    if (domain === "tool" && options.failTool) throw Error("registration failed");
    ctx.order.push(`${domain}:${name}`);
    ctx.hooks[`${domain}:${name}`] = callback;
    return { dispose: async () => {} };
  };
  const build = { id: "build", name: "build", mode: "primary", hidden: false,
    permissions: [{ action: "*", resource: "*", effect: "allow" }], ...(options.buildModel ? { model: options.buildModel } : {}) };
  ctx.worker = { id: "csa-worker", name: "csa-worker", permissions: [{ action: "*", resource: "*", effect: "ask" }] };
  ctx.tool = { hook: register("tool") };
  ctx.shell = { hook: register("shell") };
  ctx.session = { hook: register("session"), switchModel: async input => { ctx.switched.push(input); } };
  const compaction = options.compactionModel ? { id: "compaction", model: options.compactionModel } : undefined;
  ctx.agent = { transform: async fn => { fn({ get: id => id === "build" ? build : id === "compaction" ? compaction : undefined,
    update: (id, update) => { if (id === "csa-worker") update(ctx.worker); } }); return { dispose: async () => {} }; } };
  ctx.model = { transform: async fn => { fn({ default: { get: () => options.nativeDefault, set: () => { throw Error("default changed"); } } });
    return { dispose: async () => {} }; } };
  return ctx;
}
'''


if __name__ == "__main__":
    unittest.main()
