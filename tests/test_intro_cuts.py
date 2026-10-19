"""Intro edits reach the render plan; no FFmpeg, media analysis or provider calls."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest
from PySide6.QtWidgets import QApplication, QWidget, QDialog, QComboBox

from analysis.cut_engine import Segment
from analysis.hero_groups.projects import prepare_selected_outputs, selected_project
from automation.exporter import ProjectExportPlanner
from core.track_state import TrackState
from ui.main_window import MainWindow

APP = QApplication.instance() or QApplication([])


def test_manual_intro_edit_enables_cuts_and_does_not_analyze():
    track = TrackState(path="intro.mp4", source_role="intro", duration=180,
        cuts_enabled=False, cuts=[Segment(5, 10), Segment(60, 70)],
        manual_cuts=[Segment(5, 10), Segment(60, 70)], keeps=[Segment(0, 180)])
    saved = []
    owner = SimpleNamespace(analysis_mode="classic", _save_workspace_for_mode=lambda t: saved.append(t.cuts_enabled),
        _refresh_analysis_workspace_ui=lambda t: None, _apply_track_cuts_ui=lambda t: None)
    MainWindow._finalize_manual_cut_edit_without_reanalysis(owner, track)
    assert track.cuts_enabled and saved == [True]
    assert track.keeps == [Segment(0, 5), Segment(10, 60), Segment(70, 180)]
    # Removing the cuts also works without RMS/Classic analysis.
    track.cuts = track.manual_cuts = []
    MainWindow._finalize_manual_cut_edit_without_reanalysis(owner, track)
    assert track.keeps == [Segment(0, 180)]


@pytest.mark.parametrize("legacy_disabled", [False, True])
def test_trimmed_intro_intervals_reach_the_actual_export_planner(tmp_path, legacy_disabled):
    from test_multi_source_export import multi_payload
    payload = multi_payload(tmp_path)
    intro = payload["tracks"][1]
    intro.update(duration=180., segment_source_in=20., segment_source_out=200.,
        cuts_enabled=not legacy_disabled, cuts=[{"start": 5., "end": 10.}, {"start": 60., "end": 70.}],
        manual_cuts=[{"start": 5., "end": 10.}, {"start": 60., "end": 70.}],
        keeps=[{"start": 0., "end": 5.}, {"start": 10., "end": 60.}, {"start": 70., "end": 180.}])
    before = deepcopy(payload)
    plans, _ = prepare_selected_outputs(payload, tmp_path / "outputs")
    planner = ProjectExportPlanner(duration_probe=lambda p: 500., audio_probe=lambda p: True)
    for output in plans:
        plan = planner.build(SimpleNamespace(project_path=output["project"]))
        segments = [s for s in plan.segments if s.get("role") == "intro"]
        assert [(s["v_in"], s["v_out"]) for s in segments] == [(20., 25.), (30., 80.), (90., 200.)]
        assert sum(s["duration"] for s in segments) == 165.
        assert plan.segments[3]["role"] == "gameplay"
    assert payload == before  # no destructive edit of the parent project


def test_fully_removed_intro_never_falls_back_to_full_clip(tmp_path):
    from test_multi_source_export import multi_payload
    payload = multi_payload(tmp_path)
    intro = payload["tracks"][1]
    intro.update(cuts_enabled=True, cuts=[{"start": 0., "end": 2.}], keeps=[])
    assert all(t.get("source_role") != "intro" for t in selected_project(payload)["tracks"])


def test_legacy_disabled_intro_cut_flag_repaired_on_restore_and_persisted(tmp_path):
    track = TrackState(path=str(tmp_path / "intro.mp4"))
    owner = SimpleNamespace(_segments_from_payload=lambda v: MainWindow._segments_from_payload(None, v),
        _clone_segments_list=lambda v: list(v), _tracks=[track], _get_active_track=lambda: track,
        _save_workspace_for_mode=lambda t: None,
        _segments_to_payload=lambda v: MainWindow._segments_to_payload(None, v))
    saved = {"source_role": "intro", "duration": 180., "cuts_enabled": False,
        "cuts": [{"start": 5., "end": 10.}], "manual_cuts": [{"start": 5., "end": 10.}],
        "keeps": [{"start": 0., "end": 180.}]}
    assert MainWindow._apply_restore_state(owner, track, saved)
    assert track.cuts_enabled and track.classic_cuts_enabled
    assert track.keeps == [Segment(0, 5), Segment(10, 180)]
    payload = MainWindow._build_session_items(owner, include_cfg=False, include_cuts=True)[0]
    restored = TrackState(path=track.path)
    MainWindow._apply_restore_state(owner, restored, json.loads(json.dumps(payload)))
    assert restored.cuts_enabled and restored.keeps == track.keeps


def test_changing_role_to_intro_preserves_existing_edit(tmp_path, monkeypatch):
    from ui.project_sources import SourcesPanel
    track = TrackState(path=str(tmp_path / "clip.mp4"), source_role="gameplay", duration=180,
        cuts_enabled=True, cuts=[Segment(5, 10)], manual_cuts=[Segment(5, 10)],
        keeps=[Segment(0, 5), Segment(10, 180)], gameplay_cuts_enabled=True)
    owner = QWidget()
    owner._tracks = [track]
    owner._active_track_index = 0
    owner._export_in_progress = owner._gameplay_running = lambda: False
    owner._push_undo_state = lambda **kw: None
    owner._mark_project_dirty = lambda: None
    owner._refresh_gameplay_panel = lambda: None
    owner._refresh_timeline_tracks = lambda **kw: None
    panel = SourcesPanel(owner)
    def accept(dialog):
        role = dialog.findChild(QComboBox)
        role.setCurrentIndex(role.findData("intro"))
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(QDialog, "exec", accept)
    panel.configure()
    assert track.source_role == "intro" and not track.gameplay_cuts_enabled
    assert track.cuts_enabled and track.keeps == [Segment(0, 5), Segment(10, 180)]
    assert track.cuts == track.manual_cuts == [Segment(5, 10)]
    owner.close()
