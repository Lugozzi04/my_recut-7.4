"""Event-level result decision: confirmation and peak classification are separate."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from analysis.gameplay.hearthstone.result_decision import (
    NO_RESULT, UNKNOWN, ResultDecisionPolicy, ResultEvidence, cluster_evidence, decide_cluster, decide_results)
from analysis.gameplay.models import GameEventType
from analysis.gameplay.result_events import deduplicate_result_events

POLICY = ResultDecisionPolicy()
TRACES = Path(__file__).parent / 'fixtures' / 'gameplay' / 'result_decision_calibration_traces.json'


def trace(*frames, start=100.0, step=1 / 60):
    return [ResultEvidence(start + index * step, v, d) for index, (v, d) in enumerate(frames)]


def only(decisions):
    assert len(decisions) == 1
    return decisions[0]


def test_strong_win_cluster_is_victory():
    decision = only(decide_results(trace((.40, .30), (.91, .39), (.99, .39), (.97, .40), (.80, .35)), POLICY))
    assert decision.outcome == 'VICTORY' and decision.reason == 'event_peak_argmax'
    assert decision.peak.victory == .99 and len(decision.frames) == 4


def test_strong_loss_cluster_is_defeat():
    decision = only(decide_results(trace((.35, .70), (.36, .95), (.35, .88), (.37, .93)), POLICY))
    assert decision.outcome == 'DEFEAT' and decision.peak.defeat == .95


def test_subthreshold_but_well_supported_loss_is_classified():
    # The old absolute 0.85 gate rejected every frame here; the screen is still unmistakable.
    decision = only(decide_results(trace((.30, .51), (.37, .80), (.36, .78), (.33, .60)), POLICY))
    assert decision.outcome == 'DEFEAT' and decision.peak.defeat == .80


def test_transitional_wrong_class_frames_do_not_decide_the_event():
    frames = trace((.2299, .2296), (.51, .50), (.42, .34), (.39, .90), (.66, .40), (.40, .97), (.38, .93))
    assert sum(f.winner == 'VICTORY' for f in frames) == 4  # per-frame argmax would be misleading
    decision = only(decide_results(frames, POLICY))
    assert decision.outcome == 'DEFEAT' and decision.peak.defeat == .97
    assert decision.opposite_support == 1


def test_true_victory_with_early_defeat_leaning_frames():
    decision = only(decide_results(trace((.3408, .4211), (.34, .47), (.88, .40), (.98, .39), (.95, .40)), POLICY))
    assert decision.outcome == 'VICTORY'


@pytest.mark.parametrize('frames', [
    [(.4121, .4780), (.3983, .4190), (.2982, .3964), (.437, .3917)],  # reviewed hard negatives
    [(.10, .60), (.12, .62), (.11, .64)],                             # high margin, low score
    [(.02, .30), (.01, .33)],
])
def test_hard_negatives_and_low_scores_never_confirm_a_result(frames):
    assert decide_results(trace(*frames), POLICY) == []


def test_isolated_single_frame_is_not_a_confirmed_screen():
    decisions = decide_results(trace((.30, .99), start=5) + trace((.30, .99), start=9), POLICY)
    assert len(decisions) == 2
    assert all(d.outcome == NO_RESULT and d.reason == 'insufficient_support' and d.to_event() is None for d in decisions)


def test_replayed_pts_does_not_manufacture_support():
    frames = [ResultEvidence(10, .3, .95), ResultEvidence(10, .3, .90), ResultEvidence(10.0000001, .3, .93)]
    decision = only(decide_results(frames, POLICY))
    assert decision.outcome == NO_RESULT and len(decision.frames) == 1 and decision.peak.defeat == .95


def test_ambiguous_peak_is_unknown():
    decision = only(decide_results(trace((.90, .88), (.91, .89), (.89, .87)), POLICY))
    assert decision.confirmed and decision.outcome == UNKNOWN and decision.reason == 'ambiguous_peak'
    assert decision.to_event() is None


def test_exact_tie_peak_is_unknown():
    decision = only(decide_results(trace((.80, .80), (.70, .70)), POLICY))
    assert decision.outcome == UNKNOWN


def test_conflicting_supported_classes_are_unknown():
    decision = only(decide_results(trace((.95, .40), (.93, .40), (.40, .90), (.40, .92)), POLICY))
    assert decision.outcome == UNKNOWN and decision.reason == 'conflicting_support' and decision.opposite_support == 2


def test_clusters_are_class_agnostic_and_split_on_gaps():
    frames = trace((.3, .9), (.3, .9)) + trace((.9, .3), (.9, .3), start=101)
    clusters = cluster_evidence(frames, POLICY)
    assert [len(c) for c in clusters] == [2, 2]
    assert [decide_cluster(c, POLICY).outcome for c in clusters] == ['DEFEAT', 'VICTORY']


def test_event_carries_peak_evidence_and_measured_span():
    decision = only(decide_results(trace((.36, .80), (.30, .97), (.31, .90)), POLICY))
    event = decision.to_event({'roi': [0, 0, 1, 1]})
    assert event is not None and event.type == GameEventType.DEFEAT and event.confidence == .97
    assert event.timestamp == 100 and event.metadata['peak_timestamp'] == pytest.approx(100 + 1 / 60)
    assert event.metadata['observation_count'] == 3 and event.metadata['class_scores'] == {'VICTORY': .30, 'DEFEAT': .97}
    assert event.metadata['roi'] == [0, 0, 1, 1] and event.metadata['result_acceptance'] == 'event_peak_argmax'


def test_duplicate_anchors_of_one_animation_become_one_event():
    # Banner, brief dip below the floor (> gap), banner again: two clusters, one episode.
    frames = trace((.3, .99), (.3, .97), (.3, .95)) + trace((.3, .90), (.3, .91), start=101.2)
    events = [d.to_event() for d in decide_results(frames, POLICY)]
    assert len(events) == 2 and all(e is not None and e.type == GameEventType.DEFEAT for e in events)
    merged = deduplicate_result_events([e for e in events if e is not None])
    assert len(merged) == 1 and merged[0].metadata['observation_count'] == 5 and merged[0].confidence == .99


@pytest.mark.parametrize('change', [{'evidence_score': 0}, {'evidence_score': 1.5}, {'minimum_observations': 1},
                                    {'minimum_observations': True}, {'cluster_gap_s': 0}, {'minimum_peak_margin': 0},
                                    {'minimum_span_s': float('nan')}])
def test_invalid_policy_is_rejected(change):
    with pytest.raises(ValueError):
        ResultDecisionPolicy(**change)


def test_policy_mapping_is_versioned():
    assert ResultDecisionPolicy().to_mapping() == {
        'version': 'result-decision-v1', 'evidence_score': .65, 'minimum_observations': 2,
        'minimum_span_s': 1 / 120, 'cluster_gap_s': .5, 'minimum_peak_margin': .04}


def test_calibration_traces_74_86_replay_without_errors():
    """Stored 74/86 calibration scores (never 73): every result correct, no negative confirmed."""
    raw = json.loads(TRACES.read_text(encoding='utf-8'))
    assert raw['role'] == 'calibration' and not any('73' in t['group'].split('_')[0] for t in raw['traces'])
    results = negatives = 0
    for item in raw['traces']:
        decisions = decide_results((ResultEvidence(t, v, d) for t, v, d in item['frames']), POLICY)
        if item['truth'] == 'NO_RESULT':
            negatives += 1
            assert not any(decision.confirmed for decision in decisions), item['group']
        else:
            results += 1
            classified = [decision for decision in decisions if decision.classified]
            assert classified and all(d.outcome == item['truth'] for d in classified), item['group']
            assert not any(d.outcome == UNKNOWN for d in decisions), item['group']
    assert results == 20 and negatives == 24
