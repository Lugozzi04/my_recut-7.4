"""Clips page: sources, proposals list, ticks/trims saved to clips.json, export of the ticked clips only."""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget  # noqa: E402

from analysis.clips.service import ClipSession  # noqa: E402
from test_clips import FakeMap, highlight, quiet_series, sentences, write_analysis  # noqa: E402

APP = QApplication.instance() or QApplication([])


def clips_owner(tmp_path: Path):
    from core.project import Project
    from ui.clips_page import build_clips_page

    owner = QWidget()
    owner.page_clips = QWidget(owner)
    QVBoxLayout(owner)
    owner.layout().addWidget(owner.page_clips)
    owner.project = Project.create_default()
    owner.dirty = []
    owner._mark_project_dirty = lambda *a: owner.dirty.append(a)
    owner._project_file_path = None
    owner._build_project_file_payload = lambda: {"tracks": [{"path": str(tmp_path / "vod.mp4")}]}
    owner.clips = build_clips_page(owner)
    return owner


def session_with_clips(tmp_path: Path) -> tuple[ClipSession, list]:
    video = tmp_path / "Hunter.mp4"
    video.write_bytes(b"x")
    write_analysis(tmp_path / "Hunter_packaging", digest="same", sources=[str(video)], pieces=[
        {"source_index": 0, "source_start": 0.0, "source_end": 300.0, "edited_start": 0.0}],
        lines=sentences(0, 300), highlights=[highlight("H01", 50, 65, 2.0, "video"),
                                             highlight("H02", 150, 165, 1.0, "video")])
    session = ClipSession(video=str(video), edit_map_factory=lambda: FakeMap("same"), series_factory=quiet_series)
    return session, session.candidates(count=2)


def test_sources_videos_and_hint(tmp_path):
    owner = clips_owner(tmp_path)
    combo = owner.clips_source_combo
    assert [combo.itemData(i) for i in range(combo.count())] == ["project", "video"]
    assert owner.clips_video_row.isHidden() and "Current project" in owner.lbl_clips_hint.text()
    owner.project.packaging_outputs = [{"id": "x1", "project": str(tmp_path / "edit.autocutter"),
                                        "output": str(tmp_path / "Mage.mp4"), "title": "VOD — Mage"}]
    owner.clips._files = [str(tmp_path / "other.mp4")]
    owner.clips.restore_videos()
    combo.setCurrentIndex(combo.findData("video"))
    assert not owner.clips_video_row.isHidden()
    items = [owner.clips_video_combo.itemText(i) for i in range(owner.clips_video_combo.count())]
    assert items == ["VOD — Mage — export", "other.mp4 — file"]
    args = owner.clips._session_args()
    assert args["project"].endswith("edit.autocutter") and args["output"].endswith("Mage_clips")
    owner.clips_video_combo.setCurrentIndex(1)
    assert owner.clips._session_args() == {"video": str(tmp_path / "other.mp4")}
    owner.close()


def test_current_project_snapshot_goes_to_a_stable_clips_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_CUTTER_OUTPUT_DIR", str(tmp_path / "exports"))
    owner = clips_owner(tmp_path)
    first = owner.clips._session_args()
    assert Path(first["project"]).is_file() and owner.project.clips["folder"] == first["output"] and owner.dirty
    assert owner.clips._session_args()["output"] == first["output"]  # remembered in the project
    owner._project_file_path = tmp_path / "VOD.autocutter"
    assert owner.clips._session_args()["output"] == str(tmp_path / "VOD_clips")
    owner.close()


def test_rows_ticks_and_trims_are_saved_and_export_takes_ticked_only(tmp_path, monkeypatch):
    import ui.clips_page as module
    owner = clips_owner(tmp_path)
    session, clips = session_with_clips(tmp_path)
    owner.clips._found((session, clips))
    assert not owner.clips_results.isHidden() and set(owner.clips.rows) == {"C01", "C02"}
    assert not owner.btn_clips_export.isEnabled()
    row = owner.clips.rows["C02"]
    row.tick.setChecked(True)
    assert owner.btn_clips_export.text() == "Export 1 clip" and owner.btn_clips_export.isEnabled()
    row.end.setValue(row.start.value() + 1.0)  # shorter than the minimum trim: clamped to 3 s
    clip = next(c for c in owner.clips.clips if c.id == "C02")
    assert clip.duration == 3.0 and clip.parts == [(0, clip.edited_start, clip.edited_end)]
    saved = json.loads(session.store.read_text(encoding="utf-8"))
    assert [c["selected"] for c in saved["clips"]] == [False, True] and saved["clips"][1]["duration"] == 3.0
    started = []
    monkeypatch.setattr(module, "_start", lambda owner_, worker, *a: (
        started.append(worker) or SimpleNamespace(finished=SimpleNamespace(connect=lambda *x: None))))
    owner.clips.export()
    assert [c.id for c in started[0].clips] == ["C02"] and owner.btn_clips_export.text() == "Cancel"
    owner.close()


def test_main_window_has_clips_tab_and_persists_its_folder(tmp_path):
    from ui.main_window import MainWindow
    window = MainWindow()
    window._startup_auto_restore_pending = False
    assert window.pages.indexOf(window.page_clips) == 4 and window.clips is not None
    window._switch_page(4)
    assert window.seg_clips.isChecked() and not window.seg_thumbnails.isChecked()
    window.project.clips = {"folder": str(tmp_path / "c")}
    window._project_dirty = False
    window.close()


def test_shorts_options_and_both_formats(tmp_path, monkeypatch):
    import ui.clips_page as module
    owner = clips_owner(tmp_path)
    session, clips = session_with_clips(tmp_path)
    owner.clips._found((session, clips))
    owner.clips.rows["C01"].tick.setChecked(True)
    owner.clips.rows["C02"].tick.setChecked(True)
    assert owner.clips_subtitles.isHidden() and owner.btn_clips_preview.isHidden()
    owner.clips_format_combo.setCurrentIndex(owner.clips_format_combo.findData("both"))
    assert not owner.clips_subtitles.isHidden() and not owner.btn_clips_preview.isHidden()
    assert owner.btn_clips_export.text() == "Export 2 clips (4 files)"
    owner.clips_subtitles.setChecked(False)
    started = []
    monkeypatch.setattr(module, "_start", lambda owner_, worker, *a: (
        started.append(worker) or SimpleNamespace(finished=SimpleNamespace(connect=lambda *x: None))))
    owner.clips.export()
    assert started[0].formats == ("horizontal", "vertical") and started[0].subtitles is False
    owner.close()
