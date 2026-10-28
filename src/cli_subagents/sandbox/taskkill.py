"""`taskkill` for use inside an MXC process container.

Windows' own taskkill.exe never returns inside a process container (any form
of `/PID`, checked with MXC 1.0.1 on build 26300.9711, 2026-10-26), and tools
stop their children with it: OpenCode stops its private server with
`taskkill /pid N /T /F`, Node's tree-kill does the same. csa puts a
`taskkill.cmd` that runs this script first on PATH inside the sandbox.

It supports what those tools use: `/PID n` (repeatable), `/IM name`, `/T` and
`/F`. Processes are always terminated (as with /F); /T includes descendants.
Standard library only, no csa imports: it runs with `python -I`.
"""

from __future__ import annotations

import ctypes
import fnmatch
import sys

TH32CS_SNAPPROCESS = 0x2
PROCESS_TERMINATE = 0x0001
NOT_FOUND = 128


# PROCESSENTRY32W, with DWORD spelled as c_uint32 so the module imports on every OS.
class _Entry(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32), ("cntUsage", ctypes.c_uint32), ("th32ProcessID", ctypes.c_uint32),
        ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", ctypes.c_uint32), ("cntThreads", ctypes.c_uint32),
        ("th32ParentProcessID", ctypes.c_uint32), ("pcPriClassBase", ctypes.c_long), ("dwFlags", ctypes.c_uint32),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def _processes() -> list[tuple[int, int, str]]:
    """(pid, parent pid, image name) of every process this container can see."""

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    for name in ("Process32FirstW", "Process32NextW"):
        getattr(kernel, name).argtypes = [ctypes.c_void_p, ctypes.POINTER(_Entry)]
    snapshot = kernel.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot in (None, ctypes.c_void_p(-1).value):
        return []
    found = []
    try:
        entry = _Entry()
        entry.dwSize = ctypes.sizeof(_Entry)
        ok = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            found.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID), str(entry.szExeFile)))
            ok = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    return found


def _terminate(pid: int) -> bool:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not handle:
        return False
    try:
        return bool(kernel.TerminateProcess(handle, 1))
    finally:
        kernel.CloseHandle(handle)


def _parse(args: list[str]) -> tuple[list[int], list[str], bool] | str:
    pids: list[int] = []
    images: list[str] = []
    tree = False
    index = 0
    while index < len(args):
        word = args[index].lower()
        if word in ("/t", "-t"):
            tree = True
        elif word in ("/f", "-f"):
            pass
        elif word in ("/pid", "-pid", "/im", "-im") and index + 1 < len(args):
            value = args[index + 1]
            index += 1
            if word.endswith("pid"):
                if not value.isdigit():
                    return f"ERROR: Invalid argument/option - '{value}'."
                pids.append(int(value))
            else:
                images.append(value.lower())
        else:
            return (f"ERROR: Inside csa's sandbox, taskkill supports only /PID, /IM, /T and /F "
                    f"(got '{args[index]}').")
        index += 1
    if not pids and not images:
        return "ERROR: Inside csa's sandbox, taskkill needs /PID or /IM."
    return pids, images, tree


def main(args: list[str]) -> int:
    parsed = _parse(args)
    if isinstance(parsed, str):
        print(parsed, file=sys.stderr)
        return 1
    pids, images, tree = parsed
    table = _processes()
    children: dict[int, list[int]] = {}
    for pid, parent, _name in table:
        if pid != parent:
            children.setdefault(parent, []).append(pid)
    known = {pid for pid, _parent, _name in table}
    targets = [pid for pid in pids]
    targets += [pid for pid, _parent, name in table if any(fnmatch.fnmatch(name.lower(), image) for image in images)]
    code = 0
    for target in targets:
        if target not in known:
            print(f'ERROR: The process "{target}" not found.', file=sys.stderr)
            code = NOT_FOUND
            continue
        order: list[int] = []
        if tree:
            pending = list(children.get(target, []))
            while pending:
                pid = pending.pop()
                if pid in order or pid == target:
                    continue
                order.append(pid)
                pending.extend(children.get(pid, []))
        # Descendants first, so none is left behind when its parent goes.
        for pid in [*reversed(order), target]:
            if _terminate(pid):
                print(f"SUCCESS: The process with PID {pid} has been terminated.")
            elif pid == target:
                print(f"ERROR: The process with PID {pid} could not be terminated.", file=sys.stderr)
                code = code or 1
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
