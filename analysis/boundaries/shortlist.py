"""Non-destructive candidate shortlist for resolvers. It never chooses a boundary.

1. Coverage (hard, safe): an editorial region always contains its whole game,
   so a START candidate after the start anchor, or an END candidate before the
   end anchor, would cut the game itself (a kept WIN would lose its VS/result;
   a removed game would leave gameplay behind). GameKeepPolicy clamps such a
   choice anyway; hiding it avoids wasting the resolver's decision.
2. Size cap (soft): only above `max_candidates`, weakest kinds go first
   (SILENCE_MID-only, then VISUAL_CHANGE-only, farthest from the anchor first).
   Anchors, sentence/transcript edges and silence edges are never dropped; if
   they alone exceed the cap, all of them stay (quality before tokens).
On the real videoprova contexts (5-41 candidates) only rule 1 applies.
"""
from __future__ import annotations

from .models import BoundaryContext, BoundaryType, CandidateSource

DROP_ORDER = (frozenset({CandidateSource.WORD_GAP}), frozenset({CandidateSource.SILENCE_MID}),
              frozenset({CandidateSource.VISUAL_CHANGE}))
EPSILON = 0.3  # candidate dedup can move an anchor by <= 0.25 s


def eligible(context: BoundaryContext) -> list[int]:
    anchor = context.settings.get("anchor")
    if not isinstance(anchor, (int, float)):
        return list(range(len(context.candidates)))
    if context.boundary_type == BoundaryType.START:
        out = [i for i, c in enumerate(context.candidates) if c.timestamp <= float(anchor) + EPSILON]
    else:
        out = [i for i, c in enumerate(context.candidates) if c.timestamp >= float(anchor) - EPSILON]
    return out or list(range(len(context.candidates)))


def shortlist(context: BoundaryContext, max_candidates: int = 40) -> list[int]:
    indices = eligible(context)
    if len(indices) <= max_candidates:
        return indices
    anchor = float(context.settings.get("anchor", context.window.start))
    keep = list(indices)
    for weak in DROP_ORDER:
        droppable = sorted((i for i in keep if set(context.candidates[i].sources) <= weak),
                           key=lambda i: -abs(context.candidates[i].timestamp - anchor))
        while len(keep) > max_candidates and droppable:
            keep.remove(droppable.pop(0))
    return sorted(keep)
