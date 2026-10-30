"""Shared PowerShell command encoding for host integrations; input is data."""

from __future__ import annotations

import base64
import os
from pathlib import Path
import subprocess

from cli_subagents.sandbox.model import Command, Network, PolicyError

# Keep toolchain/build settings, not the agent's login or provider credentials.
ENV_NAMES = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "PROGRAMFILES", "PROGRAMFILES(X86)",
             "PROGRAMDATA", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "LANG", "LC_ALL", "CI",
             "PYTHONPATH", "PYTHONHOME", "PYTHONIOENCODING", "PYTHONUTF8", "VIRTUAL_ENV", "NODE_OPTIONS",
             "DOTNET_ROOT", "JAVA_HOME", "GOPATH", "GOROOT", "RUSTUP_HOME", "CARGO_HOME", "NUGET_PACKAGES",
             "VCINSTALLDIR", "VSINSTALLDIR", "PSMODULEPATH", "VCPKG_ROOT", "CONDA_PREFIX", "PYENV_ROOT"}


def powershell() -> str:
    # Pick the installed OS executable by absolute path, never a planted PATH tool.
    root = os.environ.get("SystemRoot") or os.environ.get("SYSTEMROOT") or r"C:\Windows"
    return str(Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")


def literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def powershell_command(line: str, cwd: str, env: dict[str, str], network: Network, timeout: float) -> Command:
    preamble = (
        "$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue';"
        "$csaToolHome=Join-Path $env:TEMP 'csa-tools';"
        "[IO.Directory]::CreateDirectory($csaToolHome)|Out-Null;"
        "$env:HOME=$csaToolHome;$env:USERPROFILE=$csaToolHome;$env:DOTNET_CLI_HOME=$csaToolHome;"
        "$env:APPDATA=Join-Path $csaToolHome 'appdata';"
        "$env:NUGET_USER_SETTINGS_DIRECTORY=Join-Path $csaToolHome 'nuget';"
        "$env:npm_config_cache=Join-Path $csaToolHome 'npm';"
        "$env:PIP_CACHE_DIR=Join-Path $csaToolHome 'pip';"
        "$ErrorActionPreference='Continue';$global:LASTEXITCODE=0;\n& {\n"
    )
    script = preamble + line + "\n};$csaOk=$?;if($LASTEXITCODE -ne 0){exit $LASTEXITCODE};if(!$csaOk){exit 1}\n"
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    command = Command([powershell(), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                      cwd, env, network, timeout)
    if os.name == "nt" and len(subprocess.list2cmdline(command.argv).encode("utf-16-le")) // 2 >= 32767:
        raise PolicyError("shell command exceeds the Windows command-line limit after PowerShell encoding; "
                          "split it into shorter commands or run a workspace script")
    return command
