from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QDoubleSpinBox, QMenu, QMessageBox, QWidget
from PySide6.QtCore import QTimer

from analysis.cut_engine import Segment, invert_to_keeps, merge_overlaps
from analysis.gameplay import selection
from analysis.hero_groups.projects import assign_group, exportable_group_games, group_project, read_project
from analysis.hero_groups.recognition import refresh_groups
from core.project_file import make_payload_portable
from core.track_state import TrackState
from ui.gameplay_timeline import GameplayTimelineController
from ui.hero_groups import HeroGroupsPanel
from test_timeline_widget import make_timeline

APP = QApplication.instance() or QApplication([])


def fixture(tmp_path):
    source = tmp_path / "local video.mp4"
    source.write_bytes(b"not decoded")
    games = [
        {"id": "one", "index": 1, "start": 10., "end": 100., "result": "WIN"},
        {"id": "two", "index": 2, "start": 150., "end": 250., "result": "LOSS"},
        {"id": "three", "index": 3, "start": 300., "end": 400., "result": "WIN"},
        {"id": "partial", "index": 4, "start": 500., "end": None, "result": "UNKNOWN"},
    ]
    report = {"duration": 600., "games": games, "keep_islands": [
        {"start": 12., "end": 103., "game": games[0]},
        {"start": 300., "end": 402., "game": games[2]},
    ]}
    grouping = {"games": [{"key": g["id"], "game": deepcopy(g), "group": group, "automatic_group": group}
                          for g, group in zip(games, ["A", "B", "A", "B"])],
                "overrides": {}, "names": {}, "selected": ["A", "B"]}
    refresh_groups(grouping)
    track = TrackState(path=str(source), duration=600., segment_source_out=600., cuts_enabled=True,
                       gameplay_cuts_enabled=True, gameplay_mode="keep", gameplay_report=report,
                       gameplay_grouping=grouping, gameplay_keeps=[Segment(12, 103), Segment(300, 402)],
                       gameplay_detected_keeps=[Segment(12, 103), Segment(300, 402)],
                       gameplay_islands=[{"game_id": "one", "start": 12., "end": 103.},
                                         {"game_id": "three", "start": 300., "end": 402.}],
                       keeps=[Segment(12, 103), Segment(300, 402)])
    track.gameplay_cuts = invert_to_keeps(600, track.gameplay_keeps, min_keep=0)
    track.cuts = list(track.gameplay_cuts)
    return track


def payload(track):
    return {"format": "autocutter_project", "version": 2, "tracks": [asdict(track)]}


def owner_for(track):
    owner = QWidget()
    owner._tracks = [track]
    owner._active_track_index = 0
    owner.timeline = make_timeline()
    owner.dirty_count = 0
    owner.undo = []
    owner._push_undo_state = lambda **kw: owner.undo.append(deepcopy(owner._tracks))
    def dirty():
        owner.dirty_count += 1
    owner._mark_project_dirty = dirty
    def refresh(**kwargs):
        owner.timeline.tracks[0]["clips"][0]["end"] = 600.
        clip = owner.timeline._audio_clips()[0]
        clip.update(end=600., gameplay_boxes=selection.timeline_boxes(track),
                    gameplay_keeps=track.gameplay_keeps, gameplay_islands=track.gameplay_islands,
                    gameplay_cuts=track.gameplay_cuts, cuts=track.cuts)
        owner.timeline.duration = 600.
        owner.timeline.view_span = 600.
        owner.timeline._changed()
    def recompute(indices):
        for i in indices:
            t = owner._tracks[i]
            # Simulate the existing cut engine, including an independent manual cut.
            t.cuts = merge_overlaps(t.manual_cuts + (t.gameplay_cuts if t.gameplay_cuts_enabled else []))
            t.keeps = invert_to_keeps(t.duration, t.cuts, min_keep=0)
            selection.enforce_selection(t)
        dirty()
        refresh()
    owner._refresh_timeline_tracks = refresh
    owner._recompute_gameplay_tracks = recompute
    owner.gameplay_actions = GameplayTimelineController(owner)
    owner.hero_groups_panel = HeroGroupsPanel(owner)
    refresh()
    return owner


def mouse(widget, button, x, y, kind=QEvent.Type.MouseButtonPress):
    QApplication.sendEvent(widget, QMouseEvent(kind, QPointF(x, y), QPointF(x, y), button,
                                               button, Qt.KeyboardModifier.NoModifier))


def test_click_toggle_keeps_box_checkbox_and_yellow_independent(tmp_path):
    track = fixture(tmp_path)
    owner = owner_for(track)
    panel, tl = owner.hero_groups_panel, owner.timeline
    panel.toggle_highlight(panel.games.topLevelItem(0))
    yellow = tl.gameHighlight()
    lane = next(l for l in tl._lanes() if l.kind == "video")
    x, y = tl._t_to_xf(50), lane.top + lane.h*.6
    mouse(tl, Qt.MouseButton.LeftButton, x, y)
    assert track.gameplay_grouping["editor"]["included"]["one"] is False
    assert panel.games.topLevelItem(0).checkState(5) == Qt.CheckState.Unchecked
    assert len(tl._gameplay_islands()) == 4
    assert tl._gameplay_islands()[0]["meta"]["included"] is False
    assert tl.gameHighlight() == yellow
    assert exportable_group_games(payload(track)) == {"A": {"three"}}
    mouse(tl, Qt.MouseButton.LeftButton, x, y)
    assert panel.games.topLevelItem(0).checkState(5) == Qt.CheckState.Checked
    assert exportable_group_games(payload(track)) == {"A": {"one", "three"}}
    panel.games.topLevelItem(0).setCheckState(5, Qt.CheckState.Unchecked)
    assert not tl._gameplay_islands()[0]["meta"]["included"]
    assert tl.gameHighlight() == yellow
    assert owner.undo and owner.dirty_count
    owner.close()


def test_export_selection_and_reassignment_chronology(tmp_path):
    track = fixture(tmp_path)
    owner = owner_for(track)
    # Add a same-class, distinct portrait group: IDs must never be collapsed to Hunter.
    track.gameplay_grouping["games"][0]["class_evidence"] = "hunter"
    track.gameplay_grouping["games"][1]["class_evidence"] = "hunter"
    refresh_groups(track.gameplay_grouping)
    owner.gameplay_actions.assign(0, "three", "B")
    assert track.gameplay_grouping["overrides"]["three"] == "B"
    assert track.gameplay_grouping["games"][2]["automatic_group"] == "A"
    assert exportable_group_games(payload(track)) == {"A": {"one"}, "B": {"three"}}
    owner.gameplay_actions.set_included(0, "two", True)
    child = group_project(payload(track), "B")
    assert [g["id"] for g in child["gameplay"]["games"]] == ["two", "three"]
    assert [g["result"] for g in child["gameplay"]["games"]] == ["LOSS", "WIN"]
    assert [k["start"] for k in child["tracks"][0]["keeps"]] == [150., 300.]
    owner.gameplay_actions.set_included(0, "three", False)
    assert [g["id"] for g in group_project(payload(track), "B")["gameplay"]["games"]] == ["two"]
    owner.close()


def test_manual_interval_unknown_save_reopen_recalculate_and_child_isolation(tmp_path):
    track = fixture(tmp_path)
    grouping = selection.initialize([track])
    key = selection.create_interval(track.gameplay_report, grouping, 420, 460, 600, "B")
    second = selection.create_interval(track.gameplay_report, grouping, 470, 490, 600, "A")
    assign_group(grouping, "one", "B")
    grouping["editor"]["included"]["three"] = False
    selection.apply([track], grouping)
    track.keeps = list(track.gameplay_keeps)
    path = tmp_path / "edit.autocutter"
    path.write_text(json.dumps(make_payload_portable(payload(track), path)), encoding="utf-8")
    saved = read_project(path)
    saved_grouping = saved["tracks"][0]["gameplay_grouping"]
    assert saved_grouping == grouping
    assert track.gameplay_report["games"][-1]["end"] is None
    assert len(track.gameplay_report["games"]) == 4
    fresh = deepcopy(fixture(tmp_path).gameplay_grouping)
    fresh.pop("editor", None)
    merged = selection.preserve_editor(saved_grouping, fresh)
    assert merged["overrides"]["one"] == "B"
    assert next(r for r in merged["games"] if r["key"] == "one")["group"] == "B"
    assert len([r for r in merged["games"] if r["key"] == key]) == 1
    assert merged["editor"]["included"]["three"] is False
    child = group_project(saved, "B")
    assert key in {g["id"] for g in child["gameplay"]["games"]}
    assert next(g for g in child["gameplay"]["games"] if g["id"] == key)["result"] == "UNKNOWN"
    assert second not in {g["id"] for g in child["tracks"][0]["gameplay_grouping"]["editor"]["manual_games"]}
    selection.delete_interval(merged, key)
    assert key not in {r["key"] for r in merged["games"]}
    with pytest.raises(ValueError, match="Only manually"):
        selection.delete_interval(merged, "one")


@pytest.mark.parametrize("start,end", [(-1, 5), (30, 30), (50, 40), (590, 601), (float("nan"), 590), (90, 110)])
def test_invalid_and_overlapping_intervals_rejected(tmp_path, start, end):
    track = fixture(tmp_path)
    with pytest.raises(ValueError):
        selection.create_interval(track.gameplay_report, deepcopy(track.gameplay_grouping), start, end, 600, "A")


def test_all_excluded_never_exported_even_after_restore_cut(tmp_path):
    track = fixture(tmp_path)
    grouping = selection.initialize([track])
    grouping["editor"]["included"].update(one=False, three=False)
    selection.apply([track], grouping)
    assert track.gameplay_keeps == [] and exportable_group_games(payload(track)) == {}
    assert len(selection.timeline_boxes(track)) == 4
    with pytest.raises(ValueError, match="no exportable"):
        group_project(payload(track), "A")
    track.gameplay_cuts_enabled = False
    track.cuts_enabled = False
    track.cuts = []
    track.keeps = [Segment(0, 600)]
    selection.enforce_selection(track)
    assert track.cuts_enabled
    assert all(min(k.end, 103) <= max(k.start, 12) for k in track.keeps)
    assert all(min(k.end, 402) <= max(k.start, 300) for k in track.keeps)


def test_legacy_removed_game_does_not_resurrect_on_first_toggle(tmp_path):
    track = fixture(tmp_path)
    track.gameplay_keeps.pop(0)
    track.gameplay_islands.pop(0)
    grouping = selection.initialize([track])
    assert grouping["editor"]["included"]["one"] is False
    grouping["editor"]["included"]["two"] = True
    selection.apply([track], grouping)
    assert [r["game_id"] for r in track.gameplay_islands] == ["two", "three"]


def test_split_fragments_share_source_time_toggle_and_boundaries(tmp_path):
    track = fixture(tmp_path)
    left, right = deepcopy(track), deepcopy(track)
    left.segment_source_out = 60
    left.gameplay_keeps = [Segment(12, 60)]
    left.gameplay_islands = [{"game_id": "one"}]
    right.segment_source_in, right.segment_source_out = 60, 600
    right.gameplay_keeps = [Segment(0, 43), Segment(240, 342)]
    right.gameplay_islands = [{"game_id": "one"}, {"game_id": "three"}]
    grouping = selection.initialize([left, right])
    grouping["editor"]["boundaries"]["one"] = [15, 105]
    selection.apply([left, right], grouping)
    assert (left.gameplay_keeps[0].start, left.gameplay_keeps[0].end) == (15, 60)
    assert (right.gameplay_keeps[0].start, right.gameplay_keeps[0].end) == (0, 45)
    grouping["editor"]["included"]["one"] = False
    selection.apply([left, right], grouping)
    assert left.gameplay_keeps == []
    assert right.gameplay_islands[0]["game_id"] == "three"


def test_context_actions_and_manual_delete_confirmation(tmp_path, monkeypatch):
    track = fixture(tmp_path)
    owner = owner_for(track)
    menu, actions = QMenu(), {}
    owner.gameplay_actions.add_menu(menu, actions, 0, "one", 0)
    assert {"Exclude from export", "Assign to class / portrait group", "Lock manual assignment", "Edit START / END…"} <= {
        a.text() for a in menu.actions()}
    lock = next(a for a in actions if a.text() == "Lock manual assignment")
    actions[lock]()
    assert track.gameplay_grouping["overrides"]["one"] == "A"
    owner.gameplay_actions.lock(0, "one", False)
    assert "one" not in track.gameplay_grouping["overrides"]
    owner.gameplay_actions._change(0, lambda g, r: selection.create_interval(r, g, 420, 460, 600, "A"))
    key = track.gameplay_grouping["editor"]["manual_games"][0]["id"]
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **kw: QMessageBox.StandardButton.No)
    owner.gameplay_actions.delete(0, key)
    assert track.gameplay_grouping["editor"]["manual_games"]
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **kw: QMessageBox.StandardButton.Yes)
    owner.gameplay_actions.delete(0, key)
    assert not track.gameplay_grouping["editor"]["manual_games"]
    owner.close()


def test_precise_dialog_creates_unknown_and_can_edit(tmp_path):
    track = fixture(tmp_path)
    owner = owner_for(track)
    def accept_dialog(start, end):
        dialog = APP.activeModalWidget()
        assert isinstance(dialog, QDialog)
        spins = dialog.findChildren(QDoubleSpinBox)
        spins[0].setValue(start)
        spins[1].setValue(end)
        buttons = dialog.findChild(QDialogButtonBox)
        buttons.button(QDialogButtonBox.StandardButton.Ok).click()
    QTimer.singleShot(0, lambda: accept_dialog(420.125, 460.5))
    owner.gameplay_actions.interval_dialog(0, None, 420)
    game = track.gameplay_grouping["editor"]["manual_games"][0]
    assert game["result"] == "UNKNOWN" and game["start"] == 420.125
    QTimer.singleShot(0, lambda: accept_dialog(421.25, 461.75))
    owner.gameplay_actions.interval_dialog(0, game["id"])
    assert track.gameplay_grouping["editor"]["boundaries"][game["id"]] == [421.25, 461.75]
    owner.close()


def test_right_click_metadata_and_normal_audio_cut_interaction(tmp_path):
    track = fixture(tmp_path)
    owner = owner_for(track)
    tl = owner.timeline
    events = []
    tl.contextMenuRequested.connect(lambda t, p, meta: events.append(meta))
    video = next(l for l in tl._lanes() if l.kind == "video")
    mouse(tl, Qt.MouseButton.RightButton, tl._t_to_xf(50), video.top+30)
    assert events[-1]["island"]["game_key"] == "one"
    mouse(tl, Qt.MouseButton.RightButton, tl._t_to_xf(430), video.top+30)
    assert events[-1]["island"] is None and events[-1]["track_state_idx"] == 0
    track.manual_cuts = [Segment(30, 31)]
    owner._recompute_gameplay_tracks([0])
    audio = next(l for l in tl._lanes() if l.kind == "audio")
    before = deepcopy(track.gameplay_grouping)
    mouse(tl, Qt.MouseButton.LeftButton, tl._t_to_xf(30.5), audio.top+20)
    assert track.gameplay_grouping == before
    assert tl.selectedCutIndices()
    owner.close()


def test_incomplete_inclusion_rejected_without_mutating_detection(tmp_path, monkeypatch):
    track = fixture(tmp_path)
    owner = owner_for(track)
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: errors.append(args[-1]))
    original = deepcopy(track.gameplay_report)
    owner.gameplay_actions.set_included(0, "partial", True)
    assert errors and "both START and END" in errors[-1]
    assert "editor" not in track.gameplay_grouping
    owner.gameplay_actions.bounds(0, "partial", 500, 590)
    owner.gameplay_actions.set_included(0, "partial", True)
    assert any(i["game_id"] == "partial" for i in track.gameplay_islands)
    assert track.gameplay_report == original
    owner.close()


def test_gui_save_restore_helpers_preserve_overrides_and_manual_rows(tmp_path):
    from ui.main_window import MainWindow
    track = fixture(tmp_path)
    owner = owner_for(track)
    owner._get_active_track = lambda: track
    owner._save_workspace_for_mode = lambda t: None
    owner._segments_to_payload = lambda values: MainWindow._segments_to_payload(owner, values)
    owner._segments_from_payload = lambda values: MainWindow._segments_from_payload(owner, values)
    owner._clone_segments_list = lambda values: list(values)
    owner.gameplay_actions.set_included(0, "one", False)
    owner.gameplay_actions.assign(0, "three", "B")
    owner.gameplay_actions._change(0, lambda g, r: selection.create_interval(r, g, 420, 460, 600, "A"))
    saved = MainWindow._build_session_items(owner, include_cfg=False, include_cuts=True)[0]
    reopened = TrackState(path=track.path)
    assert MainWindow._apply_restore_state(owner, reopened, json.loads(json.dumps(saved)))
    assert reopened.gameplay_grouping == track.gameplay_grouping
    assert reopened.gameplay_keeps == track.gameplay_keeps
    assert selection.timeline_boxes(reopened) == selection.timeline_boxes(track)
    assert exportable_group_games(payload(reopened)) == exportable_group_games(payload(track))
    owner.close()


def test_manual_insert_and_delete_keep_selected_identity(tmp_path):
    track = fixture(tmp_path)
    owner = owner_for(track)
    owner.timeline.setSelectedIsland((0, 2))
    owner.gameplay_actions._change(0, lambda g, r: selection.create_interval(r, g, 110, 130, 600, "A"))
    assert owner.timeline.selectedIsland() == (0, 3)
    key = track.gameplay_grouping["editor"]["manual_games"][0]["id"]
    owner.gameplay_actions._change(0, lambda g, r: selection.delete_interval(g, key))
    assert owner.timeline.selectedIsland() == (0, 2)
    owner.close()


def test_edge_drag_updates_persistent_boundaries_without_toggling(tmp_path):
    track = fixture(tmp_path)
    owner = owner_for(track)
    tl = owner.timeline
    video = next(l for l in tl._lanes() if l.kind == "video")
    y = video.top+video.h*.6
    x = tl._t_to_xf(12)
    mouse(tl, Qt.MouseButton.LeftButton, x, y)
    QApplication.sendEvent(tl, QMouseEvent(QEvent.Type.MouseMove, QPointF(x+15, y), QPointF(x+15, y),
                                          Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
                                          Qt.KeyboardModifier.NoModifier))
    mouse(tl, Qt.MouseButton.LeftButton, x+15, y, QEvent.Type.MouseButtonRelease)
    assert track.gameplay_grouping["editor"]["boundaries"]["one"][0] > 12
    assert track.gameplay_keeps[0].end == 103
    assert selection.catalog(track.gameplay_report, track.gameplay_grouping)[0]["included"]
    assert track.gameplay_report["games"][0]["start"] == 10
    owner.gameplay_actions.reset(0, "one")
    assert track.gameplay_keeps[0].start == 12
    owner.close()


def test_child_reopen_does_not_duplicate_manual_intervals(tmp_path):
    track = fixture(tmp_path)
    grouping = selection.initialize([track])
    key = selection.create_interval(track.gameplay_report, grouping, 420, 460, 600, "A")
    selection.apply([track], grouping)
    track.keeps = list(track.gameplay_keeps)
    child = group_project(payload(track), "A")["tracks"][0]
    rows = selection.catalog(child["gameplay_report"], child["gameplay_grouping"])
    assert len([r for r in rows if r["key"] == key]) == 1
    selection.delete_interval(child["gameplay_grouping"], key)
    assert key not in {r["key"] for r in selection.catalog(child["gameplay_report"], child["gameplay_grouping"])}
