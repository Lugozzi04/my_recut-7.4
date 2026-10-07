from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from automation.manager import PipelineManager
from automation.models import PipelineState
from automation.store import PipelineStore
from ui.twitch_integration import TwitchIntegration


class Queue:
    def __init__(self, manager, output_dir=None, **_callbacks):
        self.manager = manager
        self.output_dir = output_dir
        self.active_job_id = ""
        self.is_running = False
        self.enqueued = []

    def enqueue(self, identifier):
        self.enqueued.append(identifier)
        return True

    def shutdown(self, **_kwargs):
        if self.active_job_id:
            current = self.manager.get(self.active_job_id)
            if not current.state.is_terminal:
                self.manager.fail(current.id, "upload_cancelled", "Worker interrupted.")

    def cancel_current(self):
        if self.active_job_id:
            self.manager.cancel(self.active_job_id)
            return True
        return False


class TwitchUploadIntegrationTests(unittest.TestCase):
    def setup_pipeline(self, root: Path):
        manager = PipelineManager(PipelineStore(root / "jobs.json"))
        source = root / "source.mp4"
        project = root / "project.autocutter"
        output = root / "export.mp4"
        for file in (source, project, output):
            file.write_bytes(b"fixture")
        job, _ = manager.create_configured_job(
            vod_id="test", vod_url="https://www.twitch.tv/videos/1", start_s=0, end_s=10,
            local_source_path=source, metadata={"delivery": {"youtube": True, "title": "Private video"}},
        )
        manager.mark_analyzed(job.id, project)
        manager.start_export(job.id)
        job = manager.mark_exported(job.id, output)
        integration = TwitchIntegration(
            manager=manager, download_queue_factory=Queue,
            analysis_queue_factory=Queue, export_queue_factory=Queue,
        )
        integration._upload_queue = Queue(manager)
        return manager, job, integration

    def test_opted_in_export_uses_shared_upload_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            _manager, job, integration = self.setup_pipeline(Path(tmp))
            integration._on_export_finished(job)
            self.assertEqual(integration._upload_queue.enqueued, [job.id])
            integration.shutdown()

    def test_startup_recovers_upload_interrupted_on_previous_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, job, integration = self.setup_pipeline(Path(tmp))
            manager.set_upload_metadata(job.id, title="Private video")
            manager.start_upload(job.id)
            integration._upload_queue.active_job_id = job.id
            integration.shutdown()
            interrupted = manager.get(job.id)
            self.assertEqual(interrupted.error_code, "interrupted")
            integration._upload_queue.active_job_id = ""
            with patch("automation.exporter.PipelineProjectExporter.validate_artifact", return_value={"signature": "valid"}):
                integration.recover_pipeline()
            self.assertIn(job.id, integration._upload_queue.enqueued)

    def test_manual_cancellation_stays_cancelled_after_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, job, integration = self.setup_pipeline(Path(tmp))
            manager.set_upload_metadata(job.id, title="Private video")
            manager.start_upload(job.id)
            integration._upload_queue.active_job_id = job.id
            self.assertTrue(integration.cancel_upload())
            integration.shutdown()
            self.assertEqual(manager.get(job.id).state, PipelineState.CANCELLED)
