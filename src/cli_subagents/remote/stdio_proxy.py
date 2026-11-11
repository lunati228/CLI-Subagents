"""A stdio MCP server on the PC, reachable through the bridge at /mcp/<name>.

The bridge serves Streamable HTTP with JSON responses; this module passes each
JSON-RPC message to one long-lived child process over its stdin and stdout and
returns the child's reply. It works the way mcp-proxy and supergateway expose
a stdio server over HTTP (both MIT; no code taken), cut down to what one
Claude Code client needs:

* the child starts on the first message and stops with the bridge;
* request ids are replaced with the proxy's own, so concurrent calls from
  several HTTP connections can't collide, and restored in the reply;
* requests the child sends to the client (sampling, roots) get "method not
  found", since JSON responses have no channel back; its notifications
  (progress, logs) are dropped.

The remote runtime uses it for csa-parent, so a worker on the server can
message and ask the main session on the PC.
"""

from __future__ import annotations

import itertools
import json
import os
import subprocess
import threading
from typing import Any

from cli_subagents.fsutil import kill_tree, no_window_kwargs

REPLY_TIMEOUT_SECONDS = 3600  # ask_parent waits up to 30 minutes for an answer
MAX_LINE = 16 * 1024 * 1024


def _error(message_id: Any, code: int, text: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": text}}


class StdioProxy:
    def __init__(self, name: str, server: dict[str, Any]) -> None:
        command = server.get("command")
        if not isinstance(command, str) or not command:
            raise ValueError(f"proxied MCP server {name} needs a command")
        args = server.get("args") or []
        env = server.get("env") or {}
        if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
            raise ValueError(f"proxied MCP server {name}: args must be a list of strings")
        if not isinstance(env, dict):
            raise ValueError(f"proxied MCP server {name}: env must be a table")
        self.name = name
        self.argv = [command, *args]
        self.env = {**os.environ, **{str(key): str(value) for key, value in env.items()}}
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._ids = itertools.count(1)
        self._waiting: dict[int, tuple[threading.Event, list[dict[str, Any]]]] = {}
        self._closed = False

    # Child process ---------------------------------------------------------

    def _child(self) -> subprocess.Popen:
        with self._lock:
            if self._closed:
                raise OSError("the proxy is closed")
            if self._proc is None or self._proc.poll() is not None:
                self._proc = subprocess.Popen(self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                              stderr=subprocess.DEVNULL, env=self.env, **no_window_kwargs())
                threading.Thread(target=self._read, args=(self._proc,), name=f"mcp-proxy-{self.name}", daemon=True).start()
            return self._proc

    def _read(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for raw in iter(lambda: proc.stdout.readline(MAX_LINE), b""):
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            if "method" in message:
                if "id" in message:  # a request to the client: nothing can answer it here
                    self._send(proc, _error(message["id"], -32601, "the client can't be reached from this proxy"))
                continue
            waiter = self._waiting.pop(message.get("id"), None) if isinstance(message.get("id"), int) else None
            if waiter is not None:
                waiter[1].append(message)
                waiter[0].set()
        try:
            proc.stdout.close()
        except OSError:
            pass
        # The child ended: every call still waiting gets an error.
        for proxy_id in list(self._waiting):
            waiter = self._waiting.pop(proxy_id, None)
            if waiter is not None:
                waiter[0].set()

    def _send(self, proc: subprocess.Popen, message: dict[str, Any]) -> bool:
        data = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        with self._write_lock:
            try:
                assert proc.stdin is not None
                proc.stdin.write(data)
                proc.stdin.flush()
                return True
            except (OSError, ValueError):
                return False

    # One message -----------------------------------------------------------

    def handle(self, message: Any) -> dict[str, Any] | None:
        """The reply to one JSON-RPC message, or None for a notification or a client's response."""

        if not isinstance(message, dict):
            return _error(None, -32600, "Invalid Request")
        if "method" not in message:
            return None  # the client's response to a request the child made; those are never passed on
        if not isinstance(message["method"], str):
            return _error(message.get("id"), -32600, "Invalid Request")
        try:
            proc = self._child()
        except OSError as exc:
            return _error(message.get("id"), -32603, f"the {self.name} server could not start: {type(exc).__name__}")
        if "id" not in message:
            if message["method"] == "notifications/initialized":
                self._send(proc, message)
            return None  # other notifications (cancelled with the client's ids) are not passed on
        original = message["id"]
        proxy_id = next(self._ids)
        event = threading.Event()
        slot: list[dict[str, Any]] = []
        self._waiting[proxy_id] = (event, slot)
        if not self._send(proc, {**message, "id": proxy_id}):
            self._waiting.pop(proxy_id, None)
            return _error(original, -32603, f"the {self.name} server stopped")
        if not event.wait(REPLY_TIMEOUT_SECONDS) or not slot:
            self._waiting.pop(proxy_id, None)
            return _error(original, -32603, f"no reply from the {self.name} server")
        return {**slot[0], "id": original}

    def close(self) -> None:
        with self._lock:
            self._closed = True
            proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            try:
                if proc.stdin:
                    proc.stdin.close()
                proc.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                kill_tree(proc.pid)
