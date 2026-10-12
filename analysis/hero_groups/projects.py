"""Project persistence and group output planning. Reuses final keeps and exporter."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
import uuid
from typing import Any

from analysis.cut_engine import Segment, invert_to_keeps, merge_overlaps
from core.project_file import make_payload_portable, normalize_project_payload, resolve_project_items
from .recognition import HeroGroupingService, atomic_json, game_key, refresh_groups
from .labels import class_id, class_label


def read_project(project: Path) -> dict[str, Any]:
    payload = normalize_project_payload(json.loads(project.read_text(encoding="utf-8")))
    available, missing = resolve_project_items(payload["tracks"], project)
    if missing:
        raise ValueError("Project media missing; relink it before grouping.")
    payload["tracks"] = available
    return payload


def report_for_track(payload: dict[str, Any], track: dict[str, Any]) -> dict[str, Any]:
    report = track.get("gameplay_report") or payload.get("gameplay") or {}
    return report if isinstance(report, dict) else {}


def analyze_project(
    payload: dict[str, Any],
    *,
    service: HeroGroupingService | None = None,
    cancellation: Any = None,
    on_progress: Any = None,
) -> dict[str, Any]:
    result = deepcopy(payload)
    service = service or HeroGroupingService()
    seen: dict[str, dict[str, Any]] = {}
    for track in result["tracks"]:
        source = str(Path(track["path"]).resolve())
        report = report_for_track(result, track)
        if not report.get("games"):
            raise ValueError("Detect games first. This command does not rerun detection or boundaries.")
        if source not in seen:
            duration = service.sampler.probe(source, cancellation=cancellation).duration_s
            seen[source] = service.analyze(
                Path(source),
                report["games"],
                duration=duration,
                previous=track.get("gameplay_grouping"),
                cancellation=cancellation,
                on_progress=on_progress,
            )
        track["gameplay_report"] = deepcopy(report)
        track["gameplay_grouping"] = deepcopy(seen[source])
    return result


def assign_group(grouping: dict[str, Any], key: str, group: str | None) -> None:
    if group is not None:
        group = class_id(group) or group
        if not re.fullmatch("[A-Z]{1,4}", group) and not class_id(group):
            raise ValueError("Use a Hearthstone class name.")
    row = next((r for r in grouping["games"] if r["key"] == key), None)
    if row is None:
        raise ValueError("Unknown game.")
    grouping.setdefault("overrides", {})[key] = group
    row["group"] = group
    refresh_groups(grouping)
    if group and group not in grouping["selected"]:
        grouping["selected"].append(group)


def _segments(raw: list[dict[str, Any]]) -> list[Segment]:
    return merge_overlaps(
        [Segment(float(s["start"]), float(s["end"])) for s in raw if float(s["end"]) > float(s["start"])]
    )


def _islands(payload: dict[str, Any], track: dict[str, Any]) -> list[tuple[str, Segment]]:
    """Return the CURRENT user-adjustable islands; source-time fallback for CLI projects."""
    offset = float(track.get("segment_source_in", 0))
    current = track.get("gameplay_keeps", [])
    if track.get("gameplay_mode") == "keep" and not current:
        return []  # Explicitly removed all WINs; never resurrect the detector report.
    metadata = track.get("gameplay_islands", [])
    games = report_for_track(payload, track).get("games", [])
    by_index = {g["index"]: game_key(g) for g in games}
    if metadata and len(current) == len(metadata):
        return [
            (
                str(m.get("game_id") or by_index.get(m.get("game_index"), "")),
                Segment(float(k["start"]), float(k["end"])),
            )
            for k, m in zip(current, metadata)
        ]
    islands = report_for_track(payload, track).get("keep_islands", [])
    # CLI v2 projects: match detected source islands to the current local ones.
    if len(islands) == len(current):
        return [(game_key(i["game"]), Segment(float(k["start"]), float(k["end"]))) for k, i in zip(current, islands)]
    if current:
        raise ValueError("Cannot associate edited islands with games safely; rerun Gameplay Editing first.")
    return [(game_key(i["game"]), Segment(float(i["start"]) - offset, float(i["end"]) - offset)) for i in islands]


def exportable_group_games(payload: dict[str, Any], *, source: str | None = None) -> dict[str, set[str]]:
    """Available class outputs from current islands intersected with final keeps; no writes/render."""
    available: dict[str, set[str]] = {}
    for track in payload.get("tracks", []):
        if source and str(Path(track["path"]).resolve()) != str(Path(source).resolve()):
            continue
        rows = {r["key"]: r for r in track.get("gameplay_grouping", {}).get("games", [])}
        if not rows:
            continue
        duration = float(track.get("duration", 0))
        if duration <= 0:
            duration = float(track.get("segment_source_out", 0)) - float(track.get("segment_source_in", 0))
        final = _segments(track.get("keeps", [])) if track.get("cuts_enabled") else [Segment(0, duration)]
        for key, island in _islands(payload, track):
            row = rows.get(key)
            if row and row.get("group") and str(row["game"].get("result", "")).upper() == "WIN":
                if any(min(k.end, island.end) > max(k.start, island.start) for k in final):
                    available.setdefault(row["group"], set()).add(key)
    return available


def group_project(payload: dict[str, Any], group: str, *, source: str | None = None) -> dict[str, Any]:
    """Intersection with existing final keeps; never recompute an audio/boundary decision."""
    group = class_id(group) or group
    result = deepcopy(payload)
    output_tracks = []
    selected_games: dict[str, dict[str, Any]] = {}
    selected_islands = []
    names = set()
    sources = {str(Path(t["path"]).resolve()) for t in payload["tracks"] if t.get("gameplay_grouping")}
    if source is None and len(sources) > 1:
        raise ValueError("Choose one source VOD; group IDs are local to each VOD.")
    for track in result["tracks"]:
        if source and str(Path(track["path"]).resolve()) != str(Path(source).resolve()):
            continue
        grouping = track.get("gameplay_grouping", {})
        rows = [r for r in grouping.get("games", []) if r.get("group") == group]
        if not rows:
            continue
        # The current editor keeps only complete WINs; no LOSS is ever resurrected.
        permitted = {r["key"] for r in rows if str(r["game"].get("result", "")).upper() == "WIN"}
        islands = [(key, island) for key, island in _islands(result, track) if key in permitted]
        duration = float(track.get("duration", 0))
        if duration <= 0:
            duration = float(track.get("segment_source_out", 0)) - float(track.get("segment_source_in", 0))
        final = _segments(track.get("keeps", [])) if track.get("cuts_enabled") else [Segment(0, duration)]
        kept = merge_overlaps(
            [
                Segment(max(k.start, i.start), min(k.end, i.end))
                for k in final
                for _, i in islands
                if min(k.end, i.end) > max(k.start, i.start)
            ]
        )
        if not kept:
            continue
        islands = [(key, i) for key, i in islands if any(min(k.end, i.end) > max(k.start, i.start) for k in final)]
        permitted = {key for key, _ in islands}
        track["keeps"] = [{"start": k.start, "end": k.end} for k in kept]
        track["cuts"] = [{"start": k.start, "end": k.end} for k in invert_to_keeps(duration, kept, min_keep=0)]
        track["cuts_enabled"] = True
        track["gameplay_keeps"] = [{"start": i.start, "end": i.end} for _, i in islands]
        track["gameplay_detected_keeps"] = deepcopy(track["gameplay_keeps"])
        track["gameplay_cuts"] = [
            {"start": k.start, "end": k.end} for k in invert_to_keeps(duration, [i for _, i in islands], min_keep=0)
        ]
        track["gameplay_islands"] = [
            m
            for m in track.get("gameplay_islands", [])
            if str(
                m.get("game_id")
                or next((game_key(r["game"]) for r in rows if r["game"]["index"] == m.get("game_index")), "")
            )
            in permitted
        ]
        report = deepcopy(report_for_track(result, track))
        report["games"] = [g for g in report.get("games", []) if game_key(g) in permitted]
        original_islands = {game_key(i["game"]): i for i in report.get("keep_islands", [])}
        by_key = {game_key(r["game"]): r["game"] for r in rows}
        offset = float(track.get("segment_source_in", 0))
        report["keep_islands"] = [
            {
                **deepcopy(original_islands.get(key, {})),
                "game": by_key[key],
                "start": i.start + offset,
                "end": i.end + offset,
            }
            for key, i in islands
        ]
        track["gameplay_report"] = report
        track["gameplay_grouping"]["games"] = [r for r in grouping.get("games", []) if r["key"] in permitted]
        track["gameplay_grouping"]["selected"] = [group]
        refresh_groups(track["gameplay_grouping"])
        for row in rows:
            if row["key"] in permitted:
                selected_games[row["key"]] = row["game"]
        selected_islands.extend(report.get("keep_islands", []))
        name = grouping.get("groups", {}).get(group, {}).get("name")
        if name:
            names.add(name)
        output_tracks.append(track)
    if not output_tracks:
        raise ValueError(f"Group {group} has no exportable WIN keeps; no empty video was created.")
    output_tracks.sort(key=lambda t: (str(t["path"]), float(t.get("segment_source_in", 0))))
    result["tracks"] = output_tracks
    identity = uuid.uuid4().hex
    result["project_id"] = identity
    name = next(iter(names)) if len(names) == 1 else ""
    result["project_name"] = (
        str(payload.get("project_name") or "ReCut")
        + " — "
        + (class_label(group) if class_id(group) else "Group " + group)
        + (" — " + name if name and name != class_label(group) else "")
    )
    result["hero_group_output"] = {
        "id": identity,
        "group": group,
        "name": name or None,
        "parent_project_id": payload.get("project_id"),
    }
    by_game: dict[str, dict[str, Any]] = {}
    for island in selected_islands:
        key = game_key(island["game"])
        if key in by_game:
            by_game[key]["start"] = min(by_game[key]["start"], island["start"])
            by_game[key]["end"] = max(by_game[key]["end"], island["end"])
        else:
            by_game[key] = deepcopy(island)
    result["gameplay"] = {
        **deepcopy(report_for_track(payload, output_tracks[0])),
        "version": 2,
        "mode": "keep",
        "games": sorted(selected_games.values(), key=lambda g: g.get("start") or 0),
        "keep_islands": sorted(by_game.values(), key=lambda i: i["start"]),
    }
    result.pop("packaging_outputs", None)  # a child must not inherit sibling/parent cover associations
    # Never inherit the parent's output/YouTube identity or export artifact.
    result["automation"] = {"job_id": identity, "hero_group": group}
    return result


def prepare_outputs(
    payload: dict[str, Any], output_dir: Path, groups: list[str] | None = None, *, source: str | None = None
) -> tuple[list[dict[str, str]], list[str]]:
    from automation.exporter import ProjectExportPlanner

    output_dir = output_dir.expanduser().resolve()
    tracks = [
        t for t in payload["tracks"] if not source or str(Path(t["path"]).resolve()) == str(Path(source).resolve())
    ]
    chosen = (
        groups
        if groups is not None
        else sorted({g for t in tracks for g in t.get("gameplay_grouping", {}).get("selected", [])})
    )
    plans, warnings = [], []
    for group in sorted(set(chosen)):
        group = class_id(group) or group
        if not re.fullmatch("[A-Z]{1,4}", group) and not class_id(group):
            raise ValueError("Invalid group/class ID.")
        try:
            child = group_project(payload, group, source=source)
        except ValueError as exc:
            if "no exportable WIN" not in str(exc):
                raise
            warnings.append(str(exc))
            continue
        # Only trusted group ID + UUID forms paths. Manual class names never do.
        directory = output_dir / ("group_" + group + "_" + child["project_id"][:12])
        directory.mkdir(parents=True, exist_ok=False)
        project = directory / "edit.autocutter"
        # Resolve the same settings as the shared renderer (including legacy projects).
        extension = ProjectExportPlanner._settings_from_payload(child).output_extension()
        output = directory / ("group_" + group + extension)
        atomic_json(project, make_payload_portable(child, project))
        plans.append(
            {
                "id": child["project_id"],
                "group": group,
                "project": str(project),
                "output": str(output),
                "title": child["project_name"],
            }
        )
    return plans, warnings


def export_outputs(
    plans: list[dict[str, str]],
    *,
    manager: Any = None,
    service: Any = None,
    cancellation: Any = None,
    on_progress: Any = None,
    on_completed: Any = None,
) -> list[dict[str, Any]]:
    from automation.manager import PipelineManager
    from automation.models import PipelineJob, PipelineState
    from automation.exporter import PipelineExportService

    manager = manager or PipelineManager()
    service = service or PipelineExportService(manager)
    results = []
    for plan in plans:
        if cancellation:
            cancellation.raise_if_cancelled()
        project = read_project(Path(plan["project"]))
        job = PipelineJob.create(
            vod_id="hero-group-" + plan["id"],
            vod_url=Path(plan["project"]).as_uri(),
            source_title=plan["title"],
            metadata={"delivery": {"output_path": plan["output"], "force": False}, "hero_group": plan["group"]},
        )
        job.id = plan["id"]
        job.project_path = plan["project"]
        job.local_source_path = project["tracks"][0]["path"]
        job.state = PipelineState.READY_EXPORT
        manager.store.upsert(job)
        try:
            done = service.execute(job.id, cancellation=cancellation, on_progress=on_progress)
        except Exception:
            if cancellation is not None and cancellation.is_cancelled:
                manager.cancel(job.id)
            raise
        done = manager.complete_without_upload(done.id)
        snapshot = Path(plan["project"]).parent / "export_edit.autocutter"
        atomic_json(snapshot, make_payload_portable(project, snapshot))
        result = {**plan, "job_id": done.id, "output": done.export_path, "packaging_project": str(snapshot)}
        results.append(result)
        if on_completed:
            on_completed(result)
    return results
