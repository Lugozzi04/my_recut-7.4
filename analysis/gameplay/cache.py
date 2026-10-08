from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from analysis.gameplay.models import GameplayAnalysisResult
from core.project_file import media_fingerprint


CACHE_VERSION = 1


def video_identity(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
        "fingerprint": media_fingerprint(path),
    }


def cache_key(identity: Mapping[str, Any], detector: Mapping[str, Any], settings: Mapping[str, Any]) -> str:
    value = {"version": CACHE_VERSION, "video": dict(identity), "detector": dict(detector), "settings": dict(settings)}
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode("utf-8")).hexdigest()


def atomic_write_json(path: str | Path, payload: Mapping[str, Any]) -> Path:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix=f".{target.name}.", suffix=".tmp",
            dir=target.parent, delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(dict(payload), stream, ensure_ascii=True, allow_nan=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        return target
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class GameplayCache:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory).expanduser().resolve()

    def load(self, key: str) -> GameplayAnalysisResult | None:
        try:
            raw = json.loads((self.directory / f"{key}.json").read_text(encoding="utf-8"))
            if (raw.get("format") != "recut_gameplay_cache" or type(raw.get("version")) is not int
                    or raw.get("version") != CACHE_VERSION or raw.get("key") != key):
                return None
            result = GameplayAnalysisResult.from_mapping(raw["result"])
            if any(event.timestamp > result.duration for event in [*result.events, *(marker for game in result.games for marker in game.markers)]):
                return None
            if any(
                boundary is not None and boundary > result.duration
                for game in result.games for boundary in (game.start, game.end)
            ):
                return None
            return result
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return None

    def save(self, key: str, result: GameplayAnalysisResult) -> None:
        atomic_write_json(self.directory / f"{key}.json", {
            "format": "recut_gameplay_cache", "version": CACHE_VERSION, "key": key,
            "result": result.to_mapping(),
        })
