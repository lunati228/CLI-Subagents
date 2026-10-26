import contextlib
import io
import re
import time
from pathlib import Path
from unittest.mock import patch

from helpers import TempHome

from cli_subagents import cli, jobs, messages
from cli_subagents.follow import CodexDescriber, Follower, follow


class FollowWindowTests(TempHome):
    def setUp(self):
        super().setUp()
        self.write_config(accounts=("a1",))

    def job(self):
        return jobs.create_job(runtime="claude", prompt="p", cwd=str(self.project), start=False)["job_id"]

    def test_window_bounds_the_call_and_continuation_repeats_it(self):
        job_id, out = self.job(), io.StringIO()
        start = time.monotonic()
        self.assertEqual(follow(job_id, max_wait=540, out=out, window=1), 0)
        self.assertLess(time.monotonic() - start, 3)
        self.assertGreaterEqual(time.monotonic() - start, 1)
        lines = out.getvalue().splitlines()
        token = re.search(r"token ([0-9a-f]{32})$", lines[0]).group(1)
        self.assertIn(f"csa follow {job_id} --from 0 --window 1 # csa-token {token}", lines)
        self.assertTrue(lines[-1].endswith(f"job {job_id} · token {token}"))
        self.assertIn("STILL RUNNING", lines[-1])

    def test_question_continuation_keeps_window(self):
        job_id, out = self.job(), io.StringIO()
        messages.post_from_worker(job_id, "Which file?", kind="question")
        self.assertEqual(follow(job_id, max_wait=0, out=out, window=60), 0)
        self.assertRegex(out.getvalue(), rf"csa follow {job_id} --from 0 --window 60 # csa-token [0-9a-f]{{32}}")
        self.assertIn("[csa] QUESTION", out.getvalue())

    def test_default_terminal_wait_and_explicit_max_wait_unchanged(self):
        job_id = self.job()
        self.assertEqual(Follower(job_id, None, 540).max_wait, 540)
        self.assertEqual(Follower(job_id, None, 10, window=60).max_wait, 10)
        out = io.StringIO()
        follow(job_id, max_wait=0, out=out)
        command = next(line for line in out.getvalue().splitlines() if line.startswith("csa follow"))
        self.assertNotIn("--window", command)

    def test_all_three_commands_forward_window(self):
        job_id = self.job()
        for argv in (["run", "--follow", "--window", "23", "p"],
                     ["follow", job_id, "--window", "23"],
                     ["message", job_id, "--follow", "--window", "23", "p"]):
            with self.subTest(argv=argv), patch("cli_subagents.cli._start", return_value={"job_id": job_id}), \
                    patch("cli_subagents.tools.call", return_value={}), \
                    patch("cli_subagents.follow.follow", return_value=0) as watching:
                self.assertEqual(cli.main(argv), 0)
                self.assertEqual(watching.call_args.kwargs["window"], 23)

    def test_window_default_and_invalid_values(self):
        parser = cli.build_parser()
        self.assertEqual(parser.parse_args(["follow", "job", "--window"]).window, 60)
        self.assertIsNone(parser.parse_args(["follow", "job"]).window)
        for value in ("0", "-1", "0.5", "nan"):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parser.parse_args(["follow", "job", "--window", value])

    def test_codex_reasoning_is_one_short_line(self):
        describer = CodexDescriber()
        for text, expected in (
            ("**Checking tests**\n\nI will read the failure. Then make a fix.\nMore detail.",
             "thinking: Checking tests: I will read the failure."),
            ("Check the logs first.\nThen inspect the config.", "thinking: Check the logs first."),
            ("**Checking**", "thinking: Checking"),
        ):
            self.assertEqual(describer.describe({"type": "item.completed", "item": {"type": "reasoning", "text": text}}),
                             [expected])
        lines = describer.describe({"type": "item.completed", "item": {"type": "reasoning", "text": "word " * 200}})
        self.assertEqual(len(lines), 1)
        self.assertLessEqual(len(lines[0]), len("thinking: ") + 200)

    def test_claude_relays_use_window_with_validated_continuations(self):
        root = Path(__file__).resolve().parents[1]
        for runtime in ("claude", "codex", "antigravity", "opencode"):
            text = (root / "agents" / f"cli-subagents-{runtime}.md").read_text(encoding="utf-8")
            self.assertIn("csa run --follow --window 60", text)
            self.assertIn("csa follow <JOB> --from <OFFSET> --window <WINDOW> # csa-token <TOKEN>", text)
            self.assertIn("csa message <JOB> --follow --from <OFFSET> --window <WINDOW> -", text)
            self.assertIn("WINDOW must be the saved positive integer", text)
            self.assertIn("matching the current header", text)
