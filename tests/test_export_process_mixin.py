from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from unittest.mock import patch

from export.process_mixin import ExportProcessMixin
from export.runtime_utils import ExportCancelled


class _ProcessHarness(ExportProcessMixin):
    def __init__(self) -> None:
        self.logs: list[str] = []
        self._init_process_control()

    def _log(self, message: str) -> None:
        self.logs.append(message)


class ExportProcessMixinTests(unittest.TestCase):
    def test_runs_process_and_collects_stderr(self) -> None:
        worker = _ProcessHarness()

        return_code, stdout, stderr = worker._run_ffmpeg(
            [sys.executable, "-c", "import sys; print('diagnostic', file=sys.stderr)"]
        )

        self.assertEqual(return_code, 0)
        self.assertEqual(stdout, "")
        self.assertIn("diagnostic", stderr)
        self.assertFalse(worker._active_procs)

    def test_reports_progress_timestamps(self) -> None:
        worker = _ProcessHarness()
        progress: list[float] = []

        return_code, _ = worker._run_ffmpeg_progress(
            [
                sys.executable,
                "-c",
                "import sys; print('out_time=00:00:01.500', file=sys.stderr)",
            ],
            progress.append,
        )

        self.assertEqual(return_code, 0)
        self.assertEqual(progress, [1.5])

    def test_repeated_progress_timestamp_warns_without_killing_active_process(self) -> None:
        worker = _ProcessHarness()
        script = "import sys,time\nfor _ in range(20):\n print('out_time_us=1000000',file=sys.stderr,flush=True); time.sleep(.03)"
        with patch.dict(os.environ, {
            "AUTO_CUTTER_FFMPEG_PROGRESS_STALL_WARN_SEC": "0.15",
            "AUTO_CUTTER_FFMPEG_PROGRESS_STALL_KILL_SEC": "0",
            "AUTO_CUTTER_FFMPEG_PROGRESS_HEARTBEAT_SEC": "0.10",
        }):
            code, _ = worker._run_ffmpeg_progress([sys.executable, "-c", script], lambda t: None)
        self.assertEqual(code, 0)
        self.assertTrue(any("ffmpeg_stall_warning no_output_progress_for=" in line for line in worker.logs))
        self.assertTrue(any("ffmpeg_progress_wait" in line and "no_output_progress_for=" in line for line in worker.logs))
        self.assertFalse(worker._active_procs)

    def test_cancel_terminates_a_running_process(self) -> None:
        worker = _ProcessHarness()
        failures: list[BaseException] = []

        def _run() -> None:
            try:
                worker._run_ffmpeg(
                    [sys.executable, "-c", "import time; time.sleep(30)"]
                )
            except BaseException as exc:
                failures.append(exc)

        thread = threading.Thread(target=_run)
        thread.start()
        deadline = time.monotonic() + 5.0
        while not worker._active_procs and time.monotonic() < deadline:
            time.sleep(0.01)

        self.assertTrue(worker._active_procs)
        worker.cancel()
        thread.join(timeout=5.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], ExportCancelled)
        self.assertFalse(worker._active_procs)


if __name__ == "__main__":
    unittest.main()
