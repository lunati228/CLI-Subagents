"""agy_review: an automatic reviewer for unattended Antigravity CLI (`agy`) runs.

Headless agy can't ask anyone for permission, so actions that need a yes are
refused and the run often ends with an empty answer. This package is a
PreToolUse hook that answers instead: fixed rules approve the routine actions and
deny the forbidden ones, and a chain of tool-less model calls (Claude, agy or
Codex, the next only when one gives no verdict) judges the rest against a
written policy. It fails closed.

The package has no dependencies outside the standard library and no imports from
the rest of CLI Subagents, so it can be published on its own later.

    context.py   the per-job context file (what the job may touch)
    rules.py     fixed rules per tool
    paths.py     path rules (workspace, secrets, write protection)
    commands.py  shell command rules (read-only parser, hard denials)
    reviewer.py  the reviewer call and its strict JSON answer
    store.py     decision cache and decision log
    hook.py      the hook itself (stdin -> decision on stdout)
    install.py   hooks.json and settings.json helpers, the hook command line
    policy.md    the reviewer's policy
"""

__version__ = "0.1.0"

from .context import write_context  # noqa: E402,F401
from .install import HOOK_SET, entry_script, gated_settings, hook_command, merged_hooks  # noqa: E402,F401
from .reviewer import CONTEXT_ENV, ROLE_ENV  # noqa: E402,F401
