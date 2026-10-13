"""Sparse access and conservative routing; synthetic tests do not prove VOD recall."""
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from analysis.audio_service import AnalysisCancellation, AnalysisCancelled
from analysis.gameplay.hearthstone.adaptive_compare import compare_analyses
from analysis.gameplay.hearthstone.adaptive_v2 import SparseAdaptiveHearthstoneAnalyzer, SparseSettings
from analysis.gameplay.hearthstone.fingerprint import GameFingerprint
from analysis.gameplay.hearthstone.fingerprint_v2 import (
    DEFAULT_EXCLUSIONS, ROIS, MaximumManaReader, SparseFingerprint, SparseFingerprintExtractor, discontinuities,
)
from analysis.gameplay.hearthstone.state_aware import StateAwareHearthstoneAnalyzer
from analysis.gameplay.hearthstone.state_monitor import MonitorObservation
from analysis.shared.frames import FrameSamplingService
from test_adaptive_gameplay import AdaptiveSampler
from test_gameplay_state_aware import profile_fixture  # noqa: F401


def routed(t, state="IN_GAME", mana=None):
    regions = {k: np.full((2,2), .5, np.float32) for k in ROIS}
    return SparseFingerprint(GameFingerprint(t,.5 if state=="IN_GAME" else .1,regions,mana,1.),state)


@pytest.mark.parametrize("a,b", [("IN_GAME","OUT_OF_GAME"),("OUT_OF_GAME","IN_GAME")])
def test_board_transition_is_suspicion(a,b):
    assert discontinuities(routed(0,a),routed(30,b),.9) == ("board_transition",)


def test_same_portraits_cannot_prove_no_hidden_game():
    assert not discontinuities(routed(0),routed(30),.9)
    assert SparseSettings().recovery_period_s == 60  # independent guards remain


def test_changed_opponent_is_not_our_class():
    left,right=routed(0),routed(30)
    right.fingerprint.regions["opponent_portrait"] *= -1
    assert discontinuities(left,right,.9) == ("identity_transition_suspected",)
    np.testing.assert_array_equal(left.fingerprint.regions["player_portrait"],
                                  right.fingerprint.regions["player_portrait"])


@pytest.mark.parametrize("old,new,expected",[(9,2,True),(4,3,True),(2,4,False),(4,4,False)])
def test_mana_direction_is_only_suspicion_including_card_effects(old,new,expected):
    assert bool(discontinuities(routed(0,mana=old),routed(30,mana=new),.9)) == expected


def test_missing_mana_does_not_assert_reset():
    assert not discontinuities(routed(0,mana=9),routed(30),.9)


def test_one_hidden_map_anchor_can_use_other_stable_regions():
    left,right=routed(0),routed(30)
    del right.fingerprint.regions["map_left"]
    assert not discontinuities(left,right,.9)


def test_overlay_portrait_is_uncertain_not_guess():
    left,right=routed(0),routed(30)
    del right.fingerprint.regions["player_portrait"]
    assert discontinuities(left,right,.9) == ("identity_uncertain",)


def test_unknown_and_weak_candidates_never_prune():
    assert discontinuities(routed(0,"UNKNOWN"),routed(30),.9)
    other=routed(30);other.weak_result=True
    assert discontinuities(routed(0),other,.9) == ("visual_event_candidate",)


@pytest.mark.parametrize("values", [{"step_s":15},{"leaf_s":0},{"overlap_s":float("nan")},
                                    {"recovery_width_s":1000},{"exclusions":((-.1,0,.2,.2),)}])
def test_settings_validate(values):
    with pytest.raises(ValueError):
        replace(SparseSettings(),**values)


def test_rois_are_separate_and_do_not_intersect_default_overlays():
    assert ROIS["player_portrait"][1] > .7 and ROIS["opponent_portrait"][1] < .2
    for x,y,w,h in ROIS.values():
        assert not any(x<a+c and a<x+w and y<b+d and b<y+h for a,b,c,d in DEFAULT_EXCLUSIONS)
    assert all(not key.startswith("structure") for key in ROIS)


def test_missing_mana_pack_is_unknown(tmp_path):
    assert MaximumManaReader(tmp_path/"missing.npz").read(np.zeros((540,960),np.uint8)) == (None,0.)


def test_mana_reads_only_denominator_and_requires_margin(tmp_path):
    from analysis.gameplay.hearthstone.fingerprint_v2 import MANA_ROI
    rng=np.random.default_rng(22)
    a=rng.integers(0,255,(9,6),np.uint8)
    b=rng.integers(0,255,(9,6),np.uint8)
    file=tmp_path/"mana.npz";np.savez(file,**{"2":a,"8":b})
    reader=MaximumManaReader(file)
    image=np.zeros((540,960),np.uint8)
    image[496:505,634:640]=a
    assert reader.read(image)[0] == 2
    # Numerator/available crystals do not participate.
    image[496:505,610:628]=255
    assert reader.read(image)[0] == 2
    np.savez(file,**{"2":a,"8":a})
    assert MaximumManaReader(file).read(image)[0] is None
    assert MANA_ROI[0] > .65


class SparseSampler(AdaptiveSampler):
    def __init__(self, images, duration=160):
        super().__init__(images,duration)
        self.requests=[]

    def sample_at(self, source, timestamps, **kwargs):
        from contextlib import closing
        for t in timestamps:
            with closing(self.sample(source,start_s=t,end_s=t+.1,**kwargs)) as frames:
                frame=next(frames,None)
            if frame is not None:
                yield frame


def pair(profile_fixture,tmp_path,force=False,scheduler="legacy",sampler=None):
    profile,pack,_,images=profile_fixture
    source=tmp_path/"fixture.mp4";source.write_bytes(b"synthetic")
    kind=sampler or SparseSampler
    oldsam,newraw=kind(images),kind(images)
    newservice=FrameSamplingService(newraw,enabled=False)
    original=newservice.get_many
    def get_many(source,timestamps,**kwargs):
        newraw.requests.extend(timestamps)
        return original(source,timestamps,**kwargs)
    newservice.get_many=get_many
    old=StateAwareHearthstoneAnalyzer(state_profile=profile,template_pack=pack,sampler=oldsam,use_cache=False)
    new=SparseAdaptiveHearthstoneAnalyzer(state_profile=profile,template_pack=pack,sampler=newservice,use_cache=False,
                                        sparse_settings=SparseSettings(force_full_fallback=force,scheduler=scheduler))
    def board(gray):
        t=int(gray[0,0])
        value=.5 if 3<=t<70 or 83<=t<141 else .1
        return value,{"fixture":value}
    old.board.score=new.board.score=board
    def fingerprint(gray,t,b):
        fp=routed(t,"IN_GAME" if b>.38 else "OUT_OF_GAME").fingerprint
        if t >= 83:
            fp.regions["opponent_portrait"] *= -1
        return fp
    new.fingerprint.extract=fingerprint
    return source,old,new,newraw


def test_sparse_scan_uses_30s_requests_and_local_refinement(profile_fixture,tmp_path):
    source,old,new,sampler=pair(profile_fixture,tmp_path)
    expected=old.analyze(source);actual=new.analyze(source)
    assert sampler.requests[:6] == [0.,30.,60.,90.,120.,150.]
    assert 75. in sampler.requests  # actual midpoint seek, not sequential discard
    assert 67.5 in sampler.requests and 71.25 in sampler.requests
    comparison=compare_analyses(expected,actual)
    assert comparison["equivalent"],comparison
    assert comparison["max_timestamp_error_s"] < 1e-8
    assert actual.metadata["adaptive"]["fallback_fraction"] < 1


def test_explicit_fallback_keeps_traditional(profile_fixture,tmp_path):
    source,old,new,sampler=pair(profile_fixture,tmp_path,True)
    assert compare_analyses(old.analyze(source),new.analyze(source))["equivalent"]
    assert not sampler.requests


def test_failed_seek_falls_back_but_cancellation_never_does(profile_fixture,tmp_path):
    source,old,new,_=pair(profile_fixture,tmp_path)
    with patch.object(new.frame_service,"get_many",side_effect=RuntimeError("seek failure")):
        actual=new.analyze(source)
    assert actual.metadata["adaptive"]["full_fallback"]
    assert compare_analyses(old.analyze(source),actual)["equivalent"]
    with patch.object(new.frame_service,"get_many",side_effect=AnalysisCancelled()),pytest.raises(AnalysisCancelled):
        new.analyze(source)


def test_midpoint_splits_both_halves_for_multiple_transitions(profile_fixture,tmp_path):
    source,_,new,sampler=pair(profile_fixture,tmp_path)
    new.fingerprint.extract=lambda gray,t,b:routed(t,"IN_GAME").fingerprint
    # Force identity uncertainty at every observation: all subintervals explored,
    # including both sides, and unresolved leaves fall back to the old matcher.
    original=new.fingerprint.extract
    def missing(gray,t,b):
        fp=original(gray,t,b);fp.regions={}
        return fp
    new.fingerprint.extract=missing
    new.board.score=lambda gray:(.5,{})
    result=new.analyze(source)
    assert 7.5 in sampler.requests and 22.5 in sampler.requests
    assert result.metadata["adaptive"]["fallback_fraction"] == 1


def test_temporal_stability_resets_across_sparse_gaps(profile_fixture,tmp_path):
    _,_,new,_=pair(profile_fixture,tmp_path)
    rows=[MonitorObservation(0,.5,0,0),MonitorObservation(30,.5,0,0),MonitorObservation(60,.1,0,0)]
    assert new._plan_result_windows(rows,[],160)[0] == []
    contiguous=[MonitorObservation(t,.5 if t<12 else .1,0,0) for t in range(10,14)]
    assert new._plan_result_windows(contiguous,[],160)[0]


def test_exclusion_omits_descriptor(profile_fixture):
    profile,_,_,_=profile_fixture
    from analysis.gameplay.hearthstone.visual.state_profile import StateAwareProfile
    extractor=SparseFingerprintExtractor(StateAwareProfile(profile),exclusions=(ROIS["player_portrait"],))
    fp=extractor.extract(np.random.default_rng(1).integers(0,255,(64,96),np.uint8),0,.5)
    assert "player_portrait" not in fp.regions


def test_gui_factory_v2_selection_default_unchanged(monkeypatch):
    from analysis.gameplay_editing import GameplayEditSettings,hearthstone_detector
    monkeypatch.setenv("AUTO_CUTTER_GAMEPLAY_BACKEND","adaptive-v2")
    assert callable(hearthstone_detector(GameplayEditSettings()))
    monkeypatch.delenv("AUTO_CUTTER_GAMEPLAY_BACKEND")
    assert callable(hearthstone_detector(GameplayEditSettings()))

def test_cache_roundtrip_skips_sparse_requests(profile_fixture,tmp_path):
    from analysis.gameplay.cache import GameplayCache
    source,_,new,sampler=pair(profile_fixture,tmp_path)
    new.cache=GameplayCache(tmp_path/"detector-cache")
    first=new.analyze(source)
    requested=list(sampler.requests)
    second=new.analyze(source)
    assert second.metadata["cache_hit"]
    assert sampler.requests == requested
    assert compare_analyses(first,second)["exact_event_timestamps"]


def test_truly_invisible_game_is_an_explicit_limitation(profile_fixture,tmp_path):
    source,old,new,_=pair(profile_fixture,tmp_path)
    new.fingerprint.extract=lambda gray,t,b:routed(t,"IN_GAME" if b>.38 else "OUT_OF_GAME").fingerprint
    result=new.analyze(source)
    # Both 60s and 90s show the same heroes/map. The 70s END and 81s
    # START are outside guards. Preserve the counterexample, don't tune to it.
    assert not compare_analyses(old.analyze(source),result)["equivalent"]
    assert result.metadata["adaptive"]["invisible_transition_guarantee"] is False


def test_dynamic_play_area_hand_and_webcam_do_not_change_descriptors(profile_fixture):
    from analysis.gameplay.hearthstone.visual.state_profile import StateAwareProfile
    profile,_,_,_=profile_fixture
    extractor=SparseFingerprintExtractor(StateAwareProfile(profile))
    image=np.random.default_rng(32).integers(30,220,(540,960),np.uint8)
    before=extractor.extract(image,0,.5)
    image[160:350,300:680]=0  # minions/play area
    image[460:540,300:600]=255  # hand
    image[270:480,:200]=0  # webcam
    after=extractor.extract(image,30,.5)
    assert before.regions.keys() == after.regions.keys()
    for key in before.regions:
        np.testing.assert_array_equal(before.regions[key],after.regions[key])


def test_portrait_descriptor_tolerates_small_brightness_changes(profile_fixture):
    from analysis.gameplay.hearthstone.visual.state_profile import StateAwareProfile
    profile,_,_,_=profile_fixture
    extractor=SparseFingerprintExtractor(StateAwareProfile(profile))
    image=np.random.default_rng(21).integers(30,200,(540,960),np.uint8)
    a=extractor.extract(image,0,.5)
    b=extractor.extract(image+15,30,.5)
    for key in ("player_portrait","opponent_portrait"):
        assert float((a.regions[key]*b.regions[key]).sum()) > .99

def test_v2_shadow_runs_candidate_and_returns_traditional(monkeypatch):
    from analysis.gameplay.models import GameplayAnalysisResult
    from analysis.gameplay_editing import GameplayEditSettings,hearthstone_detector
    monkeypatch.setenv("AUTO_CUTTER_GAMEPLAY_BACKEND","adaptive-v2-shadow")
    reference=GameplayAnalysisResult("fixture.mp4",10,[],[],"reference")
    candidate=GameplayAnalysisResult("fixture.mp4",10,[],[],"v2")
    with patch.object(StateAwareHearthstoneAnalyzer,"analyze",return_value=reference) as old, \
            patch.object(SparseAdaptiveHearthstoneAnalyzer,"analyze",return_value=candidate) as new:
        run=hearthstone_detector(GameplayEditSettings())
        result=run(Path("fixture.mp4"),AnalysisCancellation(),None)
    assert result is reference
    assert old.call_count == new.call_count == 1
    assert result.metadata["adaptive_shadow"]["equivalent"]


class LongVsSampler(SparseSampler):
    """VS visible for 6 s, as measured on the real VOD (>= 5.2 s at the frozen matcher)."""

    def sample(self, *args, fps=1, start_s=0, end_s=None, **kwargs):
        from analysis.gameplay.hearthstone.visual.sampler import SampledFrame
        for t in np.arange(start_s, min(end_s or self.duration, self.duration)-1e-8, 1/fps):
            image = np.full((64,96,3),30,np.uint8)
            image[0,0] = int(t)
            if 1 <= t < 7 or 77 <= t < 83:
                image[2:10,40:52] = self.images["vs"][:,:,None]
            yield SampledFrame(float(t),image)


def lattice_pair(profile_fixture,tmp_path):
    return pair(profile_fixture,tmp_path,scheduler="lattice",sampler=LongVsSampler)


def test_lattice_matches_traditional_with_fewer_real_rows(profile_fixture,tmp_path):
    source,old,new,sampler=lattice_pair(profile_fixture,tmp_path)
    expected,actual=old.analyze(source),new.analyze(source)
    comparison=compare_analyses(expected,actual)
    assert comparison["equivalent"],comparison
    assert comparison["max_timestamp_error_s"] < 1e-8
    adaptive=actual.metadata["adaptive"]
    assert adaptive["scheduler"] == "lattice" and adaptive["real_row_fraction"] < .75
    assert actual.metadata["board_reacquisitions"] == expected.metadata["board_reacquisitions"]
    # Every traditional result window is planned (V2 may add orphan windows).
    for window in expected.metadata["result_windows"]:
        assert any(w["start"] <= window["start"] and window["end"] <= w["end"] for w in actual.metadata["result_windows"])
    # Sparse requests are lattice points or refill rows; never a sequential discard.
    assert {0.,30.,60.,90.,120.,150.} <= set(sampler.requests)
    assert all(float(t).is_integer() for t in sampler.requests)


def test_lattice_reads_real_rows_only_at_planner_transitions(profile_fixture,tmp_path):
    source,_,new,sampler=lattice_pair(profile_fixture,tmp_path)
    new.analyze(source)
    odd={int(t) for t in sampler.requests if int(t)%2}
    # Board rises at 3/83 and falls at 70/141; VS edges at 1-7 and 77-83.
    assert {3,69,141} <= odd or {3,69} <= odd
    counters=new.details["counters"]
    assert counters["refill_rows"] < 30 and counters["imputed_rows"] > 0
    assert counters["planner_rows"] == 160
    rows=new._planner_rows
    assert [r.timestamp for r in rows] == [float(t) for t in range(160)]


def test_lattice_identity_uncertainty_samples_dense_lattice_not_sequential(profile_fixture,tmp_path):
    source,_,new,sampler=lattice_pair(profile_fixture,tmp_path)
    def missing(gray,t,b):
        fp=routed(t,"IN_GAME").fingerprint;fp.regions={}
        return fp
    new.fingerprint.extract=missing
    new.board.score=lambda gray:(.5,{})
    result=new.analyze(source)
    assert set(range(0,160,2)) <= {int(t) for t in sampler.requests}
    assert result.metadata["adaptive"]["counters"]["refill_rows"] <= 10


def test_lattice_failure_falls_back_and_cancellation_propagates(profile_fixture,tmp_path):
    source,old,new,_=lattice_pair(profile_fixture,tmp_path)
    with patch.object(new.frame_service,"get_many",side_effect=RuntimeError("seek failure")):
        actual=new.analyze(source)
    assert actual.metadata["adaptive"]["full_fallback"]
    assert compare_analyses(old.analyze(source),actual)["equivalent"]
    with patch.object(new.frame_service,"get_many",side_effect=AnalysisCancelled()),pytest.raises(AnalysisCancelled):
        new.analyze(source)


@pytest.mark.parametrize("values",[{"scheduler":"other"},{"lattice_s":7},{"lattice_s":1.7},{"workers":0},{"workers":True},
                                   {"recovery_lookback_s":1},{"recovery_lookback_s":float("inf")}])
def test_lattice_settings_validate(values):
    with pytest.raises(ValueError):
        replace(SparseSettings(),**values)


def test_default_v2_uses_in_process_unpersisted_frames(profile_fixture):
    profile,pack,_,_=profile_fixture
    detector=SparseAdaptiveHearthstoneAnalyzer(state_profile=profile,template_pack=pack,use_cache=False)
    assert detector.frame_service.in_process and not detector.frame_service.enabled
    legacy=SparseAdaptiveHearthstoneAnalyzer(state_profile=profile,template_pack=pack,use_cache=False,
                                            sparse_settings=SparseSettings(scheduler="legacy"))
    assert not legacy.frame_service.in_process


@pytest.fixture(scope="module")
def encoded_clip(tmp_path_factory):
    pytest.importorskip("av")
    import subprocess
    from utils.ffmpeg import ensure_ffmpeg
    ffmpeg,_=ensure_ffmpeg()
    target=tmp_path_factory.mktemp("inprocess")/"clip.mp4"
    subprocess.run([ffmpeg,"-nostdin","-hide_banner","-v","error","-y","-f","lavfi","-i",
                    "testsrc2=size=320x180:rate=30:duration=8","-c:v","libx264","-preset","ultrafast",
                    "-g","60","-bf","2","-pix_fmt","yuv420p",str(target)],check=True)
    return target


def test_in_process_frames_match_ffmpeg_pixels_and_pts(encoded_clip):
    from contextlib import closing
    from analysis.gameplay.hearthstone.visual.sampler import FFmpegFrameSampler
    ffmpeg=FFmpegFrameSampler()
    reference=FrameSamplingService(ffmpeg,enabled=False)
    service=FrameSamplingService(ffmpeg,enabled=False,in_process=True,workers=2)
    # Exact frame-boundary requests are avoided: FFmpeg batched select compares
    # float pts and can skip a boundary frame its single-request path returns.
    times=[0.,.5,1.,1.97,2.,3.,3.3,4.,5.5,6.,7.95]
    try:
        expected=reference.get_many(encoded_clip,times,max_width=160)
        for _ in range(2):  # second pass reuses open sessions, decoding backwards and forwards
            actual=dict(service.iter_many(encoded_clip,times,max_width=160))
            for t in times:
                a,b=expected[t],actual[t]
                assert (a is None) == (b is None),t
                if a is not None:
                    assert abs(a.timestamp-b.timestamp) < 1e-9,t
                    np.testing.assert_array_equal(a.image,b.image)
        with closing(ffmpeg.sample(encoded_clip,fps=1.,start_s=1.5,end_s=6.,max_width=160)) as frames:
            sequential=list(frames)
        scanned=list(service.scan(encoded_clip,fps=1.,start_s=1.5,end_s=6.,max_width=160))
        assert [f.timestamp for f in scanned] == [f.timestamp for f in sequential]
        for a,b in zip(sequential,scanned):
            np.testing.assert_array_equal(a.image,b.image)
        assert service.access_counters()["containers"] <= 2
    finally:
        service.close()


def test_in_process_failure_returns_to_ffmpeg_route(encoded_clip):
    from analysis.gameplay.hearthstone.visual.sampler import FFmpegFrameSampler
    service=FrameSamplingService(FFmpegFrameSampler(),enabled=False,in_process=True)
    reader=service._in_process_reader()
    with patch.object(reader,"iter_many",side_effect=OSError("broken container")):
        frames=dict(service.iter_many(encoded_clip,[1.,2.],max_width=160))
    assert all(frames[t] is not None for t in (1.,2.))
    assert service._reader_failed and service._in_process_reader() is None


def test_in_process_cancellation_is_not_a_fallback(encoded_clip):
    from analysis.gameplay.hearthstone.visual.sampler import FFmpegFrameSampler
    service=FrameSamplingService(FFmpegFrameSampler(),enabled=False,in_process=True)
    token=AnalysisCancellation();token.cancel()
    with pytest.raises(AnalysisCancelled):
        list(service.iter_many(encoded_clip,[1.,2.,3.],max_width=160,cancellation=token))
    assert not service._reader_failed
    service.close()


def flicker_board(gray):
    """End-of-game dead band with a single odd-second disappearance before each short result."""
    t=int(gray[0,0])
    if 3<=t<62 or 83<=t<130:
        value=.5
    elif t in (69,141):
        value=.1
    else:
        value=.3
    return value,{"fixture":value}


def test_lattice_reads_single_second_board_drop_before_short_result(profile_fixture,tmp_path):
    # Regression (LOSS -> UNKNOWN on a real VOD): an imputed odd row hid the
    # first board disappearance, the -2/+5 s window started after a 0.1 s DEFEAT.
    source,old,new,sampler=lattice_pair(profile_fixture,tmp_path)
    old.board.score=new.board.score=flicker_board
    expected,actual=old.analyze(source),new.analyze(source)
    assert [e.type.value for e in expected.events].count("DEFEAT") == 1
    comparison=compare_analyses(expected,actual)
    assert comparison["equivalent"],comparison
    assert comparison["max_timestamp_error_s"] < 1e-8
    assert {69.,141.} <= set(sampler.requests)
    triggers={t for w in actual.metadata["result_windows"] for t,_ in w["triggers"]}
    assert {69.,141.} <= triggers
    assert actual.metadata["adaptive"]["counters"]["present_refill_rows"] > 0


def test_present_rows_only_while_board_present(profile_fixture,tmp_path):
    _,_,new,_=lattice_pair(profile_fixture,tmp_path)
    row=lambda t,b:MonitorObservation(float(t),b,0.,0.)
    rows={0.:row(0,.5),2.:row(2,.5),4.:row(4,.3),6.:row(6,.3),8.:row(8,.1),10.:row(10,.3),12.:row(12,.3)}
    keys={k:float(k) for k in range(0,13,2)}
    # Solid board (0-2) stays imputed; dead band while present is read. A single
    # low row (8) does not end presence, so 9/11 are read too.
    assert new._present_rows(keys,rows,13) == {5,7,9,11}
    # A sustained disappearance (two real low rows) ends presence: 13 stays imputed.
    rows.update({10.:row(10,.1),12.:row(12,.3)});keys={k:float(k) for k in range(0,13,2)}|{9:9.}
    rows[9.]=row(9,.1)
    assert new._present_rows(keys,rows,14) == {5,7}
    assert new._present_rows({0:0.,2:2.},{0.:row(0,.3),2.:row(2,.3)},3) == set()


# Real-source regression (LOSS -> UNKNOWN). Skipped when the VOD is not present:
# AUTO_CUTTER_TEST_THEORYCRAFTING overrides the path. The window is test data
# only; production code has no timestamps.
THEORYCRAFTING=Path(__import__("os").environ.get("AUTO_CUTTER_TEST_THEORYCRAFTING",
                                                 r"C:\Users\jakub\Downloads\THEORYCRAFTING DAY 1.mp4"))


@pytest.mark.skipif(not THEORYCRAFTING.is_file(),reason="real VOD not available")
# A whole game (VS ... DEFEAT): the traditional planner arms on the VS.
@pytest.mark.parametrize("start,end",[(12250.,12800.)])
def test_real_vod_single_second_drop_keeps_short_defeat(tmp_path,start,end):
    import subprocess
    import av
    from analysis.gameplay_editing import default_detector_assets
    from utils.ffmpeg import ensure_ffmpeg
    container=av.open(str(THEORYCRAFTING));stream=container.streams.video[0]
    container.seek(int(start/float(stream.time_base)),stream=stream,backward=True,any_frame=False)
    key=float(next(container.decode(stream)).pts*stream.time_base);container.close()
    clip=tmp_path/"window.mp4"
    subprocess.run([ensure_ffmpeg()[0],"-nostdin","-v","error","-ss",f"{key:.6f}","-i",str(THEORYCRAFTING),
                    "-t",f"{end-key:.3f}","-map","0:v:0","-c","copy",str(clip)],check=True)
    options=dict(default_detector_assets("hearthstone"))
    expected=StateAwareHearthstoneAnalyzer(**options,use_cache=False).analyze(clip)
    actual=SparseAdaptiveHearthstoneAnalyzer(**options,use_cache=False).analyze(clip)
    assert "DEFEAT" in [e.type.value for e in expected.events]
    comparison=compare_analyses(expected,actual)
    assert comparison["equivalent"],comparison
    assert comparison["max_timestamp_error_s"] < 1e-6


def test_one_frame_dip_does_not_consume_the_real_end(profile_fixture,tmp_path):
    # Regression (VICTORY lost on a real VOD): a one-frame board dip mid-game
    # ended presence; the board then stayed in the dead band, so the actual end
    # (board gone for good) opened no result window.
    _,_,new,_=lattice_pair(profile_fixture,tmp_path)
    board={t:(.5 if t<10 else .1 if t==20 else .3 if t<50 else .1) for t in range(60)}
    new._planner_rows=[MonitorObservation(float(t),b,0.,0.) for t,b in board.items()]
    new.full_fallback=False
    windows,_=new._plan_result_windows([],[],60.)
    triggers={t:c for w in windows for t,c in w.triggers}
    assert triggers.get(20.) == "orphan_board_disappeared"  # frozen-style trigger kept
    assert triggers.get(50.) == "sustained_board_disappeared"
    assert any(w.start <= 49.5 <= w.end for w in windows)


def test_end_recovery_reaches_back_to_last_solid_board(profile_fixture,tmp_path):
    # Regression (DEFEAT banner 2.7-4.2 s before the board dims): the recovery
    # window starts at the last solid board, bounded by recovery_lookback_s.
    _,_,new,_=lattice_pair(profile_fixture,tmp_path)
    new.full_fallback=False
    board={t:(.5 if t<=40 else .3 if t<45 else .1) for t in range(60)}
    new._planner_rows=[MonitorObservation(float(t),b,0.,0.) for t,b in board.items()]
    windows,_=new._plan_result_windows([],[],60.)
    recovery=[w for w in windows if any(c=="sustained_board_disappeared" for _,c in w.triggers)]
    assert recovery and recovery[0].start <= 40. and recovery[0].end >= 50.
    board={t:(.5 if t<=10 else .3 if t<45 else .1) for t in range(60)}
    new._planner_rows=[MonitorObservation(float(t),b,0.,0.) for t,b in board.items()]
    windows,_=new._plan_result_windows([],[],60.)
    assert min(w.start for w in windows if w.start > 20) == 45.-new.sparse_settings.recovery_lookback_s


def _real_window(tmp_path,start,end):
    import subprocess
    import av
    from utils.ffmpeg import ensure_ffmpeg
    container=av.open(str(THEORYCRAFTING));stream=container.streams.video[0]
    container.seek(int(start/float(stream.time_base)),stream=stream,backward=True,any_frame=False)
    key=float(next(container.decode(stream)).pts*stream.time_base);container.close()
    clip=tmp_path/f"window_{int(start)}.mp4"
    subprocess.run([ensure_ffmpeg()[0],"-nostdin","-v","error","-ss",f"{key:.6f}","-i",str(THEORYCRAFTING),
                    "-t",f"{end-key:.3f}","-map","0:v:0","-c","copy",str(clip)],check=True)
    return clip


@pytest.mark.skipif(not THEORYCRAFTING.is_file(),reason="real VOD not available")
def test_real_vod_one_frame_defeat_banner_is_a_loss_and_missing_result_stays_unknown(tmp_path):
    # Game whose "Sconfitta!" banner reaches the evidence floor in one 60 FPS frame,
    # and a game whose result is never broadcast (black screen, then menu).
    from analysis.gameplay_editing import default_detector_assets
    options=dict(default_detector_assets("hearthstone"))
    clip=_real_window(tmp_path,10430.,11175.)
    traditional=StateAwareHearthstoneAnalyzer(**options,use_cache=False).analyze(clip)
    adaptive=SparseAdaptiveHearthstoneAnalyzer(**options,use_cache=False).analyze(clip)
    assert [g.result.value for g in traditional.games] == ["UNKNOWN"]  # traditional detector unchanged
    assert [g.result.value for g in adaptive.games] == ["LOSS"]
    defeat=[e for e in adaptive.events if e.type.value=="DEFEAT"]
    assert len(defeat) == 1 and defeat[0].metadata["result_acceptance"] == "single_frame_local_peak"
    unknown=SparseAdaptiveHearthstoneAnalyzer(**options,use_cache=False).analyze(_real_window(tmp_path,9020.,9200.))
    assert [g.result.value for g in unknown.games] == ["UNKNOWN"]
    assert not [e for e in unknown.events if e.type.value in ("VICTORY","DEFEAT")]
