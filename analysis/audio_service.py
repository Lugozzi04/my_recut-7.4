from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from utils.ffmpeg import ensure_ffmpeg, ffprobe_duration_seconds, has_audio_stream
from utils.subprocess_utils import popen_no_window


ProgressCallback = Callable[[int], None]


class AudioAnalysisError(RuntimeError):
    def __init__(self, message: str, *, code: str = "analysis_failed") -> None:
        super().__init__(message)
        self.code = code


class AnalysisCancelled(AudioAnalysisError):
    def __init__(self) -> None:
        # The Qt caller already treats this value as a silent cancellation.
        super().__init__("__CANCELLED__", code="cancelled")


@dataclass(frozen=True)
class AnalysisRequest:
    path: str
    hop_s: float = 0.03
    sample_rate: int = 16000


@dataclass(frozen=True)
class AnalysisResult:
    duration: float
    rms: np.ndarray
    hop_s: float
    auto_threshold: float


def _terminate_process(proc: subprocess.Popen[bytes]) -> None:
    # The reader owns its pipe handles. Closing a BufferedReader here can wait
    # for a blocked read() and prevent us from ever terminating the child.
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=0.75)
        return
    except Exception:
        pass
    try:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=0.75)
    except Exception:
        pass


class AnalysisCancellation:
    """Thread-safe cancellation token that can interrupt the active FFmpeg process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = False
        self._process: subprocess.Popen[bytes] | None = None

    @property
    def is_cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            process = self._process
        if process is not None:
            _terminate_process(process)

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled:
            raise AnalysisCancelled()

    def attach_process(self, process: subprocess.Popen[bytes]) -> None:
        with self._lock:
            if not self._cancelled:
                self._process = process
                return
        _terminate_process(process)
        raise AnalysisCancelled()

    def detach_process(self, process: subprocess.Popen[bytes]) -> None:
        with self._lock:
            if self._process is process:
                self._process = None


class _ProgressReporter:
    def __init__(self, callback: ProgressCallback | None) -> None:
        self._callback = callback
        self._last_value = -1

    def emit(self, value: int) -> None:
        value = max(0, min(100, int(value)))
        if value <= self._last_value:
            return
        self._last_value = value
        if self._callback is not None:
            self._callback(value)


def _validate_request(request: AnalysisRequest) -> None:
    if not Path(request.path).is_file():
        raise AudioAnalysisError(f"File non trovato: {request.path}", code="source_not_found")
    if request.hop_s <= 0.0:
        raise AudioAnalysisError("hop_s deve essere maggiore di zero.", code="invalid_request")
    if request.sample_rate <= 0:
        raise AudioAnalysisError("sample_rate deve essere maggiore di zero.", code="invalid_request")


def _extract_rms(
    *,
    ffmpeg: str,
    request: AnalysisRequest,
    duration: Optional[float],
    audio_filter: str,
    cancellation: AnalysisCancellation,
    progress: _ProgressReporter,
) -> np.ndarray:
    command = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-v",
        "error",
        "-probesize",
        "50M",
        "-analyzeduration",
        "100M",
        "-fflags",
        "+genpts",
        "-err_detect",
        "ignore_err",
        "-i",
        request.path,
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-af",
        audio_filter,
        "-ac",
        "1",
        "-ar",
        str(request.sample_rate),
        "-f",
        "s16le",
        "pipe:1",
    ]
    frame_length = max(int(request.sample_rate * request.hop_s), 256)
    process = popen_no_window(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        cancellation.attach_process(process)
    except AnalysisCancelled:
        # No reader has started yet, so these handles can be closed safely.
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        raise
    assert process.stdout is not None

    stderr_buffer = bytearray()
    stderr_limit = 256 * 1024

    def drain_stderr() -> None:
        if process.stderr is None:
            return
        try:
            while True:
                chunk = process.stderr.read(8192)
                if not chunk:
                    return
                remaining = stderr_limit - len(stderr_buffer)
                if remaining > 0:
                    stderr_buffer.extend(chunk[:remaining])
        except Exception:
            return

    stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
    stderr_thread.start()
    rms_values: list[float] = []
    leftover: np.ndarray = np.zeros(0, dtype=np.int16)
    samples_read = 0
    return_code = -1

    try:
        while True:
            cancellation.raise_if_cancelled()
            try:
                chunk = process.stdout.read(65536)
            except Exception:
                cancellation.raise_if_cancelled()
                raise
            if not chunk:
                break

            samples_read += len(chunk) // 2
            if duration is not None and duration > 0.0:
                current = min(99, int((samples_read / float(request.sample_rate)) / duration * 100.0))
                progress.emit(current)

            samples = np.frombuffer(chunk, dtype=np.int16)
            if leftover.size:
                samples = np.concatenate([leftover, samples])
            frame_count = samples.size // frame_length
            if frame_count <= 0:
                leftover = samples
                continue

            frames = samples[: frame_count * frame_length].astype(np.float32).reshape(frame_count, frame_length)
            rms_values.extend((np.sqrt(np.mean(frames * frames, axis=1)) / 32768.0).tolist())
            leftover = samples[frame_count * frame_length :]

        return_code = process.wait()
    except AnalysisCancelled:
        _terminate_process(process)
        raise
    except Exception:
        _terminate_process(process)
        raise
    finally:
        cancellation.detach_process(process)
        stderr_thread.join(timeout=1.0)
        for stream in (process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass

    cancellation.raise_if_cancelled()
    if return_code != 0:
        detail = bytes(stderr_buffer).decode("utf-8", errors="ignore").strip()
        raise AudioAnalysisError(detail or "FFmpeg audio extract failed", code="ffmpeg_extract_failed")
    return np.asarray(rms_values, dtype=np.float32)


def analyze_audio(
    request: AnalysisRequest,
    *,
    cancellation: AnalysisCancellation | None = None,
    on_progress: ProgressCallback | None = None,
) -> AnalysisResult:
    """Analyze one media file without importing Qt or depending on UI state."""

    _validate_request(request)
    token = cancellation or AnalysisCancellation()
    token.raise_if_cancelled()
    progress = _ProgressReporter(on_progress)
    progress.emit(0)

    ffmpeg, _ = ensure_ffmpeg()
    try:
        audio_present = has_audio_stream(request.path)
    except Exception:
        audio_present = None
    if audio_present is False:
        raise AudioAnalysisError(
            "Nessuna traccia audio trovata nel file. Impossibile generare la timeline audio.",
            code="no_audio_stream",
        )

    try:
        duration: Optional[float] = float(ffprobe_duration_seconds(request.path))
    except Exception:
        duration = None

    voice_filter = "highpass=f=150,lowpass=f=3400,acompressor=threshold=0.10:ratio=4:attack=5:release=50"
    try:
        rms = _extract_rms(
            ffmpeg=ffmpeg,
            request=request,
            duration=duration,
            audio_filter=voice_filter,
            cancellation=token,
            progress=progress,
        )
    except AnalysisCancelled:
        raise
    except Exception as filtered_error:
        try:
            rms = _extract_rms(
                ffmpeg=ffmpeg,
                request=request,
                duration=duration,
                audio_filter="anull",
                cancellation=token,
                progress=progress,
            )
        except AnalysisCancelled:
            raise
        except Exception as fallback_error:
            raise AudioAnalysisError(
                "Analisi audio fallita.\n\n"
                f"Con filtro voice-focused: {filtered_error}\n\n"
                f"Senza filtro (fallback): {fallback_error}",
                code="ffmpeg_extract_failed",
            ) from fallback_error

    if rms.size < 10:
        raise AudioAnalysisError("Audio troppo corto o non processabile.", code="audio_too_short")

    p20 = float(np.percentile(rms, 20))
    p90 = float(np.percentile(rms, 90))
    auto_threshold = float(max(0.0008, min(0.06, p20 + 0.12 * (p90 - p20))))
    if duration is None or duration <= 0.0:
        duration = float(rms.size) * request.hop_s

    token.raise_if_cancelled()
    progress.emit(100)
    return AnalysisResult(
        duration=float(duration),
        rms=rms,
        hop_s=request.hop_s,
        auto_threshold=auto_threshold,
    )
