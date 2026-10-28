"""OpenCode v2 workers: private server, JSON events and native user settings.

The primary uses OpenCode's normal XDG directories and configured providers.
Read/edit jobs load a generated plugin that applies csa's fixed review rules,
then the reviewer chain, and fails closed. On supported Windows, individual
shell commands run in csa's MXC sandbox. OpenCode, its file tools and reviewers
stay on the host with normal configuration, sessions and logins.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import time
from typing import Any

from cli_subagents import mcp_translate, opencode_review
from cli_subagents.agy_review import write_context
from cli_subagents.config import Account, RuntimeConfig, home, jobs_dir, state_dir
from cli_subagents.fsutil import make_private_dir, no_window_kwargs, python_executable, write_text
from cli_subagents.runtimes.base import (
    AUTH, ERROR, PARENT_SERVER, SUCCESS, USAGE_LIMIT, WORKER_TOOLS_NOTE,
    AttemptResult, JobSpec, Launch, clean_environment, parent_server,
)
from cli_subagents.runtimes.reviewers import OPENCODE_REVIEW_BUDGET, build_chain

DEFAULT_MODEL = "opencode/deepseek-v4.1-flash-free"
# Every run starts on this unavailable model. Only csa's plugin switches the
# session to the real model, so a plugin that fails to load stops the job with
# "Model unavailable" before any provider request (OpenCode only logs plugin
# load failures and carries on).
GATE_MODEL = "cli-subagents/gate-not-ready"
# Free Zen models whose terms allow data use or logging (Zen privacy table,
# 2026-10-26). The plugin blocks requests to them unless they are the model the
# user selected. Entries ending in "-" are id prefixes.
RESTRICTED_FREE = ("big-pickle", "mimo-", "flint-", "nemotron-")
# csa efforts use the same words as OpenCode's variant ids (packages/core/src/variant.ts).
VARIANT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# OpenCode substitutes {env:NAME} raw and keeps the result if the JSON still
# parses (packages/core/src/config/variable.ts), so `D:\tools` would turn into a
# tab. A value that always breaks the raw pass forces the JSON-escaped pass.
FORCE_ESCAPE_VARIABLE = "CSA_OPENCODE_FORCE_ESCAPE"
WORKER_AGENT = "csa-worker"
WORKER_NOTE = (
    "[You are running as a delegated subagent for another agent session, which will read your "
    "final message. Work only on this task, stay inside its scope, and end with a short summary "
    "of what you did, the result, and anything left open."
)
GATE_NOTE = (
    " CLI Subagents' review gate approves or denies your tool calls: fixed rules first, then reviewer "
    "models for commands and other actions the rules can't settle. If denied, don't repeat "
    "the action; find another way or say what you couldn't do."
)
SANDBOX_GATE_NOTE = (
    " Your shell commands run in csa's OS sandbox; OpenCode's own file tools use the review gate. "
    "Fixed-rule builds and tests have no internet access (host loopback is allowed); reviewed "
    "commands may use egress. Files outside the workspaces and credential stores are not writable."
)
# The plugin's backstop for one gate check. The whole reviewer chain for one tool
# call (a patch touching several reviewed files makes one chain per file) shares
# OPENCODE_REVIEW_BUDGET (240 s); each reviewer gets an even share of what is left.
REVIEW_HOOK_SECONDS = 300
# v2.1.4 prints "opencode v2.1.4"; without the optional "v", \b would skip to "1.4".
VERSION_RE = re.compile(r"\bv?(\d+)\.\d+(?:\.\d+)?")
LIMIT_RE = re.compile(r"rate.?limit|usage limit|quota|too many requests|\b429\b", re.IGNORECASE)
AUTH_RE = re.compile(r"\b401\b|unauthorized|authentication (?:failed|required)|invalid (?:api )?key|not logged in", re.IGNORECASE)
CAPACITY_RE = re.compile(r"\boverloaded\b|\bat capacity\b|no capacity|high demand|\b503\b.*unavailable", re.IGNORECASE)
MODEL_UNAVAILABLE_RE = re.compile(r"Model unavailable: (\S+)")
VARIANT_UNAVAILABLE_RE = re.compile(r"Variant unavailable for (\S+): (\S+)")


def _inside(path: Path, folder: Path) -> bool:
    return path == folder or folder in path.parents


def project_context(cwd: str) -> tuple[list[str], list[str]]:
    """AGENTS.md files and project skill folders OpenCode would find on its own.

    Turning off project config (OPENCODE_DISABLE_PROJECT_CONFIG) also stops
    OpenCode v2.1.4 from discovering these text-only sources, so csa lists them
    in `instructions` and `skills` instead. Like config/plugin/instruction.ts,
    AGENTS.md is read from the cwd up to the home folder when the cwd is inside
    it, else up to the repository root; like config/discovery.ts, `.claude` and
    `.agents` skill folders come from the cwd and every parent except the
    global ones in the home folder.
    """

    start = Path(cwd).resolve()
    user_home = Path.home().resolve()
    root = next((folder for folder in (start, *start.parents) if (folder / ".git").exists()), start)
    stop = user_home if _inside(start, user_home) else root
    instructions: list[str] = []
    folder = start
    while True:
        candidate = folder / "AGENTS.md"
        if candidate.is_file():
            instructions.append(str(candidate))
        if folder == stop or folder.parent == folder or not _inside(folder.parent, stop):
            break
        folder = folder.parent
    skills = [str(folder / name / "skills") for folder in (start, *start.parents) for name in (".claude", ".agents")
              if folder != user_home and (folder / name / "skills").is_dir()]
    return instructions, skills


def _midnight() -> float:
    now = datetime.now(timezone.utc)
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _numeric(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class OpenCodeParser:
    """The v2 event stream has finished text parts and per-step usage, no final result."""

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.last_text = ""
        self.last_reasoning = ""
        self.init: dict[str, Any] = {}
        self.tool_uses = 0
        self.last_tool: str | None = None
        self.errors: list[dict[str, Any]] = []
        self.cost_usd = 0.0
        self.usage: dict[str, Any] = {}
        self.num_turns = 0
        self._seen: set[tuple[str, str]] = set()
        self._events = 0

    def feed(self, event: dict[str, Any]) -> dict[str, Any] | None:
        kind = str(event.get("type") or "")
        if kind not in ("text", "reasoning", "tool_use", "step_start", "step_finish", "error"):
            return None
        self._events += 1
        if event.get("sessionID"):
            self.session_id = str(event["sessionID"])
        part = event.get("part")
        part = part if isinstance(part, dict) else {}
        # v2 reconciles missed text at EOF; replayed parts must not double-count usage/tools.
        part_id = part.get("id")
        if part_id:
            key = (kind, str(part_id))
            if key in self._seen:
                return None
            self._seen.add(key)
        if kind == "text" and str(part.get("text") or "").strip():
            self.last_text = str(part["text"])
        elif kind == "reasoning":
            self.last_reasoning = str(part.get("text") or "")
        elif kind == "tool_use":
            self.tool_uses += 1
            self.last_tool = str(part.get("tool") or "unknown")
        elif kind == "step_finish":
            self.num_turns += 1
            if _numeric(part.get("cost")):
                self.cost_usd += float(part["cost"])
            tokens = part.get("tokens")
            if isinstance(tokens, dict):
                self._add_usage(self.usage, tokens)
        elif kind == "error":
            error = event.get("error")
            self.errors.append(error if isinstance(error, dict) else {"message": str(error or "unknown error")})
        return None

    def _add_usage(self, target: dict[str, Any], values: dict[str, Any]) -> None:
        for key, value in values.items():
            if _numeric(value):
                target[key] = target.get(key, 0) + value
            elif isinstance(value, dict):
                nested = target.setdefault(key, {})
                if isinstance(nested, dict):
                    self._add_usage(nested, value)

    def progress(self) -> dict[str, Any]:
        result: dict[str, Any] = {"tool_uses": self.tool_uses, "last_tool": self.last_tool, "last_text": self.last_text[-600:]}
        if self.last_reasoning:
            result["last_reasoning"] = self.last_reasoning[-600:]
        if self.errors:
            result["warnings"] = [self._message(error)[:300] for error in self.errors[-5:]]
        return result

    @staticmethod
    def _message(error: dict[str, Any]) -> str:
        data = error.get("data")
        return str(error.get("message") or (data.get("message") if isinstance(data, dict) else "") or error.get("name") or "OpenCode error")

    @staticmethod
    def _no_route(details: str, common: dict[str, Any]) -> AttemptResult | None:
        """OpenCode's provider.no-route errors for csa's explicit model selection, in plain words."""

        variant = VARIANT_UNAVAILABLE_RE.search(details)
        if variant:
            model, name = variant.group(1), variant.group(2).rstrip(".")
            message = (f"OpenCode model {model} has no '{name}' variant, so no model request was made. "
                       "Choose an effort the model offers (OpenCode's catalog lists low, high and max for "
                       f"{DEFAULT_MODEL}) or leave effort out.")
            return AttemptResult(ERROR, message=message, error_text=details[-4000:], **common)
        unavailable = MODEL_UNAVAILABLE_RE.search(details)
        if unavailable and unavailable.group(1).rstrip(".") == GATE_MODEL:
            return AttemptResult(ERROR, message="CLI Subagents' OpenCode plugin did not load or did not finish setup, "
                                 "so the job was stopped before any model request. OpenCode logs the reason as a "
                                 "plugin warning (see `opencode run --help` for --print-logs).",
                                 error_text=details[-4000:], **common)
        if unavailable:
            model = unavailable.group(1).rstrip(".")
            return AttemptResult(ERROR, message=f"OpenCode model {model} is not available here (check `opencode models`). "
                                 "csa does not fall back to another model.", error_text=details[-4000:], **common)
        return None

    def finish(self, exit_code: int | None, stderr_tail: str) -> AttemptResult:
        common = {"session_id": self.session_id, "text": self.last_text, "usage": self.usage or None,
                  "cost_usd": self.cost_usd if self.num_turns else None, "num_turns": self.num_turns or None}
        if exit_code == 0 and not self.errors and self._events:
            return AttemptResult(SUCCESS, **common)
        error = self.errors[-1] if self.errors else {}
        kind = str(error.get("type") or "")
        details = "\n".join([*(self._message(item) for item in self.errors), stderr_tail]).strip()
        # Old error shapes are parsed for a clear failure, even though v1 launches are refused.
        serial = json.dumps(error, ensure_ascii=False)
        message = next((line.strip() for line in details.splitlines() if line.strip()), "")[:400]
        no_route = self._no_route(details, common)
        if no_route is not None:
            return no_route
        if kind == "provider.free-limit" or "FreeUsageLimitError" in serial:
            resets = _midnight()
            when = datetime.fromtimestamp(resets, timezone.utc).strftime("%Y-%m-%d 00:00 UTC")
            return AttemptResult(USAGE_LIMIT, message=f"OpenCode's shared free-model limit was reached; it resets at {when}. "
                                 "Changing accounts does not add free quota; models outside the free tier are not blocked.", resets_at=resets, no_rotation=True, account_cooldown=False,
                                 error_text=details[-4000:], **common)
        if kind == "provider.auth" or AUTH_RE.search(details):
            return AttemptResult(AUTH, message=message or "OpenCode provider authentication failed", error_text=details[-4000:], **common)
        if kind in ("provider.internal", "unknown", "") and CAPACITY_RE.search(details):
            return AttemptResult(ERROR, message=message or "OpenCode model is at capacity", at_capacity=True,
                                 error_text=details[-4000:], **common)
        if kind in ("provider.rate-limit", "provider.quota") or LIMIT_RE.search(details):
            delay = error.get("retryAfterMs")
            reset = time.time() + float(delay) / 1000 if _numeric(delay) and delay > 0 else None
            return AttemptResult(USAGE_LIMIT, message=message or "OpenCode provider usage limit reached", resets_at=reset,
                                 error_text=details[-4000:], **common)
        detail = message or ("no JSON events" if not self._events else "run failed")
        return AttemptResult(ERROR, message=f"opencode exited with status {exit_code}: {detail}", error_text=details[-4000:], **common)


class OpenCodeRuntime:
    name = "opencode"
    # The runner enables the integration; the worker itself remains on the host.
    sandbox_integration = "opencode"

    def __init__(self) -> None:
        self._checked: tuple[tuple[str, ...], str | None] | None = None

    def find_binary(self, config: RuntimeConfig) -> list[str] | None:
        if not getattr(config, "enabled", True):
            return None
        override = os.environ.get("CLI_SUBAGENTS_OPENCODE_COMMAND")
        if override:
            return list(json.loads(override))
        found = (shutil.which(config.binary) or (config.binary if Path(config.binary).is_file() else None)) if config.binary else shutil.which("opencode")
        if not found:
            return None
        path = Path(found)
        if path.suffix.lower() in (".cmd", ".bat", ".ps1"):
            # Bypass cmd/PowerShell shims, including v1 so --version can explain the mismatch.
            # The published v2 package links the native binary itself; source builds use a Node launcher.
            native = path.parent / "node_modules/@opencode/cli/bin/opencode.exe"
            if native.is_file():
                return [str(native)]
            for relative in ("node_modules/@opencode/cli/bin/opencode.cjs", "node_modules/opencode-ai/bin/opencode"):
                launcher = path.parent / relative
                node = path.parent / "node.exe"
                node_path = str(node) if node.is_file() else shutil.which("node")
                if launcher.is_file() and node_path:
                    return [node_path, str(launcher)]
        return [found]

    def account_env(self, config: RuntimeConfig, account: Account) -> dict[str, str]:
        env = clean_environment()
        # This runtime deliberately uses the user's own providers/config and XDG state.
        # Other runtimes scrub these variables; restore them only for OpenCode.
        env.update({key: value for key, value in os.environ.items() if key.upper().startswith("OPENCODE_")})
        return env

    def version(self, config: RuntimeConfig) -> str | None:
        command = self.find_binary(config)
        if not command:
            return None
        try:
            result = subprocess.run([*command, "--version"], stdin=subprocess.DEVNULL, capture_output=True,
                                    text=True, encoding="utf-8", errors="replace", timeout=30,
                                    env=self.account_env(config, config.primary_account()), **no_window_kwargs())
        except (OSError, subprocess.TimeoutExpired):
            return None
        output = result.stdout.strip() or result.stderr.strip()
        return output.splitlines()[0] if result.returncode == 0 and output else None

    def preflight(self, config: RuntimeConfig, account: Account) -> AttemptResult | None:
        command = self.find_binary(config)
        if not command:
            return AttemptResult(ERROR, message="OpenCode is disabled or its binary was not found", before_start=True)
        if self._checked is None or self._checked[0] != tuple(command):
            self._checked = (tuple(command), self.version(config))
        version = self._checked[1]
        match = VERSION_RE.search(version or "")
        if not match or int(match.group(1)) != 2:
            detail = f"found {version}" if version else "could not determine the installed version"
            return AttemptResult(ERROR, message=f"OpenCode v2 (@opencode/cli) is required; {detail}. "
                                 "OpenCode v1 (opencode-ai) is not supported by this adapter.", before_start=True)
        return None

    def auth_status(self, config: RuntimeConfig, account: Account) -> dict[str, Any]:
        blocked = self.preflight(config, account)
        return {"loggedIn": blocked is None, "authMethod": "anonymous_or_configured" if blocked is None else None,
                "detail": blocked.message if blocked else "Free models need no login; configured providers use OpenCode's own credentials."}

    def login_command(self, config: RuntimeConfig, account: Account) -> tuple[list[str], dict[str, str]]:
        command = self.find_binary(config)
        if not command:
            raise RuntimeError("opencode binary not found; install OpenCode v2 (@opencode/cli)")
        return [*command, "auth", "login"], self.account_env(config, account)

    def interactive(self, config: RuntimeConfig, account: Account, setup: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
        command = self.find_binary(config)
        if not command:
            raise RuntimeError("opencode binary not found")
        return command, self.account_env(config, account)

    def prepare(self, config: RuntimeConfig, account: Account, spec: JobSpec) -> dict[str, Any]:
        return {"warnings": [], "conflicts": []}

    def _permissions(self, spec: JobSpec) -> list[dict[str, str]]:
        def rule(action: str, effect: str, resource: str = "*") -> dict[str, str]:
            return {"action": action, "resource": resource, "effect": effect}
        if spec.access == "full":
            return []
        rules = [rule("external_directory", "ask")]
        for folder in (spec.cwd, *spec.add_dirs):
            rules += [rule("external_directory", "allow", Path(folder).resolve().as_posix() + "/*")]
        if spec.access == "read":
            rules += [rule(action, "deny") for action in ("edit", "shell", "subagent")]
        return rules

    def _plugin(self, config: RuntimeConfig, spec: JobSpec, model: str | None, variant: str | None,
                warnings: list[str]) -> Path:
        job = jobs_dir() / spec.job_id
        folder = job / "opencode-plugin"
        make_private_dir(folder)
        context: Path | None = None
        reviewers: list[dict[str, Any]] = []
        if spec.access in ("read", "edit"):
            # The reviewer chain for what the fixed rules can't settle (default: Claude Haiku, then
            # Gemini 3.8 Flash through agy on its own account's bare reviewer folder).
            reviewers = build_chain(config, job, warnings)
            parent = spec.parent or {}
            # The gate and reviewers are on the host, outside every command grant.
            records = job
            command_sandbox = None
            host_paths: list[str] = []
            if spec.sandbox:
                from cli_subagents.sandbox import add_shims, policy
                from cli_subagents.sandbox.launcher import snapshot
                env = add_shims(dict(os.environ), job)
                grants = policy.command_policy([spec.cwd, *spec.add_dirs], env)
                grants.readonly.append(str(job / "sandbox-bin"))
                host_paths = policy._unique([
                    *policy.host_paths((spec.sandbox_backend or {}).get("command") or []),
                    *policy.host_paths(self.find_binary(config) or []),
                    *(path for entry in reviewers for path in policy.host_paths(entry.get("command") or [])),
                ])
                grants = replace(grants, pinned=policy._unique([*grants.pinned, *host_paths]))
                command_sandbox = {"backend": spec.sandbox_backend or {"name": "mxc", "command": []},
                                   "policy": grants.to_dict(), "missing_protected": snapshot(grants),
                                   "command_path": next((value for key, value in env.items() if key.upper() == "PATH"), "")}
            context = write_context(job / "review-context.json", {
                "version": 1, "job": spec.job_id, "access": spec.access, "sandbox": bool(spec.sandbox),
                **({"sandbox_kind": (spec.sandbox_backend or {}).get("name", "mxc"), "sandbox_network": False, "sandbox_mode": "command",
                    "command_sandbox": command_sandbox, "review_network": "egress"} if spec.sandbox else {}),
                "workspaces": [spec.cwd, *spec.add_dirs],
                "read_paths": [*(parent.get("instructions") or []), *(parent.get("skills") or [])],
                # opencode_review refuses edits to csa's own package (with an editable
                # install it is the gate's source) but still lets jobs read it.
                "protected_paths": [str(home()), *host_paths],
                "secret_paths": [
                    str(Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "opencode" / "auth.json"),
                    str(Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "opencode" / "service.json"),
                ],
                "agent_home": "", "user_home": str(Path.home()),
                "trusted_mcp_servers": [PARENT_SERVER], "reviewers": reviewers,
                "review_budget": OPENCODE_REVIEW_BUDGET,
                "log": str(records / "review.jsonl"), "cache": str(records / "review-cache.json"),
            })
        source = opencode_review.plugin_source(python_executable(), Path(opencode_review.__file__).with_name("__main__.py"),
                                               context, steps=spec.max_turns, model=model, variant=variant,
                                               fallback=DEFAULT_MODEL, restricted=RESTRICTED_FREE,
                                               permissions=self._permissions(spec),
                                               # A project or user plugin can't take this id and displace csa's.
                                               plugin_id="cli-subagents.worker." + secrets.token_hex(12),
                                               guarded=spec.access in ("read", "edit"),
                                               review_seconds=REVIEW_HOOK_SECONDS if reviewers else 10,
                                               sandbox=bool(spec.sandbox))
        write_text(folder / "package.json", '{"name":"cli-subagents-worker","type":"module","main":"server.js"}\n')
        write_text(folder / "server.js", source)
        return folder

    def _parent_servers(self, config: RuntimeConfig, spec: JobSpec, warnings: list[str]) -> dict[str, dict[str, Any]]:
        if not config.share_parent_mcp or not spec.parent or spec.parent.get("host") not in ("claude", "codex", "antigravity"):
            return {}
        try:
            from cli_subagents.config import load_config

            host = str(spec.parent["host"])
            primary = load_config().runtime(host).primary
            collected = mcp_translate.collect_native_servers(host, primary, spec.cwd)
            warnings.extend(collected.warnings)
            return collected.servers
        except Exception as exc:
            warnings.append(f"could not read the parent session's MCP servers: {type(exc).__name__}")
            return {}

    def launch(self, config: RuntimeConfig, account: Account, spec: JobSpec, prompt: str, setup: dict[str, Any]) -> Launch:
        blocked = self.preflight(config, account)
        if blocked is not None:
            raise RuntimeError(blocked.message)
        command = self.find_binary(config)
        assert command is not None
        # The explicit, unavailable session model prevents any provider request
        # if the plugin fails to load. Its prompt hook selects the real model.
        argv = [*command, "run", "--format", "json", "--standalone", "--thinking", "--agent", WORKER_AGENT, "-m", GATE_MODEL]
        if not spec.resume_session:
            argv += ["--title", "CLI Subagents worker"]
        if spec.resume_session:
            argv += ["-s", spec.resume_session]
        if spec.access == "full":
            argv.append("--auto")
        warnings: list[str] = []
        model, variant = self.model_and_variant(config, spec)
        env = self.account_env(config, account)
        if spec.sandbox:
            from cli_subagents.sandbox.policy import host_environment
            env = host_environment(env, [spec.cwd, *spec.add_dirs])
        # v2 chooses PWD before process.cwd(); a stale parent-shell PWD would
        # otherwise move the worker away from the scope the gate checks.
        env["PWD"] = str(Path(spec.cwd).resolve())
        guarded = spec.access in ("read", "edit")
        if guarded:
            # Project opencode.json/.opencode (in the cwd and every parent) can add
            # plugins, MCP commands and models that run outside the gate
            # (packages/core/src/config/discovery.ts). The first name wins in
            # packages/cli/src/server-process.ts, so set both.
            env["OPENCODE_CONFIG_PROJECT_DISABLE"] = "1"
            env["OPENCODE_DISABLE_PROJECT_CONFIG"] = "1"
        try:
            generated = json.loads(env.get("OPENCODE_CONFIG_CONTENT") or "{}")
        except ValueError as exc:
            raise RuntimeError("OPENCODE_CONFIG_CONTENT is not valid JSON") from exc
        if not isinstance(generated, dict):
            raise RuntimeError("OPENCODE_CONFIG_CONTENT must be a JSON object")
        generated.update(update="disable", autoupdate=False)
        if guarded:
            # Formatters and language servers run project code (prettier.config.*,
            # eslint configs) after edits, outside the review gate.
            generated.update(formatter=False, lsp=False)
        # User agent-specific rules can override root permissions. A dedicated
        # worker keeps the fallback ask rule last even when such agents exist.
        agents = generated.get("agents")
        agents = dict(agents) if isinstance(agents, dict) else {}
        agents[WORKER_AGENT] = {"mode": "primary", "hidden": True,
                                "permissions": [{"action": "*", "resource": "*", "effect": "ask"}]}
        # Auxiliary title generation can run before primary model resolution.
        agents["title"] = {"disabled": True}
        generated["agents"] = agents
        parent = spec.parent or {}
        instructions = list(generated.get("instructions") or [])
        for value in parent.get("instructions") or []:
            path = Path(value)
            selected = str(path / "**" / "*.md") if path.is_dir() else str(path)
            if selected not in instructions:
                instructions.append(selected)
        skills = [*(generated.get("skills") or []), *(parent.get("skills") or [])]
        if guarded:
            found_instructions, found_skills = project_context(spec.cwd)
            instructions += [item for item in found_instructions if item not in instructions]
            skills += found_skills
        if instructions:
            generated["instructions"] = instructions
        if skills:
            generated["skills"] = list(dict.fromkeys(skills))
        plugin = self._plugin(config, spec, model, variant, warnings)
        generated["plugins"] = [*(generated.get("plugins") or []), str(plugin)]
        # Guarded jobs don't get the parent's other MCP servers: the gate denies their tools.
        # Servers from the user's global OpenCode config still start, and are denied the same way.
        servers = {} if guarded else self._parent_servers(config, spec, warnings)
        servers[PARENT_SERVER] = parent_server(spec.job_id)
        translated = mcp_translate.to_opencode(servers)
        warnings.extend(translated.warnings)
        env.update(translated.env)
        existing_mcp = generated.get("mcp")
        existing_mcp = dict(existing_mcp) if isinstance(existing_mcp, dict) else {}
        native = existing_mcp.get("servers")
        # Inline v1-shaped server dictionaries are accepted by v2 too.
        own = dict(native) if isinstance(native, dict) else existing_mcp
        for name, entry in translated.servers.items():
            if name == PARENT_SERVER or name not in own:
                own[name] = entry
        parent_entry = dict(own[PARENT_SERVER])
        parent_entry["environment"] = {**(parent_entry.get("environment") or {}),
                                       FORCE_ESCAPE_VARIABLE: "{env:" + FORCE_ESCAPE_VARIABLE + "}"}
        own[PARENT_SERVER] = parent_entry
        env[FORCE_ESCAPE_VARIABLE] = '"'
        generated["mcp"] = {"servers": own}
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(generated, ensure_ascii=False)
        env["CLI_SUBAGENTS_DEPTH"] = str(spec.depth)
        env["CLI_SUBAGENTS_JOB_ID"] = spec.job_id
        gate_note = (SANDBOX_GATE_NOTE if spec.sandbox else GATE_NOTE) if spec.access in ("read", "edit") else ""
        note = WORKER_NOTE + WORKER_TOOLS_NOTE + gate_note + "]\n\n"
        # OpenCode keeps its normal state and logins. Only shell invocations are wrapped.
        return Launch(argv=argv, env=env, stdin=note + prompt, session_id=spec.resume_session, warnings=warnings)

    @staticmethod
    def model_and_variant(config: RuntimeConfig, spec: JobSpec) -> tuple[str | None, str | None]:
        """The user's explicit model (job, then csa config) and the variant csa asks for.

        No model means the plugin keeps the user's OpenCode model, else csa's
        default. effort wins over a `#variant` in the model id and applies to
        whichever model is used.
        """

        given = (spec.model or config.default_model or "").strip()
        model: str | None = None
        variant: str | None = None
        if given:
            model, mark, variant = given.partition("#")
            provider, _, name = model.partition("/")
            if not provider or not name or (mark and not variant):
                raise ValueError(f"OpenCode models are written provider/model[#variant], e.g. {DEFAULT_MODEL}; got '{given}'")
            variant = variant or None
        if spec.effort:
            variant = spec.effort.strip()
        if variant is not None and not VARIANT_RE.fullmatch(variant):
            raise ValueError(f"'{variant}' is not a valid OpenCode variant name")
        return model, variant

    def new_parser(self) -> OpenCodeParser:
        return OpenCodeParser()
