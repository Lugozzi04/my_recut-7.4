from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable
from copy import deepcopy
from dataclasses import replace

from analysis.gameplay.models import GameEvent, GameEventType, GameResult, GameSegment


def _non_negative(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and non-negative.")
    return float(value)


def _last_seen(event: GameEvent) -> float:
    value = event.metadata.get("last_seen", event.timestamp)
    if not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value):
        return max(float(event.timestamp), float(value))
    return float(event.timestamp)


def _observation_count(event: GameEvent) -> int:
    value = event.metadata.get("observation_count", 1)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 1


def _merge_cluster(cluster: list[GameEvent]) -> GameEvent:
    first = min(cluster, key=lambda event: event.timestamp)
    peak = max(cluster, key=lambda event: event.confidence)
    last = max(_last_seen(event) for event in cluster)
    metadata = deepcopy(peak.metadata)
    metadata.update({
        "observation_count": sum(_observation_count(event) for event in cluster),
        "first_seen": float(first.timestamp),
        "last_seen": last,
        "peak_timestamp": float(peak.metadata.get("peak_timestamp", peak.timestamp)),
        "temporal_span_s": last - float(first.timestamp),
    })
    return GameEvent(float(first.timestamp), first.type, float(peak.confidence), first.source, metadata)


def consolidate_events(events: Iterable[GameEvent], max_gap_s: float = 1.5) -> list[GameEvent]:
    """Debounce same-class observations, retaining measured peak similarity.

    Classes and detection sources are clustered separately so contradictory
    outcomes remain available to the assembler. Re-consolidating existing
    clusters preserves their observation counts and full temporal spans.
    """
    gap = _non_negative(max_gap_s, "max_gap_s")
    pending: dict[tuple[str, str], list[GameEvent]] = {}
    output: list[GameEvent] = []
    last_seen_by_key: dict[tuple[str, str], float] = {}
    for event in sorted(events, key=lambda item: (item.timestamp, item.type.value, item.source.value)):
        event.validate()
        key = (event.type.value, event.source.value)
        cluster = pending.get(key)
        if cluster and event.timestamp - last_seen_by_key[key] > gap:
            output.append(_merge_cluster(cluster))
            cluster = None
        if cluster is None:
            pending[key] = [event]
        else:
            cluster.append(event)
        last_seen_by_key[key] = max(last_seen_by_key.get(key, 0.0), _last_seen(event))
    output.extend(_merge_cluster(cluster) for cluster in pending.values())
    return sorted(output, key=lambda item: (item.timestamp, item.type.value, item.source.value))


class GameAssembler:
    """Pair visual anchors without estimating unobserved game boundaries.

    Nearby conflicting outcomes are UNKNOWN, including when a stronger LOSS
    template follows a WIN template. Editorial cut decisions belong elsewhere.
    """

    def __init__(self, *, conflicting_result_window_s: float = 5.0) -> None:
        self.conflicting_result_window_s = _non_negative(
            conflicting_result_window_s, "conflicting_result_window_s",
        )

    @staticmethod
    def _game(index: int, start: GameEvent | None, outcomes: list[GameEvent]) -> GameSegment:
        markers = ([start] if start is not None else []) + outcomes
        markers = sorted(markers, key=lambda event: (event.timestamp, event.type.value))
        first_outcome = min(outcomes, key=lambda event: event.timestamp) if outcomes else None
        result_types = {event.type for event in outcomes}
        result = GameResult.UNKNOWN
        result_confidence = 0.0
        result_source = None
        issues: list[str] = []
        if start is None:
            issues.append("missing_start")
        if first_outcome is None:
            issues.append("missing_end")
        elif len(result_types) > 1:
            issues.append("conflicting_results")
        else:
            peak = max(outcomes, key=lambda event: event.confidence)
            result = GameResult.WIN if peak.type == GameEventType.VICTORY else GameResult.LOSS
            result_confidence = float(peak.confidence)
            result_source = peak.source
        if start is not None and first_outcome is not None and first_outcome.timestamp <= start.timestamp:
            issues.append("non_positive_duration")
            result = GameResult.UNKNOWN
            result_confidence = 0.0
            result_source = None
        signature = "|".join(f"{event.type.value}:{event.timestamp:.9f}:{event.source.value}" for event in markers)
        identifier = "game-" + hashlib.sha256(signature.encode("utf-8")).hexdigest()[:20]
        return GameSegment(
            id=identifier,
            index=index,
            start=float(start.timestamp) if start is not None else None,
            end=float(first_outcome.timestamp) if first_outcome is not None else None,
            result=result,
            start_confidence=float(start.confidence) if start is not None else 0.0,
            end_confidence=float(first_outcome.confidence) if first_outcome is not None else 0.0,
            result_confidence=result_confidence,
            start_source=start.source if start is not None else None,
            end_source=first_outcome.source if first_outcome is not None else None,
            result_source=result_source,
            markers=[replace(event, metadata=deepcopy(event.metadata)) for event in markers],
            metadata={"issues": issues, "boundary_kind": "visual_anchor"},
        )

    def assemble(self, events: Iterable[GameEvent]) -> list[GameSegment]:
        ordered = consolidate_events(events)
        games: list[GameSegment] = []
        start: GameEvent | None = None
        outcomes: list[GameEvent] = []

        def flush() -> None:
            nonlocal start, outcomes
            if start is not None or outcomes:
                games.append(self._game(len(games) + 1, start, outcomes))
            start = None
            outcomes = []

        # At an identical timestamp prefer the start marker before outcomes;
        # the resulting zero-span pair is retained but classified UNKNOWN.
        ordered.sort(key=lambda event: (event.timestamp, event.type != GameEventType.VS_SCREEN, event.type.value))
        for event in ordered:
            if event.type == GameEventType.VS_SCREEN:
                flush()
                start = event
                continue
            if outcomes:
                last = max(_last_seen(outcome) for outcome in outcomes)
                if event.timestamp - last > self.conflicting_result_window_s:
                    flush()
            outcomes.append(event)
        flush()
        return games
