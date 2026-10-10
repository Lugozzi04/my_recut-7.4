"""Local silence/speech evidence reusing the editor's RMS silence engine.

Silences come from analysis.cut_engine.compute_cuts_from_rms with zero padding
(raw silent runs >= min_silence), on RMS extracted ONLY for the search window
by analysis.audio_service.extract_rms_window (same voice filter and framing as
the full-file analysis). Parameters default to the editor's default preset.
Speech is the complement of silence inside the window, or optionally Silero
VAD through the existing ai_pipeline runner.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
import tempfile
from typing import Any

import numpy as np

from analysis.audio_service import AnalysisCancellation, AnalysisRequest, auto_threshold_from_rms, extract_rms_window
from analysis.cut_engine import compute_cuts_from_rms
from analysis.intensity_map import map_intensity
from core.presets import default_presets_catalog, normalize_preset_cfg
from .models import EvidenceStatus, TimeRange

SMOOTHING_MS = {"Off": 0, "Low": 50, "Medium": 120, "High": 250}  # same mapping as analysis.classic


@dataclass(frozen=True)
class SilenceSettings:
    hop_s: float = 0.03
    sample_rate: int = 16000
    threshold: float | None = None  # amplitude; None = editor auto threshold on the local RMS
    min_silence_s: float = 0.48
    smoothing_ms: float = 120.0
    attack_ms: float = 120.0
    release_ms: float = 250.0
    speech_engine: str = "rms"  # rms | silero
    padding_s: float = 1.0  # extra audio on both sides so smoothing/debounce do not start cold at the window edge

    @classmethod
    def from_preset(cls, cfg: Mapping[str, Any] | None = None, **overrides: Any) -> SilenceSettings:
        """Same derivation as analysis.classic.compute_classic_cuts (min_silence, smoothing, attack/release)."""
        if cfg is None:
            cfg = default_presets_catalog()["Balanced (Default)"]
        config = normalize_preset_cfg(cfg)
        min_silence, _, _ = map_intensity(int(config["intensity"]))
        values: dict[str, Any] = {"min_silence_s": max(float(min_silence), float(config["merge_pauses_ms"]) / 1000.0),
                  "smoothing_ms": float(SMOOTHING_MS.get(config["smoothing_mode"], 120)),
                  "attack_ms": float(config["attack_ms"]), "release_ms": float(config["release_ms"])}
        values.update(overrides)
        return cls(**values)


@dataclass
class AudioEvidence:
    status: EvidenceStatus
    silences: list[TimeRange]
    speech: list[TimeRange]
    rms: np.ndarray | None = None
    rms_start: float = 0.0
    hop_s: float = 0.03
    threshold: float | None = None

    def mean_db(self, start: float, end: float) -> float | None:
        if self.rms is None or self.rms.size == 0 or end <= start:
            return None
        lo = max(0, int((start - self.rms_start) / self.hop_s))
        hi = min(self.rms.size, int(np.ceil((end - self.rms_start) / self.hop_s)))
        if hi <= lo:
            return None
        return float(20.0 * np.log10(max(float(np.mean(self.rms[lo:hi])), 1e-6)))


def silences_from_rms(rms: np.ndarray, *, offset: float, settings: SilenceSettings,
                      threshold: float) -> list[TimeRange]:
    duration = float(rms.size) * settings.hop_s
    raw = compute_cuts_from_rms(rms=rms, duration=duration, hop_s=settings.hop_s, threshold=threshold,
                                min_silence=settings.min_silence_s, edge_keep=0.0, pre_roll=0.0, min_cut=0.0,
                                aggressiveness=100, detection_smoothing_ms=settings.smoothing_ms,
                                attack_ms=settings.attack_ms, release_ms=settings.release_ms)
    return [TimeRange(offset + s.start, offset + s.end) for s in raw]


def complement(window: TimeRange, regions: list[TimeRange]) -> list[TimeRange]:
    out, cursor = [], window.start
    for region in sorted(regions, key=lambda r: r.start):
        if region.start > cursor:
            out.append(TimeRange(cursor, min(region.start, window.end)))
        cursor = max(cursor, region.end)
    if cursor < window.end:
        out.append(TimeRange(cursor, window.end))
    return [r for r in out if r.duration > 0]


def _silero_speech(video: Path, window: TimeRange, cancellation: AnalysisCancellation | None) -> list[TimeRange]:
    from analysis.ai_pipeline import _run_silero_vad, find_silero_python
    from .media import extract_wav

    with tempfile.TemporaryDirectory(prefix="recut-boundary-vad-") as folder:
        wav = Path(folder) / "window.wav"
        extract_wav(video, window, wav, cancellation=cancellation)
        segments = _run_silero_vad(wav, 0.5, 0.3, 0.2, silero_python=find_silero_python())
    return [TimeRange(window.start + s.start, min(window.end, window.start + s.end)) for s in segments]


def analyze_audio_window(video: str | Path, window: TimeRange, settings: SilenceSettings, *,
                         cancellation: AnalysisCancellation | None = None) -> AudioEvidence:
    status_settings = {**asdict(settings)}
    request = AnalysisRequest(str(video), hop_s=settings.hop_s, sample_rate=settings.sample_rate)
    padded_start = max(0.0, window.start - settings.padding_s)
    try:
        rms = extract_rms_window(request, start_s=padded_start, end_s=window.end + settings.padding_s,
                                 cancellation=cancellation)
    except Exception as exc:  # degrade: no audio evidence, never invented silence
        if exc.__class__.__name__ == "AnalysisCancelled":
            raise
        return AudioEvidence(EvidenceStatus("failed", "rms", str(exc), status_settings), [], [])
    if rms.size < 2:
        return AudioEvidence(EvidenceStatus("unavailable", "rms", "window has no decodable audio", status_settings), [], [])
    threshold = settings.threshold if settings.threshold is not None else auto_threshold_from_rms(rms)
    silences = [TimeRange(max(r.start, window.start), min(r.end, window.end))
                for r in silences_from_rms(rms, offset=padded_start, settings=settings, threshold=threshold)
                if r.end > window.start and r.start < window.end]
    engine = "rms"
    speech = complement(window, silences)
    detail = f"threshold={threshold:.5f} ({'given' if settings.threshold is not None else 'local auto'})"
    if settings.speech_engine == "silero":
        try:
            speech, engine = _silero_speech(Path(video), window, cancellation), "rms+silero"
        except Exception as exc:
            detail += f"; silero unavailable, speech = RMS complement ({exc})"
    return AudioEvidence(EvidenceStatus("ok", engine, detail, status_settings), silences, speech,
                         rms, padded_start, settings.hop_s, threshold)
