from __future__ import annotations

import unittest

from analysis.cut_engine import Segment
from ui.track_state import TrackState


class TrackStateTests(unittest.TestCase):
    def test_mutable_state_is_not_shared_between_tracks(self) -> None:
        first = TrackState()
        second = TrackState()

        first.cuts.append(Segment(1.0, 2.0))
        first.cfg["threshold_pct"] = 10

        self.assertEqual(second.cuts, [])
        self.assertEqual(second.cfg, {})


if __name__ == "__main__":
    unittest.main()
