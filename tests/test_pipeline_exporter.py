from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from automation.exporter import (
    ExportCancellation,
    ExportResult,
    PipelineExportCancelled,
    PipelineExportError,
    PipelineExportQueue,
    PipelineExportService,
    PipelineProjectExporter,
    ProjectExportPlanner,
)
from automation.manager import PipelineManager
from automation.models import PipelineJob, PipelineState
from automation.store import PipelineStore
from export.settings import ExportSettings
from utils.codec_detection import CodecSelection
from utils.ffmpeg import ensure_ffmpeg, has_audio_stream, has_video_stream
from utils.subprocess_utils import run_no_window


class FakeSignal:
    def __init__(self) -> None:
        self.callbacks = []

    def connect(self, callback) -> None:
        self.callbacks.append(callback)

    def emit(self, *args) -> None:
        for callback in list(self.callbacks):
            callback(*args)


class FakeWorker:
    created: list[dict] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.progress = FakeSignal()
        self.detail = FakeSignal()
        self.error = FakeSignal()
        self.debug = False
        self.cancelled = False
        self.created.append(kwargs)

    def run(self) -> None:
        if self.cancelled:
            self.error.emit("Export cancelled.")
            return
        self.detail.emit("fake_ffmpeg_flags -c:v libx264 -crf 16")
        self.progress.emit(50, "Export 50%")
        Path(self.kwargs["output_path"]).write_bytes(b"video" * 400)
        self.progress.emit(100, "100%")

    def cancel(self) -> None:
        self.cancelled = True


class SuccessfulProjectExporter:
    def __init__(self, output: Path) -> None:
        self.output = output

    def export(self, _job, *, cancellation=None, on_progress=None, on_detail=None) -> ExportResult:
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        if on_detail is not None:
            on_detail("fake export")
        if on_progress is not None:
            on_progress(40)
            on_progress(100)
        self.output.write_bytes(b"exported")
        return ExportResult(self.output, 12.0, "libx264")


class BlockingProjectExporter:
    def __init__(self, started: threading.Event) -> None:
        self.started = started

    def export(self, _job, *, cancellation=None, on_progress=None, on_detail=None) -> ExportResult:
        _ = on_progress, on_detail
        self.started.set()
        assert cancellation is not None
        while not cancellation.is_cancelled:
            time.sleep(0.005)
        raise PipelineExportCancelled()


class PipelineExporterTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeWorker.created.clear()

    @staticmethod
    def _project_payload(source: Path, *, duration: float = 10.0) -> dict:
        settings = ExportSettings(
            codec="libx264",
            method="filter_concat",
            hwaccel_decode=False,
        ).normalized()
        return {
            "format": "autocutter_project",
            "version": 2,
            "global": {"export_settings": settings.to_mapping()},
            "tracks": [
                {
                    "path": str(source),
                    "duration": duration,
                    "segment_source_in": 0.0,
                    "segment_source_out": duration,
                    "cuts_enabled": True,
                    "cuts": [{"start": 3.0, "end": 5.0}],
                    "keeps": [{"start": 0.0, "end": 3.0}, {"start": 5.0, "end": duration}],
                    "cfg": {
                        "gain_db": 1.5,
                        "normalize_lufs": True,
                        "lufs_target": -14.0,
                        "limiter": True,
                    },
                }
            ],
        }

    @staticmethod
    def _ready_job(root: Path, payload: dict) -> tuple[PipelineManager, PipelineJob]:
        manager = PipelineManager(PipelineStore(root / "jobs.json"))
        source = Path(payload["tracks"][0]["path"])
        project = root / "source.autocutter"
        project.write_text(json.dumps(payload), encoding="utf-8")
        job, _created = manager.discover_vod(vod_id="vod-1", vod_url="https://twitch.tv/videos/1")
        manager.request_range(job.id)
        manager.select_range(job.id, 0, 10)
        manager.mark_downloaded(job.id, source)
        ready = manager.mark_analyzed(job.id, project)
        return manager, ready

    def test_planner_builds_fast_single_source_keeps_and_audio_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            manager, job = self._ready_job(root, self._project_payload(source))
            _ = manager
            planner = ProjectExportPlanner(
                duration_probe=lambda _path: 10.0,
                audio_probe=lambda _path: True,
            )

            plan = planner.build(job)

            self.assertEqual(plan.input_paths, (str(source.resolve()),))
            self.assertEqual([(item.start, item.end) for item in plan.keeps], [(0.0, 3.0), (5.0, 10.0)])
            self.assertEqual(plan.segments, ())
            self.assertEqual(plan.expected_duration_s, 8.0)
            self.assertEqual(plan.audio_gain_db, 1.5)
            self.assertTrue(plan.normalize_lufs)
            self.assertEqual(plan.requested_settings.output_mode, "single")

    def test_service_moves_job_to_ready_upload_and_persists_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            manager, job = self._ready_job(root, self._project_payload(source))
            output = root / "ready.mp4"
            progress: list[int] = []
            service = PipelineExportService(
                manager,
                exporter_factory=lambda: SuccessfulProjectExporter(output),
            )

            completed = service.execute(job.id, on_progress=progress.append)

            self.assertEqual(completed.state, PipelineState.READY_UPLOAD)
            self.assertEqual(completed.export_path, str(output.resolve()))
            self.assertEqual(progress, [40, 100])
            self.assertEqual(manager.get(job.id).progress, 0.0)

    def test_export_cache_reuses_only_valid_matching_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            _manager, job = self._ready_job(root, self._project_payload(source))
            planner = ProjectExportPlanner(
                duration_probe=lambda _path: 10.0,
                audio_probe=lambda _path: True,
            )
            details: list[str] = []
            exporter = PipelineProjectExporter(
                planner=planner,
                worker_factory=FakeWorker,
                ffmpeg_provider=lambda: ("ffmpeg", "ffprobe"),
                codec_resolver=lambda _ffmpeg, requested: CodecSelection(
                    str(requested), "libx264"
                ),
                duration_probe=lambda _path: 8.0,
                video_probe=lambda _path: True,
                audio_probe=lambda _path: True,
            )

            first = exporter.export(job, on_detail=details.append)
            second = exporter.export(job, on_detail=details.append)

            self.assertFalse(first.reused)
            self.assertTrue(second.reused)
            self.assertEqual(first.path, second.path)
            self.assertEqual(len(FakeWorker.created), 1)
            self.assertTrue(any("fake_ffmpeg_flags" in line for line in details))
            self.assertTrue(any("automation_export_cache_hit" in line for line in details))

    def test_queue_cancellation_leaves_export_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            manager, job = self._ready_job(root, self._project_payload(source))
            started = threading.Event()
            failed = threading.Event()
            queue = PipelineExportQueue(
                manager,
                exporter_factory=lambda: BlockingProjectExporter(started),
                on_failed=lambda _job, _message: failed.set(),
            )

            self.assertTrue(queue.enqueue(job.id))
            self.assertTrue(started.wait(1.0))
            self.assertTrue(queue.cancel_current())
            self.assertTrue(failed.wait(1.0))
            queue.shutdown()

            result = manager.get(job.id)
            self.assertEqual(result.state, PipelineState.FAILED)
            self.assertEqual(result.retry_state, PipelineState.EXPORTING)
            self.assertEqual(result.error_code, "export_cancelled")

    def test_invalid_project_fails_without_starting_render(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            manager, job = self._ready_job(root, self._project_payload(source))
            Path(str(job.project_path)).write_text("not-json", encoding="utf-8")
            service = PipelineExportService(
                manager,
                exporter_factory=lambda: PipelineProjectExporter(
                    planner=ProjectExportPlanner(audio_probe=lambda _path: True),
                    worker_factory=FakeWorker,
                ),
            )

            with self.assertRaises(PipelineExportError):
                service.execute(job.id)

            failed = manager.get(job.id)
            self.assertEqual(failed.state, PipelineState.FAILED)
            self.assertEqual(failed.retry_state, PipelineState.EXPORTING)
            self.assertEqual(FakeWorker.created, [])

    def test_real_ffmpeg_export_contains_video_and_audio(self) -> None:
        try:
            ffmpeg, _ffprobe = ensure_ffmpeg()
        except Exception as exc:
            self.skipTest(f"FFmpeg unavailable: {exc}")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.mp4"
            generated = run_no_window(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=320x180:rate=24:duration=2",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:sample_rate=48000:duration=2",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-shortest",
                    "-y",
                    str(source),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if generated.returncode != 0:
                self.skipTest(f"Could not create FFmpeg fixture: {generated.stderr}")
            manager, job = self._ready_job(root, self._project_payload(source, duration=2.0))

            completed = PipelineExportService(manager).execute(job.id)

            output = Path(str(completed.export_path))
            self.assertEqual(completed.state, PipelineState.READY_UPLOAD)
            self.assertTrue(output.is_file())
            self.assertTrue(has_video_stream(str(output)))
            self.assertTrue(has_audio_stream(str(output)))

            # Extend the existing real-media integration through the unattended
            # frontend: actual analysis, the GUI's preset, export and validation.
            from automation.runtime import PipelineRuntime
            from core.presets import PresetRepository

            runtime = PipelineRuntime(
                manager, PresetRepository(root / "presets.json"), settings_loader=lambda: {},
            )
            destination = root / "other directory" / "l'export \u00e8 pronto \U0001f3ac.mp4"
            plan = runtime.plan_local(source, start=0.2, end=1.8, output=destination)
            unattended = runtime.run(runtime.create_job(plan).id)
            self.assertEqual(unattended.state, PipelineState.DONE)
            self.assertEqual(Path(str(unattended.export_path)), destination)
            self.assertTrue(has_video_stream(str(destination)))
            self.assertTrue(has_audio_stream(str(destination)))
            self.assertTrue(source.is_file())
            self.assertEqual(runtime.run(unattended.id).export_path, unattended.export_path)


if __name__ == "__main__":
    unittest.main()
