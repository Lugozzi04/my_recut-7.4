from __future__ import annotations

import unittest

from export.settings import ExportSettings


class ExportSettingsTests(unittest.TestCase):
    def test_defaults_preserve_original_and_allow_smart_render(self) -> None:
        settings = ExportSettings.defaults()

        self.assertEqual(settings.preset, "original_hq")
        self.assertEqual(settings.container, "mp4")
        self.assertEqual(settings.resolution, "source")
        self.assertEqual(settings.quality_value(), 16)
        self.assertFalse(settings.requires_full_video_reencode())

    def test_web_profile_is_high_quality_and_does_not_upscale(self) -> None:
        settings = ExportSettings.defaults().with_preset("web_hq")

        self.assertEqual(settings.resolution, "1080p")
        self.assertTrue(settings.no_upscale)
        self.assertEqual(settings.quality, "high")
        self.assertEqual(settings.audio_bitrate_kbps, 256)
        self.assertTrue(settings.requires_full_video_reencode())

    def test_normalization_repairs_incompatible_container_audio_and_ten_bit_h264(self) -> None:
        webm = ExportSettings(container="webm", audio_codec="aac").normalized()
        h264 = ExportSettings(codec="libx264", pixel_depth="10").normalized()

        self.assertEqual(webm.audio_codec, "opus")
        self.assertEqual(webm.audio_bitrate_kbps, 256)
        self.assertEqual(h264.pixel_depth, "8")

    def test_two_pass_only_survives_for_x264_bitrate_modes(self) -> None:
        valid = ExportSettings(codec="libx264", rate_control="bitrate", two_pass=True).normalized()
        invalid = ExportSettings(codec="h264_amf", rate_control="bitrate", two_pass=True).normalized()

        self.assertTrue(valid.two_pass)
        self.assertFalse(invalid.two_pass)

    def test_audio_only_extension_follows_audio_codec(self) -> None:
        settings = ExportSettings(output_mode="audio_only", audio_codec="pcm_s24le").normalized()

        self.assertEqual(settings.audio_codec, "pcm_s24le")
        self.assertEqual(settings.output_extension(), ".wav")

    def test_mapping_round_trip_clamps_values(self) -> None:
        settings = ExportSettings.from_mapping(
            {
                "preset": "custom",
                "video_bitrate_mbps": 9999,
                "custom_quality": -4,
                "parallel_workers": 500,
                "range_start": 20,
                "range_end": 10,
            }
        )

        self.assertEqual(settings.video_bitrate_mbps, 500.0)
        self.assertEqual(settings.custom_quality, 0)
        self.assertEqual(settings.parallel_workers, 32)
        self.assertEqual((settings.range_start, settings.range_end), (10.0, 20.0))


if __name__ == "__main__":
    unittest.main()
