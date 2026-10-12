"""Adapters to existing gameplay artifacts and compact local visual evidence."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from analysis.gameplay.cache import GameplayCache, atomic_write_json, video_identity
from analysis.gameplay.models import GameplayAnalysisResult
from analysis.boundaries.models import VisualSample
from analysis.performance_profile import current_profile, record
from utils.runtime_paths import cache_root


class VisualEvidenceStore:
    def __init__(self, directory: str | Path | None = None) -> None:
        self.directory = Path(directory) if directory is not None else cache_root() / "boundaries" / "visual"

    @staticmethod
    def gameplay(cache: GameplayCache, key: str) -> GameplayAnalysisResult | None:
        # The original format/key validates detector settings, ROI and template dependencies.
        result = cache.load(key)
        record("shared.visual", gameplay_hits=int(result is not None), gameplay_misses=int(result is None))
        return result

    def descriptor(self, video: str | Path, start: float, end: float, options: dict[str, Any]) -> dict[str, Any]:
        return {"schema": 1, "algorithm": "boundary-frame-change-v1", "source": video_identity(Path(video)),
                "range": [start, end], "options": options}

    def _path(self, descriptor: dict[str, Any]) -> Path:
        key = hashlib.sha256(json.dumps(descriptor, sort_keys=True).encode()).hexdigest()
        return self.directory / (key + ".json")

    def load(self, descriptor: dict[str, Any]) -> list[dict[str, Any]] | None:
        try:
            raw = json.loads(self._path(descriptor).read_text(encoding="utf-8"))
            if raw["descriptor"] != descriptor or not isinstance(raw["samples"], list):
                return None
            # Validate compact artifacts before returning a hit; corrupt caches fall back.
            for sample in raw["samples"]:
                VisualSample.from_mapping(sample)
            record("shared.visual", window_hits=1)
            profile = current_profile()
            if profile:
                profile.cache("shared.visual", "hit")
            return raw["samples"]
        except (OSError, ValueError, KeyError, TypeError):
            record("shared.visual", window_misses=1)
            profile = current_profile()
            if profile:
                profile.cache("shared.visual", "miss")
            return None

    def save(self, descriptor: dict[str, Any], samples: list[dict[str, Any]]) -> None:
        try:
            atomic_write_json(self._path(descriptor), {"descriptor": descriptor, "samples": samples})
        except OSError:
            record("shared.visual", write_failures=1)
