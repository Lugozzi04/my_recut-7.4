from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from automation.downloader import (
    DownloadCancellation, DownloadRequest, FfmpegRangeDownloader, PipelineDownloadQueue,
)
from automation.manager import PipelineManager
from automation.models import PipelineState
from automation.runtime import PipelineCancellation
from automation.store import PipelineStore
from utils.ffmpeg import FFprobeCancelled, cancellable_probe_scope, ffprobe_duration_seconds, run_cmd
from utils.subprocess_utils import popen_no_window


class StalledProbe:
    """Run a real child with both pipe handles open, without media/network dependencies."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.processes: list[subprocess.Popen] = []
        self.lock = threading.Lock()

    def spawn(self, _command, **kwargs):
        process = popen_no_window(
            [getattr(sys, "_base_executable", sys.executable), "-u", "-c",
             "import time; print('{\"format\":{\"duration\":\"10.0\"}}', flush=True); time.sleep(30)"],
            **kwargs,
        )
        with self.lock:
            self.processes.append(process)
        self.started.set()
        return process


def background(callback):
    errors: list[BaseException] = []

    def invoke():
        try:
            callback()
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=invoke, daemon=True)
    thread.start()
    return thread, errors


class ProbeCancellationTests(unittest.TestCase):
    def assert_reaped(self, stalled: StalledProbe) -> None:
        self.assertTrue(stalled.processes)
        for process in stalled.processes:
            self.assertIsNotNone(process.poll())
            process.wait(timeout=.2)
            self.assertTrue(process.stdout.closed)
            self.assertTrue(process.stderr.closed)
            for attribute in ("stdout_thread", "stderr_thread"):
                reader = getattr(process, attribute, None)
                if reader is not None:
                    self.assertFalse(reader.is_alive())

    def test_no_token_keeps_existing_subprocess_behavior(self):
        completed = subprocess.CompletedProcess(["probe"], 0, "ok", "")
        with patch("utils.ffmpeg.run_no_window", return_value=completed) as run, patch("utils.ffmpeg.popen_no_window") as popen:
            self.assertIs(run_cmd(["probe"], timeout_s=3), completed)
        run.assert_called_once_with(["probe"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, encoding="utf-8", errors="replace", timeout=3.0)
        popen.assert_not_called()

    def test_stalled_probe_cancel_kills_reaps_closes_pipes_and_scope_allows_retry(self):
        token = PipelineCancellation()
        stalled = StalledProbe()
        retries = []

        def invoke():
            with self.assertRaises(FFprobeCancelled):
                with cancellable_probe_scope(token):
                    ffprobe_duration_seconds("unchanged.mp4")
            # The cancelled token must not leak into the following request.
            with patch("utils.ffmpeg.popen_no_window", side_effect=popen_no_window):
                retries.append(run_cmd([sys.executable, "-c", "print('retry')"], timeout_s=3).stdout.strip())

        with patch("utils.ffmpeg.ensure_ffmpeg", return_value=("ffmpeg", "ffprobe")), patch("utils.ffmpeg.popen_no_window", side_effect=stalled.spawn):
            thread, errors = background(invoke)
            self.assertTrue(stalled.started.wait(2))
            started = time.monotonic()
            token.cancel()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(retries, ["retry"])
        self.assert_reaped(stalled)

    def test_cancel_before_spawn_and_scoped_timeout_leave_no_child(self):
        token = PipelineCancellation()
        token.cancel()
        with patch("utils.ffmpeg.popen_no_window") as popen, cancellable_probe_scope(token), self.assertRaises(FFprobeCancelled):
            run_cmd(["probe"])
        popen.assert_not_called()
        stalled = StalledProbe()
        with patch("utils.ffmpeg.popen_no_window", side_effect=stalled.spawn), cancellable_probe_scope(PipelineCancellation()), self.assertRaisesRegex(RuntimeError, "timeout"):
            run_cmd(["probe"], timeout_s=.1)
        self.assert_reaped(stalled)

    def test_child_exit_kill_race_does_not_mask_cancellation_as_corrupt_media(self):
        token = PipelineCancellation()
        process = Mock()
        process.poll.return_value = None
        process.kill.side_effect = OSError("child already exited")
        calls = []

        def communicate(*, timeout):
            calls.append(timeout)
            if len(calls) == 1:
                token.cancel()
                raise subprocess.TimeoutExpired("probe", timeout)
            return "", ""

        process.communicate.side_effect = communicate
        with patch("utils.ffmpeg.popen_no_window", return_value=process), cancellable_probe_scope(token), self.assertRaises(FFprobeCancelled):
            run_cmd(["probe"])
        self.assertEqual(calls, [.05, .75])
        process.stdout.close.assert_called_once()
        process.stderr.close.assert_called_once()

    def test_download_queue_probe_cancel_preserves_cache_and_worker_can_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = PipelineManager(PipelineStore(root / "jobs.json"))
            job, _ = manager.discover_vod(vod_id="1", vod_url="https://www.twitch.tv/videos/1")
            manager.request_range(job.id)
            job = manager.select_range(job.id, 0, 10)
            downloader = FfmpegRangeDownloader(duration_probe=ffprobe_duration_seconds, video_probe=lambda _path: True)
            request = DownloadRequest(job.id, job.vod_id, job.vod_url, 0, 10, root)
            source = root / downloader._output_name(request)
            original = b"valid cached media" * 200
            source.write_bytes(original)
            failed = threading.Event()
            finished = threading.Event()
            queue = PipelineDownloadQueue(manager, output_dir=root, downloader_factory=lambda: downloader,
                                          on_failed=lambda *_args: failed.set(), on_finished=lambda *_args: finished.set())
            self.addCleanup(queue.shutdown)
            stalled = StalledProbe()
            with patch("utils.ffmpeg.ensure_ffmpeg", return_value=("ffmpeg", "ffprobe")), patch("utils.ffmpeg.popen_no_window", side_effect=stalled.spawn):
                queue.enqueue(job.id)
                self.assertTrue(stalled.started.wait(2))
                self.assertTrue(queue.cancel_current())
                self.assertTrue(failed.wait(2))
            interrupted = manager.get(job.id)
            self.assertEqual(interrupted.error_code, "download_cancelled")
            self.assertEqual(source.read_bytes(), original)
            self.assert_reaped(stalled)
            manager.interrupt(job.id)
            self.assertEqual(manager.get(job.id).error_code, "interrupted")
            downloader._duration_probe = lambda _path: 10.0
            queue.enqueue(job.id)
            self.assertTrue(finished.wait(2))
            self.assertEqual(manager.get(job.id).state, PipelineState.ANALYZING)
            self.assertEqual(source.read_bytes(), original)
            self.assertTrue(queue.shutdown())

    def test_parallel_export_jobs_inherit_probe_cancellation(self):
        from export.exporter import ExportWorker
        worker = SimpleNamespace(_parallel_auto=False, _log=Mock())
        token = DownloadCancellation()
        stalled = StalledProbe()

        def invoke():
            with cancellable_probe_scope(token):
                ExportWorker._execute_parallel_jobs(
                    worker, [{"index": 1}, {"index": 2}],
                    lambda _job: {"elapsed_seconds": run_cmd(["probe"]).returncode},
                    2, lambda _job: 1.0, lambda _result: None, "probe_regression",
                )

        with patch("utils.ffmpeg.popen_no_window", side_effect=stalled.spawn):
            thread, errors = background(invoke)
            self.assertTrue(stalled.started.wait(2))
            token.cancel()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], FFprobeCancelled)
        self.assert_reaped(stalled)


if __name__ == "__main__":
    unittest.main()
