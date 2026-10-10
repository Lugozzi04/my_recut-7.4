"""Opt-in exception for brief result anchors; never infers an editorial cut.

No acceptance thresholds are supplied by default. Values must be frozen using
calibration-only positives AND reviewed negatives for the exact template pack.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import math
from typing import Any

from analysis.gameplay.models import DetectionSource, GameEvent, GameEventType

RESULT_TYPES = (GameEventType.VICTORY, GameEventType.DEFEAT)


def _number(value: Any, name: str, *, lower: float = 0.0, upper: float = 1.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not lower <= value <= upper:
        raise ValueError(f'{name} must be finite and within {lower}..{upper}')
    return float(value)


@dataclass(frozen=True)
class SingleFrameRule:
    peak_similarity: float
    minimum_class_margin: float
    neighbor_similarity: float
    maximum_neighbor_gap_s: float

    def __post_init__(self) -> None:
        _number(self.peak_similarity, 'peak_similarity', lower=1e-9)
        _number(self.minimum_class_margin, 'minimum_class_margin', lower=1e-9, upper=2.0)
        _number(self.neighbor_similarity, 'neighbor_similarity', lower=1e-9)
        _number(self.maximum_neighbor_gap_s, 'maximum_neighbor_gap_s', lower=1e-9, upper=1.0)
        if self.neighbor_similarity >= self.peak_similarity:
            raise ValueError('neighbor_similarity must be below peak_similarity')


@dataclass(frozen=True)
class SingleFrameResultPolicy:
    victory: SingleFrameRule | None = None
    defeat: SingleFrameRule | None = None
    pack_fingerprint: str = ''
    calibration_id: str = ''

    def __post_init__(self) -> None:
        if any(rule is not None and not isinstance(rule, SingleFrameRule) for rule in (self.victory, self.defeat)):
            raise ValueError('Result rules must be SingleFrameRule or None')
        if self.enabled and (not isinstance(self.pack_fingerprint, str) or len(self.pack_fingerprint) != 64
                             or any(c not in '0123456789abcdef' for c in self.pack_fingerprint)
                             or not isinstance(self.calibration_id, str) or not self.calibration_id.strip()):
            raise ValueError('Enabled policy requires a template pack fingerprint and calibration_id')

    @property
    def enabled(self) -> bool:
        return self.victory is not None or self.defeat is not None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> SingleFrameResultPolicy:
        if (not isinstance(raw, Mapping) or raw.get('format') != 'recut_single_frame_result_policy' or type(raw.get('version')) is not int
                or raw.get('version') != 1 or raw.get('role') != 'calibration'
                or set(raw) - {'format', 'version', 'role', 'pack_fingerprint', 'calibration_id', 'VICTORY', 'DEFEAT'}):
            raise ValueError('Expected calibration single-frame result policy version 1')
        rules = []
        for kind in RESULT_TYPES:
            value = raw.get(kind.value)
            if value is not None and not isinstance(value, dict):
                raise ValueError(f'{kind.value} rule must be an object or null')
            try:
                rules.append(SingleFrameRule(**value) if value is not None else None)
            except TypeError as exc:
                raise ValueError(f'Invalid {kind.value} rule fields') from exc
        return cls(rules[0], rules[1], raw.get('pack_fingerprint', ''), raw.get('calibration_id', ''))

    def validate_detector(self, description: Mapping[str, Any]) -> None:
        if not self.enabled:
            return
        if description.get('pack_fingerprint') != self.pack_fingerprint:
            raise ValueError('Single-frame policy was calibrated for another template pack')
        classes = description.get('classes', {})
        for kind, rule in zip(RESULT_TYPES, (self.victory, self.defeat)):
            if rule is None:
                continue
            threshold = classes.get(kind.value, {}).get('threshold')
            ordinary = _number(threshold, f'{kind.value} ordinary threshold', lower=1e-9)
            if rule.peak_similarity < ordinary or rule.minimum_class_margin < description.get('ambiguity_margin', 0):
                raise ValueError('Single-frame policy cannot relax ordinary similarity or ambiguity thresholds')

    def evaluate(self, event: GameEvent, context: Sequence[Mapping[str, Any]]) -> tuple[bool, str]:
        """Use distinct measured native frames, including subthreshold neighbors.

        A high correlation alone is insufficient: require both competing class
        scores, a complete local peak (frames on BOTH sides), and at least one
        weaker, same-class winning neighbor above the calibrated support gate.
        Replay of the same PTS does not constitute neighboring evidence.
        """
        if event.type not in RESULT_TYPES:
            return False, 'not_a_result'
        rule = self.victory if event.type == GameEventType.VICTORY else self.defeat
        if rule is None:
            return False, 'disabled'
        if event.source != DetectionSource.TEMPLATE_MATCHING or event.metadata.get('observation_count', 1) != 1:
            return False, 'not_one_template_observation'
        if event.confidence < rule.peak_similarity:
            return False, 'peak_below_gate'
        timestamp = float(event.metadata.get('peak_timestamp', event.timestamp))
        frames: dict[float, Mapping[str, Any]] = {}
        for row in context:
            t = row.get('timestamp')
            if (isinstance(t, (int, float)) and not isinstance(t, bool) and math.isfinite(t)
                    and abs(t - timestamp) <= rule.maximum_neighbor_gap_s + 1e-9):
                frames[round(float(t), 6)] = row
        peak = frames.get(round(timestamp, 6))
        target = event.type.value
        alternative = 'DEFEAT' if event.type == GameEventType.VICTORY else 'VICTORY'

        def scores(row: Mapping[str, Any] | None) -> tuple[float, float] | None:
            try:
                if row is None:
                    return None
                classes = row['classes']
                return (_number(classes[target]['similarity'], 'similarity', lower=-1),
                        _number(classes[alternative]['similarity'], 'similarity', lower=-1))
            except (KeyError, TypeError, ValueError):
                return None

        peak_scores = scores(peak)
        if peak_scores is None or abs(peak_scores[0] - event.confidence) > 1e-6:
            return False, 'missing_peak_scores'
        if peak_scores[0] - peak_scores[1] < rule.minimum_class_margin:
            return False, 'ambiguous_peak'
        before = [row for t, row in frames.items() if t < round(timestamp, 6)]
        after = [row for t, row in frames.items() if t > round(timestamp, 6)]
        if not before or not after:
            return False, 'incomplete_neighborhood'
        neighbors = [scores(row) for row in before + after]
        if any(pair is None for pair in neighbors):
            return False, 'missing_competing_scores'
        measured = [pair for pair in neighbors if pair is not None]
        if any(pair[0] >= peak_scores[0] for pair in measured):
            return False, 'not_strict_local_peak'
        if not any(pair[0] >= rule.neighbor_similarity and pair[0] > pair[1] for pair in measured):
            return False, 'unsupported_isolated_peak'
        return True, 'calibrated_single_frame_peak'
