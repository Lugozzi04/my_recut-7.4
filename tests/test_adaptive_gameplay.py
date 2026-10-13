"""Adaptive routing mechanics; synthetic similarity is not Hearthstone accuracy."""
import importlib.util
from dataclasses import replace
import json
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from analysis.audio_service import AnalysisCancellation, AnalysisCancelled
from analysis.gameplay.hearthstone.adaptive_policy import AdaptivePlanner, AdaptiveSettings, RoutingObservation
from analysis.gameplay.hearthstone.fingerprint import GameFingerprint, continuity
from analysis.gameplay.hearthstone.adaptive_compare import compare_analyses
from analysis.gameplay.models import GameEvent, GameEventType, GameplayAnalysisResult
from analysis.performance_profile import GameplayProfiler
from test_gameplay_state_aware import profile_fixture  # noqa: F401


def fp(t, board=.5, value=1., mana=None, mana_confidence=0.):
    regions = {k: np.full((2, 2), value/2, np.float32) for k in
               ("player_portrait", "opponent_portrait", "player_power", "structure_end_turn")}
    return GameFingerprint(t, board, regions, mana, mana_confidence)


def contains(windows, t):
    return any(w.start <= t < w.end for w in windows)


@pytest.mark.parametrize("old,new", [(.5,.1), (.1,.5)])
def test_game_menu_transitions_recover_entire_gap(old, new):
    planner = AdaptivePlanner(AdaptiveSettings(coarse_step_s=30))
    windows = planner.plan([RoutingObservation(fp(0, old)), RoutingObservation(fp(30, new))], 120)
    assert contains(windows, 15)
    assert "board_transition" in planner.states[-1]["reasons"]


def test_different_hero_between_coarse_samples_requests_recovery():
    rows = [RoutingObservation(fp(0)), RoutingObservation(fp(30, value=-1))]
    planner = AdaptivePlanner(AdaptiveSettings(coarse_step_s=30))
    assert contains(planner.plan(rows, 120), 14)
    assert "changed_player_portrait" in planner.states[-1]["reasons"]


def test_identical_heroes_do_not_prove_continuity_periodic_checks_remain():
    planner = AdaptivePlanner(AdaptiveSettings(coarse_step_s=30, full_fallback_fraction=1))
    windows = planner.plan([RoutingObservation(fp(t)) for t in (0,30,60,90)], 120)
    assert contains(windows, 20)  # independent of endpoint similarity
    assert contains(windows, 60)
    # Explicit limitation: an invisible transition between guards is not recoverable.
    assert not contains(windows, 32)


@pytest.mark.parametrize("before,after,confidence,suspect", [(10,2,1,True), (8,1,1,True),
    (10,8,1,False), (4,2,1,False), (8,2,.3,False), (2,10,1,False)])
def test_mana_is_optional_suspicion_never_game_outcome(before, after, confidence, suspect):
    _, reasons = continuity(fp(0, mana=before, mana_confidence=confidence), fp(10, mana=after, mana_confidence=confidence))
    assert ("possible_mana_reset" in reasons) == suspect
    assert not any(word in reasons for word in ("WIN", "LOSS", "START", "END"))


def test_unreadable_overlay_falls_back_not_guessed_continuity():
    planner = AdaptivePlanner(AdaptiveSettings(coarse_step_s=10))
    hidden = GameFingerprint(10, .3)
    windows = planner.plan([RoutingObservation(fp(0)), RoutingObservation(hidden)], 60)
    assert contains(windows, 7)
    assert planner.states[-1]["evidence_state"] == "IDENTITY_UNCERTAIN"
    assert planner.states[-1]["board_present"] is True  # identity loss is not loss of board


def test_overlapping_windows_are_merged_and_clipped():
    planner = AdaptivePlanner(AdaptiveSettings(coarse_step_s=10))
    rows = [RoutingObservation(fp(t), weak_start=True) for t in range(0,60,10)]
    windows = planner.plan(rows,60)
    assert windows[0].start == 0 and windows[-1].end == 60
    assert all(a.end < b.start for a,b in zip(windows,windows[1:]))


@pytest.mark.parametrize("change", [{"coarse_step_s":0}, {"coarse_step_s":True},
    {"recovery_width_s":float("nan")}, {"recovery_period_s":0}, {"continuity_floor":2}, {"overlap_s":0}])
def test_invalid_adaptive_settings(change):
    with pytest.raises(ValueError):
        replace(AdaptiveSettings(), **change)


def test_full_fallback_explicit_and_no_evidence():
    assert AdaptivePlanner(AdaptiveSettings()).plan([],100)[0].reasons == ("full_fallback",)
    windows = AdaptivePlanner(AdaptiveSettings(force_full_fallback=True)).plan([RoutingObservation(fp(0))],100)
    assert len(windows) == 1 and windows[0].start == 0 and windows[0].end == 100


class AdaptiveSampler:
    def __init__(self, images, duration=160):
        self.images, self.duration = images, duration
        self.closed = 0
        self.fail_sparse = False

    def probe(self, *args, **kwargs):
        from analysis.gameplay.hearthstone.visual.sampler import VideoInfo
        return VideoInfo(self.duration, 96, 64)

    def sample(self, *args, fps=1, start_s=0, end_s=None, **kwargs):
        from analysis.gameplay.hearthstone.visual.sampler import SampledFrame
        try:
            if fps < 1 and self.fail_sparse:
                raise RuntimeError("synthetic sparse failure")
            for t in np.arange(start_s, min(end_s or self.duration,self.duration)-1e-8, 1/fps):
                image = np.full((64,96,3),30,np.uint8)
                image[0,0] = int(t)
                if 1 <= t < 1.7 or 81 <= t < 81.7:
                    image[2:10,40:52] = self.images["vs"][:,:,None]
                yield SampledFrame(float(t),image)
        finally:
            self.closed += 1

    def sample_roi(self, *args, start_s=0, end_s=160, **kwargs):
        from analysis.gameplay.hearthstone.visual.sampler import SampledFrame
        try:
            for t in np.arange(start_s,end_s,1/60):
                image=np.full((32,48,3),30,np.uint8)
                for at,kind in [(70.05,"defeat"),(141.05,"victory")]:
                    if at <= t < at+.10:
                        image[10:18,15:27]=self.images[kind][:,:,None]
                yield SampledFrame(float(t),image)
        finally:
            self.closed += 1


def make_pair(profile_fixture, tmp_path, *, full=False, step=5):
    from analysis.gameplay.hearthstone.state_aware import StateAwareHearthstoneAnalyzer
    from analysis.gameplay.hearthstone.adaptive import AdaptiveHearthstoneAnalyzer
    profile,pack,_,images=profile_fixture
    source=tmp_path/"sorgente è 🎮.mp4"
    source.write_bytes(b"synthetic")
    old=StateAwareHearthstoneAnalyzer(state_profile=profile,template_pack=pack,sampler=AdaptiveSampler(images),use_cache=False)
    new=AdaptiveHearthstoneAnalyzer(state_profile=profile,template_pack=pack,sampler=AdaptiveSampler(images),use_cache=False,
        adaptive_settings=AdaptiveSettings(coarse_step_s=step,force_full_fallback=full,full_fallback_fraction=1))
    def board(gray):
        t=int(gray[0,0])
        value=.5 if 3 <= t < 70 or 83 <= t < 141 else .1
        return value,{"fixture":value}
    old.board.score=board
    new.board.score=board
    new.fingerprint.extract=lambda gray,t,b: fp(t,b)
    return source,old,new


@pytest.mark.parametrize("step", [5,10,20,30])
@pytest.mark.skipif(importlib.util.find_spec("cv2") is None, reason="OpenCV optional gameplay dependency")
def test_adaptive_reuses_existing_refinement_and_pairing(profile_fixture,tmp_path,step):
    source,old,new=make_pair(profile_fixture,tmp_path,step=step)
    expected=old.analyze(source)
    progress=[]
    actual=new.analyze(source,on_progress=progress.append)
    comparison=compare_analyses(expected,actual)
    if step == 30:
        # End and next start can be hidden between similar endpoints. Keep this
        # counterexample: never tune a guard to its known timestamp.
        assert not comparison["equivalent"]
        assert any(e["type"] == "DEFEAT" and e["status"] == "missing" for e in comparison["events"])
        return
    assert comparison["equivalent"],comparison
    # Synthetic np.arange grids started from different window boundaries differ
    # only by floating point rounding (<1e-12s); real samplers preserve source PTS.
    assert comparison["max_timestamp_error_s"] < 1e-9
    assert len(actual.games)==2 and [g.result.value for g in actual.games]==["LOSS","WIN"]
    assert comparison["result_window_candidate_recall"] == 1
    assert comparison["vs_window_candidate_recall"] == 1
    assert progress==sorted(set(progress)) and progress[-1]==100


@pytest.mark.skipif(importlib.util.find_spec("cv2") is None, reason="OpenCV optional gameplay dependency")
def test_full_fallback_matches_old_events_exactly(profile_fixture,tmp_path):
    source,old,new=make_pair(profile_fixture,tmp_path,full=True)
    expected,actual=old.analyze(source),new.analyze(source)
    assert compare_analyses(expected,actual)["exact_event_timestamps"]
    assert actual.metadata["adaptive"]["full_fallback"]


@pytest.mark.skipif(importlib.util.find_spec("cv2") is None, reason="OpenCV optional gameplay dependency")
def test_sparse_failure_retries_full_legacy_scan(profile_fixture,tmp_path):
    source,old,new=make_pair(profile_fixture,tmp_path)
    original = new.sampler.sample
    failed = False
    def fail_once(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("synthetic stream failure")
        yield from original(*args, **kwargs)
    new.sampler.sample = fail_once
    assert compare_analyses(old.analyze(source),new.analyze(source))["equivalent"]
    assert new.full_fallback


@pytest.mark.skipif(importlib.util.find_spec("cv2") is None, reason="OpenCV optional gameplay dependency")
def test_cancellation_closes_streams_and_does_not_fallback(profile_fixture,tmp_path):
    source,_,new=make_pair(profile_fixture,tmp_path)
    token=AnalysisCancellation()
    with pytest.raises(AnalysisCancelled):
        new.analyze(source,cancellation=token,on_progress=lambda p: token.cancel() if p >= 1 else None)
    assert new.sampler.closed == 1


@pytest.mark.skipif(importlib.util.find_spec("cv2") is None, reason="OpenCV optional gameplay dependency")
def test_missing_start_preserves_orphan_result(profile_fixture,tmp_path):
    from analysis.gameplay.hearthstone.state_monitor import MonitorObservation
    source,_,new=make_pair(profile_fixture,tmp_path)
    rows=[MonitorObservation(t,.5 if t < 20 else .1,0,0) for t in range(17,23)]
    windows,_=new._plan_result_windows(rows,[],160)
    assert windows and any(w.start <= 20.05 < w.end for w in windows)
    # Existing assembler retains absent start explicitly, without guessing.
    from analysis.gameplay.assembler import GameAssembler
    games=GameAssembler().assemble([GameEvent(20.05,GameEventType.DEFEAT,.95)])
    assert games[0].start is None and games[0].result.value == "LOSS"


@pytest.mark.skipif(importlib.util.find_spec("cv2") is None, reason="OpenCV optional gameplay dependency")
def test_profiler_records_actual_matching_and_fallback(profile_fixture,tmp_path):
    source,_,new=make_pair(profile_fixture,tmp_path)
    profiler=GameplayProfiler(str(source),save=False)
    with profiler.activate():
        result=new.analyze(source)
    assert profiler.phases["adaptive.fingerprint"]["counters"]["frames"] == 32
    assert profiler.phases["adaptive.routing"]["counters"]["vs_calls_avoided"] > 0
    assert 0 < result.metadata["adaptive"]["fallback_fraction"] <= 1
    assert profiler.phases["monitor.board"]["counters"]["frames"] < 160


@pytest.mark.skipif(importlib.util.find_spec("cv2") is None, reason="OpenCV optional gameplay dependency")
def test_dk_asset_is_loaded_and_classes_are_complete():
    from analysis.hero_groups.labels import class_id, CLASS_COLORS, default_class_refs
    from analysis.hero_groups.recognition import HeroGroupingService
    assert class_id("HERO_11bp") == class_id("DK") == "death_knight"
    refs=HeroGroupingService(board_gate=lambda _:1,class_refs=default_class_refs())._references()
    assert "death_knight" in refs
    assert set(refs) == set(CLASS_COLORS)


def test_wrong_class_and_extra_results_are_visible_in_comparison():
    reference=GameplayAnalysisResult("fixture",100,[GameEvent(10,GameEventType.DEFEAT,.95)],[],"reference")
    candidate=GameplayAnalysisResult("fixture",100,[GameEvent(10,GameEventType.VICTORY,.95)],[],"candidate")
    report=compare_analyses(reference,candidate)
    assert report["missing_events"] == report["extra_events"] == 1
    assert not report["equivalent"]


@pytest.mark.skipif(importlib.util.find_spec("cv2") is None, reason="OpenCV optional gameplay dependency")
def test_backend_default_and_opt_in(profile_fixture,tmp_path,monkeypatch):
    from analysis.gameplay_editing import GameplayEditSettings, hearthstone_detector
    from analysis.gameplay.hearthstone.state_aware import StateAwareHearthstoneAnalyzer
    from analysis.gameplay.hearthstone.adaptive import AdaptiveHearthstoneAnalyzer
    profile,pack,_,_=profile_fixture
    settings=GameplayEditSettings(state_profile=str(profile),template_pack=str(pack),use_detector_cache=False)
    monkeypatch.delenv("AUTO_CUTTER_GAMEPLAY_BACKEND",raising=False)
    with patch.object(StateAwareHearthstoneAnalyzer,"analyze",return_value="old"):
        assert hearthstone_detector(settings)(tmp_path/"f",AnalysisCancellation(),None)=="old"
    monkeypatch.setenv("AUTO_CUTTER_GAMEPLAY_BACKEND","adaptive")
    with patch.object(AdaptiveHearthstoneAnalyzer,"analyze",return_value="new"):
        assert hearthstone_detector(settings)(tmp_path/"f",AnalysisCancellation(),None)=="new"
    monkeypatch.setenv("AUTO_CUTTER_GAMEPLAY_BACKEND","invalid")
    with pytest.raises(ValueError):
        hearthstone_detector(settings)


@pytest.mark.skipif(importlib.util.find_spec("cv2") is None, reason="OpenCV optional gameplay dependency")
def test_fingerprint_brightness_uses_normalized_local_structure(profile_fixture):
    from analysis.gameplay.hearthstone.fingerprint import FingerprintExtractor
    from analysis.gameplay.hearthstone.visual.state_profile import StateAwareProfile
    extractor=FingerprintExtractor(StateAwareProfile(profile_fixture[0]))
    rng=np.random.default_rng(52)
    frame=np.repeat(np.repeat(rng.integers(35,170,(18,32),dtype=np.uint8),30,axis=0),30,axis=1)
    original=extractor.extract(frame,0,.5)
    brighter=extractor.extract((frame+20).astype(np.uint8),10,.5)
    score,reasons=continuity(original,brighter)
    assert score > .99 and not reasons
    assert "player_portrait" in original.regions and "opponent_portrait" in original.regions
    assert extractor.rois["player_portrait"][1] > .5 > extractor.rois["opponent_portrait"][1]


def test_nonfinite_or_missing_descriptors_never_assert_continuity():
    left,right=fp(0),fp(5)
    right.regions["player_portrait"][:]=np.nan
    _,reasons=continuity(left,right)
    assert "unreadable_portrait" in reasons


@pytest.mark.skipif(importlib.util.find_spec("cv2") is None, reason="OpenCV optional gameplay dependency")
def test_shadow_returns_reference_and_records_differences(profile_fixture,tmp_path,monkeypatch):
    from analysis.gameplay_editing import GameplayEditSettings, hearthstone_detector
    from analysis.gameplay.hearthstone.state_aware import StateAwareHearthstoneAnalyzer
    from analysis.gameplay.hearthstone.adaptive import AdaptiveHearthstoneAnalyzer
    profile,pack,_,_=profile_fixture
    settings=GameplayEditSettings(state_profile=str(profile),template_pack=str(pack),use_detector_cache=False)
    monkeypatch.setenv("AUTO_CUTTER_GAMEPLAY_BACKEND","adaptive-shadow")
    original=GameplayAnalysisResult("fixture",100,[GameEvent(10,GameEventType.DEFEAT,.95)],[],"reference")
    alternate=GameplayAnalysisResult("fixture",100,[],[],"candidate")
    profiler=GameplayProfiler("fixture",save=False)
    with profiler.activate(), patch.object(StateAwareHearthstoneAnalyzer,"analyze",return_value=original), \
            patch.object(AdaptiveHearthstoneAnalyzer,"analyze",return_value=alternate):
        actual=hearthstone_detector(settings)(tmp_path/"f",AnalysisCancellation(),None)
    assert actual is original
    assert actual.metadata["adaptive_shadow"]["missing_events"] == 1
    assert profiler.phases["adaptive.comparison"]["comparison"]["missing_events"] == 1


@pytest.mark.skipif(importlib.util.find_spec("cv2") is None, reason="OpenCV optional gameplay dependency")
def test_adaptive_cache_is_separate_and_invalidates_parameters(profile_fixture,tmp_path):
    source,_,new=make_pair(profile_fixture,tmp_path)
    from analysis.gameplay.cache import GameplayCache
    new.cache=GameplayCache(tmp_path/"adaptive-cache")
    first=new.analyze(source)
    closed=new.sampler.closed
    second=new.analyze(source)
    assert second.metadata["cache_hit"] and new.sampler.closed==closed
    new.adaptive_settings=replace(new.adaptive_settings,coarse_step_s=10)
    third=new.analyze(source)
    assert not third.metadata["cache_hit"] and third.metadata["cache_key"] != first.metadata["cache_key"]


@pytest.mark.skipif(importlib.util.find_spec("cv2") is None, reason="OpenCV optional gameplay dependency")
def test_no_result_does_not_infer_win_loss(profile_fixture,tmp_path):
    source,_,new=make_pair(profile_fixture,tmp_path)
    new.sampler.sample_roi=lambda *a,**kw: (x for x in ())
    result=new.analyze(source)
    assert result.games and all(g.result.value == "UNKNOWN" and g.end is None for g in result.games)


def test_result_or_start_recovery_does_not_require_minimum_game_duration():
    from analysis.gameplay.assembler import GameAssembler
    events=[GameEvent(1,GameEventType.VS_SCREEN,.95),GameEvent(2,GameEventType.DEFEAT,.95),
            GameEvent(3,GameEventType.VS_SCREEN,.95),GameEvent(4,GameEventType.VICTORY,.95)]
    games=GameAssembler().assemble(events)
    assert [(g.start,g.end,g.result.value) for g in games] == [(1,2,"LOSS"),(3,4,"WIN")]


@pytest.mark.skipif(importlib.util.find_spec("cv2") is None, reason="OpenCV optional gameplay dependency")
def test_shadow_failure_keeps_reference(profile_fixture,tmp_path,monkeypatch):
    from analysis.gameplay_editing import GameplayEditSettings, hearthstone_detector
    from analysis.gameplay.hearthstone.state_aware import StateAwareHearthstoneAnalyzer
    from analysis.gameplay.hearthstone.adaptive import AdaptiveHearthstoneAnalyzer
    profile,pack,_,_=profile_fixture
    monkeypatch.setenv("AUTO_CUTTER_GAMEPLAY_BACKEND","adaptive-shadow")
    original=GameplayAnalysisResult("fixture",100,[],[],"reference")
    with patch.object(StateAwareHearthstoneAnalyzer,"analyze",return_value=original), \
            patch.object(AdaptiveHearthstoneAnalyzer,"analyze",side_effect=RuntimeError("synthetic")):
        actual=hearthstone_detector(GameplayEditSettings(state_profile=str(profile),template_pack=str(pack)))(
            tmp_path/"f",AnalysisCancellation(),None)
    assert actual is original and actual.metadata["adaptive_shadow"]["status"] == "failed"


def test_dead_band_is_not_automatically_a_board_transition():
    planner = AdaptivePlanner(AdaptiveSettings())
    windows = planner.plan([RoutingObservation(fp(0,.5)), RoutingObservation(fp(5,.30)),
                            RoutingObservation(fp(10,.39))],100,promote_full=False)
    assert planner.states[1]["board_present"] is True
    assert planner.states[1]["evidence_state"] == "CONTINUITY_PLAUSIBLE"
    assert "board_transition" not in planner.states[1]["reasons"]
    assert not contains(windows,9)


def test_unknown_state_and_unknown_identity_are_different():
    planner = AdaptivePlanner(AdaptiveSettings())
    planner.plan([RoutingObservation(GameFingerprint(0,.3)),RoutingObservation(fp(5,.5)),
                  RoutingObservation(GameFingerprint(10,.3))],100,promote_full=False)
    assert planner.states[0]["evidence_state"] == "STATE_UNKNOWN"
    assert planner.states[-1]["evidence_state"] == "IDENTITY_UNCERTAIN"
    assert planner.states[-1]["board_present"] is True


def test_one_occluded_portrait_with_other_reliable_signals_is_not_global_unknown():
    from analysis.gameplay.hearthstone.fingerprint import assess_continuity
    left,right = fp(0),fp(5)
    del right.regions["player_portrait"]
    # A visible lower power still supports the player; upper portrait supports opponent.
    assessment = assess_continuity(left,right)
    assert assessment.category == "CONTINUITY_PLAUSIBLE"
    right.regions.pop("player_power")
    assert assess_continuity(left,right).category == "IDENTITY_UNCERTAIN"


def test_animated_power_does_not_override_stable_portraits_and_structure():
    from analysis.gameplay.hearthstone.fingerprint import assess_continuity
    left,right = fp(0),fp(5)
    right.regions["player_power"] *= -1
    assert assess_continuity(left,right).category == "CONTINUITY_PLAUSIBLE"


def test_both_portraits_changed_is_suspicion_not_an_inferred_game():
    from analysis.gameplay.hearthstone.fingerprint import assess_continuity
    left,right = fp(0),fp(5)
    for name in ("player_portrait","opponent_portrait"):
        right.regions[name] *= -1
    assert assess_continuity(left,right).category == "TRANSITION_SUSPECTED"
    planner = AdaptivePlanner(AdaptiveSettings())
    assert contains(planner.plan([RoutingObservation(left),RoutingObservation(right)],100,promote_full=False),3)
    assert planner.states[-1]["evidence_state"] == "TRANSITION_SUSPECTED"


def test_uncertainty_does_not_spread_into_next_clear_interval():
    planner = AdaptivePlanner(AdaptiveSettings())
    windows = planner.plan([RoutingObservation(fp(0,.1)),RoutingObservation(GameFingerprint(5,.3)),
                            RoutingObservation(fp(10,.1)),RoutingObservation(fp(15,.1))],100,promote_full=False)
    assert all(w.end <= 7 or w.start >= 17 for w in windows)


def test_streaming_plan_matches_offline_local_plan():
    rows = [RoutingObservation(fp(t,.5 if t<20 else .1)) for t in range(0,60,5)]
    settings=AdaptiveSettings()
    offline=AdaptivePlanner(settings).plan(rows,60,promote_full=False)
    planner=AdaptivePlanner(settings)
    planner.begin(60)
    for row in rows:
        planner.observe(row)
    from analysis.gameplay.hearthstone.adaptive_policy import merge_intervals
    assert merge_intervals(planner.requests) == offline
    assert planner.diagnostics()["state_counts"]["TRANSITION_SUSPECTED"] >= 1


@pytest.mark.skipif(importlib.util.find_spec("cv2") is None, reason="OpenCV optional gameplay dependency")
def test_single_monitor_decode_and_bounded_buffer(profile_fixture,tmp_path):
    source,old,new=make_pair(profile_fixture,tmp_path)
    profiler=GameplayProfiler(str(source),save=False)
    with profiler.activate():
        actual=new.analyze(source)
    details=actual.metadata["adaptive"]
    assert actual.metadata["profiling"]["monitor"]["streams"] == 1
    assert details["delivered_frames"] == 160
    assert details["buffer_max_frames"] <= 2*new.adaptive_settings.coarse_step_s+new.adaptive_settings.overlap_s+3
    assert details["recovery_frames_reused"] > 0
    assert profiler.phases["adaptive.routing"]["counters"]["board_calls_avoided"] > 0


@pytest.mark.skipif(importlib.util.find_spec("cv2") is None, reason="OpenCV optional gameplay dependency")
def test_autonomous_diagnostics_do_not_claim_fallback_starts(profile_fixture,tmp_path):
    source,old,new=make_pair(profile_fixture,tmp_path)
    old.board.score = new.board.score = lambda gray: (.5,{"fixture":.5})
    reference=old.analyze(source)
    result=new.analyze(source)
    own=result.metadata["adaptive"]["autonomous"]
    # With a constant clear board, brief starts invisible to coarse sampling
    # have no transition trigger: only leading/periodic fallback recovers them.
    assert sum(e["type"]=="VS_SCREEN" for e in own["events"]) == 0
    assert sum(e.type.value=="VS_SCREEN" for e in result.events) == 2
    assert compare_analyses(reference,result)["equivalent"]
    assert own["separate_runtime"] is None


@pytest.mark.skipif(importlib.util.find_spec("cv2") is None, reason="OpenCV optional gameplay dependency")
def test_explicit_full_fallback_does_not_waste_a_reconnaissance_decode(profile_fixture,tmp_path):
    source,old,new=make_pair(profile_fixture,tmp_path,full=True)
    result=new.analyze(source)
    assert result.metadata["profiling"]["monitor"]["streams"] == 1
    assert result.metadata["adaptive"]["full_fallback_reason"] == "explicit"
    assert not result.metadata["adaptive"]["autonomous"]["available"]


@pytest.mark.skipif(importlib.util.find_spec("cv2") is None, reason="OpenCV optional gameplay dependency")
def test_periodic_recovery_scores_vs_even_on_a_previously_suppressed_coarse_frame(profile_fixture,tmp_path):
    from analysis.gameplay.hearthstone.visual.sampler import SampledFrame
    source,old,new=make_pair(profile_fixture,tmp_path)
    images=profile_fixture[3]
    for analyzer in (old,new):
        original=analyzer.sampler.sample
        def sample(*args, _original=original, **kwargs):
            for frame in _original(*args,**kwargs):
                image=frame.image.copy()
                if 20 <= frame.timestamp < 21:
                    image[2:10,40:52]=images["vs"][:,:,None]
                yield SampledFrame(frame.timestamp,image)
        analyzer.sampler.sample=sample
        analyzer.board.score=lambda gray:(.5,{"fixture":.5})
    reference=old.analyze(source)
    candidate=new.analyze(source)
    assert compare_analyses(reference,candidate)["equivalent"]
    assert len(candidate.events)==3
    own=candidate.metadata["adaptive"]["autonomous"]
    assert all(e["type"]!="VS_SCREEN" for e in own["events"])


@pytest.mark.skipif(importlib.util.find_spec("cv2") is None, reason="OpenCV optional gameplay dependency")
def test_entirely_unknown_board_keeps_full_local_coverage_without_double_decode(profile_fixture,tmp_path):
    source,old,new=make_pair(profile_fixture,tmp_path)
    old.board.score=new.board.score=lambda gray:(.3,{"fixture":.3})
    reference=old.analyze(source)
    candidate=new.analyze(source)
    assert compare_analyses(reference,candidate)["equivalent"]
    assert candidate.metadata["adaptive"]["fallback_fraction"] == 1
    assert not candidate.metadata["adaptive"]["full_fallback"]
    assert candidate.metadata["profiling"]["monitor"]["streams"] == 1
    assert candidate.metadata["monitor_frames"] == 160
