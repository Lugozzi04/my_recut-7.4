"""GameContext per kept game, from existing gameplay artifacts (never re-detected when valid).

Sources, in order: the project's gameplay payload (CLI pipeline) or the GUI's
per-track gameplay islands; optionally the shared, cached Hearthstone detector
on a plain MP4 (--games detect); otherwise one context for the whole video.
Kept content outside any game (talk between games, intros) becomes its own
"segment" context so it can still provide highlights.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from analysis.audio_service import AnalysisCancellation

from .models import GameContext, TranscriptLine
from .timeline import EditMap

MIN_KEPT_S = 1.0
MIN_SEGMENT_S = 20.0


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _context(edit_map: EditMap, *, game_id: str, index: int | None, result: str | None, source_index: int,
             start: float, end: float, origin: str, markers: Mapping[str, Any]) -> GameContext | None:
    kept = edit_map.kept_seconds(source_index, start, end)
    if kept < MIN_KEPT_S:
        return None  # removed from the final video: not part of the packaging
    edited = edit_map.edited_range(source_index, start, end)
    mapped: dict[str, Any] = {}
    for name, value in markers.items():
        t = _float(value)
        if t is not None:
            exact = edit_map.to_edited(t, source_index)
            if exact is None and start - 1e-3 <= t <= end + 1e-3:  # e.g. a silence cut right at the VS screen
                exact = edit_map.to_edited_nearest(t, source_index)
            mapped[name] = {"source": round(t, 3), "edited": None if exact is None else round(exact, 3),
                            "exact": edit_map.to_edited(t, source_index) is not None}
    return GameContext(game_id, index, result, source_index, start, end, edited[0] if edited else None,
                       edited[1] if edited else None, kept, origin, markers=mapped)


def games_from_project(payload: Mapping[str, Any], edit_map: EditMap) -> list[GameContext]:
    """Kept games from the CLI gameplay payload or the GUI per-track islands (source time)."""
    contexts: list[GameContext] = []
    gameplay = payload.get("gameplay")
    tracks = [t for t in payload.get("tracks", []) if isinstance(t, Mapping)]
    source_of = {str(Path(s).resolve()).lower(): i for i, s in enumerate(edit_map.sources)}

    def source_index(track: Mapping[str, Any]) -> int:
        path = str(track.get("path", "") or "")
        candidates = [str(Path(path).resolve()).lower()] if path else []
        if edit_map.project_path and path and not Path(path).is_absolute():
            candidates.append(str((Path(edit_map.project_path).parent / path).resolve()).lower())
        return next((source_of[c] for c in candidates if c in source_of), 0)

    if payload.get("export_selection"):
        # The immutable export plan is authoritative. Per-track detector reports
        # still contain unselected games, and game IDs may repeat across VODs.
        intervals: dict[tuple[int, str, str], list[Mapping[str, Any]]] = {}
        for track in tracks:
            for record in track.get("export_intervals", []):
                if record.get("role") != "gameplay" or not record.get("included"):
                    continue
                key = (source_index(track), str(record["source_id"]), str(record["game_id"]))
                intervals.setdefault(key, []).append(record)
        for (source_idx, source_id, game_id), records in intervals.items():
            identity = hashlib.sha256(f"{source_id}|{game_id}".encode("utf-8")).hexdigest()[:20]
            context = _context(edit_map, game_id="game-" + identity,
                index=None, result=records[0].get("result"), source_index=source_idx,
                start=min(float(r["start"]) for r in records), end=max(float(r["end"]) for r in records),
                origin="export_selection", markers={})
            if context is not None:
                contexts.append(context)
        return sorted(contexts, key=lambda c: c.edited_start or 0.0)
    if isinstance(gameplay, Mapping) and isinstance(gameplay.get("games"), list):
        islands = {str((i.get("game") or {}).get("id")): i for i in gameplay.get("keep_islands", []) or []
                   if isinstance(i, Mapping)}
        track_index = source_index(tracks[0]) if tracks else 0
        for game in gameplay["games"]:
            if not isinstance(game, Mapping):
                continue
            island = islands.get(str(game.get("id")))
            start = _float((island or game).get("start"))
            end = _float((island or game).get("end"))
            if start is None or end is None or end <= start:
                continue
            context = _context(edit_map, game_id=str(game.get("id") or f"game-{game.get('index')}"),
                               index=game.get("index"), result=game.get("result"), source_index=track_index,
                               start=start, end=end, origin="project_gameplay",
                               markers={"vs_screen": game.get("start"), "result_screen": game.get("end")})
            if context is not None:
                contexts.append(context)
        return contexts
    for track in tracks:
        offset = _float(track.get("segment_source_in")) or 0.0
        for island in track.get("gameplay_islands") or []:
            if not isinstance(island, Mapping):
                continue
            start, end = _float(island.get("start")), _float(island.get("end"))
            if start is None or end is None or end <= start:
                continue
            index = island.get("game_index")
            context = _context(edit_map, game_id=str(island.get("game_id") or f"game-{index}"), index=index,
                               result=island.get("result"), source_index=source_index(track), start=offset + start,
                               end=offset + end, origin="project_gameplay", markers={})
            if context is not None:
                contexts.append(context)
    return contexts


def games_from_detector(edit_map: EditMap, *, detector: Callable[..., Any] | None = None,
                        cancellation: AnalysisCancellation | None = None) -> list[GameContext]:
    """Run the shared (cached) Hearthstone detector on each source; only games with kept content."""
    if detector is None:
        from analysis.gameplay_editing import GameplayEditSettings, hearthstone_detector
        detector = hearthstone_detector(GameplayEditSettings())
    contexts: list[GameContext] = []
    token = cancellation or AnalysisCancellation()
    for index, source in enumerate(edit_map.sources):
        analysis = detector(Path(source), token, None)
        for game in analysis.games:
            start = game.start if game.start is not None else 0.0
            end = game.end if game.end is not None else analysis.duration
            context = _context(edit_map, game_id=game.id, index=game.index, result=game.result.value,
                               source_index=index, start=start, end=end, origin="detector",
                               markers={"vs_screen": game.start, "result_screen": game.end})
            if context is not None:
                contexts.append(context)
    return contexts


def add_uncovered_segments(contexts: list[GameContext], edit_map: EditMap) -> list[GameContext]:
    """Kept content outside every game becomes 'segment-N' contexts (or one 'video' context)."""
    covered = sorted((c.edited_start, c.edited_end) for c in contexts
                     if c.edited_start is not None and c.edited_end is not None)
    gaps: list[tuple[float, float]] = []
    cursor = 0.0
    for a, b in covered:
        if a - cursor >= MIN_SEGMENT_S:
            gaps.append((cursor, a))
        cursor = max(cursor, b)
    if edit_map.duration - cursor >= MIN_SEGMENT_S:
        gaps.append((cursor, edit_map.duration))
    out = list(contexts)
    for n, (a, b) in enumerate(gaps, start=1):
        index, start = edit_map.to_source(a)
        _, end = edit_map.to_source(b)
        game_id = "video" if not contexts and len(gaps) == 1 else f"segment-{n}"
        out.append(GameContext(game_id, None, None, index, start, end, a, b, b - a,
                               "whole_video" if game_id == "video" else "non_game"))
    out.sort(key=lambda c: c.edited_start if c.edited_start is not None else 0.0)
    return out


def attach_transcript(contexts: Sequence[GameContext], lines: Sequence[TranscriptLine], *, excerpt_chars: int = 400) -> None:
    for context in contexts:
        if context.edited_start is None or context.edited_end is None:
            continue
        mine = [line for line in lines if context.edited_start - 1e-3 <= (line.edited_start + line.edited_end) / 2
                <= context.edited_end + 1e-3]
        context.transcript_ids = [line.id for line in mine]
        text = " ".join(line.text for line in mine)
        context.transcript_excerpt = text if len(text) <= excerpt_chars else text[:excerpt_chars].rsplit(" ", 1)[0] + " ..."


def game_for(contexts: Sequence[GameContext], edited_t: float) -> GameContext | None:
    for context in contexts:
        if context.edited_start is not None and context.edited_end is not None and \
                context.edited_start - 1e-3 <= edited_t <= context.edited_end + 1e-3:
            return context
    return None
