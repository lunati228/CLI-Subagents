"""Authorize each OpenCode shell invocation and issue a one-use launch request."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import secrets
import shlex
import sys
from typing import Any

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from cli_subagents import opencode_review
from cli_subagents.agy_review.context import load_context
from cli_subagents.agy_review.paths import inside_any, network_path
from cli_subagents.fsutil import make_private_dir, python_executable, write_json
from cli_subagents.sandbox import add_shims, policy
from cli_subagents.sandbox.launcher import recover
from cli_subagents.sandbox.model import Command, Network, Policy, PolicyError

from cli_subagents.sandbox.integrations.command import ENV_NAMES, literal, powershell, powershell_command


def prepare(event: dict[str, Any], context_path: Path) -> dict[str, str]:
    context = load_context(context_path)
    settings = context.get("command_sandbox")
    if not isinstance(settings, dict) or context.get("sandbox") is not True:
        raise PolicyError("no command sandbox configured")
    base_policy = Policy.from_dict(settings.get("policy"))
    folder = context_path.parent / "sandbox"
    # A launcher OpenCode killed on a shell timeout never ran its own check.
    recover(folder)
    line, cwd = event.get("command"), event.get("cwd")
    if not isinstance(line, str) or not line.strip() or not isinstance(cwd, str):
        raise PolicyError("shell invocation needs a command and cwd")
    # Resolving \\server\share would connect to that server from the host first.
    if network_path(cwd) and not inside_any(os.path.normpath(cwd), base_policy.workspaces):
        raise PolicyError("shell cwd is a network or device path outside the writable workspaces")
    cwd = policy._real(cwd)
    if not any(policy._inside(cwd, root) for root in base_policy.workspaces):
        raise PolicyError("shell cwd is outside the writable workspaces")
    milliseconds = event.get("timeout", 0)
    if isinstance(milliseconds, bool) or not isinstance(milliseconds, (float, int)) or not math.isfinite(milliseconds) or milliseconds < 0:
        raise PolicyError("invalid shell timeout")
    records: list[dict[str, Any]] = []
    answer = opencode_review.run(json.dumps({"tool": "shell", "input": {"command": line, "cwd": cwd}}).encode(),
                                 context_path, on_record=records.append)
    if answer.get("decision") != "auto_approve" or not records:
        raise PolicyError(answer.get("reason") or "shell command denied")
    network = Network.EGRESS if records[-1].get("source") in ("reviewer", "cache") else Network.LOOPBACK
    raw_env = event.get("env")
    if not isinstance(raw_env, dict):
        raise PolicyError("shell environment must be an object")
    env = {k: v for k, v in raw_env.items() if k.upper() in ENV_NAMES and isinstance(v, str)}
    # Activated workspace tools are safe only inside containment. The host's
    # PATH was stripped before OpenCode started; restore it for this command.
    if isinstance(settings.get("command_path"), str):
        env = {k: v for k, v in env.items() if k.upper() != "PATH"}
        env["PATH"] = settings["command_path"]
    make_private_dir(folder)
    # Redirect tool state to command-scoped container temp; it cannot change
    # the user's settings. MXC supplies its own private TEMP/TMP/LOCALAPPDATA.
    env.update(DOTNET_CLI_TELEMETRY_OPTOUT="1", DOTNET_GENERATE_ASPNET_CERTIFICATE="false",
               DOTNET_CLI_USE_MSBUILD_SERVER="0", MSBUILDDISABLENODEREUSE="1")
    if os.name == "nt":
        env = add_shims(env, context_path.parent)
    command = powershell_command(line, cwd, env, network, milliseconds / 1000)
    if os.name != "nt":
        shell = event.get("shell")
        if not isinstance(shell, str) or not Path(shell).is_absolute():
            raise PolicyError("the shell must be an absolute executable path")
        command = Command([shell, "-c", line], cwd, env, network, milliseconds / 1000)
    request = folder / ("command-" + secrets.token_hex(16) + ".json")
    write_json(request, {"version": 1, "policy": base_policy.to_dict(), "command": command.to_dict(),
                         "backend": settings["backend"]})
    launcher = Path(__file__).resolve().parents[1] / "__main__.py"
    wrapper = (f"& {literal(python_executable())} '-I' {literal(str(launcher))} '--request' {literal(str(request))} "
               "'--parent-pid' $PID; exit $LASTEXITCODE")
    if os.name == "nt":
        return {"shell": powershell(), "command": wrapper}
    wrapper = shlex.join(["exec", python_executable(), "-I", str(launcher), "--request", str(request), "--parent-pid"])
    return {"shell": "/bin/sh", "command": wrapper + " $$"}


def hook_source() -> str:
    """Fail closed even if a host version catches callback exceptions."""

    return '''if (settings.context) {
      const safeShell = SAFE_SHELL;
      await ctx.shell.hook("create.before", async invocation => {
        const original = { ...invocation, env: { ...invocation.env } };
        // Mutate before awaiting anything. An error can never leave the original command runnable.
        invocation.shell = safeShell;
        invocation.command = SAFE_FAILURE;
        try {
        if (!settings.sandbox) {
          const answer = JSON.parse(await check(JSON.stringify({tool: "shell", input: {
            command: original.command, cwd: original.cwd
          }})));
          if (!answer || answer.decision !== "auto_approve") return;
          invocation.shell = original.shell;
          invocation.command = original.command;
          return;
        }
        const answer = JSON.parse(await check(JSON.stringify(original), settings.sandboxScript));
        if (!answer || typeof answer.shell !== "string" || typeof answer.command !== "string") return;
        invocation.shell = answer.shell;
        invocation.command = answer.command;
        } catch (error) {
          // shell.create.before has no failure channel. Keep its trusted failure
          // command rather than rejecting a hook and risking plugin disposal.
        }
      });
    }'''.replace("SAFE_SHELL", json.dumps(powershell() if os.name == "nt" else "/bin/sh")).replace(
        "SAFE_FAILURE", json.dumps("throw 'CLI Subagents sandbox authorization failed closed'" if os.name == "nt"
                                   else "echo 'CLI Subagents sandbox authorization failed closed' >&2; exit 125"))


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(512 * 1024 + 1)
        if not raw or len(raw) > 512 * 1024:
            raise PolicyError("shell input is empty or too large")
        print(json.dumps(prepare(json.loads(raw), Path(sys.argv[1]))))
        return 0
    except Exception as exc:
        # The host discards this helper's stderr. Return a trusted failing shell
        # invocation so the user/model sees the reason (e.g. an overlong command)
        # without ever falling back to the original input. Quote it as data.
        message = "CLI Subagents sandbox authorization failed closed: " + str(exc)[:3000]
        failure = {"shell": powershell(), "command": "throw " + literal(message)} if os.name == "nt" else {
            "shell": "/bin/sh", "command": "printf '%s\\n' " + shlex.quote(message) + " >&2; exit 125"}
        print(json.dumps(failure))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
