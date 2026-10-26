"""Minimal MCP server over stdio (JSON-RPC 2.0, one message per line).

Implements the parts of the protocol a tool server needs: initialize, ping,
tools/list and tools/call. No third-party dependencies.
"""

from __future__ import annotations

import json
import sys
import threading
import traceback
from typing import Any, Callable

from cli_subagents import __version__, tools

SUPPORTED_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
INSTRUCTIONS = (
    "Start subagents in Claude Code, Codex, Antigravity or installed OpenCode. Each job runs in the background on one "
    "of your own accounts, with the same MCP servers, plugins, skills and instructions as your main "
    "setup. In Claude Code, start them with the Agent tool and the cli-subagents-* agent types "
    "instead of these tools, so the user sees them; the cli-subagents skill explains how. Elsewhere, "
    "start jobs with subagent_start, then call subagent_wait: it returns when a job finishes "
    "or a subagent sends a message or question. Answer or send follow-ups with subagent_message. "
    "subagent_accounts shows which accounts are ready."
)


WORKER_INSTRUCTIONS = (
    "Tools to talk to the main session that started you: send progress notes, ask a blocking question "
    "when you can't decide something yourself, and read follow-up instructions it sent."
)


class Server:
    def __init__(self, write: Callable[[dict[str, Any]], None], worker_job: str | None = None) -> None:
        self._write = write
        self._worker_job = worker_job
        self._client: str | None = None

    def handle(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        request_id = message.get("id")
        if method is None:
            return  # a response to something we never send
        if request_id is None:
            return  # notification (initialized, cancelled, ...)
        try:
            params = message.get("params") or {}
            if not isinstance(params, dict):
                raise _RpcError(-32602, "params must be an object")
            result = self._dispatch(method, params)
            self._write({"jsonrpc": "2.0", "id": request_id, "result": result})
        except _RpcError as exc:
            self._write({"jsonrpc": "2.0", "id": request_id, "error": {"code": exc.code, "message": str(exc)}})
        except Exception as exc:  # pragma: no cover - defensive
            traceback.print_exc(file=sys.stderr)
            self._write({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"}})

    def _dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "initialize":
            client = params.get("clientInfo")
            self._client = str(client.get("name")) if isinstance(client, dict) and client.get("name") else None
            requested = params.get("protocolVersion")
            version = requested if requested in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[-1]
            return {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "cli-subagents", "version": __version__},
                "instructions": WORKER_INSTRUCTIONS if self._worker_job else INSTRUCTIONS,
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": tools.definitions(self._worker_job)}
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                raise _RpcError(-32602, "arguments must be an object")
            token = (params.get("_meta") or {}).get("progressToken") if isinstance(params.get("_meta"), dict) else None
            progress = None
            if token is not None:
                def progress(count: int, message: str) -> None:
                    self._write({
                        "jsonrpc": "2.0",
                        "method": "notifications/progress",
                        "params": {"progressToken": token, "progress": count, "message": message},
                    })
            try:
                value = tools.call(str(name), arguments, progress=progress, worker_job=self._worker_job, client=self._client)
                is_error = False
            except tools.ToolError as exc:
                value = {"error": str(exc)}
                is_error = True
            return {
                "content": [{"type": "text", "text": json.dumps(value, indent=2, ensure_ascii=False, default=str)}],
                "isError": is_error,
            }
        raise _RpcError(-32601, f"method not found: {method}")


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def serve(stdin=None, stdout=None, worker_job: str | None = None) -> None:
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    lock = threading.Lock()

    def write(message: dict[str, Any]) -> None:
        data = (json.dumps(message, ensure_ascii=False, default=str) + "\n").encode("utf-8")
        with lock:
            stdout.write(data)
            stdout.flush()

    server = Server(write, worker_job)
    threads: list[threading.Thread] = []
    for raw in stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            write({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        batch = message if isinstance(message, list) else [message]
        for item in batch:
            if isinstance(item, dict):
                thread = threading.Thread(target=server.handle, args=(item,), daemon=True)
                thread.start()
                threads.append(thread)
        threads = [thread for thread in threads if thread.is_alive()]
    for thread in threads:
        thread.join(timeout=5)


def main(worker_job: str | None = None) -> int:
    if worker_job is None:
        tools.startup()
    serve(worker_job=worker_job)
    return 0
