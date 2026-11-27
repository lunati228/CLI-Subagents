#!/usr/bin/env python
"""MCP server for delegating Codex tasks to Google Antigravity CLI."""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import shutil
import threading
import time
import tomllib
import traceback
from typing import Any
import uuid

from mcp.server.fastmcp import FastMCP

from agy_adapter import run_agy_print
from capabilities import detect_capabilities
from model_catalog import load_model_catalog
from settings_guard import enforce_bridge_settings


BRIDGE_DIR = Path(__file__).resolve().parent
PROJECT_LOCAL_ROOT = BRIDGE_DIR.parent.parent
MODELS_PATH = BRIDGE_DIR / "models.json"
SETTINGS_PATH = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"
CODEX_CONFIG_PATH = Path.home() / ".codex" / "config.toml"
ENV_WORKSPACE_ROOT = "ANTIGRAVITY_BRIDGE_WORKSPACE_ROOT"
ENV_TRUSTED_ROOTS = "ANTIGRAVITY_BRIDGE_TRUSTED_ROOTS"
DEFAULT_TIMEOUT_SECONDS = 300
MAX_PROMPT_CHARS = 12000
PROMPT_WARNING_CHARS = 8000
MAX_RUN_RECORDS = 50
MAX_ASYNC_JOB_RECORDS = 50
DEFAULT_RETURN_MODE = "file_summary"
SUMMARY_MARKER = "SUMMARY:"
DETAILS_MARKER = "DETAILS:"
OPUS_MODEL = "claude-opus-4-6-thinking"
GEMINI_MODEL = "gemini-4-hydrogen-high"
GEMINI_EFFORT = "high"
ASYNC_JOBS: dict[str, dict[str, Any]] = {}
ASYNC_JOBS_LOCK = threading.Lock()

mcp = FastMCP(
    "antigravity-bridge",
    instructions=(
        "Delegate bounded tasks to Google Antigravity CLI. Use this for a second "
        "agent opinion, code review, implementation suggestions, and workspace-aware "
        "analysis. The server runs agy through ConPTY on Windows because direct "
        "agy --print subprocess pipes can return empty stdout."
    ),
)


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except json.JSONDecodeError:
        return default


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _env_paths(name: str) -> list[Path]:
    value = os.environ.get(name, "")
    paths: list[Path] = []
    for item in value.split(os.pathsep):
        item = item.strip()
        if not item:
            continue
        try:
            paths.append(Path(item).expanduser().resolve())
        except OSError:
            continue
    return paths


def _default_workspace_root() -> Path:
    configured = _env_paths(ENV_WORKSPACE_ROOT)
    if configured:
        return configured[0]
    return PROJECT_LOCAL_ROOT.resolve()


def _runs_root(workspace_root: Path) -> Path:
    local_app_data = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    project_key = hashlib.sha256(str(workspace_root.resolve()).encode("utf-8")).hexdigest()[:16]
    return local_app_data / "Codex" / "antigravity-bridge" / "runs" / project_key


def _resolve_path(value: str | None, default: Path) -> Path:
    if not value:
        return default.resolve()
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = default / path
    return path.resolve()


def _path_is_inside(path: Path, root: Path) -> bool:
    path = path.resolve()
    root = root.resolve()
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return path == root


def _trusted_roots() -> list[Path]:
    roots: list[Path] = []
    configured_default = _env_paths(ENV_WORKSPACE_ROOT)
    roots.extend(configured_default)
    roots.extend(_env_paths(ENV_TRUSTED_ROOTS))
    try:
        with CODEX_CONFIG_PATH.open("rb") as handle:
            codex_config = tomllib.load(handle)
        projects = codex_config.get("projects", {})
        if isinstance(projects, dict):
            for path_text, project_config in projects.items():
                if (
                    isinstance(path_text, str)
                    and isinstance(project_config, dict)
                    and project_config.get("trust_level") == "trusted"
                ):
                    roots.append(Path(path_text).expanduser().resolve())
    except (FileNotFoundError, OSError, tomllib.TOMLDecodeError):
        pass
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = os.path.normcase(str(root))
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def _require_trusted_workspace(path: Path, field_name: str) -> None:
    roots = _trusted_roots()
    if not roots or not any(_path_is_inside(path, root) for root in roots):
        raise ValueError(
            f"{field_name} must be inside a Codex project marked trusted or an "
            f"explicit {ENV_TRUSTED_ROOTS} root"
        )


def _validate_project_root(path: Path, field_name: str) -> None:
    if not path.exists() or not path.is_dir():
        raise ValueError(f"{field_name} must be an existing directory")
    if path == Path(path.anchor) or path == Path.home().resolve():
        raise ValueError(f"{field_name} cannot be a drive root or the user-profile root")
    if any(ord(char) < 32 for char in str(path)):
        raise ValueError(f"{field_name} contains control characters")
    windows_root = Path(os.environ.get("WINDIR", r"C:\Windows")).resolve()
    if _path_is_inside(path, windows_root):
        raise ValueError(f"{field_name} cannot be inside the Windows system directory")
    normalized = str(path).replace("\\", "/").lower()
    forbidden = (
        "/.ssh",
        "/.aws",
        "/.azure",
        "/.gcloud",
        "/.gnupg",
        "/.gemini",
        "/.codex",
        "/appdata/roaming/microsoft/credentials",
        "/appdata/local/microsoft/credentials",
        "/google/chrome/user data",
        "/microsoft/edge/user data",
        "/mozilla/firefox/profiles",
        "/1password",
        "/bitwarden",
    )
    if any(marker in normalized for marker in forbidden):
        raise ValueError(f"{field_name} cannot be a credential or agent-state directory")


def _load_model_catalog(cwd: Path | None = None, prefer_cli: bool = True) -> dict[str, Any]:
    resolved_cwd = cwd or _default_workspace_root()
    return load_model_catalog(
        models_path=MODELS_PATH,
        settings_path=SETTINGS_PATH,
        cwd=resolved_cwd,
        prefer_cli=prefer_cli,
    )


def _load_models(cwd: Path | None = None, prefer_cli: bool = True) -> list[dict[str, Any]]:
    return list(_load_model_catalog(cwd=cwd, prefer_cli=prefer_cli).get("models", []))


def _normalize_timeout_seconds(value: int | float | None) -> int:
    if value is None:
        return DEFAULT_TIMEOUT_SECONDS
    timeout = int(value)
    if timeout < 10:
        raise ValueError("timeout_seconds must be >= 10")
    if timeout > 1800:
        raise ValueError("timeout_seconds must be <= 1800")
    return timeout


def _agy_timeout(seconds: int) -> str:
    return f"{seconds}s"


def _prompt_for_mode(prompt: str, mode: str) -> str:
    if mode == "advise":
        return (
            "You are being called by Codex as a second agent. Return analysis only. "
            "Do not modify files, install packages, run destructive commands, or ask "
            "for interactive follow-up unless the task is impossible.\n\n"
            f"Task:\n{prompt}"
        )
    if mode == "workspace":
        return prompt
    raise ValueError("mode must be 'advise' or 'workspace'")


def _prompt_for_return_mode(prompt: str, return_mode: str, summary_max_bullets: int) -> str:
    if return_mode == "full":
        return prompt
    if return_mode == "file_summary":
        return (
            f"{prompt}\n\n"
            "Output contract for Codex bridge:\n"
            f"Start with `{SUMMARY_MARKER}` and provide no more than "
            f"{summary_max_bullets} compact bullets for the user.\n"
            f"Then write `{DETAILS_MARKER}` and put the full detailed answer there.\n"
            "Do not put full file contents in either section unless explicitly required."
        )
    raise ValueError("return_mode must be 'full' or 'file_summary'")


def _extract_summary(output: str) -> str:
    normalized = output.strip()
    upper = normalized.upper()
    summary_idx = upper.find(SUMMARY_MARKER)
    details_idx = upper.find(DETAILS_MARKER)
    if summary_idx >= 0:
        start = summary_idx + len(SUMMARY_MARKER)
        end = details_idx if details_idx > start else len(normalized)
        summary = normalized[start:end].strip()
        if summary:
            return summary
    if details_idx > 0:
        summary = normalized[:details_idx].strip()
        if summary:
            return summary
    lines = [line.rstrip() for line in normalized.splitlines() if line.strip()]
    return "\n".join(lines[:8]).strip()


def _agy_output_failure(output: str) -> str | None:
    normalized = output.strip().casefold()
    if normalized.startswith("jetski: no output produced"):
        return "Antigravity produced a headless execution failure instead of an answer."
    for marker, reason in (
        ("baseline model quota reached", "Antigravity baseline model quota was reached."),
        ("resource exhausted", "Antigravity reported exhausted model capacity or quota."),
        ("authentication required", "Antigravity authentication is required."),
    ):
        if marker in normalized:
            return reason
    return None


def _make_run_dir(runs_root: Path) -> tuple[str, Path]:
    run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_id, run_dir


def _prune_old_runs(runs_root: Path, retain: int = MAX_RUN_RECORDS) -> None:
    resolved_root = runs_root.resolve()
    try:
        runs = [
            path
            for path in runs_root.iterdir()
            if path.is_dir()
            and not path.is_symlink()
            and path.resolve().parent == resolved_root
        ]
    except FileNotFoundError:
        return
    runs.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    for stale in runs[retain:]:
        if stale.resolve().parent == resolved_root:
            shutil.rmtree(stale, ignore_errors=True)


def _prompt_size_warnings(prompt: str) -> list[str]:
    prompt_chars = len(prompt)
    if prompt_chars > MAX_PROMPT_CHARS:
        raise ValueError(
            f"Prompt is too large for Antigravity delegation: {prompt_chars} chars. "
            f"Limit is {MAX_PROMPT_CHARS}. Pass file paths and focused questions instead."
        )
    if prompt_chars > PROMPT_WARNING_CHARS:
        return [
            f"Prompt is large ({prompt_chars} chars); prefer paths and focused questions."
        ]
    return []


def _run_antigravity(
    *,
    prompt: str,
    cwd: str | None,
    model: str | None,
    effort: str | None,
    timeout_seconds: int | float | None,
    mode: str,
    add_dirs: list[str] | None,
    return_mode: str,
    summary_max_bullets: int,
) -> dict[str, Any]:
    timeout = _normalize_timeout_seconds(timeout_seconds)
    if not cwd:
        raise ValueError("cwd is required and must be the current Codex project root")
    if not Path(cwd).expanduser().is_absolute():
        raise ValueError("cwd must be an absolute path to the current Codex project root")
    resolved_cwd = _resolve_path(cwd, _default_workspace_root())
    _validate_project_root(resolved_cwd, "cwd")
    _require_trusted_workspace(resolved_cwd, "cwd")
    settings_changes = enforce_bridge_settings(SETTINGS_PATH, resolved_cwd)
    runs_root = _runs_root(resolved_cwd)

    resolved_add_dirs = [_resolve_path(item, resolved_cwd) for item in (add_dirs or [])]
    for item in resolved_add_dirs:
        if not _path_is_inside(item, resolved_cwd):
            raise ValueError("add_dirs items must remain inside the selected project root")

    final_prompt = _prompt_for_mode(
        _prompt_for_return_mode(prompt, return_mode, summary_max_bullets),
        mode,
    )
    prompt_chars = len(final_prompt)
    try:
        warnings: list[str] = _prompt_size_warnings(final_prompt)
        if settings_changes["overages_changed"]:
            warnings.append("Antigravity AI-credit overages were reset to Never.")
        if settings_changes["workspace_added"]:
            warnings.append("The exact Codex project root was added to Antigravity trust.")
        if settings_changes["workspace_write_added"]:
            warnings.append("Native Antigravity write access was added for the exact project root.")
    except ValueError as exc:
        run_id, run_dir = _make_run_dir(runs_root)
        preview = final_prompt[:MAX_PROMPT_CHARS] + "\n\n[truncated by prompt guardrail]"
        _write_text(run_dir / "prompt_preview.md", preview)
        meta = {
            "ok": False,
            "run_id": run_id,
            "model": model,
            "effort": effort,
            "cwd": str(resolved_cwd),
            "add_dirs": [str(item) for item in resolved_add_dirs],
            "mode": mode,
            "return_mode": return_mode,
            "timeout_seconds": timeout,
            "prompt_chars": prompt_chars,
            "max_prompt_chars": MAX_PROMPT_CHARS,
            "elapsed_ms": 0,
            "exit_status": None,
            "warnings": [str(exc)],
        }
        _write_text(run_dir / "meta.json", json.dumps(meta, indent=2, ensure_ascii=False))
        _prune_old_runs(runs_root)
        return {**meta, "output": "", "run_dir": str(run_dir)}

    run_id, run_dir = _make_run_dir(runs_root)
    started = time.monotonic()
    _write_text(run_dir / "prompt.md", final_prompt)

    capabilities = detect_capabilities(resolved_cwd)
    model_catalog = _load_model_catalog(cwd=resolved_cwd, prefer_cli=True)
    known_models = {item.get("label") for item in model_catalog.get("models", [])}
    warnings.extend(model_catalog.get("warnings", []))
    if model and model not in known_models:
        warnings.append(
            "Model label is not in `agy models`/models.json; passing it anyway as "
            "an exact Antigravity model label."
        )
    if not resolved_add_dirs:
        warnings.append("No add_dirs provided; agy may use cwd but workspace binding is less explicit.")

    exitstatus: int | None = None
    raw_output = ""
    backend = capabilities.get("backend_recommendation")
    used_cli_model_flag = False
    used_settings_model_fallback = False
    args_shape: list[str] = []
    try:
        run_result = run_agy_print(
            prompt=final_prompt,
            cwd=resolved_cwd,
            model=model,
            effort=effort,
            timeout=_agy_timeout(timeout),
            wall_timeout_seconds=timeout + 30,
            add_dirs=resolved_add_dirs,
            audit_dir=run_dir / "reviews",
            capabilities=capabilities,
        )
        exitstatus = run_result["exit_status"]
        raw_output = run_result["raw_output"]
        output = run_result["output"]
        backend = run_result["backend"]
        if run_result.get("backend_warning"):
            warnings.append(str(run_result["backend_warning"]))
        used_cli_model_flag = bool(run_result["used_cli_model_flag"])
        used_settings_model_fallback = bool(run_result["used_settings_model_fallback"])
        args_shape = list(run_result["args_shape"])
        output_failure = _agy_output_failure(output)
        ok = exitstatus in (0, None) and bool(output) and output_failure is None
        if output_failure:
            warnings.append(output_failure)
        if exitstatus in (0, None) and not output:
            warnings.append("Antigravity returned no text output.")
    except Exception as exc:  # pragma: no cover - defensive for MCP caller diagnostics.
        output = ""
        ok = False
        warnings.append(f"{type(exc).__name__}: {exc}")
        _write_text(run_dir / "traceback.txt", traceback.format_exc())

    elapsed_ms = int((time.monotonic() - started) * 1000)
    meta = {
        "ok": ok,
        "run_id": run_id,
        "model": model,
        "effort": effort,
        "cwd": str(resolved_cwd),
        "add_dirs": [str(item) for item in resolved_add_dirs],
        "mode": mode,
        "return_mode": return_mode,
        "timeout_seconds": timeout,
        "prompt_chars": prompt_chars,
        "max_prompt_chars": MAX_PROMPT_CHARS,
        "elapsed_ms": elapsed_ms,
        "exit_status": exitstatus,
        "backend": backend,
        "used_cli_model_flag": used_cli_model_flag,
        "used_settings_model_fallback": used_settings_model_fallback,
        "args_shape": args_shape,
        "warnings": warnings,
    }
    _write_text(run_dir / "result.md", output)
    _write_text(run_dir / "raw_output.txt", raw_output)
    summary = _extract_summary(output) if output else ""
    if summary:
        _write_text(run_dir / "summary.md", summary)
    _write_text(run_dir / "meta.json", json.dumps(meta, indent=2, ensure_ascii=False))
    _prune_old_runs(runs_root)

    response = {
        **meta,
        "run_dir": str(run_dir),
        "result_path": str(run_dir / "result.md"),
        "summary_path": str(run_dir / "summary.md") if summary else None,
        "output_truncated": return_mode == "file_summary",
    }
    if return_mode == "file_summary":
        return {**response, "summary": summary, "output": summary}
    return {**response, "summary": summary, "output": output}


def _make_job_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]


def _redact_result_output(result: dict[str, Any], include_output: bool) -> dict[str, Any]:
    redacted = dict(result)
    if not include_output:
        redacted.pop("output", None)
        if redacted.get("return_mode") == "full":
            redacted.pop("summary", None)
    return redacted


def _get_job(job_id: str) -> dict[str, Any]:
    with ASYNC_JOBS_LOCK:
        job = ASYNC_JOBS.get(job_id)
        if not job:
            raise ValueError(f"Unknown Antigravity job_id: {job_id}")
        return dict(job)


def _prune_async_jobs_locked(retain: int = MAX_ASYNC_JOB_RECORDS) -> None:
    finished = [
        (job_id, job)
        for job_id, job in ASYNC_JOBS.items()
        if job.get("status") != "running"
    ]
    finished.sort(
        key=lambda item: float(item[1].get("finished_at") or item[1].get("started_at") or 0),
        reverse=True,
    )
    for job_id, _job in finished[retain:]:
        ASYNC_JOBS.pop(job_id, None)


@mcp.tool()
def antigravity_delegate_opus(
    prompt: str,
    cwd: str,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    mode: str = "advise",
    add_dirs: list[str] | None = None,
    return_mode: str = DEFAULT_RETURN_MODE,
    summary_max_bullets: int = 5,
) -> dict[str, Any]:
    """Use Claude Opus 4.6 Thinking for difficult, high-value delegated work."""

    return _run_antigravity(
        prompt=prompt,
        cwd=cwd,
        model=OPUS_MODEL,
        effort=None,
        timeout_seconds=timeout_seconds,
        mode=mode,
        add_dirs=add_dirs,
        return_mode=return_mode,
        summary_max_bullets=summary_max_bullets,
    )


@mcp.tool()
def antigravity_delegate_gemini(
    prompt: str,
    cwd: str,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    mode: str = "advise",
    add_dirs: list[str] | None = None,
    return_mode: str = DEFAULT_RETURN_MODE,
    summary_max_bullets: int = 5,
) -> dict[str, Any]:
    """Use Gemini 4 Hydrogen High at high effort for lighter delegated work."""

    return _run_antigravity(
        prompt=prompt,
        cwd=cwd,
        model=GEMINI_MODEL,
        effort=GEMINI_EFFORT,
        timeout_seconds=timeout_seconds,
        mode=mode,
        add_dirs=add_dirs,
        return_mode=return_mode,
        summary_max_bullets=summary_max_bullets,
    )


@mcp.tool()
def antigravity_delegate_model(
    prompt: str,
    model: str,
    cwd: str,
    effort: str | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    mode: str = "advise",
    add_dirs: list[str] | None = None,
    return_mode: str = DEFAULT_RETURN_MODE,
    summary_max_bullets: int = 5,
) -> dict[str, Any]:
    """Use an exact alternate AGY model only when the user explicitly requests it."""

    return _run_antigravity(
        prompt=prompt,
        cwd=cwd,
        model=model,
        effort=effort,
        timeout_seconds=timeout_seconds,
        mode=mode,
        add_dirs=add_dirs,
        return_mode=return_mode,
        summary_max_bullets=summary_max_bullets,
    )


def _start_async_job(
    *,
    prompt: str,
    model: str,
    effort: str | None,
    cwd: str | None,
    timeout_seconds: int,
    mode: str,
    add_dirs: list[str] | None,
    return_mode: str,
    summary_max_bullets: int,
) -> dict[str, Any]:
    """Start a bounded delegation in a background thread and return job_id."""

    job_id = _make_job_id()
    started_at = time.time()
    with ASYNC_JOBS_LOCK:
        _prune_async_jobs_locked()
        ASYNC_JOBS[job_id] = {
            "job_id": job_id,
            "status": "running",
            "started_at": started_at,
            "finished_at": None,
            "result": None,
            "error": None,
        }

    def worker() -> None:
        try:
            result = _run_antigravity(
                prompt=prompt,
                cwd=cwd,
                model=model,
                effort=effort,
                timeout_seconds=timeout_seconds,
                mode=mode,
                add_dirs=add_dirs,
                return_mode=return_mode,
                summary_max_bullets=summary_max_bullets,
            )
            status = "completed" if result.get("ok") else "failed"
            error = None
        except Exception as exc:  # pragma: no cover - defensive async diagnostics.
            result = None
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"

        with ASYNC_JOBS_LOCK:
            ASYNC_JOBS[job_id].update(
                {
                    "status": status,
                    "finished_at": time.time(),
                    "result": result,
                    "error": error,
                }
            )
            _prune_async_jobs_locked()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return {
        "job_id": job_id,
        "status": "running",
        "started_at": started_at,
        "return_mode": return_mode,
        "message": "Poll antigravity_run_status with this job_id.",
    }


@mcp.tool()
def antigravity_delegate_opus_async(
    prompt: str,
    cwd: str,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    mode: str = "advise",
    add_dirs: list[str] | None = None,
    return_mode: str = DEFAULT_RETURN_MODE,
    summary_max_bullets: int = 5,
) -> dict[str, Any]:
    """Start Claude Opus 4.6 Thinking in the background for difficult work."""

    return _start_async_job(
        prompt=prompt,
        model=OPUS_MODEL,
        effort=None,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        mode=mode,
        add_dirs=add_dirs,
        return_mode=return_mode,
        summary_max_bullets=summary_max_bullets,
    )


@mcp.tool()
def antigravity_delegate_gemini_async(
    prompt: str,
    cwd: str,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    mode: str = "advise",
    add_dirs: list[str] | None = None,
    return_mode: str = DEFAULT_RETURN_MODE,
    summary_max_bullets: int = 5,
) -> dict[str, Any]:
    """Start Gemini 4 Hydrogen High at high effort in the background for lighter work."""

    return _start_async_job(
        prompt=prompt,
        model=GEMINI_MODEL,
        effort=GEMINI_EFFORT,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        mode=mode,
        add_dirs=add_dirs,
        return_mode=return_mode,
        summary_max_bullets=summary_max_bullets,
    )


@mcp.tool()
def antigravity_run_status(job_id: str, include_output: bool = False) -> dict[str, Any]:
    """Return status for an async Antigravity delegation job."""

    job = _get_job(job_id)
    result = job.get("result")
    if isinstance(result, dict):
        job["result"] = _redact_result_output(result, include_output)
    return job


@mcp.tool()
def antigravity_get_summary(job_id: str) -> dict[str, Any]:
    """Return the summary for a completed async Antigravity job."""

    job = _get_job(job_id)
    result = job.get("result")
    if not isinstance(result, dict):
        return {"job_id": job_id, "status": job.get("status"), "summary": None}
    return {
        "job_id": job_id,
        "status": job.get("status"),
        "summary": result.get("summary"),
        "summary_path": result.get("summary_path"),
        "run_id": result.get("run_id"),
    }


@mcp.tool()
def antigravity_get_result_path(job_id: str) -> dict[str, Any]:
    """Return the persisted result path for a completed async Antigravity job."""

    job = _get_job(job_id)
    result = job.get("result")
    if not isinstance(result, dict):
        return {"job_id": job_id, "status": job.get("status"), "result_path": None}
    return {
        "job_id": job_id,
        "status": job.get("status"),
        "run_id": result.get("run_id"),
        "run_dir": result.get("run_dir"),
        "result_path": result.get("result_path"),
        "summary_path": result.get("summary_path"),
    }


@mcp.tool()
def antigravity_smoke_test(cwd: str, worker: str = "gemini") -> dict[str, Any]:
    """Verify that Antigravity CLI responds through the PTY bridge."""

    resolved_cwd = _resolve_path(cwd, _default_workspace_root())
    if worker == "gemini":
        model, effort = GEMINI_MODEL, GEMINI_EFFORT
    elif worker == "opus":
        model, effort = OPUS_MODEL, None
    else:
        raise ValueError("worker must be 'gemini' or 'opus'")
    return _run_antigravity(
        prompt="Reply with exactly: MCP_OK",
        cwd=str(resolved_cwd),
        model=model,
        effort=effort,
        timeout_seconds=90,
        mode="advise",
        add_dirs=[str(resolved_cwd)],
        return_mode="file_summary",
        summary_max_bullets=3,
    )


@mcp.tool()
def antigravity_capabilities(cwd: str, include_live_probe: bool = False) -> dict[str, Any]:
    """Report detected Antigravity CLI flags, subcommands, and backend recommendation."""

    resolved_cwd = _resolve_path(cwd, _default_workspace_root())
    _validate_project_root(resolved_cwd, "cwd")
    _require_trusted_workspace(resolved_cwd, "cwd")
    return detect_capabilities(resolved_cwd, include_live_probe=include_live_probe)


@mcp.tool()
def antigravity_current_settings(cwd: str) -> dict[str, Any]:
    """Return non-secret Antigravity CLI settings and trusted workspace roots."""

    settings = _read_json(SETTINGS_PATH, {})
    resolved_cwd = _resolve_path(cwd, _default_workspace_root())
    _validate_project_root(resolved_cwd, "cwd")
    _require_trusted_workspace(resolved_cwd, "cwd")
    runs_root = _runs_root(resolved_cwd)
    return {
        "settings_path": str(SETTINGS_PATH),
        "model": settings.get("model"),
        "trusted_workspaces": [str(path) for path in _trusted_roots()],
        "bridge_dir": str(BRIDGE_DIR),
        "project_local_root": str(PROJECT_LOCAL_ROOT.resolve()),
        "default_workspace_root": str(_default_workspace_root()),
        "workspace_root": str(resolved_cwd),
        "runs_root": str(runs_root),
        "env_workspace_root": ENV_WORKSPACE_ROOT,
        "env_trusted_roots": ENV_TRUSTED_ROOTS,
        "max_prompt_chars": MAX_PROMPT_CHARS,
        "max_run_records": MAX_RUN_RECORDS,
        "max_async_job_records": MAX_ASYNC_JOB_RECORDS,
        "default_return_mode": DEFAULT_RETURN_MODE,
        "default_opus_model": OPUS_MODEL,
        "default_gemini_model": GEMINI_MODEL,
        "default_gemini_effort": GEMINI_EFFORT,
        "async_jobs_count": len(ASYNC_JOBS),
    }


@mcp.tool()
def antigravity_list_models(cwd: str) -> dict[str, Any]:
    """List Antigravity model labels from `agy models`, with local fallback."""

    resolved_cwd = _resolve_path(cwd, _default_workspace_root())
    _validate_project_root(resolved_cwd, "cwd")
    _require_trusted_workspace(resolved_cwd, "cwd")
    return _load_model_catalog(cwd=resolved_cwd, prefer_cli=True)


if __name__ == "__main__":
    mcp.run("stdio")
