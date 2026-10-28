"""Microsoft MXC's Windows policy adapter (stable contract 1.0.0)."""

from __future__ import annotations

import subprocess
import json
from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Any, Iterable

from cli_subagents.sandbox.model import BackendInfo, BackendRequest, Command, Network, Policy, PolicyError
from cli_subagents.sandbox import mxc
from cli_subagents.sandbox.mxc import POLICY_VERSION
from cli_subagents.sandbox.policy import (
    _unique, _real, _inside, _is_root, check_workspace, protections,
    HIDDEN_HOME_DIRS, resolve_program,
)

DROP_ENV = ("TMPDIR",)

@dataclass
class Grants:
    """MXC adapter grant aggregation, with writable roots limited to workspaces."""

    workspaces: list[str] = field(default_factory=list)  # the job's folders: read-write with protections
    readonly: list[str] = field(default_factory=list)  # dropped where a writable grant already covers them
    pinned: list[str] = field(default_factory=list)  # read-only even inside a workspace (csa's own code)
    denied: list[str] = field(default_factory=list)


def drive_roots(paths: Iterable[str]) -> list[str]:
    return _unique(str(Path(Path(path).anchor)) for path in paths if Path(path).anchor)


def environment(env: dict[str, str]) -> list[str]:
    """`KEY=VALUE` entries for the container; caller entries win over MXC's default block."""

    entries = []
    for key, value in env.items():
        if not key or key.startswith("=") or "=" in key or "\0" in key or "\0" in str(value):
            continue
        if key.upper() in DROP_ENV:
            continue
        entries.append(f"{key}={value}")
    return entries


def redacted(data: dict[str, Any]) -> dict[str, Any]:
    """The request without environment values, for the job folder."""

    copy = json.loads(json.dumps(data))
    process = copy.get("process") or {}
    process["env"] = sorted(entry.split("=", 1)[0] for entry in process.get("env") or [])
    return copy


def request(argv: list[str], cwd: str, env: dict[str, str], grants: Grants) -> dict[str, Any]:
    """The MXC request (schema 1.0.0) for one command process."""

    workspaces = [check_workspace(path) for path in grants.workspaces]
    readwrite = _unique(workspaces)
    readonly: list[str] = []
    denied: list[str] = []
    for path in _unique(_real(path) for path in grants.readonly):
        # Already readable and writable through a writable grant: a read-only
        # grant here would only take write access away (an activated venv on PATH).
        if any(_inside(path, folder) for folder in readwrite):
            continue
        if _is_root(path):
            raise PolicyError(f"the sandbox never grants a drive root ({path}); it would expose the whole drive")
        readonly.append(path)
    for workspace in workspaces:
        protected, missing = protections(workspace)
        readonly += protected
        denied += missing
    for path in grants.pinned:
        real = _real(path)
        if _is_root(real):
            raise PolicyError(f"the sandbox never grants a drive root ({real}); it would expose the whole drive")
        readonly.append(real)
    granted = [*readwrite, *readonly]
    user = Path.home()
    for name in HIDDEN_HOME_DIRS:
        hidden = _real(user / name)
        if not any(_inside(path, hidden) or _inside(hidden, path) for path in granted):
            denied.append(hidden)
    denied += [_real(path) for path in grants.denied]
    data: dict[str, Any] = {
        "version": POLICY_VERSION,
        "containment": "processcontainer",
        "process": {
            "commandLine": subprocess.list2cmdline(resolve_program(argv, env)),
            "cwd": _real(cwd),
            "env": environment(env),
            "inheritDefaultEnv": True,
            "timeout": 0,  # csa enforces the job's own timeout
        },
        "filesystem": {
            "readwritePaths": readwrite,
            "readonlyPaths": _unique(readonly),
            "deniedPaths": _unique(denied),
        },
        "fallback": {"allowDaclMutation": False},
        "network": {
            "egress": {"default": "allow"},
            "ingress": {"default": "deny", "hostLoopback": "deny"},
        },
        # Win32k stays on: git, .NET and other console tools fail to start without it.
        "ui": {"disable": False, "clipboard": "none", "injection": False},
        "processContainer": {"filesystem": {"enumeratePaths": drive_roots(granted)}},
    }
    return data


class MxcBackend:
    name = "mxc"

    def availability(self) -> tuple[bool, str]:
        return mxc.support()

    def ready(self, settings: Any) -> BackendInfo:
        hint = "or set [sandbox] enabled = false in ~/.cli-subagents/config.toml to run commands without it"
        command = mxc.launcher(settings.path)
        if not os.environ.get(mxc.COMMAND_ENV) and not Path(command[0]).is_file():
            raise mxc.MxcError(f"the sandbox is on but MXC {mxc.VERSION} isn't installed ({command[0]}). "
                               f"Run `csa setup` to install it, {hint}")
        try:
            probe = mxc.probe(command)
        except mxc.MxcError as exc:
            raise mxc.MxcError(f"{exc}. Run `csa doctor` for details, {hint}") from exc
        problems = mxc.probe_problems(probe, loopback=True)
        if problems:
            raise mxc.MxcError("MXC can't enforce the command policy: " + "; ".join(problems) + f"; {hint}")
        return BackendInfo(self.name, command, mxc.VERSION, probe)

    def request(self, policy: Policy, command: Command) -> dict[str, Any]:
        if command.network == Network.PROXY:
            raise PolicyError("MXC proxy policy is not implemented")
        if not any(_inside(_real(command.cwd), _real(root)) for root in policy.workspaces):
            raise PolicyError("command cwd is outside the writable workspaces")
        env = dict(command.env)
        # Required by PSEC when no default user environment is inherited. MXC
        # derives its disposable package/temp tree from the real LOCALAPPDATA.
        env["LOCALAPPDATA"] = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        data = request(command.argv, command.cwd, env, Grants(
            workspaces=policy.workspaces, readonly=policy.readonly, pinned=policy.pinned, denied=policy.denied))
        for workspace in policy.workspaces:
            for name in policy.protected_names:
                if Path(name).name != name or name in (".", ".."):
                    raise PolicyError("protected names must be single path components")
                target = str(Path(workspace) / name)
                data["filesystem"]["readonlyPaths" if Path(target).exists() else "deniedPaths"].append(target)
        data["network"] = {
            "egress": {"default": "allow" if command.network == Network.EGRESS else "deny"},
            "ingress": {"default": "deny", "hostLoopback": "deny" if command.network == Network.NONE else "allow"},
        }
        data["process"]["inheritDefaultEnv"] = False
        # PSEC cannot deny a path whose parent is missing (HRESULT_PATH_NOT_FOUND).
        # Deny the first missing component, which also prevents creating the secret path.
        denied = []
        for value in data["filesystem"]["deniedPaths"]:
            path = Path(value)
            while not path.parent.exists() and path.parent != path:
                path = path.parent
            denied.append(str(path))
        data["filesystem"]["deniedPaths"] = _unique(denied)
        return data

    def prepare(self, policy: Policy, command: Command, executable: list[str], policy_path: Path) -> BackendRequest:
        return BackendRequest([*executable, str(policy_path)], json.dumps(self.request(policy, command)))
