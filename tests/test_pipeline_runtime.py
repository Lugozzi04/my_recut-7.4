from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from analysis.audio_service import AnalysisCancellation, AnalysisResult

from automation.analyzer import PipelineAnalysisCancelled, PipelineAnalysisService, PipelineProjectAnalyzer
from automation.downloader import DownloadResult, PipelineDownloadService, YtDlpMediaResolver
from automation.exporter import ExportResult, PipelineExportService
from automation.exporter import ExportCancellation
from automation.manager import PipelineJobBusyError, PipelineManager
from automation.models import PipelineState
from automation.runtime import PipelineCancellation, PipelineRuntime, PipelineRuntimeError, validate_range
from automation.store import PipelineStore, default_pipeline_store_path
from core.presets import PresetRepository, default_presets_catalog
from utils.ffmpeg import ffprobe_duration_seconds
from utils.subprocess_utils import popen_no_window


class FakeMetadata:
    def metadata(self, _url):
        return {"id": "123", "title": "../../CON: VOD 🎮 . ", "duration_s": 10.0,
                "http_headers": {"Authorization": "secret"}, "url": "https://signed/?token=secret"}


class FakeDownloader:
    calls = 0

    def download(self, request, *, cancellation=None, on_progress=None):
        FakeDownloader.calls += 1
        cancellation.raise_if_cancelled()
        request.output_dir.mkdir(parents=True, exist_ok=True)
        path = request.output_dir / "downloaded.mp4"
        path.write_bytes(b"video" * 500)
        on_progress(100)
        return DownloadResult(path, request.duration_s)


class FakeAnalyzer(PipelineProjectAnalyzer):
    calls = 0

    def __init__(self):
        def fake_audio(request, *, cancellation=None, on_progress=None):
            cancellation.raise_if_cancelled()
            on_progress(100)
            return AnalysisResult(10.0, np.full(334, .1, dtype=np.float32), .03, .01)
        super().__init__(analyze_fn=fake_audio)

    def analyze(self, job, *, cancellation=None, on_progress=None):
        FakeAnalyzer.calls += 1
        return super().analyze(job, cancellation=cancellation, on_progress=on_progress)


class FakeExporter:
    calls = 0

    def export(self, job, *, cancellation=None, on_progress=None, on_detail=None):
        FakeExporter.calls += 1
        cancellation.raise_if_cancelled()
        path = Path(job.metadata["delivery"]["resolved_output_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"export" * 500)
        stat = path.stat()
        path.with_suffix(path.suffix + ".automation.json").write_text(json.dumps({
            "format": "autocutter_automation_export", "version": 1, "signature": "fake-render-signature",
            "duration_s": 10.0, "output_size": stat.st_size, "output_mtime_ns": stat.st_mtime_ns,
            "require_audio": False,
        }), encoding="utf-8")
        on_progress(100)
        return ExportResult(path, 10.0, "libx264")


class PipelineRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # The pipeline returns canonical paths; Windows TEMP may use 8.3 aliases.
        self.root = Path(self.temp.name).resolve()
        self.env = patch.dict(os.environ, {
            "AUTO_CUTTER_CONFIG_DIR": str(self.root / "config"),
            "AUTO_CUTTER_OUTPUT_DIR": str(self.root / "output"),
            "AUTO_CUTTER_DOWNLOAD_DIR": str(self.root / "downloads"),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.source = self.root / "spazi à 🎮.mp4"
        self.source.write_bytes(b"local" * 500)
        self.manager = PipelineManager(PipelineStore(self.root / "state" / "jobs.json"))
        self.presets = PresetRepository(self.root / "presets.json")
        self.presets.save(default_presets_catalog())
        FakeDownloader.calls = FakeAnalyzer.calls = FakeExporter.calls = 0

    def runtime(self, **kwargs):
        defaults = {
            "metadata_resolver": FakeMetadata(), "duration_probe": lambda _path: 10.0,
            "video_probe": lambda _path: True, "audio_probe": lambda _path: True, "settings_loader": lambda: {},
            "download_service_factory": lambda manager: PipelineDownloadService(manager, downloader_factory=FakeDownloader),
            "analysis_service_factory": lambda manager: PipelineAnalysisService(manager, analyzer_factory=FakeAnalyzer),
            "export_service_factory": lambda manager: PipelineExportService(manager, exporter_factory=FakeExporter),
        }
        defaults.update(kwargs)
        return PipelineRuntime(self.manager, self.presets, **defaults)

    def test_cancel_during_reconciliation_probe_preserves_source_and_resume(self):
        runtime = self.runtime()
        job = runtime.create_job(runtime.plan_local(self.source))
        original = self.source.read_bytes()
        runtime._duration_probe = ffprobe_duration_seconds
        token = PipelineCancellation()
        started = threading.Event()
        processes = []
        errors = []

        def spawn(_cmd, **kwargs):
            process = popen_no_window([getattr(sys, "_base_executable", sys.executable), "-c", "import time; time.sleep(30)"], **kwargs)
            processes.append(process)
            started.set()
            return process

        def run():
            try:
                runtime.run(job.id, cancellation=token)
            except BaseException as exc:
                errors.append(exc)

        with patch("utils.ffmpeg.ensure_ffmpeg", return_value=("ffmpeg", "ffprobe")), patch("utils.ffmpeg.popen_no_window", side_effect=spawn):
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            self.assertTrue(started.wait(2))
            token.cancel()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], PipelineRuntimeError)
        self.assertEqual(errors[0].exit_code, 70)
        self.assertEqual(self.manager.get(job.id).state, PipelineState.CANCELLED)
        self.assertEqual(self.source.read_bytes(), original)
        self.assertFalse(Path(job.metadata["delivery"]["project_path"]).exists())
        self.assertIsNotNone(processes[0].poll())
        processes[0].wait(timeout=.2)
        runtime._duration_probe = lambda _path: 10.0
        self.assertEqual(runtime.run(job.id, resume=True).state, PipelineState.DONE)
        self.assertEqual(self.source.read_bytes(), original)

    def test_planning_is_read_only_and_metadata_has_no_secrets(self):
        runtime = self.runtime()
        plan = runtime.plan_vod("https://www.twitch.tv/videos/123", start=2, end=10.5, youtube=True)
        self.assertEqual(plan["range"], {"start_s": 2.0, "end_s": 10.0})
        self.assertTrue(plan["warnings"])
        self.assertNotIn("secret", json.dumps(plan))
        self.assertFalse(self.manager.store.path.exists())
        self.assertFalse((self.root / "downloads").exists())
        self.assertFalse((self.root / "output").exists())
        self.assertEqual(Path(plan["delivery"]["output_path"]).parent, self.root / "output")

    def test_invalid_time_ranges(self):
        for options in ({"start": -1}, {"start": 10}, {"end": 0}, {"end": 50},
                        {"duration": 0}, {"end": 8, "duration": 2}, {"start": float("nan")}):
            with self.subTest(options=options), self.assertRaises(PipelineRuntimeError) as caught:
                validate_range(10, **options)
            self.assertEqual(caught.exception.exit_code, 2)
        self.assertEqual(validate_range(10, start=2, duration=4)[:2], (2, 6))
        self.assertEqual(validate_range(10, end=4)[:2], (0, 4))

    def test_invalid_urls_rejected_before_metadata_lookup(self):
        for url in ("http://twitch.tv/videos/123", "https://evil.test/videos/123",
                    "https://www.twitch.tv/videos/123?token=secret", "https://user@twitch.tv/videos/123"):
            with self.subTest(url=url), self.assertRaises(PipelineRuntimeError) as caught:
                self.runtime().plan_vod(url)
            self.assertEqual(caught.exception.exit_code, 2)

    def test_unknown_preset_and_missing_thumbnail_are_clear_errors(self):
        with self.assertRaises(PipelineRuntimeError) as caught:
            self.runtime().plan_local(self.source, preset="absent")
        self.assertEqual(caught.exception.code, "unknown_preset")
        with self.assertRaises(PipelineRuntimeError) as caught:
            self.runtime().plan_local(self.source, thumbnail=self.root / "absent.png")
        self.assertEqual(caught.exception.exit_code, 60)

    def test_atomic_local_creation_deduplicates_and_freezes_preset(self):
        runtime = self.runtime()
        plan = runtime.plan_local(self.source, start=2, end=8)
        first = runtime.create_job(plan)
        second = runtime.create_job(plan)
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.state, PipelineState.ANALYZING)
        self.assertEqual(first.metadata["source_range"], {"start_s": 2, "end_s": 8})
        edited = default_presets_catalog()
        edited["Balanced (Default)"]["intensity"] = 99
        self.presets.save(edited)
        self.assertEqual(self.manager.get(first.id).metadata["preset"]["config"]["intensity"], 50)

    def test_request_range_or_source_change_creates_different_jobs(self):
        runtime = self.runtime()
        first = runtime.create_job(runtime.plan_vod("https://www.twitch.tv/videos/123", start=0, end=4))
        second = runtime.create_job(runtime.plan_vod("https://www.twitch.tv/videos/123", start=4, end=8))
        self.assertNotEqual(first.id, second.id)
        local = runtime.create_job(runtime.plan_local(self.source))
        self.source.write_bytes(b"changed" * 500)
        changed = runtime.create_job(runtime.plan_local(self.source))
        self.assertNotEqual(local.id, changed.id)

    def test_local_run_uses_shared_services_and_preserves_original(self):
        events = []
        runtime = self.runtime(on_event=events.append)
        job = runtime.create_job(runtime.plan_local(self.source))
        completed = runtime.run(job.id)
        self.assertEqual(completed.state, PipelineState.DONE)
        self.assertTrue(self.source.exists())
        self.assertEqual((FakeDownloader.calls, FakeAnalyzer.calls, FakeExporter.calls), (0, 1, 1))
        self.assertIn("validation", {event["stage"] for event in events})
        self.assertEqual(runtime.run(job.id).state, PipelineState.DONE)
        self.assertEqual(FakeExporter.calls, 1)

    def test_vod_run_cleans_only_owned_download_after_done(self):
        runtime = self.runtime()
        job = runtime.create_job(runtime.plan_vod("https://www.twitch.tv/videos/123"))
        completed = runtime.run(job.id)
        self.assertEqual(completed.state, PipelineState.DONE)
        self.assertFalse(Path(completed.local_source_path).exists())
        self.assertTrue(Path(completed.export_path).exists())
        self.assertTrue(self.manager.get(job.id).metadata["source_cleaned"])

    def test_keep_source_keeps_download(self):
        runtime = self.runtime()
        job = runtime.create_job(runtime.plan_vod("https://www.twitch.tv/videos/123", keep_source=True))
        self.assertTrue(Path(runtime.run(job.id).local_source_path).exists())

    def test_partial_export_recovery_reuses_analysis_and_reserved_destination(self):
        runtime = self.runtime()
        job = runtime.create_job(runtime.plan_local(self.source))
        analyzed = runtime._stage(job, "analysis", runtime._analysis_factory, AnalysisCancellation(), PipelineCancellation())
        from automation.paths import reserve_output
        output = reserve_output(self.manager, analyzed)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"corrupt")
        self.manager.start_export(job.id)
        recovered = PipelineManager(PipelineStore(self.manager.store.path))
        recovered.recover_interrupted_jobs()
        done = self.runtime().run(job.id)
        self.assertEqual(done.state, PipelineState.DONE)
        self.assertEqual(Path(done.export_path), output)
        self.assertEqual(FakeAnalyzer.calls, 1)
        self.assertGreater(output.stat().st_size, 1024)

    def test_missing_project_reanalysis_and_corrupt_source_redownload(self):
        runtime = self.runtime()
        job = runtime.create_job(runtime.plan_vod("https://www.twitch.tv/videos/123", keep_source=True))
        downloaded = PipelineDownloadService(self.manager, downloader_factory=FakeDownloader).execute(job.id)
        analyzed = PipelineAnalysisService(self.manager, analyzer_factory=FakeAnalyzer).execute(job.id)
        Path(analyzed.project_path).unlink()
        Path(downloaded.local_source_path).write_bytes(b"partial")
        done = runtime.run(job.id)
        self.assertEqual(done.state, PipelineState.DONE)
        self.assertEqual((FakeDownloader.calls, FakeAnalyzer.calls), (2, 2))

    def test_existing_output_gets_suffix_and_source_collision_is_rejected(self):
        runtime = self.runtime()
        destination = self.root / "different drive" / "video.mp4"
        destination.parent.mkdir()
        destination.write_bytes(b"existing user video")
        job = runtime.create_job(runtime.plan_local(self.source, output=destination))
        done = runtime.run(job.id)
        self.assertEqual(Path(done.export_path).name, "video_2.mp4")
        self.assertEqual(destination.read_bytes(), b"existing user video")
        with self.assertRaises(PipelineRuntimeError):
            runtime.plan_local(self.source, output=self.source, force=True)

    def test_cancellation_stops_stage_marks_cancelled_and_explicit_resume_works(self):
        started = threading.Event()
        errors = []

        class BlockingAnalyzer:
            def analyze(self, job, *, cancellation=None, on_progress=None):
                started.set()
                while not cancellation.is_cancelled:
                    time.sleep(.005)
                raise PipelineAnalysisCancelled()

        runtime = self.runtime(analysis_service_factory=lambda manager: PipelineAnalysisService(manager, analyzer_factory=BlockingAnalyzer))
        job = runtime.create_job(runtime.plan_local(self.source))
        token = PipelineCancellation()

        def run():
            try:
                runtime.run(job.id, cancellation=token)
            except PipelineRuntimeError as exc:
                errors.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(started.wait(2))
        with self.assertRaises(PipelineJobBusyError):
            self.runtime().run(job.id)
        other_source = self.root / "different.mp4"
        other_source.write_bytes(b"video" * 500)
        other = self.runtime().create_job(self.runtime().plan_local(other_source))
        self.assertEqual(self.runtime().run(other.id).state, PipelineState.DONE)
        token.cancel()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors[0].exit_code, 70)
        self.assertEqual(self.manager.get(job.id).state, PipelineState.CANCELLED)
        self.assertEqual(self.runtime().run(job.id, resume=True).state, PipelineState.DONE)

    def test_completed_output_is_validated_before_reuse(self):
        runtime = self.runtime()
        job = runtime.create_job(runtime.plan_local(self.source))
        done = runtime.run(job.id)
        Path(done.export_path).write_bytes(b"partial")
        with self.assertRaises(PipelineRuntimeError) as caught:
            runtime.run(job.id)
        self.assertEqual(caught.exception.exit_code, 30)

    def test_project_recovery_rejects_wrong_tracks_job_snapshot_and_changed_source(self):
        runtime = self.runtime()
        job = runtime.create_job(runtime.plan_local(self.source))
        analyzed = PipelineAnalysisService(self.manager, analyzer_factory=FakeAnalyzer).execute(job.id)
        project = Path(analyzed.project_path)
        original = json.loads(project.read_text(encoding="utf-8"))
        for change in ("track", "job", "preset"):
            changed = json.loads(json.dumps(original))
            if change == "track":
                changed["tracks"][0]["path"] = "missing.mp4"
            elif change == "job":
                changed["automation"]["job_id"] = "another-job"
            else:
                changed["automation"]["analysis_request_signature"] = "wrong-snapshot"
            project.write_text(json.dumps(changed), encoding="utf-8")
            self.assertFalse(runtime._valid_project(analyzed), change)
        project.write_text(json.dumps(original), encoding="utf-8")
        self.source.write_bytes(b"changed playable source" * 200)
        self.assertFalse(runtime._valid_project(analyzed))
        done = runtime.run(job.id)
        self.assertEqual(done.state, PipelineState.DONE)
        self.assertEqual(FakeAnalyzer.calls, 2)

    def test_delivered_artifact_rejects_missing_manifest_fingerprint_signature_and_audio(self):
        runtime = self.runtime(audio_probe=lambda _path: False)
        job = runtime.create_job(runtime.plan_local(self.source))
        done = runtime.run(job.id)
        path = Path(done.export_path)
        sidecar = path.with_suffix(path.suffix + ".automation.json")
        original = json.loads(sidecar.read_text(encoding="utf-8"))
        sidecar.unlink()
        self.assertFalse(runtime._valid_export(done))
        for key, value in (("output_size", 999), ("output_mtime_ns", 999),
                           ("signature", "wrong-render"), ("duration_s", float("nan")),
                           ("require_audio", True)):
            changed = {**original, key: value}
            sidecar.write_text(json.dumps(changed), encoding="utf-8")
            self.assertFalse(runtime._valid_export(done), key)
        sidecar.write_text(json.dumps(original), encoding="utf-8")
        self.assertTrue(runtime._valid_export(done))

    def test_legacy_finalized_export_manifest_still_requires_duration_validation(self):
        runtime = self.runtime()
        job = runtime.create_job(runtime.plan_local(self.source))
        done = runtime.run(job.id)
        sidecar = Path(done.export_path + ".automation.json")
        manifest = json.loads(sidecar.read_text(encoding="utf-8"))
        manifest.pop("output_size")
        manifest.pop("output_mtime_ns")
        sidecar.write_text(json.dumps(manifest), encoding="utf-8")
        self.assertTrue(runtime._valid_export(done))
        manifest["duration_s"] = 100
        sidecar.write_text(json.dumps(manifest), encoding="utf-8")
        self.assertFalse(runtime._valid_export(done))

    def test_unknown_interrupted_upload_outcome_blocks_blind_retry(self):
        runtime = self.runtime()
        job = runtime.create_job(runtime.plan_local(self.source, youtube=True))
        analyzed = PipelineAnalysisService(self.manager, analyzer_factory=FakeAnalyzer).execute(job.id)
        exported = PipelineExportService(self.manager, exporter_factory=FakeExporter).execute(analyzed.id)
        self.manager.set_upload_metadata(exported.id, title="private")
        self.manager.start_upload(exported.id)
        Path(exported.export_path).write_bytes(b"partial")
        with self.assertRaises(PipelineRuntimeError) as caught:
            runtime.run(job.id)
        self.assertEqual(caught.exception.code, "upload_outcome_unknown")
        failed = self.manager.get(job.id)
        self.assertEqual(failed.state, PipelineState.FAILED)
        self.assertEqual(failed.retry_state, PipelineState.UPLOADING)
        with self.assertRaises(PipelineRuntimeError) as repeated:
            runtime.run(job.id, resume=True)
        self.assertEqual(repeated.exception.code, "upload_outcome_unknown")
        self.assertEqual(FakeExporter.calls, 1)

    def test_output_without_extension_uses_current_container(self):
        plan = self.runtime(settings_loader=lambda: {"container": "mkv"}).plan_local(self.source, output=self.root / "film")
        self.assertEqual(Path(plan["delivery"]["output_path"]).suffix, ".mkv")

    def test_startup_finishes_ready_export_delivery_with_explicit_upload_optout(self):
        runtime = self.runtime()
        job = runtime.create_job(runtime.plan_local(self.source))
        analyzed = PipelineAnalysisService(self.manager, analyzer_factory=FakeAnalyzer).execute(job.id)
        PipelineExportService(self.manager, exporter_factory=FakeExporter).execute(analyzed.id)
        restored = PipelineManager(PipelineStore(self.manager.store.path))
        with patch("automation.exporter.PipelineProjectExporter.validate_artifact", return_value={"signature": "valid"}):
            self.assertEqual(restored.reconstruct_queues(None, None, None), [])
        self.assertEqual(restored.get(job.id).state, PipelineState.DONE)
        self.assertIsNone(restored.get(job.id).youtube_video_id)

    def test_startup_invalid_optout_export_is_requeued_without_marking_done(self):
        runtime = self.runtime()
        job = runtime.create_job(runtime.plan_local(self.source))
        analyzed = PipelineAnalysisService(self.manager, analyzer_factory=FakeAnalyzer).execute(job.id)
        exported = PipelineExportService(self.manager, exporter_factory=FakeExporter).execute(analyzed.id)
        Path(exported.export_path).write_bytes(b"partial")
        exports = []
        restored = PipelineManager(PipelineStore(self.manager.store.path))
        with patch("utils.ffmpeg.ffprobe_duration_seconds", return_value=10.0), patch("utils.ffmpeg.has_video_stream", return_value=True):
            queued = restored.reconstruct_queues(None, None, exports.append)
        self.assertEqual(exports, [job.id])
        self.assertEqual(len(queued), 1)
        self.assertEqual(restored.get(job.id).state, PipelineState.READY_EXPORT)

    def test_startup_shared_reconciliation_requeues_missing_project_for_analysis(self):
        runtime = self.runtime()
        job = runtime.create_job(runtime.plan_local(self.source))
        analyzed = PipelineAnalysisService(self.manager, analyzer_factory=FakeAnalyzer).execute(job.id)
        Path(analyzed.project_path).unlink()
        analysis, exports = [], []
        with patch("utils.ffmpeg.ffprobe_duration_seconds", return_value=10.0), patch("utils.ffmpeg.has_video_stream", return_value=True):
            queued = self.manager.reconstruct_queues(None, analysis.append, exports.append)
        self.assertEqual(analysis, [job.id])
        self.assertEqual(exports, [])
        self.assertEqual(queued[0].state, PipelineState.ANALYZING)

    def test_startup_shared_reconciliation_redownloads_invalid_remote_source(self):
        runtime = self.runtime()
        job = runtime.create_job(runtime.plan_vod("https://www.twitch.tv/videos/123"))
        downloaded = PipelineDownloadService(self.manager, downloader_factory=FakeDownloader).execute(job.id)
        Path(downloaded.local_source_path).write_bytes(b"partial")
        downloads, analysis = [], []
        self.manager.reconstruct_queues(downloads.append, analysis.append, None)
        self.assertEqual(downloads, [job.id])
        self.assertEqual(analysis, [])
        self.assertEqual(self.manager.get(job.id).state, PipelineState.DOWNLOADING)

    def test_startup_shared_reconciliation_never_redownloads_missing_local_original(self):
        job = self.runtime().create_job(self.runtime().plan_local(self.source))
        self.source.unlink()
        downloads = []
        self.assertEqual(self.manager.reconstruct_queues(downloads.append, None, None), [])
        self.assertEqual(downloads, [])
        failed = self.manager.get(job.id)
        self.assertEqual(failed.state, PipelineState.FAILED)
        self.assertEqual(failed.error_code, "source_missing")

    def test_startup_recovers_valid_export_after_crash_before_mark_without_source(self):
        from automation.paths import reserve_output
        runtime = self.runtime()
        job = runtime.create_job(runtime.plan_local(self.source))
        analyzed = PipelineAnalysisService(self.manager, analyzer_factory=FakeAnalyzer).execute(job.id)
        reserve_output(self.manager, analyzed)
        self.manager.start_export(job.id)
        rendered = FakeExporter().export(self.manager.get(job.id), cancellation=ExportCancellation(), on_progress=lambda value: None)
        self.manager.update_metadata(job.id, {"export_artifact": {"signature": "fake-render-signature"}})
        self.source.unlink()
        Path(analyzed.project_path).unlink()
        exports = []
        with patch("utils.ffmpeg.ffprobe_duration_seconds", return_value=10.0), patch("utils.ffmpeg.has_video_stream", return_value=True):
            self.manager.reconstruct_queues(None, None, exports.append)
        self.assertEqual(exports, [])
        recovered = self.manager.get(job.id)
        self.assertEqual(recovered.state, PipelineState.DONE)
        self.assertEqual(recovered.export_path, str(rendered.path))

    def test_graceful_shutdown_cancellation_failure_becomes_auto_resumable_interruption(self):
        runtime = self.runtime()
        job = runtime.create_job(runtime.plan_local(self.source))
        self.manager.fail(job.id, "analysis_cancelled", "Automatic analysis was cancelled.")
        interrupted = self.manager.interrupt(job.id)
        self.assertEqual(interrupted.error_code, "interrupted")
        self.assertEqual(interrupted.retry_state, PipelineState.ANALYZING)
        self.assertEqual(self.manager.interrupt(job.id).to_mapping(), interrupted.to_mapping())
        analysis = []
        with patch("utils.ffmpeg.ffprobe_duration_seconds", return_value=10.0), patch("utils.ffmpeg.has_video_stream", return_value=True):
            self.manager.reconstruct_queues(None, analysis.append, None)
        self.assertEqual(analysis, [job.id])

    def test_graceful_shutdown_does_not_resume_explicit_user_cancel(self):
        job = self.runtime().create_job(self.runtime().plan_local(self.source))
        self.manager.cancel(job.id)
        cancelled = self.manager.get(job.id)
        self.assertEqual(self.manager.interrupt(job.id).to_mapping(), cancelled.to_mapping())
        self.assertEqual(self.manager.reconstruct_queues(lambda id: True, lambda id: True, lambda id: True), [])

    def test_known_youtube_id_allows_thumbnail_resume_without_local_output(self):
        runtime = self.runtime()
        job = runtime.create_job(runtime.plan_local(self.source, youtube=True))
        analyzed = PipelineAnalysisService(self.manager, analyzer_factory=FakeAnalyzer).execute(job.id)
        exported = PipelineExportService(self.manager, exporter_factory=FakeExporter).execute(analyzed.id)
        self.manager.set_upload_metadata(job.id, title="Uploaded video")
        self.manager.start_upload(job.id)
        self.manager.record_youtube_video_id(job.id, "existing-id")
        Path(exported.export_path).unlink()
        self.manager.interrupt(job.id)
        self.manager.retry(job.id)
        # The recovery map sends UPLOADING back to READY_UPLOAD.
        self.assertEqual(self.manager.start_upload(job.id).state, PipelineState.UPLOADING)

    def test_late_upload_id_after_user_cancel_is_persisted_without_resurrecting_job(self):
        runtime = self.runtime()
        job = runtime.create_job(runtime.plan_local(self.source, youtube=True))
        analyzed = PipelineAnalysisService(self.manager, analyzer_factory=FakeAnalyzer).execute(job.id)
        PipelineExportService(self.manager, exporter_factory=FakeExporter).execute(analyzed.id)
        self.manager.set_upload_metadata(job.id, title="Private video")
        self.manager.start_upload(job.id)
        self.manager.cancel(job.id)
        accepted = self.manager.record_youtube_video_id(job.id, "late-known-id")
        self.assertEqual(accepted.state, PipelineState.CANCELLED)
        self.assertEqual(accepted.youtube_video_id, "late-known-id")
        self.assertEqual(self.manager.resume_cancelled(job.id).youtube_video_id, "late-known-id")

    def test_runtime_completes_already_rendered_export_without_requiring_deleted_inputs(self):
        from automation.paths import reserve_output
        runtime = self.runtime()
        job = runtime.create_job(runtime.plan_local(self.source))
        analyzed = PipelineAnalysisService(self.manager, analyzer_factory=FakeAnalyzer).execute(job.id)
        reserve_output(self.manager, analyzed)
        self.manager.start_export(job.id)
        FakeExporter().export(self.manager.get(job.id), cancellation=ExportCancellation(), on_progress=lambda value: None)
        self.manager.update_metadata(job.id, {"export_artifact": {"signature": "fake-render-signature"}})
        self.source.unlink()
        Path(analyzed.project_path).unlink()
        completed = runtime.run(job.id)
        self.assertEqual(completed.state, PipelineState.DONE)
        self.assertEqual(FakeExporter.calls, 1)

    def test_video_id_upload_resume_uses_same_job_without_new_download_or_export(self):
        uploads = []

        class FakeUploadService:
            def __init__(self, manager):
                self.manager = manager

            def execute(self, job_id, *, cancellation=None, on_progress=None):
                request = self.manager.get(job_id)
                self.manager.set_upload_metadata(job_id, title=request.metadata["delivery"]["title"])
                current = self.manager.start_upload(job_id)
                if not current.youtube_video_id:
                    uploads.append(job_id)
                    self.manager.record_youtube_video_id(job_id, "existing-video-id")
                return self.manager.mark_uploaded(job_id, "existing-video-id")

        runtime = self.runtime(upload_service_factory=FakeUploadService)
        job = runtime.create_job(runtime.plan_local(self.source, youtube=True))
        done = runtime.run(job.id)
        self.assertEqual(done.youtube_video_id, "existing-video-id")
        runtime.run(job.id, resume=True)
        self.assertEqual(len(uploads), 1)

    def test_runtime_import_without_gui_widgets(self):
        result = subprocess.run([sys.executable, "-c", "import sys; import automation.runtime; assert 'PySide6.QtWidgets' not in sys.modules; assert 'ui.main_window' not in sys.modules"], cwd=str(Path(__file__).resolve().parents[1]), capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_data_dir_override_applies_to_job_storage(self):
        with patch.dict(os.environ, {"AUTO_CUTTER_DATA_DIR": str(self.root / "isolated data"), "AUTO_CUTTER_PIPELINE_STORE": ""}):
            self.assertEqual(default_pipeline_store_path(), self.root / "isolated data" / "pipeline" / "jobs.json")

    def test_public_metadata_uses_same_resolver_extract_and_strips_stream_secrets(self):
        class Downloader:
            def __init__(self, options):
                self.options = options
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return None
            def extract_info(self, url, *, download):
                self_download = download
                self.assertion = self_download is False
                return {"id": "123", "title": "VOD", "duration": 10,
                        "url": "https://cdn/?secret=secret", "http_headers": {"Authorization": "secret"}}
        from types import SimpleNamespace
        metadata = YtDlpMediaResolver(lambda: SimpleNamespace(YoutubeDL=Downloader)).metadata("https://www.twitch.tv/videos/123")
        self.assertEqual(metadata["duration_s"], 10)
        self.assertNotIn("secret", json.dumps(metadata))


if __name__ == "__main__":
    unittest.main()
