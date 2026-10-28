"""Backend registry. Integrations depend on this interface, never backend schemas."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from cli_subagents.sandbox.model import BackendInfo, BackendRequest, Command, Policy, SandboxError


class Backend(Protocol):
    name: str

    def availability(self) -> tuple[bool, str]: ...

    def ready(self, settings: Any) -> BackendInfo: ...

    def prepare(self, policy: Policy, command: Command, executable: list[str], policy_path: Path) -> BackendRequest: ...


def get_backend(name: str) -> Backend:
    if name == "mxc":
        from .mxc import MxcBackend

        return MxcBackend()
    raise SandboxError(f"unknown sandbox backend: {name}")
