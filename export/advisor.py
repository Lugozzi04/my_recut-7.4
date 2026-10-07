from __future__ import annotations

from dataclasses import dataclass
import math
import os
import time
from collections.abc import Callable

from PySide6.QtCore import QObject, Signal, Slot

from utils.codec_detection import probe_video_encoder
from utils.ffmpeg import ffprobe_duration_seconds
from utils.subprocess_utils import run_no_window


HIGH_QUALITY_ENCODER_ARGS: dict[str, list[str]] = {
    "libx264": ["-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p"],
    "h264_amf": [
        "-c:v", "h264_amf", "-quality", "quality", "-rc", "cqp",
        "-qp_i", "14", "-qp_p", "16", "-qp_b", "18", "-bf", "0", "-pix_fmt", "yuv420p",
    ],
    "h264_nvenc": [
        "-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq",
        "-rc", "constqp", "-qp", "16", "-bf", "0", "-pix_fmt", "yuv420p",
    ],
    "h264_qsv": [
        "-c:v", "h264_qsv", "-preset", "medium", "-global_quality", "16",
        "-bf", "0", "-pix_fmt", "nv12",
    ],
    "hevc_amf": [
        "-c:v", "hevc_amf", "-quality", "quality", "-rc", "cqp",
        "-qp_i", "16", "-qp_p", "18", "-qp_b", "20", "-bf", "0", "-pix_fmt", "yuv420p",
    ],
    "hevc_nvenc": [
        "-c:v", "hevc_nvenc", "-preset", "p5", "-tune", "hq",
        "-rc", "constqp", "-qp", "18", "-bf", "0", "-pix_fmt", "yuv420p",
    ],
    "hevc_qsv": [
        "-c:v", "hevc_qsv", "-preset", "medium", "-global_quality", "18",
        "-bf", "0", "-pix_fmt", "nv12",
    ],
    "libx265": ["-c:v", "libx265", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p"],
    "av1_amf": [
        "-c:v", "av1_amf", "-quality", "quality", "-rc", "cqp",
        "-qp_i", "20", "-qp_p", "22", "-pix_fmt", "yuv420p",
    ],
    "av1_nvenc": [
        "-c:v", "av1_nvenc", "-preset", "p5", "-tune", "hq",
        "-rc", "constqp", "-qp", "22", "-pix_fmt", "yuv420p",
    ],
    "av1_qsv": [
        "-c:v", "av1_qsv", "-preset", "medium", "-global_quality", "22",
        "-pix_fmt", "nv12",
    ],
    "libaom-av1": [
        "-c:v", "libaom-av1", "-cpu-used", "6", "-crf", "22", "-b:v", "0",
        "-pix_fmt", "yuv420p",
    ],
}


@dataclass(frozen=True)
class EncoderBenchmark:
    codec: str
    elapsed_seconds: float
    media_seconds: float

    @property
    def speed(self) -> float:
        return self.media_seconds / max(1e-6, self.elapsed_seconds)


@dataclass(frozen=True)
class ExportRecommendation:
    codec: str
    method: str
    workers: int
    chunks: int
    benchmarks: tuple[EncoderBenchmark, ...]
    note: str


def recommend_export_settings(
    benchmarks: list[EncoderBenchmark],
    *,
    total_seconds: float,
    segment_count: int,
    input_count: int,
    cpu_count: int | None = None,
) -> ExportRecommendation:
    usable = [item for item in benchmarks if item.elapsed_seconds > 0.0 and item.media_seconds > 0.0]
    if not usable:
        raise RuntimeError("No encoder completed the recommendation benchmark.")
    best = max(usable, key=lambda item: item.speed)
    cpus = max(2, int(cpu_count or os.cpu_count() or 4))
    budget = max(2, int(cpus * 0.75))
    is_gpu = any(tag in best.codec for tag in ("_amf", "_nvenc", "_qsv"))
    if is_gpu:
        workers = max(1, min(4, int(math.ceil(budget / 8.0))))
    else:
        workers = max(1, min(8, int(math.ceil(budget / 4.0))))

    total = max(0.0, float(total_seconds or 0.0))
    segments = max(1, int(segment_count or 1))
    target_chunks = max(workers * 3, int(math.ceil(total / 90.0)) if total > 0.0 else 1)
    max_by_duration = max(1, int(total / 30.0)) if total > 0.0 else 1
    chunks = max(1, min(target_chunks, max_by_duration, segments))
    method = "Auto (smart multi-source when compatible)" if input_count > 1 else "Auto (smart hybrid)"
    note = (
        "Recommendation only: controls were not changed. Profiles use very high quality; "
        "smart rendering preserves copied GOPs bit-for-bit."
    )
    return ExportRecommendation(
        codec=best.codec,
        method=method,
        workers=workers,
        chunks=chunks,
        benchmarks=tuple(sorted(usable, key=lambda item: item.speed, reverse=True)),
        note=note,
    )


def benchmark_export_settings(
    ffmpeg_path: str,
    input_path: str,
    *,
    requested_codec: str,
    total_seconds: float,
    segment_count: int,
    input_count: int,
    sample_seconds: float = 1.5,
    runner: Callable = run_no_window,
) -> ExportRecommendation:
    requested = str(requested_codec or "auto").strip().lower()
    candidates = ["h264_amf", "h264_nvenc", "h264_qsv", "libx264"]
    if requested in HIGH_QUALITY_ENCODER_ARGS:
        candidates.insert(0, requested)

    try:
        duration = max(0.0, float(ffprobe_duration_seconds(input_path) or 0.0))
    except Exception:
        duration = 0.0
    sample = max(0.5, min(3.0, float(sample_seconds)))
    seek = min(60.0, duration * 0.10) if duration > (sample + 2.0) else 0.0

    results: list[EncoderBenchmark] = []
    for codec in dict.fromkeys(candidates):
        args = HIGH_QUALITY_ENCODER_ARGS.get(codec)
        if not args:
            continue
        available, _reason = probe_video_encoder(
            ffmpeg_path,
            codec,
            runner=runner,
            use_cache=True,
        )
        if not available:
            continue
        cmd = [ffmpeg_path, "-hide_banner", "-v", "error", "-y", "-nostdin"]
        if seek > 1e-6:
            cmd += ["-ss", f"{seek:.3f}"]
        cmd += [
            "-t", f"{sample:.3f}", "-i", input_path,
            "-map", "0:v:0", "-an", "-sn", "-dn",
            *args,
            "-f", "null", "-",
        ]
        started = time.perf_counter()
        try:
            completed = runner(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
        except Exception:
            continue
        elapsed = max(1e-6, time.perf_counter() - started)
        if int(completed.returncode) == 0:
            results.append(EncoderBenchmark(codec, elapsed, sample))

    return recommend_export_settings(
        results,
        total_seconds=total_seconds,
        segment_count=segment_count,
        input_count=input_count,
    )


class ExportAdvisorWorker(QObject):
    finished = Signal(object)
    error = Signal(str)

    def __init__(
        self,
        ffmpeg_path: str,
        input_path: str,
        requested_codec: str,
        total_seconds: float,
        segment_count: int,
        input_count: int,
    ) -> None:
        super().__init__()
        self.ffmpeg_path = ffmpeg_path
        self.input_path = input_path
        self.requested_codec = requested_codec
        self.total_seconds = total_seconds
        self.segment_count = segment_count
        self.input_count = input_count

    @Slot()
    def run(self) -> None:
        try:
            recommendation = benchmark_export_settings(
                self.ffmpeg_path,
                self.input_path,
                requested_codec=self.requested_codec,
                total_seconds=self.total_seconds,
                segment_count=self.segment_count,
                input_count=self.input_count,
            )
        except Exception as exc:
            self.error.emit(str(exc))
            return
        self.finished.emit(recommendation)
