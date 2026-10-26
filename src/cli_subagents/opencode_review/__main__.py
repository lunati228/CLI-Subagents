"""Absolute-path entry point for the generated OpenCode plugin's review call."""

from pathlib import Path
import json
import sys

# OpenCode executes this file by absolute path, independent of the job's cwd and
# PYTHONPATH. Put the installed package ahead of the project's import paths.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cli_subagents.opencode_review import run  # noqa: E402
from cli_subagents.agy_review.hook import MAX_INPUT  # noqa: E402


def main() -> int:
    try:
        answer = run(sys.stdin.buffer.read(MAX_INPUT + 1), Path(sys.argv[1]))
    except Exception:
        answer = {"decision": "deny", "reason": "CLI Subagents review failed closed"}
    sys.stdout.write(json.dumps(answer))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
