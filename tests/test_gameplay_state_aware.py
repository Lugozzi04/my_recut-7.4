"""Mechanics/regression tests. Synthetic images do not measure Hearthstone accuracy."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import sys
from unittest.mock import patch

import numpy as np
import pytest

from analysis.audio_service import AnalysisCancellation, AnalysisCancelled
from analysis.gameplay.models import GameEvent, GameEventType
from analysis.gameplay.result_events import deduplicate_result_events
from analysis.gameplay.hearthstone.state_monitor import GameStateMonitor, MonitorObservation, MonitorSettings, ResultWindow, merge_windows


def event(t, kind='DEFEAT', score=.95):
    return GameEvent(t, GameEventType(kind), score)


def row(t, board=.5, score=.1, margin=.01):
    return MonitorObservation(t, board, score, margin)


def test_board_disappearance_requires_vs_and_observed_board():
    settings = MonitorSettings()
    monitor = GameStateMonitor(settings)
    rows = [row(0), row(1), row(2, board=.1)]
    assert monitor.plan(rows, [], duration=10)[0] == []
    windows, reacquired = monitor.plan(rows, [event(0, 'VS_SCREEN')], duration=10)
    assert windows == [ResultWindow(0, 7, ((2, 'board_disappeared'),))]
    assert reacquired == [1]


def test_start_without_board_does_not_trigger_a_result_window():
    rows = [row(t, board=.1) for t in range(10)]
    assert GameStateMonitor(MonitorSettings()).plan(rows, [event(0, 'VS_SCREEN')], duration=20)[0] == []


def test_hysteresis_ignores_dimming_and_triggers_actual_disappearance():
    rows = [row(0), row(1), row(2, board=.30), row(3, board=.21), row(4, board=.19)]
    windows, _ = GameStateMonitor(MonitorSettings()).plan(rows, [event(0, 'VS_SCREEN')], duration=20)
    assert windows[0].triggers == ((4, 'board_disappeared'),)


def test_weak_result_is_independent_backup_even_without_vs():
    windows, _ = GameStateMonitor(MonitorSettings()).plan([row(4, board=.1, score=.8, margin=.3)], [], duration=20)
    assert windows == [ResultWindow(2, 9, ((4, 'weak_result'),))]


def test_ambiguous_weak_result_does_not_trigger():
    assert GameStateMonitor(MonitorSettings()).plan([row(4, score=.9, margin=.01)], [], duration=20)[0] == []


def test_sustained_state_does_not_open_repeated_windows():
    rows = [row(0), row(1)] + [row(t, board=.1, score=.8, margin=.3) for t in range(2, 15)]
    windows, _ = GameStateMonitor(MonitorSettings()).plan(rows, [event(0, 'VS_SCREEN')], duration=20)
    assert len(windows) == 1
    assert len(windows[0].triggers) == 2


def test_board_reacquisition_rearms_transition():
    rows = [row(0), row(1), row(2, board=.1), row(10), row(11), row(12, board=.1)]
    windows, reacquired = GameStateMonitor(MonitorSettings()).plan(rows, [event(0, 'VS_SCREEN')], duration=20)
    assert len(windows) == 2
    assert reacquired == [1, 11]


def test_window_merge_preserves_past_future_and_all_trigger_evidence():
    assert merge_windows([ResultWindow(5, 10, ((7, 'weak_result'),)), ResultWindow(0, 7, ((2, 'board_disappeared'),))]) == [
        ResultWindow(0, 10, ((2, 'board_disappeared'), (7, 'weak_result')))]


@pytest.mark.parametrize('change', [{'fps':0}, {'fps':3}, {'high_fps':float('nan')}, {'high_fps':9},
                                  {'board_low':.8}, {'board_observations':True}, {'before_s':-1},
                                  {'after_s':0}, {'weak_margin':1.1}, {'weak_score':True}])
def test_invalid_monitor_settings(change):
    with pytest.raises(ValueError):
        replace(MonitorSettings(), **change)


@pytest.mark.parametrize('rows', [[row(2),row(1)], [row(1),row(1)], [row(-1)], [row(2,board=float('nan'))],
                                  [row(2,score=1.1)], [row(21)]])
def test_invalid_monitor_evidence(rows):
    with pytest.raises(ValueError):
        GameStateMonitor(MonitorSettings()).plan(rows, [], duration=20)


def test_result_episode_merges_multiple_animation_phases():
    merged = deduplicate_result_events([event(10), event(12,'DEFEAT',.99), event(36)])
    assert len(merged) == 1
    assert merged[0].timestamp == 10
    assert merged[0].confidence == .99
    assert merged[0].metadata['result_episode_clusters'] == 3
    assert merged[0].metadata['observation_count'] == 3


@pytest.mark.parametrize('barrier', [11.,20.,35.])
def test_board_return_is_hard_result_episode_barrier(barrier):
    assert len(deduplicate_result_events([event(10), event(36)], barriers=[barrier])) == 2


def test_intervening_vs_preserves_separate_games():
    events = deduplicate_result_events([event(10), event(20,'VS_SCREEN'), event(30)])
    assert [x.type for x in events] == [GameEventType.DEFEAT,GameEventType.VS_SCREEN,GameEventType.DEFEAT]


def test_opposite_result_remains_visible_and_never_overwritten_by_confidence():
    events = deduplicate_result_events([event(10,'VICTORY',.93), event(11,'DEFEAT',.99)])
    assert len(events) == 2


def test_result_dedup_is_idempotent_and_does_not_mutate_input():
    original = [event(10),event(12)]
    once = deduplicate_result_events(original)
    assert deduplicate_result_events(once) == once
    assert original[0].metadata == {}


def test_distant_same_results_do_not_merge_without_state_evidence():
    assert len(deduplicate_result_events([event(10), event(100)])) == 2


requires_cv = pytest.mark.skipif(importlib.util.find_spec('cv2') is None, reason='Install requirements-gameplay.txt')


@pytest.fixture
def profile_fixture(tmp_path):
    from analysis.gameplay.hearthstone.visual.templates import require_opencv
    cv = require_opencv()
    folder = tmp_path/'profilo è 🎮'
    folder.mkdir()
    images = {}
    for index, name in enumerate(['victory','defeat','board','vs']):
        shape = (16,16) if name == 'board' else (8,12)
        image = np.random.default_rng(345+index).integers(0,256,shape,dtype=np.uint8)
        if name != 'board':
            image[:2,:]=30;image[-2:,:]=30;image[:,:2]=30;image[:,-2:]=30
        images[name] = image
        (folder/(name+'.png')).write_bytes(cv.imencode('.png',image)[1].tobytes())
    profile = {'schema_version':1,'profile':'hearthstone-state-aware','reference_size':[96,64],
               'result':{'roi':[.25,.25,.5,.5],'representation':'gray','threshold':.85,'margin':.04,'scales':[1.],
                         'templates':[{'class':name.upper(),'path':name+'.png'} for name in ('victory','defeat')]},
               'board':{'anchors':[{'id':'structural','roi':[0,.5,16/96,.25],'exclude':[.25,.25,.5,.5],
                                    'padding_px':2,'templates':[{'path':'board.png'}]}]},
               'monitor':MonitorSettings().to_mapping()}
    profile_path = folder/'profile.json'
    profile_path.write_text(json.dumps(profile),encoding='utf-8')
    pack = {'schema_version':1,'profile':'hearthstone','reference_size':[96,64],'viewport':[0,0,1,1],
            'classes':{kind:{'roi':[0,0,1,1],'threshold':.92,'scales':[1.],'directory':kind.lower()}
                       for kind in ('VS_SCREEN','VICTORY','DEFEAT')}}
    for kind, name in [('VS_SCREEN','vs'),('VICTORY','victory'),('DEFEAT','defeat')]:
        directory=folder/kind.lower();directory.mkdir();(directory/'patch.png').write_bytes((folder/(name+'.png')).read_bytes())
    pack_path=folder/'templates.json';pack_path.write_text(json.dumps(pack))
    return profile_path,pack_path,profile,images


@requires_cv
@pytest.mark.parametrize('method', ['gray','clahe','gradient'])
def test_text_methods_measure_class_scores_and_margin(profile_fixture, method):
    from analysis.gameplay.hearthstone.visual.state_profile import StateAwareProfile
    from analysis.gameplay.hearthstone.visual.result_text import ResultTextDetector
    filename,_,raw,images=profile_fixture
    raw['result']['representation']=method
    filename.write_text(json.dumps(raw))
    detector=ResultTextDetector(StateAwareProfile(filename))
    roi=np.full((32,48,3),30,np.uint8);roi[10:18,15:27]=images['defeat'][:,:,None]
    measured=detector.score_roi(roi)
    detected=detector.event(measured,3.5)
    assert detected is not None and detected.type == GameEventType.DEFEAT
    assert detected.confidence == measured['score']
    assert detected.metadata['class_scores']['DEFEAT'] > detected.metadata['class_scores']['VICTORY']


@requires_cv
def test_identical_text_templates_return_unknown_even_with_zero_margin(profile_fixture):
    from analysis.gameplay.hearthstone.visual.state_profile import StateAwareProfile
    from analysis.gameplay.hearthstone.visual.result_text import ResultTextDetector
    filename,_,raw,images=profile_fixture
    (filename.parent/'victory.png').write_bytes((filename.parent/'defeat.png').read_bytes())
    raw['result']['margin']=0;filename.write_text(json.dumps(raw))
    detector=ResultTextDetector(StateAwareProfile(filename))
    roi=np.zeros((32,48),np.uint8);roi[10:18,15:27]=images['defeat']
    assert detector.event(detector.score_roi(roi),1) is None


@requires_cv
def test_blank_result_roi_has_no_outcome(profile_fixture):
    from analysis.gameplay.hearthstone.visual.state_profile import StateAwareProfile
    from analysis.gameplay.hearthstone.visual.result_text import ResultTextDetector
    detector=ResultTextDetector(StateAwareProfile(profile_fixture[0]))
    assert detector.event(detector.score_roi(np.zeros((32,48),np.uint8)),1) is None
    with pytest.raises(ValueError):detector.score_roi(np.zeros((31,48),np.uint8))


@requires_cv
@pytest.mark.parametrize('unsafe',['../outside.png',r'C:\outside.png',r'D:relative.png',r'..\outside.png'])
def test_profile_rejects_unsafe_asset_paths(profile_fixture,unsafe):
    from analysis.gameplay.hearthstone.visual.state_profile import StateAwareProfile
    filename,_,raw,_=profile_fixture;raw['result']['templates'][0]['path']=unsafe;filename.write_text(json.dumps(raw))
    with pytest.raises(ValueError):StateAwareProfile(filename)


@requires_cv
@pytest.mark.parametrize('change',[('threshold',float('nan')),('scales',[1.,1.]),('representation','neural')])
def test_profile_rejects_invalid_result_configuration(profile_fixture,change):
    from analysis.gameplay.hearthstone.visual.state_profile import StateAwareProfile
    filename,_,raw,_=profile_fixture;raw['result'][change[0]]=change[1];filename.write_text(json.dumps(raw))
    with pytest.raises(ValueError):StateAwareProfile(filename)


@requires_cv
def test_profile_fingerprint_changes_when_asset_changes(profile_fixture):
    from analysis.gameplay.hearthstone.visual.state_profile import StateAwareProfile
    filename,_,_,_=profile_fixture;before=StateAwareProfile(filename).fingerprint
    target=filename.parent/'defeat.png';target.write_bytes((filename.parent/'victory.png').read_bytes())
    assert StateAwareProfile(filename).fingerprint != before


@requires_cv
def test_board_blank_nonfinite_correlations_are_not_positive(profile_fixture):
    from analysis.gameplay.hearthstone.visual.state_profile import StateAwareProfile
    from analysis.gameplay.hearthstone.visual.board import BoardPresenceDetector
    detector=BoardPresenceDetector(StateAwareProfile(profile_fixture[0]))
    confidence,scores=detector.score(np.zeros((64,96,3),np.uint8))
    assert confidence == 0 and scores == {'structural':0}


@requires_cv
def test_state_aware_import_does_not_initialize_qt_or_ml():
    import subprocess
    command=[sys.executable,'-c',"import sys;import analysis.gameplay.hearthstone.state_aware;assert not any(x in sys.modules for x in ['PySide6.QtWidgets','PySide6.QtWebEngineWidgets','torch'])"]
    completed=subprocess.run(command,capture_output=True,text=True,timeout=20)
    assert completed.returncode == 0,completed.stderr


class FixtureSampler:
    def __init__(self, images, *, timestamps=(10.,10.016667,10.033333)):
        self.images=images;self.timestamps=timestamps;self.closed=0;self.roi_calls=[]

    def probe(self,*args,**kwargs):
        from analysis.gameplay.hearthstone.visual.sampler import VideoInfo
        return VideoInfo(20.,96,64)

    def sample(self,*args,fps=1.,**kwargs):
        from analysis.gameplay.hearthstone.visual.sampler import SampledFrame
        try:
            times=[1.,1.166667,1.333333] if fps==6 else list(range(20))
            for t in times:
                frame=np.zeros((64,96,3),np.uint8)
                if 1<=t<2:frame[2:10,40:52]=self.images['vs'][:,:,None]
                yield SampledFrame(float(t),frame)
        finally:self.closed+=1

    def sample_roi(self,*args,**kwargs):
        from analysis.gameplay.hearthstone.visual.sampler import SampledFrame
        self.roi_calls.append(kwargs)
        try:
            for t in self.timestamps:
                if kwargs['start_s']<=t<kwargs['end_s']:
                    image=np.zeros((32,48,3),np.uint8);image[10:18,15:27]=self.images['defeat'][:,:,None]
                    yield SampledFrame(t,image)
        finally:self.closed+=1


@contextmanager
def candidate_fixture(profile_fixture,tmp_path,*,timestamps=(10.,10.016667,10.033333),use_cache=False):
    from analysis.gameplay.hearthstone.state_aware import StateAwareHearthstoneAnalyzer
    filename,pack,_,images=profile_fixture
    sampler=FixtureSampler(images,timestamps=timestamps)
    analyzer=StateAwareHearthstoneAnalyzer(state_profile=filename,template_pack=pack,sampler=sampler,
                                          use_cache=use_cache,cache_directory=tmp_path/'cache')
    count=[0]
    def board(_):
        t=count[0];count[0]+=1
        value=.5 if 2<=t<=7 else .1
        return value,{'fixture':value}
    source=tmp_path/'source è 🎮.mp4';source.write_bytes(b'fixture-media')
    with patch.object(analyzer.board,'score',side_effect=board):
        yield analyzer,sampler,source


@requires_cv
def test_candidate_analyzes_only_trigger_windows_and_reuses_shared_assembler(profile_fixture,tmp_path):
    with candidate_fixture(profile_fixture,tmp_path) as (analyzer,sampler,source):
        progress=[];result=analyzer.analyze(source,on_progress=progress.append)
    assert len(result.games)==1 and result.games[0].result.value=='LOSS'
    assert result.games[0].start==1 and result.games[0].end==10
    assert sampler.roi_calls[0]['start_s']==6 and sampler.roi_calls[0]['end_s']==13
    assert sampler.roi_calls[0]['fps']==60
    assert result.metadata['dense_result_frames']==0
    assert progress==sorted(set(progress)) and progress[-1]==100
    assert sampler.closed==3


@requires_cv
@pytest.mark.parametrize('timestamps',[(10.,),(10.,10.),(10.,10.001)])
def test_single_or_duplicate_source_frame_does_not_manufacture_stability(profile_fixture,tmp_path,timestamps):
    with candidate_fixture(profile_fixture,tmp_path,timestamps=timestamps) as (analyzer,_,source):
        result=analyzer.analyze(source)
    assert not any(e.type==GameEventType.DEFEAT for e in result.events)
    assert result.games[0].end is None


@requires_cv
def test_cancellation_closes_generators_and_allows_clean_retry(profile_fixture,tmp_path):
    with candidate_fixture(profile_fixture,tmp_path) as (analyzer,sampler,source):
        token=AnalysisCancellation()
        def cancel(progress):
            if progress>=5:token.cancel()
        with pytest.raises(AnalysisCancelled):analyzer.analyze(source,cancellation=token,on_progress=cancel)
        assert sampler.closed==1
    with candidate_fixture(profile_fixture,tmp_path) as (analyzer,_,source):
        assert analyzer.analyze(source).games[0].result.value=='LOSS'


@requires_cv
def test_cache_avoids_second_decode_and_isolated_from_old_strategy(profile_fixture,tmp_path):
    with candidate_fixture(profile_fixture,tmp_path,use_cache=True) as (analyzer,sampler,source):
        first=analyzer.analyze(source);closed=sampler.closed
        second=analyzer.analyze(source)
        assert second.metadata['cache_hit'] and sampler.closed==closed
        assert first.settings['analyzer_version']=='hearthstone-state-aware-v2'


@requires_cv
def test_source_change_is_rejected_before_cache_publish(profile_fixture,tmp_path):
    from analysis.gameplay.analyzer import GameplayAnalysisError
    with candidate_fixture(profile_fixture,tmp_path,use_cache=True) as (analyzer,_,source):
        def mutate(progress):
            if progress>=65:source.write_bytes(b'changed-source-much-larger')
        with pytest.raises(GameplayAnalysisError,match='Source changed'):analyzer.analyze(source,on_progress=mutate)
        assert not list((tmp_path/'cache').glob('*.json'))


def test_debug_cli_state_profile_requires_explicit_strategy(capsys):
    from analysis.gameplay.hearthstone.debug_analyze import main
    assert main(['not-needed.mp4','--state-profile','profile.json'])==2
    assert 'require --strategy state-aware' in capsys.readouterr().err


def test_debug_cli_state_strategy_requires_profile(capsys):
    from analysis.gameplay.hearthstone.debug_analyze import main
    assert main(['not-needed.mp4','--strategy','state-aware'])==2
    assert 'requires --state-profile' in capsys.readouterr().err


@requires_cv
def test_robustness_perturbations_are_bounded_reproducible_and_not_templates(profile_fixture):
    from analysis.gameplay.hearthstone.result_experiment import robustness_variants
    image=np.repeat(profile_fixture[3]['defeat'][:,:,None],3,axis=2)
    before=image.copy();first=list(robustness_variants(image));second=list(robustness_variants(image))
    assert len(first)==15
    assert np.array_equal(before,image)
    assert all(a.shape==image.shape and a.dtype==np.uint8 for _,a in first)
    assert all(n==m and np.array_equal(a,b) for (n,a),(m,b) in zip(first,second))


@pytest.mark.parametrize('text,expected',[('Vittoria!','VICTORY'),('Vittor,ia!','VICTORY'),
    ('Sconfitta!!','DEFEAT'),('Scorifitta!','UNKNOWN'),('Vittoria Sconfitta','UNKNOWN'),('','UNKNOWN')])
def test_ocr_exact_letter_normalization_does_not_fuzzily_force_loss(text,expected):
    from tools.gameplay.ocr_experiment import recognized_result
    assert recognized_result(text)==expected


@requires_cv
def test_calibration_comparison_preserves_unreviewed_labels_and_does_not_choose_threshold(profile_fixture):
    from analysis.gameplay.hearthstone.visual.state_profile import StateAwareProfile
    from analysis.gameplay.hearthstone.result_experiment import compare_samples
    filename,_,raw,images=profile_fixture;p=StateAwareProfile(filename)
    image=np.full((32,48,3),30,np.uint8);image[10:18,15:27]=images['defeat'][:,:,None]
    result=compare_samples(p,[{'image':image,'timestamp':1.,'label':'UNREVIEWED'}])
    assert all(method['frames']==0 for method in result['summary'].values())
    assert p.config['result']['threshold']==.85
    assert len(result['rows'])==3


@requires_cv
def test_native_window_cancellation_closes_sampler_without_publishing_cache(profile_fixture,tmp_path):
    with candidate_fixture(profile_fixture,tmp_path,use_cache=True) as (analyzer,sampler,source):
        token=AnalysisCancellation()
        def cancel(progress):
            if progress>=80:token.cancel()
        with pytest.raises(AnalysisCancelled):analyzer.analyze(source,cancellation=token,on_progress=cancel)
        assert sampler.closed==3
        assert not list((tmp_path/'cache').glob('*.json'))


@requires_cv
def test_state_aware_cli_rejects_ignored_global_sampling_overrides(capsys):
    from analysis.gameplay.hearthstone.debug_analyze import main
    code=main(['not-needed.mp4','--strategy','state-aware','--state-profile','profile.json','--result-fps','10'])
    assert code==2
    assert 'global sampling overrides do not apply' in capsys.readouterr().err


def scripted_text(script):
    """Monitor ROIs are blank; each window frame receives the next scripted (VICTORY, DEFEAT) pair."""
    pending=iter(script)
    def score(image):
        v,d=next(pending) if image.any() else (.1,.12)
        winner='VICTORY' if v>d else 'DEFEAT'
        return {'scores':{'VICTORY':v,'DEFEAT':d},'winner':winner,'score':max(v,d),'margin':abs(v-d),
                'best_templates':{k:{'template_id':k.lower(),'scale':1.,'location':[0,0]} for k in ('VICTORY','DEFEAT')}}
    return score


@requires_cv
def test_supported_subthreshold_loss_is_classified_from_event_peak(profile_fixture,tmp_path):
    with candidate_fixture(profile_fixture,tmp_path) as (analyzer,_,source):
        with patch.object(analyzer.text,'score_roi',side_effect=scripted_text([(.37,.80),(.36,.78),(.33,.60)])):
            result=analyzer.analyze(source)
    assert result.games[0].result.value=='LOSS' and result.games[0].end==10
    defeat=[e for e in result.events if e.type==GameEventType.DEFEAT]
    assert len(defeat)==1 and defeat[0].confidence==.80 and defeat[0].metadata['observation_count']==2
    assert defeat[0].metadata['result_acceptance']=='event_peak_argmax'
    assert result.settings['result_decision']['evidence_score']==.65
    assert [d['outcome'] for d in result.metadata['result_decisions']]==['DEFEAT']


@requires_cv
@pytest.mark.parametrize('script,reason',[([(.90,.88),(.91,.89),(.89,.88)],'ambiguous_peak'),
                                          ([(.30,.97),(.40,.60),(.40,.62)],'insufficient_support')])
def test_ambiguous_or_unsupported_result_leaves_outcome_unknown(profile_fixture,tmp_path,script,reason):
    with candidate_fixture(profile_fixture,tmp_path) as (analyzer,_,source):
        with patch.object(analyzer.text,'score_roi',side_effect=scripted_text(script)):
            result=analyzer.analyze(source)
    assert not any(e.type in (GameEventType.VICTORY,GameEventType.DEFEAT) for e in result.events)
    assert result.games[0].end is None and result.games[0].result.value=='UNKNOWN'
    assert [d['reason'] for d in result.metadata['rejected_events']]==[reason]


@requires_cv
def test_duplicate_clusters_of_one_animation_produce_one_event(profile_fixture,tmp_path):
    times=(10.,10.016667,10.6,10.616667)
    with candidate_fixture(profile_fixture,tmp_path,timestamps=times) as (analyzer,_,source):
        with patch.object(analyzer.text,'score_roi',side_effect=scripted_text([(.3,.97),(.3,.95),(.3,.90),(.3,.92)])):
            result=analyzer.analyze(source)
    assert result.metadata['result_clusters_before_deduplication']==2
    defeat=[e for e in result.events if e.type==GameEventType.DEFEAT]
    assert len(defeat)==1 and defeat[0].metadata['observation_count']==4 and defeat[0].confidence==.97
    assert len(result.games)==1 and result.games[0].result.value=='LOSS'
