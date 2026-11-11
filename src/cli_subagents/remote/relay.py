"""One remote attempt, run on the PC as the worker process csa's runner starts.

    python -I relay.py <job file>      (the prompt comes on stdin)

It serves the project through the PC bridge, opens the ssh session with a
reverse forward to it, sends the job, prints the server's Claude Code
stream-json events on stdout for the runner and sends the main session's
follow-ups into the running conversation. Diagnostics go to stderr.

See docs/remote.md and runtimes/remote.py.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
from typing import IO, Any

if not __package__:  # started by path with -I: put the installed package first
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cli_subagents import messages  # noqa: E402
from cli_subagents.config import jobs_dir  # noqa: E402
from cli_subagents.fsutil import no_window_kwargs, read_json  # noqa: E402
from cli_subagents.remote import protocol  # noqa: E402

RELAY_EVENT = "csa.remote"
ALLOCATED = re.compile(r"Allocated port (\d+) for remote forward")
FORWARD_WAIT_SECONDS = 30
POLL_SECONDS = 1.0
STDERR_TAIL = 20
TOOL_INPUT_CAP = 600
TOOL_RESULT_CAP = 1500
# Permission modes on the server. Built-in file and shell tools are off there either
# way; the bridge decides what the pc_* tools may do.
PERMISSION_MODES = {"read": "default", "edit": "acceptEdits", "full": "acceptEdits"}


class Output:
    """Thread-safe JSON lines on stdout: what the runner parses and records."""

    def __init__(self, stream: IO[bytes]) -> None:
        self.stream = stream
        self.lock = threading.Lock()

    def event(self, event: dict[str, Any]) -> None:
        line = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
        with self.lock:
            try:
                self.stream.write(line)
                self.stream.flush()
            except (OSError, ValueError):
                pass

    def note(self, what: str, **fields: Any) -> None:
        self.event({"type": RELAY_EVENT, "event": what, **fields})


class Ssh:
    """The ssh session to the server's forced command, and what it said on stderr."""

    def __init__(self, job: dict[str, Any], bridge_port: int | None) -> None:
        argv = [*job["ssh"], "-T", "-o", "ExitOnForwardFailure=yes"]
        if bridge_port:
            # Port 0: the server picks a free port on its loopback and ssh reports it on stderr.
            argv += ["-R", f"127.0.0.1:0:127.0.0.1:{bridge_port}"]
        argv += [job["server"], "job"]
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     **no_window_kwargs())
        self.tail: list[str] = []
        self._lines: queue.Queue[str | None] = queue.Queue()
        self.write_lock = threading.Lock()
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self) -> None:
        assert self.proc.stderr is not None
        for raw in iter(self.proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                self.tail = (self.tail + [line])[-STDERR_TAIL:]
                print(f"ssh: {line}", file=sys.stderr, flush=True)
            self._lines.put(line)
        self._lines.put(None)

    def forwarded_port(self) -> int:
        deadline = time.monotonic() + FORWARD_WAIT_SECONDS
        while True:
            try:
                line = self._lines.get(timeout=max(0.1, deadline - time.monotonic()))
            except queue.Empty:
                raise RuntimeError("ssh did not report the forwarded port; does the server key allow port forwarding?") from None
            if line is None:
                raise RuntimeError("ssh ended before the job started" + (f": {self.tail[-1]}" if self.tail else ""))
            match = ALLOCATED.search(line)
            if match:
                return int(match.group(1))

    def send(self, message: dict[str, Any]) -> bool:
        assert self.proc.stdin is not None
        with self.write_lock:
            try:
                self.proc.stdin.write(protocol.encode(message))
                self.proc.stdin.flush()
                return True
            except (OSError, ValueError):
                return False

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()


# Transcript for a resume whose session the server no longer has -------------


def _cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f" ... ({len(text) - limit} more characters)"


def transcript_markdown(job_ids: list[str]) -> str:
    """The conversation so far, from the events csa recorded for these jobs."""

    parts: list[str] = []
    for job_id in job_ids:
        path = jobs_dir() / job_id / "events.jsonl"
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            content = (event.get("message") or {}).get("content") if isinstance(event.get("message"), dict) else None
            if kind == RELAY_EVENT and event.get("event") in ("prompt", "delivered") and event.get("text"):
                parts.append(f"## User\n\n{event['text']}\n")
            elif kind == "assistant" and isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text" and block.get("text"):
                        parts.append(f"## Claude\n\n{block['text']}\n")
                    elif block.get("type") == "tool_use":
                        arguments = _cap(json.dumps(block.get("input"), ensure_ascii=False), TOOL_INPUT_CAP)
                        parts.append(f"Tool `{block.get('name')}`: `{arguments}`\n")
            elif kind == "user" and isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        value = block.get("content")
                        if isinstance(value, list):
                            value = "\n".join(str(item.get("text", "")) for item in value if isinstance(item, dict))
                        parts.append(f"Result:\n\n```\n{_cap(str(value or ''), TOOL_RESULT_CAP)}\n```\n")
    return "\n".join(parts)


def history_for_server(job_ids: list[str]) -> str | None:
    text = transcript_markdown(job_ids)
    if not text:
        return None
    data = text.encode("utf-8")
    room = protocol.MAX_HISTORY - 200
    if len(data) <= room:
        return text
    return "(earlier part of the conversation left out)\n\n" + data[-room:].decode("utf-8", "ignore")


# The attempt ---------------------------------------------------------------


class Forwarder:
    """Sends the main session's follow-ups into the running conversation.

    A follow-up counts as read only when the server says it went in; one that
    arrives after the conversation ended stays unread, and the runner's
    follow-up turn delivers it.
    """

    def __init__(self, job_id: str, ssh: Ssh, out: Output) -> None:
        self.job_id = job_id
        self.ssh = ssh
        self.out = out
        self.sent: dict[str, str] = {}
        self.stop = threading.Event()

    def run(self) -> None:
        while not self.stop.wait(POLL_SECONDS):
            try:
                fresh = [item for item in messages.unread_follow_ups(self.job_id) if item.get("id") not in self.sent]
            except (OSError, ValueError):
                continue
            for item in fresh:
                text = str(item.get("text") or "")
                self.sent[str(item["id"])] = text
                body = "Follow-up from the main session:\n\n" + text
                if not self.ssh.send({"type": "message", "id": str(item["id"]), "text": body}):
                    return

    def status(self, message: dict[str, Any]) -> None:
        item_id = str(message.get("id") or "")
        if item_id not in self.sent:
            return
        if message.get("delivered"):
            messages.mark_read(self.job_id, {item_id})
            self.out.note("delivered", id=item_id, text=self.sent[item_id])
        else:
            self.out.note("not_delivered", id=item_id)


def run(job: dict[str, Any], prompt: str, out: Output) -> int:
    from cli_subagents.remote.bridge import Bridge

    job_id = job["csa_job_id"]
    folder = jobs_dir() / job_id / "remote"
    audit_path = folder / "pc-tools.jsonl"

    def audit(entry: dict[str, Any]) -> None:
        with open(audit_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"time": time.time(), **entry}, ensure_ascii=False) + "\n")

    token = secrets.token_urlsafe(32)
    bridge = Bridge(job["cwd"], token, read_only=job["access"] == "read", audit=audit,
                    proxies={"csa-parent": job["parent"]})
    port = bridge.start()
    ssh: Ssh | None = None
    forwarder: Forwarder | None = None
    try:
        ssh = Ssh(job, port)
        remote_port = ssh.forwarded_port()
        bundle = job.get("bundle")
        resume = None
        if job.get("resume"):
            resume = {**job["resume"], "history_md": history_for_server(job.get("history_jobs") or [])}
        out.note("prompt", text=prompt)
        sent = ssh.send({
            "type": "job",
            "version": protocol.VERSION,
            "job_id": job["wire_job_id"],
            "account": job["account"],
            "prompt": prompt,
            "model": job.get("model"),
            "effort": job.get("effort"),
            "max_turns": job.get("max_turns"),
            "permission_mode": PERMISSION_MODES.get(job["access"], "acceptEdits"),
            "workspace": {"mode": "pc", "label": Path(job["cwd"]).name or "project", "bridge_port": remote_port,
                          "bridge_token": token, "proxies": ["csa-parent"]},
            "bundle": {"sha256": bundle["sha256"], "size": bundle["size"]} if bundle else None,
            "resume": resume,
        })
        if not sent:
            raise RuntimeError("the ssh session closed before the job was sent")
        assert ssh.proc.stdout is not None
        try:
            for message in protocol.read_messages(ssh.proc.stdout):
                kind = message.get("type")
                if kind == "need_bundle" and bundle and message.get("sha256") == bundle["sha256"]:
                    data = Path(bundle["path"]).read_bytes()
                    ssh.send({"type": "bundle", "sha256": bundle["sha256"], "data": base64.b64encode(data).decode("ascii")})
                elif kind == "accepted":
                    out.note("accepted", account=message.get("account"), session_id=message.get("session_id"),
                             resumed=bool(message.get("resumed")))
                    if forwarder is None:
                        forwarder = Forwarder(job_id, ssh, out)
                        threading.Thread(target=forwarder.run, daemon=True).start()
                elif kind == "event" and isinstance(message.get("event"), dict):
                    out.event(message["event"])
                elif kind == "message_status" and forwarder is not None:
                    forwarder.status(message)
                elif kind == "warning":
                    text = str(message.get("text") or "")
                    print(f"server: {text}", file=sys.stderr, flush=True)
                    out.note("warning", text=text)
                elif kind == "done":
                    return 1 if message.get("is_error") else 0
                elif kind == "error":
                    out.note("error", code=message.get("code"), message=message.get("message"))
                    print(f"server refused the job ({message.get('code')}): {message.get('message')}", file=sys.stderr, flush=True)
                    return 1
        except protocol.ProtocolError as exc:
            out.note("error", code="transport", message=f"unreadable reply from the server: {exc}")
            return 1
        detail = ssh.tail[-1] if ssh.tail else "no reply"
        out.note("error", code="transport", message=f"the ssh session ended before the job finished ({detail})")
        return 1
    except (OSError, RuntimeError) as exc:
        out.note("error", code="transport", message=str(exc))
        print(f"relay: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        if forwarder is not None:
            forwarder.stop.set()
        if ssh is not None:
            ssh.close()
        bridge.stop()
        if bundle := job.get("bundle"):
            try:
                os.remove(bundle["path"])
            except OSError:
                pass


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: relay.py <job file>", file=sys.stderr)
        return 2
    job = read_json(Path(argv[0]))
    if not isinstance(job, dict):
        print("relay: the job file is missing or unreadable", file=sys.stderr)
        return 2
    prompt = sys.stdin.buffer.read().decode("utf-8", "replace")
    return run(job, prompt, Output(sys.stdout.buffer))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
