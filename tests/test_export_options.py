from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from analysis.cut_engine import Segment
from export.exporter import ExportWorker
from export.settings import ExportSettings
from ui.main_window import MainWindow


class ExportOptionsTests(unittest.TestCase):
    def _worker(self, settings: ExportSettings, output: str = "output.mp4") -> ExportWorker:
        return ExportWorker(
            ffmpeg_path="ffmpeg",
            input_path="source.mp4",
            output_path=output,
            keeps=[Segment(0.0, 2.0)],
            codec=settings.codec,
            export_method=settings.method,
            export_settings=settings,
        )

    def test_web_profile_adds_real_scale_color_quality_and_audio_args(self) -> None:
        settings = replace(
            ExportSettings.defaults().with_preset("web_hq"),
            codec="libx264",
            method="filter_concat",
        ).normalized()
        with tempfile.TemporaryDirectory() as tmp:
            output = str(Path(tmp) / "web.mp4")
            worker = self._worker(settings, output)
            temp_files: list[str] = []
            command, _duration = worker._build_cmd_filter_concat_to(
                worker.keeps,
                output,
                temp_files,
            )
            filter_path = command[command.index("-filter_complex_script") + 1]
            filter_text = Path(filter_path).read_text(encoding="utf-8")

        self.assertIn("scale=", filter_text)
        self.assertIn("setparams=color_primaries=bt709", filter_text)
        self.assertEqual(command[command.index("-crf") + 1], "18")
        self.assertEqual(command[command.index("-b:a") + 1], "256k")
        self.assertIn("-ar", command)
        self.assertIn("-ac", command)

    def test_mkv_opus_uses_selected_mux_and_audio_encoder(self) -> None:
        settings = replace(
            ExportSettings.defaults(),
            preset="custom",
            codec="libx264",
            method="filter_concat",
            container="mkv",
            audio_codec="opus",
            audio_bitrate_kbps=192,
        ).normalized()
        with tempfile.TemporaryDirectory() as tmp:
            output = str(Path(tmp) / "delivery.mkv")
            worker = self._worker(settings, output)
            command, _duration = worker._build_cmd_filter_concat_to(
                worker.keeps,
                output,
                [],
            )

        self.assertIn("libopus", command)
        self.assertNotIn("-movflags", command)
        self.assertEqual(command[-1], output)

    def test_audio_only_sinks_video_and_maps_only_audio(self) -> None:
        settings = replace(
            ExportSettings.defaults(),
            preset="custom",
            codec="libx264",
            method="filter_concat",
            output_mode="audio_only",
            audio_codec="aac",
        ).normalized()
        with tempfile.TemporaryDirectory() as tmp:
            output = str(Path(tmp) / "audio.m4a")
            worker = self._worker(settings, output)
            command, _duration = worker._build_cmd_filter_concat_to(
                worker.keeps,
                output,
                [],
            )
            filter_path = command[command.index("-filter_complex_script") + 1]
            filter_text = Path(filter_path).read_text(encoding="utf-8")

        self.assertIn("nullsink", filter_text)
        self.assertIn("-vn", command)
        self.assertNotIn("-c:v", command)
        self.assertIn("-c:a", command)

    def test_target_size_calculates_video_budget_after_audio(self) -> None:
        settings = replace(
            ExportSettings.defaults(),
            preset="custom",
            codec="libx264",
            rate_control="target_size",
            target_size_mb=100,
            audio_bitrate_kbps=320,
        ).normalized()
        worker = self._worker(settings)
        worker._summary_keeps_total = 100.0

        self.assertEqual(worker._target_video_bitrate_kbps(), 7680)

    def test_export_configuration_log_contains_all_settings_and_effective_flags(self) -> None:
        requested = replace(
            ExportSettings.defaults(),
            codec="auto",
            fps="30",
            audio_codec="aac",
        ).normalized()
        effective = replace(requested, codec="libx264").normalized()
        worker = ExportWorker(
            ffmpeg_path="ffmpeg",
            input_path="source.mp4",
            output_path="output.mp4",
            keeps=[Segment(0.0, 2.0)],
            export_settings=effective,
            requested_export_settings=requested,
        )
        worker._ffmpeg_threads_override = 4
        messages: list[str] = []
        worker.detail.connect(messages.append)

        worker._log_effective_export_configuration(60.0)

        requested_line = next(line for line in messages if line.startswith("export_config requested="))
        effective_line = next(line for line in messages if line.startswith("export_config effective="))
        requested_payload = json.loads(requested_line.split("=", 1)[1])
        effective_payload = json.loads(effective_line.split("=", 1)[1])
        self.assertEqual(set(requested_payload), set(ExportSettings.__dataclass_fields__))
        self.assertEqual(set(effective_payload), set(ExportSettings.__dataclass_fields__))
        self.assertEqual(requested_payload["codec"], "auto")
        self.assertEqual(effective_payload["codec"], "libx264")
        self.assertTrue(any("export_flags video=" in line and "-crf 16" in line for line in messages))
        self.assertTrue(any("export_flags video_cut_hq=" in line and "-crf 14" in line for line in messages))
        self.assertTrue(any("export_flags audio=" in line and "-b:a 320k" in line for line in messages))
        self.assertTrue(any("export_flags timing=-fps_mode cfr -r 30.000" in line for line in messages))
        self.assertEqual(messages[0], "export_config_begin")
        self.assertEqual(messages[-1], "export_config_end")

    def test_two_pass_runs_analysis_before_progress_pass(self) -> None:
        settings = replace(
            ExportSettings.defaults(),
            preset="custom",
            codec="libx264",
            method="filter_concat",
            rate_control="bitrate",
            two_pass=True,
        ).normalized()
        worker = self._worker(settings)
        first_calls: list[list[str]] = []
        second_calls: list[list[str]] = []
        worker._run_ffmpeg_with_hw_fallback = (  # type: ignore[method-assign]
            lambda command, context: (first_calls.append(list(command)) or (0, "", ""))
        )
        worker._run_ffmpeg_progress = (  # type: ignore[method-assign]
            lambda command, on_progress: (second_calls.append(list(command)) or (0, ""))
        )
        command = [
            "ffmpeg", "-i", "source.mp4", "-c:v", "libx264",
            "-pass", "2", "-passlogfile", "test-passlog", "output.mp4",
        ]

        result = worker._run_ffmpeg_progress_with_hw_fallback(
            command,
            on_progress=lambda _seconds: None,
            context="two_pass_test",
        )

        self.assertEqual(result, (0, ""))
        self.assertEqual(first_calls[0][first_calls[0].index("-pass") + 1], "1")
        self.assertEqual(first_calls[0][-3:], ["-f", "null", str(Path(first_calls[0][-1]))])
        self.assertEqual(second_calls[0][second_calls[0].index("-pass") + 1], "2")

    def test_timeline_range_clips_segments_and_source_offsets(self) -> None:
        segments = [
            {
                "start": 0.0,
                "end": 10.0,
                "duration": 10.0,
                "v_idx": 0,
                "v_in": 20.0,
                "v_out": 30.0,
                "a_idx": 0,
                "a_in": 20.0,
                "a_out": 30.0,
            },
            {
                "start": 10.0,
                "end": 20.0,
                "duration": 10.0,
                "v_idx": 0,
                "v_in": 40.0,
                "v_out": 50.0,
                "a_idx": 0,
                "a_in": 40.0,
                "a_out": 50.0,
            },
        ]

        clipped = MainWindow._clip_flat_segments_to_range(segments, 5.0, 13.0)

        self.assertEqual(len(clipped), 2)
        self.assertEqual((clipped[0]["v_in"], clipped[0]["v_out"]), (25.0, 30.0))
        self.assertEqual((clipped[1]["v_in"], clipped[1]["v_out"]), (40.0, 43.0))
        self.assertEqual((clipped[0]["start"], clipped[1]["end"]), (0.0, 8.0))

    def test_timeline_range_clips_legacy_keeps_in_output_time(self) -> None:
        clipped = MainWindow._clip_keeps_to_output_range(
            [Segment(10.0, 15.0), Segment(30.0, 40.0)],
            3.0,
            8.0,
        )

        self.assertEqual(clipped, [Segment(13.0, 15.0), Segment(30.0, 33.0)])


if __name__ == "__main__":
    unittest.main()
