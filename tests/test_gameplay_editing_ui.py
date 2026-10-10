from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from analysis.cut_engine import Segment
from ui.gameplay_editing_ui import (
    GameplayEditWorker,
    complement,
    island_metadata,
    resize_island,
    to_segment_domain,
)


def pairs(segments):
    return [(round(s.start, 3), round(s.end, 3)) for s in segments]


class GameplayEditingHelpersTests(unittest.TestCase):
    def test_complement_matches_keep_policy_rule(self) -> None:
        keeps = [Segment(10, 20), Segment(40, 50)]
        self.assertEqual(pairs(complement(keeps, 60)), [(0, 10), (20, 40), (50, 60)])
        self.assertEqual(pairs(complement([], 30)), [(0, 30)])  # no island: everything removed

    def test_segment_domain_slices_and_shifts(self) -> None:
        keeps = [Segment(10, 20), Segment(40, 50)]
        # track is the source range [15, 45]: first island clipped, second shifted
        self.assertEqual(pairs(to_segment_domain(keeps, 15, 45)), [(0, 5), (25, 30)])

    def test_resize_island_is_clamped_to_neighbours_and_minimum(self) -> None:
        keeps = [Segment(10, 20), Segment(30, 40)]
        self.assertEqual(pairs(resize_island(keeps, 0, 5, 35, 60)), [(5, 30), (30, 40)])  # stops at next island
        self.assertEqual(pairs(resize_island(keeps, 1, 39.5, 39.0, 60)), [(10, 20), (39.5, 40.5)])  # >= 1 s long
        self.assertEqual(pairs(resize_island(keeps, 1, 25, 80, 60)), [(10, 20), (25, 60)])  # clamped to track

    def test_island_metadata_in_segment_time(self) -> None:
        report = {"keep_islands": [{"start": 100.0, "end": 200.0, "game": {"index": 2, "result": "WIN"},
                                    "start_source": "decision", "end_source": "anchor",
                                    "decisions": {"START": {"resolver": "exact"}}, "adjustments": []}]}
        meta = island_metadata(report, 50.0, 150.0)
        self.assertEqual(len(meta), 1)
        self.assertEqual((meta[0]["start"], meta[0]["end"]), (50.0, 100.0))
        self.assertEqual(meta[0]["game_index"], 2)
        self.assertEqual(meta[0]["resolvers"], {"START": "exact"})


class GameplayEditWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_worker_uses_the_shared_service_and_returns_keeps(self) -> None:
        seen = {}

        def fake_run(self, video, *, cancellation=None, on_progress=None):
            seen["mode"] = self.settings.boundary_mode
            seen["keep_games"] = self.settings.keep_games
            on_progress(50)
            return SimpleNamespace(gameplay_keeps=[Segment(1.0, 4.0)], report=lambda: {"keep_islands": []})

        worker = GameplayEditWorker("video.mp4", "codex")
        results, progress = [], []
        worker.finished.connect(results.append)
        worker.progress.connect(progress.append)
        with patch("analysis.gameplay_editing.GameplayEditingService.run", fake_run):
            worker.run()
        self.assertEqual(seen, {"mode": "codex", "keep_games": "wins"})
        self.assertEqual(progress, [50])
        self.assertEqual(results[0]["keeps"], [(1.0, 4.0)])

    def test_cancellation_is_reported_as_cancelled(self) -> None:
        def fake_run(self, video, *, cancellation=None, on_progress=None):
            cancellation.raise_if_cancelled()
            raise AssertionError("should have been cancelled")

        worker = GameplayEditWorker("video.mp4")
        failures = []
        worker.failed.connect(failures.append)
        worker.cancel()
        with patch("analysis.gameplay_editing.GameplayEditingService.run", fake_run):
            worker.run()
        self.assertEqual(failures, ["cancelled"])


if __name__ == "__main__":
    unittest.main()
