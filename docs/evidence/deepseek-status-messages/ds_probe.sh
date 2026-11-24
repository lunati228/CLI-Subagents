#!/usr/bin/env bash
# Run free DeepSeek directly in OpenCode (no csa, no Claude relay) on one read-only task.
# Usage: REPO=<checkout to read> OUT=<folder for the runs> ds_probe.sh <label> <runs> [extra-note-line-file]
# START=<n> resumes numbering at run n. The runs used a scratch worktree of this repository.
set -u
LABEL="$1"; RUNS="$2"; EXTRA_FILE="${3:-}"
OUT="${OUT:?set OUT to a folder for the run logs}"
mkdir -p "$OUT"
cd "${REPO:?set REPO to the checkout the task reads}" || exit 1
NOTE="[You are running as a delegated subagent for another agent session, which will read your final message. Work only on this task, stay inside its scope, and end with a short summary of what you did, the result, and anything left open."
if [ -n "$EXTRA_FILE" ]; then NOTE="$NOTE $(cat "$EXTRA_FILE")"; fi
NOTE="$NOTE]"
TASK="Read-only task: do not edit any file. In this repository, find out how an account's cooldown works after it hits a usage limit: where it is set (src/cli_subagents/accounts.py and its callers), how long it lasts, and which config.toml keys change it. Check at least the code that sets it and the code that reads it. End with a 5-line answer."
for i in $(seq ${START:-1} "$RUNS"); do
  f="$OUT/$LABEL-$i.jsonl"
  start=$(date +%s)
  OPENCODE_DISABLE_PROJECT_CONFIG=1 opencode run --format json --thinking --archived --agent plan -m "opencode/deepseek-v4.1-flash-free#max" "$NOTE

$TASK" > "$f" 2> "$OUT/$LABEL-$i.err"
  echo "$LABEL-$i exit=$? secs=$(( $(date +%s) - start )) bytes=$(wc -c < "$f")"
done
