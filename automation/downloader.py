from __future__ import annotations

import importlib
import math
import os
import re
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from automation.manager import PipelineManager
from automation.execution import job_execution
from automation.models import PipelineJob, PipelineState
from automation.retry import automatic_retry_allowed, transient_network_error
from utils.ffmpeg import ensure_ffmpeg, ffprobe_duration_seconds, has_video_stream
from utils.redaction import redact_secrets
from utils.subprocess_utils import popen_no_window


ProgressCallback = Callable[[int], None]
StartedCallback = Callable[[str], None]
FinishedCallback = Callable[[PipelineJob], None]
FailedCallback = Callable[[PipelineJob, str], None]

_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9._-]+")
_SAFE_HEADER_NAME = re.compile(r"^[A-Za-z0-9-]+$")
_TWITCH_VIDEO_PATH = re.compile(r"^/videos/\d+/?$")
_URL_IN_MESSAGE = re.compile(r"https?://\S+", re.IGNORECASE)


class PipelineDownloadError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = str(code or "download_failed")
        self.retryable = retryable


class PipelineDownloadCancelled(PipelineDownloadError):
    def __init__(self) -> None:
        super().__init__("download_cancelled", "Download cancelled.")


class PipelineDownloadDependencyError(PipelineDownloadError):
    pass


@dataclass(frozen=True)
class ResolvedMedia:
    url: str
    headers: Mapping[str, str]
    title: str = ""


@dataclass(frozen=True)
class DownloadRequest:
    job_id: str
    vod_id: str
    vod_url: str
    start_s: float
    end_s: float
    output_dir: Path

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s

    def validate(self) -> None:
        if not self.job_id.strip() or not self.vod_id.strip():
            raise PipelineDownloadError("invalid_download", "The pipeline job is incomplete.")
        parsed = urlsplit(self.vod_url)
        hostname = str(parsed.hostname or "").lower()
        if parsed.scheme != "https" or hostname not in {"twitch.tv", "www.twitch.tv"}:
            raise PipelineDownloadError("invalid_download_url", "The VOD URL is not a valid Twitch URL.")
        if not _TWITCH_VIDEO_PATH.fullmatch(parsed.path):
            raise PipelineDownloadError("invalid_download_url", "The Twitch VOD URL has an invalid path.")
        if (
            not math.isfinite(self.start_s)
            or not math.isfinite(self.end_s)
            or self.start_s < 0.0
            or self.end_s <= self.start_s
        ):
            raise PipelineDownloadError("invalid_download_range", "The selected VOD range is invalid.")


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    duration_s: float
    reused: bool = False


class MediaResolver(Protocol):
    def resolve(self, url: str) -> ResolvedMedia: ...


class RangeDownloader(Protocol):
    def download(
        self,
        request: DownloadRequest,
        *,
        cancellation: "DownloadCancellation | None" = None,
        on_progress: ProgressCallback | None = None,
    ) -> DownloadResult: ...


def default_download_dir() -> Path:
    custom = str(os.environ.get("AUTO_CUTTER_DOWNLOAD_DIR", "") or "").strip()
    if custom:
        return Path(custom).expanduser().resolve()
    return Path.home() / "Videos" / "Auto Cutter" / "Automation"


def _redact_message(value: object) -> str:
    message = redact_secrets(str(value or "").strip())
    message = _URL_IN_MESSAGE.sub("[URL]", message)
    return message[-1500:] or "Unknown download error."


def _load_yt_dlp() -> Any:
    try:
        return importlib.import_module("yt_dlp")
    except Exception as exc:
        raise PipelineDownloadDependencyError(
            "yt_dlp_missing",
            "yt-dlp is not installed. Reinstall the Auto Cutter runtime dependencies.",
        ) from exc


class _YtDlpLogger:
    def debug(self, _message: str) -> None:
        pass

    def info(self, _message: str) -> None:
        pass

    def warning(self, _message: str) -> None:
        pass

    def error(self, _message: str) -> None:
        pass


class YtDlpMediaResolver:
    """Resolve a Twitch page to its best direct media stream without downloading it."""

    def __init__(self, module_loader: Callable[[], Any] = _load_yt_dlp) -> None:
        self._module_loader = module_loader

    def _extract(self, url: str) -> Mapping[str, Any]:
        module = self._module_loader()
        options = {
            "format": "best",
            "ignoreconfig": True,
            "logger": _YtDlpLogger(),
            "noplaylist": True,
            "no_warnings": True,
            "quiet": True,
            "socket_timeout": 20,
        }
        try:
            with module.YoutubeDL(options) as downloader:
                raw = downloader.extract_info(url, download=False)
        except PipelineDownloadError:
            raise
        except Exception as exc:
            raise PipelineDownloadError(
                "stream_resolution_failed",
                f"Could not resolve the Twitch VOD stream: {_redact_message(exc)}",
                retryable=transient_network_error(exc),
            ) from exc

        return self._first_video(raw)

    def metadata(self, url: str) -> dict[str, Any]:
        """Return public VOD metadata, excluding signed URLs and request headers."""
        info = self._extract(url)
        return {
            "id": str(info.get("id") or ""),
            "title": str(info.get("title") or ""),
            "duration_s": info.get("duration"),
            "channel_id": str(info.get("channel_id") or info.get("uploader_id") or ""),
            "channel": str(info.get("channel") or info.get("uploader") or ""),
        }

    def resolve(self, url: str) -> ResolvedMedia:
        info = self._extract(url)
        media_url = str(info.get("url", "") or "").strip()
        if not media_url:
            raise PipelineDownloadError(
                "stream_resolution_failed",
                "yt-dlp did not return a playable stream for this Twitch VOD.",
            )
        headers_raw = info.get("http_headers", {})
        if not isinstance(headers_raw, Mapping):
            headers_raw = {}
        headers = {
            str(key): str(value)
            for key, value in headers_raw.items()
            if str(key).strip() and value is not None
        }
        return ResolvedMedia(
            url=media_url,
            headers=headers,
            title=str(info.get("title", "") or "").strip(),
        )

    @staticmethod
    def _first_video(raw: object) -> Mapping[str, Any]:
        if not isinstance(raw, Mapping):
            raise PipelineDownloadError(
                "stream_resolution_failed",
                "yt-dlp returned an invalid response for this Twitch VOD.",
            )
        entries = raw.get("entries")
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, Mapping):
                    return entry
            raise PipelineDownloadError(
                "stream_resolution_failed",
                "The Twitch response did not contain a playable VOD.",
            )
        return raw


class DownloadCancellation:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise PipelineDownloadCancelled()

    def attach_process(self, process: subprocess.Popen[str]) -> None:
        with self._lock:
            if not self._event.is_set():
                self._process = process
                return
        _terminate_process(process)
        raise PipelineDownloadCancelled()

    def detach_process(self, process: subprocess.Popen[str]) -> None:
        with self._lock:
            if self._process is process:
                self._process = None

    def cancel(self) -> None:
        self._event.set()
        with self._lock:
            process = self._process
        if process is not None:
            _terminate_process(process)


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=1.5)
        return
    except Exception:
        pass
    try:
        process.kill()
        process.wait(timeout=1.0)
    except Exception:
        pass


class FfmpegRangeDownloader:
    def __init__(
        self,
        resolver: MediaResolver | None = None,
        *,
        ffmpeg_path: str | Path | None = None,
        duration_probe: Callable[[str], float] = ffprobe_duration_seconds,
        video_probe: Callable[[str], bool] | None = None,
    ) -> None:
        self.resolver = resolver or YtDlpMediaResolver()
        self._ffmpeg_path = str(ffmpeg_path) if ffmpeg_path is not None else None
        self._duration_probe = duration_probe
        self._video_probe = video_probe

    def download(
        self,
        request: DownloadRequest,
        *,
        cancellation: DownloadCancellation | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> DownloadResult:
        request.validate()
        token = cancellation or DownloadCancellation()
        token.raise_if_cancelled()
        output_dir = Path(request.output_dir).expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / self._output_name(request)

        reused_duration = self._valid_existing_duration(output_path, request.duration_s)
        if reused_duration is not None:
            self._emit_progress(on_progress, 100)
            return DownloadResult(output_path, reused_duration, reused=True)

        self._emit_progress(on_progress, 0)
        media = self.resolver.resolve(request.vod_url)
        token.raise_if_cancelled()
        ffmpeg = self._ffmpeg_path or ensure_ffmpeg()[0]
        temporary = output_dir / f".{output_path.stem}.partial.mp4"
        temporary.unlink(missing_ok=True)
        command = self._build_command(ffmpeg, media, request, temporary)

        try:
            self._run_ffmpeg(
                command,
                duration_s=request.duration_s,
                cancellation=token,
                on_progress=on_progress,
            )
            token.raise_if_cancelled()
            duration = self._validate_output(temporary, request.duration_s)
            token.raise_if_cancelled()
            os.replace(temporary, output_path)
            self._emit_progress(on_progress, 100)
            return DownloadResult(output_path, duration)
        except PipelineDownloadCancelled:
            temporary.unlink(missing_ok=True)
            raise
        except PipelineDownloadError:
            temporary.unlink(missing_ok=True)
            raise
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            raise PipelineDownloadError(
                "download_failed",
                f"Could not download the selected Twitch range: {_redact_message(exc)}",
            ) from exc

    @staticmethod
    def _output_name(request: DownloadRequest) -> str:
        vod = _SAFE_COMPONENT.sub("_", request.vod_id).strip("._") or "vod"
        job = _SAFE_COMPONENT.sub("_", request.job_id).strip("._")[:10] or "job"
        start_ms = max(0, int(round(request.start_s * 1000.0)))
        end_ms = max(start_ms + 1, int(round(request.end_s * 1000.0)))
        return f"twitch_{vod}_{start_ms}-{end_ms}_{job}.mp4"

    def _valid_existing_duration(self, output_path: Path, expected_duration_s: float | None = None) -> float | None:
        if not output_path.is_file() or output_path.stat().st_size <= 1024:
            return None
        try:
            duration = self._validate_output(output_path, expected_duration_s)
        except Exception:
            return None
        return duration

    def _validate_output(self, output_path: Path, expected_duration_s: float | None = None) -> float:
        if not output_path.is_file() or output_path.stat().st_size <= 1024:
            raise PipelineDownloadError(
                "invalid_download_output",
                "FFmpeg completed without producing a valid video file.",
            )
        try:
            duration = float(self._duration_probe(str(output_path)))
        except Exception as exc:
            raise PipelineDownloadError(
                "invalid_download_output",
                "The downloaded Twitch range could not be validated.",
            ) from exc
        video_probe = self._video_probe or has_video_stream
        if not math.isfinite(duration) or duration <= 0.0 or not video_probe(str(output_path)):
            raise PipelineDownloadError(
                "invalid_download_output",
                "The downloaded Twitch range has no playable duration.",
            )
        if expected_duration_s is not None:
            # Stream-copy boundaries can include a GOP and AAC priming. A
            # short interrupted recording must still never count as complete.
            tolerance = max(2.0, min(10.0, float(expected_duration_s) * 0.05))
            if abs(duration - expected_duration_s) > tolerance:
                raise PipelineDownloadError("invalid_download_output", "The downloaded interval is incomplete.")
        return duration

    def validate_artifact(self, path: str | Path, expected_duration_s: float | None = None) -> float:
        """Apply the downloader's full media validation during startup reconciliation."""
        return self._validate_output(Path(path).expanduser().resolve(), expected_duration_s)

    @staticmethod
    def _header_blob(headers: Mapping[str, str]) -> str:
        lines: list[str] = []
        for raw_name, raw_value in headers.items():
            name = str(raw_name).strip()
            value = str(raw_value).replace("\r", " ").replace("\n", " ").strip()
            if not name or not value or not _SAFE_HEADER_NAME.fullmatch(name):
                continue
            lines.append(f"{name}: {value}")
        return "\r\n".join(lines) + ("\r\n" if lines else "")

    @classmethod
    def _build_command(
        cls,
        ffmpeg: str,
        media: ResolvedMedia,
        request: DownloadRequest,
        output_path: Path,
    ) -> list[str]:
        command = [
            str(ffmpeg),
            "-hide_banner",
            "-v",
            "error",
            "-y",
            "-nostdin",
        ]
        is_http = urlsplit(media.url).scheme.lower() in {"http", "https"}
        if is_http:
            command.extend(
                [
                    "-reconnect",
                    "1",
                    "-reconnect_streamed",
                    "1",
                    "-reconnect_delay_max",
                    "5",
                ]
            )
        header_blob = cls._header_blob(media.headers)
        if is_http and header_blob:
            command.extend(["-headers", header_blob])
        command.extend(
            [
                "-ss",
                f"{request.start_s:.3f}",
                "-i",
                media.url,
                "-t",
                f"{request.duration_s:.3f}",
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-c",
                "copy",
                "-fflags",
                "+genpts",
                "-avoid_negative_ts",
                "make_zero",
                "-movflags",
                "+faststart",
                "-progress",
                "pipe:1",
                "-nostats",
                str(output_path),
            ]
        )
        return command

    @staticmethod
    def _run_ffmpeg(
        command: list[str],
        *,
        duration_s: float,
        cancellation: DownloadCancellation,
        on_progress: ProgressCallback | None,
    ) -> None:
        process = popen_no_window(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        cancellation.attach_process(process)
        lines: deque[str] = deque(maxlen=80)
        line_queue: deque[str] = deque()
        queue_lock = threading.Lock()
        reader_done = threading.Event()

        def read_output() -> None:
            try:
                assert process.stdout is not None
                for raw in process.stdout:
                    line = raw.strip()
                    if not line:
                        continue
                    with queue_lock:
                        line_queue.append(line)
                        lines.append(line)
            except Exception:
                pass
            finally:
                reader_done.set()

        reader = threading.Thread(target=read_output, name="twitch-ffmpeg-output", daemon=True)
        reader.start()
        last_percent = -1

        def consume_progress() -> None:
            nonlocal last_percent
            while True:
                with queue_lock:
                    if not line_queue:
                        break
                    line = line_queue.popleft()
                if not (line.startswith("out_time_ms=") or line.startswith("out_time_us=")):
                    continue
                try:
                    out_time = max(0.0, int(line.split("=", 1)[1]) / 1_000_000.0)
                except (TypeError, ValueError):
                    continue
                percent = int(max(0.0, min(99.0, (out_time / max(0.001, duration_s)) * 100.0)))
                if percent > last_percent:
                    last_percent = percent
                    FfmpegRangeDownloader._emit_progress(on_progress, percent)

        try:
            while True:
                cancellation.raise_if_cancelled()
                consume_progress()
                return_code = process.poll()
                if return_code is not None:
                    reader.join(timeout=1.0)
                    consume_progress()
                    if int(return_code) != 0:
                        details = _redact_message("\n".join(lines))
                        raise PipelineDownloadError(
                            "ffmpeg_download_failed",
                            f"FFmpeg could not download the selected Twitch range: {details}",
                            retryable=transient_network_error(details),
                        )
                    return
                if reader_done.is_set() and process.poll() is None:
                    time.sleep(0.05)
                else:
                    time.sleep(0.1)
        finally:
            cancellation.detach_process(process)
            if process.poll() is None:
                _terminate_process(process)
            try:
                if process.stdout is not None:
                    process.stdout.close()
            except Exception:
                pass

    @staticmethod
    def _emit_progress(callback: ProgressCallback | None, value: int) -> None:
        if callback is None:
            return
        callback(max(0, min(100, int(value))))


class PipelineDownloadService:
    def __init__(
        self,
        manager: PipelineManager,
        output_dir: str | Path | None = None,
        *,
        downloader_factory: Callable[[], RangeDownloader] = FfmpegRangeDownloader,
    ) -> None:
        self.manager = manager
        self.output_dir = Path(output_dir) if output_dir is not None else default_download_dir()
        self._downloader_factory = downloader_factory

    @job_execution
    def execute(
        self,
        job_id: str,
        *,
        cancellation: DownloadCancellation | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> PipelineJob:
        token = cancellation or DownloadCancellation()
        job = self.manager.get(job_id)
        if job.state == PipelineState.CANCELLED and job.metadata.get("cancelled_from") == "downloading":
            job = self.manager.resume_cancelled(job.id)
        if job.state == PipelineState.FAILED and job.retry_state == PipelineState.DOWNLOADING:
            job = self.manager.retry(job.id)
        if job.state != PipelineState.DOWNLOADING:
            raise PipelineDownloadError(
                "invalid_download_state",
                f"Pipeline job {job.id} is not waiting for download.",
            )
        if job.range_start_s is None or job.range_end_s is None:
            raise PipelineDownloadError("invalid_download_range", "The pipeline job has no selected range.")

        delivery = job.metadata.get("delivery")
        persisted_dir = delivery.get("download_dir") if isinstance(delivery, dict) else None
        request = DownloadRequest(
            job_id=job.id,
            vod_id=job.vod_id,
            vod_url=job.vod_url,
            start_s=job.range_start_s,
            end_s=job.range_end_s,
            output_dir=Path(str(persisted_dir)) if persisted_dir else self.output_dir,
        )
        last_persisted = int(job.progress)

        def report(progress: int) -> None:
            nonlocal last_persisted
            value = max(last_persisted, min(100, int(progress)))
            if value > last_persisted:
                self.manager.update_progress(job.id, value)
                last_persisted = value
            if on_progress is not None:
                on_progress(value)

        try:
            result = self._downloader_factory().download(
                request,
                cancellation=token,
                on_progress=report,
            )
            return self.manager.mark_downloaded(job.id, result.path)
        except PipelineDownloadCancelled as exc:
            self._fail_active_job(job.id, exc.code, str(exc))
            raise
        except PipelineDownloadError as exc:
            self._fail_active_job(job.id, exc.code, str(exc), retryable=exc.retryable)
            raise
        except Exception as exc:
            message = f"Unexpected download failure: {_redact_message(exc)}"
            retryable = transient_network_error(exc)
            self._fail_active_job(job.id, "download_failed", message, retryable=retryable)
            raise PipelineDownloadError("download_failed", message, retryable=retryable) from exc

    def _fail_active_job(self, job_id: str, code: str, message: str, *, retryable: bool = False) -> None:
        try:
            if self.manager.get(job_id).state == PipelineState.DOWNLOADING:
                self.manager.fail(job_id, code, _redact_message(message), automatic_retry=retryable and automatic_retry_allowed())
        except Exception:
            pass


class PipelineDownloadQueue:
    """Single-worker persistent download queue with retryable cancellation."""

    def __init__(
        self,
        manager: PipelineManager,
        output_dir: str | Path | None = None,
        *,
        downloader_factory: Callable[[], RangeDownloader] = FfmpegRangeDownloader,
        on_started: StartedCallback | None = None,
        on_progress: Callable[[str, int], None] | None = None,
        on_finished: FinishedCallback | None = None,
        on_failed: FailedCallback | None = None,
    ) -> None:
        self.manager = manager
        self.service = PipelineDownloadService(
            manager,
            output_dir,
            downloader_factory=downloader_factory,
        )
        self.on_started = on_started
        self.on_progress = on_progress
        self.on_finished = on_finished
        self.on_failed = on_failed
        self._condition = threading.Condition()
        self._pending: deque[str] = deque()
        self._queued: set[str] = set()
        self._active_job_id = ""
        self._active_cancellation: DownloadCancellation | None = None
        self._stopping = False
        self._thread: threading.Thread | None = None

    @property
    def output_dir(self) -> Path:
        return Path(self.service.output_dir)

    @property
    def active_job_id(self) -> str:
        with self._condition:
            return self._active_job_id

    @property
    def is_running(self) -> bool:
        return bool(self.active_job_id)

    def set_output_dir(self, path: str | Path) -> None:
        target = Path(path).expanduser().resolve()
        with self._condition:
            if self._active_job_id:
                raise PipelineDownloadError(
                    "download_active",
                    "The download folder cannot be changed while a download is running.",
                )
            self.service.output_dir = target

    def enqueue(self, job_id: str) -> bool:
        job = self.manager.get(job_id)
        retryable = job.state == PipelineState.FAILED and job.retry_state == PipelineState.DOWNLOADING
        retryable = retryable or (job.state == PipelineState.CANCELLED
                                  and job.metadata.get("cancelled_from") == "downloading")
        if job.state != PipelineState.DOWNLOADING and not retryable:
            raise PipelineDownloadError(
                "invalid_download_state",
                f"Pipeline job {job.id} cannot be queued for download from {job.state.value}.",
            )
        with self._condition:
            if self._stopping:
                raise PipelineDownloadError("download_queue_stopped", "The download queue is shutting down.")
            if job.id == self._active_job_id or job.id in self._queued:
                return False
            self._pending.append(job.id)
            self._queued.add(job.id)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run,
                    name="pipeline-download-queue",
                    daemon=True,
                )
                self._thread.start()
            self._condition.notify_all()
            return True

    def cancel_current(self) -> bool:
        with self._condition:
            cancellation = self._active_cancellation
        if cancellation is None:
            return False
        cancellation.cancel()
        return True

    def shutdown(self, timeout: float = 3.0) -> bool:
        with self._condition:
            self._stopping = True
            self._pending.clear()
            self._queued.clear()
            cancellation = self._active_cancellation
            thread = self._thread
            self._condition.notify_all()
        if cancellation is not None:
            cancellation.cancel()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, float(timeout)))
        return thread is None or not thread.is_alive()

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._pending and not self._stopping:
                    self._condition.wait()
                if self._stopping:
                    return
                job_id = self._pending.popleft()
                self._queued.discard(job_id)
                cancellation = DownloadCancellation()
                self._active_job_id = job_id
                self._active_cancellation = cancellation

            self._notify(self.on_started, job_id)
            try:
                def report_progress(value: int) -> None:
                    self._notify(self.on_progress, job_id, value)

                completed = self.service.execute(
                    job_id,
                    cancellation=cancellation,
                    on_progress=report_progress,
                )
                self._notify(self.on_finished, completed)
            except Exception as exc:
                try:
                    failed = self.manager.get(job_id)
                except Exception:
                    failed = None
                if failed is not None:
                    self._notify(self.on_failed, failed, _redact_message(exc))
            finally:
                with self._condition:
                    if self._active_job_id == job_id:
                        self._active_job_id = ""
                        self._active_cancellation = None
                    self._condition.notify_all()

    @staticmethod
    def _notify(callback: Callable[..., None] | None, *args: object) -> None:
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:
            pass
