from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
import threading
from collections.abc import Callable

from utils.subprocess_utils import run_no_window


H264_HARDWARE_CODECS = ("h264_nvenc", "h264_qsv", "h264_amf")
SUPPORTED_VIDEO_CODECS = {
    "auto",
    "h264_nvenc",
    "h264_qsv",
    "h264_amf",
    "hevc_amf",
    "av1_amf",
    "libx264",
}


@dataclass(frozen=True)
class CodecSelection:
    requested: str
    resolved: str
    fallback_reason: str = ""

    @property
    def used_fallback(self) -> bool:
        return self.requested not in {"", "auto", self.resolved}


_PROBE_CACHE: dict[tuple[str, str], tuple[bool, str]] = {}
_PROBE_LOCK = threading.Lock()


def clear_encoder_probe_cache() -> None:
    with _PROBE_LOCK:
        _PROBE_CACHE.clear()


def probe_video_encoder(
    ffmpeg_path: str,
    encoder: str,
    *,
    timeout_s: float = 6.0,
    runner: Callable[..., subprocess.CompletedProcess] = run_no_window,
    use_cache: bool = True,
) -> tuple[bool, str]:
    ffmpeg = str(ffmpeg_path or "").strip()
    codec = str(encoder or "").strip().lower()
    if not ffmpeg or not Path(ffmpeg).exists():
        return False, "FFmpeg executable not found"
    if codec not in SUPPORTED_VIDEO_CODECS or codec == "auto":
        return False, f"Unsupported video encoder: {codec or '<empty>'}"

    key = (str(Path(ffmpeg).resolve()).lower(), codec)
    if use_cache:
        with _PROBE_LOCK:
            cached = _PROBE_CACHE.get(key)
        if cached is not None:
            return cached

    cmd = [
        ffmpeg,
        "-hide_banner",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=64x64:r=25:d=0.08",
        "-frames:v",
        "1",
        "-an",
        "-c:v",
        codec,
        "-pix_fmt",
        "yuv420p",
        "-f",
        "null",
        "-",
    ]
    try:
        result = runner(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1.0, float(timeout_s)),
        )
        ok = int(result.returncode) == 0
        reason = ""
        if not ok:
            error_lines = str(result.stderr or "").strip().splitlines()
            reason = error_lines[-1] if error_lines else "encoder probe failed"
    except subprocess.TimeoutExpired:
        ok, reason = False, "encoder probe timed out"
    except Exception as exc:
        ok, reason = False, str(exc)

    outcome = (bool(ok), str(reason))
    if use_cache:
        with _PROBE_LOCK:
            _PROBE_CACHE[key] = outcome
    return outcome


def resolve_video_codec(
    ffmpeg_path: str,
    requested: str | None,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = run_no_window,
    use_cache: bool = True,
) -> CodecSelection:
    choice = str(requested or "auto").strip().lower()
    if choice not in SUPPORTED_VIDEO_CODECS:
        choice = "auto"

    candidates = H264_HARDWARE_CODECS + ("libx264",) if choice == "auto" else (choice,)
    failures: list[str] = []
    for candidate in candidates:
        ok, reason = probe_video_encoder(
            ffmpeg_path,
            candidate,
            runner=runner,
            use_cache=use_cache,
        )
        if ok:
            return CodecSelection(requested=choice, resolved=candidate)
        failures.append(f"{candidate}: {reason or 'unavailable'}")

    if choice != "auto" and choice != "libx264":
        ok, software_reason = probe_video_encoder(
            ffmpeg_path,
            "libx264",
            runner=runner,
            use_cache=use_cache,
        )
        if ok:
            return CodecSelection(
                requested=choice,
                resolved="libx264",
                fallback_reason="; ".join(failures),
            )
        failures.append(f"libx264: {software_reason or 'unavailable'}")

    raise RuntimeError("No usable video encoder found (" + "; ".join(failures) + ")")
