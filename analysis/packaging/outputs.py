"""Packaging for exported edits: one existing pipeline/run per immutable child project."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Mapping

from analysis.audio_service import AnalysisCancellation
from analysis.gameplay.cache import atomic_write_json
from .pipeline import PackagingPipeline, PackagingSettings


def settings_for_output(output: Mapping[str, Any], options: Mapping[str, Any]) -> PackagingSettings:
    project = Path(str(output.get("packaging_project") or output["project"])).expanduser().resolve()
    if not project.is_file():
        raise ValueError(f"The exported edit project is missing: {project}")
    # The child project UUID already owns this directory; never use a parent/VOD-wide run.
    return PackagingSettings(output=str(project.parent / "packaging"), project=str(project),
                             games="project", overwrite=True,
                             provider=str(options.get("provider", "codex")),
                             language=str(options.get("language", "it")),
                             thumbnail_engine=str(options.get("thumbnail_engine", "gen")))


def record_status(output: Mapping[str, Any], settings: PackagingSettings, status: str) -> dict[str, Any]:
    """Persist the output association before expensive work. No credentials or provider payloads."""
    record = {**deepcopy(dict(output)), "packaging": settings.output, "packaging_status": status}
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
