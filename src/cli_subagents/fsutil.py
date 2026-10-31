"""Small filesystem and process helpers shared by every module."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import time
from typing import Any, BinaryIO, Iterator

if os.name == "nt":
    import msvcrt
else:  # pragma: no cover - exercised on POSIX only
    import fcntl


def read_json(path: Path, default: Any = None) -> Any:
    try:
        # Editors and PowerShell 5.1 can save user files such as settings.json with a byte order mark.
        return json.loads(read_text(path).removeprefix("﻿"))
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        return default


def read_text(path: Path) -> str:
    """Read a UTF-8 file that another process may be replacing with write_private."""

    attempt = 0
    while True:
        try:
            return path.read_text(encoding="utf-8")
        except PermissionError:
            # Windows refuses to open a file for a moment while os.replace swaps it.
            attempt += 1
            if os.name != "nt" or attempt == 20:
                raise
            time.sleep(0.05)


def make_private_dir(path: Path) -> None:
    """Create `path` and its missing parents with owner-only permissions (0o700).

    `Path.mkdir(parents=True)` ignores the mode for parents, so each missing
    level is created on its own. Existing folders are left as they are.
    """

    missing: list[Path] = []
    current = Path(path)
    while not os.path.lexists(current) and current.parent != current:
        missing.append(current)
        current = current.parent
    for folder in reversed(missing):
        try:
            folder.mkdir(mode=0o700)
        except FileExistsError:
            pass


def open_private(path: Path, append: bool = True) -> BinaryIO:
    """Open a log file for binary writing; a new file gets owner-only permissions (0o600)."""

    make_private_dir(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
    flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    return os.fdopen(os.open(path, flags, 0o600), "ab" if append else "wb")


def temp_sibling(path: Path) -> Path:
    """A random, unused-looking name next to `path` for write-then-rename."""

    return path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")


def copy_exclusive(source: Path, destination: Path) -> None:
    """Copy a file to a path that must not exist yet (O_EXCL: never follows a link)."""

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(destination, flags, 0o666)
    try:
        with os.fdopen(descriptor, "wb") as target, source.open("rb") as origin:
            shutil.copyfileobj(origin, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(destination)
        raise


def replace_with_copy(source: Path, destination: Path) -> None:
    """Put a copy of `source` at `destination` via a fresh temporary file and a rename.

    A link already at `destination` is replaced, never written through.
    """

    temporary = temp_sibling(destination)
    copy_exclusive(source, temporary)
    try:
        os.replace(temporary, destination)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def write_private(path: Path, data: bytes) -> None:
    """Write a file atomically with owner-only permissions where the OS supports it."""

    make_private_dir(path.parent)
    temporary = temp_sibling(path)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        for attempt in range(20):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                # Windows refuses to replace a file another process has open.
                if attempt == 19:
                    raise
                time.sleep(0.05)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_json(path: Path, value: Any) -> None:
    write_private(path, (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


def write_text(path: Path, text: str) -> None:
    write_private(path, text.encode("utf-8"))


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def same_path(left: Path | str, right: Path | str) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


@contextlib.contextmanager
def file_lock(path: Path, timeout: float = 10.0) -> Iterator[None]:
    """Hold an exclusive OS lock on `path` for the duration of the block."""

    handle = try_lock(path, timeout=timeout)
    if handle is None:
        raise TimeoutError(f"timed out waiting for lock {path.name}")
    try:
        yield
    finally:
        unlock(handle)


def try_lock(path: Path, timeout: float = 0.0) -> BinaryIO | None:
    """Return an open, locked handle, or None if the lock is held elsewhere."""

    make_private_dir(path.parent)
    deadline = time.monotonic() + timeout
    while True:
        handle = path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except OSError:
            handle.close()
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.05)


def unlock(handle: BinaryIO) -> None:
    try:
        handle.seek(0)
        if os.name == "nt":
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:  # pragma: no cover
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        handle.close()


# Links -----------------------------------------------------------------------


def is_link(path: Path) -> bool:
    """True for symlinks and Windows directory junctions."""

    if path.is_symlink():
        return True
    if os.name == "nt":
        try:
            return bool(os.lstat(path).st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT)
        except (OSError, AttributeError):
            return False
    return False


def link_target(path: Path) -> str | None:
    try:
        target = os.readlink(path)
    except OSError:
        return None
    if target.startswith("\\\\?\\"):
        target = target[4:]
    return target


def make_dir_link(source: Path, link: Path) -> str:
    """Link a directory without admin rights. Returns 'junction', 'symlink' or 'copy'."""

    link.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        try:
            import _winapi

            _winapi.CreateJunction(str(source), str(link))
            return "junction"
        except (ImportError, OSError):
            pass
    try:
        os.symlink(source, link, target_is_directory=True)
        return "symlink"
    except OSError:
        shutil.copytree(source, link)
        return "copy"


def remove_link(path: Path) -> None:
    """Remove a symlink or junction without touching what it points to."""

    if not is_link(path):
        raise ValueError(f"refusing to remove non-link {path}")
    if os.name == "nt" and path.is_dir():
        os.rmdir(path)
    else:
        os.unlink(path)


def tree_signature(path: Path) -> str:
    """Cheap change detector for copied directories: file names, sizes and mtimes."""

    digest = hashlib.sha256()
    for item in sorted(path.rglob("*")):
        if item.is_file():
            info = item.stat()
            digest.update(f"{item.relative_to(path)}|{info.st_size}|{int(info.st_mtime)}".encode())
    return digest.hexdigest()


# Processes -------------------------------------------------------------------


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        import ctypes

        process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not process:
            return False
        try:
            code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(process, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(process)
    for process in _SPAWNED:  # pragma: no cover - POSIX zombie reaping
        if process.pid == pid and process.poll() is not None:
            return False
    try:  # pragma: no cover
        finished, _status = os.waitpid(pid, os.WNOHANG)
        if finished == pid:
            return False
    except ChildProcessError:
        pass
    except OSError:
        return False
    try:  # pragma: no cover
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def process_start_time(pid: int | None) -> int | None:
    """An opaque start-time stamp for a running process, to tell it from a later one with the same PID.

    Windows: creation time from GetProcessTimes (100 ns units). Linux: field 22
    of /proc/<pid>/stat (clock ticks since boot). Elsewhere, or when it can't be
    read: None.
    """

    if not pid:
        return None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = wintypes.HANDLE
        process = kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
        if not process:
            return None
        try:
            # An exited process stays readable while anyone holds a handle to it; it isn't running.
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(process, ctypes.byref(code)) or code.value != 259:  # STILL_ACTIVE
                return None
            created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
            ok = kernel32.GetProcessTimes(
                process, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)
            )
            if not ok:
                return None
            return (created.dwHighDateTime << 32) | created.dwLowDateTime
        finally:
            kernel32.CloseHandle(process)
    try:  # pragma: no cover - Linux only
        raw = Path(f"/proc/{int(pid)}/stat").read_text(encoding="ascii", errors="replace")
        # The command name (field 2) may contain spaces or ')', so split after the last ')'.
        fields = raw[raw.rindex(")") + 2 :].split()
        return int(fields[19])
    except (OSError, ValueError, IndexError):
        return None


def same_process(pid: int | None, started: int | None) -> bool:
    """True if `pid` is alive and is the process whose start stamp was recorded.

    When either stamp is unknown (unsupported OS), an alive PID counts as a match.
    """

    if not pid or not pid_alive(int(pid)):
        return False
    if started is None:
        return True
    current = process_start_time(int(pid))
    return current is None or current == int(started)


def recorded_process_alive(pid: Any, started: Any) -> bool:
    """same_process() for a PID and start stamp read from a record; malformed values count as gone."""

    try:
        return same_process(int(pid), int(started) if started is not None else None) if pid else False
    except (TypeError, ValueError, OverflowError):
        return False


def kill_tree(pid: int) -> None:
    """Terminate a process and all of its children."""

    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            check=False,
            timeout=30,
            **no_window_kwargs(),
        )
        return
    try:  # pragma: no cover
        os.killpg(pid, signal.SIGTERM)
        time.sleep(2)
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass


def no_window_kwargs() -> dict[str, Any]:
    """Popen/run arguments that keep a console program from opening a window on Windows.

    The background runner has no console of its own, so without this every console
    program it starts (version and login checks, taskkill) flashes a window.
    """

    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def new_group_kwargs() -> dict[str, Any]:
    """Popen arguments that put the child in its own process group."""

    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}  # pragma: no cover


class _WindowsJob:
    """Own a worker tree, including descendants whose original parent has exited.

    https://learn.microsoft.com/windows/win32/procthread/job-objects
    """

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits), ("IoInfo", ctypes.c_ulonglong * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "QueryInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                           ctypes.c_void_p], wintypes.BOOL),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = arguments, result
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.kernel.CloseHandle(self.handle)
            raise error

    def start(self, process: subprocess.Popen) -> None:
        import ctypes
        from ctypes import wintypes

        handle = wintypes.HANDLE(int(process._handle))
        if not self.kernel.AssignProcessToJobObject(self.handle, handle):
            raise ctypes.WinError(ctypes.get_last_error())
        # Popen closes the initial thread handle. Resume through its process handle
        # only after assignment, so even a short-lived worker cannot escape the job.
        resume = ctypes.WinDLL("ntdll").NtResumeProcess
        resume.argtypes, resume.restype = [wintypes.HANDLE], ctypes.c_long
        status = resume(handle)
        if status < 0:
            raise OSError(f"NtResumeProcess failed: {status:#x}")

    def close(self) -> None:
        import ctypes
        from ctypes import wintypes

        class Accounting(ctypes.Structure):
            _fields_ = [
                ("times", ctypes.c_longlong * 4), ("TotalPageFaultCount", wintypes.DWORD),
                ("TotalProcesses", wintypes.DWORD), ("ActiveProcesses", wintypes.DWORD),
                ("TotalTerminatedProcesses", wintypes.DWORD),
            ]

        try:
            if not self.kernel.TerminateJobObject(self.handle, 1):
                raise ctypes.WinError(ctypes.get_last_error())
            while True:
                accounting = Accounting()
                if not self.kernel.QueryInformationJobObject(
                    self.handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None
                ):
                    raise ctypes.WinError(ctypes.get_last_error())
                if not accounting.ActiveProcesses:
                    break
                time.sleep(0.01)
        finally:
            self.kernel.CloseHandle(self.handle)


BATCH_SUFFIXES = (".cmd", ".bat")
BATCH_UNQUOTED = frozenset("&|<>^()")


def batch_argument_problem(argv: list[str]) -> str | None:
    """Why cmd.exe would not pass `argv` to a .cmd/.bat program unchanged, or None.

    Windows starts a batch file through cmd.exe, which reads the command line that
    subprocess.list2cmdline built: `&`, `|`, `<`, `>`, `^` and parentheses outside
    double quotes start other commands or vanish, and `%` expands variables even
    inside quotes. list2cmdline writes an embedded quote as `\\"`, which cmd.exe
    still counts as a quote, so such an argument can leave the quoted part.
    """

    if not argv or Path(str(argv[0])).suffix.lower() not in BATCH_SUFFIXES:
        return None
    quoted = False
    for char in subprocess.list2cmdline([str(item) for item in argv[1:]]):
        if char in "\r\n":
            return "an argument contains a line break"
        if char == "%":
            return "an argument contains %"
        if char == '"':
            quoted = not quoted
        elif not quoted and char in BATCH_UNQUOTED:
            return f"an argument contains {char!r} that cmd.exe would interpret"
    return None


@contextlib.contextmanager
def worker_process(argv: list[str], **kwargs: Any) -> Iterator[subprocess.Popen]:
    """Launch a contained worker; destroy its whole tree before releasing its lease."""

    problem = batch_argument_problem(argv) if os.name == "nt" else None
    if problem:
        raise OSError(f"{Path(str(argv[0])).name} is a batch file run by cmd.exe and {problem}; "
                      "point the runtime's binary setting at the program itself (an .exe, or node with its script)")
    job = _WindowsJob() if os.name == "nt" else None
    process = None
    options = new_group_kwargs()
    if job is not None:
        options["creationflags"] |= 0x4  # CREATE_SUSPENDED: assign before the first instruction
    try:
        process = subprocess.Popen(argv, **kwargs, **options)
        if job is not None:
            job.start(process)
        yield process
    finally:
        try:
            if job is not None:
                job.close()
            elif process is not None:
                # The group can outlive its leader, so cleanup is unconditional.
                kill_tree(process.pid)
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()  # also covers a Windows assignment failure while suspended
                process.wait(timeout=15)
                for stream in (process.stdin, process.stdout):
                    if stream is not None:
                        stream.close()


# Detached children we started. Keeping them referenced lets us reap finished
# ones (no zombies on POSIX) without warnings about still-running processes.
_SPAWNED: list[subprocess.Popen] = []


def spawn_detached(argv: list[str], log_path: Path, env: dict[str, str] | None = None) -> int:
    """Start a background process that outlives its parent. Returns its PID."""

    _SPAWNED[:] = [process for process in _SPAWNED if process.poll() is None]
    log = open_private(log_path)
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": log,
        "stderr": log,
        "close_fds": True,
        "env": env,
    }
    try:
        if os.name == "nt":
            base = (
                subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NO_WINDOW
            )
            try:
                # Leave the host's job object so closing the host doesn't kill running jobs.
                process = subprocess.Popen(argv, creationflags=base | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kwargs)
            except OSError:
                process = subprocess.Popen(argv, creationflags=base, **kwargs)
        else:  # pragma: no cover
            process = subprocess.Popen(argv, start_new_session=True, **kwargs)
    finally:
        log.close()
    _SPAWNED.append(process)
    return process.pid


def python_executable() -> str:
    """The interpreter to use for background runners (pythonw-safe)."""

    executable = Path(sys.executable)
    if executable.name.lower() == "pythonw.exe":
        candidate = executable.with_name("python.exe")
        if candidate.exists():
            return str(candidate)
    return str(executable)
