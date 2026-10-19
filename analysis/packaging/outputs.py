"""Packaging for exported edits: one existing pipeline/run per immutable child project."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Mapping

from analysis.audio_service import AnalysisCancellation
from analysis.gameplay.cache import atomic_write_json
from .pipeline import PackagingPipeline, PackagingSettings


def find_previous_export(video: str | Path, known_outputs: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """Recover an exact export association locally; never guess from a parent project backup.

    Search only the selected file's directory and its named ReCut artifact siblings.
    Ambiguous snapshots or unavailable original media fall back to the final video.
    No probing, decoding, provider or detection is performed here.
    """
    video = Path(video).expanduser().resolve()
    if not video.is_file():
        raise ValueError("Choose an existing exported video.")
    from analysis.hero_groups.projects import read_project

    def usable(entry: dict[str, Any], base: Path) -> dict[str, Any] | None:
        item = deepcopy(entry)
        for key in ("output", "project", "packaging_project", "packaging"):
            if item.get(key):
                item[key] = str((base / str(item[key])).resolve())
        if Path(str(item.get("output", ""))).resolve() != video:
            return None
        project = item.get("packaging_project") or item.get("project")
        if not project or not Path(project).is_file():
            return None
        try:
            if not read_project(Path(project)).get("tracks"):  # validate sources without decoding media
                return None
        except (OSError, ValueError, KeyError, TypeError):
            return None
        item.setdefault("id", hashlib.sha256(str(video).encode("utf-8")).hexdigest()[:24])
        item.setdefault("project", project)
        return item

    for known in known_outputs or []:
        if found := usable(known, video.parent):
            return found
    directories = [video.parent, *sorted(p for p in video.parent.iterdir()
                                        if p.is_dir() and p.name.startswith(video.stem + "_recut_"))]
    found_entries = []
    for folder in directories:
        request = folder / "packaging_request.json"
        try:
            raw = json.loads(request.read_text(encoding="utf-8"))
            entry = usable(raw.get("export") or {}, folder)
        except (OSError, ValueError, AttributeError, TypeError):
            entry = None
        if entry:
            found_entries.append(entry)
            continue
        snapshot = folder / "export_edit.autocutter"
        # UUID class folders contain their output; named siblings identify a single-video output.
        try:
            media = [p.resolve() for p in folder.iterdir() if p.is_file() and
                     p.suffix.lower() in (".mp4", ".mkv", ".mov", ".webm", ".ts", ".avi", ".m4v")]
        except OSError:
            continue
        if not snapshot.is_file() or (folder == video.parent and media != [video]):
            continue
        try:
            payload = read_project(snapshot)
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if not payload.get("tracks"):
            continue
        found_entries.append({"id": str(payload.get("project_id") or hashlib.sha256(str(snapshot).encode()).hexdigest()[:24]),
                              "project": str(snapshot), "packaging_project": str(snapshot), "output": str(video),
                              "title": str(payload.get("project_name") or video.stem),
                              "group": str(payload.get("hero_group_output", {}).get("group") or "selected")})
    unique = {str(Path(e.get("packaging_project") or e["project"]).resolve()): e for e in found_entries}
    return next(iter(unique.values())) if len(unique) == 1 else None


def settings_for_previous_video(video: str | Path, options: Mapping[str, Any]) -> PackagingSettings:
    """A standalone final render works even after its original project/VODs are gone."""
    video = Path(video).expanduser().resolve()
    if not video.is_file():
        raise ValueError("Choose an existing exported video.")
    return PackagingSettings(output=str(video.with_name(video.stem + "_packaging")), video=str(video),
                             games="none", overwrite=True, provider=str(options.get("provider", "codex")),
                             language=str(options.get("language", "it")),
                             thumbnail_engine=str(options.get("thumbnail_engine", "gen")),
                             creative_direction=deepcopy(dict(options.get("creative_direction") or {})))


def settings_for_output(output: Mapping[str, Any], options: Mapping[str, Any]) -> PackagingSettings:
    project = Path(str(output.get("packaging_project") or output["project"])).expanduser().resolve()
    if not project.is_file():
        raise ValueError(f"The exported edit project is missing: {project}")
    # Legacy exports may keep their edit in cache. Copy it beside the actual video
    # before starting new packaging; existing cached runs are not moved or deleted.
    video = Path(str(output["output"])).expanduser().resolve()
    if not project.parent.is_relative_to(video.parent):
        from analysis.hero_groups.projects import read_project
        from export.artifacts import save_snapshot
        directory = video.parent / (video.stem + "_recut_" + str(output.get("id") or "legacy")[:12])
        copied = directory / "export_edit.autocutter"
        if not copied.is_file():
            save_snapshot(read_project(project), copied)
        project = copied
    # The child project UUID already owns this directory; never use a parent/VOD-wide run.
    return PackagingSettings(output=str(project.parent / "packaging"), project=str(project),
                             games="project", overwrite=True,
                             provider=str(options.get("provider", "codex")),
                             language=str(options.get("language", "it")),
                                                           thumbnail_engine=str(options.get("thumbnail_engine", "gen")),
                              creative_direction=deepcopy(dict(options.get("creative_direction") or output.get("creative_direction") or {})))


def record_status(output: Mapping[str, Any], settings: PackagingSettings, status: str) -> dict[str, Any]:
    """Persist the output association before expensive work. No credentials or provider payloads."""
    record = {**deepcopy(dict(output)), "packaging": settings.output, "packaging_status": status,
                             "packaging_project": settings.project, "creative_direction": deepcopy(settings.creative_direction)}
    atomic_write_json(Path(settings.output).parent / "packaging_request.json", {
        "version": 1, "export": record,
        "options": {"provider": settings.provider, "language": settings.language,
                    "thumbnail_engine": settings.thumbnail_engine},
    })
    return record


def package_outputs(
    outputs: list[dict[str, Any]], options: Mapping[str, Any], *,
    cancellation: AnalysisCancellation | None = None,
    progress: Callable[[str], None] | None = None,
    on_result: Callable[[dict[str, Any]], None] | None = None,
    pipeline_factory: Callable[..., Any] = PackagingPipeline,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Serial, cancellable runs. A cover failure never discards a successfully rendered video."""
    token = cancellation or AnalysisCancellation()
    results, warnings = [], []
    for index, output in enumerate(outputs, 1):
        record = dict(output)
        settings = None
        try:
            settings = settings_for_output(output, options)
            record = record_status(output, settings, "cancelled" if token.is_cancelled else "running")
            if not token.is_cancelled:
                def report(stage: str, message: str) -> None:
                    if progress:
                        progress(f"Cover {index}/{len(outputs)} · {output.get('title', output.get('group', ''))} · {stage}: {message}")
                outcome = pipeline_factory(settings, cancellation=token, progress=report).run()
                for warning in (outcome or {}).get("warnings", []):
                    warnings.append(f"Cover {output.get('group', '')}: {warning}")
                token.raise_if_cancelled()
                record = record_status(output, settings, "done")
        except Exception as exc:
            status = "cancelled" if token.is_cancelled else "failed"
            record = {**record, "packaging_status": status}
            if settings is not None:
                try:
                    record = record_status(output, settings, status)
                except OSError:
                    pass
            if not token.is_cancelled:
                warnings.append(f"Video {output.get('group', '')} exported; cover failed: {exc}")
        results.append(record)
        if on_result:
            on_result(record)
    return results, warnings
