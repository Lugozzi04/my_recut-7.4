from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from analysis.cut_engine import Segment
from widgets.timeline import TimelineWidget, classify_cuts


def make_timeline(*, gameplay: bool = True) -> TimelineWidget:
    duration = 100.0
    cuts = [Segment(0.0, 20.0), Segment(30.0, 31.0), Segment(60.0, 100.0)]  # 1st/3rd are gameplay removals
    gameplay_cuts = [Segment(0.0, 20.0), Segment(60.0, 100.0)] if gameplay else []
    keeps = [Segment(20.0, 60.0)] if gameplay else []
    audio_clip = {"start": 0.0, "end": duration, "source_in": 0.0, "source_out": duration, "kind": "audio",
                  "clip_id": "a1", "track_state_idx": 0, "cuts": cuts, "manual_cuts": [],
                  "gameplay_cuts": gameplay_cuts, "gameplay_keeps": keeps, "restored_cuts": [],
                  "gameplay_islands": [], "rms": np.abs(np.sin(np.arange(3334) / 7.0)).astype(np.float32),
                  "hop_s": 0.03}
    video_clip = {"start": 0.0, "end": duration, "source_in": 0.0, "source_out": duration, "kind": "video",
                  "clip_id": "v1", "track_state_idx": 0, "name": "clip"}
    tl = TimelineWidget()
    tl.resize(1200, 300)
    tl.setTracks([
        {"name": "Video", "duration": duration, "rms": None, "hop_s": 0.03, "cuts": [], "clips": [video_clip],
         "kind": "video", "track_state_idx": None},
        {"name": "Audio", "duration": duration, "rms": None, "hop_s": 0.03, "cuts": [], "clips": [audio_clip],
         "kind": "audio", "track_state_idx": None, "active_state_idx": 0},
    ], reset_view=True, global_duration=duration)
    tl.setActiveTrack(1)
    return tl


class TimelineWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_classify_cuts_by_provenance(self) -> None:
        cuts = [Segment(0, 10), Segment(20, 22), Segment(40, 45)]
        kinds = classify_cuts(cuts, manual=[Segment(20, 22)], gameplay=[Segment(0, 10)])
        self.assertEqual(kinds, ["gameplay", "manual", "auto"])

    def test_playhead_moves_do_not_rebuild_the_static_layer(self) -> None:
        tl = make_timeline()
        tl.show()
        tl.grab()
        rebuilds = tl.static_renders
        for t in (10.0, 10.5, 11.0, 50.0):
            tl.setPlayhead(t)
            tl.grab()
        self.assertEqual(tl.static_renders, rebuilds)  # only playhead strips were repainted
        tl.setCutSelection([1])
        tl.grab()
        self.assertEqual(tl.static_renders, rebuilds + 1)  # real content changes still re-render

    def test_follow_turns_pages_instead_of_scrolling_every_tick(self) -> None:
        tl = make_timeline()
        tl.setZoomIndex(4, anchor_t=5.0)  # span 100/6 s
        start = tl.view_start
        tl.setPlayhead(start + 1.0)
        self.assertEqual(tl.view_start, start)
        tl.setPlayhead(start + tl._span() + 1.0)  # leaves the view → one page jump
        self.assertGreater(tl.view_start, start)
        self.assertLess(tl._t_to_xf(tl.playhead), tl.width() / 2)

    def test_gameplay_islands_and_removed_spans(self) -> None:
        tl = make_timeline()
        islands = tl._gameplay_islands()
        self.assertEqual([(i["t0"], i["t1"]) for i in islands], [(20.0, 60.0)])
        spans = tl._removed_spans()
        # gameplay removals outside the island are not drawn as cuts (the overlay dims them)
        self.assertEqual([(a, b, kind) for a, b, kind, _sel in spans], [(30.0, 31.0, "auto")])

    def test_gameplay_cuts_are_not_selectable_but_audio_cuts_inside_are(self) -> None:
        tl = make_timeline()
        audio = tl._audio_track_index()
        self.assertIsNone(tl._cut_hit(audio, tl._t_to_xf(10.0)))  # inside a gameplay removal
        hit = tl._cut_hit(audio, tl._t_to_xf(30.5))
        self.assertIsNotNone(hit)
        self.assertEqual(hit[0], 1)

    def _mouse(self, tl, kind, x, y):
        etype = {"press": QEvent.MouseButtonPress, "move": QEvent.MouseMove,
                 "release": QEvent.MouseButtonRelease}[kind]
        held = Qt.NoButton if kind == "release" else Qt.LeftButton
        button = Qt.NoButton if kind == "move" else Qt.LeftButton
        QApplication.sendEvent(tl, QMouseEvent(etype, QPointF(x, y), QPointF(x, y), button, held, Qt.NoModifier))

    def test_island_click_selects_and_edge_drag_requests_resize(self) -> None:
        tl = make_timeline()
        selected, resized = [], []
        tl.islandSelected.connect(selected.append)
        tl.islandResizeRequested.connect(lambda *a: resized.append(a))
        video = next(ln for ln in tl._lanes() if ln.kind == "video")
        y = video.top + video.h * 0.6
        x_mid = tl._t_to_xf(40.0)
        self._mouse(tl, "press", x_mid, y)
        self._mouse(tl, "release", x_mid, y)
        self.assertEqual(selected[-1], (0, 0))
        x_edge = tl._t_to_xf(20.0)
        self._mouse(tl, "press", x_edge, y)
        self._mouse(tl, "move", x_edge + 60, y)
        self._mouse(tl, "release", x_edge + 60, y)
        self.assertEqual(len(resized), 1)
        ts, index, start, end = resized[0]
        self.assertEqual((ts, index, end), (0, 0, 60.0))
        self.assertGreater(start, 20.0)

    def test_no_islands_without_gameplay(self) -> None:
        tl = make_timeline(gameplay=False)
        self.assertEqual(tl._gameplay_islands(), [])


if __name__ == "__main__":
    unittest.main()
