"""A runner PID that another process took over (after a crash or a reboot) is not the runner."""

import json
import os
import time
import unittest
from unittest.mock import patch

from helpers import TempHome

from cli_subagents import jobs, limits
from cli_subagents.fsutil import process_start_time, write_text
from cli_subagents.runner import Runner
from cli_subagents.runtimes.base import CANCELLED, LOST


class ReusedRunnerPidTests(TempHome):
    def stale_job(self, started):
        """A job whose recorded runner PID now belongs to this test process."""

        self.write_config()
        job_id = jobs.create_job(runtime="claude", prompt="x", cwd=str(self.project), start=False)["job_id"]
        folder = jobs.job_dir(job_id)
        write_text(folder / "runner.pid", str(os.getpid()))
        path = folder / "status.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw.update(state="running", runner_pid=os.getpid(), runner_started=started, updated_at=time.time() - 120)
        path.write_text(json.dumps(raw), encoding="utf-8")
        return job_id

    def test_reused_pid_reads_as_lost(self):
        own = process_start_time(os.getpid())
        if own is None:
            self.skipTest("no process start times on this system")
        job_id = self.stale_job(own + 1)
        self.assertEqual(jobs.read_status(job_id)["outcome"], LOST)

    def test_reused_pid_does_not_count_as_running(self):
        own = process_start_time(os.getpid())
        if own is None:
            self.skipTest("no process start times on this system")
        self.stale_job(own + 1)
        self.assertEqual(limits.count_active().total, 0)

    def test_cancel_never_kills_the_process_that_reused_the_pid(self):
        own = process_start_time(os.getpid())
        if own is None:
            self.skipTest("no process start times on this system")
        job_id = self.stale_job(own + 1)
        with patch.object(jobs, "kill_tree") as kill:
            status = jobs.cancel(job_id)
        self.assertNotIn(os.getpid(), [call.args[0] for call in kill.call_args_list])
        self.assertEqual(status["outcome"], CANCELLED)

    def test_live_runner_with_its_own_start_time_stays_running(self):
        job_id = self.stale_job(process_start_time(os.getpid()))
        self.assertEqual(jobs.read_status(job_id)["state"], "running")
        self.assertEqual(limits.count_active().total, 1)

    def test_runner_records_its_start_time(self):
        self.write_config()
        job_id = jobs.create_job(runtime="claude", prompt="x", cwd=str(self.project), start=False)["job_id"]
        Runner(job_id).status(state="starting")
        status = jobs.read_status(job_id)
        self.assertEqual(status["runner_pid"], os.getpid())
        self.assertEqual(status["runner_started"], process_start_time(os.getpid()))


if __name__ == "__main__":
    unittest.main()
