from __future__ import annotations

import unittest

from automation.models import (
    PipelineFormatError,
    PipelineJob,
    PipelineState,
    PipelineTransitionError,
)


class PipelineModelTests(unittest.TestCase):
    def make_job(self) -> PipelineJob:
        return PipelineJob.create(
            vod_id="12345",
            vod_url="https://www.twitch.tv/videos/12345",
            channel_id="channel-1",
            source_title="Test live",
            metadata={"language": "it"},
        )

    def test_happy_path_enforces_every_pipeline_stage(self) -> None:
        job = self.make_job()

        job.transition(PipelineState.WAITING_RANGE)
        job.select_range(10.0, 70.0)
        job.transition(PipelineState.ANALYZING)
        job.transition(PipelineState.READY_EXPORT)
        job.transition(PipelineState.EXPORTING)
        job.transition(PipelineState.READY_UPLOAD)
        job.transition(PipelineState.UPLOADING)
        job.transition(PipelineState.DONE)

        self.assertEqual(job.state, PipelineState.DONE)
        self.assertEqual(job.progress, 100.0)

    def test_invalid_transition_is_rejected(self) -> None:
        job = self.make_job()

        with self.assertRaises(PipelineTransitionError):
            job.transition(PipelineState.DOWNLOADING)

    def test_download_requires_a_valid_selected_range(self) -> None:
        job = self.make_job()
        job.transition(PipelineState.WAITING_RANGE)

        with self.assertRaises(PipelineTransitionError):
            job.transition(PipelineState.DOWNLOADING)
        with self.assertRaises(PipelineTransitionError):
            job.select_range(20.0, 20.0)

        job.select_range(20.0, 21.0)
        self.assertEqual(job.state, PipelineState.DOWNLOADING)

    def test_progress_is_monotonic_inside_active_stage(self) -> None:
        job = self.make_job()
        job.transition(PipelineState.WAITING_RANGE)
        job.select_range(0.0, 60.0)
        job.update_progress(20.0)

        with self.assertRaises(PipelineTransitionError):
            job.update_progress(19.0)

        job.update_progress(100.0)
        self.assertEqual(job.progress, 100.0)

    def test_failed_job_remembers_stage_and_can_retry(self) -> None:
        job = self.make_job()
        job.transition(PipelineState.WAITING_RANGE)
        job.select_range(0.0, 60.0)
        job.update_progress(25.0)
        job.fail("network_error", "Download interrupted")

        self.assertEqual(job.state, PipelineState.FAILED)
        self.assertEqual(job.retry_state, PipelineState.DOWNLOADING)

        job.retry()
        self.assertEqual(job.state, PipelineState.DOWNLOADING)
        self.assertEqual(job.progress, 0.0)
        self.assertEqual(job.retry_count, 1)
        self.assertIsNone(job.error_code)

    def test_mapping_round_trip_preserves_job(self) -> None:
        job = self.make_job()
        job.transition(PipelineState.WAITING_RANGE)
        job.select_range(5.5, 80.0)

        restored = PipelineJob.from_mapping(job.to_mapping())

        self.assertEqual(restored, job)
        self.assertIsNot(restored.metadata, job.metadata)

    def test_invalid_mapping_is_rejected(self) -> None:
        payload = self.make_job().to_mapping()
        payload["state"] = "not-a-state"

        with self.assertRaises(PipelineFormatError):
            PipelineJob.from_mapping(payload)


if __name__ == "__main__":
    unittest.main()
