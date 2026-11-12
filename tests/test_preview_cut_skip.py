"""Preview playback over cuts: skip targets, scheduled skip and single-decoder audio routing."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])


def _window(tmp_path, monkeypatch):
    from analysis.cut_engine import Segment
    from ui.main_window import MainWindow

    for name in ("CACHE", "CHUNK_CACHE", "CONFIG", "DATA", "OUTPUT"):
        monkeypatch.setenv(f"AUTO_CUTTER_{name}_DIR", str(tmp_path / name.lower()))
    w = MainWindow()
    w._startup_auto_restore_pending = False
    track = w._get_active_track()
    track.cuts = [Segment(3.0, 3.25), Segment(7.0, 8.5)]
    track.cuts_enabled = True
    seg = {"track_state_idx": 0, "path": str(tmp_path / "v.mp4"), "start": 0.0, "end": 20.0,
           "duration": 20.0, "source_in": 0.0}
    w._audio_segments = [dict(seg)]
    w._audio_segments_track_idx = None
    w._video_segments = [dict(seg)]
    w._global_duration = 20.0
    return w


def test_skip_info_gives_landing_inside_a_cut_and_next_cut_start_outside(tmp_path, monkeypatch):
    w = _window(tmp_path, monkeypatch)
    assert w._preview_cut_skip_info(2.9) == (None, 3.0)
    assert w._preview_cut_skip_info(3.1) == (3.25, None)
    assert w._preview_cut_skip_info(5.0) == (None, 7.0)
    assert w._preview_cut_skip_info(8.0) == (8.5, None)
    assert w._preview_cut_skip_info(9.0) == (None, None)
    w._project_dirty = False
    w.close()


def test_audio_is_heard_from_the_video_player_when_it_is_the_same_clip(tmp_path, monkeypatch):
    w = _window(tmp_path, monkeypatch)
    assert w._audio_matches_video(5.0, str(tmp_path / "v.mp4"), 5.0)
    assert not w._audio_matches_video(5.0, str(tmp_path / "other.wav"), 5.0)
    assert not w._audio_matches_video(5.0, str(tmp_path / "v.mp4"), 6.0)
    w._set_preview_audio_route(True)
    assert not w.video_audio.isMuted() and w.audio_output.isMuted()  # never both audible
    w._set_preview_audio_route(False)
    assert w.video_audio.isMuted() and not w.audio_output.isMuted()
    w._project_dirty = False
    w.close()


def test_stale_position_right_after_a_skip_is_ignored(tmp_path, monkeypatch):
    import time

    w = _window(tmp_path, monkeypatch)
    w._preview_skip_last = (3.25, time.monotonic())
    w._last_pos = 3.25
    w._on_position_changed(3016)  # decoder still reporting the pre-skip frame (inside the cut)
    assert w._last_pos == 3.25 and w._preview_skip_last is not None
    w._project_dirty = False
    w.close()
