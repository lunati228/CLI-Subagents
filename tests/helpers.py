import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

FAKE_CLAUDE = Path(__file__).with_name("fake_claude.py")


def stop_job_runners(home: Path, wait: float = 2.0) -> None:
    """Ask the jobs a test left running to stop, so its folder can be removed.

    Uses the runner's own cancel marker and kills only a worker whose PID still
    belongs to the same process (checked by start time), never a bare PID.
    """

    from cli_subagents.fsutil import kill_tree, read_json, same_process

    active = []
    for folder in (home / "jobs").glob("*"):
        status = read_json(folder / "status.json", {}) or {}
        if status.get("state") not in ("queued", "starting", "running"):
            continue
        active.append(folder)
        try:
            (folder / "cancel").write_text("test teardown", encoding="utf-8")
        except OSError:
            pass
        pid, started = status.get("child_pid"), status.get("child_started")
        try:
            if pid and started is not None and same_process(int(pid), int(started)):
                kill_tree(int(pid))
        except (OSError, TypeError, ValueError):
            pass
    deadline = time.monotonic() + wait
    while active and time.monotonic() < deadline:
        active = [f for f in active if (read_json(f / "status.json", {}) or {}).get("state") in ("queued", "starting", "running")]
        time.sleep(0.1)


class TempHome(unittest.TestCase):
    """Isolated CLI_SUBAGENTS_HOME, primary setup and fake claude binary."""

    def setUp(self):
        # A job runner that is still finishing can hold its log open for a moment on Windows.
        self._temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._temp.name).resolve()
        self.home = self.root / "csa"
        self.primary = self.root / "primary"
        self.project = self.root / "project"
        self.user_home = self.root / "user"
        for folder in (self.home, self.primary, self.project, self.user_home):
            folder.mkdir()
        self.record = self.root / "record.jsonl"
        self._env = patch.dict(
            os.environ,
            {
                "CLI_SUBAGENTS_HOME": str(self.home),
                "HOME": str(self.user_home),
                "USERPROFILE": str(self.user_home),
                "CLAUDE_CONFIG_DIR": str(self.root / "claude-config"),
                "CODEX_HOME": str(self.root / "codex-config"),
                "CLI_SUBAGENTS_CLAUDE_COMMAND": json.dumps([sys.executable, str(FAKE_CLAUDE)]),
                "FAKE_CLAUDE_RECORD": str(self.record),
                "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            },
        )
        self._env.start()
        for key in ("CLI_SUBAGENTS_DEPTH", "FAKE_CLAUDE_LIMITED", "FAKE_CLAUDE_SCENARIO"):
            os.environ.pop(key, None)

    def tearDown(self):
        stop_job_runners(self.home)
        self._env.stop()
        self._temp.cleanup()

    def write_config(self, accounts=("a1", "a2"), failover="on-limit", extra=""):
        lines = [
            "[claude]",
            f"primary = {json.dumps(str(self.primary))}",
            f'failover = "{failover}"',
            extra,
        ]
        for name in accounts:
            lines += ["[[claude.accounts]]", f'name = "{name}"', f"home = {json.dumps(str(self.root / 'homes' / name))}"]
        (self.home / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def records(self):
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text(encoding="utf-8").splitlines() if line.strip()]
