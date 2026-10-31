"""Opt-in Claude main-session hooks. Never turn command containment into approval."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import sys
from typing import Any

from cli_subagents.agy_review.paths import inside_any, network_path
from cli_subagents.config import home, load_config
from cli_subagents.fsutil import make_private_dir, python_executable, read_json, write_json
from cli_subagents.sandbox import SHIMS, add_shims, policy
from cli_subagents.sandbox.backends import get_backend
from cli_subagents.sandbox.integrations.command import ENV_NAMES, literal, powershell_command
from cli_subagents.sandbox.launcher import recover
from cli_subagents.sandbox.model import Network, Policy, PolicyError


def supported_windows() -> bool:
    return os.name == "nt"


def _folder(event: dict[str, Any]) -> Path:
    session = event.get("session_id")
    if not isinstance(session, str) or not session or len(session) > 1024:
        raise PolicyError("missing Claude session identity; restart the session")
    return home() / "claude-sandbox" / hashlib.sha256(session.encode()).hexdigest()


def _deny(reason: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": "csa sandbox: " + reason}}


def handle(event: dict[str, Any], action: str) -> dict[str, Any] | None:
    settings = load_config().sandbox
    config = settings.claude
    # A plugin update installs these hooks everywhere, but never opts anyone in.
    # Worker jobs keep their existing permissions and hooks.
    if config.mode == "off" or os.environ.get("CLI_SUBAGENTS_DEPTH", "0") != "0":
        return None
    if action == "claude-sandbox" and event.get("tool_name") not in ("Bash", "PowerShell"):
        return None
    if action == "claude-sandbox" and event.get("tool_name") == "Bash":
        return _deny("Bash is disabled in block-bash mode; use the PowerShell tool") if config.mode == "block-bash" else None
    if settings.enabled is False:
        raise PolicyError("Claude sandbox mode is enabled but [sandbox] enabled = false")
    if not supported_windows():
        raise PolicyError("the Claude command sandbox requires supported Windows and MXC")
    folder = _folder(event)
    state_path = folder / "session.json"
    if action == "claude-sandbox-start":
        # Never widen a resumed session's grants from its current shell cwd.
        state = read_json(state_path, None)
        if state is None:
            if state_path.exists():
                raise PolicyError("the session baseline is invalid; start a new Claude session")
            cwd = event.get("cwd")
            if not isinstance(cwd, str) or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
                raise PolicyError("SessionStart needs an existing absolute project cwd")
            roots = policy._unique([policy.check_workspace(cwd),
                                    *(policy.check_workspace(str(p)) for p in config.additional_dirs)])
            if not all(Path(root).is_dir() for root in roots):
                raise PolicyError("all writable roots must be existing directories")
            filesystem = policy.command_policy(roots, dict(os.environ))
            backend = get_backend("mxc").ready(settings)
            make_private_dir(folder)
            write_json(state_path, {"policy": filesystem.to_dict(),
                                    "backend": {"name": backend.name, "command": backend.command}})
        else:
            if not isinstance(state, dict):
                raise PolicyError("the session baseline is invalid; start a new Claude session")
            Policy.from_dict(state.get("policy"))
            # Claude, its file tools and the user change the project between commands
            # (.claude/settings.local.json, git init), so only a killed launcher's
            # own record says what a command may have planted.
            recover(folder)
        return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext":
                f"csa command sandbox ({config.mode}): PowerShell uses MXC with loopback-only networking. "
                "Writable roots are the initial project cwd and [sandbox.claude] additional_dirs. "
                "Use a new session after changing those roots. " +
                ("Bash is blocked." if config.mode == "block-bash" else "Bash commands run outside this sandbox.")}}
    state = read_json(state_path, None)
    if not isinstance(state, dict):
        raise PolicyError("no SessionStart baseline; restart Claude after enabling its sandbox")
    filesystem = Policy.from_dict(state.get("policy"))
    if action == "claude-sandbox-end":
        # The session state is retained for --resume; a record that can't be
        # settled now stays for the resumed session's next check.
        try:
            recover(folder)
        finally:
            for pattern in ("command-*.json*", "policy-*.json"):
                for request in folder.glob(pattern):
                    request.unlink(missing_ok=True)
        return None
    recover(folder)
    tool = event.get("tool_input")
    if not isinstance(tool, dict) or not isinstance(tool.get("command"), str) or not tool["command"].strip():
        raise PolicyError("missing PowerShell command")
    cwd = tool.get("cwd", event.get("cwd"))
    # Checking \\server\share would connect to that server from the host first.
    if isinstance(cwd, str) and network_path(cwd) and not inside_any(os.path.normpath(cwd), filesystem.workspaces):
        raise PolicyError("command cwd is a network or device path outside the session's writable roots")
    if not isinstance(cwd, str) or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
        raise PolicyError("invalid command cwd")
    cwd = policy._real(cwd)
    if not any(policy._inside(cwd, root) for root in filesystem.workspaces):
        raise PolicyError("command cwd is outside the session's writable roots")
    milliseconds = tool.get("timeout", config.timeout_seconds * 1000)
    if (isinstance(milliseconds, bool) or not isinstance(milliseconds, (int, float))
            or not math.isfinite(milliseconds) or milliseconds <= 0):
        raise PolicyError("invalid command timeout")
    timeout = min(milliseconds / 1000, config.timeout_seconds)
    env = {k: v for k, v in os.environ.items() if k.upper() in ENV_NAMES}
    env.update(DOTNET_CLI_TELEMETRY_OPTOUT="1", DOTNET_GENERATE_ASPNET_CERTIFICATE="false",
               DOTNET_CLI_USE_MSBUILD_SERVER="0", MSBUILDDISABLENODEREUSE="1")
    env = add_shims(env, folder)
    filesystem = policy.command_policy(filesystem.workspaces, env)
    # A narrow read grant exposes only the tool stand-in, not session state.
    filesystem.readonly.append(str(folder / SHIMS))
    command = powershell_command(tool["command"], cwd, env, Network.LOOPBACK, timeout)
    request = folder / ("command-" + secrets.token_hex(16) + ".json")
    write_json(request, {"version": 1, "policy": filesystem.to_dict(), "command": command.to_dict(),
                         "backend": state["backend"]})
    launcher = Path(__file__).resolve().parents[1] / "__main__.py"
    wrapper = (f"& {literal(python_executable())} '-I' {literal(str(launcher))} '--request' {literal(str(request))} "
               "'--parent-pid' $PID; exit $LASTEXITCODE")
    # Omit permissionDecision: Claude still checks the rewritten invocation.
    updated = {**tool, "command": wrapper, "timeout": int(timeout * 1000)}
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": updated}}


def main(action: str) -> int:
    try:
        raw = sys.stdin.read(512 * 1024 + 1)
        if len(raw) > 512 * 1024:
            raise PolicyError("hook input too large")
        event = json.loads(raw)
        if not isinstance(event, dict):
            raise PolicyError("expected a hook object")
        output = handle(event, action)
        if output:
            print(json.dumps(output))
        return 0
    except Exception as exc:
        if action == "claude-sandbox":
            print(json.dumps(_deny(str(exc)[:2000])))
            return 0
        print("csa sandbox failed closed: " + str(exc)[:2000], file=sys.stderr)
        return 2
