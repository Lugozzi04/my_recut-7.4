"""Cheap global visual-transition evidence over kept content: keyframe-only decode.

Decoding only keyframes (-skip_frame nokey) of the kept spans at 160x90 gray
gives one real frame every GOP (about 2 s on Twitch VODs) for a fraction of a
full decode. Consecutive keyframe differences mark scene/board transitions.
Frame-accurate visuals for thumbnails come later from FrameExtractor bursts.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from analysis.audio_service import AnalysisCancellation, AnalysisCancelled

from .cache import PackagingCache, stable_key
from .timeline import EditMap

W, H = 160, 90
MERGE_GAP_S = 20.0
_PTS = re.compile(r"pts_time:\s*([0-9.]+)")


@dataclass
class VisualProfile:
    status: str
    detail: str = ""
    samples: list[tuple[int, float, float]] = field(default_factory=list)  # (source, time, change 0..1 vs previous)

    def peaks(self, *, min_change: float = 0.18, limit: int = 30) -> list[tuple[int, float, float]]:
        strong = sorted((s for s in self.samples if s[2] >= min_change), key=lambda s: s[2], reverse=True)
        return strong[:limit]

    def change_near(self, source_index: int, start: float, end: float) -> float:
        values = [c for i, t, c in self.samples if i == source_index and start <= t <= end]
        return max(values) if values else 0.0

    def summary(self) -> dict[str, Any]:
        return {"status": self.status, "detail": self.detail, "keyframes": len(self.samples),
                "engine": "ffmpeg keyframes 160x90"}


def keyframe_scan(source: str, start: float, end: float, *, cancellation: AnalysisCancellation | None = None
                  ) -> list[tuple[float, np.ndarray]]:
    from utils.ffmpeg import ensure_ffmpeg
    from utils.subprocess_utils import popen_no_window

    token = cancellation or AnalysisCancellation()
    token.raise_if_cancelled()
    ffmpeg, _ = ensure_ffmpeg()
    command = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "info", "-skip_frame", "nokey", "-copyts",
               "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", source, "-map", "0:v:0", "-an",
               "-vf", f"scale={W}:{H},format=gray,showinfo", "-fps_mode", "passthrough", "-f", "rawvideo", "-"]
    process = popen_no_window(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    token.attach_process(process)
    try:
        stdout, stderr = process.communicate()
    finally:
        token.detach_process(process)
    if token.is_cancelled:
        raise AnalysisCancelled()
    if process.returncode != 0:
        raise RuntimeError(((stderr or b"").decode("utf-8", "ignore").strip().splitlines() or ["ffmpeg failed"])[-1])
    times = [float(m) for m in _PTS.findall((stderr or b"").decode("utf-8", "ignore"))]
    count = min(len(times), len(stdout) // (W * H))
    frames = np.frombuffer(stdout[: count * W * H], dtype=np.uint8).reshape(count, H, W)
    return [(times[i], frames[i]) for i in range(count)]


def analyze_kept_visual(edit_map: EditMap, *, cache: PackagingCache, scanner: Any = None,
                        cancellation: AnalysisCancellation | None = None) -> VisualProfile:
    scanner = scanner or keyframe_scan
    samples: list[tuple[int, float, float]] = []
    try:
        for index, source in enumerate(edit_map.sources):
            for a, b in edit_map.spans(index, merge_gap=MERGE_GAP_S):
                key = stable_key("keyframe_change", edit_map.range_digest(index, a, b), a, b, W, H)
                cached = cache.load_json("evidence", key)
                if not isinstance(cached, list):
                    frames = scanner(source, a, b, cancellation=cancellation)
                    cached, previous = [], None
                    for t, image in frames:
                        change = 0.0 if previous is None else float(
                            np.mean(np.abs(image.astype(np.int16) - previous.astype(np.int16)))) / 64.0
                        previous = image
                        if edit_map.to_edited(t, index) is not None:
                            cached.append([round(t, 3), round(min(1.0, change), 4)])
                    cache.store_json("evidence", key, cached)
                samples.extend((index, float(t), float(c)) for t, c in cached)
    except AnalysisCancelled:
        raise
    except Exception as exc:  # optional evidence
        return VisualProfile("failed", f"{type(exc).__name__}: {exc}"[:300], samples)
    return VisualProfile("ok" if samples else "unavailable", "", samples)
