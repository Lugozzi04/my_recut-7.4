"""Result-screen episodes are consolidated before game reconstruction."""
from __future__ import annotations

from copy import deepcopy
import math
from typing import Iterable

from analysis.gameplay.assembler import consolidate_events
from analysis.gameplay.models import GameEvent, GameEventType


def deduplicate_result_events(events: Iterable[GameEvent], *, barriers: Iterable[float] = (),
                              maximum_gap_s: float = 45.0) -> list[GameEvent]:
    """Merge same-result animation phases without crossing a VS/board return.

    Opposite outcomes are retained for the assembler's UNKNOWN conflict policy.
    The gap bounds missing state evidence; this does not invent game boundaries.
    """
    if (isinstance(maximum_gap_s, bool) or not isinstance(maximum_gap_s, (int, float))
            or not math.isfinite(maximum_gap_s) or maximum_gap_s < 0):
        raise ValueError('maximum_gap_s must be finite and nonnegative')
    ordered = sorted(events, key=lambda event: (event.timestamp, event.type.value))
    stops = sorted(set(barriers) | {event.timestamp for event in ordered if event.type == GameEventType.VS_SCREEN})
    if any(isinstance(value, bool) or not math.isfinite(value) or value < 0 for value in stops):
        raise ValueError('Invalid episode barrier')
    result: list[GameEvent] = []
    previous: GameEvent | None = None
    for event in ordered:
        event.validate()
        if event.type == GameEventType.VS_SCREEN:
            result.append(deepcopy(event))
            previous = None
            continue
        last = float(previous.metadata.get('last_seen', previous.timestamp)) if previous is not None else -math.inf
        can_merge = (previous is not None and previous.type == event.type and previous.source == event.source
                     and event.timestamp - last <= maximum_gap_s
                     and not any(last < boundary <= event.timestamp for boundary in stops))
        if can_merge:
            assert previous is not None
            merged = consolidate_events([previous, event], max_gap_s=maximum_gap_s)[0]
            merged.metadata['result_episode_clusters'] = int(previous.metadata.get('result_episode_clusters', 1)) + 1
            result[result.index(previous)] = merged
            previous = merged
        else:
            previous = deepcopy(event)
            result.append(previous)
    return sorted(result, key=lambda event: (event.timestamp, event.type.value))
