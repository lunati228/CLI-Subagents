"""The pinned Microsoft MXC release csa uses as its Windows process sandbox.

MXC (https://github.com/microsoft/mxc, MIT) ships `wxc-exec.exe`, which runs one
command inside a Windows process container described by a JSON policy:
`wxc-exec.exe policy.json`. This module knows the pinned release, where csa
installs it, whether this Windows build can run it, and how to ask the binary
what the host supports (`--probe`). It never enables MXC telemetry or asks for
telemetry consent: csa's policies carry no `telemetry` section.

Facts relied on here were read at tag v1.0.1; sources and dates are in
docs/research/sandbox-choice.md.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import subprocess
import sys
from typing import Any

from cli_subagents.config import home
from cli_subagents.fsutil import no_window_kwargs
from cli_subagents.sandbox.model import SandboxError

VERSION = "1.0.1"
TAG = f"v{VERSION}"
ASSET_NAME = "mxc-release-binaries.zip"
ASSET_URL = f"https://github.com/microsoft/mxc/releases/download/{TAG}/{ASSET_NAME}"
ASSET_SIZE = 542_051_775
ASSET_SHA256 = "0e832239454ba7fcd338ff56218a287eef4c0a01c1484084b869f55feff8abe9"
# Authenticode signer of every Windows binary in the release (checked 2026-10-26;
# issuer "Microsoft Code Signing PCA 2024").
SIGNER = "CN=Microsoft Corporation, O=Microsoft Corporation, L=Redmond, S=Washington, C=US"
EXECUTABLE = "wxc-exec.exe"
POLICY_VERSION = "1.0.0"  # the stable request contract of v1.0.x (docs/containment-policy/1.0.0)
REQUIRED_TIER = "base-container"
# Windows 11 releases and the first update with process isolation, from
# docs/backends/process-container/os-version-support.md at v1.0.1. Newer builds
# not in the table are tried; the probe decides.
MIN_UPDATE = {26100: 9278, 26200: 9278, 26300: 9550, 28000: 2804}
FIRST_BUILD = 26100
# Tests and debugging: a JSON argv list that stands in for wxc-exec.exe.
COMMAND_ENV = "CLI_SUBAGENTS_SANDBOX_COMMAND"
PROBE_TIMEOUT = 60


class MxcError(SandboxError):
    """MXC is missing, unusable on this host, or answered unexpectedly."""


def arch() -> str | None:
    """MXC's folder name for this machine's architecture, or None if MXC has no build for it."""

    machine = platform.machine().lower()
    if machine in ("amd64", "x86_64"):
        return "x64"
    if machine in ("arm64", "aarch64"):
        return "arm64"
    return None


def windows_version() -> tuple[int, int] | None:
    """(build, update build revision) of this Windows, or None elsewhere."""

    if os.name != "nt":
        return None
    build = sys.getwindowsversion().build
    revision = 0
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as key:
            revision = int(winreg.QueryValueEx(key, "UBR")[0])
    except (OSError, ValueError, TypeError):
        revision = 0
    return build, revision


def support() -> tuple[bool, str]:
    """Whether this system can run MXC process containers at all, and why (not)."""

    if os.environ.get(COMMAND_ENV):
        return True, f"test launcher from {COMMAND_ENV}"
    if os.name != "nt":
        return False, "csa's sandbox uses MXC process containers, which csa supports on Windows only for now"
    if arch() is None:
        return False, f"MXC has no build for this processor ({platform.machine()})"
    version = windows_version()
    assert version is not None
    build, revision = version
    if build < FIRST_BUILD:
        return False, (f"Windows build {build}.{revision} is older than Windows 11 24H2; MXC process containers "
                       "need 26100.9278 (24H2), 26200.9278 (25H2) or later")
    minimum = MIN_UPDATE.get(build)
    if minimum is not None and revision < minimum:
        return False, f"Windows build {build}.{revision} needs update {build}.{minimum} or later for MXC process containers"
    return True, f"Windows build {build}.{revision}"


def install_root() -> Path:
    """csa's sandbox folder. Workers never get write access to it."""

    return home() / "sandbox"


def install_dir(version: str = VERSION) -> Path:
    return install_root() / f"mxc-{version}" / (arch() or "x64")


def default_executable(version: str = VERSION) -> Path:
    return install_dir(version) / EXECUTABLE


def launcher(path: Path | None = None) -> list[str]:
    """The argv prefix that runs wxc-exec: the test override, the configured path, or csa's install."""

    override = os.environ.get(COMMAND_ENV)
    if override:
        try:
            value = json.loads(override)
        except ValueError:
            value = None
        if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
            raise MxcError(f"{COMMAND_ENV} must be a JSON list of strings")
        return value
    return [str(path or default_executable())]


def probe(command: list[str], timeout: float = PROBE_TIMEOUT) -> dict[str, Any]:
    """`wxc-exec --probe`: the host's isolation tier and supported features, as JSON."""

    if not os.environ.get(COMMAND_ENV) and not Path(command[0]).is_file():
        raise MxcError(f"MXC is not installed at {command[0]}")
    try:
        completed = subprocess.run(
            [*command, "--probe"], stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
            **no_window_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MxcError(f"could not run MXC's probe: {type(exc).__name__}: {exc}") from exc
    text = completed.stdout.decode("utf-8", errors="replace")
    if completed.returncode != 0:
        detail = (completed.stderr.decode("utf-8", errors="replace").strip() or text.strip())[:300]
        raise MxcError(f"MXC's probe failed (exit {completed.returncode}): {detail}")
    try:
        data = json.loads(text[text.index("{"):])
    except ValueError as exc:
        raise MxcError("MXC's probe printed no JSON") from exc
    if not isinstance(data, dict):
        raise MxcError("MXC's probe printed unexpected JSON")
    return data


def probe_problems(data: dict[str, Any], *, deny: bool = True, enumerate: bool = True,
                   loopback: bool = False) -> list[str]:
    """Why this probe result can't run csa's policies (empty when it can).

    csa requires the BaseContainer tier (PSEC), which enforces grants without
    changing any file's DACL, and the features its policies use.
    """

    problems = []
    tier = data.get("tier")
    if tier != REQUIRED_TIER:
        problems.append(f"MXC reports isolation tier '{tier}', csa requires '{REQUIRED_TIER}'")
    if data.get("needsDaclAugmentation"):
        problems.append("MXC would have to change file permissions (DACLs) on this host, which csa never allows")
    probes = data.get("probes") if isinstance(data.get("probes"), dict) else {}
    if deny and not probes.get("baseContainerSupportsDenyPaths"):
        problems.append("this Windows build's process container can't enforce denied paths")
    if enumerate and not probes.get("baseContainerSupportsEnumeratePaths"):
        problems.append("this Windows build's process container can't grant enumerate-only access, which "
                        "OpenCode and Node need to resolve paths")
    if loopback and not probes.get("baseContainerSupportsIngressHostLoopbackAllow"):
        problems.append("this Windows build's process container can't enforce host-loopback access for local test servers")
    return problems


_SIGNATURE_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$paths = [Console]::In.ReadToEnd() | ConvertFrom-Json
$result = @()
foreach ($path in $paths) {
  $signature = Get-AuthenticodeSignature -LiteralPath $path
  $subject = $null
  if ($signature.SignerCertificate) { $subject = $signature.SignerCertificate.Subject }
  $result += [pscustomobject]@{ path = $path; status = [string]$signature.Status; subject = $subject }
}
ConvertTo-Json -InputObject $result -Compress
"""


def signatures(paths: list[Path], timeout: float = 120) -> dict[str, dict[str, Any]]:
    """Authenticode status of each file: {path: {"status", "subject", "ok"}}.

    Uses Windows PowerShell's Get-AuthenticodeSignature (WinVerifyTrust), which
    ships with Windows. `ok` means a valid, trusted signature by Microsoft.
    """

    if os.name != "nt":
        raise MxcError("Authenticode signatures can only be checked on Windows")
    if not paths:
        return {}
    system_root = os.environ.get("SystemRoot") or os.environ.get("SYSTEMROOT") or r"C:\Windows"
    powershell = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    try:
        completed = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", _SIGNATURE_SCRIPT],
            input=json.dumps([str(path) for path in paths]).encode("utf-8"), capture_output=True, timeout=timeout,
            **no_window_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MxcError(f"could not check signatures: {type(exc).__name__}: {exc}") from exc
    if completed.returncode != 0:
        raise MxcError("could not check signatures: " + completed.stderr.decode("utf-8", errors="replace").strip()[:300])
    try:
        items = json.loads(completed.stdout.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise MxcError("could not read the signature check's output") from exc
    if isinstance(items, dict):
        items = [items]
    result: dict[str, dict[str, Any]] = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        status, subject = str(item.get("status") or ""), str(item.get("subject") or "")
        result[str(item.get("path"))] = {"status": status, "subject": subject,
                                         "ok": status == "Valid" and subject == SIGNER}
    for path in paths:
        result.setdefault(str(path), {"status": "Unknown", "subject": "", "ok": False})
    return result
