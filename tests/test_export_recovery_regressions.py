from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from automation.exporter import (
    ExportCancellation,
    PipelineExportCancelled,
    PipelineExportError,
    PipelineProjectExporter,
    PipelineExportService,
    ProjectExportPlanner,
)
from automation.locking import FileLockBusyError
from automation.manager import PipelineManager
from automation.models import PipelineJob, PipelineState
from automation.paths import output_execution_lock
from automation.store import PipelineStore
from export.output_safety import atomic_promote_output, choose_output_path
from export.settings import ExportSettings
from utils.codec_detection import CodecSelection


class Signal:
    def __init__(self) -> None:
        self.callbacks = []

    def connect(self, callback) -> None:
        self.callbacks.append(callback)

    def emit(self, *args) -> None:
        for callback in self.callbacks:
            callback(*args)


class Worker:
    def __init__(self, **kwargs) -> None:
        self.output = Path(kwargs["output_path"])
        self.progress = Signal()
        self.detail = Signal()
        self.error = Signal()
        self.debug = False

    def run(self) -> None:
        self.output.write_bytes(b"valid-render" * 300)

    def cancel(self) -> None:
        pass


class ExportRecoveryRegressions(unittest.TestCase):
    def make_job(self, root: Path, delivery: dict | None = None) -> PipelineJob:
        source = root / "source.mp4"
        source.write_bytes(b"protected-source")
        project = root / "source.autocutter"
        settings = ExportSettings(codec="libx264", method="filter_concat", hwaccel_decode=False)
        project.write_text(
            json.dumps({
                "format": "autocutter_project",
                "version": 2,
                "global": {"export_settings": settings.to_mapping()},
                "tracks": [{"path": str(source), "duration": 8.0, "cuts_enabled": False}],
            }),
            encoding="utf-8",
        )
        job = PipelineJob.create(vod_id="test-vod", vod_url="https://example.invalid/vod")
        job.local_source_path = str(source)
        job.project_path = str(project)
        if delivery is not None:
            job.metadata["delivery"] = delivery
        return job

    def make_exporter(self, *, worker_factory=Worker, duration_probe=None, video_probe=None, audio_probe=None):
        return PipelineProjectExporter(
            planner=ProjectExportPlanner(audio_probe=lambda _path: True),
            worker_factory=worker_factory,
            ffmpeg_provider=lambda: ("ffmpeg", "ffprobe"),
            codec_resolver=lambda _ffmpeg, requested: CodecSelection(str(requested), "libx264"),
            duration_probe=duration_probe or (lambda _path: 8.0),
            video_probe=video_probe or (lambda _path: True),
            audio_probe=audio_probe or (lambda _path: True),
        )

    def test_matching_sidecar_with_truncated_cache_renders_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job = self.make_job(Path(tmp))
            exporter = self.make_exporter()
            first = exporter.export(job)
            first.path.write_bytes(b"truncated")

            recovered = exporter.export(job)

            self.assertFalse(recovered.reused)
            self.assertEqual(recovered.path, first.path)
            self.assertGreater(recovered.path.stat().st_size, 1024)
            self.assertTrue(exporter.export(job).reused)

    def test_invalid_cached_streams_duration_and_probe_errors_are_cache_misses(self) -> None:
        for defect in ("video", "audio", "duration", "probe"):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                job = self.make_job(root)
                output = self.make_exporter().export(job).path

                def duration(path: str) -> float:
                    if Path(path) == output and defect == "probe":
                        raise RuntimeError("damaged container")
                    return 100.0 if Path(path) == output and defect == "duration" else 8.0

                exporter = self.make_exporter(
                    duration_probe=duration,
                    video_probe=lambda path: not (Path(path) == output and defect == "video"),
                    audio_probe=lambda path: not (Path(path) == output and defect == "audio"),
                )

                self.assertFalse(exporter.export(job).reused)

    def test_cancel_during_validation_preserves_existing_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job = self.make_job(root)
            output = root / "source.youtube-ready.mp4"
            output.write_bytes(b"previous-output")
            token = ExportCancellation()

            def probe(_path: str) -> float:
                token.cancel()
                return 8.0

            with self.assertRaises(PipelineExportCancelled):
                self.make_exporter(duration_probe=probe).export(job, cancellation=token)

            self.assertEqual(output.read_bytes(), b"previous-output")
            self.assertEqual(list(root.glob(".*.partial*")), [])

    def test_failed_render_preserves_existing_delivery(self) -> None:
        class FailingWorker(Worker):
            def run(self) -> None:
                self.output.write_bytes(b"incomplete")
                self.error.emit("render failed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job = self.make_job(root)
            output = root / "source.youtube-ready.mp4"
            output.write_bytes(b"previous-output")

            with self.assertRaises(PipelineExportError) as caught:
                self.make_exporter(worker_factory=FailingWorker).export(job)

            self.assertEqual(caught.exception.code, "render_failed")
            self.assertEqual(output.read_bytes(), b"previous-output")
            self.assertEqual(list(root.glob(".*.partial*")), [])

    def test_source_project_and_hardlink_collisions_never_start_worker(self) -> None:
        for collision in ("source", "project", "hardlink"):
            with self.subTest(collision=collision), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                job = self.make_job(root)
                protected = Path(job.project_path if collision == "project" else job.local_source_path)
                requested = protected
                if collision == "hardlink":
                    requested = root / "alias.mp4"
                    try:
                        os.link(protected, requested)
                    except OSError as exc:
                        self.skipTest(f"Hardlinks unavailable: {exc}")
                before = protected.read_bytes()
                job.metadata["delivery"] = {"output_path": str(requested), "force": True}
                calls = []

                def create_worker(**kwargs):
                    calls.append(kwargs)
                    return Worker(**kwargs)

                with self.assertRaises(PipelineExportError) as caught:
                    self.make_exporter(worker_factory=create_worker).export(job)

                self.assertEqual(caught.exception.code, "source_collision")
                self.assertEqual(calls, [])
                self.assertEqual(protected.read_bytes(), before)

    def test_source_collision_is_rechecked_after_render(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job = self.make_job(root)
            output = root / "source.youtube-ready.mp4"
            source = Path(job.local_source_path)

            class RacedWorker(Worker):
                def run(self) -> None:
                    super().run()
                    os.link(source, output)

            with self.assertRaises(PipelineExportError) as caught:
                self.make_exporter(worker_factory=RacedWorker).export(job)

            self.assertEqual(caught.exception.code, "source_collision")
            self.assertEqual(source.read_bytes(), b"protected-source")

    def test_override_avoids_existing_file_and_sidecar_and_keeps_cache_choice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            requested = root / "custom.mp4"
            requested.write_bytes(b"existing-delivery")
            second_sidecar = root / "custom_2.mp4.automation.json"
            second_sidecar.write_bytes(b"existing-sidecar")
            job = self.make_job(root, {"output_path": str(requested), "force": False})
            exporter = self.make_exporter()

            first = exporter.export(job)
            cached = exporter.export(job)

            self.assertEqual(first.path.name, "custom_3.mp4")
            self.assertEqual(job.metadata["delivery"]["resolved_output_path"], str(first.path))
            self.assertTrue(cached.reused)
            self.assertEqual(requested.read_bytes(), b"existing-delivery")
            self.assertEqual(second_sidecar.read_bytes(), b"existing-sidecar")

    def test_force_override_replaces_requested_output_after_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            requested = root / "custom.mp4"
            requested.write_bytes(b"previous-output")
            job = self.make_job(root, {"output_path": str(requested), "force": True})

            result = self.make_exporter().export(job)

            self.assertEqual(result.path, requested)
            self.assertGreater(requested.stat().st_size, 1024)

    def test_output_directory_override_and_durable_path_are_honored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / "deliveries"
            job = self.make_job(root, {"output_dir": str(directory)})
            result = self.make_exporter().export(job)
            self.assertEqual(result.path, directory / "source.youtube-ready.mp4")
            chosen = root / "durably-reserved.mp4"
            job.metadata["delivery"]["resolved_output_path"] = str(chosen)
            result = self.make_exporter().export(job)
            self.assertEqual(result.path, chosen)

    def test_no_force_publish_cannot_clobber_a_racing_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = choose_output_path(root / "delivery.mp4")
            temporary = root / ".delivery.partial.mp4"
            temporary.write_bytes(b"validated-render")
            output.write_bytes(b"racing-delivery")

            with self.assertRaises(FileExistsError):
                atomic_promote_output(temporary, output, overwrite=False)

            self.assertEqual(output.read_bytes(), b"racing-delivery")
            self.assertEqual(temporary.read_bytes(), b"validated-render")

    def test_owned_override_with_corrupt_cache_is_repaired_at_reserved_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job = self.make_job(root, {"output_path": str(root / "custom.mp4"), "force": False})
            exporter = self.make_exporter()
            first = exporter.export(job)
            first.path.write_bytes(b"corrupt")

            recovered = exporter.export(job)

            self.assertFalse(recovered.reused)
            self.assertEqual(recovered.path, first.path)
            self.assertTrue(exporter.export(job).reused)

    def test_legacy_sidecar_without_file_identity_can_still_be_reused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job = self.make_job(root)
            exporter = self.make_exporter()
            first = exporter.export(job)
            sidecar = first.path.with_suffix(".mp4.automation.json")
            metadata = json.loads(sidecar.read_text(encoding="utf-8"))
            metadata.pop("output_size")
            metadata.pop("output_mtime_ns")
            sidecar.write_text(json.dumps(metadata), encoding="utf-8")

            self.assertTrue(exporter.export(job).reused)

    def test_crash_between_manifest_and_video_does_not_reuse_previous_video(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job = self.make_job(root)
            exporter = self.make_exporter()
            first = exporter.export(job)
            os.utime(first.path, ns=(1_000_000_000, 1_000_000_000))
            old_video = first.path.read_bytes()
            project = Path(job.project_path)
            payload = json.loads(project.read_text(encoding="utf-8"))
            payload["revision"] = "new-render"
            project.write_text(json.dumps(payload), encoding="utf-8")

            with patch("automation.exporter.atomic_promote_output", side_effect=RuntimeError("power failure")):
                with self.assertRaises(PipelineExportError):
                    exporter.export(job)

            self.assertEqual(first.path.read_bytes(), old_video)
            self.assertEqual(list(root.glob(".*.partial*")), [])
            # The manifest describes the new partial's mtime, not this old final.
            self.assertFalse(exporter.export(job).reused)
            self.assertTrue(exporter.export(job).reused)

    def test_legacy_job_preserves_foreign_output_and_recovers_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job = self.make_job(root)
            previous = root / "source.youtube-ready.mp4"
            previous.write_bytes(b"user-video")
            exporter = self.make_exporter()
            result = exporter.export(job)
            self.assertEqual(result.path, root / "source.youtube-ready_2.mp4")
            self.assertEqual(previous.read_bytes(), b"user-video")
            self.assertTrue(exporter.export(job).reused)

    def test_failed_manifest_write_never_promotes_the_video(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job = self.make_job(root)
            output = root / "source.youtube-ready.mp4"
            output.write_bytes(b"previous-video")
            exporter = self.make_exporter()

            with patch.object(
                exporter,
                "_write_sidecar",
                side_effect=PipelineExportError("export_metadata_failed", "disk full"),
            ):
                with self.assertRaises(PipelineExportError):
                    exporter.export(job)

            self.assertEqual(output.read_bytes(), b"previous-video")
            self.assertEqual(list(root.glob(".*.partial*")), [])

    def test_foreign_video_after_pending_manifest_is_not_overwritten_on_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "delivery.mp4"
            job = self.make_job(root, {"output_path": str(output), "force": False})
            exporter = self.make_exporter()

            def raced_promote(temporary: Path, destination: Path, *, overwrite: bool) -> None:
                if destination == output:
                    output.write_bytes(b"foreign-user-video" * 200)
                atomic_promote_output(temporary, destination, overwrite=overwrite)

            with patch("automation.exporter.atomic_promote_output", side_effect=raced_promote):
                with self.assertRaises(PipelineExportError) as caught:
                    exporter.export(job)
            self.assertEqual(caught.exception.code, "output_exists")
            foreign = output.read_bytes()
            sidecar = output.with_suffix(".mp4.automation.json")
            before = sidecar.read_bytes()
            self.assertTrue(json.loads(before)["promotion_pending"])
            with self.assertRaises(PipelineExportError) as caught:
                exporter.export(job)
            self.assertEqual(caught.exception.code, "output_exists")
            self.assertEqual(output.read_bytes(), foreign)
            self.assertEqual(sidecar.read_bytes(), before)

    def test_foreign_sidecar_created_during_publish_is_never_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "delivery.mp4"
            sidecar = output.with_suffix(".mp4.automation.json")
            job = self.make_job(root, {"output_path": str(output), "force": False})

            def raced_promote(temporary: Path, destination: Path, *, overwrite: bool) -> None:
                if destination == sidecar:
                    sidecar.write_bytes(b"foreign-user-metadata")
                atomic_promote_output(temporary, destination, overwrite=overwrite)

            with patch("automation.exporter.atomic_promote_output", side_effect=raced_promote):
                with self.assertRaises(PipelineExportError) as caught:
                    self.make_exporter().export(job)
            self.assertEqual(caught.exception.code, "output_exists")
            self.assertEqual(sidecar.read_bytes(), b"foreign-user-metadata")
            self.assertFalse(output.exists())
            self.assertEqual(list(root.glob(".*.partial*")), [])

    def test_promoted_pending_export_recovers_cache_and_finalizes_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "delivery.mp4"
            job = self.make_job(root, {"output_path": str(output)})
            exporter = self.make_exporter()
            original = exporter._write_sidecar

            def crash_after_promotion(*args, **kwargs) -> None:
                if kwargs.get("promotion_pending") is False:
                    raise RuntimeError("process died after video promotion")
                original(*args, **kwargs)

            with patch.object(exporter, "_write_sidecar", side_effect=crash_after_promotion):
                with self.assertRaises(PipelineExportError):
                    exporter.export(job)
            sidecar = output.with_suffix(".mp4.automation.json")
            self.assertTrue(json.loads(sidecar.read_text(encoding="utf-8"))["promotion_pending"])
            recovered = exporter.export(job)
            self.assertTrue(recovered.reused)
            self.assertFalse(json.loads(sidecar.read_text(encoding="utf-8"))["promotion_pending"])

    def test_cancelled_pending_manifest_without_final_allows_clean_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "delivery.mp4"
            job = self.make_job(root, {"output_path": str(output)})
            exporter = self.make_exporter()
            original = exporter._write_sidecar
            token = ExportCancellation()

            def cancel_after_manifest(*args, **kwargs) -> None:
                original(*args, **kwargs)
                token.cancel()

            with patch.object(exporter, "_write_sidecar", side_effect=cancel_after_manifest):
                with self.assertRaises(PipelineExportCancelled):
                    exporter.export(job, cancellation=token)
            self.assertFalse(output.exists())
            recovered = exporter.export(job)
            self.assertFalse(recovered.reused)
            self.assertTrue(output.is_file())

    def test_interrupted_corrupt_owned_output_repair_retries_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "delivery.mp4"
            job = self.make_job(root, {"output_path": str(output), "force": False})
            exporter = self.make_exporter()
            original_result = exporter.export(job)
            original_result.path.write_bytes(b"corrupt-owned-output")
            corrupt_stat = output.stat()
            token = ExportCancellation()
            original = exporter._write_sidecar

            def cancel_during_repair(*args, **kwargs) -> None:
                original(*args, **kwargs)
                token.cancel()

            with patch.object(exporter, "_write_sidecar", side_effect=cancel_during_repair):
                with self.assertRaises(PipelineExportCancelled):
                    exporter.export(job, cancellation=token)
            sidecar = output.with_suffix(".mp4.automation.json")
            pending = json.loads(sidecar.read_text(encoding="utf-8"))
            self.assertTrue(pending["promotion_pending"])
            self.assertEqual(pending["previous_owned_output_identity"], {
                "output_size": corrupt_stat.st_size, "output_mtime_ns": corrupt_stat.st_mtime_ns,
            })
            recovered = exporter.export(job)
            self.assertEqual(recovered.path, original_result.path)
            self.assertFalse(recovered.reused)
            self.assertGreater(output.stat().st_size, 1024)
            final = json.loads(sidecar.read_text(encoding="utf-8"))
            self.assertFalse(final["promotion_pending"])
            self.assertNotIn("previous_owned_output_identity", final)
            self.assertTrue(exporter.export(job).reused)

    def test_legacy_service_reserves_and_locks_destination_before_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = PipelineManager(PipelineStore(root / "jobs.json"))
            job = self.make_job(root)
            job.state = PipelineState.READY_EXPORT
            manager.store.upsert(job)
            expected = root / "source.youtube-ready.mp4"
            outer = self

            class CheckReservationWorker(Worker):
                def run(self) -> None:
                    snapshot = manager.get(job.id)
                    outer.assertEqual(snapshot.metadata["delivery"]["resolved_output_path"], str(expected))
                    with outer.assertRaises(FileLockBusyError):
                        with output_execution_lock(expected).hold(reentrant=False):
                            pass
                    super().run()

            service = PipelineExportService(
                manager, exporter_factory=lambda: self.make_exporter(worker_factory=CheckReservationWorker),
            )
            completed = service.execute(job.id)
            self.assertEqual(completed.state, PipelineState.READY_UPLOAD)
            self.assertEqual(completed.export_path, str(expected))

    def test_concurrent_legacy_jobs_reserve_different_destinations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = PipelineManager(PipelineStore(root / "jobs.json"))
            first = self.make_job(root)
            first.state = PipelineState.READY_EXPORT
            second = PipelineJob.from_mapping(first.to_mapping())
            second.id = "second-job"
            second.vod_id = "second-vod"
            manager.store.save([first, second])
            started = threading.Event()
            release = threading.Event()
            failures: list[BaseException] = []

            class BlockingWorker(Worker):
                def run(self) -> None:
                    started.set()
                    if not release.wait(3):
                        raise RuntimeError("test worker timed out")
                    super().run()

            service = PipelineExportService(
                manager, exporter_factory=lambda: self.make_exporter(worker_factory=BlockingWorker),
            )

            def execute_first() -> None:
                try:
                    service.execute(first.id)
                except BaseException as exc:
                    failures.append(exc)

            thread = threading.Thread(target=execute_first)
            thread.start()
            try:
                self.assertTrue(started.wait(2))
                other = PipelineExportService(manager, exporter_factory=self.make_exporter).execute(second.id)
                self.assertEqual(Path(other.export_path).name, "source.youtube-ready_2.mp4")
            finally:
                release.set()
                thread.join(timeout=4)
            self.assertFalse(thread.is_alive())
            self.assertEqual(failures, [])
            self.assertNotEqual(manager.get(first.id).export_path, manager.get(second.id).export_path)


if __name__ == "__main__":
    unittest.main()
