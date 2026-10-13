"""Event-level result decision: confirm a result screen, then classify its peak.

Two questions are answered separately on frames measured inside the local
high-rate windows:

1. RESULT_SCREEN confirmation (class-agnostic): at least ``minimum_observations``
   distinct source frames whose best class score reaches ``evidence_score``,
   chained in time by gaps of at most ``cluster_gap_s``.
2. WIN/LOSS classification: argmax(VICTORY, DEFEAT) of the cluster's peak frame
   (highest best-class score), never of arbitrary single frames. A peak whose
   class margin is below ``minimum_peak_margin``, or a cluster where the
   opposite class has its own confirmed support, stays UNKNOWN.

Defaults reuse existing calibration parameters (monitor weak score, ordinary
two-PTS support, result gap, class ambiguity margin); see
docs/gameplay_validation/state_aware/result_decision/README.md. Scores are
correlations, not probabilities. No game boundaries are inferred here.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
import math
from typing import Any

from analysis.gameplay.models import GameEvent, GameEventType

POLICY_VERSION = 'result-decision-v1'
CLASSES = ('VICTORY', 'DEFEAT')
NO_RESULT = 'NO_RESULT'
UNKNOWN = 'UNKNOWN'


def _number(value: Any, name: str, lower: float, upper: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not lower <= value <= upper:
        raise ValueError(f'{name} must be finite and within {lower}..{upper}')
    return float(value)


@dataclass(frozen=True)
class ResultDecisionPolicy:
    evidence_score: float = 0.65
    minimum_observations: int = 2
    minimum_span_s: float = 1.0 / 120
    cluster_gap_s: float = 0.5
    minimum_peak_margin: float = 0.04

    def __post_init__(self) -> None:
        _number(self.evidence_score, 'evidence_score', 1e-9, 1.0)
        _number(self.minimum_span_s, 'minimum_span_s', 0.0, 1.0)
        _number(self.cluster_gap_s, 'cluster_gap_s', 1e-9, 5.0)
        _number(self.minimum_peak_margin, 'minimum_peak_margin', 1e-9, 1.0)
        if type(self.minimum_observations) is not int or not 2 <= self.minimum_observations <= 30:
            raise ValueError('minimum_observations must be an integer in 2..30; single frames are never confirmed')

    def to_mapping(self) -> dict[str, Any]:
        return {'version': POLICY_VERSION, **asdict(self)}


@dataclass(frozen=True)
class ResultEvidence:
    """Both class scores of one measured source frame (PTS)."""

    timestamp: float
    victory: float
    defeat: float
    templates: Mapping[str, Any] = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        _number(self.timestamp, 'timestamp', 0.0, math.inf)
        _number(self.victory, 'victory', -1.0, 1.0)
        _number(self.defeat, 'defeat', -1.0, 1.0)

    @classmethod
    def from_scores(cls, timestamp: float, scores: Mapping[str, Any]) -> ResultEvidence:
        return cls(timestamp, scores['scores']['VICTORY'], scores['scores']['DEFEAT'], dict(scores.get('best_templates', {})))

    @property
    def score(self) -> float:
        return max(self.victory, self.defeat)

    @property
    def margin(self) -> float:
        return abs(self.victory - self.defeat)

    @property
    def winner(self) -> str | None:
        if self.victory == self.defeat:
            return None
        return 'VICTORY' if self.victory > self.defeat else 'DEFEAT'

    def to_mapping(self) -> dict[str, Any]:
        return {'timestamp': self.timestamp, 'scores': {'VICTORY': self.victory, 'DEFEAT': self.defeat},
                'winner': self.winner, 'margin': self.margin}


@dataclass(frozen=True)
class ResultDecision:
    outcome: str  # VICTORY, DEFEAT, UNKNOWN (confirmed screen) or NO_RESULT
    reason: str
    frames: tuple[ResultEvidence, ...]
    peak: ResultEvidence
    opposite_support: int = 0

    @property
    def confirmed(self) -> bool:
        return self.outcome != NO_RESULT

    @property
    def classified(self) -> bool:
        return self.outcome in CLASSES

    def to_mapping(self) -> dict[str, Any]:
        return {'outcome': self.outcome, 'reason': self.reason, 'result_screen_confirmed': self.confirmed,
                'first_seen': self.frames[0].timestamp, 'last_seen': self.frames[-1].timestamp,
                'supporting_frames': len(self.frames), 'opposite_support': self.opposite_support,
                'peak': self.peak.to_mapping()}

    def to_event(self, metadata: Mapping[str, Any] | None = None) -> GameEvent | None:
        """One GameEvent per classified cluster; timestamps are measured PTS."""
        if not self.classified:
            return None
        first, last = self.frames[0].timestamp, self.frames[-1].timestamp
        templates = self.peak.templates.get(self.outcome, {})
        return GameEvent(first, GameEventType(self.outcome), max(0.0, self.peak.score), metadata={
            **dict(metadata or {}), **dict(templates), 'raw_similarity': self.peak.score,
            'class_scores': {'VICTORY': self.peak.victory, 'DEFEAT': self.peak.defeat},
            'class_margin': self.peak.margin, 'observation_count': len(self.frames),
            'first_seen': first, 'last_seen': last, 'peak_timestamp': self.peak.timestamp,
            'temporal_span_s': last - first, 'result_acceptance': self.reason,
            'opposite_support': self.opposite_support, 'decision_policy': POLICY_VERSION,
        })


def cluster_evidence(evidence: Iterable[ResultEvidence], policy: ResultDecisionPolicy) -> list[tuple[ResultEvidence, ...]]:
    """Class-agnostic temporal clusters of frames at or above the evidence floor.

    Replays of one PTS count once (highest score kept), so overlapping windows
    never fabricate support.
    """
    by_pts: dict[float, ResultEvidence] = {}
    for item in evidence:
        if item.score < policy.evidence_score:
            continue
        key = round(item.timestamp, 6)
        if key not in by_pts or item.score > by_pts[key].score:
            by_pts[key] = item
    clusters: list[list[ResultEvidence]] = []
    for item in sorted(by_pts.values(), key=lambda value: value.timestamp):
        if clusters and item.timestamp - clusters[-1][-1].timestamp <= policy.cluster_gap_s + 1e-9:
            clusters[-1].append(item)
        else:
            clusters.append([item])
    return [tuple(cluster) for cluster in clusters]


def decide_cluster(frames: tuple[ResultEvidence, ...], policy: ResultDecisionPolicy) -> ResultDecision:
    if not frames:
        raise ValueError('A result cluster needs at least one frame')
    peak = max(frames, key=lambda item: (item.score, -item.timestamp))
    span = frames[-1].timestamp - frames[0].timestamp
    if len(frames) < policy.minimum_observations or span + 1e-9 < policy.minimum_span_s:
        return ResultDecision(NO_RESULT, 'insufficient_support', frames, peak)
    winner = peak.winner
    if winner is None or peak.margin < policy.minimum_peak_margin:
        return ResultDecision(UNKNOWN, 'ambiguous_peak', frames, peak)
    opposite = sum(item.winner not in (None, winner) and item.margin >= policy.minimum_peak_margin for item in frames)
    if opposite >= policy.minimum_observations:
        return ResultDecision(UNKNOWN, 'conflicting_support', frames, peak, opposite)
    return ResultDecision(winner, 'event_peak_argmax', frames, peak, opposite)


def decide_results(evidence: Iterable[ResultEvidence], policy: ResultDecisionPolicy) -> list[ResultDecision]:
    return [decide_cluster(cluster, policy) for cluster in cluster_evidence(evidence, policy)]


@dataclass(frozen=True)
class SingleFrameConfirmation:
    """Opt-in confirmation of a result screen seen in ONE frame of the 60 FPS windows.

    Some result banners are on screen for a few frames only (animation, scene
    switch), so the ordinary two-PTS support never forms. A single PTS is
    confirmed only by its 60 FPS neighbourhood, re-measured around it:

    - the peak reaches ``peak_score`` and is the strict local maximum;
    - the opposite class at the peak, and every opposite-class neighbour, stay
      at or below ``negative_ceiling``;
    - at least one distinct neighbour of the same class exceeds ``negative_ceiling``.

    Defaults are existing calibration numbers, not new tuning:
    peak_score 0.85 = iteration-5 per-frame acceptance; negative_ceiling 0.511 =
    highest score of any reviewed hard negative (0.478), game/menu frame (0.437)
    or wrong-class result frame (0.511) in result_decision/calibration_study.json.
    """

    peak_score: float = 0.85
    negative_ceiling: float = 0.511
    neighbourhood_s: float = 0.25

    def __post_init__(self) -> None:
        _number(self.peak_score, 'peak_score', 1e-9, 1.0)
        _number(self.negative_ceiling, 'negative_ceiling', 1e-9, 1.0)
        _number(self.neighbourhood_s, 'neighbourhood_s', 1.0 / 120, 1.0)
        if self.negative_ceiling >= self.peak_score:
            raise ValueError('negative_ceiling must be below peak_score')

    def to_mapping(self) -> dict[str, Any]:
        return asdict(self)

    def evaluate(self, peak: ResultEvidence, neighbourhood: Iterable[ResultEvidence]) -> tuple[str | None, str]:
        """Return (VICTORY/DEFEAT, reason) when confirmed, else (None, rejection reason)."""
        winner = peak.winner
        if winner is None or peak.score < self.peak_score:
            return None, 'peak_below_gate'
        if min(peak.victory, peak.defeat) > self.negative_ceiling:
            return None, 'ambiguous_peak'
        others = [item for item in neighbourhood if abs(item.timestamp - peak.timestamp) > 1e-6
                  and abs(item.timestamp - peak.timestamp) <= self.neighbourhood_s + 1e-9]
        before = [item for item in others if item.timestamp < peak.timestamp]
        after = [item for item in others if item.timestamp > peak.timestamp]
        if not before or not after:
            return None, 'incomplete_neighbourhood'
        if any(item.score >= peak.score for item in others):
            return None, 'not_strict_local_peak'
        if any(item.winner not in (None, winner) and item.score > self.negative_ceiling for item in others):
            return None, 'conflicting_neighbour'
        if not any(item.winner == winner and item.score > self.negative_ceiling for item in others):
            return None, 'unsupported_isolated_peak'
        return winner, 'single_frame_local_peak'

    def confirm(self, decision: ResultDecision, neighbourhood: Iterable[ResultEvidence]) -> ResultDecision:
        """Upgrade a one-PTS NO_RESULT decision; anything else is returned unchanged."""
        if decision.outcome != NO_RESULT or decision.reason != 'insufficient_support' or len(decision.frames) != 1:
            return decision
        winner, reason = self.evaluate(decision.peak, neighbourhood)
        if winner is None:
            return ResultDecision(NO_RESULT, 'single_frame_' + reason, decision.frames, decision.peak)
        return ResultDecision(winner, reason, decision.frames, decision.peak)
