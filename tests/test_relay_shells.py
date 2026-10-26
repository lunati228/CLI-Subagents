"""Execute the relay transports against a capture stub, never a real csa job."""

import base64
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent
DELIMITER = "CSA_RELAY_PAYLOAD_8c85aaf97de2456c9a544d2c09b16eb1"
COMMAND = "csa run --follow --window 60 --runtime codex --relay -"
PAYLOAD = "override=user's \"words\" $HOME $(echo nope) `echo nope` C:\\Users\\O'Brien\\ café 日本語 😀\n\nRead this.\n| already prefixed\n'@\n’@\n"


class RelayShellTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.capture = Path(self.temp.name) / "capture.py"
        # Like csa, drop the byte order mark some PowerShell pipes prepend.
        self.capture.write_text(
            "import json, sys\n"
            "print(json.dumps({'argv': sys.argv[1:], 'stdin': sys.stdin.read().removeprefix('\\ufeff')}, ensure_ascii=True))\n",
            encoding="utf-8",
        )

    def shell(self, shell, source):
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "MSYS_NO_PATHCONV": "1"}
        if shell == "bash":
            binary = shutil.which("bash")
            if not binary:
                self.skipTest("Bash is not installed")
            stub = f'csa() {{ {shlex.quote(Path(sys.executable).as_posix())} {shlex.quote(self.capture.as_posix())} "$@"; }}\n'
            argv = [binary, "--noprofile", "--norc", "-c", stub + source]
        else:
            binary = shutil.which("pwsh") or shutil.which("powershell")
            if not binary:
                self.skipTest("PowerShell is not installed")
            quote = lambda value: "'" + str(value).replace("'", "''") + "'"
            stub = ("$OutputEncoding = [System.Text.UTF8Encoding]::new($false)\n"
                    "[Console]::OutputEncoding = $OutputEncoding\n"
                    "$env:PYTHONIOENCODING = 'utf-8'\n"
                    f"function csa {{ $input | & {quote(sys.executable)} {quote(self.capture)} @args }}\n")
            encoded = base64.b64encode((stub + source).encode("utf-16-le")).decode("ascii")
            argv = [binary, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
        return subprocess.run(argv, env=env, capture_output=True, text=True, encoding="utf-8", timeout=20)

    def bash_payload(self, command, payload, delimiter=DELIMITER):
        while delimiter in payload.split("\n"):
            delimiter += "_NEXT"
        return f"{command} <<'{delimiter}'\n{payload}\n{delimiter}\n"

    def powershell_payload(self, command, payload):
        prefixed = "\n".join("|" + line for line in payload.split("\n"))
        return "$csaPayload = @'\n" + prefixed + "\n'@\n($csaPayload -replace '(?m)^[|]', '') | " + command + "\n"

    def assert_captured(self, result, argv, payload):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        record = json.loads(result.stdout)
        self.assertEqual(record, {"argv": argv.split(), "stdin": payload + "\n"})

    def test_git_bash_fixed_command_preserves_options_task_and_delimiter_collisions(self):
        payload = PAYLOAD + DELIMITER + "\n" + DELIMITER + "_NEXT\nprintf SHOULD_NOT_EXECUTE\n"
        self.assert_captured(self.shell("bash", self.bash_payload(COMMAND, payload)), COMMAND[4:], payload)

    def test_powershell_fixed_command_preserves_options_task_and_terminator_lines(self):
        self.assert_captured(self.shell("powershell", self.powershell_payload(COMMAND, PAYLOAD)), COMMAND[4:], PAYLOAD)

    def test_answers_and_followups_use_the_same_literal_transport(self):
        for shell, transport in (("bash", self.bash_payload), ("powershell", self.powershell_payload)):
            for command in ("csa message saved-job --reply-to deadbeef -",
                            "csa message saved-job --follow --from 123 --window 60 -"):
                with self.subTest(shell=shell, command=command):
                    self.assert_captured(self.shell(shell, transport(command, PAYLOAD)), command[4:], PAYLOAD)

    def test_old_git_bash_quoted_task_handles_apostrophes_but_unescaped_option_does_not(self):
        command = "csa run --follow --window 60 --runtime codex --access read -"
        task = PAYLOAD.replace("'@\n", "")
        self.assert_captured(self.shell("bash", self.bash_payload(command, task, "CSA_TASK")), command[4:], task)
        value = PAYLOAD.partition("\n")[0].removeprefix("override=")
        quoted = command.replace(" --access read", " --access read --override " + shlex.quote(value))
        result = self.shell("bash", self.bash_payload(quoted, "Task", "CSA_TASK"))
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = json.loads(result.stdout)["argv"]
        self.assertEqual(argv[argv.index("--override") + 1], value)
        broken = command.replace(" --access read", " --access read --override 'user's words'")
        result = self.shell("bash", self.bash_payload(broken, "Task", "CSA_TASK"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unexpected EOF", result.stderr)

    def test_old_powershell_unescaped_option_apostrophe_fails_before_csa(self):
        result = self.shell("powershell", "$csaPayload = @'\nTask\n'@\n$csaPayload | csa run --override 'user's words' -\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('"argv"', result.stdout)

    def test_old_powershell_literal_task_preserves_special_characters(self):
        payload = PAYLOAD.replace("'@\n", "").replace("’@\n", "")
        command = "csa run --follow --window 60 --runtime codex --access read -"
        source = "$csaPayload = @'\n" + payload + "\n'@\n$csaPayload | " + command + "\n"
        self.assert_captured(self.shell("powershell", source), command[4:], payload)

    def test_old_powershell_legacy_native_arguments_lose_embedded_double_quotes(self):
        # Select the Windows PowerShell 5.1 behavior when running under pwsh 7.3+.
        source = ("$PSNativeCommandArgumentPassing = 'Legacy'\n"
                  "$csaPayload = @'\nTask\n'@\n"
                  "$csaPayload | csa run --override 'user''s \"words\"' -\n")
        result = self.shell("powershell", source)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["argv"], ["run", "--override", "user's words", "-"])

    def test_old_git_bash_delimiter_terminates_payload_and_executes_later_lines(self):
        source = "csa message saved-job - <<'CSA_MESSAGE'\nfirst\nCSA_MESSAGE\nprintf SHELL_RAN_AFTER_DELIMITER\n"
        result = self.shell("bash", source)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.endswith("SHELL_RAN_AFTER_DELIMITER"), result.stdout)
        self.assertEqual(json.loads(result.stdout.splitlines()[0])["stdin"], "first\n")

    def test_old_powershell_here_string_terminator_executes_later_lines(self):
        source = "$csaPayload = @'\nfirst\n'@\nWrite-Output SHELL_RAN_AFTER_TERMINATOR\n$csaPayload | csa message saved-job -\n"
        result = self.shell("powershell", source)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[0], "SHELL_RAN_AFTER_TERMINATOR")
        self.assertEqual(json.loads(result.stdout.splitlines()[1])["stdin"], "first\n")

    def test_all_nine_relays_use_file_tools_and_short_commands(self):
        for runtime in ("claude", "codex", "antigravity"):
            for path in (ROOT / "agents" / f"cli-subagents-{runtime}.md",
                         ROOT / "src/cli_subagents/templates/codex-agents" / f"cli-subagents-{runtime}.toml",
                         ROOT / "src/cli_subagents/templates/antigravity-plugin/agents" / f"cli-subagents-{runtime}.md"):
                with self.subTest(path=path):
                    text = path.read_text(encoding="utf-8")
                    self.assertIn(f"csa run --follow --window 60 --runtime {runtime} --relay-file '<INBOX_PATH>'", text)
                    self.assertIn("csa message <JOB> --reply-to <ID> --relay-file '<INBOX_PATH>'", text)
                    self.assertIn("csa message <JOB> --follow --from <OFFSET> --window <WINDOW> --relay-file '<INBOX_PATH>'", text)
                    self.assertIn("csa relay-path", text)
                    self.assertIn("Never interpolate payload into shell syntax", text)
                    self.assertNotIn("<<'", text)
                    self.assertNotIn("$csaPayload", text)
                    if path.suffix == ".toml":
                        self.assertIn("native apply_patch tool directly", text)
                        self.assertIn("plain text", text)
                        self.assertNotIn("--relay-json", text)
                    elif "antigravity-plugin" in str(path):
                        self.assertIn("write_to_file", text)
                    else:
                        self.assertIn("tools: Bash, Write", text)
                    self.assertNotIn("Turn each one into the flag", text)
                    self.assertNotIn("Shell-quote each option", text)

    def test_large_file_handoff_keeps_shell_commands_short_for_all_message_forms(self):
        payload = PAYLOAD * 180 + "no final newline"
        self.assertGreater(len(payload), 10_000)
        path = Path(self.temp.name) / "O'Brien café ’quoted’.txt"
        path.write_bytes(payload.encode("utf-8"))
        self.capture.write_text(
            "import json, pathlib, sys\n"
            "args = sys.argv[1:]\n"
            "text = pathlib.Path(args[args.index('--relay-file') + 1]).read_bytes().decode('utf-8')\n"
            "print(json.dumps({'argv': args, 'payload': text}, ensure_ascii=True))\n", encoding="utf-8")
        for shell in ("bash", "powershell"):
            quote = shlex.quote(path.as_posix()) if shell == "bash" else "'" + str(path).replace("'", "''").replace("’", "’’") + "'"
            for command in ("run --follow --window 60 --runtime codex", "message saved-job --reply-to deadbeef",
                            "message saved-job --follow --from 123 --window 60"):
                with self.subTest(shell=shell, command=command):
                    source = f"csa {command} --relay-file {quote}"
                    self.assertLess(len(source), 1000)
                    result = self.shell(shell, source)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    record = json.loads(result.stdout)
                    self.assertEqual(record["payload"], payload)
                    self.assertEqual(record["argv"][-2:], ["--relay-file", str(path) if shell == "powershell" else path.as_posix()])

    def test_write_hook_filter_and_command_fail_open_with_an_older_csa(self):
        manifest = json.loads((ROOT / "hooks/hooks.json").read_text(encoding="utf-8"))
        group = manifest["hooks"]["PreToolUse"][0]
        self.assertEqual(group["matcher"], "Write")
        handler = group["hooks"][0]
        self.assertEqual(handler["if"], "Write(**/relay-inbox/**)")
        # An installed pre-0.2.15 csa rejects the unknown relay-write action
        # with argparse's exit code 2. Never invoke that real csa in this test.
        self.capture.write_text("import sys\nsys.stderr.write('unknown relay-write choice\\n')\nsys.exit(2)\n", encoding="utf-8")
        for shell in ("bash", "powershell"):
            with self.subTest(shell=shell):
                result = self.shell(shell, handler["command"])
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertIn("unknown relay-write choice", result.stderr)
