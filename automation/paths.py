from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from automation.locking import ProcessFileLock
from automation.manager import PipelineManager
from automation.models import PipelineJob
from export.output_safety import choose_output_path, ensure_output_is_safe
from export.settings import ExportSettings
from utils.runtime_paths import exports_root


_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.IGNORECASE)


def safe_output_name(title: str) -> str:
    """A remote title is one Windows-safe filename component, never a path."""
    name = _INVALID.sub("_", str(title)).strip().rstrip(". ")
    if not name or name in {".", ".."}:
        name = "video"
    if _RESERVED.match(name):
        name = "_" + name
    # Count Windows UTF-16 units, including astral characters, leaving room for suffixes.
    while len(name.encode("utf-16-le", errors="replace")) > 320:
        name = name[:-1]
    return name.rstrip(". ") or "video"


def output_execution_lock(output: Path) -> ProcessFileLock:
    key = hashlib.sha256(os.path.normcase(str(output.resolve())).encode("utf-8")).hexdigest()
    return ProcessFileLock(output.parent / ".autocutter-locks" / f"{key}.lock")


def requested_output(job: PipelineJob) -> Path:
    delivery = job.metadata.get("delivery", {})
    if delivery.get("output_path"):
        return Path(delivery["output_path"]).expanduser().resolve()
    folder = Path(delivery.get("output_dir") or exports_root()).expanduser().resolve()
    settings = ExportSettings.from_mapping(job.metadata.get("export_settings"))
    return folder / f"{safe_output_name(job.source_title or job.vod_id)}{settings.output_extension()}"


def reserve_output(manager: PipelineManager, job: PipelineJob) -> Path:
    """Persist a collision-free destination before FFmpeg can create any artifact."""
    delivery = job.metadata.get("delivery", {})
    protected = tuple(Path(p) for p in (job.local_source_path, job.project_path) if p)
    resolved = delivery.get("resolved_output_path")
    if resolved:
        return ensure_output_is_safe(resolved, protected)
    requested = requested_output(job)
    allocation_lock = ProcessFileLock(requested.parent / ".autocutter-locks" / "allocation.lock")
    with allocation_lock.hold(timeout=10):
        # Re-read in case the same job was reserved since the caller's snapshot.
        current = manager.get(job.id)
        resolved = current.metadata.get("delivery", {}).get("resolved_output_path")
        if resolved:
            return ensure_output_is_safe(resolved, protected)
        others = {
            os.path.normcase(str(Path(value).resolve()))
            for other in manager.list_jobs()
            if other.id != job.id
            for value in [other.metadata.get("delivery", {}).get("resolved_output_path")]
            if value
        }
        force = delivery.get("force") is True
        candidate = choose_output_path(requested, force=force, protected=protected)
        index = 2
        while not force and os.path.normcase(str(candidate)) in others:
            candidate = choose_output_path(
                requested.with_name(f"{requested.stem}_{index}{requested.suffix}"), protected=protected,
            )
            index += 1
        manager.update_metadata(job.id, {"delivery": {"resolved_output_path": str(candidate)}})
        return candidate
