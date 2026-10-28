"""Install, verify and report csa's pinned MXC release (`csa setup`, `csa update`, `csa doctor`).

The release is one zip of about 540 MB with every platform's files. csa
downloads it (or reuses a verified earlier download), checks its size and
SHA-256 against the pinned values before opening it, and extracts only the
top-level `.exe` and `.dll` files of this machine's architecture into a fresh
staging folder. Every extracted file must carry a valid Authenticode signature
by Microsoft before the folder is swapped into place. A record of the install
(`csa-install.json`: release, file names and hashes) lets later runs check the
files without the zip, which is deleted once the install is in place.

Nothing here enables MXC telemetry or runs `--telemetry-consent`.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import sys
import time
from typing import Any
import urllib.request
import zipfile

from cli_subagents import __version__
from cli_subagents.config import SandboxSettings, config_path
from cli_subagents.fsutil import is_link, make_private_dir, read_json, try_lock, unlock, write_json
from cli_subagents.sandbox import mxc, state
from cli_subagents.sandbox.mxc import MxcError

MARKER = "csa-install.json"
LOCK = ".install.lock"
CHUNK = 1024 * 1024
MEMBER_LIMIT = 128 * 1024 * 1024  # no single MXC binary comes close (the largest x64 file is a few MB)
TOTAL_LIMIT = 512 * 1024 * 1024
SPACE_MARGIN = 256 * 1024 * 1024
DOWNLOAD_TIMEOUT = 60
# A plain file name: no separators, drive letters, streams or dot-only names.
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,126}\.(?:exe|dll)", re.IGNORECASE)
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(10)), *(f"lpt{i}" for i in range(10))}


class InstallError(Exception):
    """The install can't go ahead; the message says why."""


def _off_hint() -> str:
    return f"set `enabled = false` under [sandbox] in {config_path()} to run workers without it"


def _update_windows_todo() -> str:
    return ("Update Windows: csa's sandbox needs MXC process containers (Windows 11 24H2 26100.9278, 25H2 26200.9278 "
            f"or later). Or {_off_hint()}.")


def _mb(size: int) -> str:
    return f"{size / (1024 * 1024):.0f} MB" if size >= 10 * 1024 * 1024 else f"{size / (1024 * 1024):.1f} MB"


def download_dir() -> Path:
    return mxc.install_root() / f"download-{mxc.VERSION}"


def download_path() -> Path:
    return download_dir() / mxc.ASSET_NAME


def member_name(name: str, arch: str) -> str | None:
    """The file name to install for a zip member `<arch>/<name>.exe|dll`, or None to skip the member.

    Anything that isn't a plain file directly in the architecture folder is
    skipped: deeper paths, '..', absolute paths, drive letters, backslashes,
    NUL, alternate data streams and device names.
    """

    if not name or "\x00" in name or "\\" in name or ":" in name or name.startswith("/"):
        return None
    parts = name.split("/")
    if len(parts) != 2 or parts[0].lower() != arch.lower():
        return None
    base = parts[1]
    if not _NAME.fullmatch(base) or base.split(".")[0].lower() in _RESERVED:
        return None
    return base


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _remove_tree(path: Path) -> None:
    """Remove a folder csa made. A link is removed itself, never followed."""

    if is_link(path):
        try:
            os.unlink(path)
        except OSError:
            os.rmdir(path)
        return

    def retry(function: Any, target: str, _: Any) -> None:
        os.chmod(target, stat.S_IWRITE)
        function(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=retry)


def _signature_check(paths: list[Path]) -> tuple[bool, list[str]]:
    """(all valid and signed by Microsoft, problems)."""

    try:
        results = mxc.signatures(paths)
    except MxcError as exc:
        return False, [str(exc)]
    problems = []
    for path in paths:
        result = results.get(str(path)) or {}
        if result.get("ok"):
            continue
        detail = str(result.get("status") or "no signature result")
        subject = str(result.get("subject") or "")
        if subject and subject != mxc.SIGNER:
            detail += f", signed by {subject}"
        problems.append(f"{path.name}: {detail}")
    return not problems, problems


# Checking an install -----------------------------------------------------------


def check_install(folder: Path, signatures: bool = True) -> dict[str, Any]:
    """Whether `folder` holds exactly the files csa installed from the pinned release.

    Returns {"ok", "reason", "files", "signatures"}; "signatures" is
    {"ok", "problems"} once the hashes matched and signatures were checked.
    """

    result: dict[str, Any] = {"ok": False, "reason": "", "files": [], "signatures": None}
    if not os.path.lexists(folder):
        result["reason"] = "not installed"
        return result
    if is_link(folder) or not folder.is_dir():
        result["reason"] = f"{folder} isn't a plain folder"
        return result
    marker = read_json(folder / MARKER)
    if not isinstance(marker, dict):
        result["reason"] = "no csa install record (not installed by csa)"
        return result
    if marker.get("version") != mxc.VERSION or marker.get("asset_sha256") != mxc.ASSET_SHA256:
        result["reason"] = f"installed from another release ({marker.get('version')})"
        return result
    entries = marker.get("files")
    if not isinstance(entries, list) or not all(
        isinstance(entry, dict) and isinstance(entry.get("name"), str) and isinstance(entry.get("sha256"), str)
        and _NAME.fullmatch(entry["name"]) for entry in entries
    ):
        result["reason"] = "the install record is damaged"
        return result
    names = {entry["name"].lower() for entry in entries}
    if mxc.EXECUTABLE.lower() not in names:
        result["reason"] = f"the install record doesn't list {mxc.EXECUTABLE}"
        return result
    try:
        present = {item.name.lower() for item in folder.iterdir()} - {MARKER.lower()}
    except OSError as exc:
        result["reason"] = f"can't list the folder: {exc}"
        return result
    extra = sorted(present - names)
    if extra:
        # A stray DLL next to wxc-exec.exe could be loaded by it.
        result["reason"] = "it has files csa didn't install: " + ", ".join(extra[:5])
        return result
    paths = []
    for entry in entries:
        path = folder / entry["name"]
        if not path.is_file() or is_link(path):
            result["reason"] = f"{entry['name']} is missing"
            return result
        try:
            changed = _sha256(path) != entry["sha256"]
        except OSError as exc:
            result["reason"] = f"can't read {entry['name']}: {exc}"
            return result
        if changed:
            result["reason"] = f"{entry['name']} was changed"
            return result
        paths.append(path)
    result["files"] = paths
    if signatures:
        ok, problems = _signature_check(paths)
        result["signatures"] = {"ok": ok, "problems": problems}
        if not ok:
            result["reason"] = "signature check failed: " + "; ".join(problems[:5])
            return result
    result["ok"] = True
    result["reason"] = "verified"
    return result


# Downloading -----------------------------------------------------------------


def _check_space(folder: Path) -> None:
    probe = folder
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError:
        return
    needed = mxc.ASSET_SIZE + SPACE_MARGIN
    if free < needed:
        raise InstallError(f"the download needs about {_mb(needed)} free on the drive of {folder}, but only {_mb(free)} is free")


def _download(url: str, destination: Path) -> tuple[int, str]:
    """Stream `url` into a new file. Returns (bytes, sha256); never writes more than ASSET_SIZE bytes."""

    expected = mxc.ASSET_SIZE
    request = urllib.request.Request(url, headers={"User-Agent": f"cli-subagents/{__version__}"})
    digest = hashlib.sha256()
    size = 0
    shown = 0
    last = time.monotonic()
    with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response, destination.open("xb") as output:
        length = response.headers.get("Content-Length")
        if length is not None and length.strip().isdigit() and int(length) != expected:
            raise InstallError(f"the server offers {int(length):,} bytes, but the pinned release has {expected:,}")
        while chunk := response.read(CHUNK):
            size += len(chunk)
            if size > expected:
                raise InstallError(f"the download is larger than the pinned release ({expected:,} bytes)")
            digest.update(chunk)
            output.write(chunk)
            percent = size * 100 // expected if expected else 100
            if percent >= shown + 10 or time.monotonic() - last >= 15:
                print(f"  {percent}% ({_mb(size)} of {_mb(expected)})", flush=True)
                shown, last = percent - percent % 10, time.monotonic()
    return size, digest.hexdigest()


def _obtain(report: Any) -> Path:
    """The verified release zip: an earlier verified download, or a new one."""

    folder = download_dir()
    path = folder / mxc.ASSET_NAME
    if path.is_file() and not is_link(path) and path.stat().st_size == mxc.ASSET_SIZE:
        print(f"Checking the earlier download {path} ...", flush=True)
        if _sha256(path) == mxc.ASSET_SHA256:
            report.done.append(f"Sandbox: reused the earlier download {path} (size and SHA-256 match the pinned release).")
            return path
        print("  its SHA-256 doesn't match; downloading again.", flush=True)
    make_private_dir(folder)
    _check_space(folder)
    part = folder / f".{mxc.ASSET_NAME}.{secrets.token_hex(6)}.part"
    print(f"Downloading MXC {mxc.VERSION} ({_mb(mxc.ASSET_SIZE)}) from {mxc.ASSET_URL} ...", flush=True)
    try:
        try:
            size, digest = _download(mxc.ASSET_URL, part)
        except InstallError:
            raise
        except Exception as exc:  # urllib raises URLError, HTTPError, OSError, ValueError, ...
            raise InstallError(f"the download from {mxc.ASSET_URL} failed: {type(exc).__name__}: {exc}") from exc
        if size != mxc.ASSET_SIZE:
            raise InstallError(f"the download has {size:,} bytes, but the pinned release has {mxc.ASSET_SIZE:,}; "
                               "it was deleted and nothing was installed")
        if digest != mxc.ASSET_SHA256:
            raise InstallError(f"the download's SHA-256 is {digest}, not the pinned {mxc.ASSET_SHA256}; "
                               "it was deleted and nothing was installed")
        os.replace(part, path)
    finally:
        if part.exists():
            part.unlink()
    report.done.append(f"Sandbox: downloaded {mxc.ASSET_NAME} ({_mb(mxc.ASSET_SIZE)}); size and SHA-256 match the pinned release.")
    return path


# Extracting ------------------------------------------------------------------


def extract(zip_path: Path, staging: Path, arch: str) -> list[dict[str, Any]]:
    """Copy `<arch>/*.exe` and `<arch>/*.dll` (top level only) into the empty folder `staging`."""

    found: dict[str, dict[str, Any]] = {}
    total = 0
    root = staging.resolve()
    try:
        with zipfile.ZipFile(zip_path) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                base = member_name(info.filename, arch)
                if base is None:
                    continue
                if base.lower() in found:
                    raise InstallError(f"the release has {arch}/{base} twice")
                if info.file_size > MEMBER_LIMIT:
                    raise InstallError(f"{arch}/{base} is larger than csa allows ({info.file_size:,} bytes)")
                target = staging / base
                if target.resolve().parent != root:
                    raise InstallError(f"refused the zip member {info.filename!r}")
                digest = hashlib.sha256()
                size = 0
                with archive.open(info) as source, target.open("xb") as output:
                    while chunk := source.read(CHUNK):
                        size += len(chunk)
                        total += len(chunk)
                        if size > info.file_size or size > MEMBER_LIMIT or total > TOTAL_LIMIT:
                            raise InstallError(f"{arch}/{base} unpacks to more than the zip says")
                        digest.update(chunk)
                        output.write(chunk)
                found[base.lower()] = {"name": base, "size": size, "sha256": digest.hexdigest()}
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, RuntimeError, EOFError) as exc:
        raise InstallError(f"couldn't extract {zip_path.name}: {type(exc).__name__}: {exc}") from exc
    if mxc.EXECUTABLE.lower() not in found:
        raise InstallError(f"the release has no {arch}/{mxc.EXECUTABLE}")
    return sorted(found.values(), key=lambda entry: entry["name"].lower())


def _swap(staging: Path, target: Path) -> None:
    """Put `staging` at `target`; an old folder there is moved aside first, then removed."""

    make_private_dir(target.parent)
    aside = None
    if os.path.lexists(target):
        aside = target.with_name(f"{target.name}.old-{secrets.token_hex(4)}")
        try:
            os.replace(target, aside)
        except OSError as exc:
            raise InstallError(f"couldn't move the old install at {target} aside ({exc}). Wait for running csa "
                               "workers to finish and try again") from exc
    try:
        os.replace(staging, target)
    except OSError as exc:
        if aside is not None:
            try:
                os.replace(aside, target)
            except OSError:
                pass
        raise InstallError(f"couldn't move the new install into {target}: {exc}") from exc
    if aside is not None:
        try:
            _remove_tree(aside)
        except OSError:
            pass  # removed on the next install


def _clear_leftovers() -> None:
    """Folders and partial downloads an interrupted install left behind (csa's own names only)."""

    root = mxc.install_root()
    leftovers = list(root.glob(".staging-*")) + list(root.glob("mxc-*/*.old-*"))
    for folder in leftovers:
        try:
            _remove_tree(folder)
        except OSError:
            pass
    for part in download_dir().glob(f".{mxc.ASSET_NAME}.*.part"):
        try:
            part.unlink()
        except OSError:
            pass


def _cleanup(report: Any, dry_run: bool, protect: list[Path]) -> None:
    """Remove other csa-installed MXC versions and the downloaded zip once the current install is in place."""

    root = mxc.install_root()
    current = f"mxc-{mxc.VERSION}".lower()
    keep = [path.resolve() for path in protect]
    removed: list[str] = []
    for folder in sorted(root.glob("mxc-*")) if root.is_dir() else []:
        if folder.name.lower() == current or is_link(folder) or not folder.is_dir():
            continue
        for arch_dir in sorted(folder.iterdir()):
            if is_link(arch_dir) or not arch_dir.is_dir() or not isinstance(read_json(arch_dir / MARKER), dict):
                continue  # not csa's
            resolved = arch_dir.resolve()
            if any(path == resolved or resolved in path.parents for path in keep):
                continue
            if not dry_run:
                try:
                    _remove_tree(arch_dir)
                except OSError as exc:
                    report.notes.append(f"Sandbox: couldn't remove the old MXC install {arch_dir}: {exc}")
                    continue
            removed.append(str(arch_dir))
        if not dry_run:
            try:
                folder.rmdir()  # only when empty
            except OSError:
                pass
    if removed:
        verb = "would remove" if dry_run else "removed"
        report.done.append(f"Sandbox: {verb} older MXC versions csa installed: {', '.join(removed)}")
    path = download_path()
    if path.is_file():
        size = path.stat().st_size
        if dry_run:
            report.done.append(f"Sandbox: would delete the downloaded {path} ({_mb(size)}) once MXC is installed")
        else:
            try:
                path.unlink()
                report.done.append(f"Sandbox: deleted the downloaded {path} ({_mb(size)}); MXC is installed.")
            except OSError as exc:
                report.notes.append(f"Sandbox: couldn't delete the downloaded {path}: {exc}")
    if not dry_run:
        try:
            download_dir().rmdir()
        except OSError:
            pass


# Probe -----------------------------------------------------------------------


def _probe(executable: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        return mxc.probe(mxc.launcher(executable)), None
    except MxcError as exc:
        return None, str(exc)


def _report_probe(executable: Path, report: Any) -> None:
    data, error = _probe(executable)
    if error is not None:
        report.failed.append(f"Sandbox: MXC is installed, but its probe failed: {error}")
        report.add_todo(_update_windows_todo())
        return
    assert data is not None
    problems = mxc.probe_problems(data, loopback=True)
    if problems:
        report.failed.append("Sandbox: MXC is installed, but can't run csa's sandbox on this system: " + "; ".join(problems) + ".")
        report.add_todo(_update_windows_todo())
        return
    report.notes.append(f"Sandbox: on. MXC's probe reports tier {data.get('tier')} with denied and enumerate-only "
                        "paths and host loopback; OpenCode edit workers' shell commands run inside it.")


# setup / update --------------------------------------------------------------


def ensure(settings: SandboxSettings, report: Any, dry_run: bool = False) -> None:
    """Install or check csa's MXC for `csa setup` and `csa update`; results go into `report` (setup.Report)."""

    dry_run = dry_run or bool(getattr(report, "dry_run", False))
    status, why = state(settings)
    if status == "off":
        report.notes.append(f"Sandbox: off, {why}. Workers run without csa's OS sandbox.")
        return
    if status == "unavailable":
        report.failed.append(f"Sandbox: [sandbox] enabled = true, but csa's sandbox can't run here: {why}.")
        report.add_todo(f"Remove `enabled = true` from [sandbox] in {config_path()}, or set it to false, "
                        "to run workers without csa's sandbox.")
        return
    if settings.path:
        _check_configured(Path(settings.path), report)
        return
    arch = mxc.arch()
    if arch is None:
        report.failed.append("Sandbox: MXC has no build for this processor.")
        report.add_todo(f"To run workers without csa's sandbox, {_off_hint()}.")
        return
    try:
        _install(report, dry_run, arch)
    except InstallError as exc:
        report.failed.append(f"Sandbox: MXC {mxc.VERSION} wasn't installed: {exc}.")
        report.add_todo(f"Run `csa setup` again to retry the sandbox install, or {_off_hint()}.")


def _check_configured(path: Path, report: Any) -> None:
    if not path.is_file():
        report.failed.append(f"Sandbox: the configured [sandbox] path {path} doesn't exist.")
        report.add_todo(f"Fix `path` under [sandbox] in {config_path()}, or remove it so csa installs MXC itself.")
        return
    ok, problems = _signature_check([path])
    if not ok:
        report.failed.append(f"Sandbox: the configured {path} isn't validly signed by Microsoft: " + "; ".join(problems))
        report.add_todo(f"Point `path` under [sandbox] in {config_path()} at a signed wxc-exec.exe, or remove it so csa "
                        "installs MXC itself.")
        return
    report.unchanged.append(f"Sandbox: uses the configured {path} (signature valid); csa doesn't install MXC itself.")
    _report_probe(path, report)


def _install(report: Any, dry_run: bool, arch: str) -> None:
    target = mxc.install_dir(mxc.VERSION)
    check = check_install(target)
    if check["ok"]:
        report.unchanged.append(f"Sandbox: MXC {mxc.VERSION} is installed at {target} "
                                f"({len(check['files'])} files; hashes and signatures verified).")
        _cleanup(report, dry_run, protect=[target])
        _report_probe(mxc.default_executable(mxc.VERSION), report)
        return
    action = "replace" if os.path.lexists(target) else "install"
    if dry_run:
        path = download_path()
        if path.is_file() and path.stat().st_size == mxc.ASSET_SIZE:
            source = f"reuse the earlier download {path} after checking its SHA-256"
        else:
            source = f"download {mxc.ASSET_URL} ({_mb(mxc.ASSET_SIZE)}) and check its size and SHA-256"
        why = f" ({check['reason']})" if action == "replace" else ""
        report.done.append(f"Sandbox: would {action} MXC {mxc.VERSION} at {target}{why}: {source}, extract the {arch} "
                           ".exe and .dll files and check their Microsoft signatures")
        _cleanup(report, True, protect=[target])
        return
    root = mxc.install_root()
    make_private_dir(root)
    lock = try_lock(root / LOCK)
    if lock is None:
        raise InstallError("another csa is installing the sandbox right now; try again when it has finished")
    try:
        _clear_leftovers()
        zip_path = _obtain(report)
        staging = root / f".staging-{mxc.VERSION}-{secrets.token_hex(4)}"
        staging.mkdir()
        try:
            print(f"Extracting the {arch} files ...", flush=True)
            files = extract(zip_path, staging, arch)
            print(f"Checking the signatures of {len(files)} files ...", flush=True)
            ok, problems = _signature_check([staging / entry["name"] for entry in files])
            if not ok:
                raise InstallError("not every file carries a valid Microsoft signature (" + "; ".join(problems[:5])
                                   + "); the old install was left in place")
            write_json(staging / MARKER, {
                "version": mxc.VERSION,
                "asset": mxc.ASSET_NAME,
                "asset_url": mxc.ASSET_URL,
                "asset_sha256": mxc.ASSET_SHA256,
                "arch": arch,
                "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "installed_by": f"cli-subagents {__version__}",
                "files": files,
            })
            _swap(staging, target)
        finally:
            if staging.exists():
                try:
                    _remove_tree(staging)
                except OSError:
                    pass
        verb = "replaced" if action == "replace" else "installed"
        why = f" (it was {check['reason']})" if action == "replace" else ""
        size = sum(entry["size"] for entry in files)
        report.done.append(f"Sandbox: {verb} MXC {mxc.VERSION} at {target}{why}: {len(files)} files, {_mb(size)}, "
                           "all signed by Microsoft.")
        _cleanup(report, False, protect=[target])
    finally:
        unlock(lock)
    _report_probe(mxc.default_executable(mxc.VERSION), report)


# doctor ----------------------------------------------------------------------


def status(settings: SandboxSettings) -> dict[str, Any]:
    """What `csa doctor` shows. Signatures and the probe are skipped when the sandbox is off."""

    current, why = state(settings)
    executable = Path(settings.path) if settings.path else mxc.default_executable(mxc.VERSION)
    info: dict[str, Any] = {
        "state": current, "reason": why, "version": mxc.VERSION, "executable": str(executable),
        "configured": bool(settings.path), "installed": executable.is_file(), "integrity": None,
        "signatures": None, "probe": None, "probe_error": None, "problems": [],
    }
    problems: list[str] = info["problems"]
    if current == "off":
        return info
    if current == "unavailable":
        problems.append(f"[sandbox] enabled = true, but csa's sandbox can't run here: {why}. Remove that line or set it to false")
        return info
    override = bool(os.environ.get(mxc.COMMAND_ENV))
    if info["installed"]:
        if settings.path:
            info["integrity"] = "configured path"
            ok, signature_problems = _signature_check([executable])
            info["signatures"] = {"ok": ok, "problems": signature_problems}
            if not ok:
                problems.append(f"{executable} isn't validly signed by Microsoft: " + "; ".join(signature_problems))
        else:
            check = check_install(mxc.install_dir(mxc.VERSION))
            info["integrity"] = check["reason"]
            info["files"] = len(check["files"])
            info["signatures"] = check["signatures"]
            if not check["ok"]:
                problems.append(f"csa can't verify its MXC install at {mxc.install_dir(mxc.VERSION)}: {check['reason']}. "
                                "Run `csa setup` to reinstall it")
    elif not override:
        if settings.path:
            problems.append(f"the configured [sandbox] path {executable} doesn't exist")
        else:
            problems.append(f"MXC {mxc.VERSION} isn't installed at {executable}. Run `csa setup` to install it")
        return info
    data, error = _probe(executable)
    if error is not None:
        info["probe_error"] = error
        problems.append(f"MXC's probe failed: {error}")
        return info
    assert data is not None
    probes = data.get("probes") if isinstance(data.get("probes"), dict) else {}
    info["probe"] = {
        "tier": data.get("tier"),
        "needsDaclAugmentation": data.get("needsDaclAugmentation"),
        "baseContainerSupportsDenyPaths": probes.get("baseContainerSupportsDenyPaths"),
        "baseContainerSupportsEnumeratePaths": probes.get("baseContainerSupportsEnumeratePaths"),
        "baseContainerSupportsIngressHostLoopbackAllow": probes.get("baseContainerSupportsIngressHostLoopbackAllow"),
    }
    problems.extend(mxc.probe_problems(data, loopback=True))
    return info


def _yes(value: Any) -> str:
    return "yes" if value else "no"


def describe(info: dict[str, Any]) -> list[str]:
    """The Sandbox block `csa doctor` prints."""

    reason = str(info.get("reason") or "")
    lines = [f"Sandbox: {info['state']}. {reason[:1].upper()}{reason[1:]}."]
    if info["state"] != "on":
        for problem in info["problems"]:
            lines.append(f"Problem: sandbox: {problem}.")
        return lines
    lines.append("  Backend: mxc. Integration: OpenCode shell.create.before (commands only; read jobs run none).")
    lines.append("  Other integrations: Claude Code PowerShell hook planned; Codex uses its own sandbox.")
    where = "configured path" if info.get("configured") else "installed by csa" if info.get("installed") else "not installed"
    detail = f"  MXC {info['version']}: {info['executable']} ({where}"
    if info.get("integrity") == "verified":
        detail += f"; {info.get('files', 0)} files verified"
    signatures = info.get("signatures")
    if signatures is not None and signatures.get("ok"):
        detail += "; signatures valid"
    lines.append(detail + ")")
    probe = info.get("probe")
    if probe:
        lines.append(f"  Probe: tier {probe.get('tier')}; denied paths {_yes(probe.get('baseContainerSupportsDenyPaths'))}; "
                     f"enumerate-only paths {_yes(probe.get('baseContainerSupportsEnumeratePaths'))}; "
                     f"DACL changes {_yes(probe.get('needsDaclAugmentation'))}")
        lines.append(f"  Host-loopback policy: {_yes(probe.get('baseContainerSupportsIngressHostLoopbackAllow'))}")
    for problem in info["problems"]:
        lines.append(f"Problem: sandbox: {problem}.")
    return lines


def opencode_line(info: dict[str, Any]) -> str:
    """The OpenCode line in `csa doctor --runtime opencode`."""

    if info["state"] == "on" and not info["problems"]:
        return "OpenCode shell commands run in csa's MXC sandbox; the worker, gate and reviewers stay on the host."
    if info["state"] == "on":
        return "OpenCode's command sandbox isn't working here yet (see Sandbox), so edit jobs won't start."
    if info["state"] == "unavailable":
        return "csa's sandbox is turned on but can't run here (see Sandbox), so OpenCode edit workers won't start."
    return (f"OpenCode read and edit workers use csa's review gate; csa's MXC sandbox is off here ({info['reason']}), "
            "so shell writes are not OS-confined.")
