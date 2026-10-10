"""Local media extraction (FFmpeg input seek); never decodes outside the window."""
from __future__ import annotations

from pathlib import Path
import subprocess

from analysis.audio_service import AnalysisCancellation, AnalysisCancelled
from utils.ffmpeg import ensure_ffmpeg
from utils.subprocess_utils import popen_no_window
from .models import TimeRange


def extract_wav(video: str | Path, window: TimeRange, target: Path, *, sample_rate: int = 16000,
                cancellation: AnalysisCancellation | None = None) -> Path:
    token = cancellation or AnalysisCancellation()
    token.raise_if_cancelled()
    ffmpeg, _ = ensure_ffmpeg()
    command = [ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-y", "-ss", f"{window.start:.6f}",
               "-i", str(video), "-t", f"{window.duration:.6f}", "-map", "0:a:0", "-vn", "-ac", "1",
               "-ar", str(int(sample_rate)), "-c:a", "pcm_s16le", str(target)]
    process = popen_no_window(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    token.attach_process(process)
    try:
        _, stderr = process.communicate()
    finally:
        token.detach_process(process)
    if token.is_cancelled:
        raise AnalysisCancelled()
    if process.returncode != 0 or not target.is_file():
        raise RuntimeError((stderr or b"").decode("utf-8", "ignore").strip() or "FFmpeg audio extraction failed")
    return target
