"""Focused class search and export-local artifacts; no VODs, ASR or AI providers."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import json

import numpy as np
import pytest

from analysis.audio_service import AnalysisCancellation, AnalysisCancelled
from analysis.cut_engine import Segment
from analysis.gameplay.models import GameplayAnalysisResult, GameSegment, GameResult, GameEvent, GameEventType
from analysis.gameplay.hearthstone.class_focus import ClassFocusedAnalyzer, WindowSampler, focus_windows, relocate
from analysis.gameplay.hearthstone.visual.sampler import SampledFrame, VideoInfo
from analysis.hero_groups.recognition import PortraitSettings
from analysis.hero_groups.projects import prepare_selected_outputs, read_project, selected_project
from analysis.hero_groups.packaging import group_context
from core.track_state import TrackState
from export.artifacts import timing_text
from ui.main_window import MainWindow


def point(t, cls="warlock", status="matched"):
    return {"timestamp": float(t), "class": cls, "status": status}


def test_sparse_window_policy_target_unknown_changes_and_hidden_game_limit():
    points = [point(t) for t in range(0, 601, 30)]
    assert focus_windows(points, "hunter", 600) == []
    # A completely invisible Hunter game between identical non-target samples
    # cannot be inferred. This is an opt-in fast search, not exhaustive detection.
    for status in ("estimated", "no_samples"):
        altered = [dict(p) for p in points]
        altered[5]["status"] = status
        assert focus_windows(altered, "hunter", 600) == [(60., 240.)]
    altered = [dict(p) for p in points]
    altered[5]["class"] = "hunter"
    assert focus_windows(altered, "hunter", 600) == [(60., 240.)]
    altered[5]["class"] = "mage"
    assert focus_windows(altered, "hunter", 600) == [(60., 240.)]
    assert focus_windows([point(0)], "hunter", .02) == [(0., .02)]


class Service:
    def __init__(self):
        self.requests = []
        self.streams = []

    def probe(self, *args, **kwargs):
        return VideoInfo(3000., 960, 540)

    def get_many(self, source, times, **kwargs):
        self.requests.extend(times)
        return {t: SampledFrame(t, np.full((40, 40, 3), 2 if 900 <= t < 1200 else 1, np.uint8))
                for t in times}

    def sample(self, source, **kwargs):
        self.streams.append(dict(kwargs))
        yield SampledFrame(kwargs["start_s"] + .25, np.zeros((40, 40, 3), np.uint8))

    sample_roi = sample


class Grouping:
    settings = PortraitSettings()
    board_gate = staticmethod(lambda image: 1.)
    refs = {"hunter": np.ones((20, 20, 3), np.uint8), "warlock": np.zeros((20, 20, 3), np.uint8)}

    def _references(self):
        return self.refs

    def _power_evidence(self, images, refs):
        if not images:
            return {"status": "no_samples", "class": None}
        name = "hunter" if images[0][0, 0, 0] == 2 else "warlock"
        return {"class": name, "status": "matched", "scores": {name: .95}, "margin": .5}

    def analyze(self, source, games, **kwargs):
        rows = []
        overrides = kwargs.get("previous", {}).get("overrides", {})
        for game in games:
            cls = "hunter" if 900 <= game["start"] < 1200 else "warlock"
            rows.append({"key": game["id"], "game": game, "group": overrides.get(game["id"], cls),
                         "class_status": "matched", "class_evidence": cls})
        return {"games": rows, "groups": {r["group"]: {"class_name": r["group"]} for r in rows},
                "overrides": dict(overrides)}


class Detector:
    analyzer_version = "fake-baseline"
    profile = SimpleNamespace(fingerprint="profile")
    vs = SimpleNamespace(pack_fingerprint="pack")

    def __init__(self, service):
        self.sampler = service
        self.calls = []

    def analyze(self, source, **kwargs):
        self.calls.append(self.sampler.probe(source).duration_s)
        duration = self.sampler.probe(source).duration_s
        start = min(100., duration / 4)
        end = min(start + 100., duration - 1.)
        events = [GameEvent(start, GameEventType.VS_SCREEN, .99), GameEvent(end, GameEventType.VICTORY, .99)]
        game = GameSegment("local", 1, start, end, GameResult.WIN, markers=events)
        return GameplayAnalysisResult(str(source), duration, events, [game], "fake")


def focused(tmp_path, *, grouping=None):
    source = tmp_path / "fixture.mp4"
    source.write_bytes(b"fixture media")
    service = Service()
    base = Detector(service)
    analyzer = ClassFocusedAnalyzer("hunter", state_profile=Path("unused"), template_pack=Path("unused"),
        service=service, grouping=grouping or Grouping(), detector=base, cache_dir=tmp_path / "cache")
    return source, service, base, analyzer


def test_focus_uses_sparse_seeks_local_windows_global_times_cache_and_manual_overrides(tmp_path):
    source, service, base, analyzer = focused(tmp_path)
    result = analyzer.analyze(source)
    assert len(service.requests) < 400  # 3000 seconds, not 3000 sequential monitor frames
    assert len(base.calls) == 2
    assert sum(base.calls) < 700  # only targeted/uncertain source windows reach the full detector
    assert result.duration == 3000 and result.games[0].start == 910
    assert result.events[0].timestamp == result.games[0].start
    assert result.metadata["target_game_ids"] == [result.games[0].id]
    assert result.metadata["focused_grouping"]["selected"] == ["hunter"]
    requested, calls = len(service.requests), len(base.calls)
    again = analyzer.analyze(source)
    assert again.metadata["cache_hit"]
    assert len(service.requests) == requested and len(base.calls) == calls
    analyzer.previous = {"overrides": {result.games[1].id: "hunter"}}
    corrected = analyzer.analyze(source)
    assert len(corrected.metadata["target_game_ids"]) == 2  # edits do not invalidate detection
    analyzer.target = "mage"
    analyzer.analyze(source)
    assert len(service.requests) == requested  # missing reference uses full baseline, no bad negative gate
    assert base.calls[-1] == 3000.


def test_uncertain_board_falls_back_without_dropping_source_regions(tmp_path):
    grouping = Grouping()
    grouping.board_gate = lambda image: 0.
    source, _, base, analyzer = focused(tmp_path, grouping=grouping)
    result = analyzer.analyze(source)
    assert result.metadata["class_focus"]["scanned_fraction"] == 1.
    assert base.calls == [3000.]


def test_focus_cancel_before_any_seek(tmp_path):
    source, service, base, analyzer = focused(tmp_path)
    token = AnalysisCancellation()
    token.cancel()
    with pytest.raises(AnalysisCancelled):
        analyzer.analyze(source, cancellation=token)
    assert not service.requests and not base.calls


def test_window_adapter_preserves_fps_roi_and_real_source_clock():
    service = Service()
    adapter = WindowSampler(service, service.probe(None), 600, 900)
    assert adapter.probe(None).duration_s == 300
    found = list(adapter.sample("v", fps=6, start_s=10, end_s=12))
    assert found[0].timestamp == 10.25
    assert service.streams[-1]["start_s"] == 610 and service.streams[-1]["end_s"] == 612
    found = list(adapter.sample_roi("v", fps=60, start_s=299, end_s=310, roi=(0, 0, 1, 1)))
    assert found[0].timestamp == 299.25 and service.streams[-1]["end_s"] == 900
    assert service.streams[-1]["fps"] == 60 and service.streams[-1]["roi"] == (0, 0, 1, 1)


def test_local_incomplete_games_remain_incomplete_and_never_pair_across_gaps():
    left = GameplayAnalysisResult("v", 100, [], [GameSegment("a", 1, 50, None, GameResult.UNKNOWN)], "test")
    right = GameplayAnalysisResult("v", 100, [], [GameSegment("b", 1, None, 20, GameResult.WIN)], "test")
    _, a = relocate(left, 100, 1)
    _, b = relocate(right, 1000, 2)
    assert not a[0].complete and not b[0].complete
    assert a[0].start == 150 and b[0].end == 1020


def test_target_settings_roundtrip_and_only_target_win_gets_boundaries(tmp_path):
    from analysis.gameplay_editing import GameplayEditSettings, GameplayEditingService
    from test_gameplay_editing import analysis, game

    settings = GameplayEditSettings(target_class="hunter")
    assert GameplayEditSettings.from_mapping(settings.to_mapping()).target_class == "hunter"
    assert GameplayEditSettings().target_class is None
    with pytest.raises(ValueError):
        GameplayEditSettings(target_class="not a class")
    source = analysis([game(1, 100, 200, "WIN"), game(2, 300, 400, "WIN")], duration=500)
    source.metadata["target_game_ids"] = ["g1"]
    # No target WIN -> neither evidence analyzer nor resolver should be invoked.
    source.metadata["target_game_ids"] = []
    result = GameplayEditingService(settings, boundary_analyzer=object(), resolver=object()).run(
        tmp_path / "fixture.mp4", analysis=source)
    assert not result.keeps
    assert all(not g["publishable"] for g in result.report()["games"])


def test_single_class_artifacts_are_export_local_complete_and_names_are_explicit(tmp_path):
    from test_multi_source_export import multi_payload

    payload = multi_payload(tmp_path)
    for track in payload["tracks"]:
        if track.get("source_role") != "intro":
            track["gameplay_grouping"]["selected"] = ["mage"]
    target = tmp_path / "delivery" / "Mage.mp4"
    outputs, _ = prepare_selected_outputs(payload, target.parent, separate=False, output_path=target)
    output = outputs[0]
    project = Path(output["project"])
    assert project.parent.is_relative_to(target.parent)
    assert (project.parent / "project_before_export.autocutter").is_file()
    original = read_project(project.parent / "project_before_export.autocutter")
    assert len(original["tracks"]) == len(payload["tracks"])
    child = read_project(project)
    assert child["hero_group_output"]["name"] == "Mage" and "Mage" in output["title"]
    assert all(r["output_group"] == "mage" for r in child["export_selection"]["intervals"] if r["role"] == "gameplay")
    text = (project.parent / "timestamps.txt").read_text(encoding="utf-8")
    assert text.startswith("0:00 Intro\n") and "Partita 1" in text
    assert all(" Intro" in line or " Partita " in line for line in text.splitlines())
    assert "death_knight" not in text
    assert "Mage" in "\n".join(group_context(child))
    assert "opponent" in "\n".join(group_context(child))


def test_timing_text_respects_intro_cuts_noncontiguous_games_and_multiple_sources(tmp_path):
    tracks = [
        {"path": str(tmp_path / "intro.mp4"), "source_role": "intro", "duration": 20, "cuts_enabled": True,
         "keeps": [{"start": 0, "end": 3}, {"start": 10, "end": 12}]},
        {"path": str(tmp_path / "a.mp4"), "duration": 100, "cuts_enabled": True,
         "keeps": [{"start": 20, "end": 25}, {"start": 30, "end": 40}],
         "export_intervals": [{"start": 20, "end": 40, "game_id": "game-a", "game_index": 9, "output_group": "hunter", "result": "WIN", "role": "gameplay"}]},
        {"path": str(tmp_path / "b.mp4"), "duration": 100, "cuts_enabled": True,
         "keeps": [{"start": 5, "end": 15}],
         "export_intervals": [{"start": 5, "end": 15, "game_id": "game-b", "game_index": 4, "output_group": "hunter", "result": "WIN", "role": "gameplay"}]}]
    text = timing_text({"tracks": tracks})
    assert text == "0:00 Intro\n0:05 Partita 1\n0:20 Partita 2\n"
    limited = timing_text({"tracks": tracks}, segments=[{"v_idx": 1, "v_in": 31., "v_out": 34.}],
                          input_paths=[t["path"] for t in tracks])
    assert limited == "0:00 Partita 1\n"


def test_legacy_cache_project_is_copied_beside_video_before_thumbnail_generation(tmp_path):
    from analysis.packaging.outputs import settings_for_output
    from export.artifacts import save_snapshot

    source = tmp_path / "source.mp4"
    source.write_bytes(b"fake")
    project = tmp_path / "cache" / "edit.autocutter"
    save_snapshot({"tracks": [{"path": str(source), "duration": 10}]}, project)
    output = {"id": "a"*32, "project": str(project), "output": str(tmp_path / "delivery" / "Hunter.mp4")}
    settings = settings_for_output(output, {})
    assert Path(settings.project).parent.is_relative_to(tmp_path / "delivery")
    assert Path(settings.output).parent == Path(settings.project).parent
    assert read_project(Path(settings.project))["tracks"][0]["path"] == str(source)
    assert project.is_file()  # do not delete or migrate the old run


def test_automatic_cuts_off_keeps_manual_and_game_selection_without_rms():
    track = TrackState(path="fixture.mp4", duration=100, automatic_cuts_enabled=False,
                       manual_cuts=[Segment(20, 25)], gameplay_cuts_enabled=True,
                       gameplay_cuts=[Segment(0, 10), Segment(90, 100)], cfg={"intensity": 80})
    owner = SimpleNamespace(_tracks=[track], _normalize_preset_cfg=lambda cfg: {"intensity": 80},
        _segment_rms=lambda t: None, _enabled_gameplay_cuts=MainWindow._enabled_gameplay_cuts,
        _save_classic_cuts=lambda t: None)
    MainWindow._compute_cuts_for_track(owner, 0)
    assert track.cuts == [Segment(0, 10), Segment(20, 25), Segment(90, 100)]
    assert track.keeps == [Segment(10, 20), Segment(25, 90)]
    assert track.cfg == {"intensity": 80}  # disabling does not destroy tuning settings
    track.gameplay_cuts_enabled = False
    MainWindow._compute_cuts_for_track(owner, 0)
    assert track.keeps == [Segment(0, 20), Segment(25, 100)]


def test_options_persist_and_legacy_projects_default_on(tmp_path):
    track = TrackState(path=str(tmp_path / "v.mp4"), duration=100, automatic_cuts_enabled=False,
                       gameplay_target_class="death_knight", keeps=[Segment(0, 100)])
    owner = SimpleNamespace(_tracks=[track], _get_active_track=lambda: track,
        _segments_to_payload=lambda v: MainWindow._segments_to_payload(None, v),
        _save_workspace_for_mode=lambda t: None,
        _segments_from_payload=lambda v: MainWindow._segments_from_payload(None, v),
        _clone_segments_list=lambda v: list(v))
    saved = MainWindow._build_session_items(owner, include_cfg=False, include_cuts=True)[0]
    reopened = TrackState(path=track.path)
    MainWindow._apply_restore_state(owner, reopened, json.loads(json.dumps(saved)))
    assert not reopened.automatic_cuts_enabled and reopened.gameplay_target_class == "death_knight"
    legacy = TrackState(path=track.path)
    MainWindow._apply_restore_state(owner, legacy, {"duration": 100, "keeps": [{"start": 0, "end": 100}]})
    assert legacy.automatic_cuts_enabled and legacy.gameplay_target_class is None


def test_target_win_survives_policy_and_other_win_does_not_receive_boundary_work(tmp_path):
    from analysis.boundaries.resolvers import ExactBoundaryResolver
    from test_gameplay_editing import analysis, game, service, ScriptedEngine, local_evidence
    source = analysis([game(1, 100, 200, "WIN"), game(2, 300, 400, "WIN")], duration=500)
    source.metadata["target_game_ids"] = ["g1"]
    edit = service(ExactBoundaryResolver(), ScriptedEngine([]))
    edit.settings = replace(edit.settings, target_class="hunter")
    with local_evidence():
        result = edit.run(tmp_path / "fixture.mp4", analysis=source)
    assert [k.game.id for k in result.keeps] == ["g1"]
    assert set(result.contexts) == {1}
    assert not result.report()["games"][1]["publishable"]


def test_changed_source_and_reference_invalidate_focused_cache(tmp_path):
    grouping = Grouping()
    grouping.refs = {k: v.copy() for k, v in grouping.refs.items()}
    source, service, _, analyzer = focused(tmp_path, grouping=grouping)
    analyzer.analyze(source)
    first = len(service.requests)
    grouping.refs["hunter"].fill(7)
    assert not analyzer.analyze(source).metadata["cache_hit"]
    assert len(service.requests) == first * 2
    source.write_bytes(b"different media identity")
    assert not analyzer.analyze(source).metadata["cache_hit"]
    assert len(service.requests) == first * 3


def test_renderer_updates_timestamps_from_normalized_keeps(tmp_path):
    from export.exporter import ExportWorker
    from export.settings import ExportSettings
    worker = ExportWorker(ffmpeg_path="unused", input_path=str(tmp_path / "source.mp4"),
        output_path=str(tmp_path / "out.mp4"), keeps=[], codec="libx264", use_hwaccel=False,
        export_settings=ExportSettings.defaults())
    payload = {"tracks": [{"path": str(tmp_path / "source.mp4"), "duration": 100.,
        "export_intervals": [{"role": "intro", "start": 1., "end": 5.},
                             {"role": "gameplay", "game_id": "game", "start": 10., "end": 20.}]}]}
    target = tmp_path / "timestamps.txt"
    worker.timeline_report = (payload, target)
    raw = [Segment(1.001, 3.001), Segment(3.1, 4.09), Segment(10., 20.)]
    normalized = worker._snap_keeps_to_frames(worker._normalize_keeps(raw, merge_gap=.25, min_dur=.08), 30.)
    worker._write_output_timings([{"v_idx": 0, "v_in": k.start, "v_out": k.end} for k in normalized])
    text = target.read_text(encoding="utf-8")
    # Raw fragments total under 3s; normalized/merged intro lasts just over 3s.
    assert text == "0:00 Intro\n0:03 Partita 1\n"


def test_focused_ids_match_traditional_and_do_not_depend_on_window_position():
    from analysis.gameplay.assembler import GameAssembler
    events = [GameEvent(110., GameEventType.VS_SCREEN, .99), GameEvent(120., GameEventType.VICTORY, .99)]
    expected = GameAssembler().assemble(events)[0]
    for offset in (50., 100.):
        local_events = [replace(e, timestamp=e.timestamp-offset) for e in events]
        local_game = GameAssembler().assemble(local_events)[0]
        local = GameplayAnalysisResult("v", 100, local_events, [local_game], "test")
        _, shifted = relocate(local, offset, 1)
        assert shifted[0].id == expected.id
        assert shifted[0].start == expected.start and shifted[0].end == expected.end


def test_class_change_reuses_source_reconnaissance_without_decoding_again(tmp_path):
    source, service, _, analyzer = focused(tmp_path)
    analyzer.analyze(source)
    requests = len(service.requests)
    analyzer.target = "warlock"
    result = analyzer.analyze(source)
    assert not result.metadata["cache_hit"]
    assert len(service.requests) == requests
    assert result.metadata["focused_grouping"]["selected"] == ["warlock"]


def test_zero_win_gameplay_filter_can_be_disabled_again():
    track = TrackState(path="v.mp4", duration=100, gameplay_mode="keep", gameplay_cuts_enabled=True,
                       gameplay_report={"settings": {"target_class": "hunter"}}, gameplay_cuts=[Segment(0, 100)])
    changed = []
    owner = SimpleNamespace(_tracks=[track], _push_undo_state=lambda **kwargs: None,
                            timeline=SimpleNamespace(setSelectedIsland=lambda value: None),
                            _recompute_gameplay_tracks=lambda rows: changed.extend(rows))
    MainWindow._on_gameplay_apply_toggled(owner, False)
    assert not track.gameplay_cuts_enabled and changed == [0]


def test_final_image_generator_receives_actual_output_class_not_only_planning_agent(tmp_path):
    from PySide6.QtWidgets import QApplication
    from test_youtube_packaging_gen import make, FakeGenerator
    app = QApplication.instance() or QApplication([])
    generator = FakeGenerator()
    pipeline = make(tmp_path, generator=generator)
    pipeline.prepare()
    pipeline._evidence.payload = {"export_selection": {"intervals": [{"role": "gameplay", "included": True,
                                                                   "output_group": "hunter"}]}}
    pipeline.plan()
    pipeline.finish()
    assert app is not None and len(generator.calls) == 1
    prompt = generator.calls[0]["prompt"]
    assert "EXPORTED PLAYER CLASSES" in prompt and "Hunter" in prompt
    assert "opponent" in prompt and "Intro" in prompt



@pytest.mark.parametrize(("duration", "timestamp"), [(59.9, "0:59"), (60., "1:00"), (3723., "1:02:03")])
def test_youtube_chapters_use_output_seconds_and_hours_without_intro(tmp_path, duration, timestamp):
    tracks = [{"path": str(tmp_path / f"{i}.mp4"), "duration": length, "source_role": "gameplay"}
              for i, length in enumerate((duration, 20.))]
    assert timing_text({"tracks": tracks}) == f"0:00 Partita 1\n{timestamp} Partita 2\n"


def test_youtube_chapters_combine_consecutive_intros_and_handle_empty_plan(tmp_path):
    tracks = [{"path": str(tmp_path / f"{i}.mp4"), "duration": 20., "source_role": role}
              for i, role in enumerate(("intro", "intro", "gameplay"))]
    assert timing_text({"tracks": tracks}) == "0:00 Intro\n0:40 Partita 1\n"
    assert timing_text({"tracks": []}) == ""
