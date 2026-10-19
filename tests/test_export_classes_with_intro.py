"""Class selection remains visible when the player is showing an Intro."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QWidget

from core.track_state import TrackState
from ui.hero_groups import HeroGroupsPanel
from test_gameplay_export_selection import fixture

APP = QApplication.instance() or QApplication([])


def choice(panel, key):
    return next(panel.groups.topLevelItem(i) for i in range(panel.groups.topLevelItemCount())
                if panel.groups.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole) == key)


def test_intro_keeps_gameplay_class_choices_and_edits_correct_source(tmp_path):
    _, gameplay = fixture(tmp_path)
    intro = TrackState(path=str(tmp_path / "intro.mp4"), source_role="intro", duration=180)
    other = deepcopy(gameplay)
    other.path = str(tmp_path / "other-gameplay.mp4")
    owner = QWidget()
    owner._tracks = [gameplay, intro, other]
    owner._active_track_index = 1
    owner._mark_project_dirty = lambda: None
    panel = HeroGroupsPanel(owner)
    assert panel.groups.topLevelItemCount() == 2
    assert panel.games.topLevelItemCount() == 0  # Intro has no games of its own
    assert panel.group_source == str(Path(gameplay.path).resolve())
    key = panel.groups.topLevelItem(0).data(0, Qt.ItemDataRole.UserRole)
    before_other = deepcopy(other.gameplay_grouping)
    choice(panel, key).setCheckState(0, Qt.CheckState.Unchecked)
    assert key not in gameplay.gameplay_grouping["selected"]
    assert other.gameplay_grouping == before_other
    assert intro.gameplay_grouping == {}
    assert "intro.mp4" not in panel.selection_scope.text()

    owner._active_track_index = 2
    panel.refresh()
    assert choice(panel, key).checkState(0) == Qt.CheckState.Checked
    owner._active_track_index = 1
    panel.refresh()
    assert panel.group_source == other.path
    assert choice(panel, key).checkState(0) == Qt.CheckState.Checked
    assert key not in gameplay.gameplay_grouping["selected"]
    owner.close()


def test_intro_without_gameplay_does_not_invent_class_choices(tmp_path):
    owner = QWidget()
    owner._tracks = [TrackState(path=str(tmp_path / "intro.mp4"), source_role="intro", duration=180)]
    owner._active_track_index = 0
    owner._mark_project_dirty = lambda: None
    panel = HeroGroupsPanel(owner)
    assert panel.groups.topLevelItemCount() == 0
    assert panel.group_source == ""
    owner.close()


def test_class_duration_uses_final_keeps_and_intro_is_separate(tmp_path):
    from dataclasses import asdict
    from analysis.cut_engine import Segment
    from core.project import Project

    _, track = fixture(tmp_path)
    intro = TrackState(path=str(tmp_path / "intro.mp4"), source_role="intro",
        duration=20, intro_targets=["*"], cuts_enabled=True,
        keeps=[Segment(0, 5), Segment(10, 20)])
    owner = QWidget()
    owner._tracks = [track, intro]
    owner._active_track_index = 1
    owner._mark_project_dirty = lambda: None
    owner.project = Project.create_default()
    owner.project.gameplay_export = {"mode": "by_class", "wins_only": True}
    owner._build_project_file_payload = lambda: {"format": "autocutter_project", "version": 2,
        "tracks": [asdict(t) for t in owner._tracks]}
    panel = HeroGroupsPanel(owner)
    assert panel.groups.headerItem().text(4) == "Duration"
    assert choice(panel, "hunter").text(4) == "3:10"  # two games, including a 10s manual cut
    assert choice(panel, "mage").text(4) == "1:40"
    assert "gameplay 3:10 + Intro 0:15 = 3:25" in panel.plan_summary.text()
    assert "gameplay 1:40 + Intro 0:15 = 1:55" in panel.plan_summary.text()
    choice(panel, "hunter").setCheckState(0, Qt.CheckState.Unchecked)
    assert choice(panel, "hunter").text(4) == "3:10"  # potential duration remains visible
    assert panel.preview_summary["outputs"] == 1
    assert "hunter" not in track.gameplay_grouping["selected"]  # no projection mutation
    track.keeps = [Segment(k.start, 185 if k.end == 195 else k.end) for k in track.keeps]
    panel.refresh()
    assert choice(panel, "hunter").text(4) == "3:00"
    owner.close()
