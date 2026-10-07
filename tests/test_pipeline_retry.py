from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from automation.downloader import (
    DownloadCancellation, DownloadResult, PipelineDownloadCancelled,
    PipelineDownloadError, PipelineDownloadService,
)
from automation.manager import PipelineManager
from automation.models import PipelineState
from automation.exporter import ExportCancellation, PipelineExportCancelled, PipelineExportError, PipelineExportService
from automation.retry import automatic_retry_allowed, transient_network_error, wait_for_retry
from automation.store import PipelineStore


class PipelineRetryTests(unittest.TestCase):
    def test_network_classification_does_not_retry_permanent_errors(self) -> None:
        for error in ("HTTP Error 503", "server returned 429", "Connection reset", TimeoutError()):
            self.assertTrue(transient_network_error(error))
        for error in ("HTTP Error 404", "HTTP Error 403 timeout", "invalid FFmpeg option", "unknown preset"):
            self.assertFalse(transient_network_error(error))

    def test_shared_download_executor_retries_transient_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = PipelineManager(PipelineStore(root / "jobs.json"))
            job, _ = manager.discover_vod(vod_id="1", vod_url="https://www.twitch.tv/videos/1")
            manager.request_range(job.id)
            manager.select_range(job.id, 0, 10)
            output = root / "source.mp4"
            output.write_bytes(b"fixture")
            attempts = []

            class Downloader:
                def download(self, _request, **_kwargs):
                    attempts.append(1)
                    if len(attempts) == 1:
                        raise PipelineDownloadError("network", "HTTP Error 503", retryable=True)
                    return DownloadResult(output, 10)

            service = PipelineDownloadService(manager, downloader_factory=Downloader)
            completed = service.execute(job.id)
            self.assertEqual(completed.state, PipelineState.ANALYZING)
            self.assertEqual(completed.retry_count, 1)
            self.assertEqual(len(attempts), 2)

    def test_cancel_stops_retry_backoff(self) -> None:
        token = DownloadCancellation()
        with tempfile.TemporaryDirectory() as tmp:
            manager = PipelineManager(PipelineStore(Path(tmp) / "jobs.json"))
            job, _ = manager.discover_vod(vod_id="1", vod_url="https://www.twitch.tv/videos/1")
            manager.request_range(job.id)
            manager.select_range(job.id, 0, 10)

            class Downloader:
                def download(self, _request, **_kwargs):
                    token.cancel()
                    raise PipelineDownloadError("network", "timeout", retryable=True)

            with patch("automation.retry.time.sleep") as sleep:
                with self.assertRaises(PipelineDownloadCancelled):
                    PipelineDownloadService(manager, downloader_factory=Downloader).execute(job.id, cancellation=token)
                sleep.assert_not_called()

    def _job(self, root: Path, stage: str):
        manager = PipelineManager(PipelineStore(root / "jobs.json"))
        job, _created = manager.discover_vod(vod_id="1", vod_url="https://www.twitch.tv/videos/1")
        manager.request_range(job.id)
        manager.select_range(job.id, 0, 10)
        if stage == "export":
            source = root / "source.mp4"
            project = root / "project.autocutter"
            source.write_bytes(b"source")
            project.write_text("project", encoding="utf-8")
            manager.mark_downloaded(job.id, source)
            manager.mark_analyzed(job.id, project)
        return manager, job.id

    def test_queue_shutdown_during_download_or_export_backoff_remains_auto_resumable(self):
        for stage in ("download", "export"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp:
                manager, job_id = self._job(Path(tmp), stage)
                token = DownloadCancellation() if stage == "download" else ExportCancellation()

                class RetryableExportError(PipelineExportError):
                    retryable = True

                class FailedStage:
                    def download(self, request, **kwargs):
                        token.cancel()  # Queue shutdown, without explicit user cancellation.
                        raise PipelineDownloadError("network", "HTTP 503", retryable=True)

                    def export(self, job, **kwargs):
                        token.cancel()
                        raise RetryableExportError("network", "HTTP 503")

                service = PipelineDownloadService(manager, downloader_factory=FailedStage) if stage == "download" else PipelineExportService(manager, exporter_factory=FailedStage)
                cancelled = PipelineDownloadCancelled if stage == "download" else PipelineExportCancelled
                with self.assertRaises(cancelled):
                    service.execute(job_id, cancellation=token)
                interrupted = manager.get(job_id)
                self.assertEqual(interrupted.state, PipelineState.FAILED)
                self.assertEqual(interrupted.error_code, "interrupted")
                self.assertNotIn("automatic_retry_pending", interrupted.metadata)
                queued = []
                restored = PipelineManager(PipelineStore(manager.store.path))
                restored.reconstruct_queues(queued.append if stage == "download" else None, None,
                                            queued.append if stage == "export" else None)
                self.assertEqual(queued, [job_id])

    def test_explicit_cancel_during_download_or_export_backoff_stays_cancelled(self):
        for stage in ("download", "export"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp:
                manager, job_id = self._job(Path(tmp), stage)
                token = DownloadCancellation() if stage == "download" else ExportCancellation()

                class RetryableExportError(PipelineExportError):
                    retryable = True

                class FailedStage:
                    def download(self, request, **kwargs):
                        token.cancel()
                        manager.cancel(job_id)
                        raise PipelineDownloadError("network", "HTTP 503", retryable=True)

                    def export(self, job, **kwargs):
                        token.cancel()
                        manager.cancel(job_id)
                        raise RetryableExportError("network", "HTTP 503")

                service = PipelineDownloadService(manager, downloader_factory=FailedStage) if stage == "download" else PipelineExportService(manager, exporter_factory=FailedStage)
                cancelled = PipelineDownloadCancelled if stage == "download" else PipelineExportCancelled
                with self.assertRaises(cancelled):
                    service.execute(job_id, cancellation=token)
                self.assertEqual(manager.get(job_id).state, PipelineState.CANCELLED)
                self.assertEqual(manager.reconstruct_queues(lambda id: True, None, lambda id: True), [])

    def test_crash_during_backoff_recovers_durable_pending_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, job_id = self._job(Path(tmp), "download")
            manager.fail(job_id, "network", "HTTP 503")
            with patch("automation.retry.time.sleep", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    wait_for_retry(manager, job_id, None, .5)
            self.assertTrue(manager.get(job_id).metadata["automatic_retry_pending"])
            restored = PipelineManager(PipelineStore(manager.store.path))
            restored.recover_interrupted_jobs()
            self.assertEqual(restored.get(job_id).error_code, "interrupted")
            self.assertNotIn("automatic_retry_pending", restored.get(job_id).metadata)

    def test_permanent_failure_is_not_promoted_to_retry_by_shutdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, job_id = self._job(Path(tmp), "download")
            failed = manager.fail(job_id, "invalid_url", "HTTP 404")
            self.assertEqual(manager.interrupt(job_id).to_mapping(), failed.to_mapping())
            self.assertEqual(manager.recover_interrupted_jobs(), [])

    def test_transient_failure_and_retry_marker_commit_before_backoff_callback(self):
        for stage in ("download", "export"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp:
                manager, job_id = self._job(Path(tmp), stage)

                class RetryableExportError(PipelineExportError):
                    retryable = True

                class FailedStage:
                    def download(self, request, **kwargs):
                        raise PipelineDownloadError("network", "HTTP 503", retryable=True)
                    def export(self, job, **kwargs):
                        raise RetryableExportError("network", "HTTP 503")

                service = PipelineDownloadService(manager, downloader_factory=FailedStage) if stage == "download" else PipelineExportService(manager, exporter_factory=FailedStage)
                # Simulate process loss at the exact fail->wait boundary: the
                # wait helper never gets a chance to persist its own marker.
                with patch("automation.execution.wait_for_retry", side_effect=KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        service.execute(job_id)
                saved = PipelineManager(PipelineStore(manager.store.path)).get(job_id)
                self.assertEqual(saved.state, PipelineState.FAILED)
                self.assertTrue(saved.metadata["automatic_retry_pending"])
                self.assertFalse(automatic_retry_allowed())
                manager.recover_interrupted_jobs()
                self.assertEqual(manager.get(job_id).error_code, "interrupted")

    def test_exhausted_transient_retry_budget_is_not_automatically_recovered(self):
        for stage in ("download", "export"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp:
                manager, job_id = self._job(Path(tmp), stage)
                attempts = []

                class RetryableExportError(PipelineExportError):
                    retryable = True

                class FailedStage:
                    def download(self, request, **kwargs):
                        attempts.append(1)
                        raise PipelineDownloadError("network", "HTTP 503", retryable=True)
                    def export(self, job, **kwargs):
                        attempts.append(1)
                        raise RetryableExportError("network", "HTTP 503")

                service = PipelineDownloadService(manager, downloader_factory=FailedStage) if stage == "download" else PipelineExportService(manager, exporter_factory=FailedStage)
                with patch("automation.execution.wait_for_retry"):
                    with self.assertRaises(PipelineDownloadError if stage == "download" else PipelineExportError):
                        service.execute(job_id)
                self.assertEqual(len(attempts), 3)
                failed = manager.get(job_id)
                self.assertEqual(failed.error_code, "network")
                self.assertNotIn("automatic_retry_pending", failed.metadata)
                self.assertEqual(manager.recover_interrupted_jobs(), [])
                self.assertEqual(manager.reconstruct_queues(lambda id: True, None, lambda id: True), [])
