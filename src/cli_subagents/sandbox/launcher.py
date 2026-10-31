"""Shared argv launcher. It has no dependency on an agent or review gate."""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import secrets
import stat
import sys
import tempfile
import time
from typing import Any

from cli_subagents.fsutil import (make_private_dir, process_start_time, recorded_process_alive, same_process,
                                  worker_process, write_json, write_text)
from cli_subagents.sandbox.backends import get_backend
from cli_subagents.sandbox.model import Command, Policy, SandboxError
from cli_subagents.sandbox.policy import PROTECTED_NAMES, _inside, _key, _real, _unique, host_paths

# A launcher's own pre-command snapshot, kept beside its private request until its
# post-command check has run. A launcher killed before that leaves the record behind.
RECOVERY_PREFIX = "recovery-"
RECOVERY_LIMIT = 16 * 1024 * 1024


class QuarantineError(SandboxError):
    """A new protected path appeared; `unresolved` when one of them could not be moved aside."""

    def __init__(self, message: str, unresolved: bool) -> None:
        super().__init__(message)
        self.unresolved = unresolved

# Opportunistic nested-repository detection. Bounds apply independently before
# and after a command; unvisited existing subtrees are never treated as new.
NESTED_DEPTH = 3
NESTED_DIRS = 1024
NESTED_ENTRIES = 20000
NESTED_BYTES = 48 * 1024  # leave room for policy/review fields in the 256 KiB gate context
NESTED_SKIP = {"node_modules", ".venv", "venv", "__pycache__", "target", "dist", "build"}


def _nested_folders(policy: Policy) -> list[dict[str, Any]]:
    result = []
    pending = [(Path(_real(root)), Path(_real(root)), 0) for root in _unique(policy.workspaces)]
    entries = 0
    stored_bytes = 0
    protected = {_key(name) for name in [*PROTECTED_NAMES, *policy.protected_names]}
    while pending and len(result) < NESTED_DIRS and entries < NESTED_ENTRIES:
        folder, root, depth = pending.pop(0)
        children = []
        try:
            # Never follow a link/junction, including one replacing a queued dir.
            attributes = folder.lstat()
            if getattr(attributes, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT or folder.is_symlink():
                continue
            with os.scandir(folder) as items:
                for item in items:
                    entries += 1
                    if entries > NESTED_ENTRIES:
                        # A partial listing cannot prove a child did not exist.
                        return result
                    if (item.is_dir(follow_symlinks=False) and not item.is_symlink()
                            and not getattr(item.stat(follow_symlinks=False), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                        children.append(item.name)
            row = {"folder": str(folder), "root": str(root), "children": children,
                   "git": _entry_exists(folder / ".git")}
            # Budget for the extra indentation when embedded in a gate context.
            stored_bytes += len(json.dumps(row, indent=2, ensure_ascii=False).encode("utf-8")) + 24 * (len(children) + 8)
            if stored_bytes > NESTED_BYTES:
                return result
            result.append(row)
            if depth < NESTED_DEPTH:
                for name in sorted(children):
                    if (_key(name) not in protected and name.lower() not in NESTED_SKIP
                            and ".csa-quarantine-" not in name):
                        pending.append((folder / name, root, depth + 1))
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as exc:
            raise SandboxError(f"cannot scan nested repository protections at {folder}: {exc}") from exc
    return result


def _new_nested_git(policy: Policy, before: list[dict[str, Any]]) -> list[tuple[Path, Path]]:
    known = {_key(row["folder"]): row for row in before}
    new_folders: set[str] = set()
    missing = []
    for row in _nested_folders(policy):
        folder, root = Path(row["folder"]), Path(row["root"])
        previous = known.get(_key(str(folder)))
        parent = known.get(_key(str(folder.parent)))
        new = (_key(str(folder.parent)) in new_folders or
               (parent is not None and _key(folder.name) not in {_key(n) for n in parent["children"]}))
        if new:
            new_folders.add(_key(str(folder)))
        if row["git"] and ((previous is not None and not previous["git"]) or new):
            missing.append((folder / ".git", root))
    return missing


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
    unresolved = False
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
            unresolved = True
            findings.append(f"could not quarantine {path}: {exc}; move it aside before using git or agents")
    if findings:
        raise QuarantineError("a new protected path appeared during the sandboxed command: " + "; ".join(findings),
                              unresolved)


def snapshot(policy: Policy) -> dict[str, Any]:
    """Absent protected entries and nested-repository listings, taken right before a command starts."""

    return {"missing": [[str(path), str(root)] for path, root in _missing_protected_paths(policy)],
            "nested": _nested_folders(policy)}


def check_snapshot(policy: Policy, baseline: Any) -> None:
    """Quarantine protected entries that appeared since `baseline`; malformed baselines fail closed."""

    nested = None
    if isinstance(baseline, dict) and set(baseline) == {"missing", "nested"}:
        nested = baseline["nested"]
        baseline = baseline["missing"]
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
    if nested is not None:
        if not isinstance(nested, list) or len(nested) > NESTED_DIRS:
            raise SandboxError("invalid nested-repository snapshot")
        for row in nested:
            if (not isinstance(row, dict) or set(row) != {"folder", "root", "children", "git"}
                    or not isinstance(row["folder"], str) or not isinstance(row["root"], str)
                    or not isinstance(row["git"], bool) or not isinstance(row["children"], list)
                    or not Path(row["folder"]).is_absolute() or not Path(row["root"]).is_absolute()
                    or _key(row["root"]) not in roots or not _inside(row["folder"], row["root"])
                    or ".." in Path(row["folder"]).parts
                    or not all(isinstance(n, str) and n not in ("", ".", "..") and not any(c in n for c in "\\/:\0")
                               for n in row["children"])):
                raise SandboxError("invalid nested-repository snapshot")
        missing += _new_nested_git(policy, nested)
    missing = list({_key(str(path)): (path, root) for path, root in missing}.values())
    _quarantine_new_paths(missing)


def execute(policy: Policy, command: Command, backend: dict[str, Any], folder: Path,
            *, parent_pid: int | None = None) -> int:
    """Inherit stdio, contain descendants, and end them on timeout or parent death."""

    parent_pid = parent_pid if parent_pid is not None else os.getppid()
    parent_started = process_start_time(parent_pid)
    # Some systems (macOS) give no start stamp; then a live PID is enough, as in same_process.
    if not same_process(parent_pid, parent_started):
        raise SandboxError("the parent shell is no longer running")
    adapter = get_backend(str(backend.get("name") or ""))
    argv = backend.get("command")
    if not isinstance(argv, list) or not argv or not all(isinstance(v, str) and v for v in argv):
        raise SandboxError("no backend executable configured")
    make_private_dir(folder)
    token = secrets.token_hex(12)
    path = folder / f"policy-{token}.json"
    record = folder / f"{RECOVERY_PREFIX}{token}.json"
    try:
        # Only the launcher can read this folder; the container receives neither
        # its policy nor its authorization request as a filesystem grant.
        protected = replace(policy, pinned=_unique([*policy.pinned, *host_paths(argv)]))
        baseline = snapshot(protected)
        # The check below compares against this command's own start, so entries the
        # host creates between commands (an approved edit, the user's git init) are
        # never mistaken for planted ones. If this launcher is killed before the
        # check, recover() runs it from this record.
        write_json(record, {"version": 1, "policy": protected.to_dict(), "baseline": baseline,
                            "pid": os.getpid(), "started": process_start_time(os.getpid())})
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
            settled = False
            try:
                check_snapshot(protected, baseline)
                settled = True
            except QuarantineError as exc:
                # Keep the record while a planted item could not be moved: later
                # authorizations retry the move and keep failing closed until then.
                settled = not exc.unresolved
                raise
            finally:
                if settled:
                    record.unlink(missing_ok=True)
    finally:
        path.unlink(missing_ok=True)


def recover(folder: Path) -> None:
    """Run the post-command check of every launcher in `folder` that was killed before its own.

    A record whose launcher still runs is left to that launcher. Unreadable or
    malformed records, and check errors, fail closed and keep the record; every
    record is still tried before the first error is raised.
    """

    errors: list[SandboxError] = []
    for record in sorted(folder.glob(f"{RECOVERY_PREFIX}*.json")) if folder.is_dir() else []:
        try:
            try:
                if record.stat().st_size > RECOVERY_LIMIT:
                    raise SandboxError(f"sandbox recovery record {record.name} is too large")
                data = json.loads(record.read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue  # its launcher finished meanwhile
            except (OSError, ValueError) as exc:
                raise SandboxError(f"cannot read sandbox recovery record {record.name}: {exc}") from exc
            if not isinstance(data, dict) or data.get("version") != 1:
                raise SandboxError(f"invalid sandbox recovery record {record.name}")
            if recorded_process_alive(data.get("pid"), data.get("started")) or not record.exists():
                continue
            try:
                check_snapshot(Policy.from_dict(data.get("policy")), data.get("baseline"))
            except QuarantineError as exc:
                if not exc.unresolved:
                    record.unlink(missing_ok=True)  # reported now; everything was moved aside
                raise
            record.unlink(missing_ok=True)
        except SandboxError as exc:
            errors.append(exc)
    if errors:
        raise errors[0] if len(errors) == 1 else SandboxError("; ".join(str(exc) for exc in errors))


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
