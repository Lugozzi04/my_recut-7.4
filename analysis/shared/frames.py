"""On-demand selective lossless frames; stream/burst caches stay owned by their consumers."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
from zipfile import BadZipFile
from typing import Any

import numpy as np

from analysis.audio_service import AnalysisCancellation, AnalysisCancelled
from analysis.gameplay.cache import video_identity
from analysis.gameplay.hearthstone.visual.sampler import FFmpegFrameSampler, SampledFrame
from analysis.performance_profile import current_profile, measure, record
from utils.runtime_paths import cache_root


class FrameSamplingService:
    def __init__(self, sampler: Any = None, *, directory: str | Path | None = None,
                 max_gap_s: float = 3.0, max_span_s: float = 24.0, enabled: bool = True) -> None:
        self.sampler = sampler or FFmpegFrameSampler()
        self.directory = Path(directory) if directory is not None else cache_root() / "selected_frames"
        self.max_gap_s, self.max_span_s = max_gap_s, max_span_s
        self.enabled = enabled

    def probe(self, *args: Any, **kwargs: Any) -> Any:
        return self.sampler.probe(*args, **kwargs)

    def sample(self, *args: Any, **kwargs: Any) -> Any:
        # Packaging retains its existing source burst cache; do not duplicate it here.
        record("shared.frames", stream_requests=1)
        return self.sampler.sample(*args, **kwargs)

    def sample_roi(self, *args: Any, **kwargs: Any) -> Any:
        return self.sampler.sample_roi(*args, **kwargs)

    def _single(self, source: str | Path, timestamp: float, width: int,
                token: AnalysisCancellation) -> SampledFrame | None:
        with closing(self.sampler.sample(source, fps=1.0, start_s=timestamp,
                                        end_s=timestamp + 0.10, max_width=width, cancellation=token)) as frames:
            return next(frames, None)

    def get_many(self, source: str | Path, timestamps: list[float], *, max_width: int = 960,
                 cancellation: AnalysisCancellation | None = None) -> dict[float, SampledFrame | None]:
        token = cancellation or AnalysisCancellation()
        token.raise_if_cancelled()
        if any(not np.isfinite(t) or t < 0 for t in timestamps):
            raise ValueError("Invalid requested timestamp")
        if isinstance(max_width, bool) or not isinstance(max_width, int) or not 16 <= max_width <= 4096:
            raise ValueError("Invalid max_width")
        identity = video_identity(Path(source))
        out: dict[float, SampledFrame | None] = {}
        paths = {}
        missing = []
        record("shared.frames", requested_frames=len(timestamps))
        for timestamp in sorted(set(timestamps)):
            token.raise_if_cancelled()
            descriptor = {"schema": 1, "algorithm": "first-real-frame-v1", "source": identity,
                          "timestamp": timestamp, "window_s": 0.10, "max_width": max_width}
            key = hashlib.sha256(json.dumps(descriptor, sort_keys=True).encode()).hexdigest()
            target = self.directory / (key + ".npz")
            paths[timestamp] = (target, descriptor)
            if self.enabled:
                try:
                    with np.load(target, allow_pickle=False) as data:
                        image, pts = data["image"].copy(), float(data["timestamp"])
                        if (json.loads(str(data["metadata"].item())) != descriptor or image.dtype != np.uint8
                                or image.ndim != 3 or image.shape[2] != 3 or not image.size
                                or not timestamp - 1e-6 <= pts < timestamp + 0.10 + 1e-6):
                            raise ValueError("Incompatible frame")
                    out[timestamp] = SampledFrame(pts, image)
                    record("shared.frames", cached_frames=1)
                    continue
                except (OSError, ValueError, KeyError, TypeError, BadZipFile, EOFError):
                    pass
            missing.append(timestamp)
        groups: list[list[float]] = []
        for timestamp in missing:
            if groups and timestamp - groups[-1][-1] <= self.max_gap_s and timestamp - groups[-1][0] <= self.max_span_s:
                groups[-1].append(timestamp)
            else:
                groups.append([timestamp])
        profile = current_profile()
        if profile:
            profile.cache("shared.frames", "hit" if not missing else "miss" if not out else "partial")
        for group in groups:
            token.raise_if_cancelled()
            with measure("shared.frames.extraction"):
                if len(group) > 1 and callable(getattr(self.sampler, "sample_at", None)):
                    try:
                        record("shared.frames", batch_sessions=1)
                        frames = list(self.sampler.sample_at(source, group, max_width=max_width, cancellation=token))
                        for t in group:
                            out[t] = next((f for f in frames if t - 1e-6 <= f.timestamp < t + 0.10 + 1e-6), None)
                    except AnalysisCancelled:
                        raise
                    except Exception:
                        record("shared.frames", batch_fallbacks=1)
                        for t in group:
                            out[t] = self._single(source, t, max_width, token)
                else:
                    for t in group:
                        record("shared.frames", single_sessions=1)
                        out[t] = self._single(source, t, max_width, token)
            for t in group:
                token.raise_if_cancelled()
                frame = out[t]
                if frame is None:
                    continue
                record("shared.frames", decoded_frames=1)
                if not self.enabled:
                    continue
                target, descriptor = paths[t]
                from .arrays import atomic_save_arrays
                try:
                    atomic_save_arrays(target, metadata=json.dumps(descriptor, sort_keys=True),
                                       timestamp=frame.timestamp, image=frame.image)
                except OSError:
                    record("shared.frames", write_failures=1)
        token.raise_if_cancelled()
        return out
