from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import numpy as np

from analysis.cut_engine import Segment, compute_cuts_from_rms, invert_to_keeps, merge_overlaps
from analysis.intensity_map import map_intensity
from core.presets import normalize_preset_cfg


def threshold_pct_to_amp(
    pct: float,
    rms_min: float,
    rms_max: float,
    rms_eps: float | None = None,
    gain_db: float = 0.0,
    gain_affects_detection: bool = False,
) -> float:
    fraction = max(0.0, min(1.0, float(pct) / 100.0))
    epsilon = max(1e-9, float(rms_max) * 0.001) if rms_eps is None else float(rms_eps)
    threshold = float(rms_min) + fraction * ((float(rms_max) + epsilon) - float(rms_min))
    if gain_affects_detection:
        gain = 10.0 ** (float(gain_db) / 20.0)
        if gain > 1e-9:
            threshold /= gain
    return float(max(0.0, threshold))


def threshold_amp_to_pct(
    amp: float,
    rms_min: float,
    rms_max: float,
    rms_eps: float | None = None,
) -> int:
    epsilon = max(1e-9, float(rms_max) * 0.001) if rms_eps is None else float(rms_eps)
    high = float(rms_max) + epsilon
    if high <= float(rms_min) + 1e-12:
        return 45
    value = (float(amp) - float(rms_min)) / (high - float(rms_min)) * 100.0
    return int(max(0, min(100, round(value))))


def subtract_segments(base: Iterable[Segment], masks: Iterable[Segment], eps: float = 1e-6) -> list[Segment]:
    base_segments = merge_overlaps(list(base))
    mask_segments = merge_overlaps(list(masks))
    remaining: list[Segment] = []
    for segment in base_segments:
        cursor = float(segment.start)
        end = float(segment.end)
        for mask in mask_segments:
            if mask.end <= cursor + eps:
                continue
            if mask.start >= end - eps:
                break
            if mask.start > cursor + eps:
                remaining.append(Segment(cursor, min(float(mask.start), end)))
            cursor = max(cursor, float(mask.end))
            if cursor >= end - eps:
                break
        if cursor < end - eps:
            remaining.append(Segment(cursor, end))
    return merge_overlaps(remaining)


def compute_classic_cuts(
    rms: np.ndarray,
    duration: float,
    hop_s: float,
    cfg: Mapping[str, Any],
    manual_cuts: Iterable[Segment] = (),
    suppressed_cuts: Iterable[Segment] = (),
    threshold_amp: float | None = None,
    rms_min: float | None = None,
    rms_max: float | None = None,
    rms_eps: float | None = None,
) -> tuple[list[Segment], list[Segment]]:
    """Compute the editor's classic workspace without widgets or thread state."""
    config = normalize_preset_cfg(cfg)
    intensity = int(config["intensity"])
    manual = merge_overlaps(list(manual_cuts))
    if intensity < 10:
        return manual, invert_to_keeps(float(duration), manual, min_keep=0.0)
    if rms.size == 0 or duration <= 0.0:
        return manual, invert_to_keeps(float(duration), manual, min_keep=0.0)
    if hop_s <= 0.0:
        raise ValueError("Analysis hop must be positive.")

    low = float(np.min(rms)) if rms_min is None else float(rms_min)
    high = float(np.max(rms)) if rms_max is None else float(rms_max)
    threshold = threshold_amp
    if threshold is None:
        threshold = threshold_pct_to_amp(
            config["threshold_pct"], low, high, rms_eps,
            config["gain_db"], config["gain_affects_detection"],
        )
    min_silence, edge_keep, min_keep = map_intensity(intensity)
    min_silence = max(float(min_silence), max(0.0, float(config["merge_pauses_ms"]) / 1000.0))
    edge_keep = max(float(edge_keep), float(config["post_pad_s"]))
    aggressiveness = int(max(0, min(100, round(20 + (float(intensity) / 100.0) * 70))))
    smoothing_ms = {"Off": 0, "Low": 50, "Medium": 120, "High": 250}.get(config["smoothing_mode"], 120)
    auto = compute_cuts_from_rms(
        rms=rms, duration=float(duration), hop_s=float(hop_s), threshold=float(threshold),
        min_silence=min_silence, edge_keep=edge_keep,
        pre_roll=float(config["pre_pad_s"]), min_cut=float(config["min_cut_s"]),
        aggressiveness=aggressiveness, detection_smoothing_ms=float(smoothing_ms),
        attack_ms=float(config["attack_ms"]), release_ms=float(config["release_ms"]),
        merge_short_pauses_ms=float(config["merge_pauses_ms"]),
    )
    auto = subtract_segments(auto, suppressed_cuts)
    cuts = merge_overlaps(auto + manual)
    return cuts, invert_to_keeps(float(duration), cuts, min_keep=float(min_keep))
