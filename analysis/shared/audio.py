"""Persistent native RMS evidence, without PCM or edit-dependent invalidation."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from zipfile import BadZipFile
from typing import Any

import numpy as np

from analysis.gameplay.cache import video_identity
from analysis.performance_profile import current_profile, record
from utils.runtime_paths import cache_root


class AudioEvidenceStore:
    schema_version = 1
    algorithm_version = "voice-rms-v1"

    def __init__(self, directory: str | Path | None = None) -> None:
        self.directory = Path(directory) if directory is not None else cache_root() / "audio_evidence"

    def descriptor(self, source: str | Path, *, hop_s: float, sample_rate: int, audio_filter: str,
                   start_s: float | None, duration_s: float | None) -> dict[str, Any]:
        return {"schema": self.schema_version, "algorithm": self.algorithm_version,
                "source": video_identity(Path(source)), "hop_s": hop_s, "sample_rate": sample_rate,
                "filter": audio_filter, "start_s": start_s, "duration_s": duration_s}

    def _path(self, descriptor: dict[str, Any]) -> Path:
        key = hashlib.sha256(json.dumps(descriptor, sort_keys=True).encode()).hexdigest()
        return self.directory / (key + ".npz")

    def load(self, descriptor: dict[str, Any]) -> np.ndarray | None:
        try:
            with np.load(self._path(descriptor), allow_pickle=False) as data:
                if json.loads(str(data["metadata"].item())) != descriptor:
                    return None
                rms = data["rms"].copy()
                if rms.ndim != 1 or rms.dtype != np.float32 or not np.isfinite(rms).all():
                    return None
            record("shared.audio", hits=1, rms_blocks=rms.size)
            profile = current_profile()
            if profile:
                profile.cache("shared.audio", "hit")
            return rms
        except (OSError, ValueError, KeyError, TypeError, BadZipFile, EOFError):
            record("shared.audio", misses=1)
            profile = current_profile()
            if profile:
                profile.cache("shared.audio", "miss")
            return None

    def save(self, descriptor: dict[str, Any], rms: np.ndarray) -> None:
        target = self._path(descriptor)
        from .arrays import atomic_save_arrays
        try:
            atomic_save_arrays(target, metadata=json.dumps(descriptor, sort_keys=True),
                               rms=np.asarray(rms, dtype=np.float32))
        except OSError:
            record("shared.audio", write_failures=1)
