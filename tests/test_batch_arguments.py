"""Workers started through a .cmd or .bat file: cmd.exe must not reinterpret their arguments."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from cli_subagents.fsutil import batch_argument_problem, worker_process


class BatchArgumentProblemTest(unittest.TestCase):
    def test_programs_other_than_batch_files_are_never_checked(self):
        self.assertIsNone(batch_argument_problem(["C:/tools/claude.exe", "--model", "a&b"]))
        self.assertIsNone(batch_argument_problem(["node", "cli.js", "--add-dir", "C:/R&D"]))

    def test_plain_and_quoted_arguments_pass(self):
        argv = ["C:/npm/claude.cmd", "-p", "--model", "claude-opus-5-5[1m]", "--append-system-prompt",
                "Work only on the task (stay in scope) & say what is left | done",
                "--settings", '{"disableAllHooks": true}', "--tools", "", "--add-dir", "C:/Program Files (x86)/x"]
        self.assertIsNone(batch_argument_problem(argv))

    def test_unquoted_metacharacters_are_refused(self):
        for value in ("a&calc", "x|y", "a>b", "a<b", "x^y", "C:/R&D/project", "fn(x)"):
            with self.subTest(value=value):
                self.assertIsNotNone(batch_argument_problem(["C:/npm/claude.CMD", "--add-dir", value]))

    def test_embedded_quote_cannot_reopen_cmd_parsing(self):
        # list2cmdline turns " into \", which cmd.exe still reads as a quote toggle.
        problem = batch_argument_problem(["C:/npm/codex.bat", "--model", 'x" & calc & "'])
        self.assertIsNotNone(problem)

    def test_percent_and_line_breaks_are_refused_even_inside_quotes(self):
        for value in ("100% sure today", "%PATH%", "two\nlines here", "a\rb c"):
            with self.subTest(value=value):
                self.assertIsNotNone(batch_argument_problem(["C:/npm/claude.cmd", "--model", value]))


@unittest.skipUnless(os.name == "nt", "cmd.exe runs .cmd files on Windows only")
class WorkerProcessBatchTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.folder, ignore_errors=True))
        self.script = self.folder / "worker.cmd"
        self.script.write_text("@echo ARGS %*\r\n", encoding="ascii")
        self.marker = self.folder / "injected.txt"

    def test_injection_through_a_batch_file_is_refused_before_start(self):
        # No spaces, so list2cmdline leaves it unquoted; without the check cmd.exe creates the marker.
        self.assertNotIn(" ", str(self.marker))
        payload = f"x&echo.>{self.marker}"
        with self.assertRaises(OSError):
            with worker_process([str(self.script), payload], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL) as process:
                process.wait(timeout=30)
        self.assertFalse(self.marker.exists())

    def test_safe_arguments_still_reach_the_batch_file(self):
        with worker_process([str(self.script), "--model", "opus", "two words"], stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL) as process:
            output = process.stdout.read().decode("utf-8", errors="replace")
            process.wait(timeout=30)
        self.assertIn('ARGS --model opus "two words"', output)


if __name__ == "__main__":
    unittest.main()
