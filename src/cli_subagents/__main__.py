from pathlib import Path
import sys

# Absolute-path host helpers use -I so a workspace package or PYTHONPATH cannot
# shadow either stdlib imports or the installed cli_subagents package.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cli_subagents.cli import main

raise SystemExit(main())
