"""Single-use UTF-8 payloads in csa's managed relay inboxes.

The workspace inbox lets native file tools use their existing writable root.
Neither a caller-supplied directory nor the worker's requested cwd is trusted.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import secrets
import stat
import time

from cli_subagents.config import home
from cli_subagents.fsutil import make_private_dir, write_private

MAX_BYTES = 16 * 1024 * 1024
STALE_SECONDS = 24 * 60 * 60
NAME = re.compile(r"relay-[0-9a-f]{32}\.txt\Z")
WORKSPACE_INBOX = ".csa-relay-inbox"


def inbox(workspace: bool = False) -> Path:
    # Resolve only the trusted base, never an inbox or a supplied filename.
    base = Path.cwd().resolve() if workspace else home().resolve()
    return base / (WORKSPACE_INBOX if workspace else "relay-inbox")


def _linked(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _directory(folder: Path) -> os.stat_result:
    info = folder.lstat()
    if _linked(info) or not stat.S_ISDIR(info.st_mode) or folder.resolve() != folder:
        raise ValueError("relay inbox must be a real directory without links or junctions")
    if hasattr(os, "getuid") and (info.st_uid != os.getuid() or info.st_mode & 0o022):
        raise ValueError("relay inbox must belong to this user and not be writable by others")
    return info


def _regular(info: os.stat_result) -> None:
    if _linked(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("relay payload must be a regular file without links or reparse points")
    if info.st_size > MAX_BYTES:
        raise ValueError(f"relay payload exceeds {MAX_BYTES} bytes")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise ValueError("relay payload must belong to this user")


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def validate_path(value: str, *, new: bool = False, home_only: bool = False) -> Path:
    """Validate metadata only; never open the caller's payload during validation."""

    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or not NAME.fullmatch(path.name):
        raise ValueError("relay path must be an absolute csa inbox filename (relay-<32 hex>.txt)")
    roots = (inbox(),) if home_only else (inbox(), inbox(workspace=True))
    if path.parent not in roots:
        raise ValueError("relay file must be directly inside a csa relay inbox")
    _directory(path.parent)
    if new:
        if os.path.lexists(path):
            raise ValueError("relay Write must create a new inbox file")
    else:
        _regular(path.lstat())
    return path


def cleanup(folder: Path) -> None:
    """Remove only stale, direct, ordinary csa payload files; never follow links."""

    _directory(folder)
    cutoff = time.time() - STALE_SECONDS
    for path in folder.iterdir():
        if not NAME.fullmatch(path.name):
            continue
        try:
            info = path.lstat()
            if not _linked(info) and stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass  # another relay may have consumed its file


def fresh_path(*, workspace: bool = False) -> Path:
    folder = inbox(workspace)
    make_private_dir(folder)
    cleanup(folder)
    if workspace:
        # Publish no payload path until Git ignores this inbox. Atomic replace
        # also repairs an existing ignore file without following a link to it.
        write_private(folder / ".gitignore", b"*\n")
    while True:
        path = folder / f"relay-{secrets.token_hex(16)}.txt"
        if not os.path.lexists(path):
            return path


def consume(value: str) -> str:
    """Validate before reading, bound the read, then delete before processing text.

    O_NOFOLLOW protects POSIX opens. Metadata/identity checks before any read
    also catch Windows reparse points, hard links and replacements during open.
    These are path restrictions, not isolation from another same-user process.
    """

    path = validate_path(value)
    parent = _directory(path.parent)
    before = path.lstat()
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        _regular(opened)
        if _identity(before) != _identity(opened) or _identity(parent) != _identity(_directory(path.parent)):
            raise ValueError("relay file or inbox changed while opening")
        current = path.lstat()
        _regular(current)
        if _identity(current) != _identity(opened):
            raise ValueError("relay file changed while opening")
        data = stream.read(MAX_BYTES + 1)
    # Windows cannot unlink a file while this stream is open.
    current = path.lstat()
    _regular(current)
    if _identity(current) != _identity(opened) or _identity(parent) != _identity(_directory(path.parent)):
        raise ValueError("relay file or inbox changed while reading")
    path.unlink()
    if len(data) > MAX_BYTES:
        raise ValueError(f"relay payload exceeds {MAX_BYTES} bytes")
    # Decode bytes directly: preserve CRLF, trailing newlines and whitespace.
    return data.decode("utf-8")
