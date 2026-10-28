"""Shared argv launcher. It has no dependency on an agent or review gate."""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import time
from typing import Any

from cli_subagents.fsutil import make_private_dir, process_start_time, same_process, worker_process, write_text
from cli_subagents.sandbox.backends import get_backend
from cli_subagents.sandbox.model import Command, Policy, SandboxError
from cli_subagents.sandbox.policy import PROTECTED_NAMES, _inside, _key, _real, _unique, host_paths


def _entry_exists(path: Path) -> bool:
    # Unlike exists(), lstat counts dangling links and reports access errors.
    try:
        path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return False
    return True


def _missing_protected_paths(policy: Policy) -> list[tuple[Path, Path]]:
    """Snapshot absent protected entries, without walking the writable trees."""

    names = _unique([*PROTECTED_NAMES, *policy.protected_names])
    if any(Path(name).name != name or name in (".", "..") or any(c in name for c in "\\/:\0") for name in names):
        raise SandboxError("protected names must be single path components")
    keys = {_key(name) for name in names}
    candidates: dict[str, tuple[Path, Path]] = {}
    for root in (Path(value) for value in _unique(_real(p) for p in policy.workspaces)):
        for name in names:
            path = root / name
            candidates.setdefault(_key(str(path)), (path, root))
        # Explicit policy paths can protect a nested name, or a file inside it.
        # Keep the lexical name: resolving a new link would move its target.
        for value in [*policy.readonly, *policy.denied, *policy.pinned]:
            path = Path(os.path.abspath(os.path.expanduser(value)))
            try:
                relative = path.relative_to(root)
            except ValueError:
                continue
            current = root
            for part in relative.parts:
                current /= part
                if _key(part) in keys:
                    candidates.setdefault(_key(str(current)), (current, root))
    missing = []
    for path, root in candidates.values():
        try:
            if not _entry_exists(path):
                missing.append((path, root))
        except OSError as exc:
            raise SandboxError(f"cannot snapshot protected path {path}: {exc}") from exc
    return missing


def _quarantine_new_paths(missing: list[tuple[Path, Path]]) -> None:
    """Move newly planted entries aside after all contained processes stop."""

    findings = []
    for path, root in missing:
        try:
            if not _entry_exists(path):
                continue
            # A command may have replaced a nested ancestor with a junction.
            # Move the entry itself, including a link, but never follow a parent
            # outside the original writable root to move someone else's data.
            if not _inside(str(path.parent.resolve()), str(root)):
                raise OSError("the parent now resolves outside the writable root")
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            saved = Path(tempfile.mkdtemp(prefix=f"{path.name}.csa-quarantine-{stamp}-", dir=path.parent))
            # Reserving a fresh directory avoids overwriting an earlier item,
            # even on platforms where rename replaces an existing destination.
            destination = saved / "item"
            path.rename(destination)
            findings.append(f"moved {path} to quarantine {destination}")
        except (OSError, RuntimeError) as exc:
            findings.append(f"could not quarantine {path}: {exc}; move it aside before using git or agents")
    if findings:
        raise SandboxError("a new protected path appeared during the sandboxed command: " + "; ".join(findings))


def snapshot(policy: Policy) -> list[list[str]]:
    """Job-lifetime baseline kept on the host, beyond any one launcher's lifetime."""

    return [[str(path), str(root)] for path, root in _missing_protected_paths(policy)]


def check_snapshot(policy: Policy, baseline: Any) -> None:
    """Recover after a launcher was killed outright; malformed baselines fail closed."""

    if not isinstance(baseline, list):
        raise SandboxError("missing protected-path job snapshot")
    roots = {_key(_real(root)) for root in policy.workspaces}
    names = {_key(name) for name in [*PROTECTED_NAMES, *policy.protected_names]}
    missing = []
    for pair in baseline:
        if not isinstance(pair, list) or len(pair) != 2 or not all(isinstance(p, str) for p in pair):
            raise SandboxError("invalid protected-path job snapshot")
        path, root = map(Path, pair)
        if (not path.is_absolute() or not root.is_absolute() or _key(str(root)) not in roots
                or not _inside(str(path), str(root)) or path == root or _key(path.name) not in names
                or ".." in path.parts):
            raise SandboxError("invalid protected-path job snapshot")
        missing.append((path, root))
    _quarantine_new_paths(missing)


def execute(policy: Policy, command: Command, backend: dict[str, Any], folder: Path,
            *, parent_pid: int | None = None) -> int:
    """Inherit stdio, contain descendants, and end them on timeout or parent death."""

    parent_pid = parent_pid if parent_pid is not None else os.getppid()
    parent_started = process_start_time(parent_pid)
    if parent_started is None:
        raise SandboxError("the parent shell is no longer running")
    adapter = get_backend(str(backend.get("name") or ""))
    argv = backend.get("command")
    if not isinstance(argv, list) or not argv or not all(isinstance(v, str) and v for v in argv):
        raise SandboxError("no backend executable configured")
    make_private_dir(folder)
    path = folder / f"policy-{secrets.token_hex(12)}.json"
    try:
        # Only the launcher can read this folder; the container receives neither
        # its policy nor its authorization request as a filesystem grant.
        protected = replace(policy, pinned=_unique([*policy.pinned, *host_paths(argv)]))
        missing = _missing_protected_paths(protected)
        prepared = adapter.prepare(protected, command, argv, path)
        write_text(path, prepared.policy_text)
        started = time.monotonic()
        try:
            with worker_process(prepared.argv, cwd=command.cwd, env=dict(os.environ),
                                stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr) as child:
                while child.poll() is None:
                    if not same_process(parent_pid, parent_started):
                        return 130
                    if command.timeout and time.monotonic() - started >= command.timeout:
                        return 124
                    time.sleep(0.05)
                return child.returncode
        finally:
            # worker_process has stopped the whole tree, including on early
            # returns and exceptions, before we inspect or rename anything.
            _quarantine_new_paths(missing)
    finally:
        path.unlink(missing_ok=True)


def consume(path: Path, *, parent_pid: int | None = None) -> int:
    """Atomically claim a private one-use request, then execute and discard it."""

    running = path.with_name(path.name + ".running-" + secrets.token_hex(8))
    try:
        path.rename(running)
    except OSError as exc:
        raise SandboxError("the command request is missing or already consumed") from exc
    try:
        if running.stat().st_size > 2 * 1024 * 1024:
            raise SandboxError("command request is too large")
        data = json.loads(running.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1:
            raise SandboxError("unknown command request version")
        return execute(Policy.from_dict(data.get("policy")), Command.from_dict(data.get("command")),
                       data.get("backend") or {}, path.parent, parent_pid=parent_pid)
    finally:
        running.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Execute a private csa sandbox command request")
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--parent-pid", required=True, type=int)
    args = parser.parse_args(argv)
    try:
        return consume(args.request, parent_pid=args.parent_pid)
    except Exception as exc:
        sys.stderr.write(f"csa sandbox failed closed: {exc}\n")
        return 125
