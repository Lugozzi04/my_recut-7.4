from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from automation.manager import PipelineManager
from automation.models import PipelineJob, PipelineState, PipelineTransitionError
from automation.store import PipelineStore


class PipelineResumeTests(unittest.TestCase):
    def manager(self, root: Path) -> PipelineManager:
        return PipelineManager(PipelineStore(root / "jobs.json"))

    def discover(self, manager: PipelineManager, vod_id: str) -> PipelineJob:
        return manager.discover_vod(vod_id=vod_id, vod_url=f"https://www.twitch.tv/videos/{vod_id}")[0]

    def ready_export(self, manager: PipelineManager, root: Path, vod_id: str) -> PipelineJob:
        job = self.discover(manager, vod_id)
        source = root / f"{vod_id}.mp4"
        project = root / f"{vod_id}.autocutter"
        source.write_bytes(b"source")
        project.write_bytes(b"project")
        manager.request_range(job.id)
        manager.select_range(job.id, 0.0, 30.0)
        manager.mark_downloaded(job.id, source)
        return manager.mark_analyzed(job.id, project)

    def ready_upload(self, manager: PipelineManager, root: Path, vod_id: str) -> PipelineJob:
        job = self.ready_export(manager, root, vod_id)
        output = root / f"{vod_id}.youtube-ready.mp4"
        output.write_bytes(b"export")
        manager.start_export(job.id)
        return manager.mark_exported(job.id, output)

    def test_metadata_merge_is_atomic_detached_and_can_replace_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = self.manager(root)
            second = self.manager(root)
            job = self.discover(first, "metadata")
            original = {"preset": {"threshold": 20, "cuts": {"minimum": 1}}, "delivery": {"youtube": False}}
            first.update_metadata(job.id, original)
            original["preset"]["threshold"] = 99
            updated = second.update_metadata(job.id, {"preset": {"cuts": {"padding": 0.1}}})
            self.assertEqual(updated.metadata["preset"]["threshold"], 20)
            self.assertEqual(updated.metadata["preset"]["cuts"], {"minimum": 1, "padding": 0.1})
            updated.metadata["delivery"]["youtube"] = True
            self.assertFalse(first.get(job.id).metadata["delivery"]["youtube"])
            replaced = first.update_metadata(job.id, {"preset": {"threshold": 30}}, deep_merge=False)
            self.assertEqual(replaced.metadata["preset"], {"threshold": 30})
            self.assertEqual(replaced.metadata["delivery"], {"youtube": False})

    def test_finish_without_upload_records_done_and_no_fake_video_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = self.manager(root)
            job = self.ready_upload(manager, root, "offline")
            finished = manager.complete_without_upload(job.id)
            self.assertEqual(finished.state, PipelineState.DONE)
            self.assertEqual(finished.progress, 100.0)
            self.assertIsNone(finished.youtube_video_id)
            self.assertEqual(self.manager(root).get(job.id), finished)
            with self.assertRaises(PipelineTransitionError):
                manager.complete_without_upload(job.id)

    def test_video_id_survives_failed_thumbnail_and_retry_without_completing_early(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = self.manager(root)
            job = self.ready_upload(manager, root, "upload")
            manager.set_upload_metadata(job.id, title="My upload")
            manager.start_upload(job.id)
            recorded = manager.record_youtube_video_id(job.id, "real-video-id")
            self.assertEqual(recorded.state, PipelineState.UPLOADING)
            manager.fail(job.id, "thumbnail_failed", "Thumbnail request failed")
            retried = self.manager(root).retry(job.id)
            self.assertEqual(retried.youtube_video_id, "real-video-id")
            self.assertEqual(retried.state, PipelineState.UPLOADING)
            self.assertEqual(manager.mark_uploaded(job.id, "real-video-id").state, PipelineState.DONE)

    def test_cancel_stays_terminal_until_explicit_resume_and_preserves_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = self.manager(root)
            job = self.ready_export(manager, root, "cancelled")
            manager.start_export(job.id)
            cancelled = manager.cancel(job.id)
            self.assertEqual(cancelled.state, PipelineState.CANCELLED)
            self.assertEqual(cancelled.metadata["cancelled_from"], "exporting")
            self.assertEqual(manager.list_jobs(include_terminal=False), [])
            self.assertEqual(manager.reconstruct_queues(None, None, lambda _id: True), [])
            resumed = self.manager(root).retry(job.id)
            self.assertEqual(resumed.state, PipelineState.READY_EXPORT)
            self.assertEqual(resumed.project_path, job.project_path)
            self.assertEqual(resumed.retry_count, 1)
            self.assertEqual(resumed.progress, 0.0)
            with self.assertRaises(PipelineTransitionError):
                manager.resume_cancelled(job.id)

    def test_legacy_cancelled_job_without_resume_stage_requires_explicit_information(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = self.manager(Path(tmp))
            job = self.discover(manager, "legacy-cancel")
            job.state = PipelineState.CANCELLED
            manager.store.upsert(job)
            with self.assertRaises(PipelineTransitionError):
                manager.resume_cancelled(job.id)
            self.assertEqual(manager.get(job.id).state, PipelineState.CANCELLED)

    def test_failed_cancellation_finalizes_without_retry_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = self.manager(root)
            job = self.ready_export(manager, root, "cancelled-stage")
            manager.start_export(job.id)
            manager.fail(job.id, "export_cancelled", "The cancellation token stopped the worker")
            cancelled = manager.cancel(job.id)
            self.assertEqual(cancelled.state, PipelineState.CANCELLED)
            self.assertEqual(cancelled.metadata["cancelled_from"], "exporting")
            self.assertEqual(cancelled.retry_count, 0)
            self.assertIsNone(cancelled.error_code)
            self.assertEqual(manager.cancel(job.id), cancelled)
            self.assertEqual(manager.resume_cancelled(job.id).state, PipelineState.READY_EXPORT)

    def test_reconstruct_resumes_ready_export_and_orphans_but_not_other_failures_or_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = self.manager(root)
            ready = self.ready_export(manager, root, "ready")
            active = self.ready_export(manager, root, "active")
            manager.start_export(active.id)
            unrelated = self.ready_export(manager, root, "failed")
            manager.fail(unrelated.id, "export_failed", "Retry manually")
            cancelled = self.ready_export(manager, root, "cancelled")
            manager.cancel(cancelled.id)
            owned = self.ready_export(manager, root, "owned")
            queued: list[str] = []

            def enqueue(job_id: str) -> None:
                # Workers need to claim the job after reconstruction has released it.
                with manager.execution_lock(job_id, reentrant=False):
                    queued.append(job_id)

            with manager.execution_lock(owned.id):
                restored = self.manager(root).reconstruct_queues(None, None, enqueue)
            self.assertEqual(set(queued), {ready.id, active.id})
            self.assertEqual({job.id for job in restored}, set(queued))
            self.assertEqual(manager.get(ready.id).state, PipelineState.READY_EXPORT)
            self.assertEqual(manager.get(active.id).retry_state, PipelineState.READY_EXPORT)
            self.assertEqual(manager.get(active.id).error_code, "interrupted")
            self.assertEqual(manager.get(unrelated.id).error_code, "export_failed")
            self.assertEqual(manager.get(cancelled.id).state, PipelineState.CANCELLED)

    def test_reconstruct_requires_both_global_permission_and_per_job_youtube_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = self.manager(root)
            opted = self.ready_upload(manager, root, "opted")
            legacy = self.ready_upload(manager, root, "legacy")
            disabled = self.ready_upload(manager, root, "disabled")
            manager.update_metadata(opted.id, {"delivery": {"youtube": True}})
            manager.update_metadata(disabled.id, {"delivery": {"youtube": False}})
            queued: list[str] = []
            manager.reconstruct_queues(None, None, None, queued.append)
            self.assertEqual(queued, [])
            restored = manager.reconstruct_queues(None, None, None, queued.append, upload_enabled=True)
            self.assertEqual(queued, [opted.id])
            self.assertEqual([job.id for job in restored], [opted.id])
            self.assertEqual(manager.get(legacy.id).state, PipelineState.READY_UPLOAD)


if __name__ == "__main__":
    unittest.main()
