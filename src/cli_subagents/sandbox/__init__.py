"""Command sandbox discovery and shared tool stand-ins.

Policy/model.py, backends/, launcher.py and integrations/ are separate layers.
OpenCode and its reviewers stay on the host; only shell commands are contained.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from cli_subagents.config import SandboxSettings
from cli_subagents.fsutil import make_private_dir
from cli_subagents.sandbox.model import BackendInfo, PolicyError, SandboxError
from cli_subagents.sandbox.backends import get_backend

def state(settings: SandboxSettings, backend: str = "mxc") -> tuple[str, str]:
    """("on" | "off" | "unavailable", why). "unavailable": turned on explicitly but this system can't run it."""

    if settings.enabled is False:
        return "off", "turned off with [sandbox] enabled = false"
    supported, why = get_backend(backend).availability()
    if supported:
        return "on", why
    if settings.enabled:
        return "unavailable", why
    return "off", f"not available here ({why})"


def applies(runtime: Any, access: str) -> bool:
    return bool(getattr(runtime, "sandbox_integration", None)) and access == "edit"


def for_job(settings: SandboxSettings, runtime: Any, access: str, *, backend: str = "mxc") -> BackendInfo | None:
    """The sandbox for a job, None when it runs without one, SandboxError when an enabled integration cannot run."""

    if not applies(runtime, access):
        return None
    status, why = state(settings, backend)
    if status == "off":
        return None
    if status == "unavailable":
        raise SandboxError(f"the sandbox is turned on ([sandbox] enabled = true) but can't run here: {why}. "
                       f"Remove that line or set it to false to run commands without it")
    return get_backend(backend).ready(settings)


SHIMS = "sandbox-bin"


def add_shims(env: dict[str, str], job_folder: Path) -> dict[str, str]:
    """Put csa's stand-ins for tools that hang inside a process container first on PATH.

    Windows' taskkill.exe can hang in a container; command tools such as Node's
    tree-kill stop child processes through cmd.exe, which finds the
    first `taskkill` on PATH. The stand-in (sandbox/taskkill.py) does the same job
    with TerminateProcess.
    """

    from cli_subagents.fsutil import python_executable, write_text

    folder = job_folder / SHIMS
    make_private_dir(folder)
    script = Path(__file__).resolve().with_name("taskkill.py")
    write_text(folder / "taskkill.cmd", f'@"{python_executable()}" -I "{script}" %*\r\n')
    result = dict(env)
    key = next((name for name in result if name.upper() == "PATH"), "PATH")
    result[key] = str(folder) + (os.pathsep + result[key] if result.get(key) else "")
    return result


def launch_error(stderr: str) -> str | None:
    """MXC's own error message from a worker's stderr (its JSON error envelope), or None."""

    for line in reversed(stderr.splitlines()):
        line = line.strip()
        start = line.find('{"error"')
        if start < 0:
            continue
        try:
            data = json.loads(line[start:])
        except ValueError:
            continue
        error = data.get("error") if isinstance(data, dict) else None
        # MXC's envelope: {"error": {"code": "backend_error", "message": "..."}} (src/tools/wxc/src/main.rs).
        if isinstance(error, dict) and isinstance(error.get("code"), str) and error.get("message"):
            return str(error["message"])[:600]
    return None
