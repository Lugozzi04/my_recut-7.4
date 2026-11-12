"""Teasers: copies of a fragment of the edit, placed elsewhere on the timeline (by default at the start).

A teaser is an ordinary timeline piece (TrackState + clips on the same media) marked with a
``teaser_id``. The original footage stays where it is. Teasers are numbered in timeline order
("Teaser 1", "Teaser 2", ...); the parts of a split teaser share one id and one number.
They are never game sources: game detection, class exports and the Sources list skip them.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

MIN_TEASER_SECONDS = 0.2


def is_teaser(track: Any) -> bool:
    """True for a TrackState or a project-file track dict that is a teaser copy."""
    if isinstance(track, Mapping):
        return bool(track.get("teaser_id"))
    return bool(getattr(track, "teaser_id", ""))


def teaser_label(index: int) -> str:
    return f"Teaser {max(1, int(index))}"


def renumber(tracks: Iterable[Any]) -> None:
    """Teaser N follows timeline order; parts of one teaser keep the same number."""
    numbers: dict[str, int] = {}
    for track in tracks:
        tid = str(getattr(track, "teaser_id", "") or "")
        if not tid:
            if getattr(track, "teaser_index", 0):
                track.teaser_index = 0
            continue
        if tid not in numbers:
            numbers[tid] = len(numbers) + 1
        track.teaser_index = numbers[tid]


def start_insert_index(tracks: Sequence[Any]) -> int:
    """Where "at the start" means: after the leading intros and the teasers already placed there."""
    index = 0
    for track in tracks:
        if getattr(track, "source_role", "gameplay") == "intro" or is_teaser(track):
            index += 1
            continue
        break
    return index


def reanchor_overlays(overlays: Sequence[Any], before: Sequence[Mapping[str, Any]],
                      after: Sequence[Mapping[str, Any]]) -> list[Any]:
    """Move timeline texts with their footage after pieces were inserted, moved, split or removed.

    ``before``/``after`` describe each piece: key (identity of the piece object), path, group
    (split parts share it), teaser id, src_in/src_out (seconds in the media) and tl_in/tl_out
    (seconds on the timeline). A text whose footage no longer exists keeps its time.
    """
    out: list[Any] = []
    for ov in overlays:
        start = float(ov.start)
        old = next((p for p in before if float(p["tl_in"]) - 1e-6 <= start < float(p["tl_out"]) - 1e-6), None)
        if old is None:
            out.append(ov)
            continue
        source_t = float(old["src_in"]) + (start - float(old["tl_in"]))

        def holds(piece: Mapping[str, Any]) -> bool:
            return (str(piece["path"]) == str(old["path"]) and str(piece.get("teaser", "")) == str(old.get("teaser", ""))
                    and float(piece["src_in"]) - 1e-6 <= source_t < float(piece["src_out"]) + 1e-6)

        candidates = [p for p in after if holds(p)]
        new = (next((p for p in candidates if p.get("key") == old.get("key")), None)
               or next((p for p in candidates if p.get("group") and p.get("group") == old.get("group")), None)
               or (candidates[0] if candidates else None))
        if new is None:
            out.append(ov)
            continue
        delta = float(new["tl_in"]) + (source_t - float(new["src_in"])) - start
        if abs(delta) < 1e-6:
            out.append(ov)
            continue
        out.append(dataclasses.replace(ov, start=round(start + delta, 3), end=round(float(ov.end) + delta, 3)))
    return out
