"""Conservative routing. Ambiguous gaps are searched; similarity never proves identity."""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from enum import Enum
import math
from typing import Any, Iterable

from .fingerprint import GameFingerprint, assess_continuity


class AdaptiveState(str, Enum):
    OUT_OF_GAME = "OUT_OF_GAME"
    POSSIBLE_START = "POSSIBLE_START"
    IN_GAME = "IN_GAME"
    POSSIBLE_END = "POSSIBLE_END"
    UNKNOWN = "UNKNOWN_RECOVERY"


class EvidenceState(str, Enum):
    STATE_UNKNOWN = "STATE_UNKNOWN"
    IDENTITY_UNCERTAIN = "IDENTITY_UNCERTAIN"
    TRANSITION_SUSPECTED = "TRANSITION_SUSPECTED"
    CONTINUITY_PLAUSIBLE = "CONTINUITY_PLAUSIBLE"


@dataclass(frozen=True)
class AdaptiveSettings:
    coarse_step_s: float = 5.0
    recovery_period_s: float = 20.0
    recovery_width_s: float = 6.0
    overlap_s: float = 2.0
    continuity_floor: float = .90
    full_fallback_fraction: float = .65
    force_full_fallback: bool = False

    def __post_init__(self) -> None:
        for key, value in asdict(self).items():
            if key == "force_full_fallback":
                if type(value) is not bool:
                    raise ValueError("force_full_fallback must be boolean")
            elif isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError("Adaptive settings must be finite numbers")
        if self.coarse_step_s not in (5, 10, 20, 30):
            raise ValueError("Adaptive coarse step must be 5, 10, 20 or 30 seconds")
        if not 5 <= self.recovery_period_s <= 60 or not 2 <= self.recovery_width_s <= self.recovery_period_s:
            raise ValueError("Invalid periodic recovery coverage")
        if not 1 <= self.overlap_s <= 10 or not 0 < self.continuity_floor <= 1:
            raise ValueError("Invalid continuity/overlap settings")
        if not 0 < self.full_fallback_fraction <= 1:
            raise ValueError("Invalid fallback fraction")


@dataclass
class RoutingObservation:
    fingerprint: GameFingerprint
    weak_start: bool = False
    weak_result: bool = False


@dataclass(frozen=True)
class RecoveryInterval:
    start: float
    end: float
    reasons: tuple[str, ...]


def merge_intervals(intervals: Iterable[RecoveryInterval]) -> list[RecoveryInterval]:
    merged: list[RecoveryInterval] = []
    for item in sorted(intervals, key=lambda w: w.start):
        if item.start < 0 or item.end <= item.start or not all(math.isfinite(t) for t in (item.start, item.end)):
            raise ValueError("Invalid recovery interval")
        if merged and item.start <= merged[-1].end:
            old = merged.pop()
            merged.append(RecoveryInterval(old.start, max(old.end, item.end), tuple(sorted(set(old.reasons + item.reasons)))))
        else:
            merged.append(item)
    return merged


class AdaptivePlanner:
    def __init__(self, settings: AdaptiveSettings, *, board_high: float = .38, board_low: float = .20) -> None:
        self.settings, self.board_high, self.board_low = settings, board_high, board_low
        self.states: list[dict[str, Any]] = []
        self.requests: list[RecoveryInterval] = []
        self.previous: RoutingObservation | None = None
        self.board_present: bool | None = None
        self.duration = 0.0

    def _add(self, start: float, end: float, reasons: tuple[str, ...]) -> None:
        lo, hi = max(0.0, math.floor(start)), min(self.duration, math.ceil(end))
        if hi > lo:
            self.requests.append(RecoveryInterval(lo, hi, reasons))

    def begin(self, duration: float) -> None:
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("Invalid duration")
        self.duration = duration
        self.states, self.requests, self.previous, self.board_present = [], [], None, None
        self._add(0, self.settings.coarse_step_s, ("leading_recovery",))
        self._add(duration-self.settings.coarse_step_s, duration, ("trailing_recovery",))
        t = self.settings.recovery_period_s
        while t < duration:
            self._add(t-self.settings.recovery_width_s/2, t+self.settings.recovery_width_s/2, ("periodic_recovery",))
            t += self.settings.recovery_period_s

    def observe(self, row: RoutingObservation) -> None:
        fp, previous = row.fingerprint, self.previous
        if (not math.isfinite(fp.timestamp) or not 0 <= fp.timestamp < self.duration
                or not math.isfinite(fp.board_score) or not 0 <= fp.board_score <= 1):
            raise ValueError("Invalid routing evidence")
        if previous and previous.fingerprint.timestamp >= fp.timestamp:
            raise ValueError("Routing evidence must be chronological and distinct")
        prior_board = self.board_present
        # Preserve the last *observed* definite board state inside the dead band.
        # Identity uncertainty cannot change board presence.
        if fp.board_score >= self.board_high:
            self.board_present = True
        elif fp.board_score <= self.board_low:
            self.board_present = False
        assessment = assess_continuity(previous.fingerprint, fp, similarity_floor=self.settings.continuity_floor) if previous else None
        category = (EvidenceState.STATE_UNKNOWN.value if self.board_present is None else
                    assessment.category if self.board_present and assessment else
                    EvidenceState.IDENTITY_UNCERTAIN.value if self.board_present else
                    EvidenceState.CONTINUITY_PLAUSIBLE.value)
        reasons: list[str] = []
        if self.board_present is None:
            reasons.append("uncertain_board")
        elif self.board_present and assessment and assessment.category != EvidenceState.CONTINUITY_PLAUSIBLE.value:
            reasons.extend("changed_"+k for k in assessment.changed)
            if assessment.category == EvidenceState.IDENTITY_UNCERTAIN.value:
                reasons.append("identity_uncertain")
        if previous and prior_board is not None and self.board_present is not None and prior_board != self.board_present:
            reasons.append("board_transition")
            category = EvidenceState.TRANSITION_SUSPECTED.value
        # A reliable mana reset remains a suspicion only.
        if previous:
            old = previous.fingerprint
            if (old.maximum_mana is not None and fp.maximum_mana is not None
                    and min(old.mana_confidence, fp.mana_confidence) >= .9
                    and old.maximum_mana >= 6 and fp.maximum_mana <= 3 and old.maximum_mana-fp.maximum_mana >= 4):
                reasons.append("possible_mana_reset")
                category = EvidenceState.TRANSITION_SUSPECTED.value
            if fp.timestamp-old.timestamp > self.settings.coarse_step_s*1.5:
                reasons.append("sampling_gap")
                category = EvidenceState.STATE_UNKNOWN.value
        if row.weak_start:
            reasons.append("weak_start")
            category = EvidenceState.TRANSITION_SUSPECTED.value
        if row.weak_result and (previous is None or not previous.weak_result):
            reasons.append("weak_result")
            category = EvidenceState.TRANSITION_SUSPECTED.value
        if reasons:
            start = previous.fingerprint.timestamp if previous else 0
            self._add(start-self.settings.overlap_s, fp.timestamp+self.settings.overlap_s, tuple(sorted(set(reasons))))
        state = (AdaptiveState.POSSIBLE_END if row.weak_result else AdaptiveState.POSSIBLE_START if row.weak_start else
                 AdaptiveState.UNKNOWN if category == EvidenceState.STATE_UNKNOWN.value else
                 AdaptiveState.IN_GAME if self.board_present else AdaptiveState.OUT_OF_GAME)
        self.states.append({"timestamp": fp.timestamp, "state": state.value, "evidence_state": category,
            "board_present": self.board_present, "board_score": fp.board_score,
            "continuity": min(assessment.similarities.values(), default=0.0) if assessment else 0.0,
            "similarities": assessment.similarities if assessment else {},
            "stable_regions": list(assessment.stable) if assessment else [],
            "changed_regions": list(assessment.changed) if assessment else [],
            "missing_regions": list(assessment.missing) if assessment else [],
            "reasons": sorted(set(reasons))})
        self.previous = row

    def plan(self, rows: list[RoutingObservation], duration: float, *, promote_full: bool = True) -> list[RecoveryInterval]:
        self.begin(duration)
        for row in rows:
            self.observe(row)
        merged = merge_intervals(self.requests)
        coverage = sum(w.end-w.start for w in merged)/duration
        if not rows or self.settings.force_full_fallback or promote_full and coverage >= self.settings.full_fallback_fraction:
            return [RecoveryInterval(0, duration, ("full_fallback",))]
        return merged

    def diagnostics(self) -> dict[str, Any]:
        # Per-reason coverage overlaps; never add it as exclusive elapsed time.
        reasons = sorted({r for w in self.requests for r in w.reasons})
        return {
            "state_counts": dict(Counter(s["evidence_state"] for s in self.states)),
            "reason_counts": dict(Counter(r for s in self.states for r in s["reasons"])),
            "reason_source_seconds_overlapping": {reason: sum(w.end-w.start for w in merge_intervals(
                RecoveryInterval(w.start,w.end,(reason,)) for w in self.requests if reason in w.reasons)) for reason in reasons},
            "requested_intervals": len(self.requests),
            "merged_intervals": len(merge_intervals(self.requests)),
        }
