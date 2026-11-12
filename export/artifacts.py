"""Export-local snapshots and readable chapter timings; no media analysis."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
import os
import uuid

from analysis.gameplay.cache import atomic_write_json
from core.project_file import make_payload_portable
from analysis.packaging.timeline import EditMap, KeepPiece


def save_snapshot(payload: dict[str, Any], target: Path) -> Path:
    return atomic_write_json(target, make_payload_portable(payload, target))


def _clock(t: float) -> str:
    seconds = max(0, int(t))
    minutes, sec = divmod(seconds, 60)
    hours, minute = divmod(minutes, 60)
    return f"{hours}:{minute:02d}:{sec:02d}" if hours else f"{minute}:{sec:02d}"


def timing_text(payload: Mapping[str, Any], *, segments: Sequence[Mapping[str, Any]] | None = None,
                input_paths: Sequence[str] = ()) -> str:
    """Copy/paste-ready chapter starts on the final output timeline, after internal cuts."""
    tracks = list(payload.get("tracks", []))
    sources: list[str] = list(input_paths)
    fragments: list[tuple[int, float, float, dict[str, Any]]] = []
    for track in tracks:
        source = str(Path(track["path"]).resolve())
        if source not in sources:
            sources.append(source)
    if segments is not None:
        for segment in segments:
            index = int(segment["v_idx"])
            a, b = float(segment["v_in"]), float(segment["v_out"])
            teaser = str(segment.get("teaser_id") or "")
            same_source = [t for t in tracks if str(Path(t["path"]).resolve()) == str(Path(sources[index]).resolve())
                           and str(t.get("teaser_id") or "") == teaser]
            track = next((t for t in same_source if float(t.get("segment_source_in", 0)) - 1e-3 <= a
                          and (not t.get("segment_source_out") or a < float(t["segment_source_out"]) + 1e-3)),
                         same_source[0] if same_source else {})
            fragments.append((index, a, b, track))
    else:
        for track in tracks:
            index = sources.index(str(Path(track["path"]).resolve()))
            offset = float(track.get("segment_source_in", 0))
            spans = track.get("keeps", []) if track.get("cuts_enabled") else [{"start": 0, "end": track.get("duration", 0)}]
            fragments.extend((index, offset + float(s["start"]), offset + float(s["end"]), track) for s in spans)
    pieces, cursor = [], 0.0
    for index, a, b, _ in fragments:
        pieces.append(KeepPiece(index, a, b, cursor))
        cursor += max(0, b - a)
    if not pieces:
        return ""
    mapping = EditMap(sources, pieces, origin="export_plan")
    chapters: list[dict[str, Any]] = []
    for piece, (_, a, b, track) in zip(mapping.pieces, fragments):
        records = list(track.get("export_intervals", []))
        if track.get("teaser_id"):
            # A teaser is one chapter of its own, whatever games its footage shows.
            records = [{"role": "teaser", "game_id": str(track["teaser_id"]), "start": a, "end": b}]
        elif not records:
            offset = float(track.get("segment_source_in", 0))
            grouping = {str(r.get("key")): r.get("group") for r in track.get("gameplay_grouping", {}).get("games", [])}
            records = [{**r, "start": offset + float(r["start"]), "end": offset + float(r["end"]),
                        "group": grouping.get(str(r.get("game_id"))), "role": "gameplay"}
                       for r in track.get("gameplay_islands", [])]
        grouped: dict[tuple[Any, Any], dict[str, Any]] = {}
        for r in records:
            key = (r.get("role"), r.get("game_id") or r.get("game_index"))
            if key in grouped:
                grouped[key]["start"] = min(float(grouped[key]["start"]), float(r["start"]))
                grouped[key]["end"] = max(float(grouped[key]["end"]), float(r["end"]))
            else:
                grouped[key] = dict(r)
        records = list(grouped.values())
        cuts = sorted({a, b, *(max(a, min(b, float(r[k]))) for r in records for k in ("start", "end"))})

        for lo, hi in zip(cuts, cuts[1:]):
            if hi <= lo:
                continue
            record: dict[str, Any] = next((r for r in records if float(r["start"]) <= lo + .02 and float(r["end"]) >= hi - .02), {})
            role = record.get("role", track.get("source_role", "gameplay"))
            game = record.get("game_id") or record.get("game_index")
            group = record.get("output_group") or record.get("group")
            identity = (piece.source_index, role, game, group)
            start, end = piece.edited_start + lo - a, piece.edited_start + hi - a
            if chapters and chapters[-1]["identity"] == identity and abs(chapters[-1]["end"] - start) < 1e-6:
                chapters[-1].update(end=end, source_end=hi)
            else:
                chapters.append({"identity": identity, "start": start, "end": end,
                                 "source_start": lo, "source_end": hi, "result": record.get("result", "UNKNOWN"),
                                 "game_index": record.get("game_index")})
    lines: list[str] = []
    game_number = 0
    teaser_numbers: dict[Any, int] = {}
    previous_role = None
    for chapter in chapters:
        role = chapter["identity"][1]
        if role == "intro":
            # Consecutive intro sources form one opening chapter.
            if previous_role == "intro":
                continue
            label = "Intro"
        elif role == "teaser":
            teaser = chapter["identity"][2]
            if previous_role == "teaser" and teaser in teaser_numbers:
                continue  # the parts of one teaser stay one chapter
            teaser_numbers.setdefault(teaser, len(teaser_numbers) + 1)
            label = f"Teaser {teaser_numbers[teaser]}"
        else:
            game_number += 1
            label = f"Partita {game_number}"
        lines.append(f"{_clock(chapter['start'])} {label}")
        previous_role = role
    return "\n".join(lines) + ("\n" if lines else "")


def write_timings(payload: Mapping[str, Any], target: Path, **kwargs: Any) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(timing_text(payload, **kwargs), encoding="utf-8")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
