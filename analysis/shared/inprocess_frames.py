"""In-process sparse frame access: no FFmpeg process per seek.

Same frames as FFmpegFrameSampler full-frame requests: the same libavcodec
decoder, swscale BICUBIC (FFmpeg's scale filter default) to the same geometry,
BGR24, video-relative timestamps as with -copyts -start_at_zero, and the
"first real frame in [t, t + 0.10)" selection of ``sample_at``. Tests compare
pixels and PTS against the FFmpeg path; an unavailable/failing reader raises
and the caller keeps its existing FFmpeg route.

Access is cost-aware: a request ahead of a worker's decoder position is decoded
forward when that is cheaper than a new seek. The break-even distance comes
from costs measured during the run (seed values documented in
docs/performance_reports/ADAPTIVE_SCHEDULER_RESULTS.md), not a fixed threshold.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import math
from pathlib import Path
import queue
import threading
import time
from typing import Any, Iterator

from analysis.audio_service import AnalysisCancellation, AnalysisCancelled
from analysis.gameplay.hearthstone.visual.sampler import SampledFrame

try:  # Pinned in requirements.txt; optional so the FFmpeg route keeps working.
    import av
except ImportError:  # pragma: no cover - depends on the environment
    av = None  # type: ignore[assignment]

REQUEST_WINDOW_S = 0.10


def available() -> bool:
    return av is not None


@dataclass
class AccessCosts:
    """Measured wall costs, exponentially averaged during one analysis.

    seek_s: one seek plus decoding up to the requested frame.
    forward_s_per_s: decoding forward, per source second, without seeking.
    Seeds: videoprova 1080p60, GOP 2 s (24-25 ms keyframe seek, 22 ms/s forward).
    """

    seek_s: float = 0.025
    forward_s_per_s: float = 0.022
    alpha: float = 0.1

    def update_seek(self, seconds: float) -> None:
        self.seek_s += self.alpha * (seconds - self.seek_s)

    def update_forward(self, seconds: float, source_seconds: float) -> None:
        if source_seconds > 0.05:
            self.forward_s_per_s += self.alpha * (seconds / source_seconds - self.forward_s_per_s)

    def break_even_s(self) -> float:
        """Forward decoding up to this distance costs no more than a seek."""
        return self.seek_s / max(self.forward_s_per_s, 1e-6)


class _Cursor:
    """One open container and its decoder position, used by one thread at a time."""

    def __init__(self, path: Path, width: int, height: int) -> None:
        assert av is not None
        self.container = av.open(str(path))
        self.stream = self.container.streams.video[0]
        self.stream.thread_type = "AUTO"
        if self.stream.time_base is None:
            raise RuntimeError("Video stream has no time base")
        self.time_base = float(self.stream.time_base)
        start = self.container.start_time
        self.offset = float(start) / av.time_base if start is not None else 0.0
        self.width, self.height = width, height
        self.frames: Iterator[Any] | None = None
        # Latest decoded frame and the PTS decoded just before it (None when
        # unknown, e.g. right after a seek). Together they prove whether a
        # request window can be answered without decoding again.
        self.last: tuple[float, Any] | None = None
        self.before: float | None = None

    def close(self) -> None:
        self.reset()
        self.container.close()

    def reset(self) -> None:
        self.frames = None
        self.last = None
        self.before = None

    def _next(self) -> tuple[float, Any] | None:
        assert self.frames is not None
        for frame in self.frames:
            if frame.pts is None:
                continue
            self.before = self.last[0] if self.last is not None else None
            # Exact rational PTS, rounded like FFmpeg showinfo's pts_time (6 decimals).
            self.last = (round(float(frame.pts * self.stream.time_base) - self.offset, 6), frame)
            return self.last
        self.reset()
        return None

    def seek(self, timestamp: float) -> None:
        target = max(0, int(math.floor((timestamp + self.offset) / self.time_base)))
        self.container.seek(target, stream=self.stream, backward=True, any_frame=False)
        self.reset()
        self.frames = self.container.decode(self.stream)

    def first_from(self, timestamp: float, costs: AccessCosts, counts: Counter[str]) -> SampledFrame | None:
        until = timestamp + REQUEST_WINDOW_S
        known_gap = self.before is not None and self.before < timestamp - 1e-9
        if self.last is not None and self.last[0] >= timestamp - 1e-9:
            if known_gap:
                # No decoded frame lies in [before, last): answer without decoding.
                counts["reused_frames"] += 1
                return self._image(self.last) if self.last[0] < until else None
            seek = True
        else:
            seek = (self.frames is None or self.last is None
                    or timestamp - self.last[0] > costs.break_even_s())
        started = time.perf_counter()
        origin = None if seek or self.last is None else self.last[0]
        if seek:
            self.seek(timestamp)
            counts["seeks"] += 1
        else:
            counts["forward_requests"] += 1
        current = None
        while True:
            current = self._next()
            if current is None:
                break
            counts["decoded_frames"] += 1
            if current[0] >= timestamp - 1e-9:
                break
        elapsed = time.perf_counter() - started
        if seek:
            costs.update_seek(elapsed)
        elif current is not None and origin is not None:
            costs.update_forward(elapsed, current[0] - origin)
        if current is None or current[0] >= until:
            return None
        return self._image(current)

    def _image(self, item: tuple[float, Any]) -> SampledFrame:
        image = item[1].to_ndarray(width=self.width, height=self.height, format="bgr24", interpolation="BICUBIC")
        return SampledFrame(item[0], image)

    def scan(self, start: float, finish: float, fps: float, counts: Counter[str]) -> Iterator[SampledFrame]:
        """FFmpeg ``select`` at fps from the first frame at or after start (sample())."""
        period = max(1e-9, 1.0 / fps - 1e-7)
        self.seek(start)
        counts["seeks"] += 1
        previous: float | None = None
        while True:
            current = self._next()
            if current is None or current[0] >= finish:
                return
            counts["decoded_frames"] += 1
            if current[0] < start - 1e-9:
                continue
            if previous is None or current[0] - previous >= period:
                previous = current[0]
                yield self._image(current)


class InProcessFrameReader:
    """A few persistent containers per source; requests are split across them."""

    def __init__(self, *, workers: int = 4, costs: AccessCosts | None = None) -> None:
        if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 16:
            raise ValueError("workers must be an integer in 1..16")
        if av is None:
            raise RuntimeError("PyAV is not available")
        self.workers = workers
        self.costs = costs or AccessCosts()
        self.counts: Counter[str] = Counter()
        self._lock = threading.Lock()
        self._cursors: dict[tuple[Path, int, int, int], _Cursor] = {}

    def close(self) -> None:
        with self._lock:
            cursors, self._cursors = list(self._cursors.values()), {}
        for cursor in cursors:
            cursor.close()

    def _cursor(self, source: Path, width: int, height: int, index: int) -> _Cursor:
        key = (source, width, height, index)
        with self._lock:
            cursor = self._cursors.get(key)
            if cursor is None:
                cursor = self._cursors[key] = _Cursor(source, width, height)
                self.counts["containers"] += 1
            return cursor

    def iter_many(self, source: str | Path, timestamps: list[float], *, width: int, height: int,
                  cancellation: AnalysisCancellation | None = None) -> Iterator[tuple[float, SampledFrame | None]]:
        """Yield (request, frame) as decoded; contiguous chunks keep forward decoding local."""
        token = cancellation or AnalysisCancellation()
        times = sorted(set(timestamps))
        if not times:
            return
        path = Path(source).expanduser().resolve()
        count = min(self.workers, len(times))
        size = math.ceil(len(times) / count)
        chunks = [times[i:i + size] for i in range(0, len(times), size)]
        output: queue.Queue[Any] = queue.Queue(maxsize=2 * len(chunks))
        stop = threading.Event()
        done = object()

        def work(index: int, chunk: list[float]) -> None:
            local: Counter[str] = Counter()
            try:
                cursor = self._cursor(path, width, height, index)
                for timestamp in chunk:
                    if stop.is_set() or token.is_cancelled:
                        break
                    frame = cursor.first_from(timestamp, self.costs, local)
                    local["delivered_frames" if frame is not None else "empty_requests"] += 1
                    while not stop.is_set():
                        try:
                            output.put((timestamp, frame), timeout=0.1)
                            break
                        except queue.Full:
                            continue
            except BaseException as exc:  # noqa: BLE001 - re-raised in the consumer
                output.put(exc)
            finally:
                with self._lock:
                    self.counts.update(local)
                output.put(done)

        threads = [threading.Thread(target=work, args=(index, chunk), daemon=True,
                                    name=f"recut-frames-{index}") for index, chunk in enumerate(chunks)]
        for thread in threads:
            thread.start()
        finished = 0
        try:
            while finished < len(threads):
                if token.is_cancelled:
                    raise AnalysisCancelled()
                try:
                    item = output.get(timeout=0.1)
                except queue.Empty:
                    continue
                if item is done:
                    finished += 1
                elif isinstance(item, BaseException):
                    raise item
                else:
                    yield item
            token.raise_if_cancelled()
        finally:
            stop.set()
            while any(thread.is_alive() for thread in threads):
                try:
                    output.get(timeout=0.05)
                except queue.Empty:
                    pass

    def scan(self, source: str | Path, *, start: float, finish: float, fps: float, width: int, height: int,
             cancellation: AnalysisCancellation | None = None) -> Iterator[SampledFrame]:
        token = cancellation or AnalysisCancellation()
        path = Path(source).expanduser().resolve()
        cursor = self._cursor(path, width, height, 0)
        local: Counter[str] = Counter()
        try:
            for frame in cursor.scan(start, finish, fps, local):
                token.raise_if_cancelled()
                local["delivered_frames"] += 1
                yield frame
        finally:
            cursor.reset()
            with self._lock:
                self.counts.update(local)
