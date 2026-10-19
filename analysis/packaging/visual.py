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


# Hearthstone play area of a full-screen 16:9 capture (normalized x0, y0, x1, y1): both boards, the hero
# portraits and the played-card zone; hands, deck/mana trackers and the stream overlay corners stay outside.
BOARD_REGION = (0.2, 0.17, 0.8, 0.83)
PIXEL_CHANGE = 24  # gray levels: a pixel counts as changed above this (keyframe noise stays below)
MAX_PAIR_GAP_S = 5.0  # keyframes further apart (or across a cut) are not compared


def board_mask(exclude: tuple[float, float, float, float] | None = None, *, margin: float = 0.02) -> np.ndarray:
    """Pixels of the W x H keyframe inside BOARD_REGION, minus the webcam ROI (x, y, w, h) with a margin."""
    ys, xs = (np.arange(H)[:, None] + 0.5) / H, (np.arange(W)[None, :] + 0.5) / W
    x0, y0, x1, y1 = BOARD_REGION
    mask = (xs >= x0) & (xs < x1) & (ys >= y0) & (ys < y1)
    if exclude is not None:
        ex, ey, ew, eh = exclude
        mask &= ~((xs >= ex - margin) & (xs < ex + ew + margin) & (ys >= ey - margin) & (ys < ey + eh + margin))
    return mask


def board_change(previous: np.ndarray, image: np.ndarray, mask: np.ndarray) -> float:
    """Share of board pixels that changed (0..1): a localized board event counts, a small brightness drift does
    not. Raw value: the board always animates a little, so consumers rank it within the video."""
    if not mask.any():
        return 0.0
    diff = np.abs(image.astype(np.int16) - previous.astype(np.int16))[mask]
    return float(np.mean(diff > PIXEL_CHANGE))


def analyze_kept_board(edit_map: EditMap, *, cache: PackagingCache, exclude: tuple[float, float, float, float] | None
                       = None, scanner: Any = None, cancellation: AnalysisCancellation | None = None) -> VisualProfile:
    """Keyframe change on the game board only (webcam ROI `exclude` and overlays masked), kept content only.

    Same keyframe decode as analyze_kept_visual, cached separately. Keyframes more than MAX_PAIR_GAP_S of
    source time apart (a long removed part inside a merged span) are not compared, so a big cut never reads
    as a board swing; the short silence cuts of the edit hide only seconds of play and are compared."""
    scanner = scanner or keyframe_scan
    mask = board_mask(exclude)
    region = [round(v, 4) for v in (*BOARD_REGION, *(exclude or ()))]
    samples: list[tuple[int, float, float]] = []
    try:
        for index, source in enumerate(edit_map.sources):
            for a, b in edit_map.spans(index, merge_gap=MERGE_GAP_S):
                key = stable_key("board_change_v2", edit_map.range_digest(index, a, b), a, b, W, H, region,
                                 PIXEL_CHANGE)
                cached = cache.load_json("evidence", key)
                if not isinstance(cached, list):
                    cached, previous = [], None
                    for t, image in scanner(source, a, b, cancellation=cancellation):
                        edited = edit_map.to_edited(t, index)
                        if edited is not None and previous is not None and t - previous[0] <= MAX_PAIR_GAP_S:
                            cached.append([round(t, 3), round(board_change(previous[1], image, mask), 4)])
                        previous = (t, image)
                    cache.store_json("evidence", key, cached)
                samples.extend((index, float(t), float(c)) for t, c in cached)
    except AnalysisCancelled:
        raise
    except Exception as exc:  # optional evidence
        return VisualProfile("failed", f"{type(exc).__name__}: {exc}"[:300], samples)
    return VisualProfile("ok" if samples else "unavailable", "", samples)
