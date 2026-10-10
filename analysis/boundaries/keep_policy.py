"""GameSegment[] + BoundaryDecision[] -> gameplay KEEPS (analysis.cut_engine.Segment).

`keep-games wins` is defined by what is KEPT: one editorial island per
publishable game (start AND end observed AND result WIN), from its smart START
to its smart END. Everything else in the file - intros, chatter that no kept
game owns, losses, incomplete games, the tail after the last win - is removed.
Gameplay cuts are only derived, as the complement of the keeps.

Two clamps make any resolver (exact, agent, fallback) safe:
  * coverage: an island always contains its whole game [VS, result];
  * isolation: an island never enters another game's gameplay span (removed
    games use their observed or estimated region, see GameplayTimeline).
Both are recorded in the provenance. Overlapping islands are merged only in
`segments`; provenance stays per game.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from analysis.cut_engine import Segment, invert_to_keeps, merge_overlaps
from .gameplay import GameplayTimeline
from .models import BoundaryDecision, BoundaryType, GameDisposition, GameInfo


@dataclass
class GameplayKeep:
    segment: Segment
    game: GameInfo
    start_source: str  # decision | anchor
    end_source: str
    decisions: dict[str, dict[str, Any]] = field(default_factory=dict)
    adjustments: list[str] = field(default_factory=list)

    def to_mapping(self) -> dict[str, Any]:
        return {"start": self.segment.start, "end": self.segment.end, "game": self.game.to_mapping(),
                "raw_start": self.game.start, "raw_end": self.game.end,
                "start_source": self.start_source, "end_source": self.end_source,
                "decisions": self.decisions, "adjustments": list(self.adjustments)}


class GameKeepPolicy:
    def __init__(self, timeline: GameplayTimeline) -> None:
        self.timeline = timeline

    def publishable(self) -> list[Any]:
        return [g for g in self.timeline.games if self.timeline.info(g).disposition == GameDisposition.KEEP
                and g.start is not None and g.end is not None]

    def _other_spans(self, index: int) -> list[tuple[float, float]]:
        spans = []
        for other in self.timeline.games:
            if other.index == index:
                continue
            if self.timeline.info(other).disposition == GameDisposition.KEEP and other.start is not None and other.end is not None:
                spans.append((float(other.start), float(other.end)))
            else:
                region = self.timeline.removal_anchors(other)
                spans.append((region.start, region.end))
        return spans

    def keeps(self, decisions: Mapping[int, Mapping[BoundaryType, BoundaryDecision]]) -> list[GameplayKeep]:
        out: list[GameplayKeep] = []
        for game in self.publishable():
            assert game.start is not None and game.end is not None
            info = self.timeline.info(game)
            chosen = decisions.get(game.index, {})
            adjustments: list[str] = []
            raw_start, raw_end = float(game.start), float(game.end)
            if BoundaryType.START in chosen:
                start, start_source = float(chosen[BoundaryType.START].timestamp), "decision"
            else:
                start, start_source = raw_start, "anchor"
                adjustments.append("no START decision: used the VS anchor")
            if BoundaryType.END in chosen:
                end, end_source = float(chosen[BoundaryType.END].timestamp), "decision"
            else:
                end, end_source = raw_end, "anchor"
                adjustments.append("no END decision: used the result anchor")
            if start > raw_start:
                adjustments.append(f"coverage: START {start:.3f} moved to VS {raw_start:.3f}")
                start = raw_start
            if end < raw_end:
                adjustments.append(f"coverage: END {end:.3f} moved to result {raw_end:.3f}")
                end = raw_end
            for span_start, span_end in self._other_spans(game.index):
                if span_end <= raw_start and start < span_end:
                    adjustments.append(f"isolation: START {start:.3f} moved after game ending {span_end:.3f}")
                    start = span_end
                if span_start >= raw_end and end > span_start:
                    adjustments.append(f"isolation: END {end:.3f} moved before game starting {span_start:.3f}")
                    end = span_start
            start, end = max(0.0, start), min(self.timeline.duration, end)
            if end > start:
                out.append(GameplayKeep(Segment(start, end), info, start_source, end_source,
                                        {kind.value: decision.to_mapping() for kind, decision in chosen.items()},
                                        adjustments))
        return out

    @staticmethod
    def segments(keeps: list[GameplayKeep]) -> list[Segment]:
        return merge_overlaps([Segment(k.segment.start, k.segment.end) for k in keeps])

    @staticmethod
    def cuts(keeps: list[GameplayKeep], duration: float) -> list[Segment]:
        """Derived only: everything outside the keep islands (intro, chatter, losses, partials, tail)."""
        islands = GameKeepPolicy.segments(keeps)
        if not islands:
            return [Segment(0.0, float(duration))] if duration > 0 else []
        inverted = invert_to_keeps(float(duration), islands, min_keep=0.0)
        return [Segment(s.start, s.end) for s in inverted]
