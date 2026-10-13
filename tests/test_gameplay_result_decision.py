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


# --- Single-frame confirmation (opt-in, used by Adaptive V2) ---------------------------

SINGLE = Path(__file__).parent / 'fixtures' / 'gameplay' / 'single_frame_result_traces.json'


def _single_frame_case(name: str) -> tuple[ResultEvidence, list[ResultEvidence]]:
    case = json.loads(SINGLE.read_text(encoding='utf-8'))['cases'][name]
    frames = [ResultEvidence(t, v, d) for t, v, d in case['frames']]
    return max(frames, key=lambda f: f.score), frames


def test_real_isolated_defeat_banner_is_confirmed_from_its_60fps_neighbourhood():
    # THEORYCRAFTING game 10: "Sconfitta!" above the floor in one PTS only.
    from analysis.gameplay.hearthstone.result_decision import NO_RESULT, SingleFrameConfirmation
    peak, frames = _single_frame_case('theorycrafting_g10_isolated_defeat')
    decision = decide_results([f for f in frames if f.score >= POLICY.evidence_score], POLICY)
    assert [(d.outcome, d.reason) for d in decision] == [(NO_RESULT, 'insufficient_support')]  # ordinary policy unchanged
    confirmed = SingleFrameConfirmation().confirm(decision[0], frames)
    assert confirmed.outcome == 'DEFEAT' and confirmed.reason == 'single_frame_local_peak'
    event = confirmed.to_event()
    assert event is not None and event.type == GameEventType.DEFEAT and event.timestamp == peak.timestamp


def test_trailing_echo_frames_are_real_banner_frames_or_rejected():
    from analysis.gameplay.hearthstone.result_decision import SingleFrameConfirmation
    rule = SingleFrameConfirmation()
    peak, frames = _single_frame_case('theorycrafting_g3_trailing_defeat_echo')
    assert rule.evaluate(peak, frames) == (None, 'peak_below_gate')
    peak, frames = _single_frame_case('theorycrafting_g17_trailing_defeat_echo')
    assert rule.evaluate(peak, frames)[0] == 'DEFEAT'  # same class as the LOSS it follows; deduplicated


@pytest.mark.parametrize('neighbours,reason', [
    ([(0.95, .1, .52), (1.05, .1, .52)], 'single_frame_local_peak'),
    ([(0.95, .1, .40), (1.05, .1, .45)], 'unsupported_isolated_peak'),   # below every negative seen
    ([(0.95, .1, .55), (1.05, .62, .2)], 'conflicting_neighbour'),
    ([(0.95, .1, .91), (1.05, .1, .55)], 'not_strict_local_peak'),
    ([(0.95, .1, .55)], 'incomplete_neighbourhood'),
])
def test_single_frame_rule_requires_coherent_same_class_neighbourhood(neighbours, reason):
    from analysis.gameplay.hearthstone.result_decision import SingleFrameConfirmation
    peak = ResultEvidence(1.0, .30, .90)
    winner, why = SingleFrameConfirmation().evaluate(peak, [peak, *(ResultEvidence(*n) for n in neighbours)])
    assert why == reason and (winner == 'DEFEAT') == (reason == 'single_frame_local_peak')


@pytest.mark.parametrize('peak,reason', [((.30, .84), 'peak_below_gate'), ((.55, .90), 'ambiguous_peak'),
                                         ((.60, .60), 'peak_below_gate')])
def test_single_frame_peak_must_be_strong_and_unambiguous(peak, reason):
    from analysis.gameplay.hearthstone.result_decision import SingleFrameConfirmation
    centre = ResultEvidence(1.0, *peak)
    around = [ResultEvidence(.95, .1, .6), ResultEvidence(1.05, .1, .6)]
    assert SingleFrameConfirmation().evaluate(centre, [centre, *around]) == (None, reason)


def test_single_frame_rule_only_upgrades_one_pts_no_result():
    from analysis.gameplay.hearthstone.result_decision import SingleFrameConfirmation
    strong = decide_results([ResultEvidence(1 + i / 60, .2, .95) for i in range(6)], POLICY)[0]
    assert SingleFrameConfirmation().confirm(strong, []) is strong


def test_every_calibration_frame_as_isolated_peak_never_gives_a_false_result():
    """Stress: treat EACH stored 74/86 frame as a lone candidate with its real 60 FPS neighbours.

    Hard negatives must never confirm; result traces may confirm only their own class.
    """
    from analysis.gameplay.hearthstone.result_decision import SingleFrameConfirmation
    rule = SingleFrameConfirmation()
    raw = json.loads(TRACES.read_text(encoding='utf-8'))
    confirmed = {'NO_RESULT': 0, 'right': 0}
    for item in raw['traces']:
        frames = [ResultEvidence(t, v, d) for t, v, d in item['frames']]
        for candidate in frames:
            winner, _ = rule.evaluate(candidate, frames)
            if winner is None:
                continue
            assert item['truth'] != 'NO_RESULT', item['group']
            assert winner == item['truth'], (item['group'], candidate)
            confirmed['right'] += 1
    assert confirmed['right'] > 0


def test_invalid_single_frame_rule_is_rejected():
    from analysis.gameplay.hearthstone.result_decision import SingleFrameConfirmation
    for change in ({'peak_score': .5, 'negative_ceiling': .6}, {'neighbourhood_s': 0}, {'peak_score': float('nan')}):
        with pytest.raises(ValueError):
            SingleFrameConfirmation(**change)
