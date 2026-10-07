from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from analysis.cut_engine import Segment
from export.exporter import ExportWorker
from utils.ffmpeg import ensure_ffmpeg, ffprobe_duration_seconds
from utils.subprocess_utils import run_no_window


class ExportIntegrationTests(unittest.TestCase):
    def test_short_clip_is_accepted_as_a_valid_export(self) -> None:
        ffmpeg, _ffprobe = ensure_ffmpeg()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "short_source.mp4"
            output = root / "short_output.mp4"
            generated = run_no_window(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-v",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=160x90:rate=30:duration=0.4",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:sample_rate=48000:duration=0.4",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-shortest",
                    str(source),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)

            errors: list[str] = []
            worker = ExportWorker(
                ffmpeg_path=ffmpeg,
                input_path=str(source),
                output_path=str(output),
                keeps=[Segment(0.0, 0.3)],
                codec="libx264",
                export_method="filter_concat",
                parallel_workers=1,
            )
            worker.error.connect(errors.append)

            worker.run()

            self.assertEqual(errors, [])
            self.assertTrue(output.is_file())
            self.assertGreater(ffprobe_duration_seconds(str(output)), 0.02)

    def test_filter_concat_exports_real_audio_video(self) -> None:
        ffmpeg, _ffprobe = ensure_ffmpeg()
        os.environ["AUTO_CUTTER_EXPORT_DEBUG"] = "0"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.mp4"
            output = root / "output.mp4"
            generated = run_no_window(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-v",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=320x180:rate=25:duration=2",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:sample_rate=48000:duration=2",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-shortest",
                    str(source),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)

            errors: list[str] = []
            finished: list[bool] = []
            worker = ExportWorker(
                ffmpeg_path=ffmpeg,
                input_path=str(source),
                output_path=str(output),
                keeps=[Segment(0.25, 1.5)],
                codec="libx264",
                use_hwaccel=False,
                export_method="filter_concat",
                parallel_workers=1,
            )
            worker.error.connect(errors.append)
            worker.finished.connect(lambda: finished.append(True))

            worker.run()

            self.assertEqual(errors, [])
            self.assertEqual(finished, [True])
            self.assertTrue(output.is_file())
            self.assertGreater(output.stat().st_size, 1_000)
            self.assertAlmostEqual(ffprobe_duration_seconds(str(output)), 1.25, delta=0.2)

    def test_hardware_decode_filter_export_or_software_fallback(self) -> None:
        ffmpeg, _ffprobe = ensure_ffmpeg()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "hw_source.mp4"
            output = root / "hw_output.mp4"
            generated = run_no_window(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-v",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=160x90:rate=10:duration=2",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=550:sample_rate=48000:duration=2",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-shortest",
                    str(source),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)

            errors: list[str] = []
            details: list[str] = []
            worker = ExportWorker(
                ffmpeg_path=ffmpeg,
                input_path=str(source),
                output_path=str(output),
                keeps=[Segment(0.2, 1.7)],
                codec="libx264",
                use_hwaccel=True,
                export_method="filter_concat",
                parallel_workers=1,
            )
            worker._log = details.append  # type: ignore[method-assign]
            worker.error.connect(errors.append)

            worker.run()

            self.assertEqual(errors, [])
            self.assertTrue(output.is_file())
            self.assertGreater(output.stat().st_size, 1_000)
            self.assertAlmostEqual(ffprobe_duration_seconds(str(output)), 1.5, delta=0.2)
            if os.name == "nt":
                self.assertTrue(
                    any("hwaccel_filtergraph_enabled" in row for row in details),
                    details,
                )

    def test_auto_multitrack_export_uses_chunked_renderer(self) -> None:
        ffmpeg, _ffprobe = ensure_ffmpeg()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sources = [root / f"source_{idx}.mp4" for idx in range(3)]
            output = root / "multitrack_output.mp4"

            for idx, source in enumerate(sources):
                generated = run_no_window(
                    [
                        ffmpeg,
                        "-hide_banner",
                        "-v",
                        "error",
                        "-y",
                        "-f",
                        "lavfi",
                        "-i",
                        f"testsrc2=size=160x90:rate=10:duration=2,"
                        f"hue=h={idx * 45}",
                        "-f",
                        "lavfi",
                        "-i",
                        f"sine=frequency={440 + (idx * 110)}:sample_rate=48000:duration=2",
                        "-c:v",
                        "libx264",
                        "-preset",
                        "ultrafast",
                        "-pix_fmt",
                        "yuv420p",
                        "-c:a",
                        "aac",
                        "-shortest",
                        str(source),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(generated.returncode, 0, generated.stderr)

            segments: list[dict] = []
            for pos in range(30):
                source_idx = (pos + (pos // 15)) % len(sources)
                start = float(pos * 2)
                segments.append(
                    {
                        "start": start,
                        "end": start + 2.0,
                        "duration": 2.0,
                        "v_idx": source_idx,
                        "v_in": 0.0,
                        "v_out": 2.0,
                        "a_idx": source_idx,
                        "a_in": 0.0,
                        "a_out": 2.0,
                    }
                )

            errors: list[str] = []
            finished: list[bool] = []
            progress_messages: list[str] = []
            worker = ExportWorker(
                ffmpeg_path=ffmpeg,
                input_path=str(sources[0]),
                input_paths=[str(path) for path in sources],
                output_path=str(output),
                keeps=[],
                segments=segments,
                codec="libx264",
                use_hwaccel=False,
                export_method="auto",
                parallel_workers=2,
            )
            worker.error.connect(errors.append)
            worker.finished.connect(lambda: finished.append(True))
            worker.progress.connect(lambda _percent, message: progress_messages.append(message))

            started = time.monotonic()
            with patch.dict(
                os.environ,
                {
                    "AUTO_CUTTER_EXPORT_DEBUG": "0",
                    "AUTO_CUTTER_EXPORT_MAX_CONSERVATIVE": "1",
                    "AUTO_CUTTER_CHUNK_CACHE": "0",
                },
            ):
                worker.run()
            elapsed = time.monotonic() - started

            self.assertEqual(errors, [])
            self.assertEqual(finished, [True])
            self.assertEqual(worker._export_method_used, "chunked_parallel_segments")
            self.assertTrue(any("Chunk" in message for message in progress_messages))
            self.assertTrue(output.is_file())
            self.assertGreater(output.stat().st_size, 10_000)
            self.assertAlmostEqual(ffprobe_duration_seconds(str(output)), 60.0, delta=1.0)
            self.assertLess(elapsed, 45.0)

    def test_auto_reordered_single_track_uses_scalable_renderer(self) -> None:
        ffmpeg, _ffprobe = ensure_ffmpeg()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.mp4"
            output = root / "reordered_output.mp4"
            generated = run_no_window(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-v",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=160x90:rate=10:duration=3",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:sample_rate=48000:duration=3",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-shortest",
                    str(source),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)

            segments = [
                {
                    "start": 0.0,
                    "end": 1.5,
                    "duration": 1.5,
                    "v_idx": 0,
                    "v_in": 1.5,
                    "v_out": 3.0,
                    "a_idx": 0,
                    "a_in": 1.5,
                    "a_out": 3.0,
                },
                {
                    "start": 1.5,
                    "end": 3.0,
                    "duration": 1.5,
                    "v_idx": 0,
                    "v_in": 0.0,
                    "v_out": 1.5,
                    "a_idx": 0,
                    "a_in": 0.0,
                    "a_out": 1.5,
                },
            ]
            errors: list[str] = []
            finished: list[bool] = []
            worker = ExportWorker(
                ffmpeg_path=ffmpeg,
                input_path=str(source),
                output_path=str(output),
                keeps=[],
                segments=segments,
                codec="libx264",
                use_hwaccel=False,
                export_method="auto",
                parallel_workers=2,
            )
            worker.error.connect(errors.append)
            worker.finished.connect(lambda: finished.append(True))

            with patch.dict(
                os.environ,
                {
                    "AUTO_CUTTER_EXPORT_DEBUG": "0",
                    "AUTO_CUTTER_EXPORT_MAX_CONSERVATIVE": "1",
                    "AUTO_CUTTER_CHUNK_CACHE": "0",
                },
            ):
                worker.run()

            self.assertEqual(errors, [])
            self.assertEqual(finished, [True])
            self.assertEqual(worker._export_method_used, "chunked_parallel_segments")
            self.assertTrue(output.is_file())
            self.assertAlmostEqual(ffprobe_duration_seconds(str(output)), 3.0, delta=0.5)

    def test_auto_multitrack_smart_hybrid_copies_compatible_source_runs(self) -> None:
        ffmpeg, _ffprobe = ensure_ffmpeg()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sources = [root / f"smart_source_{idx}.mp4" for idx in range(3)]
            output = root / "smart_multitrack_output.mp4"
            for idx, source in enumerate(sources):
                generated = run_no_window(
                    [
                        ffmpeg,
                        "-hide_banner",
                        "-v",
                        "error",
                        "-y",
                        "-f",
                        "lavfi",
                        "-i",
                        f"testsrc2=size=160x90:rate=10:duration=12,hue=h={idx * 45}",
                        "-f",
                        "lavfi",
                        "-i",
                        f"sine=frequency={440 + (idx * 110)}:sample_rate=48000:duration=12",
                        "-c:v",
                        "libx264",
                        "-preset",
                        "ultrafast",
                        "-g",
                        "20",
                        "-keyint_min",
                        "20",
                        "-sc_threshold",
                        "0",
                        "-pix_fmt",
                        "yuv420p",
                        "-c:a",
                        "aac",
                        "-shortest",
                        str(source),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(generated.returncode, 0, generated.stderr)

            segments = []
            for idx in range(3):
                start = float(idx * 12)
                segments.append(
                    {
                        "start": start,
                        "end": start + 12.0,
                        "duration": 12.0,
                        "v_idx": idx,
                        "v_in": 0.0,
                        "v_out": 12.0,
                        "a_idx": idx,
                        "a_in": 0.0,
                        "a_out": 12.0,
                    }
                )

            errors: list[str] = []
            finished: list[bool] = []
            details: list[str] = []
            worker = ExportWorker(
                ffmpeg_path=ffmpeg,
                input_path=str(sources[0]),
                input_paths=[str(path) for path in sources],
                output_path=str(output),
                keeps=[],
                segments=segments,
                codec="libx264",
                use_hwaccel=False,
                export_method="auto",
                parallel_workers=3,
            )
            worker.error.connect(errors.append)
            worker.finished.connect(lambda: finished.append(True))
            worker._log = details.append

            with patch.dict(
                os.environ,
                {
                    "AUTO_CUTTER_EXPORT_DEBUG": "0",
                    "AUTO_CUTTER_EXPORT_MAX_CONSERVATIVE": "1",
                    "AUTO_CUTTER_CHUNK_CACHE": "0",
                },
            ):
                worker.run()

            self.assertEqual(errors, [])
            self.assertEqual(finished, [True])
            self.assertEqual(worker._export_method_used, "chunked_parallel_segments")
            self.assertTrue(any("multi_source_smart_hybrid apply=yes" in line for line in details))
            self.assertTrue(
                any("copy_pct=" in line and "smart_hybrid_stats" in line for line in details),
                "\n".join(details),
            )
            self.assertTrue(output.is_file())
            self.assertAlmostEqual(ffprobe_duration_seconds(str(output)), 36.0, delta=1.0)


if __name__ == "__main__":
    unittest.main()
