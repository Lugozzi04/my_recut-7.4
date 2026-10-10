"""Kept-content audio energy through the editor's own RMS extraction (voice-band filter).

Only kept pieces are analysed (merged spans, cached per span identity). Levels are
expressed relative to the baseline of the kept content, so a quiet streamer and a
loud one produce comparable "above baseline" values. RMS is evidence of energy,
never of emotion.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

from analysis.audio_service import AnalysisCancellation, AnalysisRequest, extract_rms_window

from .cache import PackagingCache, stable_key
from .timeline import EditMap

HOP_S = 0.05
MERGE_GAP_S = 20.0
RmsExtractor = Callable[..., np.ndarray]


@dataclass
class AudioTrack:
    """dB series of one merged span; frames outside the keeps are NaN."""

    source_index: int
    start: float
    hop_s: float
    db: np.ndarray

    def slice(self, start: float, end: float) -> np.ndarray:
        a = max(0, int((start - self.start) / self.hop_s))
        b = min(len(self.db), int(np.ceil((end - self.start) / self.hop_s)))
        return self.db[a:b] if b > a else self.db[:0]


@dataclass
class AudioProfile:
    status: str
    detail: str = ""
    tracks: list[AudioTrack] = field(default_factory=list)
    baseline_db: float = float("nan")
    loud_db: float = float("nan")  # 95th percentile of kept frames

    @property
    def available(self) -> bool:
        return self.status in ("ok", "cached") and bool(self.tracks)

    def features(self, source_index: int, start: float, end: float) -> dict[str, float]:
        values = [t.slice(start, end) for t in self.tracks if t.source_index == source_index]
        frames = np.concatenate(values) if values else np.zeros(0)
        frames = frames[np.isfinite(frames)]
        if not self.available or frames.size == 0:
            return {}
        rel = frames - self.baseline_db
        smooth = np.convolve(rel, np.ones(10) / 10.0, mode="same") if rel.size >= 10 else rel
        return {"mean_db_above_baseline": float(np.mean(rel)), "peak_db_above_baseline": float(np.max(smooth)),
                "loud_ratio": float(np.mean(frames >= self.loud_db)),
                "voiced_ratio": float(np.mean(rel > 3.0))}

    def peaks(self, *, min_rel_db: float = 6.0, min_gap_s: float = 8.0, limit: int = 40) -> list[tuple[int, float, float]]:
        """(source_index, time, smoothed dB above baseline) of the strongest 1-second energy peaks."""
        found: list[tuple[int, float, float]] = []
        if not self.available:
            return found
        width = max(1, int(round(1.0 / HOP_S)))
        for track in self.tracks:
            series = np.where(np.isfinite(track.db), track.db - self.baseline_db, -60.0)
            if series.size < width:
                continue
            smooth = np.convolve(series, np.ones(width) / width, mode="same")
            order = np.argsort(smooth)[::-1]
            taken: list[float] = []
            for i in order[: max(2000, limit * 50)]:
                value = float(smooth[i])
                if value < min_rel_db:
                    break
                t = track.start + i * track.hop_s
                if all(abs(t - other) >= min_gap_s for other in taken):
                    taken.append(t)
                    found.append((track.source_index, t, value))
        found.sort(key=lambda item: item[2], reverse=True)
        return found[:limit]

    def summary(self) -> dict[str, Any]:
        return {"status": self.status, "detail": self.detail, "hop_s": HOP_S, "spans": len(self.tracks),
                "baseline_db": None if not np.isfinite(self.baseline_db) else round(self.baseline_db, 2),
                "loud_db_p95": None if not np.isfinite(self.loud_db) else round(self.loud_db, 2)}


def analyze_kept_audio(edit_map: EditMap, *, cache: PackagingCache, extractor: RmsExtractor | None = None,
                       cancellation: AnalysisCancellation | None = None) -> AudioProfile:
    extractor = extractor or extract_rms_window
    tracks: list[AudioTrack] = []
    hits = 0
    try:
        for index, source in enumerate(edit_map.sources):
            for a, b in edit_map.spans(index, merge_gap=MERGE_GAP_S):
                key = stable_key("audio_rms_db", edit_map.range_digest(index, a, b), a, b, HOP_S)
                cached = cache.load_json("evidence", key)
                if isinstance(cached, list):
                    db = np.array([np.nan if v is None else v for v in cached], dtype=np.float32)
                    hits += 1
                else:
                    rms = np.asarray(extractor(AnalysisRequest(source, hop_s=HOP_S), start_s=a, end_s=b,
                                               cancellation=cancellation), dtype=np.float64)
                    db = (20.0 * np.log10(np.maximum(rms, 1e-6))).astype(np.float32)
                    times = a + np.arange(db.size) * HOP_S
                    kept = np.zeros(db.size, dtype=bool)
                    for ka, kb in edit_map.kept_parts(index, a, b):
                        kept |= (times >= ka) & (times < kb)
                    db[~kept] = np.nan
                    cache.store_json("evidence", key, [None if not np.isfinite(v) else round(float(v), 2) for v in db])
                tracks.append(AudioTrack(index, a, HOP_S, db))
    except Exception as exc:  # optional evidence: the pipeline continues without audio
        from analysis.audio_service import AnalysisCancelled
        if isinstance(exc, AnalysisCancelled):
            raise
        return AudioProfile("failed", f"{type(exc).__name__}: {exc}"[:300])
    values = np.concatenate([t.db for t in tracks]) if tracks else np.zeros(0)
    values = values[np.isfinite(values)]
    if values.size == 0:
        return AudioProfile("unavailable", "no kept audio frames", tracks)
    return AudioProfile("cached" if hits == len(tracks) else "ok", "", tracks,
                        float(np.median(values)), float(np.percentile(values, 95)))
