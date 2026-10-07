from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from analysis.cut_engine import Segment
from export.exporter import ExportWorker
from export.settings import ExportSettings
from utils.ffmpeg import ensure_ffmpeg
from utils.subprocess_utils import run_no_window


class ExportDeliveryIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp_dir.name)
        cls.ffmpeg, cls.ffprobe = ensure_ffmpeg()
        cls.source = cls.root / "delivery_source.mp4"
        generated = run_no_window(
            [
                cls.ffmpeg,
                "-hide_banner",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=320x180:rate=20:duration=1.5",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000:duration=1.5",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-shortest",
                str(cls.source),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
        if generated.returncode != 0:
            raise RuntimeError(generated.stderr)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp_dir.cleanup()

    def _run_export(
        self,
        settings: ExportSettings,
        output: Path,
        keeps: list[Segment] | None = None,
    ) -> None:
        errors: list[str] = []
        worker = ExportWorker(
            ffmpeg_path=self.ffmpeg,
            input_path=str(self.source),
            output_path=str(output),
            keeps=keeps or [Segment(0.1, 1.2)],
            export_settings=settings.normalized(),
        )
        worker.debug = False
        worker.error.connect(errors.append)
        worker.run()
        self.assertEqual(errors, [])

    def _probe_streams(self, path: Path) -> list[dict]:
        result = run_no_window(
            [
                self.ffprobe,
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type,codec_name,width,height,pix_fmt,"
                "color_primaries,color_transfer,color_space",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return list(json.loads(result.stdout).get("streams") or [])

    def test_webm_av1_opus_export_is_playable(self) -> None:
        output = self.root / "delivery.webm"
        settings = replace(
            ExportSettings.defaults(),
            preset="custom",
            codec="libaom-av1",
            method="filter_concat",
            container="webm",
            audio_codec="opus",
            hwaccel_decode=False,
        ).normalized()

        self._run_export(settings, output)

        streams = self._probe_streams(output)
        self.assertEqual(settings.audio_bitrate_kbps, 256)
        self.assertTrue(any(row.get("codec_name") == "av1" for row in streams))
        self.assertTrue(any(row.get("codec_name") == "opus" for row in streams))

    def test_audio_only_and_video_only_have_the_requested_streams(self) -> None:
        base = replace(
            ExportSettings.defaults(),
            preset="custom",
            codec="libx264",
            method="filter_concat",
            hwaccel_decode=False,
        )
        audio_output = self.root / "audio_only.m4a"
        pcm_output = self.root / "audio_only.wav"
        video_output = self.root / "video_only.mp4"

        self._run_export(replace(base, output_mode="audio_only", audio_codec="aac"), audio_output)
        self._run_export(
            replace(base, output_mode="audio_only", audio_codec="pcm_s24le"),
            pcm_output,
        )
        self._run_export(replace(base, output_mode="video_only"), video_output)

        audio_streams = self._probe_streams(audio_output)
        pcm_streams = self._probe_streams(pcm_output)
        video_streams = self._probe_streams(video_output)
        self.assertEqual([row.get("codec_type") for row in audio_streams], ["audio"])
        self.assertEqual([row.get("codec_name") for row in pcm_streams], ["pcm_s24le"])
        self.assertEqual([row.get("codec_type") for row in video_streams], ["video"])

    def test_vertical_scale_and_no_upscale_create_requested_canvas(self) -> None:
        output = self.root / "vertical.mp4"
        settings = replace(
            ExportSettings.defaults(),
            preset="custom",
            codec="libx264",
            method="filter_concat",
            resolution="720p",
            aspect="vertical",
            no_upscale=True,
            hwaccel_decode=False,
        )

        self._run_export(settings, output)

        video = next(row for row in self._probe_streams(output) if row.get("codec_type") == "video")
        self.assertEqual((video.get("width"), video.get("height")), (720, 1280))

    def test_per_clip_mode_creates_one_playable_file_per_keep(self) -> None:
        output_dir = self.root / "clips"
        settings = replace(
            ExportSettings.defaults(),
            preset="custom",
            codec="libx264",
            method="filter_concat",
            output_mode="per_clip",
            hwaccel_decode=False,
        )

        self._run_export(
            settings,
            output_dir,
            keeps=[Segment(0.0, 0.5), Segment(0.8, 1.3)],
        )

        outputs = sorted(output_dir.glob("*.mp4"))
        self.assertEqual(len(outputs), 2)
        for path in outputs:
            self.assertGreater(path.stat().st_size, 1_000)
            self.assertTrue(any(row.get("codec_type") == "video" for row in self._probe_streams(path)))

    def test_hevc_ten_bit_rec2020_preserves_delivery_metadata(self) -> None:
        output = self.root / "ten_bit.mp4"
        settings = replace(
            ExportSettings.defaults(),
            preset="custom",
            codec="libx265",
            method="filter_concat",
            pixel_depth="10",
            color_mode="rec2020",
            hwaccel_decode=False,
        )

        self._run_export(settings, output)

        video = next(row for row in self._probe_streams(output) if row.get("codec_type") == "video")
        self.assertEqual(video.get("codec_name"), "hevc")
        self.assertEqual(video.get("pix_fmt"), "yuv420p10le")
        self.assertEqual(video.get("color_primaries"), "bt2020")
        self.assertEqual(video.get("color_space"), "bt2020nc")

    def test_x264_two_pass_bitrate_export_completes_and_cleans_logs(self) -> None:
        output = self.root / "two_pass.mp4"
        settings = replace(
            ExportSettings.defaults(),
            preset="custom",
            codec="libx264",
            method="filter_concat",
            rate_control="bitrate",
            video_bitrate_mbps=2.0,
            two_pass=True,
            hwaccel_decode=False,
        )

        self._run_export(settings, output)

        self.assertGreater(output.stat().st_size, 1_000)
        self.assertEqual(list(self.root.glob("two_pass.mp4.passlog*")), [])


if __name__ == "__main__":
    unittest.main()
