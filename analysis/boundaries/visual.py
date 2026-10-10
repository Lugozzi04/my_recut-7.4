"""Cheap visual-change samples inside a window (no new detector, no model).

Reuses the gameplay FFmpegFrameSampler at a low rate and tiny width; change is
0.5 * mean absolute grayscale difference + 0.5 * histogram total variation, in
0..1, between consecutive sampled frames. Informational only.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

from analysis.audio_service import AnalysisCancellation
from .models import EvidenceStatus, TimeRange, VisualSample


@dataclass(frozen=True)
class VisualSettings:
    enabled: bool = True
    fps: float = 2.0
    width: int = 160


def _gray(image: np.ndarray) -> np.ndarray:
    if image.ndim == 3:
        return (image[..., 0] * 0.114 + image[..., 1] * 0.587 + image[..., 2] * 0.299).astype(np.float32)
    return image.astype(np.float32)


def frame_change(previous: np.ndarray, current: np.ndarray) -> float:
    a, b = _gray(previous), _gray(current)
    if a.shape != b.shape:
        return 1.0
    mad = float(np.mean(np.abs(a - b))) / 255.0
    ha = np.histogram(a, bins=32, range=(0, 256))[0].astype(np.float64)
    hb = np.histogram(b, bins=32, range=(0, 256))[0].astype(np.float64)
    tv = 0.5 * float(np.abs(ha / max(ha.sum(), 1) - hb / max(hb.sum(), 1)).sum())
    return float(min(1.0, 0.5 * mad + 0.5 * tv))


def analyze_visual_window(video: str | Path, window: TimeRange, settings: VisualSettings, *,
                          sampler: Any = None, cancellation: AnalysisCancellation | None = None
                          ) -> tuple[EvidenceStatus, list[VisualSample]]:
    options = asdict(settings)
    if not settings.enabled:
        return EvidenceStatus("disabled", "frame-difference", "", options), []
    try:
        if sampler is None:
            from analysis.gameplay.hearthstone.visual.sampler import FFmpegFrameSampler
            sampler = FFmpegFrameSampler()
        from contextlib import closing

        samples: list[VisualSample] = []
        previous = None
        with closing(sampler.sample(video, fps=settings.fps, start_s=window.start, end_s=window.end,
                                    max_width=settings.width, cancellation=cancellation)) as frames:
            for frame in frames:
                if previous is not None:
                    samples.append(VisualSample(float(frame.timestamp), frame_change(previous, frame.image)))
                previous = frame.image
    except Exception as exc:
        if exc.__class__.__name__ == "AnalysisCancelled":
            raise
        return EvidenceStatus("failed", "frame-difference", str(exc), options), []
    return EvidenceStatus("ok", "frame-difference", f"{len(samples)} transitions", options), samples
