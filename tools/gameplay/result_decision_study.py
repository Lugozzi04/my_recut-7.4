"""Offline result-decision study on existing 74/86 calibration artifacts only.

No video is decoded and 73.mp4 artifacts are never read. The implemented
policy (analysis.gameplay.hearthstone.result_decision) is replayed on stored
grayscale class scores:

* reviewed native 60 FPS result traces and hard negatives
  (final_representation_study_reviewed.json, label override already applied);
* 1 FPS monitor scores across the full 86/74 VODs (new_*_60/gameplay_analysis.json),
  split into in-game / pre-game menus / post-result phases with 86 ground truth
  and, for 74, the visually verified candidate games.

Usage: python tools/gameplay/result_decision_study.py [--fixture PATH]
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from analysis.gameplay.hearthstone.result_decision import (  # noqa: E402
    ResultDecisionPolicy, ResultEvidence, cluster_evidence, decide_cluster, decide_results)

VALIDATION = ROOT / 'docs' / 'gameplay_validation'
CALIBRATION = VALIDATION / 'state_aware' / 'calibration'
OUTPUT = VALIDATION / 'state_aware' / 'result_decision' / 'calibration_study.json'
FIXTURE = ROOT / 'tests' / 'fixtures' / 'gameplay' / 'result_decision_calibration_traces.json'
TRUTH_74 = {'74_window_02': 'VICTORY', '74_window_04': 'VICTORY', '74_window_06': 'VICTORY',
            '74_window_08': 'DEFEAT', '74_window_09': 'DEFEAT', '74_window_10': 'DEFEAT'}
TEMPLATE_SOURCE_GROUPS = {'74_window_02', '74_window_04', '74_window_08', '74_window_09'}


def quantiles(values: list[float]) -> dict[str, float | int]:
    ordered = sorted(values)
    if not ordered:
        return {'count': 0}
    pick = lambda p: ordered[min(len(ordered) - 1, int(p * (len(ordered) - 1) + 0.5))]  # noqa: E731
    return {'count': len(ordered), 'min': ordered[0], 'p10': pick(.1), 'median': pick(.5),
            'p90': pick(.9), 'p99': pick(.99), 'max': ordered[-1]}


def evidence(timestamp: float, scores: dict[str, float]) -> ResultEvidence:
    return ResultEvidence(timestamp, scores['VICTORY'], scores['DEFEAT'])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', type=Path, default=FIXTURE)
    arguments = parser.parse_args()
    policy = ResultDecisionPolicy()
    study = json.loads((CALIBRATION / 'final_representation_study_reviewed.json').read_text(encoding='utf-8'))
    rows = [row for row in study['rows'] if row['method'] == 'gray' and row['variant'] == 'native']
    truth_86 = json.loads((VALIDATION / '86' / 'ground_truth_manual.json').read_text(encoding='utf-8'))
    truth = {game['id']: {'WIN': 'VICTORY', 'LOSS': 'DEFEAT'}[game['result']] for game in truth_86['games']}
    truth.update(TRUTH_74)
    best = lambda row: max(row['scores'].values())  # noqa: E731

    # Frame level: reviewed labels only.
    labels: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        labels[row['label']].append(best(row))
    negatives = [row for row in rows if row['label'] == 'NEGATIVE']
    transitional = [row for row in rows if row['label'] in ('VICTORY', 'DEFEAT') and row['winner'] != row['label']]
    frame_level = {
        'best_score_by_label': {label: quantiles(values) for label, values in sorted(labels.items()) if label != 'UNREVIEWED'},
        'negative_max_margin': max(abs(r['scores']['VICTORY'] - r['scores']['DEFEAT']) for r in negatives),
        'hardest_negatives': [{'group': r['group'], 'timestamp': r['timestamp'], 'scores': r['scores']}
                              for r in sorted(negatives, key=best, reverse=True)[:5]],
        'transitional_wrong_class_frames': len(transitional),
        'transitional_wrong_class_max_best': max(map(best, transitional)),
        'hardest_transitional': [{'group': r['group'], 'timestamp': r['timestamp'], 'label': r['label'], 'scores': r['scores']}
                                 for r in sorted(transitional, key=best, reverse=True)[:5]],
        'per_frame_argmax_correct_on_labeled_results': sum(r['winner'] == r['label'] for r in rows if r['label'] in ('VICTORY', 'DEFEAT')),
        'labeled_result_frames': sum(r['label'] in ('VICTORY', 'DEFEAT') for r in rows),
    }

    # Event level: replay the implemented policy on every result trace (incl. UNREVIEWED phases).
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[row['group']].append(row)
    events: list[dict[str, Any]] = []
    fixture_traces: list[dict[str, Any]] = []
    for name in sorted(truth):
        trace = sorted(groups[name], key=lambda r: r['timestamp'])
        decisions = decide_results((evidence(r['timestamp'], r['scores']) for r in trace), policy)
        classified = [d for d in decisions if d.classified]
        events.append({'group': name, 'truth': truth[name], 'independent_of_templates': name not in TEMPLATE_SOURCE_GROUPS,
                       'decisions': [d.to_mapping() for d in decisions],
                       'correct': bool(classified) and all(d.outcome == truth[name] for d in classified),
                       'wrong_winner_frames_in_trace': sum(r['winner'] != truth[name] for r in trace),
                       'wrong_winner_frames_at_or_above_floor': sum(r['winner'] != truth[name] and best(r) >= policy.evidence_score for r in trace)})
        fixture_traces.append({'group': name, 'truth': truth[name],
                               'frames': [[round(r['timestamp'], 6), round(r['scores']['VICTORY'], 4), round(r['scores']['DEFEAT'], 4)] for r in trace]})
    peaks = [d['peak'] for e in events for d in e['decisions'] if d['outcome'] in ('VICTORY', 'DEFEAT')]
    supports = [d['supporting_frames'] for e in events for d in e['decisions'] if d['outcome'] in ('VICTORY', 'DEFEAT')]
    negative_groups = sorted({r['group'] for r in negatives})
    negative_decisions: list[dict[str, Any]] = []
    for name in negative_groups:
        trace = [r for r in groups[name] if r['label'] == 'NEGATIVE']
        negative_decisions.extend(d.to_mapping() for d in decide_results((evidence(r['timestamp'], r['scores']) for r in trace), policy))
        fixture_traces.append({'group': name, 'truth': 'NO_RESULT',
                               'frames': [[round(r['timestamp'], 6), round(r['scores']['VICTORY'], 4), round(r['scores']['DEFEAT'], 4)] for r in trace]})

    # Full-VOD 1 FPS monitor scores: what non-result content looks like at scale.
    monitor: dict[str, Any] = {}
    for video in ('86', '74'):
        analysis = json.loads((CALIBRATION / f'new_{video}_60' / 'gameplay_analysis.json').read_text(encoding='utf-8'))
        rows_1fps = analysis['metadata']['monitor_evidence']
        if video == '86':
            games = [(g['start'], g['result_time']) for g in truth_86['games']]
            basis = 'manual ground truth (complete)'
        else:
            games = [(g['start'], g['end']) for g in analysis['games'] if g['start'] is not None and g['end'] is not None]
            basis = 'visually verified candidate games (sparse annotations; not complete ground truth)'
        phases: dict[str, list[dict[str, Any]]] = {'in_game': [], 'before_first_game': [], 'post_result_until_next_vs': []}
        for row in rows_1fps:
            t = row['timestamp']
            if t < games[0][0] - 1:
                phases['before_first_game'].append(row)
            elif any(start + 1 <= t <= end - 1 for start, end in games):
                phases['in_game'].append(row)
            elif any(end - 1 < t < (games[i + 1][0] if i + 1 < len(games) else float('inf')) for i, (_, end) in enumerate(games)):
                phases['post_result_until_next_vs'].append(row)
        top = lambda row: max(row['result_scores'].values())  # noqa: E731
        monitor[video] = {'basis': basis, 'frames': len(rows_1fps), **{
            phase: {'best_score': quantiles([top(r) for r in values]),
                    'at_or_above_evidence_floor': sum(top(r) >= policy.evidence_score for r in values),
                    'max_margin': max((r['result_margin'] for r in values), default=0.0)}
            for phase, values in phases.items()}}
        # Post-result frames: does a wrong class ever win with strong evidence?
        wrong: list[dict[str, Any]] = []
        for index, (_, end) in enumerate(games):
            if video != '86':
                break
            next_start = games[index + 1][0] if index + 1 < len(games) else float('inf')
            expected = {'WIN': 'VICTORY', 'LOSS': 'DEFEAT'}[truth_86['games'][index]['result']]
            wrong.extend(r for r in rows_1fps if end - 1 < r['timestamp'] < next_start and top(r) >= 0.5
                         and max(r['result_scores'], key=r['result_scores'].get) != expected)
        if video == '86':
            monitor[video]['post_result_wrong_class_frames_best_ge_0_5'] = len(wrong)
            monitor[video]['post_result_frames_between_floor_and_old_gate'] = sum(
                policy.evidence_score <= top(r) < 0.85 for r in phases['post_result_until_next_vs'])

    report: dict[str, Any] = {
        'format': 'recut_result_decision_calibration_study', 'version': 1, 'role': 'calibration',
        'videos': ['74.mp4', '86.mp4'], 'excluded': '73.mp4 (frozen validation) is never read',
        'policy': policy.to_mapping(),
        'parameter_provenance': {
            'evidence_score': 'existing monitor weak_score (0.65), frozen in iteration 5 before the 73 blind run',
            'minimum_observations': 'existing ordinary support: 2 distinct source PTS',
            'minimum_span_s': 'existing 1/120 s span',
            'cluster_gap_s': 'existing result_gap_s 0.5 s',
            'minimum_peak_margin': 'existing profile ambiguity margin 0.04',
        },
        'frame_level': frame_level,
        'event_level': {
            'result_traces': len(events), 'correct': sum(e['correct'] for e in events),
            'independent_of_templates': sum(e['independent_of_templates'] for e in events),
            'classified_clusters': len(peaks),
            'peak_score': quantiles([p['scores'][p['winner']] for p in peaks]),
            'peak_margin': quantiles([p['margin'] for p in peaks]),
            'supporting_frames': quantiles([float(s) for s in supports]),
            'events': events,
        },
        'hard_negative_groups': {'groups': len(negative_groups), 'frames': len(negatives),
                                 'confirmed_result_screens': sum(d['result_screen_confirmed'] for d in negative_decisions),
                                 'decisions': negative_decisions},
        'monitor_1fps': monitor,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, indent=2), encoding='utf-8')
    arguments.fixture.write_text(json.dumps({
        'format': 'recut_result_decision_traces', 'version': 1, 'role': 'calibration',
        'source': 'docs/gameplay_validation/state_aware/calibration/final_representation_study_reviewed.json (gray, native)',
        'columns': ['timestamp', 'VICTORY', 'DEFEAT'], 'traces': fixture_traces}, separators=(',', ':')), encoding='utf-8')
    print(json.dumps({k: report['event_level'][k] for k in ('result_traces', 'correct', 'classified_clusters', 'peak_score', 'peak_margin')}, indent=1))
    print('hard negatives confirmed:', report['hard_negative_groups']['confirmed_result_screens'])
    print(json.dumps(monitor, indent=1))
    print(json.dumps({k: v for k, v in frame_level.items() if not k.startswith('hardest')}, indent=1))
    return 0 if report['event_level']['correct'] == len(events) else 1


if __name__ == '__main__':
    raise SystemExit(main())
