"""Current WIN selection, class outputs and additive context menus; no VOD/AI calls."""
from copy import deepcopy
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import numpy as np
import pytest
from PySide6.QtCore import Qt, QPointF, QEvent
from PySide6.QtGui import QMouseEvent, QAction
from PySide6.QtWidgets import QApplication, QWidget
from analysis.cut_engine import Segment
from analysis.classic import compute_classic_cuts
from analysis.hero_groups.projects import exportable_group_games, group_project, prepare_outputs, read_project
from analysis.hero_groups.recognition import refresh_groups
from core.track_state import TrackState
from core.project_file import make_payload_portable
from ui.gameplay_editing_ui import island_metadata, remove_gameplay_island
from ui.hero_groups import HeroGroupsPanel
from test_hero_groups import project
from test_timeline_widget import make_timeline

APP = QApplication.instance() or QApplication([])


def fixture(tmp_path):
    payload = project(tmp_path)
    data = payload["tracks"][0]
    grouping = data["gameplay_grouping"]
    for row in grouping["games"]:
        row["group"] = "hunter" if row["group"] == "A" else "mage"
    grouping["selected"] = ["hunter", "mage"]
    refresh_groups(grouping)
    keeps = [Segment(**k) for k in data["gameplay_keeps"]]
    track = TrackState(path=data["path"], duration=500.0, segment_source_out=500.0,
                       cuts_enabled=True, gameplay_mode="keep", gameplay_cuts_enabled=True,
                       gameplay_keeps=keeps, gameplay_detected_keeps=deepcopy(keeps),
                       gameplay_report=data["gameplay_report"], gameplay_grouping=grouping,
                       gameplay_islands=island_metadata(data["gameplay_report"], 0, 500),
                       manual_cuts=[Segment(110, 120)])
    from ui.gameplay_editing_ui import complement
    track.gameplay_cuts = complement(keeps, 500)
    track.keeps = [Segment(**k) for k in data["keeps"]]
    return payload, track


def edited(payload, track):
    result = deepcopy(payload)
    data = result["tracks"][0]
    for key in ("gameplay_keeps", "keeps", "gameplay_cuts"):
        data[key] = [{"start": k.start, "end": k.end} for k in getattr(track, key)]
    data["gameplay_islands"] = deepcopy(track.gameplay_islands)
    return result


def recompute(track):
    track.cuts, track.keeps = compute_classic_cuts(
        np.array([]), track.duration, .03, {}, manual_cuts=track.manual_cuts,
        suppressed_cuts=track.suppressed_cuts, gameplay_cuts=track.gameplay_cuts,
    )


def test_removed_win_is_not_exported_and_remaining_order_and_manual_cuts_survive(tmp_path):
    payload, track = fixture(tmp_path)
    before_report = deepcopy(track.gameplay_report)
    track.suppressed_cuts = [Segment(105, 130), Segment(310, 315)]
    assert remove_gameplay_island([track], 0, 0) == [0]
    recompute(track)
    updated = edited(payload, track)
    assert exportable_group_games(updated) == {"hunter": {"3"}, "mage": {"2"}}
    child = group_project(updated, "hunter")
    assert child["tracks"][0]["keeps"] == [{"start": 295.0, "end": 395.0}]
    assert [g["index"] for g in child["gameplay"]["games"]] == [3]
    assert track.manual_cuts == [Segment(110, 120)]
    assert track.suppressed_cuts == [Segment(310, 315)]
    assert track.gameplay_report == before_report
    assert len(track.gameplay_detected_keeps) == 3
    import json
    target = tmp_path / "edited.autocutter"
    target.write_text(json.dumps(make_payload_portable(updated, target)), encoding="utf8")
    assert exportable_group_games(read_project(target)) == {"hunter": {"3"}, "mage": {"2"}}


def test_removing_last_win_cannot_fall_back_to_original_report(tmp_path):
    payload, track = fixture(tmp_path)
    for _ in range(3):
        remove_gameplay_island([track], 0, 0)
    recompute(track)
    updated = edited(payload, track)
    assert track.gameplay_keeps == [] and track.keeps == []
    assert track.gameplay_cuts == [Segment(0, 500)]
    assert len(track.gameplay_report["keep_islands"]) == 3
    assert exportable_group_games(updated) == {}
    with pytest.raises(ValueError, match="no exportable WIN"):
        group_project(updated, "hunter")
    plans, warnings = prepare_outputs(updated, tmp_path / "output")
    assert plans == [] and len(warnings) == 2


def test_removed_game_fragments_across_splits_but_not_another_source(tmp_path):
    source = str(tmp_path / "vod.mp4")
    first = TrackState(path=source, duration=150, segment_source_out=150,
                       gameplay_keeps=[Segment(100, 150)], gameplay_islands=[{"game_id": "1"}])
    second = TrackState(path=source, duration=150, segment_source_in=150, segment_source_out=300,
                        gameplay_keeps=[Segment(0, 40), Segment(50, 140)],
                        gameplay_islands=[{"game_id": "1"}, {"game_id": "2"}])
    other = TrackState(path=str(tmp_path / "other.mp4"), duration=150,
                       gameplay_keeps=[Segment(100, 150)], gameplay_islands=[{"game_id": "1"}])
    assert remove_gameplay_island([first, second, other], 1, 0) == [0, 1]
    assert first.gameplay_keeps == []
    assert second.gameplay_keeps == [Segment(50, 140)]
    assert second.gameplay_islands == [{"game_id": "2"}]
    assert other.gameplay_keeps == [Segment(100, 150)]
    assert remove_gameplay_island([first], 0, 2) == []


def test_export_choices_hide_removed_or_fully_cut_classes_and_restore_with_undo(tmp_path):
    payload, track = fixture(tmp_path)
    owner = QWidget()
    owner._tracks = [track]
    owner._active_track_index = 0
    owner._mark_project_dirty = lambda: None
    owner.section_gameplay_exports = QWidget(owner)
    panel = HeroGroupsPanel(owner)
    assert panel.groups.parentWidget() is panel.outputs
    assert panel.export.parentWidget() is panel.outputs
    assert panel.games.parentWidget() is panel
    assert panel.groups.topLevelItemCount() == 2
    before = deepcopy(track)
    remove_gameplay_island([track], 0, 1)  # only Mage WIN
    recompute(track)
    panel.refresh()
    assert panel.groups.topLevelItemCount() == 1
    assert panel.groups.topLevelItem(0).text(0) == "Hunter"
    assert panel.groups.topLevelItem(0).text(2) == "2"
    for _ in range(2):
        remove_gameplay_island([track], 0, 0)
    recompute(track)
    panel.refresh()
    assert panel.groups.topLevelItemCount() == 0 and not panel.export.isEnabled()
    assert panel.outputs.isHidden() and owner.section_gameplay_exports.isHidden()
    owner._tracks = [before]  # timeline undo restores the existing TrackState snapshot
    panel.refresh()
    assert panel.groups.topLevelItemCount() == 2
    assert not panel.outputs.isHidden() and not owner.section_gameplay_exports.isHidden()
    before.keeps = [Segment(95, 195), Segment(295, 395)]  # all Mage footage manually cut
    panel.refresh()
    assert panel.groups.topLevelItemCount() == 1
    owner.close()


def test_no_gameplay_keeps_normal_export_ui_unchanged():
    owner = QWidget()
    owner._tracks = [TrackState(path="normal.mp4", duration=30, keeps=[Segment(0, 30)])]
    owner._active_track_index = 0
    panel = HeroGroupsPanel(owner)
    assert panel.outputs.isHidden() and not panel.export.isEnabled()
    assert owner._tracks[0].keeps == [Segment(0, 30)]
    owner.close()


@pytest.mark.parametrize("lane_kind", ["video", "audio", "cuts"])
def test_right_click_win_adds_metadata_without_replacing_cut_or_clip_metadata(lane_kind):
    timeline = make_timeline()
    menus = []
    timeline.contextMenuRequested.connect(lambda t, pos, meta: menus.append(meta))
    lane = next(ln for ln in timeline._lanes() if ln.kind == lane_kind)
    for moment in (40, 70):
        point = QPointF(timeline._t_to_xf(moment), lane.top + lane.h / 2)
        QApplication.sendEvent(timeline, QMouseEvent(QEvent.MouseButtonPress, point, point,
                              Qt.RightButton, Qt.RightButton, Qt.NoModifier))
    assert menus[0]["island"] == {"track_state_idx": 0, "index": 0}
    assert "cut_idx" in menus[0] and "selected" in menus[0] and "clip_id" in menus[0]
    assert menus[1]["island"] is None
    timeline.close()


def test_existing_context_actions_stay_with_optional_win_removal(monkeypatch):
    from ui.main_window import MainWindow
    import ui.main_window as module
    menus = []
    selected = []
    class Menu:
        def __init__(self, parent):
            self.items = []
            menus.append(self)

        def addAction(self, text):
            action = QAction(text)
            self.items.append(action)
            return action

        def addSeparator(self):
            pass

        def actions(self):
            return self.items

        def exec(self, pos):
            return next((a for a in self.items if a.text() == "Remove this WIN from the edit"), None)

    monkeypatch.setattr(module, "QMenu", Menu)
    owner = QWidget()
    owner._tracks = [TrackState(duration=100)]
    owner._active_track_index = 0
    owner.duration = 100
    owner.cuts = [Segment(30, 31)]
    owner._pending_cut_start = owner._pending_cut_end = None
    owner._select_all_cuts = lambda: None
    owner._on_clip_split_requested = lambda *args: None
    owner._remove_gameplay_island = lambda ts, index: selected.append((ts, index))
    owner._get_active_track = lambda: owner._tracks[0]
    owner._app_log = lambda *a, **kw: None
    meta = {"row_kind": "video", "track_state_idx": 0, "local_t": 40, "clip_id": "v1",
            "island": {"track_state_idx": 0, "index": 0}}
    MainWindow._on_timeline_context_menu(owner, 40, QPointF(), meta)
    labels = [a.text() for a in menus[0].actions()]
    assert "Remove this WIN from the edit" in labels
    assert "Split video here" in labels and "Remove this video" in labels
    assert "Mark cut start here\tI" in labels and "Cut everything after here" in labels
    assert selected == [(0, 0)]
    meta["island"] = None
    MainWindow._on_timeline_context_menu(owner, 70, QPointF(), meta)
    assert "Remove this WIN from the edit" not in [a.text() for a in menus[1].actions()]
    owner.close()
