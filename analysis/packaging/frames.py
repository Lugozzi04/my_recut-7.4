"""Full-resolution frame extraction, frame quality metrics and contact sheets.

Frames come from the shared FFmpegFrameSampler (real decoded frames with their
source PTS, never synthesized). Each saved frame carries its source and edited
timestamps; the frames cache is keyed by source identity + time + size, so a
re-run (other provider, other thumbnail text) never decodes the video again.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np

from analysis.audio_service import AnalysisCancellation

from .cache import PackagingCache, stable_key
from .models import FrameRecord
from analysis.shared.timeline import TimelineMapper as EditMap

MAX_WIDTH = 1920


def require_cv2() -> Any:
    try:
        import cv2
    except ImportError as exc:  # pragma: no cover - opencv-python-headless is a gameplay requirement
        from .models import PackagingError
        raise PackagingError("opencv_missing", "opencv-python-headless is required for packaging frames") from exc
    return cv2


def frame_quality(image: np.ndarray) -> dict[str, float]:
    """Sharpness (Laplacian variance at 640 px), exposure and contrast, plus a 0..1 combined score."""
    cv2 = require_cv2()
    h, w = image.shape[:2]
    small = cv2.resize(image, (640, max(1, round(h * 640 / w))), interpolation=cv2.INTER_AREA) if w > 640 else image
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY) if small.ndim == 3 else small
    sharp = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    bright = float(gray.mean()) / 255.0
    contrast = float(gray.std()) / 128.0
    exposure = 1.0 - min(1.0, abs(bright - 0.45) / 0.45)
    score = 0.55 * min(1.0, math.log1p(sharp) / math.log1p(800.0)) + 0.25 * exposure + 0.20 * min(1.0, contrast)
    return {"sharpness": sharp, "brightness": bright, "contrast": contrast, "score": score}


def write_image(path: Path, image: np.ndarray, *, quality: int = 95) -> None:
    cv2 = require_cv2()
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()
    params = [cv2.IMWRITE_JPEG_QUALITY, quality] if suffix in (".jpg", ".jpeg") else [cv2.IMWRITE_PNG_COMPRESSION, 3]
    ok, data = cv2.imencode(suffix, image, params)
    if not ok:
        raise OSError(f"could not encode {path.name}")
    path.write_bytes(data.tobytes())  # imencode + write_bytes: unicode-safe paths on Windows


def read_image(path: Path) -> np.ndarray | None:
    cv2 = require_cv2()
    try:
        data = np.frombuffer(Path(path).read_bytes(), dtype=np.uint8)
    except OSError:
        return None
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    return image if image is not None and image.size else None


class FrameExtractor:
    def __init__(self, edit_map: EditMap, output_dir: Path, cache: PackagingCache, *, sampler: Any = None,
                 cancellation: AnalysisCancellation | None = None) -> None:
        if sampler is None:
            from analysis.shared.frames import FrameSamplingService
            sampler = FrameSamplingService(enabled=cache.enabled)
        self.edit_map = edit_map
        self.output_dir = output_dir
        self.cache = cache
        self.sampler = sampler
        self.cancellation = cancellation
        self.records: dict[str, FrameRecord] = {}
        self.images: dict[str, np.ndarray] = {}  # full-resolution BGR images kept in memory for crops
        self._counters: dict[str, int] = {}
        self.decode_calls = 0
        self.allowed_intervals: list[tuple[int, float, float]] | None = None

    def _next_id(self, prefix: str = "F") -> str:
        self._counters[prefix] = self._counters.get(prefix, 0) + 1
        return f"{prefix}{self._counters[prefix]:03d}"

    def burst(self, source_index: int, start: float, end: float, *, fps: float) -> list[tuple[float, np.ndarray]]:
        """Kept frames in [start, end); the shared cache stores the unfiltered source bundle."""
        if self.cancellation is not None:
            self.cancellation.raise_if_cancelled()
        # An Intro, excluded game or removed span cannot supply usable evidence.
        # Skip FFmpeg and bundle loading when there is no overlap with BOTH filters.
        allowed = self.allowed_intervals
        if not any(p.source_index == source_index and max(start, p.source_start) < min(end, p.source_end)
                   and (allowed is None or any(i == source_index and
                        max(start, p.source_start, a) < min(end, p.source_end, b) for i, a, b in allowed))
                   for p in self.edit_map.pieces):
            return []
        identity = self.edit_map.identities[source_index] if self.edit_map.identities else self.edit_map.sources[source_index]
        key = stable_key("burst", identity, round(start, 3), round(end, 3), fps, MAX_WIDTH)
        bundle = self.cache.file_path("frames", key, ".npz")
        if self.cache.enabled and bundle.is_file():
            try:
                with np.load(bundle, allow_pickle=False) as data:
                    times, offsets, blob = data["times"], data["offsets"], data["blob"]
                self.cache.stats["frames"]["hit"] += 1
                return self._kept(source_index, [(float(t), self._decode(blob[offsets[i]:offsets[i + 1]]))
                                                 for i, t in enumerate(times)])
            except (OSError, ValueError, KeyError):
                pass
        self.cache.stats["frames"]["miss"] += 1
        self.decode_calls += 1
        frames = [(float(f.timestamp), f.image) for f in self.sampler.sample(
            self.edit_map.sources[source_index], fps=fps, start_s=max(0.0, start), end_s=end, max_width=MAX_WIDTH,
            cancellation=self.cancellation)]
        if frames and self.cache.enabled:
            try:
                bundle.parent.mkdir(parents=True, exist_ok=True)
                encoded = [self._encode(image) for _, image in frames]  # JPEG bytes, no pickled objects
                offsets = np.cumsum([0] + [len(e) for e in encoded]).astype(np.int64)
                import uuid
                temporary = bundle.with_name(f".{bundle.stem}.{uuid.uuid4().hex}.tmp.npz")
                np.savez(temporary, times=np.array([t for t, _ in frames]), offsets=offsets,
                         blob=np.concatenate(encoded) if encoded else np.zeros(0, np.uint8))
                temporary.replace(bundle)
            except (OSError, ValueError):
                pass
        return self._kept(source_index, frames)

    def _kept(self, source_index: int, frames: list[tuple[float, np.ndarray]]) -> list[tuple[float, np.ndarray]]:
        # Cache raw source frames; apply this edit's keeps AFTER reading/writing the shared bundle.
        return [(t, image) for t, image in frames if self._contains(source_index, t)]

    def _contains(self, source_index: int, t: float) -> bool:
        if self.allowed_intervals is not None and not any(i == source_index and a <= t < b
                for i, a, b in self.allowed_intervals):
            return False
        # Frame timestamps are half-open: an exact cut-out frame belongs to the removed material.
        return any(p.source_index == source_index and p.source_start <= t < p.source_end
                   for p in self.edit_map.pieces)

    @staticmethod
    def _encode(image: np.ndarray) -> np.ndarray:
        cv2 = require_cv2()
        ok, data = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 95])
        return data if ok else np.zeros(0, np.uint8)

    @staticmethod
    def _decode(data: np.ndarray) -> np.ndarray:
        cv2 = require_cv2()
        return cv2.imdecode(np.asarray(data, dtype=np.uint8), cv2.IMREAD_COLOR)

    def best_frame(self, source_index: int, t: float, *, radius: float = 1.0, fps: float = 6.0) -> tuple[float, np.ndarray] | None:
        """Sharpest kept frame within +-radius of t (frames in removed content are skipped)."""
        frames = self.burst(source_index, max(0.0, t - radius), t + radius, fps=fps)
        kept = [(ft, image) for ft, image in frames if self.edit_map.to_edited(ft, source_index) is not None]
        if not kept:
            return None
        return max(kept, key=lambda item: frame_quality(item[1])["score"] - 0.05 * abs(item[0] - t))

    def save(self, image: np.ndarray, *, source_index: int, source_time: float, kind: str = "gameplay",
             highlight_id: str | None = None, role: str = "", notes: str = "", prefix: str = "F",
             folder: str = "frames") -> FrameRecord:
        if not self._contains(source_index, source_time):
            raise ValueError("Thumbnail evidence must belong to the exported edit's keeps.")
        frame_id = self._next_id(prefix)
        relative = f"{folder}/{frame_id}_{source_time:09.2f}s.jpg".replace(" ", "0")  # q95 JPEG: 1080p PNGs are ~3 MB
        write_image(self.output_dir / relative, image)
        record = FrameRecord(frame_id, kind, source_index, source_time, self.edit_map.to_edited(source_time, source_index),
                             relative, int(image.shape[1]), int(image.shape[0]), frame_quality(image), highlight_id,
                             role, notes)
        self.records[frame_id] = record
        self.images[frame_id] = image
        return record

    def grab(self, source_index: int, t: float, **kwargs: Any) -> FrameRecord | None:
        found = self.best_frame(source_index, t, radius=float(kwargs.pop("radius", 1.0)))
        if found is None:
            return None
        frame_time, image = found
        return self.save(image, source_index=source_index, source_time=frame_time, **kwargs)


def contact_sheet(records: Sequence[FrameRecord], images: dict[str, np.ndarray], target: Path, *, columns: int = 3,
                  cell_width: int = 640, title: str = "") -> Path:
    """Grid of frames labelled with frame id and edited time (mm:ss) for the agent and the report."""
    cv2 = require_cv2()
    cells = []
    for record in records:
        image = images.get(record.id)
        if image is None:
            continue
        h, w = image.shape[:2]
        cell = cv2.resize(image, (cell_width, max(1, round(h * cell_width / w))), interpolation=cv2.INTER_AREA)
        label = f"{record.id}  {_clock(record.edited_time)}"
        cv2.rectangle(cell, (0, 0), (cell_width, 34), (0, 0, 0), -1)
        cv2.putText(cell, label, (8, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2, cv2.LINE_AA)
        cells.append(cell)
    if not cells:
        raise ValueError("contact sheet without frames")
    height = max(c.shape[0] for c in cells)
    rows = []
    for i in range(0, len(cells), columns):
        row = [np.pad(c, ((0, height - c.shape[0]), (0, 0), (0, 0))) for c in cells[i:i + columns]]
        row += [np.zeros((height, cell_width, 3), np.uint8)] * (columns - len(row))
        rows.append(np.hstack([np.pad(c, ((4, 4), (4, 4), (0, 0))) for c in row]))
    sheet = np.vstack(rows)
    if title:
        banner = np.zeros((44, sheet.shape[1], 3), np.uint8)
        cv2.putText(banner, _ascii(title)[:110], (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2,
                    cv2.LINE_AA)
        sheet = np.vstack([banner, sheet])
    write_image(target, sheet, quality=88)
    return target


def _ascii(text: str) -> str:
    """OpenCV's Hershey fonts are ASCII only: transliterate accents and quotes instead of printing '?'."""
    import unicodedata

    table = {ord(k): v for k, v in {"«": '"', "»": '"', "“": '"', "”": '"', "’": "'", "‘": "'", "–": "-", "—": "-"}.items()}
    text = text.translate(table)
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def _clock(edited: float | None) -> str:
    if edited is None:
        return "cut"
    seconds = int(edited)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def ids(records: Iterable[FrameRecord]) -> list[str]:
    return [r.id for r in records]


# ---------------------------------------------------------------------------------------------------
# candidate selection (local, cheap, deterministic)

def visual_hash(image: np.ndarray) -> int:
    """64-bit difference hash: near-identical frames have a small Hamming distance."""
    from .quality import dhash

    return dhash(image)


def rank_gameplay(frames: Sequence[tuple[float, np.ndarray]], *, target: float) -> list[tuple[float, float, np.ndarray]]:
    """(score, time, image) best first. Prefers sharp, settled frames (not mid-animation), readable density.

    sharpness is relative to the burst median (a blurry transition scores low even on a busy board);
    motion is the mean difference to the neighbouring frames (animations and camera sweeps score low);
    clutter penalises extreme edge density; a small term keeps the choice close to the signal peak.
    """
    cv2 = require_cv2()
    if not frames:
        return []
    grays = [cv2.cvtColor(cv2.resize(im, (320, 180), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
             for _, im in frames]
    sharp = [frame_quality(im)["sharpness"] for _, im in frames]
    median_sharp = float(np.median(sharp)) or 1.0
    ranked = []
    for i, (t, image) in enumerate(frames):
        neighbours = [grays[j] for j in (i - 1, i + 1) if 0 <= j < len(grays)]
        motion = float(np.mean([np.mean(cv2.absdiff(grays[i], n)) for n in neighbours])) / 40.0 if neighbours else 0.0
        edges = float(np.mean(cv2.Canny(grays[i], 60, 150) > 0))
        clutter = max(0.0, edges - 0.16) * 4.0
        score = (0.50 * min(1.5, sharp[i] / median_sharp) + 0.30 * (1.0 - min(1.0, motion))
                 - 0.15 * min(1.0, clutter) - 0.02 * abs(t - target))
        ranked.append((score, t, image))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked


def pick_diverse(ranked: Sequence[tuple[float, float, np.ndarray]], *, k: int, taken: list[int],
                 min_distance: int = 12) -> list[tuple[float, float, np.ndarray]]:
    """Best k candidates that differ visually from each other and from everything already selected."""
    from .quality import hamming

    out = []
    for item in ranked:
        h = visual_hash(item[2])
        if all(hamming(h, other) >= min_distance for other in taken):
            out.append(item)
            taken.append(h)
            if len(out) >= k:
                break
    if not out and ranked:  # every highlight keeps at least its best frame, even if it resembles another
        out.append(ranked[0])
        taken.append(visual_hash(ranked[0][2]))
    return out


def read_image_unchanged(path: Path) -> np.ndarray | None:
    """Read keeping an alpha channel (PNG cut-outs)."""
    cv2 = require_cv2()
    try:
        data = np.frombuffer(Path(path).read_bytes(), dtype=np.uint8)
    except OSError:
        return None
    image = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
    return image if image is not None and image.size else None
