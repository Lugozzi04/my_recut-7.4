from __future__ import annotations

from copy import deepcopy
import io
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest

pytest.importorskip("cv2")
from PySide6.QtWidgets import QApplication, QWidget, QInputDialog
from PySide6.QtCore import Qt
from core.track_state import TrackState
from analysis.hero_groups.recognition import features, PortraitSettings, group_signatures
from analysis.hero_groups.quality import confirmation_template, confirmation_visible
from analysis.hero_groups.projects import prepare_outputs, group_project, read_project
from analysis.hero_groups.packaging import group_context
from automation.cli import run_cli
from test_hero_groups import project, art, record

APP = QApplication.instance() or QApplication([])


def test_confirmation_overlay_rejected_but_real_art_kept():
    import cv2
    import numpy as np

    button = confirmation_template()
    image = np.zeros((80, 64), np.uint8)
    image[25:68, :] = button
    bgr = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    assert confirmation_visible(bgr)
    assert features(bgr) is None
    assert features(art()) is not None


def test_group_ui_review_manual_edits_and_selection(tmp_path, monkeypatch):
    from ui.hero_groups import HeroGroupsPanel

    p = project(tmp_path)
    owner = QWidget()
    t = p["tracks"][0]
    from analysis.hero_groups.recognition import refresh_groups
    grouping = t["gameplay_grouping"]
    for row in grouping["games"]:
        row["group"] = "hunter" if row["group"] == "A" else "mage"
    grouping["selected"] = ["hunter", "mage"]
    refresh_groups(grouping)
    from analysis.cut_engine import Segment
    track = TrackState(path=t["path"], duration=t["duration"], cuts_enabled=True,
                       keeps=[Segment(**k) for k in t["keeps"]], gameplay_mode="keep",
                       gameplay_keeps=[Segment(**k) for k in t["gameplay_keeps"]],
                       gameplay_report=t["gameplay_report"], gameplay_grouping=grouping)
    owner._tracks = [track]
    owner._active_track_index = 0
    dirty = []
    owner._mark_project_dirty = lambda: dirty.append(True)
    panel = HeroGroupsPanel(owner)
    assert panel.games.topLevelItemCount() == 3 and panel.groups.topLevelItemCount() == 2
    assert not hasattr(panel, "analyze")
    assert panel.status.text().startswith("Classes ready.")
    assert not hasattr(panel, "refs")
    monkeypatch.setattr(QInputDialog, "getItem", lambda *a, **kw: ("Hunter", True))
    panel.edit_game(panel.games.topLevelItem(1), 2)
    assert track.gameplay_grouping["overrides"]["2"] == "hunter"
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **kw: ("Nome manuale", True))
    panel.edit_name(panel.groups.topLevelItem(0), 1)
    assert track.gameplay_grouping["names"]["hunter"] == "Nome manuale"
    panel.groups.topLevelItem(0).setCheckState(0, Qt.CheckState.Unchecked)
    assert track.gameplay_grouping["selected"] == []
    assert dirty
    owner.close()


def test_group_worker_background_completion_no_live_thread(tmp_path, monkeypatch):
    from ui.hero_groups import HeroGroupsPanel, GroupWorker
    import ui.hero_groups as module

    p = project(tmp_path)
    owner = QWidget()
    owner._tracks = [TrackState(path=p["tracks"][0]["path"], gameplay_report=p["tracks"][0]["gameplay_report"])]
    owner._active_track_index = 0
    owner._export_in_progress = lambda: False
    owner._gameplay_running = lambda: False
    owner._mark_project_dirty = lambda: None
    panel = HeroGroupsPanel(owner)
    monkeypatch.setattr(module, "analyze_project", lambda *a, **kw: p)
    monkeypatch.setattr(module, "HeroGroupingService", lambda **kw: object())
    panel._start(GroupWorker(p))
    deadline = time.monotonic() + 5
    while panel.work_thread is not None and time.monotonic() < deadline:
        APP.processEvents()
        time.sleep(0.005)
    assert panel.work_thread is None
    assert owner._tracks[0].gameplay_grouping["groups"]
    owner.close()


def test_cli_groups_cached_json_and_analyze_no_runtime(tmp_path, monkeypatch):
    from analysis.hero_groups.recognition import atomic_json
    from core.project_file import make_payload_portable
    import analysis.hero_groups.projects as module

    p = project(tmp_path)
    target = tmp_path / "progetto è 🎮.autocutter"
    atomic_json(target, make_payload_portable(p, target))
    for args in ([str(target), "--json"], [str(target), "--analyze", "--json"]):
        out, err = io.StringIO(), io.StringIO()
        monkeypatch.setattr(module, "analyze_project", lambda payload, **kw: payload)
        code = run_cli(
            ["groups", *args], stdout=out, stderr=err, runtime_factory=lambda **kw: pytest.fail("must not run analysis")
        )
        assert code == 0, err.getvalue()
        data = json.loads(out.getvalue())
        assert len(data["sources"][0]["groups"]["groups"]) == 2
        assert data["project"] == str(target.resolve())


def test_cli_groups_export_selected_reuses_saved_project(tmp_path, monkeypatch):
    import analysis.hero_groups.projects as module
    from analysis.hero_groups.recognition import atomic_json
    from core.project_file import make_payload_portable

    p = project(tmp_path)
    p["tracks"][0]["gameplay_grouping"]["selected"] = ["B"]
    target = tmp_path / "edit.autocutter"
    atomic_json(target, make_payload_portable(p, target))
    seen = []

    def export(plans, **kwargs):
        seen.extend(plans)
        return plans

    monkeypatch.setattr(module, "export_outputs", export)
    out, err = io.StringIO(), io.StringIO()
    code = run_cli(
        ["groups", str(target), "--export", "selected", "--output-dir", str(tmp_path / "outputs"), "--json"],
        stdout=out,
        stderr=err,
    )
    assert code == 0, err.getvalue()
    assert [p["group"] for p in seen] == ["B"]
    assert read_project(Path(seen[0]["project"]))["tracks"][0]["gameplay_grouping"]["selected"] == ["B"]


def test_relative_output_directory_and_path_traversal_labels(tmp_path, monkeypatch):
    p = project(tmp_path)
    p["tracks"][0]["gameplay_grouping"]["names"]["A"] = "../../CON:bad*"
    from analysis.hero_groups.recognition import refresh_groups

    refresh_groups(p["tracks"][0]["gameplay_grouping"])
    monkeypatch.chdir(tmp_path)
    plans, _ = prepare_outputs(p, Path("directory è 🎮"), ["A"])
    assert Path(plans[0]["project"]).is_absolute()
    assert Path(plans[0]["project"]).is_relative_to(tmp_path / "directory è 🎮")
    assert plans[0]["title"].endswith("../../CON:bad*")  # label only, never a path
    with pytest.raises(ValueError, match="Invalid group"):
        prepare_outputs(p, tmp_path / "out", ["../A"])


def test_packaging_anonymous_group_never_claims_class(tmp_path):
    child = group_project(project(tmp_path), "A")
    context = group_context(child)
    assert any("class is UNKNOWN" in line for line in context)
    assert group_context({"tracks": []}) == []
    child["hero_group_output"]["name"] = "Hunter"
    assert "Hunter" in context[0] or "Hunter" in group_context(child)[0]


def test_stable_ids_cache_and_auto_group_unchanged_by_override(tmp_path):
    from analysis.hero_groups.projects import assign_group

    previous = group_signatures([record(1, art()), record(2, art(2))], PortraitSettings())
    assign_group(previous, "2", "A")
    result = group_signatures([record(1, art()), record(2, art(2))], PortraitSettings(), previous)
    assert result["games"][1]["automatic_group"] == "B"
    assert result["games"][1]["group"] == "A"


def test_export_cancelled_job_remains_resumable(tmp_path):
    from analysis.hero_groups.projects import export_outputs
    from automation.manager import PipelineManager
    from automation.store import PipelineStore
    from automation.exporter import ExportCancellation, PipelineExportCancelled

    p = project(tmp_path)
    plans, _ = prepare_outputs(p, tmp_path / "out", ["A"])
    manager = PipelineManager(PipelineStore(tmp_path / "jobs.json"))
    token = ExportCancellation()

    class Fake:
        def execute(self, job_id, **kwargs):
            manager.start_export(job_id)
            token.cancel()
            raise PipelineExportCancelled()

    with pytest.raises(PipelineExportCancelled):
        export_outputs(plans, manager=manager, service=Fake(), cancellation=token)
    job = manager.list_jobs()[0]
    assert job.state.value == "cancelled"
    assert manager.resume_cancelled(job.id).state.value == "ready_export"


def test_group_export_saved_boundary_order_with_track_splits(tmp_path):
    p = project(tmp_path)
    t = p["tracks"][0]
    late = deepcopy(t)
    late.update(
        duration=200.0,
        segment_source_in=250.0,
        segment_source_out=450.0,
        keeps=[{"start": 45.0, "end": 145.0}],
        gameplay_keeps=[{"start": 45.0, "end": 145.0}],
        gameplay_islands=[{"game_id": "3", "game_index": 3}],
    )
    early = deepcopy(t)
    early.update(
        duration=200.0,
        segment_source_out=200.0,
        keeps=[{"start": 95.0, "end": 110.0}, {"start": 120.0, "end": 195.0}],
        gameplay_keeps=[{"start": 95.0, "end": 195.0}],
        gameplay_islands=[{"game_id": "1", "game_index": 1}],
    )
    p["tracks"] = [late, early]
    child = group_project(p, "A")
    assert [t["segment_source_in"] for t in child["tracks"]] == [0.0, 250.0]
    assert child["tracks"][0]["keeps"][1]["start"] == 120.0


def test_clicking_a_game_toggles_a_yellow_band_on_the_timeline(tmp_path):
    """View-only: click shows the game's span on the timeline, same click hides it, another moves it."""
    from analysis.cut_engine import Segment
    from test_timeline_widget import make_timeline
    from ui.hero_groups import HeroGroupsPanel, game_spans

    p = project(tmp_path)
    t = p["tracks"][0]
    track = TrackState(path=t["path"], duration=t["duration"], cuts_enabled=True,
                       keeps=[Segment(**k) for k in t["keeps"]], gameplay_mode="keep",
                       gameplay_keeps=[Segment(**k) for k in t["gameplay_keeps"]],
                       gameplay_report=t["gameplay_report"], gameplay_grouping=t["gameplay_grouping"])
    owner = QWidget()
    owner._tracks = [track]
    owner._active_track_index = 0
    dirty = []
    owner._mark_project_dirty = lambda: dirty.append(True)
    owner.timeline = make_timeline()
    panel = HeroGroupsPanel(owner)
    first, second = panel.games.topLevelItem(0), panel.games.topLevelItem(1)
    span = first.data(1, Qt.ItemDataRole.UserRole)
    panel.toggle_highlight(first)
    assert owner.timeline.gameHighlight() == (0, span[0], span[1])
    panel.toggle_highlight(second)
    other = second.data(1, Qt.ItemDataRole.UserRole)
    assert owner.timeline.gameHighlight() == (0, other[0], other[1])
    panel.toggle_highlight(second)
    assert owner.timeline.gameHighlight() is None and not panel.games.selectedItems()
    assert not dirty  # never saved in the project
    owner.close()

    # Incomplete games (UNKNOWN without END, result without START) still get a bounded span.
    games = [{"start": 10.0, "end": 40.0}, {"start": 50.0, "end": None}, {"start": None, "end": 90.0},
             {"start": None, "end": None}]
    assert game_spans(games, 100.0) == [(10.0, 40.0), (50.0, 90.0), (50.0, 90.0), None]


def test_game_band_is_painted_yellow_without_rebuilding_the_static_layer():
    from test_timeline_widget import make_timeline

    timeline = make_timeline(gameplay=False)
    timeline.show()
    timeline.grab()
    before = timeline.grab().toImage()
    renders = timeline.static_renders
    timeline.setGameHighlight(0, 70.0, 90.0)
    after = timeline.grab().toImage()
    assert timeline.static_renders == renders  # dynamic layer only: playback unaffected
    x = timeline._t_to_x(80.0)
    y = timeline._lanes()[0].top + 20
    old, new = before.pixelColor(x, int(y)), after.pixelColor(x, int(y))
    assert new.red() > old.red() and new.green() > old.green() and new.blue() <= old.blue() + 5
    outside = timeline._t_to_x(20.0)
    assert after.pixelColor(outside, int(y)) == before.pixelColor(outside, int(y))
    timeline.setGameHighlight(None)
    assert timeline.gameHighlight() is None
    timeline.close()
