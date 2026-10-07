from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from analysis.audio_service import (
    AnalysisCancellation,
    AnalysisCancelled,
    AnalysisRequest,
    AudioAnalysisError,
    analyze_audio,
)
from utils.ffmpeg import ensure_ffmpeg
from utils.subprocess_utils import run_no_window


class AudioServiceTests(unittest.TestCase):
    def test_service_import_does_not_load_qt(self) -> None:
        code = "import sys; import analysis.audio_service; assert 'PySide6' not in sys.modules"
        result = run_no_window(
            [sys.executable, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_pre_cancelled_request_stops_before_ffmpeg(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.mp4"
            source.write_bytes(b"not-media")
            cancellation = AnalysisCancellation()
            cancellation.cancel()

            with self.assertRaises(AnalysisCancelled):
                analyze_audio(AnalysisRequest(str(source)), cancellation=cancellation)

    def test_missing_source_has_structured_error(self) -> None:
        with self.assertRaises(AudioAnalysisError) as caught:
            analyze_audio(AnalysisRequest("missing-source.mp4"))

        self.assertEqual(caught.exception.code, "source_not_found")

    def test_real_mp4_analysis_finishes_and_reports_progress(self) -> None:
        ffmpeg, _ = ensure_ffmpeg()
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "analysis_source.mp4"
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
                    "testsrc2=size=160x90:rate=25:duration=3",
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
            progress: list[int] = []

            result = analyze_audio(AnalysisRequest(str(source)), on_progress=progress.append)

            self.assertAlmostEqual(result.duration, 3.0, delta=0.15)
            self.assertGreater(result.rms.size, 90)
            self.assertGreater(result.auto_threshold, 0.0)
            self.assertEqual(progress[0], 0)
            self.assertEqual(progress[-1], 100)
            self.assertEqual(progress, sorted(progress))


if __name__ == "__main__":
    unittest.main()
