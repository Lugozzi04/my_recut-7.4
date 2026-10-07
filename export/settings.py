from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any, Mapping


VIDEO_QUALITY_CRF = {
    "maximum": 10,
    "very_high": 16,
    "high": 18,
    "balanced": 20,
    "compact": 24,
}

RESOLUTION_HEIGHTS = {
    "source": 0,
    "2160p": 2160,
    "1440p": 1440,
    "1080p": 1080,
    "720p": 720,
}

EXPORT_PRESETS: dict[str, dict[str, Any]] = {
    "original_hq": {
        "container": "mp4",
        "resolution": "source",
        "aspect": "source",
        "no_upscale": True,
        "fps": "source",
        "fps_mode": "cfr",
        "quality": "very_high",
        "rate_control": "quality",
        "custom_quality": 16,
        "audio_codec": "auto",
        "audio_bitrate_kbps": 320,
        "sample_rate": "source",
        "channels": "source",
        "pixel_depth": "source",
        "color_mode": "preserve",
        "two_pass": False,
    },
    "web_hq": {
        "container": "mp4",
        "resolution": "1080p",
        "aspect": "source",
        "no_upscale": True,
        "fps": "source",
        "fps_mode": "cfr",
        "quality": "high",
        "rate_control": "quality",
        "custom_quality": 18,
        "audio_codec": "aac",
        "audio_bitrate_kbps": 256,
        "sample_rate": "48000",
        "channels": "stereo",
        "pixel_depth": "8",
        "color_mode": "rec709",
        "two_pass": False,
    },
    "compact": {
        "container": "mp4",
        "resolution": "1080p",
        "aspect": "source",
        "no_upscale": True,
        "fps": "30",
        "fps_mode": "cfr",
        "quality": "compact",
        "rate_control": "quality",
        "custom_quality": 24,
        "audio_codec": "aac",
        "audio_bitrate_kbps": 192,
        "sample_rate": "48000",
        "channels": "stereo",
        "pixel_depth": "8",
        "color_mode": "rec709",
        "two_pass": False,
    },
    "master": {
        "codec": "libx264",
        "container": "mov",
        "resolution": "source",
        "aspect": "source",
        "no_upscale": True,
        "fps": "source",
        "fps_mode": "cfr",
        "quality": "maximum",
        "rate_control": "quality",
        "custom_quality": 10,
        "audio_codec": "pcm_s24le",
        "audio_bitrate_kbps": 320,
        "sample_rate": "48000",
        "channels": "source",
        "pixel_depth": "source",
        "color_mode": "preserve",
        "two_pass": False,
    },
}


@dataclass(frozen=True)
class ExportSettings:
    preset: str = "original_hq"
    codec: str = "auto"
    method: str = "auto"
    container: str = "mp4"
    output_mode: str = "single"
    resolution: str = "source"
    aspect: str = "source"
    no_upscale: bool = True
    fps: str = "source"
    fps_mode: str = "cfr"
    quality: str = "very_high"
    rate_control: str = "quality"
    video_bitrate_mbps: float = 18.0
    target_size_mb: int = 1500
    custom_quality: int = 16
    two_pass: bool = False
    audio_codec: str = "auto"
    audio_bitrate_kbps: int = 320
    sample_rate: str = "source"
    channels: str = "source"
    pixel_depth: str = "source"
    color_mode: str = "preserve"
    cut_quality: str = "balanced"
    parallel_workers: int = 0
    chunk_count: int = 0
    hwaccel_decode: bool = True
    range_start: float = 0.0
    range_end: float = 0.0

    @classmethod
    def defaults(cls) -> ExportSettings:
        return cls()

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> ExportSettings:
        if not raw:
            return cls.defaults()
        fields = cls.__dataclass_fields__
        values = {key: raw[key] for key in fields if key in raw}
        try:
            settings = cls(**values)
        except (TypeError, ValueError):
            settings = cls.defaults()
        return settings.normalized()

    def to_mapping(self) -> dict[str, Any]:
        return asdict(self.normalized())

    def with_preset(self, preset: str) -> ExportSettings:
        key = str(preset or "original_hq").strip().lower()
        if key == "custom":
            return replace(self, preset="custom").normalized()
        profile = EXPORT_PRESETS.get(key, EXPORT_PRESETS["original_hq"])
        return replace(self, preset=key, **profile).normalized()

    def normalized(self) -> ExportSettings:
        preset = self.preset if self.preset in {*EXPORT_PRESETS, "custom"} else "custom"
        codec = str(self.codec or "auto").lower()
        method = str(self.method or "auto").lower()
        container = self.container if self.container in {"mp4", "mkv", "mov", "webm"} else "mp4"
        output_mode = self.output_mode if self.output_mode in {
            "single", "per_clip", "audio_only", "video_only", "selected_range"
        } else "single"
        resolution = self.resolution if self.resolution in RESOLUTION_HEIGHTS else "source"
        aspect = self.aspect if self.aspect in {"source", "landscape", "vertical", "square"} else "source"
        if aspect != "source" and resolution == "source":
            resolution = "1080p"
        fps = str(self.fps or "source").lower()
        if fps not in {"source", "24", "25", "30", "50", "60"}:
            fps = "source"
        fps_mode = self.fps_mode if self.fps_mode in {"cfr", "vfr"} else "cfr"
        if fps != "source":
            fps_mode = "cfr"
        quality = self.quality if self.quality in VIDEO_QUALITY_CRF or self.quality == "custom" else "very_high"
        rate_control = self.rate_control if self.rate_control in {"quality", "bitrate", "target_size"} else "quality"
        audio_codec = self.audio_codec if self.audio_codec in {"auto", "copy", "aac", "opus", "pcm_s24le"} else "auto"
        sample_rate = str(self.sample_rate or "source")
        if sample_rate not in {"source", "44100", "48000"}:
            sample_rate = "source"
        channels = self.channels if self.channels in {"source", "mono", "stereo"} else "source"
        pixel_depth = str(self.pixel_depth or "source")
        if pixel_depth not in {"source", "8", "10"}:
            pixel_depth = "source"
        color_mode = self.color_mode if self.color_mode in {
            "preserve", "rec709", "rec2020", "hdr_preserve"
        } else "preserve"
        cut_quality = self.cut_quality if self.cut_quality in {
            "balanced", "higher", "faster", "maximum_speed"
        } else "balanced"

        if container == "webm":
            audio_codec = "opus" if audio_codec in {"auto", "copy", "aac", "pcm_s24le"} else audio_codec
        elif (
            container == "mp4"
            and output_mode != "audio_only"
            and audio_codec in {"opus", "pcm_s24le"}
        ):
            audio_codec = "aac"
        if output_mode == "audio_only" and audio_codec == "copy":
            audio_codec = "auto"
        audio_bitrate_kbps = max(32, min(1536, int(self.audio_bitrate_kbps or 320)))
        if audio_codec == "opus":
            audio_bitrate_kbps = min(256, audio_bitrate_kbps)

        is_h264 = codec in {"auto", "h264_nvenc", "h264_qsv", "h264_amf", "libx264"}
        if is_h264 and pixel_depth == "10":
            pixel_depth = "8"
        if color_mode == "hdr_preserve" and pixel_depth == "8":
            pixel_depth = "source"

        two_pass = bool(self.two_pass and codec == "libx264" and rate_control in {"bitrate", "target_size"})
        start = max(0.0, float(self.range_start or 0.0))
        end = max(0.0, float(self.range_end or 0.0))
        if end > 0.0 and end < start:
            start, end = end, start

        return replace(
            self,
            preset=preset,
            codec=codec,
            method=method,
            container=container,
            output_mode=output_mode,
            resolution=resolution,
            aspect=aspect,
            no_upscale=bool(self.no_upscale),
            fps=fps,
            fps_mode=fps_mode,
            quality=quality,
            rate_control=rate_control,
            video_bitrate_mbps=max(0.1, min(500.0, float(self.video_bitrate_mbps or 18.0))),
            target_size_mb=max(1, min(1_000_000, int(self.target_size_mb or 1500))),
            custom_quality=max(0, min(51, int(self.custom_quality or 0))),
            two_pass=two_pass,
            audio_codec=audio_codec,
            audio_bitrate_kbps=audio_bitrate_kbps,
            sample_rate=sample_rate,
            channels=channels,
            pixel_depth=pixel_depth,
            color_mode=color_mode,
            cut_quality=cut_quality,
            parallel_workers=max(0, min(32, int(self.parallel_workers or 0))),
            chunk_count=max(0, min(200, int(self.chunk_count or 0))),
            hwaccel_decode=bool(self.hwaccel_decode),
            range_start=start,
            range_end=end,
        )

    def quality_value(self) -> int:
        if self.quality == "custom":
            return int(self.custom_quality)
        return int(VIDEO_QUALITY_CRF.get(self.quality, 16))

    def requested_fps(self) -> float:
        if self.fps == "source":
            return 0.0
        return float(self.fps)

    def output_extension(self) -> str:
        settings = self.normalized()
        if settings.output_mode == "audio_only":
            codec = settings.audio_codec
            if codec == "pcm_s24le":
                return ".wav"
            if codec == "opus":
                return ".opus"
            return ".m4a"
        return ".mkv" if settings.container == "mkv" else f".{settings.container}"

    def requires_full_video_reencode(self) -> bool:
        return bool(
            self.container != "mp4"
            or self.output_mode != "single"
            or self.resolution != "source"
            or self.aspect != "source"
            or self.fps != "source"
            or self.fps_mode != "cfr"
            or self.quality != "very_high"
            or self.rate_control != "quality"
            or self.custom_quality != 16
            or self.pixel_depth != "source"
            or self.color_mode != "preserve"
            or self.two_pass
        )

    def requires_accurate_pipeline(self) -> bool:
        return bool(
            self.requires_full_video_reencode()
            or self.audio_codec not in {"auto", "copy"}
            or self.audio_bitrate_kbps != 320
            or self.sample_rate != "source"
            or self.channels != "source"
        )

    def compatibility_messages(self) -> tuple[str, ...]:
        messages: list[str] = []
        if self.requires_accurate_pipeline():
            messages.append("Accurate full render required; Smart Render will be disabled.")
        else:
            messages.append("Smart Render remains available when source codecs match.")
        if self.container == "webm" and not self.codec.startswith("av1"):
            messages.append("WebM requires an AV1 video encoder in this application.")
        if self.color_mode == "hdr_preserve" and self.codec.startswith("h264"):
            messages.append("HDR preservation requires HEVC or AV1; H.264 is not recommended.")
        if self.output_mode == "selected_range" and self.range_end <= self.range_start:
            messages.append("Set a valid timeline range before exporting.")
        if self.rate_control == "target_size":
            messages.append("Target size is approximate and includes the selected audio bitrate.")
        return tuple(messages)
