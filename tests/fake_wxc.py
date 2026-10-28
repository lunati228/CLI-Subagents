"""Fake MXC launcher (wxc-exec.exe); never starts a real sandbox.

`fake_wxc.py --probe` prints a probe result. `fake_wxc.py POLICY.json` records
the policy, then runs `process.commandLine` itself (split with the Windows
CommandLineToArgvW rules) in `process.cwd`, with `process.env` layered over its
own environment, passing stdin, stdout, stderr and the exit code through.

FAKE_WXC_RECORD: JSONL file that gets one {"argv", "policy"} record per run.
FAKE_WXC_PROBE: JSON that replaces the default probe result.
FAKE_WXC_PROBE_EXIT: exit code for --probe (default 0).
FAKE_WXC_FAIL: print an MXC error envelope with this message and exit 1 without running anything.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

GOOD_PROBE = {
    "tier": "base-container",
    "needsDaclAugmentation": False,
    "warnings": [],
    "probes": {"baseContainerApiPresent": True, "baseContainerSupportsDenyPaths": True,
               "baseContainerSupportsEnumeratePaths": True, "baseContainerSupportsIngressHostLoopbackAllow": True},
}


def split_windows(text):
    """CommandLineToArgvW-compatible splitting (the rules list2cmdline quotes for)."""

    args, buffer, quoted, have, index = [], [], False, False, 0
    while index < len(text):
        char = text[index]
        if char == "\\":
            end = index
            while end < len(text) and text[end] == "\\":
                end += 1
            count = end - index
            if end < len(text) and text[end] == '"':
                buffer.append("\\" * (count // 2))
                if count % 2:
                    buffer.append('"')
                    end += 1
                index, have = end, True
                continue
            buffer.append("\\" * count)
            index, have = end, True
            continue
        if char == '"':
            if quoted and index + 1 < len(text) and text[index + 1] == '"':
                buffer.append('"')
                index += 2
                continue
            quoted, have = not quoted, True
            index += 1
            continue
        if char in " \t" and not quoted:
            if have:
                args.append("".join(buffer))
                buffer, have = [], False
            index += 1
            continue
        buffer.append(char)
        have = True
        index += 1
    if have:
        args.append("".join(buffer))
    return args


def main():
    argv = sys.argv[1:]
    if argv == ["--probe"]:
        print(os.environ.get("FAKE_WXC_PROBE") or json.dumps(GOOD_PROBE, indent=2))
        return int(os.environ.get("FAKE_WXC_PROBE_EXIT", "0"))
    if len(argv) != 1:
        sys.stderr.write("fake wxc-exec takes --probe or one policy file\n")
        return 64
    policy = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    record = os.environ.get("FAKE_WXC_RECORD")
    if record:
        with open(record, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"argv": sys.argv, "policy": policy}) + "\n")
    if os.environ.get("FAKE_WXC_FAIL"):
        message = os.environ["FAKE_WXC_FAIL"]
        sys.stderr.write(message + json.dumps({"error": {"code": "backend_error", "message": message}}) + "\n")
        return 1
    process = policy["process"]
    env = dict(os.environ) if process.get("inheritDefaultEnv") else {}
    for entry in process.get("env") or []:
        key, _, value = entry.partition("=")
        env[key] = value
    command = split_windows(process["commandLine"])
    with tempfile.TemporaryDirectory() as private:
        for name in ("TEMP", "TMP", "LOCALAPPDATA"):
            env[name] = private
        completed = subprocess.run(command, cwd=process.get("cwd"), env=env,
                                   stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr)
        return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
