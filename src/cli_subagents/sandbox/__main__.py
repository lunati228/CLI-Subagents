"""Script entry point, including with Python's isolated mode."""

from pathlib import Path
import sys

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cli_subagents.sandbox.launcher import main

if __name__ == "__main__":
    raise SystemExit(main())
