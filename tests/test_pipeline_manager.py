from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from automation.manager import (
    PipelineArtifactError,
    PipelineJobNotFoundError,
    PipelineManager,
)
from automation.models import PipelineState, PipelineTransitionError
from automation.store import PipelineStore


class PipelineManagerTests(unittest.TestCase):
    def make_manager(self, root: Path) -> PipelineManager:
        return PipelineManager(PipelineStore(root / "pipeline" / "jobs.json"))

    def discover(self, manager: PipelineManager, vod_id: str = "vod-1"):
        return manager.discover_vod(
            vod_id=vod_id,
            vod_url=f"https://www.twitch.tv/videos/{vod_id}",
            channel_id="channel-1",
            source_title=f"Live {vod_id}",
        )

    def test_discovery_is_idempotent_for_the_same_vod(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = self.make_manager(Path(tmp))

            first, first_created = self.discover(manager)
            second, second_created = self.discover(manager)

            self.assertTrue(first_created)
            self.assertFalse(second_created)
            self.assertEqual(second.id, first.id)
            self.assertEqual(len(manager.list_jobs()), 1)

    def test_complete_workflow_is_persisted_across_manager_instances(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.mp4"
            project = root / "edit.autocutter"
            exported = root / "export.mp4"
            thumbnail = root / "thumbnail.jpg"
            for artifact in (source, project, exported, thumbnail):
                artifact.write_bytes(b"data")

            manager = self.make_manager(root)
            job, _ = self.discover(manager)
            manager.request_range(job.id)
            manager.select_range(job.id, 60.0, 360.0)
            manager.update_progress(job.id, 50.0)
            manager.mark_downloaded(job.id, source)
            manager.update_progress(job.id, 100.0)
            manager.mark_analyzed(job.id, project)
            manager.start_export(job.id)
            manager.mark_exported(job.id, exported)
            manager.set_upload_metadata(
                job.id,
                title="Video pronto",
                description="Descrizione",
                thumbnail_path=thumbnail,
            )
            manager.start_upload(job.id)
            manager.update_progress(job.id, 100.0)
            manager.mark_uploaded(job.id, "youtube-123")

            reopened = self.make_manager(root).get(job.id)
            self.assertEqual(reopened.state, PipelineState.DONE)
            self.assertEqual(reopened.youtube_video_id, "youtube-123")
            self.assertEqual(reopened.range_start_s, 60.0)
            self.assertEqual(reopened.range_end_s, 360.0)
            self.assertEqual(reopened.metadata["youtube"]["title"], "Video pronto")

    def test_missing_artifact_does_not_advance_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = self.make_manager(Path(tmp))
            job, _ = self.discover(manager)
            manager.request_range(job.id)
            manager.select_range(job.id, 0.0, 30.0)

            with self.assertRaises(PipelineArtifactError):
                manager.mark_downloaded(job.id, Path(tmp) / "missing.mp4")

            self.assertEqual(manager.get(job.id).state, PipelineState.DOWNLOADING)

    def test_failure_and_retry_are_durable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = self.make_manager(root)
            job, _ = self.discover(manager)
            manager.request_range(job.id)
            manager.select_range(job.id, 0.0, 30.0)
            failed = manager.fail(job.id, "network", "Connection lost")

            self.assertEqual(failed.state, PipelineState.FAILED)
            retried = self.make_manager(root).retry(job.id)
            self.assertEqual(retried.state, PipelineState.DOWNLOADING)
            self.assertEqual(retried.retry_count, 1)

    def test_restart_marks_only_active_work_as_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = self.make_manager(root)
            active, _ = self.discover(manager, "active")
            waiting, _ = self.discover(manager, "waiting")
            manager.request_range(active.id)
            manager.select_range(active.id, 0.0, 30.0)
            manager.request_range(waiting.id)

            recovered = self.make_manager(root).recover_interrupted_jobs()

            self.assertEqual([job.id for job in recovered], [active.id])
            recovered_job = manager.get(active.id)
            self.assertEqual(recovered_job.state, PipelineState.FAILED)
            self.assertEqual(recovered_job.retry_state, PipelineState.DOWNLOADING)
            self.assertEqual(manager.get(waiting.id).state, PipelineState.WAITING_RANGE)

    def test_upload_requires_user_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.mp4"
            project = root / "edit.autocutter"
            exported = root / "export.mp4"
            for artifact in (source, project, exported):
                artifact.write_bytes(b"data")
            manager = self.make_manager(root)
            job, _ = self.discover(manager)
            manager.request_range(job.id)
            manager.select_range(job.id, 0.0, 30.0)
            manager.mark_downloaded(job.id, source)
            manager.mark_analyzed(job.id, project)
            manager.start_export(job.id)
            manager.mark_exported(job.id, exported)

            with self.assertRaises(PipelineArtifactError):
                manager.start_upload(job.id)
            with self.assertRaises(PipelineArtifactError):
                manager.set_upload_metadata(job.id, title=" ")

            self.assertEqual(manager.get(job.id).state, PipelineState.READY_UPLOAD)

    def test_missing_job_and_invalid_order_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = self.make_manager(Path(tmp))
            job, _ = self.discover(manager)

            with self.assertRaises(PipelineJobNotFoundError):
                manager.get("missing")
            with self.assertRaises(PipelineTransitionError):
                manager.start_export(job.id)

    def test_cancelled_job_is_persisted_and_excluded_from_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = self.make_manager(Path(tmp))
            job, _ = self.discover(manager)

            cancelled = manager.cancel(job.id)

            self.assertEqual(cancelled.state, PipelineState.CANCELLED)
            self.assertEqual(manager.list_jobs(include_terminal=False), [])


if __name__ == "__main__":
    unittest.main()
