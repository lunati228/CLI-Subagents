"""Runtime adapters. Each adapter launches one vendor CLI headless."""

from __future__ import annotations

from cli_subagents.runtimes.base import Runtime

# Runtimes with a working adapter. Others are accepted in the config but not yet runnable.
AVAILABLE = ("claude", "codex", "antigravity", "opencode", "remote")


def offered_runtimes() -> tuple[str, ...]:
    """Optional workers appear once they can run: OpenCode when installed, remote when a server is set."""
    from cli_subagents.config import load_config

    config = load_config()

    def offered(name: str) -> bool:
        if name == "opencode":
            return bool(get_runtime(name).find_binary(config.runtime(name)))
        if name == "remote":
            remote = config.runtime(name)
            return bool(remote.server and remote.accounts)
        return True

    return tuple(name for name in AVAILABLE if offered(name))


def get_runtime(name: str) -> Runtime:
    if name not in AVAILABLE:
        raise ValueError(f"runtime '{name}' is not available yet. Available: {', '.join(AVAILABLE)}")
    if name == "claude":
        from cli_subagents.runtimes.claude import ClaudeRuntime

        return ClaudeRuntime()
    if name == "codex":
        from cli_subagents.runtimes.codex import CodexRuntime

        return CodexRuntime()
    if name == "antigravity":
        from cli_subagents.runtimes.antigravity import AntigravityRuntime

        return AntigravityRuntime()
    if name == "opencode":
        from cli_subagents.runtimes.opencode import OpenCodeRuntime

        return OpenCodeRuntime()
    if name == "remote":
        from cli_subagents.runtimes.remote import RemoteRuntime

        return RemoteRuntime()
    raise ValueError(f"unknown runtime '{name}'")
