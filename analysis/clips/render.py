"""One FFmpeg call per clip, from the ORIGINAL sources: the kept parts are trimmed (accurate input seek), joined
with tiny audio fades on every splice, laid out (16:9 pad, or the Shorts 9:16 layout with burned captions),
loudness-normalized and encoded. The file is written as <name>.partial<ext> and promoted only after validation.

Captions and fonts are referenced RELATIVE to the clips folder (FFmpeg runs there), so no Windows path has to be
escaped inside the filter graph.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Sequence

from .layout import VerticalLayout
from .models import ClipCandidate, ClipError

SPLICE_FADE_S = 0.02
OUTPUT_SIZE = (1920, 1080)


def encoder_args(codec: str, quality: int = 18) -> list[str]:
    """Constant-quality H.264 settings (same families as the editor's export)."""
    if codec == "h264_amf":
        return ["-c:v", "h264_amf", "-quality", "quality", "-rc", "cqp", "-qp_i", str(max(0, quality - 2)),
                "-qp_p", str(quality), "-qp_b", str(quality + 2), "-bf", "0"]
    if codec == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq", "-rc", "constqp", "-qp", str(quality)]
    if codec == "h264_qsv":
        return ["-c:v", "h264_qsv", "-preset", "medium", "-global_quality", str(quality)]
    return ["-c:v", "libx264", "-preset", "medium", "-crf", str(quality)]


def _layout_chains(layout: VerticalLayout | None, captions: str | None, fonts: str | None) -> list[str]:
    """[vc] -> [vo]: nothing more for 16:9, the Shorts panels (+ captions) for 9:16."""
    if layout is None:
        return ["[vc]null[vo]"]
    chains = layout.filter_graph()
    burn = (f"ass={captions}" + (f":fontsdir={fonts}" if fonts else "")) if captions else "null"
    chains.append(f"[stack]{burn},format=yuv420p[vo]")
    return chains


def clip_command(ffmpeg: str, clip: ClipCandidate, sources: Sequence[str], target: Path, *, codec: str,
                 size: tuple[int, int] = OUTPUT_SIZE, fps: float | None = None, loudnorm: bool = True,
                 quality: int = 18, layout: VerticalLayout | None = None, captions: str | None = None,
                 fonts: str | None = None) -> list[str]:
    """layout: Shorts 9:16 (parts are normalized to layout.source, then cropped/stacked); captions/fonts: names
    relative to the working directory of the FFmpeg process."""
    if not clip.parts:
        raise ClipError("empty_clip", f"{clip.id} has no kept content")
    w, h = layout.source if layout is not None else size
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y"]
    chains, labels = [], []
    for n, (index, start, end) in enumerate(clip.parts):
        duration = end - start
        cmd += ["-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(sources[index])]
        video = (f"[{n}:v:0]scale={w}:{h}:force_original_aspect_ratio=decrease,"
                 f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
                 + (f",fps={fps:g}" if fps else "") + f"[v{n}]")
        fade = min(SPLICE_FADE_S, duration / 4)
        audio = (f"[{n}:a:0]aresample=48000,aformat=channel_layouts=stereo,"
                 f"afade=t=in:d={fade:.3f},afade=t=out:st={max(0.0, duration - fade):.3f}:d={fade:.3f}[a{n}]")
        chains += [video, audio]
        labels.append(f"[v{n}][a{n}]")
    chains.append("".join(labels) + f"concat=n={len(clip.parts)}:v=1:a=1[vc][ac]")
    chains += _layout_chains(layout, captions, fonts)
    chains.append("[ac]" + ("loudnorm=I=-14:TP=-1.5:LRA=11," if loudnorm else "") + "aresample=48000[ao]")
    cmd += ["-filter_complex", ";".join(chains), "-map", "[vo]", "-map", "[ao]",
            *encoder_args(codec, quality), "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart",
            "-progress", "pipe:1", "-nostats", str(target)]
    return cmd


def preview_command(ffmpeg: str, image: Path, layout: VerticalLayout, target: Path, *,
                    captions: str | None = None, fonts: str | None = None) -> list[str]:
    """The Shorts layout applied to one still frame (same graph as the render): a WYSIWYG preview PNG."""
    w, h = layout.source
    chains = [f"[0:v]scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
              "setsar=1[vc]", *_layout_chains(layout, captions, fonts)]
    return [ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-i", str(image),
            "-filter_complex", ";".join(chains), "-map", "[vo]", "-frames:v", "1", "-update", "1", str(target)]


def partial_path(target: Path) -> Path:
    return target.with_name(target.stem + ".partial" + target.suffix)


def render_clip(clip: ClipCandidate, sources: Sequence[str], target: Path, *, ffmpeg: str, codec: str,
                cancellation: Any = None, on_progress: Callable[[float], None] | None = None,
                fps: float | None = None, loudnorm: bool = True, layout: VerticalLayout | None = None,
                captions: str | None = None, fonts: str | None = None) -> dict[str, Any]:
    """Renders one clip; returns {path, duration_s, codec, runtime_s}. Never leaves a half-written final file."""
    from utils.ffmpeg import ffprobe_duration_seconds, has_audio_stream, has_video_stream
    from utils.subprocess_utils import popen_no_window
    import subprocess

    missing = [str(sources[i]) for i in {p[0] for p in clip.parts} if not Path(sources[i]).is_file()]
    if missing:
        raise ClipError("source_missing", "Source video not found: " + ", ".join(missing))
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = partial_path(target)
    cmd = clip_command(ffmpeg, clip, sources, partial, codec=codec, fps=fps, loudnorm=loudnorm, layout=layout,
                       captions=captions, fonts=fonts)
    started = time.monotonic()
    process = popen_no_window(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              encoding="utf-8", errors="replace", cwd=str(target.parent))
    if cancellation is not None:
        cancellation.attach_process(process)
    tail: list[str] = []
    try:
        import threading

        def drain() -> None:
            assert process.stderr is not None
            for line in process.stderr:
                tail.append(line)
                del tail[:-40]
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        assert process.stdout is not None
        for line in process.stdout:
            match = re.match(r"out_time_us=(\d+)", line.strip())
            if match and on_progress is not None and clip.duration > 0:
                on_progress(min(1.0, int(match.group(1)) / 1e6 / clip.duration))
        code = process.wait()
        reader.join(timeout=5)
    finally:
        if cancellation is not None:
            cancellation.detach_process(process)
    if cancellation is not None and cancellation.is_cancelled:
        partial.unlink(missing_ok=True)
        raise ClipError("cancelled", "Clip rendering cancelled")
    if code != 0:
        partial.unlink(missing_ok=True)
        raise ClipError("ffmpeg_failed", f"FFmpeg failed for {clip.id} (exit {code}): " + "".join(tail[-8:]).strip())
    duration = ffprobe_duration_seconds(str(partial))
    if not (has_video_stream(str(partial)) and has_audio_stream(str(partial))) or abs(duration - clip.duration) > 1.0:
        partial.unlink(missing_ok=True)
        raise ClipError("invalid_output", f"{clip.id}: rendered file failed validation ({duration:.2f} s)")
    os.replace(partial, target)
    if on_progress is not None:
        on_progress(1.0)
    return {"path": str(target), "duration_s": round(duration, 3), "codec": codec,
            "runtime_s": round(time.monotonic() - started, 2)}
