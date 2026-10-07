from __future__ import annotations

import unittest

from export.advisor import (
    HIGH_QUALITY_ENCODER_ARGS,
    EncoderBenchmark,
    recommend_export_settings,
)


class ExportAdvisorTests(unittest.TestCase):
    def test_recommends_fastest_measured_encoder_without_applying_controls(self) -> None:
        recommendation = recommend_export_settings(
            [
                EncoderBenchmark("libx264", elapsed_seconds=1.5, media_seconds=3.0),
                EncoderBenchmark("h264_amf", elapsed_seconds=0.5, media_seconds=3.0),
            ],
            total_seconds=1800.0,
            segment_count=120,
            input_count=3,
            cpu_count=32,
        )

        self.assertEqual(recommendation.codec, "h264_amf")
        self.assertEqual(recommendation.workers, 3)
        self.assertEqual(recommendation.chunks, 20)
        self.assertIn("not changed", recommendation.note)
        self.assertIn("smart multi-source", recommendation.method)

    def test_software_recommendation_uses_cpu_worker_budget(self) -> None:
        recommendation = recommend_export_settings(
            [EncoderBenchmark("libx264", elapsed_seconds=1.0, media_seconds=2.0)],
            total_seconds=60.0,
            segment_count=30,
            input_count=1,
            cpu_count=16,
        )

        self.assertEqual(recommendation.workers, 3)
        self.assertEqual(recommendation.chunks, 2)
        self.assertIn("smart hybrid", recommendation.method)

    def test_benchmark_profiles_keep_high_quality_settings(self) -> None:
        self.assertIn("16", HIGH_QUALITY_ENCODER_ARGS["libx264"])
        self.assertIn("quality", HIGH_QUALITY_ENCODER_ARGS["h264_amf"])
        self.assertIn("14", HIGH_QUALITY_ENCODER_ARGS["h264_amf"])

    def test_requires_at_least_one_successful_encoder(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "No encoder"):
            recommend_export_settings(
                [],
                total_seconds=10.0,
                segment_count=1,
                input_count=1,
            )


if __name__ == "__main__":
    unittest.main()
