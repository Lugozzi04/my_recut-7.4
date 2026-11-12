"""Text overlays: model, renderer (preview == export) and burned-in export with real ffmpeg."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from core.text_overlays import (  # noqa: E402
    FONTS,
    PRESETS,
    active_at,
    apply_preset,
    new_overlay,
    output_intervals,
    overlays_from_payload,
    overlays_to_payload,
)

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
app = QApplication.instance() or QApplication([])


def test_timeline_range_maps_to_output_time_across_cuts():
    kept = [(0.0, 10.0), (20.0, 30.0), (40.0, 50.0)]
    assert output_intervals(kept, 8.0, 22.0) == [(8.0, 12.0)]  # removed 10-20 s: contiguous on output
    assert output_intervals(kept, 12.0, 18.0) == []              # entirely inside a cut
    assert output_intervals(kept, 25.0, 45.0) == [(15.0, 25.0)]
    reordered = [(20.0, 30.0), (0.0, 10.0)]
    assert output_intervals(reordered, 5.0, 25.0) == [(0.0, 5.0), (15.0, 20.0)]


def test_payload_roundtrip_and_validation():
    ov = apply_preset(new_overlay(12.5, "caption", "ciao"), "label")
    raw = overlays_to_payload([ov])
    back = overlays_from_payload(raw + [{"text": "", "start": 1, "end": 2}, {"text": "x", "start": 5, "end": 1},
                                        {"text": "bad font", "start": 1, "end": 2, "font": "comic sans", "x": 9}])
    assert back[1] == ov
    assert back[0].font in FONTS and back[0].x == 1.0
    assert len(back) == 2


def test_fades_and_activity():
    ov = new_overlay(10.0, "title", "X", duration=2.0)
    assert active_at([ov], 9.99) == []
    assert active_at([ov], 10.1)[0][1] == pytest.approx(0.5)
    assert active_at([ov], 11.0)[0][1] == 1.0
    assert active_at([ov], 12.0) == []


def test_every_preset_renders_with_its_bundled_font():
    from ui.text_render import ensure_fonts, qfont, render_overlay

    assert all(ensure_fonts().values()), "bundled fonts must load"
    for key in PRESETS:
        ov = new_overlay(0, key, "NON CRAFTARLA!")
        assert qfont(ov.font, 40).family() == FONTS[ov.font][0]
        img, x, y = render_overlay(ov, 1920, 1080)
        assert not img.isNull() and img.width() > 200
        assert 0 <= x and x + img.width() <= 1920 + 4 and 0 <= y and y + img.height() <= 1080 + 4
    # same text, double resolution -> double size (preview and export agree at any size)
    a, _, _ = render_overlay(new_overlay(0, "title", "ABC"), 1280, 720)
    b, _, _ = render_overlay(new_overlay(0, "title", "ABC"), 2560, 1440)
    assert abs(b.width() / a.width() - 2.0) < 0.05


def _make_source(path: Path) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:size=320x180:rate=30:d=6",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=6", "-c:v", "libx264", "-g", "30",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)], check=True)


def _pixel(video: Path, t: float, x: int, y: int) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(180, 320, 3)[y, x].astype(int)


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")
@pytest.mark.parametrize("method", ["smart_hybrid", "chunked_parallel"])
def test_text_is_burned_in_on_output_time_even_when_smart_render_was_requested(tmp_path, monkeypatch, method):
    from PySide6.QtGui import QColor, QImage

    from analysis.cut_engine import Segment
    from export.exporter import ExportWorker

    for name in ("CACHE", "CHUNK_CACHE", "CONFIG", "DATA"):
        monkeypatch.setenv(f"AUTO_CUTTER_{name}_DIR", str(tmp_path / name.lower()))
    source = tmp_path / "src.mp4"
    _make_source(source)
    png = tmp_path / "text.png"
    tile = QImage(60, 40, QImage.Format_ARGB32)
    tile.fill(QColor(255, 0, 0))
    assert tile.save(str(png))
    keeps = [Segment(0.0, 2.0), Segment(3.0, 6.0)]  # output: 0-2 from 0-2, 2-5 from 3-6
    texts = [{"png": str(png), "x": 20, "y": 30, "intervals": output_intervals([(0, 2), (3, 6)], 1.0, 4.0),
              "fade": False, "sig": "red"}]
    assert texts[0]["intervals"] == [(1.0, 3.0)]
    out = tmp_path / "out.mp4"
    worker = ExportWorker("ffmpeg", str(source), str(out), keeps, codec="libx264", export_method=method,
                          parallel_workers=2, text_overlays=texts)
    log: list[str] = []
    worker._log = log.append  # type: ignore[method-assign,assignment]
    errors: list[str] = []
    worker.error.connect(errors.append)
    worker.run()
    assert not errors and out.is_file(), errors
    assert any("text_overlays" in line for line in log)
    for t, red in ((0.5, False), (1.5, True), (2.5, True), (4.0, False)):
        r, g, b = _pixel(out, t, 50, 50)
        assert (r > 180 and g < 80) == red, (t, r, g, b)
    r, g, b = _pixel(out, 1.5, 150, 100)  # outside the text box: still the black video
    assert r < 40


def test_edit_panel_adds_moves_saves_and_exports_texts(tmp_path, monkeypatch):
    from PySide6.QtGui import QImage

    from ui.main_window import MainWindow

    for name in ("CACHE", "CHUNK_CACHE", "CONFIG", "DATA", "OUTPUT"):
        monkeypatch.setenv(f"AUTO_CUTTER_{name}_DIR", str(tmp_path / name.lower()))
    window = MainWindow()
    window._startup_auto_restore_pending = False
    ctl = window.text_overlays_ctl
    assert ctl.add() is None and ctl.overlays() == []  # no video: nothing to put a text on
    window.input_path = str(tmp_path / "video.mp4")
    window._last_pos = 12.0
    ov = ctl.add("label")
    ctl.text_edit.setPlainText("trappola")
    ctl._commit_text()
    assert ctl.selected().display_text() == "TRAPPOLA" and ctl.selected().start == 12.0
    ctl.move(ov.id, 0.25, 0.75)
    assert (ctl.selected().x, ctl.selected().y) == (0.25, 0.75)
    assert ctl.pos_combo.currentData() == "custom"
    assert window.timeline._texts[0][0] == ov.id
    window._last_pos = 13.0
    assert len(ctl.preview_items(1280, 720)) == 1
    payload = window._build_project_file_payload()
    assert payload["text_overlays"][0]["text"] == "trappola"
    # 10-14 s kept, 14-20 s cut, 20-30 s kept: the 3 s text (12-15 s) is on the output at 12-14 s only
    specs = ctl.export_specs([(10.0, 14.0), (20.0, 30.0)], 1280, 720, str(tmp_path / "texts"))
    assert specs[0]["intervals"] == [(2.0, 4.0)] and QImage(specs[0]["png"]).width() > 50
    ctl.delete_selected()
    assert ctl.overlays() == [] and window.timeline._texts == []
    window.input_path = None
    window._project_dirty = False
    window.close()
