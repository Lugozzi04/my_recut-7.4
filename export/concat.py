"""Small headless two-file concat. Copy when safe; normalize incompatible inputs without an editing pipeline."""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from fractions import Fraction
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from export.output_safety import atomic_promote_output, ensure_output_is_safe
from utils.ffconcat import quote_ffconcat_path
from utils.ffmpeg import ensure_ffmpeg, run_cmd
from utils.subprocess_utils import run_no_window


class ConcatError(RuntimeError):
    def __init__(self, code: str, message: str, exit_code: int = 30) -> None:
        super().__init__(message)
        self.code, self.exit_code = code, exit_code


@dataclass(frozen=True)
class ConcatResult:
    path: Path
    duration_s: float
    audio_reencoded: bool
    video_reencoded: bool = False


_VIDEO = ("codec_name", "profile", "level", "width", "height", "pix_fmt",
          "sample_aspect_ratio", "r_frame_rate", "avg_frame_rate", "time_base",
          "field_order", "color_range", "color_space", "color_transfer", "color_primaries",
          "extradata_hash", "side_data_list")
_AUDIO = ("codec_name", "profile", "sample_fmt", "sample_rate", "channels",
          "channel_layout", "time_base", "extradata_hash")
_TIMING = re.compile(r"non[- ]monoton|invalid.*(?:timestamp|dts)|out of order|timestamp discontinuity", re.I)


def _probe(ffprobe: str, source: Path) -> list[dict[str, Any]]:
    result = run_cmd([ffprobe, "-v", "error", "-show_streams", "-show_data_hash", "sha256",
                      "-of", "json", str(source)], timeout_s=30)
    try:
        streams = json.loads(result.stdout)["streams"]
        if result.returncode or result.stderr.strip() or not isinstance(streams, list) or not streams:
            raise ValueError(result.stderr.strip() or "No media streams")
        # Auxiliary timecode/data/subtitle streams are not media to concatenate.
        # Keep real A/V indices so the planner can detect demuxer layout changes.
        streams = [s for s in streams if s.get("codec_type") in ("video", "audio")]
        if ([s.get("codec_type") for s in streams] not in (["video"], ["video", "audio"], ["audio", "video"])
                or any(s.get("disposition", {}).get("attached_pic") for s in streams)):
            raise ValueError("Only one video and optionally one audio stream are supported")
        return streams
    except (ValueError, KeyError, TypeError) as exc:
        raise ConcatError("invalid_media", f"Cannot inspect {source.name}: {exc}", 2) from exc


def _duration(stream: dict[str, Any]) -> float:
    try:
        raw = stream.get("duration")
        if raw is None or raw == "N/A":
            h, m, s = stream["tags"]["DURATION"].split(":")
            value = float(h)*3600 + float(m)*60 + float(s)
        else:
            value = float(raw)
        if not math.isfinite(value) or value <= 0:
            raise ValueError
        return value
    except (ValueError, TypeError, KeyError) as exc:
        raise ConcatError("unknown_duration", "Accurate stream durations are required for safe concatenation.", 2) from exc


def _starts_at_zero(stream: dict[str, Any]) -> bool:
    try:
        value = float(stream.get("start_time", "nan"))
        return math.isfinite(value) and abs(value) <= .001
    except (TypeError, ValueError):
        return False


def _video(streams: list[dict[str, Any]]) -> dict[str, Any]:
    return next(s for s in streams if s["codec_type"] == "video")


@dataclass(frozen=True)
class ConcatPlan:
    durations: list[float]
    fps: float
    video_reencoded: bool
    audio_reencoded: bool
    reasons: tuple[str, ...]


def _number(value: Any) -> float:
    try:
        result = float(value)
        if math.isfinite(result):
            return result
    except (TypeError, ValueError):
        pass
    raise ConcatError("unsupported_timing", "Media timestamps must be finite and available.", 2)


def _rate(value: Any) -> float:
    try:
        rate = float(Fraction(value))
        if math.isfinite(rate) and rate > 0:
            return rate
    except (TypeError, ValueError, ZeroDivisionError):
        pass
    raise ConcatError("unsupported_timing", "A valid video frame rate is required.", 2)


def _sar(value: Any) -> float:
    if value in (None, "N/A", "0:1"):
        return 1.
    try:
        ratio = _rate(str(value).replace(":", "/"))
        return ratio
    except ConcatError as exc:
        raise ConcatError("unsupported_aspect_ratio", "Invalid sample aspect ratio.", 2) from exc


def _plan(first: list[dict[str, Any]], second: list[dict[str, Any]]) -> ConcatPlan:
    videos = [_video(s) for s in (first, second)]
    audios = [next((s for s in streams if s["codec_type"] == "audio"), None) for streams in (first, second)]
    if (audios[0] is None) != (audios[1] is None):
        raise ConcatError("incompatible_media", "The files must have the same video/audio stream layout.", 2)
    fps = _rate(videos[0]["avg_frame_rate"])
    nominal = _rate(videos[0]["r_frame_rate"])
    if math.isclose(fps, nominal, rel_tol=1e-6):
        fps = nominal
    reasons = []
    video_changes = []
    for key in _VIDEO:
        a,b = videos[0].get(key),videos[1].get(key)
        if key in ("avg_frame_rate", "r_frame_rate"):
            same = math.isclose(_rate(a), _rate(b), rel_tol=1e-6)
        elif key == "sample_aspect_ratio":
            same = math.isclose(_sar(a), _sar(b), rel_tol=1e-6)
        elif key.startswith("color_") and (a in (None,"unknown","unspecified") or b in (None,"unknown","unspecified")):
            same = True  # absent metadata is not proof of a different color space
        else:
            same = a == b
        if not same:
            video_changes.append("video."+key)
    reasons.extend(video_changes)
    audio_changes = []
    if audios[0] is not None and audios[1] is not None:
        audio_changes = ["audio."+k for k in _AUDIO if audios[0].get(k) != audios[1].get(k)]
        reasons.extend(audio_changes)
    # Named stream selection handles a different index order without dropping audio.
    if ([s["codec_type"] for s in first] != [s["codec_type"] for s in second]
            or [s.get("index") for s in first] != [s.get("index") for s in second]):
        video_changes.append("stream_order")
        reasons.append("stream_order")
    for streams in (first,second):
        for stream in streams:
            if not _starts_at_zero(stream):
                _number(stream.get("start_time"))
                video_changes.append("stream_offsets")
                reasons.append("stream_offsets")
    durations = [_duration(v) for v in videos]
    for audio,duration in zip(audios,durations):
        if audio is not None and abs(_duration(audio)-duration) > max(.05,1/fps)+1e-6:
            video_changes.append("stream_durations")
            reasons.append("stream_durations")
    reencode = bool(video_changes)
    if reencode:
        for video in videos:
            if video.get("color_transfer") in ("smpte2084","arib-std-b67"):
                raise ConcatError("unsupported_color", "HDR normalization is not supported by concat; export matching SDR files first.", 2)
            if video.get("field_order") not in (None,"unknown","progressive"):
                raise ConcatError("unsupported_video", "Interlaced normalization requires the existing export workflow.", 2)
            if any(item.get("rotation",0) for item in video.get("side_data_list",[])):
                raise ConcatError("unsupported_video", "Rotate/export both videos before concatenating incompatible rotated files.", 2)
        for field in ("color_primaries","color_transfer","color_space","color_range"):
            a,b = (v.get(field) for v in videos)
            if a not in (None,"unknown","unspecified") and b not in (None,"unknown","unspecified") and a != b:
                raise ConcatError("unsupported_color", "Different declared color parameters require an explicit color conversion.", 2)
        # Include leading/trailing audio and video offsets rather than dropping
        # audio to force each segment to match the raw video duration.
        durations = []
        for streams in (first,second):
            origin = min(_number(s.get("start_time")) for s in streams)
            end = max(_number(s.get("start_time"))+_duration(s) for s in streams)
            durations.append(math.ceil((end-origin)*fps-1e-6)/fps)
    return ConcatPlan(durations,fps,reencode,bool(audio_changes) or reencode,tuple(sorted(set(reasons))))


def _target_size(video: dict[str, Any]) -> tuple[int,int]:
    # Normalize anamorphic pixels into square pixels without stretching.
    return (max(2,2*round(int(video["width"])*_sar(video.get("sample_aspect_ratio"))/2)),
            max(2,2*round(int(video["height"])/2)))


def normalization_video_filter(width: int, height: int, pixel_format: str = "yuv420p") -> str:
    """Shared SAR/size normalization used by concat and selected multi-source exports."""
    return (f"scale=w='trunc(iw*sar/2)*2':h=ih,setsar=1,"
            f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,format={pixel_format},setsar=1")


def _normalized_command(ffmpeg: str, sources: list[Path], streams: list[list[dict[str, Any]]],
                        plan: ConcatPlan, partial: Path, common: list[str]) -> list[str]:
    first_video = _video(streams[0])
    width,height = _target_size(first_video)
    fps = str(Fraction(plan.fps).limit_denominator(1000000))
    audio = next((s for s in streams[0] if s["codec_type"] == "audio"),None)
    if audio is not None and audio.get("channels") not in (1,2):
        raise ConcatError("unsupported_audio", "Normalization currently supports mono/stereo audio; export matching multichannel files first.",2)
    filters = []
    labels = []
    for i,(source_streams,duration) in enumerate(zip(streams,plan.durations)):
        video = _video(source_streams)
        origin = min(_number(s.get("start_time")) for s in source_streams)
        video_delay = _number(video.get("start_time"))-origin
        tail = max(0.,duration-video_delay-_duration(video))+1/plan.fps
        # Explicit initial/final holds cover original A/V offsets. They avoid
        # black frames while preserving all source audio on the same timeline.
        filters.append(f"[{i}:v:0]setpts=PTS-STARTPTS,"
            f"{normalization_video_filter(width, height)},"
            f"fps={fps},tpad=start_mode=clone:start_duration={video_delay:.9f}:"
            f"stop_mode=clone:stop_duration={tail:.9f},trim=duration={duration:.9f},"
            f"setpts=PTS-STARTPTS,settb=AVTB[v{i}]")
        labels.append(f"[v{i}]")
        if audio is not None:
            original_audio = next(s for s in source_streams if s["codec_type"] == "audio")
            delay = _number(original_audio.get("start_time"))-origin
            layout = "mono" if audio["channels"] == 1 else "stereo"
            filters.append(f"[{i}:a:0]asetpts=PTS-STARTPTS+{delay:.9f}/TB,"
                f"aresample={audio['sample_rate']}:async=1:first_pts=0,"
                f"aformat=channel_layouts={layout},apad,atrim=duration={duration:.9f},"
                f"asetpts=PTS-STARTPTS[a{i}]")
            labels.append(f"[a{i}]")
    filters.append("".join(labels)+f"concat=n=2:v=1:a={int(audio is not None)}[vout]"+("[aout]" if audio is not None else ""))
    command = [ffmpeg,"-nostdin","-hide_banner","-loglevel","warning","-xerror","-copyts"]
    for source in sources:
        command.extend(["-i",str(source)])
    command.extend(["-filter_complex",";".join(filters),"-map","[vout]"])
    if audio is not None:
        command.extend(["-map","[aout]","-c:a","aac","-b:a","192k",
                        "-ar",str(audio["sample_rate"]),"-ac",str(audio["channels"])])
    command.extend(["-c:v","libx264","-preset","fast","-crf","18","-pix_fmt","yuv420p","-r",fps])
    for option,key in (("-colorspace","color_space"),("-color_trc","color_transfer"),
                       ("-color_primaries","color_primaries"),("-color_range","color_range")):
        value = first_video.get(key)
        if value not in (None,"unknown","unspecified"):
            command.extend([option,str(value)])
    return [*command,*common,"-y",str(partial)]


def _render(command: list[str], *, timing_fallback: bool = False) -> list[str]:
    result = run_no_window(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    warnings = [line for line in result.stderr.splitlines() if _TIMING.search(line)]
    if result.returncode and not (timing_fallback and warnings):
        raise ConcatError("concat_failed", "FFmpeg concat failed: " + result.stderr.strip()[-3000:])
    return warnings


def concat_videos(first: str | Path, second: str | Path, output: str | Path, *,
                  on_status: Callable[[str], None] | None = None) -> ConcatResult:
    sources = [Path(p).expanduser().resolve() for p in (first, second)]
    for source in sources:
        if not source.is_file():
            raise FileNotFoundError(f"Video not found: {source}")
    requested = Path(output).expanduser().absolute()
    if os.path.lexists(requested):
        raise FileExistsError(f"Output already exists: {requested}. Choose a different name.")
    target = ensure_output_is_safe(requested, sources)
    if target.suffix.lower() not in {".mp4", ".mov", ".mkv"}:
        raise ConcatError("unsupported_output", "Use an .mp4, .mov or .mkv output file.", 2)
    ffmpeg, ffprobe = ensure_ffmpeg()
    streams = [_probe(ffprobe, p) for p in sources]
    plan = _plan(*streams)
    durations, fps = plan.durations, plan.fps
    audio = next((s for s in streams[0] if s["codec_type"] == "audio"), None)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".recut-concat-", dir=target.parent) as folder:
        partial = Path(folder) / ("joined" + target.suffix.lower())
        listing = Path(folder) / "inputs.ffconcat"
        listing.write_text("ffconcat version 1.0\n" + "".join(
            f"file {quote_ffconcat_path(source)}\nduration {duration:.9f}\n"
            for source, duration in zip(sources, durations)), encoding="utf-8", newline="\n")
        base = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "warning", "-xerror", "-copyts",
                "-f", "concat", "-safe", "0", "-i", str(listing)]
        common = ["-map_metadata", "-1", "-map_chapters", "-1"]
        if target.suffix.lower() in {".mp4", ".mov"}:
            common += ["-movflags", "+faststart", "-write_tmcd", "0"]
        audio_reencoded = False
        if plan.video_reencoded:
            if on_status:
                on_status("Normalizing incompatible inputs: re-encoding video (H.264 CRF 18) and audio. This can take time. Differences: " + ", ".join(plan.reasons))
            warnings = _render(_normalized_command(ffmpeg,sources,streams,plan,partial,common))
            if warnings:
                raise ConcatError("unsafe_timestamps", "Video normalization produced unsafe timestamps.")
            audio_reencoded = audio is not None
        else:
            warnings = [] if plan.audio_reencoded else _render([*base, "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
                                *common, "-y", str(partial)], timing_fallback=True)
            audio_reencoded = False
            if warnings or plan.audio_reencoded:
                # Separate AAC encoders often leave priming packets whose DTS overlap
                # at a splice. Never silently accept FFmpeg's timestamp correction.
                if audio is None or (warnings and (audio["codec_name"] != "aac" or any(
                        not re.search(r"aost|audio|0:1", line, re.I) for line in warnings))):
                    raise ConcatError("unsafe_timestamps", "FFmpeg reported unsafe timestamps: " + "\n".join(warnings))
                if on_status:
                    on_status("Using audio normalization; video is copied without re-encoding.")
                filters = ";".join(
                    f"[{i}:a:0]asetpts=PTS-STARTPTS,apad,atrim=duration={duration:.9f}[a{i}]"
                    for i, duration in enumerate(durations, 1))
                filters += ";[a1][a2]concat=n=2:v=0:a=1[aout]"
                # Same FFmpeg concat/asetpts strategy as the existing filter export.
                # Decode inputs independently so each AAC priming/skip_samples is
                # honored. Only audio is normalized; all video packets stay copied.
                warnings = _render([*base, "-i", str(sources[0]), "-i", str(sources[1]),
                    "-filter_complex", filters, "-map", "0:v:0", "-map", "[aout]",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                    "-ar", str(audio["sample_rate"]), "-ac", str(audio["channels"]), *common, "-y", str(partial)])
                if warnings:
                    raise ConcatError("unsafe_timestamps", "Audio normalization still produced unsafe timestamps.")
                audio_reencoded = True
        try:
            checked = _probe(ffprobe, partial)
        except ConcatError as exc:
            raise ConcatError("validation_failed", str(exc)) from exc
        video = _video(checked)
        expected = sum(durations)
        tolerance = max(.05, 1/fps)
        if (video["codec_name"] != ("h264" if plan.video_reencoded else _video(streams[0])["codec_name"])
                or abs(_duration(video)-expected) > tolerance
                or not _starts_at_zero(video)
                or sum(s["codec_type"] == "audio" for s in checked) != int(audio is not None)):
            raise ConcatError("validation_failed", "Concatenated video failed stream/duration validation.")
        for stream in checked:
            if stream["codec_type"] == "audio" and (
                    audio is None
                    or stream.get("channels") != audio["channels"]
                    or stream.get("sample_rate") != audio["sample_rate"]
                    or abs(_duration(stream)-expected) > tolerance
                    or not _starts_at_zero(stream)):
                raise ConcatError("validation_failed", "Concatenated audio failed timing validation.")
        # Publish only after validation, in the destination filesystem, refusing
        # both pre-existing outputs and files created concurrently.
        atomic_promote_output(partial, target, overwrite=False)
    return ConcatResult(target, expected, audio_reencoded, plan.video_reencoded)
