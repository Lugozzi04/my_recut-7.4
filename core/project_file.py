from __future__ import annotations

from copy import deepcopy
import hashlib
import os
from pathlib import Path
from typing import Any


PROJECT_FORMAT = "autocutter_project"
PROJECT_VERSION = 2


class ProjectFormatError(ValueError):
    pass


def normalize_project_payload(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ProjectFormatError("Project file has invalid structure.")

    project_format = raw.get("format")
    if project_format not in (None, "", PROJECT_FORMAT):
        raise ProjectFormatError(f"Unsupported project format: {project_format}")

    try:
        version = int(raw.get("version", 1))
    except (TypeError, ValueError) as exc:
        raise ProjectFormatError("Project version is invalid.") from exc
    if version < 1:
        raise ProjectFormatError(f"Unsupported project version: {version}")
    if version > PROJECT_VERSION:
        raise ProjectFormatError(
            f"This project requires a newer Auto Cutter version (project v{version}, supported v{PROJECT_VERSION})."
        )

    tracks = raw.get("tracks", raw.get("items"))
    if not isinstance(tracks, list) or not tracks:
        raise ProjectFormatError("Project file does not contain tracks.")

    payload = deepcopy(raw)
    payload["format"] = PROJECT_FORMAT
    payload["version"] = PROJECT_VERSION
    payload["tracks"] = [deepcopy(item) for item in tracks if isinstance(item, dict)]
    payload.pop("items", None)
    if not payload["tracks"]:
        raise ProjectFormatError("Project file does not contain valid tracks.")
    return payload


def media_fingerprint(path: Path, sample_bytes: int = 64 * 1024) -> str:
    target = Path(path)
    size = int(target.stat().st_size)
    digest = hashlib.sha256()
    digest.update(str(size).encode("ascii"))
    with target.open("rb") as stream:
        digest.update(stream.read(sample_bytes))
        if size > sample_bytes:
            stream.seek(max(0, size - sample_bytes))
            digest.update(stream.read(sample_bytes))
    return digest.hexdigest()


def make_payload_portable(payload: dict[str, Any], project_path: Path) -> dict[str, Any]:
    normalized = normalize_project_payload(payload)
    project_dir = Path(project_path).resolve().parent

    for item in normalized["tracks"]:
        raw_path = str(item.get("path", "") or "").strip()
        if not raw_path:
            continue
        media_path = Path(raw_path)
        if not media_path.is_absolute():
            media_path = (project_dir / media_path).resolve()
        else:
            media_path = media_path.resolve()

        try:
            relative = os.path.relpath(str(media_path), str(project_dir))
            item["path"] = Path(relative).as_posix()
            item["path_kind"] = "relative"
        except ValueError:
            item["path"] = str(media_path)
            item["path_kind"] = "absolute"

        item["media_name"] = media_path.name
        if media_path.is_file():
            try:
                item["media_size"] = int(media_path.stat().st_size)
                item["media_fingerprint"] = media_fingerprint(media_path)
            except OSError:
                pass
    return normalized


def resolve_project_items(
    items: list[dict[str, Any]],
    project_path: Path | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    base_dir = Path(project_path).resolve().parent if project_path is not None else None
    available: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []

    for original in items:
        item = deepcopy(original)
        raw_path = str(item.get("path", "") or "").strip()
        if not raw_path:
            missing.append(item)
            continue
        candidate = Path(raw_path)
        path_kind = str(item.get("path_kind", "") or "").strip().lower()
        if base_dir is not None and (path_kind == "relative" or not candidate.is_absolute()):
            candidate = base_dir / candidate
        try:
            candidate = candidate.resolve()
        except OSError:
            candidate = candidate.absolute()
        item["path"] = str(candidate)
        if candidate.is_file():
            available.append(item)
        else:
            missing.append(item)
    return available, missing


def relink_items_in_directory(
    items: list[dict[str, Any]],
    search_dir: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    root = Path(search_dir)
    by_name: dict[str, list[Path]] = {}
    if root.is_dir():
        for candidate in root.rglob("*"):
            if candidate.is_file():
                by_name.setdefault(candidate.name.lower(), []).append(candidate)

    resolved: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for original in items:
        item = deepcopy(original)
        expected_name = str(item.get("media_name", "") or "").strip()
        if not expected_name:
            expected_name = Path(str(item.get("path", "") or "")).name
        candidates = by_name.get(expected_name.lower(), [])
        expected_size = item.get("media_size")
        expected_fingerprint = str(item.get("media_fingerprint", "") or "").strip()

        matches: list[Path] = []
        for candidate in candidates:
            try:
                if expected_size is not None and int(candidate.stat().st_size) != int(expected_size):
                    continue
                if expected_fingerprint and media_fingerprint(candidate) != expected_fingerprint:
                    continue
                matches.append(candidate)
            except (OSError, TypeError, ValueError):
                continue

        if len(matches) == 1:
            item["path"] = str(matches[0].resolve())
            item["path_kind"] = "absolute"
            resolved.append(item)
        else:
            unresolved.append(item)
    return resolved, unresolved
