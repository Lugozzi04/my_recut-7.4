"""Persistent, source-time gameplay overrides. Detection reports remain immutable."""
from __future__ import annotations

from copy import deepcopy
import math
from pathlib import Path
from typing import Any
import uuid

from analysis.cut_engine import Segment, invert_to_keeps, merge_overlaps
from analysis.classic import subtract_segments
from analysis.hero_groups.recognition import game_key, refresh_groups


def editor(grouping: dict[str, Any]) -> dict[str, Any]:
    return grouping.setdefault("editor", {"version": 1, "included": {}, "boundaries": {}, "manual_games": []})


def preserve_editor(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Merge user-owned data after automatic grouping, without changing recognition."""
    result = deepcopy(current)
    if "editor" in previous:
        result["editor"] = deepcopy(previous["editor"])
    result.setdefault("games", [])
    result.setdefault("overrides", {}).update(previous.get("overrides", {}))
    result.setdefault("names", {}).update(previous.get("names", {}))
    keys = {r["key"] for r in result["games"]}
    for game in result.get("editor", {}).get("manual_games", []):
        key = game_key(game)
        if key not in keys:
            result["games"].append({"key": key, "game": deepcopy(game), "group": game.get("group"),
                                    "automatic_group": None, "reason": "Manual interval"})
    for row in result["games"]:
        if row["key"] in result["overrides"]:
            row["group"] = result["overrides"][row["key"]]
    refresh_groups(result)
    return result


def catalog(report: dict[str, Any], grouping: dict[str, Any]) -> list[dict[str, Any]]:
    """Effective boundaries and inclusion, separate from original result/anchors."""
    state = grouping.get("editor", {})
    rows = {r["key"]: r for r in grouping.get("games", [])}
    islands = {game_key(i["game"]): i for i in report.get("keep_islands", [])}
    games = list(report.get("games", [])) + list(state.get("manual_games", []))
    out = []
    for key, game in {game_key(g): g for g in games}.items():
        island = islands.get(key, game)
        bounds = state.get("boundaries", {}).get(key, [island.get("start"), island.get("end")])
        a, b = bounds
        complete = a is not None and b is not None and math.isfinite(float(a)) and math.isfinite(float(b)) and b > a
        default = complete and (key in islands if "keep_islands" in report else str(game.get("result")).upper() == "WIN")
        included = bool(state.get("included", {}).get(key, default)) and complete
        row = rows.get(key, {})
        group = row.get("group", game.get("group"))
        info = grouping.get("groups", {}).get(group, {})
        out.append({"key": key, "game": game, "start": a, "end": b, "complete": complete,
                    "included": included, "manual": bool(game.get("manual")), "group": group,
                    "hero_class": info.get("class_name"), "locked": key in grouping.get("overrides", {})})
    return sorted(out, key=lambda r: (r["start"] if r["start"] is not None else r["end"] or 0, r["key"]))


def permitted(row: dict[str, Any], grouping: dict[str, Any]) -> bool:
    override = grouping.get("editor", {}).get("included", {}).get(row["key"])
    return bool(override) if override is not None else str(row["game"].get("result", "")).upper() == "WIN"


def segment_bounds(track: Any) -> tuple[float, float]:
    a = float(track.segment_source_in or 0)
    b = float(track.segment_source_out or 0)
    return (a, b) if b > a else (0.0, float(track.duration))


def timeline_boxes(track: Any) -> list[dict[str, Any]]:
    a, b = segment_bounds(track)
    grouping = track.gameplay_grouping
    if "editor" not in grouping and track.gameplay_mode == "keep":
        grouping = initialize([track])
    rows = catalog(track.gameplay_report, grouping)
    boxes = []
    for row in rows:
        # Missing sides are estimates for display only, never exportable boundaries.
        start, end = row["start"], row["end"]
        if start is None and end is None:
            continue
        if start is None:
            start = max((float(r["end"]) for r in rows if r["end"] is not None and r["end"] < end), default=0.0)
        if end is None:
            end = min((float(r["start"]) for r in rows if r["start"] is not None and r["start"] > start), default=b)
        left, right = max(a, float(start)), min(b, float(end))
        if right > left:
            boxes.append({"start": left-a, "end": right-a, "game_id": row["key"],
                          "game_index": row["game"].get("index"), "result": row["game"].get("result", "UNKNOWN"),
                          "hero_class": row["hero_class"], "included": row["included"],
                          "manual": row["manual"], "complete": row["complete"]})
    return boxes


def initialize(tracks: list[Any]) -> dict[str, Any]:
    """Adopt older edited keeps once, so first interaction never resurrects removed WINs."""
    grouping = preserve_editor(tracks[0].gameplay_grouping, tracks[0].gameplay_grouping)
    if "editor" not in grouping:
        state = editor(grouping)
        for row in catalog(tracks[0].gameplay_report, grouping):
            if not row["included"]:
                continue
            fragments = []
            visible = False
            for track in tracks:
                a, b = segment_bounds(track)
                if min(b, row["end"]) <= max(a, row["start"]):
                    continue
                visible = True
                for meta, keep in zip(track.gameplay_islands, track.gameplay_keeps):
                    if str(meta.get("game_id")) == row["key"]:
                        fragments.append((a + keep.start, a + keep.end))
                if not track.gameplay_islands:
                    fragments.extend((a+k.start, a+k.end) for k in track.gameplay_keeps
                                     if min(a+k.end, row["end"]) > max(a+k.start, row["start"]))
            if visible and any(t.gameplay_mode == "keep" for t in tracks):
                state["included"][row["key"]] = bool(fragments)
                if fragments:
                    left = min(x[0] for x in fragments)
                    right = max(x[1] for x in fragments)
                    # Only adopt boundaries for an unsplit, full-source track.
                    if any(segment_bounds(t)[0] == 0 and segment_bounds(t)[1] >= row["end"] for t in tracks):
                        if abs(left-row["start"]) > .001 or abs(right-row["end"]) > .001:
                            state["boundaries"][row["key"]] = [left, right]
    return grouping


def validate_bounds(report: dict[str, Any], grouping: dict[str, Any], key: str,
                    start: float, end: float, duration: float) -> None:
    if not all(math.isfinite(v) for v in (start, end, duration)) or not 0 <= start < end <= duration:
        raise ValueError("Use 0 ≤ START < END ≤ video duration.")
    for row in catalog(report, grouping):
        if row["key"] != key and row["complete"] and min(end, row["end"]) > max(start, row["start"]) + 1e-6:
            raise ValueError("This interval overlaps another game. Adjust its boundaries first.")


def create_interval(report: dict[str, Any], grouping: dict[str, Any], start: float,
                    end: float, duration: float, group: str) -> str:
    if group not in grouping.get("groups", {}):
        raise ValueError("Choose an existing group.")
    key = "manual-" + uuid.uuid4().hex
    validate_bounds(report, grouping, key, start, end, duration)
    state = editor(grouping)
    game = {"id": key, "index": max((r["game"].get("index", 0) for r in catalog(report, grouping)), default=0)+1,
            "start": start, "end": end, "result": "UNKNOWN", "manual": True, "group": group}
    state["manual_games"].append(game)
    state["included"][key] = True
    grouping.setdefault("overrides", {})[key] = group
    merged = preserve_editor(grouping, grouping)
    grouping.clear()
    grouping.update(merged)
    return key


def delete_interval(grouping: dict[str, Any], key: str) -> None:
    state = editor(grouping)
    if not any(game_key(g) == key for g in state["manual_games"]):
        raise ValueError("Only manually created intervals can be deleted; exclude detected games instead.")
    state["manual_games"] = [g for g in state["manual_games"] if game_key(g) != key]
    for name in ("included", "boundaries"):
        state[name].pop(key, None)
    grouping.get("overrides", {}).pop(key, None)
    grouping["games"] = [r for r in grouping.get("games", []) if r["key"] != key]
    refresh_groups(grouping)


def apply(tracks: list[Any], grouping: dict[str, Any]) -> None:
    """Project source-time selections onto existing segment-local keep/cut fields."""
    for track in tracks:
        track.gameplay_grouping = deepcopy(grouping)
        a, b = segment_bounds(track)
        rows = catalog(track.gameplay_report, grouping)
        chosen = [r for r in rows if r["included"] and min(b, r["end"]) > max(a, r["start"])]
        track.gameplay_keeps = [Segment(max(a, r["start"])-a, min(b, r["end"])-a) for r in chosen]
        old = {m.get("game_id"): m for m in track.gameplay_islands}
        track.gameplay_islands = [
            {**old.get(r["key"], {}), "game_id": r["key"], "game_index": r["game"].get("index"),
             "result": r["game"].get("result", "UNKNOWN"), "start": k.start, "end": k.end}
            for r, k in zip(chosen, track.gameplay_keeps)]
        track.gameplay_mode = "keep"
        track.gameplay_cuts_enabled = True
        track.gameplay_cuts = invert_to_keeps(b-a, merge_overlaps(track.gameplay_keeps), min_keep=0)
        track.cuts_enabled = True
        enforce_selection(track)


def enforce_selection(track: Any) -> None:
    """Explicit exclusions outrank Restore cut, even with automatic Gameplay Editing off."""
    if getattr(track, "source_role", "gameplay") == "intro" or "editor" not in track.gameplay_grouping:
        return
    a, b = segment_bounds(track)
    if track.gameplay_cuts_enabled:
        masks = list(track.gameplay_cuts)
    else:
        state = track.gameplay_grouping["editor"]
        masks = [Segment(max(a, r["start"])-a, min(b, r["end"])-a)
                 for r in catalog(track.gameplay_report, track.gameplay_grouping)
                 if state["included"].get(r["key"]) is False and r["complete"]
                 and min(b, r["end"]) > max(a, r["start"])]
    if masks:
        track.cuts_enabled = True
        track.keeps = subtract_segments(track.keeps, masks)
        track.cuts = merge_overlaps(list(track.cuts) + masks)


def source_tracks(tracks: list[Any], index: int) -> list[Any]:
    target = Path(tracks[index].path).resolve()
    return [t for t in tracks if t.path and Path(t.path).resolve() == target
            and getattr(t, "source_role", "gameplay") != "intro"]
