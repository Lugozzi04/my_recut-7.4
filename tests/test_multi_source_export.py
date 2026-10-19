"""Selection regressions and multi-source composition; no VOD/AI analysis."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest
from PySide6.QtWidgets import QApplication

from analysis.hero_groups.projects import selected_project, prepare_selected_outputs
from automation.exporter import ProjectExportPlanner
from test_hero_groups import project

APP = QApplication.instance() or QApplication([])


def test_filtered_export_worker_receives_only_checked_wins(tmp_path, monkeypatch):
    from ui.hero_groups import GroupWorker
    import ui.hero_groups as module

    payload = project(tmp_path)
    track = payload["tracks"][0]
    track["gameplay_grouping"]["selected"] = ["A"]
    # Game 3 is in selected class A, but explicitly excluded individually.
    track["gameplay_grouping"]["editor"] = {"included": {"3": False}}
    seen = []
    def render(plans, **kwargs):
        for item in plans:
            plan = ProjectExportPlanner(audio_probe=lambda p: True).build(SimpleNamespace(project_path=item["project"]))
            seen.extend((s.start, s.end) for s in plan.keeps)
        return plans
    monkeypatch.setattr(module, "export_outputs", render)
    monkeypatch.setattr("utils.runtime_paths.cache_root", lambda: tmp_path / "cache")
    worker = GroupWorker(payload, output_dir=str(tmp_path), selected=True, separate=False,
                         output_path=str(tmp_path / "selected.mp4"))
    errors = []
    worker.failed.connect(errors.append)
    worker.run()
    assert not errors
    assert seen == [(95., 110.), (120., 195.)]


def test_empty_selection_never_becomes_whole_vod(tmp_path):
    payload = project(tmp_path)
    payload["tracks"][0]["gameplay_grouping"]["selected"] = []
    with pytest.raises(ValueError, match="Nothing to export"):
        selected_project(payload)
    with pytest.raises(ValueError, match="Nothing to export"):
        prepare_selected_outputs(payload, tmp_path / "out")


def test_complete_wins_only_is_applied_after_manual_inclusion(tmp_path):
    payload = project(tmp_path)
    track = payload["tracks"][0]
    track["gameplay_grouping"]["games"][0]["game"]["result"] = "LOSS"
    track["gameplay_grouping"]["editor"] = {"included": {"1": True}}
    child = selected_project(payload)
    assert child["export_selection"]["games"] == 2
    assert child["export_selection"]["wins"] == 2
    assert all(r["game_id"] != "1" for r in child["export_selection"]["intervals"])
    assert selected_project(payload, wins_only=False)["export_selection"]["games"] == 3


def test_main_export_routes_to_filtered_workflow():
    from ui.main_window import MainWindow
    seen = []
    owner = SimpleNamespace(hero_groups_panel=SimpleNamespace(filter_enabled=lambda: True,
                                                            start_export=lambda: seen.append("filtered")))
    MainWindow.export_mp4(owner)
    assert seen == ["filtered"]


def multi_payload(tmp_path):
    from analysis.hero_groups.recognition import refresh_groups

    base = project(tmp_path)
    first = base["tracks"][0]
    first["source_id"] = "vod-A"
    first["source_role"] = "gameplay"
    second = deepcopy(first)
    second["path"] = str(tmp_path / "second.mp4")
    Path(second["path"]).write_bytes(b"second")
    second["source_id"] = "vod-B"
    for track in (first, second):
        for row in track["gameplay_grouping"]["games"]:
            row["group"] = "mage" if row["group"] == "A" else "death_knight"
        track["gameplay_grouping"]["selected"] = ["mage", "death_knight"]
        refresh_groups(track["gameplay_grouping"])
    intro_path = tmp_path / "intro.mp4"
    intro_path.write_bytes(b"intro")
    intro = {"path": str(intro_path), "source_id": "intro-X", "source_role": "intro",
             "intro_targets": ["mage", "death_knight"], "duration": 2., "cuts_enabled": False,
             "segment_source_in": 0., "segment_source_out": 2.}
    base["tracks"] = [first, intro, second]
    return base


def test_two_vods_merge_named_classes_but_preserve_source_ids_and_order(tmp_path):
    payload = multi_payload(tmp_path)
    child = selected_project(payload, output="mage")
    assert [t["source_id"] for t in child["tracks"]] == ["intro-X", "vod-A", "vod-B"]
    assert child["export_selection"]["games"] == child["export_selection"]["wins"] == 4
    records = child["export_selection"]["intervals"]
    assert [r["source_id"] for r in records] == ["intro-X"] + ["vod-A"]*3 + ["vod-B"]*3
    assert all(r["group"] == "mage" and r["result"] == "WIN" and r["included"] for r in records[1:])
    # Per-source checkbox independence: deselect Mage in A, keep it in B.
    payload["tracks"][0]["gameplay_grouping"]["selected"] = ["death_knight"]
    changed = selected_project(payload, output="mage")
    assert [t["source_id"] for t in changed["tracks"]] == ["intro-X", "vod-B"]
    assert changed["export_selection"]["games"] == 2


def test_shared_intro_once_per_output_and_one_prefix_in_combined_video(tmp_path):
    payload = multi_payload(tmp_path)
    combined = selected_project(payload)
    assert len([r for r in combined["export_selection"]["intervals"] if r["role"] == "intro"]) == 1
    for output in ("mage", "death_knight"):
        child = selected_project(payload, output=output)
        assert child["tracks"][0]["source_id"] == "intro-X"
        assert sum(r["role"] == "intro" for r in child["export_selection"]["intervals"]) == 1
    payload["tracks"][1]["intro_targets"] = ["death_knight"]
    assert all(t["source_role"] != "intro" for t in selected_project(payload, output="mage")["tracks"])


def test_anonymous_group_ids_never_collide_across_vods(tmp_path):
    from analysis.hero_groups.projects import output_key
    payload = multi_payload(tmp_path)
    assert output_key(payload["tracks"][0], "A") != output_key(payload["tracks"][2], "A")


def test_planner_and_edit_map_use_separate_source_clocks(tmp_path):
    from analysis.packaging.timeline import EditMap
    payload = selected_project(multi_payload(tmp_path), output="mage")
    path = tmp_path / "edit.autocutter"
    path.write_text(json.dumps(payload), encoding="utf-8")
    planner = ProjectExportPlanner(audio_probe=lambda name: Path(name).name != "intro.mp4")
    plan = planner.build(SimpleNamespace(project_path=str(path)))
    assert len(plan.input_paths) == 3 and not plan.keeps
    assert Path(plan.input_paths[0]).name != "intro.mp4"
    assert plan.segments[0]["a_idx"] is None
    assert plan.segments[0]["source_id"] == "intro-X"
    assert all(s["included"] for s in plan.segments)
    assert len(plan.selection_intervals) == len(plan.segments)
    cursor = 0.
    for segment in plan.segments:
        assert segment["start"] == pytest.approx(cursor)
        cursor += segment["duration"]
        assert segment["end"] == pytest.approx(cursor)
    mapping = EditMap.from_project(path, planner=planner)
    index_a = next(i for i, p in enumerate(mapping.sources) if Path(p).name == Path(payload["tracks"][1]["path"]).name)
    index_b = next(i for i, p in enumerate(mapping.sources) if Path(p).name == "second.mp4")
    assert mapping.to_edited(100, index_a) != mapping.to_edited(100, index_b)
    for piece in mapping.pieces:
        assert mapping.to_source(piece.edited_start+.01) == pytest.approx((piece.source_index, piece.source_start+.01))


def test_output_projects_isolated_and_existing_single_file_refused(tmp_path, monkeypatch):
    from analysis.hero_groups.projects import read_project
    payload = multi_payload(tmp_path)
    plans, errors = prepare_selected_outputs(payload, tmp_path / "outputs")
    assert not errors and len(plans) == 2
    assert plans[0]["id"] != plans[1]["id"]
    assert Path(plans[0]["project"]).parent != Path(plans[1]["project"]).parent
    for plan in plans:
        child = read_project(Path(plan["project"]))
        assert child["hero_group_output"]["group"] == plan["group"]
        assert all(r["output_group"] == plan["group"] for r in child["export_selection"]["intervals"]
                   if r["role"] == "gameplay")
    existing = tmp_path / "exists.mp4"
    existing.write_bytes(b"original")
    with pytest.raises(ValueError, match="already exists"):
        prepare_selected_outputs(payload, tmp_path, separate=False, output_path=existing)
    assert existing.read_bytes() == b"original"


def test_intro_skips_audio_ai_and_game_detection_queue(tmp_path):
    from core.track_state import TrackState
    from ui.main_window import MainWindow
    intro = TrackState(path="intro.mp4", source_role="intro")
    gameplay = TrackState(path="vod.mp4", source_role="gameplay")
    owner = SimpleNamespace(_tracks=[intro, gameplay], _app_log=lambda *a, **kw: None)
    # Guards return before creating any audio or ASR worker.
    MainWindow._start_analysis_thread(owner, "intro.mp4", 0)
    MainWindow._start_ai_analysis(owner, 0)
    owner.input_path = "vod.mp4"
    owner.analysis_mode = "classic"
    owner._export_in_progress = lambda: False
    owner.gameplay_boundary_combo = SimpleNamespace(currentData=lambda: "exact")
    owner._same_local_path = lambda a, b: a == b
    owner._run_next_gameplay_source = lambda: None
    MainWindow._start_gameplay_edit(owner)
    assert owner._gameplay_queue == ["vod.mp4"]


def test_role_and_intro_targets_persist_through_gui_restore(tmp_path):
    from core.track_state import TrackState
    from ui.main_window import MainWindow
    source = tmp_path / "intro.mp4"
    source.write_bytes(b"source")
    track = TrackState(path=str(source), duration=2., source_role="intro", source_id="stable-intro",
                       intro_targets=["mage", "death_knight"], cuts_enabled=True, keeps=[])
    owner = SimpleNamespace(_tracks=[track], _get_active_track=lambda: track,
                            _save_workspace_for_mode=lambda t: None,
                            _segments_to_payload=lambda v: MainWindow._segments_to_payload(None, v),
                            _segments_from_payload=lambda v: MainWindow._segments_from_payload(None, v),
                            _clone_segments_list=lambda v: list(v))
    saved = MainWindow._build_session_items(owner, include_cfg=False, include_cuts=True)[0]
    restored = TrackState(path=str(source))
    assert MainWindow._apply_restore_state(owner, restored, json.loads(json.dumps(saved)))
    assert (restored.source_id, restored.source_role, restored.intro_targets) == (
        "stable-intro", "intro", ["mage", "death_knight"])
    legacy = TrackState(path=str(source))
    MainWindow._apply_restore_state(owner, legacy, {"duration": 2., "keeps": [], "cuts_enabled": True})
    assert legacy.source_role == "gameplay" and legacy.intro_targets == []
    assert legacy.source_id


@pytest.mark.parametrize("intro_audio", [False, True])
def test_real_multi_source_render_order_audio_and_joins(tmp_path, intro_audio):
    """Tiny SDR clips only: different sizes/FPS/audio rates, with a silence-cut hole."""
    import numpy as np
    from utils.ffmpeg import ensure_ffmpeg, ffprobe_duration_seconds
    from utils.subprocess_utils import run_no_window
    from analysis.hero_groups.projects import export_outputs
    from automation.manager import PipelineManager
    from automation.store import PipelineStore

    ffmpeg, ffprobe = ensure_ffmpeg()
    def create(name, color, size, fps, frequency, rate):
        target = tmp_path / name
        command = [ffmpeg, "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                   f"color=c={color}:s={size}:r={fps}:d=2"]
        if frequency:
            command += ["-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate={rate}:duration=2"]
        command += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
        command += ["-c:a", "aac", "-ac", "1"] if frequency else ["-an"]
        command += ["-t", "2", "-y", str(target)]
        generated = run_no_window(command, capture_output=True, timeout=20)
        assert generated.returncode == 0, generated.stderr
        return target

    intro = create("intro è.mp4", "red", "96x64", 24, 220 if intro_audio else None, 32000)
    a = create("A.mp4", "blue", "160x90", 30, 440, 44100)
    b = create("B.mp4", "green", "128x72", 25, 880, 48000)
    def track(source, sid, start, end):
        game = {"id": "same-id", "index": 1, "start": start, "end": end, "result": "WIN"}
        return {"path": str(source), "source_id": sid, "source_role": "gameplay", "duration": 2.,
                "cuts_enabled": True, "keeps": [{"start": start, "end": end}],
                "gameplay_mode": "keep", "gameplay_keeps": [{"start": start, "end": end}],
                "gameplay_report": {"games": [game], "keep_islands": [{"start": start, "end": end, "game": game}]},
                "gameplay_grouping": {"games": [{"key": "same-id", "game": game, "group": "mage"}],
                                      "groups": {"mage": {"class_name": "mage"}}, "selected": ["mage"]}}
    first = track(a, "A", .4, 1.2)
    first["keeps"] = [{"start": .4, "end": .7}, {"start": .8, "end": 1.2}]
    second = track(b, "B", .3, 1.1)
    payload = {"format": "autocutter_project", "version": 2, "global": {"export_settings": {
        "codec": "libx264", "method": "filter_concat", "parallel_workers": 1, "chunk_count": 1,
        "hwaccel_decode": False, "cut_quality": "maximum_speed"}},
        "tracks": [first, {"path": str(intro), "source_id": "intro", "source_role": "intro",
                          "intro_targets": ["mage"], "duration": 2., "cuts_enabled": True,
                          "keeps": [{"start": 0., "end": .4}]}, second]}
    plans, _ = prepare_selected_outputs(payload, tmp_path / "outputs")
    manager = PipelineManager(PipelineStore(tmp_path / "jobs.json"))
    outputs = export_outputs(plans, manager=manager)
    assert len(outputs) == 1
    output = Path(outputs[0]["output"])
    assert abs(ffprobe_duration_seconds(str(output))-1.9) < .1
    inspected = run_no_window([ffprobe, "-v", "error", "-show_streams", "-of", "json", str(output)],
                              capture_output=True, text=True, timeout=10)
    streams = json.loads(inspected.stdout)["streams"]
    video = next(s for s in streams if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == (160, 90)  # gameplay defines geometry, not intro
    assert any(s["codec_type"] == "audio" for s in streams)
    def frame(timestamp):
        read = run_no_window([ffmpeg, "-v", "error", "-ss", str(timestamp), "-i", str(output),
                              "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
                             capture_output=True, timeout=10)
        assert read.returncode == 0
        image = np.frombuffer(read.stdout, np.uint8).reshape(90, 160, 3)
        return image[30:60, 60:100].mean(axis=(0, 1))
    assert frame(.2).argmax() == 0     # red intro
    assert frame(.6).argmax() == 2     # blue VOD A
    assert frame(1.5).argmax() == 1    # green VOD B
    # No black video frame at the actual joins or the cut within A.
    for timestamp in (.37, .43, .67, .73, 1.07, 1.13):
        assert frame(timestamp).max() > 65
    audio = run_no_window([ffmpeg, "-v", "error", "-i", str(output), "-vn", "-ac", "1", "-ar", "48000",
                           "-f", "f32le", "pipe:1"], capture_output=True, timeout=10)
    assert audio.returncode == 0
    samples = np.frombuffer(audio.stdout, np.float32)
    def frequency(start, end):
        part = samples[int(start*48000):int(end*48000)]
        spectrum = abs(np.fft.rfft(part*np.hanning(len(part))))
        return np.argmax(spectrum)*48000/len(part), float(np.sqrt(np.mean(part**2)))
    f_a, rms_a = frequency(.5, .65)
    f_b, rms_b = frequency(1.3, 1.6)
    assert abs(f_a-440) < 12 and abs(f_b-880) < 12
    assert rms_a > .01 and rms_b > .01
    if not intro_audio:
        assert frequency(.1, .25)[1] < .001
    frame_times = run_no_window([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                                "frame=best_effort_timestamp_time", "-of", "json", str(output)],
                               capture_output=True, text=True, timeout=10)
    times = [float(f["best_effort_timestamp_time"]) for f in json.loads(frame_times.stdout)["frames"]]
    assert np.all(np.diff(times) > 0)
    assert np.max(np.diff(times)) < .04


def test_gui_source_switch_and_class_checkbox_are_independent(tmp_path):
    from dataclasses import asdict
    from core.project import Project
    from core.track_state import TrackState
    from analysis.cut_engine import Segment
    from ui.hero_groups import HeroGroupsPanel
    from ui.project_sources import SourcesPanel
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QWidget
    from test_timeline_widget import make_timeline

    original = multi_payload(tmp_path)
    tracks = []
    for item in original["tracks"]:
        track = TrackState(path=item["path"], source_id=item["source_id"], source_role=item["source_role"],
                           duration=item["duration"], segment_source_out=item["duration"],
                           intro_targets=item.get("intro_targets", []), cuts_enabled=item["cuts_enabled"],
                           gameplay_mode=item.get("gameplay_mode", ""),
                           gameplay_report=item.get("gameplay_report", {}),
                           gameplay_grouping=item.get("gameplay_grouping", {}))
        track.keeps = [Segment(**k) for k in item.get("keeps", [])]
        track.gameplay_keeps = [Segment(**k) for k in item.get("gameplay_keeps", [])]
        tracks.append(track)
    owner = QWidget()
    owner._tracks, owner._active_track_index = tracks, 0
    owner.project = Project.create_default()
    owner.timeline = make_timeline()
    owner._activate_track = lambda i: setattr(owner, "_active_track_index", i)
    seeks = []
    owner._clip_timeline_in = lambda i: sum(t.duration for t in owner._tracks[:i])
    owner._seek_to = seeks.append
    owner._mark_project_dirty = lambda *a: None
    owner._export_in_progress = owner._gameplay_running = lambda: False
    owner._build_project_file_payload = lambda: {"format": "autocutter_project", "version": 2,
                                                "tracks": [asdict(t) for t in owner._tracks]}
    owner.hero_groups_panel = HeroGroupsPanel(owner)
    owner.sources_panel = SourcesPanel(owner)
    owner._refresh_gameplay_panel = lambda: (owner.sources_panel.refresh(), owner.hero_groups_panel.refresh())
    panel = owner.hero_groups_panel
    mage = next(panel.groups.topLevelItem(i) for i in range(panel.groups.topLevelItemCount())
                if panel.groups.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole) == "mage")
    mage.setCheckState(0, Qt.CheckState.Unchecked)
    assert "mage" not in tracks[0].gameplay_grouping["selected"]
    assert "mage" in tracks[2].gameplay_grouping["selected"]
    owner.sources_panel.activate(owner.sources_panel.sources.topLevelItem(2))
    assert owner._active_track_index == 2 and seeks[-1] == 502
    assert owner.timeline.view_start == 502 and owner.timeline.view_span == 500
    mage = next(panel.groups.topLevelItem(i) for i in range(panel.groups.topLevelItemCount())
                if panel.groups.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole) == "mage")
    assert mage.checkState(0) == Qt.CheckState.Checked
    panel.mode.setCurrentIndex(panel.mode.findData("by_class"))
    assert panel.preview_summary["games"] == 4
    assert panel.preview_summary["outputs"] == 2
    panel.mode.setCurrentIndex(panel.mode.findData("normal"))
    assert not panel.filter_enabled()
    owner.close()


def test_missing_detection_and_intro_only_are_clear_errors(tmp_path):
    payload = multi_payload(tmp_path)
    payload["tracks"][2]["gameplay_report"] = {}
    payload["tracks"][2]["gameplay_grouping"] = {}
    with pytest.raises(ValueError, match="has not been detected"):
        selected_project(payload)
    payload["tracks"] = [payload["tracks"][1]]
    with pytest.raises(ValueError, match="Nothing to export"):
        selected_project(payload)


def test_group_output_order_follows_project_source_order(tmp_path):
    payload = multi_payload(tmp_path)
    payload["tracks"] = list(reversed(payload["tracks"]))
    projected = selected_project(payload, output="mage")
    assert [t["source_id"] for t in projected["tracks"]] == ["intro-X", "vod-B", "vod-A"]


def test_intro_assignment_dialog_does_not_start_analysis(tmp_path):
    from core.track_state import TrackState
    from PySide6.QtCore import QTimer, Qt
    from PySide6.QtWidgets import QWidget, QDialog, QDialogButtonBox, QListWidget
    from ui.project_sources import SourcesPanel

    payload = multi_payload(tmp_path)
    intro = TrackState(path=payload["tracks"][1]["path"], source_id="intro-X", source_role="intro", duration=2.)
    game = TrackState(path=payload["tracks"][0]["path"], source_id="vod-A",
                      gameplay_grouping=payload["tracks"][0]["gameplay_grouping"])
    owner = QWidget()
    owner._tracks, owner._active_track_index = [intro, game], 0
    owner._export_in_progress = owner._gameplay_running = lambda: False
    owner._push_undo_state = owner._mark_project_dirty = lambda *a, **kw: None
    owner._refresh_timeline_tracks = lambda **kw: None
    owner._start_analysis_thread = lambda *a: pytest.fail("assigning intro must not analyze")
    owner.sources_panel = SourcesPanel(owner)
    owner._refresh_gameplay_panel = owner.sources_panel.refresh
    def accept():
        dialog = APP.activeModalWidget()
        assert isinstance(dialog, QDialog)
        choices = dialog.findChild(QListWidget)
        for index in range(choices.count()):
            item = choices.item(index)
            if item.data(Qt.ItemDataRole.UserRole) in {"mage", "death_knight"}:
                item.setCheckState(Qt.CheckState.Checked)
        dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()
    QTimer.singleShot(0, accept)
    owner.sources_panel.configure()
    assert intro.source_role == "intro"
    assert set(intro.intro_targets) == {"mage", "death_knight"}
    owner.close()
