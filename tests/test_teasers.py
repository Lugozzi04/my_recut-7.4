"""Teasers: copy a moment to the start of the video while the original stays in place."""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from core import teasers  # noqa: E402
from core.text_overlays import new_overlay  # noqa: E402
from export.artifacts import timing_text  # noqa: E402

app = QApplication.instance() or QApplication([])
HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _t(role="gameplay", teaser_id=""):
    return SimpleNamespace(source_role=role, teaser_id=teaser_id, teaser_index=0)


def test_teasers_go_after_intros_and_previous_teasers_and_are_numbered_in_timeline_order():
    tracks = [_t("intro"), _t(teaser_id="a"), _t(), _t(teaser_id="b"), _t(teaser_id="a")]
    assert teasers.start_insert_index(tracks) == 2
    teasers.renumber(tracks)
    assert [t.teaser_index for t in tracks] == [0, 1, 0, 2, 1]  # the parts of teaser "a" share its number
    assert teasers.is_teaser({"teaser_id": "x"}) and not teasers.is_teaser({"path": "v.mp4"})


def test_texts_follow_their_footage_when_pieces_move():
    before = [{"key": 1, "path": "v.mp4", "group": "g", "teaser": "", "src_in": 0, "src_out": 60, "tl_in": 0, "tl_out": 60}]
    after = [{"key": 2, "path": "v.mp4", "group": "t", "teaser": "T", "src_in": 30, "src_out": 35, "tl_in": 0, "tl_out": 5},
             {"key": 1, "path": "v.mp4", "group": "g", "teaser": "", "src_in": 0, "src_out": 60, "tl_in": 5, "tl_out": 65}]
    on_original = new_overlay(31.0, "title", "on the original", duration=2.0)
    out = teasers.reanchor_overlays([on_original], before, after)
    assert (out[0].start, out[0].end) == (36.0, 38.0)  # stays on the original footage, not on the teaser copy
    gone = teasers.reanchor_overlays([on_original], before, [])
    assert gone[0] == on_original  # footage removed: the text keeps its time


def test_chapters_name_teasers_in_order_and_keep_games_after_them(tmp_path):
    video = str(tmp_path / "v.mp4")
    tracks = [
        {"path": video, "teaser_id": "T1", "segment_source_in": 30.0, "segment_source_out": 35.0},
        {"path": video, "teaser_id": "T2", "segment_source_in": 50.0, "segment_source_out": 52.0},
        {"path": video, "segment_source_in": 0.0, "segment_source_out": 60.0},
    ]
    segments = [{"v_idx": 0, "v_in": 30.0, "v_out": 35.0, "teaser_id": "T1"},
                {"v_idx": 0, "v_in": 50.0, "v_out": 52.0, "teaser_id": "T2"},
                {"v_idx": 0, "v_in": 0.0, "v_out": 60.0}]
    text = timing_text({"tracks": tracks}, segments=segments, input_paths=[video])
    assert text == "0:00 Teaser 1\n0:05 Teaser 2\n0:07 Partita 1\n"


def _make_source(path: Path) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=10",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=10", "-c:v", "libx264", "-g", "30",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)], check=True)


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")
def test_teaser_tool_copies_a_moment_to_the_start_and_everything_follows(tmp_path):
    from analysis.cut_engine import Segment
    from ui.main_window import MainWindow

    source = tmp_path / "video.mp4"
    _make_source(source)
    w = MainWindow()
    w._startup_auto_restore_pending = False
    w._open_path(str(source))
    deadline = time.time() + 60
    while time.time() < deadline and not (w.input_path and w._tracks and w._tracks[0].duration > 9):
        app.processEvents()
        time.sleep(0.02)
    original = w._tracks[0]
    original.cuts_enabled = True
    original.cuts = [Segment(5.0, 5.5)]
    original.keeps = [Segment(0.0, 5.0), Segment(5.5, 10.0)]
    w.project.text_overlays = [new_overlay(1.0, "title", "on the original")]

    # Teaser tool: two clicks (start, end) on the original -> the copy goes to the start.
    w._set_tool_mode("teaser")
    w._on_teaser_tool_click(4.0)
    assert w._pending_teaser == (0, 4.0)
    w._on_teaser_tool_click(6.0)
    assert w._pending_teaser is None
    assert len(w._tracks) == 2 and teasers.is_teaser(w._tracks[0]) and w._tracks[1] is original
    teaser = w._tracks[0]
    assert teaser.teaser_index == 1 and w._segment_display_name(teaser) == "Teaser 1"
    assert (teaser.segment_source_in, teaser.segment_source_out) == (4.0, 6.0)
    assert teaser.cuts == [Segment(1.0, 1.5)] and not teaser.automatic_cuts_enabled  # the original's cut, copied
    assert original.keeps == [Segment(0.0, 5.0), Segment(5.5, 10.0)]  # untouched
    assert w.project.text_overlays[0].start == pytest.approx(3.0)  # followed its footage (+2 s)

    _paths, segs = w._build_flatten_export_segments()
    assert [(round(s["v_in"], 3), round(s["v_out"], 3), s.get("teaser_id")) for s in segs] == [
        (4.0, 5.0, teaser.teaser_id), (5.5, 6.0, teaser.teaser_id), (0.0, 5.0, None), (5.5, 10.0, None)]

    payload = w._build_project_file_payload()
    saved = [t for t in payload["tracks"] if t.get("teaser_id")]
    assert len(saved) == 1 and saved[0]["teaser_index"] == 1

    # A second teaser lines up after the first one: Teaser 1, Teaser 2, then the video.
    w._activate_track(1)
    w._create_teaser_from_range(1, 8.0, 9.0)
    assert [w._segment_display_name(t) for t in w._tracks[:2]] == ["Teaser 1", "Teaser 2"]

    # Free placement: move Teaser 1 into the middle of the original (it gets split there).
    original_tl_in = w._find_clip(original.video_clip_id).timeline_in
    assert w._move_teaser_to_time(0, original_tl_in + 3.0)
    names = [w._segment_display_name(t) for t in w._tracks]
    assert names[0] == "Teaser 1" and names[2] == "Teaser 2", names  # renumbered in timeline order
    assert w._tracks[1] is original and w._tracks[3].segment_source_in == pytest.approx(3.0)

    # Undo the move, the second teaser and the first one: the text comes back to 1 s.
    w._undo_cuts()
    w._undo_cuts()
    w._undo_cuts()
    assert not any(teasers.is_teaser(t) for t in w._tracks) and len(w._tracks) == 1
    assert w.project.text_overlays[0].start == pytest.approx(1.0)

    w._project_dirty = False
    w.input_path = None
    w.close()
