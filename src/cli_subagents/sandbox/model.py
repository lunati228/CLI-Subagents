"""Backend-neutral command and filesystem policy contracts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import math
from typing import Any


class SandboxError(Exception):
    """An enabled sandbox could not safely execute a command."""


class PolicyError(SandboxError):
    pass


class Network(str, Enum):
    NONE = "none"
    LOOPBACK = "loopback"
    EGRESS = "egress"
    PROXY = "proxy"  # reserved: backends must reject this until implemented


@dataclass(frozen=True)
class BackendInfo:
    name: str
    command: list[str]
    version: str
    probe: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.name.upper()} {self.version} ({self.probe.get('tier', '?')})"


@dataclass(frozen=True)
class BackendRequest:
    """Executor arguments and serialized policy; the launcher owns its file."""

    argv: list[str]
    policy_text: str


@dataclass(frozen=True)
class Policy:
    workspaces: list[str]
    readonly: list[str] = field(default_factory=list)
    denied: list[str] = field(default_factory=list)
    pinned: list[str] = field(default_factory=list)
    protected_names: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> Policy:
        if not isinstance(data, dict) or set(data) - set(cls.__dataclass_fields__):
            raise PolicyError("unknown filesystem policy fields")
        for key, values in data.items():
            if not isinstance(values, list) or not all(isinstance(v, str) and v and "\0" not in v for v in values):
                raise PolicyError(f"policy {key} must be a list of paths or names")
        if not data.get("workspaces"):
            raise PolicyError("policy has no writable workspace")
        return cls(**data)


@dataclass(frozen=True)
class Command:
    argv: list[str]
    cwd: str
    env: dict[str, str]
    network: Network = Network.LOOPBACK
    timeout: float = 0  # seconds; zero means no integration-specific timeout

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "network": self.network.value}

    @classmethod
    def from_dict(cls, data: Any) -> Command:
        if not isinstance(data, dict) or set(data) - set(cls.__dataclass_fields__):
            raise PolicyError("unknown command fields")
        argv, env, cwd = data.get("argv"), data.get("env"), data.get("cwd")
        if not isinstance(argv, list) or not argv or not all(isinstance(v, str) and "\0" not in v for v in argv):
            raise PolicyError("command argv must be a nonempty list of strings")
        if not isinstance(cwd, str) or not cwd or "\0" in cwd:
            raise PolicyError("command cwd must be a path")
        if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            raise PolicyError("command environment must contain strings")
        timeout = data.get("timeout", 0)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0:
            raise PolicyError("command timeout must be a finite nonnegative number")
        try:
            network = Network(data.get("network", Network.LOOPBACK))
        except ValueError as exc:
            raise PolicyError("unknown network class") from exc
        return cls(argv, cwd, env, network, float(timeout))
