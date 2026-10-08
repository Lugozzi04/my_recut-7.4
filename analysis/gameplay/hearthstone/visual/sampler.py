from __future__ import annotations

import json
import math
import queue
import re
import subprocess
import threading
import time
from collections import deque
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from analysis.audio_service import AnalysisCancellation, AnalysisCancelled
from utils.ffmpeg import FFprobeCancelled, cancellable_probe_scope, ensure_ffmpeg, run_cmd
from utils.subprocess_utils import popen_no_window


class FrameSamplingError(RuntimeError):
    def __init__(self, message: str, *, code: str = "frame_sampling_failed") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class VideoInfo:
    duration_s: float
    width: int
    height: int
    start_time_s: float = 0.0


@dataclass(frozen=True)
class SampledFrame:
    """A BGR uint8 image and its original video-relative presentation timestamp."""

    timestamp: float
    image: np.ndarray


class FrameSampler(Protocol):
    def probe(self, path: str | Path, *, cancellation: AnalysisCancellation | None = None) -> VideoInfo: ...

    def sample(
        self, path: str | Path, *, fps: float, start_s: float = 0.0,
        end_s: float | None = None, max_width: int = 960,
        cancellation: AnalysisCancellation | None = None,
    ) -> Generator[SampledFrame, None, None]: ...


class RoiFrameSampler(FrameSampler, Protocol):
    def sample_roi(
        self, path: str | Path, *, roi: tuple[float, float, float, float],
        reference_size: tuple[int, int], fps: float, start_s: float = 0.0,
        end_s: float | None = None, output_size: tuple[int, int] | None = None,
        cancellation: AnalysisCancellation | None = None,
    ) -> Generator[SampledFrame, None, None]: ...


_END = object()
_TIMESTAMP = re.compile(r"\bn:\s*\d+\s+pts:\s*\S+\s+pts_time:\s*([-+0-9.eE]+)")


@dataclass(frozen=True)
class _ReaderFailure:
    message: str


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    # Kill the child before closing reader-owned pipes: close() can wait for a
    # read lock on Windows. FFmpeg is one child, without shell descendants.
    if process.poll() is None:
        try:
            process.terminate()
            process.wait(timeout=0.75)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=0.75)
            except (OSError, subprocess.TimeoutExpired):
                pass
    else:
        process.wait()


class FFmpegFrameSampler:
    """Bounded, sequential offline sampling; no Qt or OpenCV dependency.

    select keeps actual source frames, unlike fps which can synthesize duplicate
    frames/timestamps. showinfo supplies the selected PTS, including VFR gaps.
    copyts/start_at_zero retains video-relative timestamps when seeking windows.
    Interframe codecs still decode dependency frames; CV only sees the samples.
    """

    def __init__(self, *, stall_timeout_s: float = 60.0) -> None:
        if not math.isfinite(stall_timeout_s) or stall_timeout_s <= 0:
            raise ValueError("stall_timeout_s must be finite and positive.")
        self.stall_timeout_s = float(stall_timeout_s)
        self._metadata_lock = threading.Lock()
        self._metadata_cache: dict[Path, tuple[int, int, VideoInfo]] = {}

    def probe(self, path: str | Path, *, cancellation: AnalysisCancellation | None = None) -> VideoInfo:
        token = cancellation or AnalysisCancellation()
        token.raise_if_cancelled()
        source = Path(path).expanduser().resolve()
        if not source.is_file():
            raise FrameSamplingError(f"Video not found: {source}", code="source_not_found")
        signature = source.stat()
        with self._metadata_lock:
            cached = self._metadata_cache.get(source)
            if cached is not None and cached[:2] == (signature.st_size, signature.st_mtime_ns):
                token.raise_if_cancelled()
                return cached[2]
        _, ffprobe = ensure_ffmpeg()
        try:
            with cancellable_probe_scope(token):
                result = run_cmd([
                    ffprobe, "-v", "error", "-select_streams", "v:0",
                    "-show_entries", "stream=width,height,duration,start_time:format=format_name,duration,start_time",
                    "-of", "json", str(source),
                ], timeout_s=30.0)
            token.raise_if_cancelled()
            if result.returncode:
                raise ValueError(str(result.stderr).strip() or "FFprobe failed.")
            payload = json.loads(result.stdout)
            stream = payload["streams"][0]
            width, height = int(stream["width"]), int(stream["height"])
            container = payload.get("format", {})
            origin = float(container.get("start_time", 0.0) or 0.0)
            if not math.isfinite(origin):
                raise ValueError("Video start time is invalid.")
            duration = self._positive(stream.get("duration"))
            if duration is not None:
                video_start = float(stream.get("start_time", origin) or origin)
                if not math.isfinite(video_start):
                    raise ValueError("Video stream start time is invalid.")
                # Source timestamps use the container timeline. If audio starts
                # before video, stream.duration alone omits that leading delay.
                duration += max(0.0, video_start - origin)
            if duration is None:
                duration = self._positive(container.get("duration"))
                # Matroska commonly lacks stream.duration and its container
                # duration includes the initial timestamp offset. MPEG-TS,
                # in contrast, reports elapsed duration: do not subtract its
                # origin blindly. Normalize only the known Matroska convention.
                formats = str(container.get("format_name", "")).split(",")
                if duration is not None and origin > 0 and "matroska" in formats:
                    duration = self._positive(duration - origin)
            if width <= 0 or height <= 0 or duration is None:
                raise ValueError("Video dimensions/duration are unavailable or invalid.")
            info = VideoInfo(duration, width, height, origin)
            current = source.stat()
            if (current.st_size, current.st_mtime_ns) != (signature.st_size, signature.st_mtime_ns):
                raise FrameSamplingError("Source changed while probing video.", code="source_changed")
            token.raise_if_cancelled()
            with self._metadata_lock:
                # Metadata is tiny, but bound an instance reused for many VODs.
                if source not in self._metadata_cache and len(self._metadata_cache) >= 16:
                    self._metadata_cache.pop(next(iter(self._metadata_cache)))
                self._metadata_cache[source] = (signature.st_size, signature.st_mtime_ns, info)
            return info
        except FFprobeCancelled:
            token.raise_if_cancelled()
            raise
        except (AnalysisCancelled, FrameSamplingError):
            raise
        except (OSError, RuntimeError, ValueError, TypeError, KeyError, IndexError) as exc:
            raise FrameSamplingError(f"Could not inspect video: {exc}", code="invalid_video") from exc

    @staticmethod
    def _positive(value: object) -> float | None:
        try:
            result = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        return result if math.isfinite(result) and result > 0.0 else None

    def sample(
        self, path: str | Path, *, fps: float, start_s: float = 0.0,
        end_s: float | None = None, max_width: int = 960,
        cancellation: AnalysisCancellation | None = None,
    ) -> Generator[SampledFrame, None, None]:
        """Sample full frames, preserving the existing aspect-ratio resize."""
        if isinstance(max_width, bool) or not isinstance(max_width, int) or not 16 <= max_width <= 4096:
            raise ValueError("max_width must be an integer between 16 and 4096.")
        yield from self._sample(
            path, fps=fps, start_s=start_s, end_s=end_s, max_width=max_width,
            cancellation=cancellation,
        )

    def sample_roi(
        self, path: str | Path, *, roi: tuple[float, float, float, float],
        reference_size: tuple[int, int], fps: float, start_s: float = 0.0,
        end_s: float | None = None, output_size: tuple[int, int] | None = None,
        cancellation: AnalysisCancellation | None = None,
    ) -> Generator[SampledFrame, None, None]:
        """Crop a normalized source ROI inside FFmpeg, then resize only that ROI.

        The image has the same floor/ceil pixel bounds as the ROI in the
        reference frame. Dense result scans therefore transfer a small crop,
        rather than full video frames. ROI coordinates refer to the encoded
        source frame; callers compose any template-pack viewport beforehand.
        output_size optionally preserves exact detector geometry after viewport
        composition, without a second normalized-coordinate rounding.
        """
        if len(roi) != 4 or any(
            isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            for value in roi
        ):
            raise ValueError("roi must contain four finite normalized numbers.")
        x, y, width, height = roi
        if not 0 <= x < 1 or not 0 <= y < 1 or width <= 0 or height <= 0 or x + width > 1 + 1e-12 or y + height > 1 + 1e-12:
            raise ValueError("roi must be non-empty and contained in the normalized source frame.")
        if len(reference_size) != 2 or any(
            isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 4096
            for value in reference_size
        ):
            raise ValueError("reference_size must contain two positive integers at most 4096.")
        if output_size is not None and (len(output_size) != 2 or any(
            isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 4096
            for value in output_size
        )):
            raise ValueError("output_size must contain two positive integers at most 4096.")
        yield from self._sample(
            path, fps=fps, start_s=start_s, end_s=end_s, max_width=960,
            roi=roi, reference_size=reference_size, output_size=output_size, cancellation=cancellation,
        )

    @staticmethod
    def _roi_bounds(
        roi: tuple[float, float, float, float], width: int, height: int,
    ) -> tuple[int, int, int, int]:
        x, y, roi_width, roi_height = roi
        left, top = math.floor(x * width), math.floor(y * height)
        right = min(width, math.ceil((x + roi_width) * width))
        bottom = min(height, math.ceil((y + roi_height) * height))
        if right <= left or bottom <= top:
            raise ValueError("roi must cover at least one reference/source pixel.")
        return left, top, right - left, bottom - top

    def _sample(
        self, path: str | Path, *, fps: float, start_s: float,
        end_s: float | None, max_width: int,
        roi: tuple[float, float, float, float] | None = None,
        reference_size: tuple[int, int] | None = None,
        output_size: tuple[int, int] | None = None,
        cancellation: AnalysisCancellation | None = None,
    ) -> Generator[SampledFrame, None, None]:
        token = cancellation or AnalysisCancellation()
        token.raise_if_cancelled()
        if not math.isfinite(fps) or not 0.0 < fps <= 120.0:
            raise ValueError("fps must be finite, positive and at most 120.")
        if not math.isfinite(start_s) or start_s < 0.0:
            raise ValueError("start_s must be finite and non-negative.")
        if end_s is not None and (not math.isfinite(end_s) or end_s <= start_s):
            raise ValueError("end_s must be finite and after start_s.")
        source = Path(path).expanduser().resolve()
        info = self.probe(source, cancellation=token)
        finish = min(info.duration_s, end_s) if end_s is not None else info.duration_s
        if start_s >= finish:
            raise ValueError("The sample range is outside the video.")
        crop_filter = ""
        if roi is None:
            width = min(info.width, max_width)
            height = max(1, round(info.height * width / info.width))
        else:
            assert reference_size is not None
            left, top, crop_width, crop_height = self._roi_bounds(roi, info.width, info.height)
            _, _, width, height = self._roi_bounds(roi, *reference_size)
            if output_size is not None:
                width, height = output_size
            # exact=1 avoids silently rounding odd ROI bounds for subsampled
            # YUV input; the final BGR frame must match reference geometry.
            crop_filter = f"crop={crop_width}:{crop_height}:{left}:{top}:exact=1,"
        # Tiny tolerance avoids floating subtraction dropping an exactly due
        # source frame (e.g. 0.9 - 0.7 < 0.2 in binary floating point).
        period = max(1e-9, 1.0 / fps - 1e-7)
        if roi is None:
            selection = f"isnan(prev_selected_t)+gte(t-prev_selected_t,{period:.12f})"
        else:
            # Dense sampling chooses a real frame in each centered time bin.
            # A prev_selected_t + period gate would silently turn 20 FPS into
            # 15 FPS on a 30 FPS source. Empty VFR bins stay empty; no frames
            # are synthesized and gaps do not trigger catch-up bursts.
            # Centered bins tolerate millisecond-quantized 60 FPS timestamps
            # (.016/.033/.050...) without dropping every third source frame.
            selection = (
                "isnan(prev_selected_t)+gt("
                f"floor((t-start_t)*{fps:.12f}+0.5),"
                f"floor((prev_selected_t-start_t)*{fps:.12f}+0.5))"
            )
        vf = f"select='{selection}',{crop_filter}scale={width}:{height},format=bgr24,showinfo"
        ffmpeg, _ = ensure_ffmpeg()
        command = [
            ffmpeg, "-nostdin", "-hide_banner", "-nostats", "-loglevel", "info",
            "-copyts", "-start_at_zero", "-ss", f"{start_s:.9f}", "-t", f"{finish - start_s:.9f}",
            "-noautorotate", "-i", str(source), "-map", "0:v:0", "-an", "-sn", "-dn",
            "-vf", vf, "-fps_mode", "passthrough", "-threads", "1",
            "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1",
        ]
        process = popen_no_window(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, bufsize=0)
        frames: queue.Queue[object] = queue.Queue(maxsize=2)
        timestamps: queue.Queue[object] = queue.Queue(maxsize=8)
        stop = threading.Event()
        errors: deque[bytes] = deque(maxlen=64)
        readers: list[threading.Thread] = []
        try:
            token.attach_process(process)
            assert process.stdout is not None and process.stderr is not None
            frame_bytes = width * height * 3

            def put(target: queue.Queue[object], value: object) -> bool:
                while not stop.is_set():
                    try:
                        target.put(value, timeout=0.05)
                        return True
                    except queue.Full:
                        pass
                return False

            def read_frames() -> None:
                assert process.stdout is not None
                try:
                    while not stop.is_set():
                        buffer = bytearray()
                        while len(buffer) < frame_bytes and not stop.is_set():
                            chunk = process.stdout.read(min(65536, frame_bytes - len(buffer)))
                            if not chunk:
                                break
                            buffer.extend(chunk)
                        if stop.is_set():
                            return
                        if not buffer:
                            break
                        if len(buffer) != frame_bytes:
                            put(frames, _ReaderFailure("FFmpeg returned a truncated raw video frame."))
                            return
                        if not put(frames, bytes(buffer)):
                            return
                except (OSError, ValueError) as exc:
                    put(frames, _ReaderFailure(str(exc)))
                finally:
                    put(frames, _END)

            def read_metadata() -> None:
                assert process.stderr is not None
                try:
                    while not stop.is_set():
                        line = process.stderr.readline(4096)
                        if not line:
                            break
                        errors.append(line[-1024:])
                        match = _TIMESTAMP.search(line.decode("utf-8", errors="replace"))
                        if match is not None and not put(timestamps, float(match.group(1))):
                            return
                except (OSError, ValueError) as exc:
                    put(timestamps, _ReaderFailure(str(exc)))
                finally:
                    put(timestamps, _END)

            def receive(target: queue.Queue[object]) -> object:
                deadline = time.monotonic() + self.stall_timeout_s
                while True:
                    token.raise_if_cancelled()
                    try:
                        value = target.get(timeout=0.05)
                    except queue.Empty:
                        if time.monotonic() >= deadline:
                            raise FrameSamplingError("FFmpeg sampling stalled.", code="sampling_timeout")
                        continue
                    token.raise_if_cancelled()
                    if isinstance(value, _ReaderFailure):
                        raise FrameSamplingError(value.message)
                    return value

            readers = [
                threading.Thread(target=read_frames, name=f"gameplay-frames-{process.pid}", daemon=True),
                threading.Thread(target=read_metadata, name=f"gameplay-timestamps-{process.pid}", daemon=True),
            ]
            for reader in readers:
                reader.start()
            previous = -math.inf
            while True:
                raw = receive(frames)
                if raw is _END:
                    break
                timestamp = receive(timestamps)
                if timestamp is _END:
                    raise FrameSamplingError("FFmpeg did not provide a timestamp for a sampled frame.")
                assert isinstance(timestamp, float) and isinstance(raw, bytes)
                if not math.isfinite(timestamp) or timestamp <= previous:
                    raise FrameSamplingError("FFmpeg returned invalid/non-increasing frame timestamps.")
                previous = timestamp
                if timestamp < start_s - 1e-6 or timestamp >= finish + 1e-6:
                    continue
                image = np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3)
                yield SampledFrame(timestamp, image)
            deadline = time.monotonic() + self.stall_timeout_s
            while process.poll() is None:
                token.raise_if_cancelled()
                if time.monotonic() >= deadline:
                    raise FrameSamplingError("FFmpeg did not exit after sampling.", code="sampling_timeout")
                stop.wait(0.05)
            token.raise_if_cancelled()
            if process.returncode:
                detail = b"".join(errors).decode("utf-8", errors="replace").strip()
                raise FrameSamplingError(detail or "FFmpeg sampling failed.")
        finally:
            stop.set()
            _stop_process(process)
            token.detach_process(process)
            for reader in readers:
                reader.join(timeout=1.0)
            # Pipes are closed only after their reader has finished; never wait
            # on the BufferedReader lock while its child can still write.
            for stream, reader in zip((process.stdout, process.stderr), readers):
                if stream is not None and not reader.is_alive():
                    stream.close()
            if not readers:
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
