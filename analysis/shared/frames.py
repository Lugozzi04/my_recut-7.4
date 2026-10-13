"""On-demand selective lossless frames; stream/burst caches stay owned by their consumers."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
from zipfile import BadZipFile
from typing import Any, Iterator

import numpy as np

from analysis.audio_service import AnalysisCancellation, AnalysisCancelled
from analysis.gameplay.cache import video_identity
from analysis.gameplay.hearthstone.visual.sampler import FFmpegFrameSampler, SampledFrame
from analysis.performance_profile import current_profile, measure, record
from utils.runtime_paths import cache_root


class FrameSamplingService:
    """Selected frames: persistent cache, FFmpeg batching, optional in-process access.

    in_process=True serves cache misses with one decoder session per worker
    (analysis.shared.inprocess_frames) instead of an FFmpeg process per seek or
    batch. Same frames and PTS; any reader failure returns to the FFmpeg route.
    """

    def __init__(self, sampler: Any = None, *, directory: str | Path | None = None,
                 max_gap_s: float = 3.0, max_span_s: float = 24.0, enabled: bool = True,
                 in_process: bool = False, workers: int = 4) -> None:
        self.sampler = sampler or FFmpegFrameSampler()
        self.directory = Path(directory) if directory is not None else cache_root() / "selected_frames"
        self.max_gap_s, self.max_span_s = max_gap_s, max_span_s
        self.enabled = enabled
        self.in_process = in_process
        self.workers = workers
        self._reader: Any = None
        self._reader_failed = False

    def _in_process_reader(self) -> Any:
        if not self.in_process or self._reader_failed:
            return None
        if self._reader is None:
            from . import inprocess_frames
            if not inprocess_frames.available():
                self._reader_failed = True
                record("shared.frames", in_process_unavailable=1)
                return None
            self._reader = inprocess_frames.InProcessFrameReader(workers=self.workers)
        return self._reader

    def _disable_reader(self) -> None:
        self._reader_failed = True
        record("shared.frames", in_process_failures=1)
        self.close()

    def close(self) -> None:
        """Release in-process decoder sessions; the service remains usable."""
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.close()

    def access_counters(self) -> dict[str, float]:
        reader = self._reader
        if reader is None:
            return {}
        return {**dict(reader.counts), "seek_cost_s": reader.costs.seek_s,
                "forward_cost_s_per_s": reader.costs.forward_s_per_s,
                "break_even_s": reader.costs.break_even_s()}

    def _geometry(self, source: str | Path, max_width: int, token: AnalysisCancellation) -> tuple[int, int, float]:
        info = self.sampler.probe(source, cancellation=token)
        width = min(info.width, max_width)
        return width, max(1, round(info.height * width / info.width)), info.duration_s

    def scan(self, source: str | Path, *, fps: float, start_s: float, end_s: float, max_width: int = 960,
             cancellation: AnalysisCancellation | None = None) -> Iterator[SampledFrame]:
        """Sequential frames like sample(); one in-process decoder session when enabled."""
        token = cancellation or AnalysisCancellation()
        reader = self._in_process_reader()
        if reader is not None:
            width, height, duration = self._geometry(source, max_width, token)
            record("shared.frames", in_process_scans=1)
            delivered = 0
            try:
                for frame in reader.scan(source, start=start_s, finish=min(duration, end_s), fps=fps,
                                         width=width, height=height, cancellation=token):
                    delivered += 1
                    yield frame
                return
            except AnalysisCancelled:
                raise
            except Exception:
                if delivered:
                    raise
                self._disable_reader()
        with closing(self.sample(source, fps=fps, start_s=start_s, end_s=end_s, max_width=max_width,
                                 cancellation=token)) as frames:
            yield from frames

    def iter_many(self, source: str | Path, timestamps: list[float], *, max_width: int = 960,
                  cancellation: AnalysisCancellation | None = None) -> Iterator[tuple[float, SampledFrame | None]]:
        """Like get_many, yielding each request as soon as its frame is available."""
        token = cancellation or AnalysisCancellation()
        reader = self._in_process_reader()
        if reader is None:
            yield from self.get_many(source, timestamps, max_width=max_width, cancellation=token).items()
            return
        times = sorted(set(timestamps))
        if any(not np.isfinite(t) or t < 0 for t in times):
            raise ValueError("Invalid requested timestamp")
        width, height, _ = self._geometry(source, max_width, token)
        record("shared.frames", requested_frames=len(times), in_process_batches=1)
        served: set[float] = set()
        try:
            for timestamp, frame in reader.iter_many(source, times, width=width, height=height, cancellation=token):
                served.add(timestamp)
                record("shared.frames", decoded_frames=int(frame is not None))
                yield timestamp, frame
            return
        except AnalysisCancelled:
            raise
        except Exception:
            self._disable_reader()
        remaining = [t for t in times if t not in served]
        yield from self.get_many(source, remaining, max_width=max_width, cancellation=token).items()

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
