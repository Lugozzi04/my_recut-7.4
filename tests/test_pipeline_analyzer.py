from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

import numpy as np

from analysis.audio_service import AnalysisResult
from automation.analyzer import (
    AnalysisProjectResult,
    PipelineAnalysisCancelled,
    PipelineAnalysisError,
    PipelineAnalysisQueue,
    PipelineAnalysisService,
    PipelineProjectAnalyzer,
)
from automation.manager import PipelineManager
from automation.models import PipelineState
from automation.store import PipelineStore
from core.project_file import normalize_project_payload, resolve_project_items
from utils.ffmpeg import ensure_ffmpeg
from utils.subprocess_utils import run_no_window


def fake_audio_analysis(request, *, cancellation=None, on_progress=None):
    _ = request
    if cancellation is not None:
        cancellation.raise_if_cancelled()
    if on_progress is not None:
        on_progress(0)
        on_progress(50)
        on_progress(100)
    rms = np.concatenate(
        [
            np.zeros(50, dtype=np.float32),
            np.full(50, 0.08, dtype=np.float32),
            np.zeros(100, dtype=np.float32),
        ]
    )
    return AnalysisResult(duration=6.0, rms=rms, hop_s=0.03, auto_threshold=0.01)


class SuccessfulProjectAnalyzer:
    def analyze(self, job, *, cancellation=None, on_progress=None):
        _ = cancellation
        source = Path(job.local_source_path)
        project = source.with_suffix(".autocutter")
        project.write_text("project", encoding="utf-8")
        if on_progress is not None:
            on_progress(30)
            on_progress(100)
        return AnalysisProjectResult(project, 2.0, 1)


class FailingProjectAnalyzer:
    def analyze(self, _job, *, cancellation=None, on_progress=None):
        _ = (cancellation, on_progress)
        raise PipelineAnalysisError("analysis_test_failed", "analysis failed for test")


class BlockingProjectAnalyzer:
    started = threading.Event()

    def analyze(self, _job, *, cancellation=None, on_progress=None):
        _ = on_progress
        self.started.set()
        assert cancellation is not None
        while not cancellation.is_cancelled:
            time.sleep(0.01)
        raise PipelineAnalysisCancelled()


class PipelineAnalyzerTests(unittest.TestCase):
    def _manager_with_analysis_job(self, root: Path) -> tuple[PipelineManager, str, Path]:
        source = root / "downloaded.mp4"
        source.write_bytes(b"downloaded-video")
        manager = PipelineManager(PipelineStore(root / "jobs.json"))
        job, _created = manager.discover_vod(
            vod_id="12345",
            vod_url="https://www.twitch.tv/videos/12345",
            source_title="Test VOD",
        )
        manager.request_range(job.id)
        manager.select_range(job.id, 1.0, 3.0)
        manager.mark_downloaded(job.id, source)
        return manager, job.id, source

    def test_project_analyzer_writes_portable_editor_project_and_reuses_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, job_id, source = self._manager_with_analysis_job(root)
            analyzer = PipelineProjectAnalyzer(analyze_fn=fake_audio_analysis)
            progress: list[int] = []

            result = analyzer.analyze(manager.get(job_id), on_progress=progress.append)

            self.assertTrue(result.path.is_file())
            self.assertFalse(result.reused)
            self.assertGreater(result.cuts_count, 0)
            self.assertEqual(progress, [0, 47, 95, 100])
            payload = normalize_project_payload(json.loads(result.path.read_text(encoding="utf-8")))
            self.assertEqual(payload["format"], "autocutter_project")
            self.assertEqual(payload["automation"]["job_id"], job_id)
            self.assertEqual(payload["global"]["analysis_mode"], "classic")
            self.assertEqual(payload["global"]["export_settings"]["preset"], "original_hq")
            track = payload["tracks"][0]
            self.assertEqual(track["path_kind"], "relative")
            self.assertEqual(track["cfg"]["intensity"], 50)
            self.assertTrue(track["cuts_enabled"])
            self.assertEqual(track["cuts"], track["classic_cuts"])
            available, missing = resolve_project_items(payload["tracks"], result.path)
            self.assertEqual(missing, [])
            self.assertEqual(Path(available[0]["path"]), source.resolve())

            reused_progress: list[int] = []
            reused = analyzer.analyze(manager.get(job_id), on_progress=reused_progress.append)
            self.assertTrue(reused.reused)
            self.assertEqual(reused.path, result.path)
            self.assertEqual(reused_progress, [100])

    def test_cached_project_is_invalidated_when_source_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, job_id, source = self._manager_with_analysis_job(root)
            calls = 0

            def counting_analysis(*args, **kwargs):
                nonlocal calls
                calls += 1
                return fake_audio_analysis(*args, **kwargs)

            analyzer = PipelineProjectAnalyzer(analyze_fn=counting_analysis)
            analyzer.analyze(manager.get(job_id))
            source.write_bytes(b"changed-downloaded-video")
            analyzer.analyze(manager.get(job_id))

            self.assertEqual(calls, 2)

    def test_service_marks_successful_analysis_ready_for_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, job_id, _source = self._manager_with_analysis_job(root)
            progress: list[int] = []
            service = PipelineAnalysisService(
                manager,
                analyzer_factory=SuccessfulProjectAnalyzer,
            )

            completed = service.execute(job_id, on_progress=progress.append)

            self.assertEqual(completed.state, PipelineState.READY_EXPORT)
            self.assertTrue(Path(completed.project_path or "").is_file())
            self.assertEqual(progress, [30, 100])

    def test_real_ffmpeg_analysis_generates_a_ready_project(self) -> None:
        ffmpeg, _ffprobe = ensure_ffmpeg()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, job_id, source = self._manager_with_analysis_job(root)
            generated = run_no_window(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-v",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=160x90:r=25:d=2",
                    "-f",
                    "lavfi",
                    "-i",
                    "anullsrc=r=16000:cl=mono",
                    "-t",
                    "2",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-c:a",
                    "aac",
                    "-shortest",
                    str(source),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)

            completed = PipelineAnalysisService(manager).execute(job_id)

            self.assertEqual(completed.state, PipelineState.READY_EXPORT)
            project = Path(completed.project_path or "")
            payload = normalize_project_payload(json.loads(project.read_text(encoding="utf-8")))
            self.assertEqual(payload["automation"]["job_id"], job_id)
            self.assertGreater(float(payload["tracks"][0]["duration"]), 1.5)

    def test_service_failure_is_persisted_as_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, job_id, _source = self._manager_with_analysis_job(root)
            service = PipelineAnalysisService(
                manager,
                analyzer_factory=FailingProjectAnalyzer,
            )

            with self.assertRaises(PipelineAnalysisError):
                service.execute(job_id)

            failed = manager.get(job_id)
            self.assertEqual(failed.state, PipelineState.FAILED)
            self.assertEqual(failed.retry_state, PipelineState.ANALYZING)
            self.assertEqual(failed.error_code, "analysis_test_failed")

    def test_queue_cancellation_is_retryable_and_stops_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, job_id, _source = self._manager_with_analysis_job(root)
            failed_event = threading.Event()
            BlockingProjectAnalyzer.started.clear()
            queue = PipelineAnalysisQueue(
                manager,
                analyzer_factory=BlockingProjectAnalyzer,
                on_failed=lambda _job, _message: failed_event.set(),
            )

            self.assertTrue(queue.enqueue(job_id))
            self.assertTrue(BlockingProjectAnalyzer.started.wait(1.0))
            self.assertTrue(queue.cancel_current())
            self.assertTrue(failed_event.wait(2.0))
            self.assertTrue(queue.shutdown(timeout=2.0))

            failed = manager.get(job_id)
            self.assertEqual(failed.state, PipelineState.FAILED)
            self.assertEqual(failed.retry_state, PipelineState.ANALYZING)
            self.assertEqual(failed.error_code, "analysis_cancelled")


if __name__ == "__main__":
    unittest.main()
