from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from automation.models import PipelineJob, PipelineState
from automation.store import (
    PIPELINE_STORE_FORMAT,
    PIPELINE_STORE_VERSION,
    PipelineStore,
    PipelineStoreError,
    default_pipeline_store_path,
)


class PipelineStoreTests(unittest.TestCase):
    def make_job(self, vod_id: str = "vod-1") -> PipelineJob:
        return PipelineJob.create(vod_id=vod_id, vod_url=f"https://www.twitch.tv/videos/{vod_id}")

    def test_missing_store_loads_as_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = PipelineStore(Path(tmp) / "nested" / "jobs.json")

            self.assertEqual(store.load(), [])

    def test_round_trip_uses_versioned_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "jobs.json"
            store = PipelineStore(path)
            job = self.make_job()
            job.transition(PipelineState.WAITING_RANGE)

            store.save([job])

            restored = store.load()
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(restored, [job])
            self.assertEqual(raw["format"], PIPELINE_STORE_FORMAT)
            self.assertEqual(raw["version"], PIPELINE_STORE_VERSION)

    def test_upsert_updates_without_duplicating(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = PipelineStore(Path(tmp) / "jobs.json")
            job = self.make_job()
            store.upsert(job)
            job.transition(PipelineState.WAITING_RANGE)
            store.upsert(job)

            self.assertEqual(len(store.load()), 1)
            restored = store.get(job.id)
            self.assertIsNotNone(restored)
            assert restored is not None
            self.assertEqual(restored.state, PipelineState.WAITING_RANGE)

    def test_duplicate_vod_ids_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = PipelineStore(Path(tmp) / "jobs.json")
            first = self.make_job()
            second = self.make_job()

            with self.assertRaises(PipelineStoreError):
                store.save([first, second])

    def test_corrupt_file_is_never_silently_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "jobs.json"
            path.write_text("{broken", encoding="utf-8")
            store = PipelineStore(path)

            with self.assertRaises(PipelineStoreError):
                store.upsert(self.make_job())

            self.assertEqual(path.read_text(encoding="utf-8"), "{broken")

    def test_failed_atomic_replace_keeps_previous_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "jobs.json"
            store = PipelineStore(path)
            original = self.make_job("original")
            store.save([original])
            before = path.read_bytes()

            with patch("automation.store.os.replace", side_effect=OSError("locked")):
                with self.assertRaises(PipelineStoreError):
                    store.save([self.make_job("replacement")])

            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(path.parent.glob(".*.tmp")), [])

    def test_invalid_job_is_reported_as_store_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "jobs.json"
            store = PipelineStore(path)
            job = self.make_job()
            job.progress = float("nan")

            with self.assertRaises(PipelineStoreError):
                store.save([job])

            self.assertFalse(path.exists())

    def test_delete_reports_whether_job_existed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = PipelineStore(Path(tmp) / "jobs.json")
            job = self.make_job()
            store.save([job])

            self.assertTrue(store.delete(job.id))
            self.assertFalse(store.delete(job.id))

    def test_custom_default_path_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            custom = str(Path(tmp) / "queue.json")
            with patch.dict(os.environ, {"AUTO_CUTTER_PIPELINE_STORE": custom}):
                self.assertEqual(default_pipeline_store_path(), Path(custom).resolve())


if __name__ == "__main__":
    unittest.main()
