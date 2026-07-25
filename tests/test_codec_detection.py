from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from utils.codec_detection import resolve_video_codec


class CodecDetectionTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.NamedTemporaryFile(delete=False)
        temp.close()
        self.ffmpeg = temp.name
        self.addCleanup(Path(self.ffmpeg).unlink, missing_ok=True)

    @staticmethod
    def _runner_with_working(*working: str):
        available = set(working)

        def run(cmd, **_kwargs):
            codec = cmd[cmd.index("-c:v") + 1]
            return subprocess.CompletedProcess(
                cmd,
                0 if codec in available else 1,
                stderr="" if codec in available else f"{codec} unavailable",
            )

        return run

    def test_auto_prefers_first_working_hardware_encoder(self) -> None:
        result = resolve_video_codec(
            self.ffmpeg,
            "auto",
            runner=self._runner_with_working("h264_qsv", "libx264"),
            use_cache=False,
        )

        self.assertEqual(result.resolved, "h264_qsv")
        self.assertFalse(result.used_fallback)

    def test_auto_falls_back_to_software(self) -> None:
        result = resolve_video_codec(
            self.ffmpeg,
            "auto",
            runner=self._runner_with_working("libx264"),
            use_cache=False,
        )

        self.assertEqual(result.resolved, "libx264")
        self.assertFalse(result.used_fallback)

    def test_explicit_unavailable_hardware_falls_back_with_reason(self) -> None:
        result = resolve_video_codec(
            self.ffmpeg,
            "h264_amf",
            runner=self._runner_with_working("libx264"),
            use_cache=False,
        )

        self.assertEqual(result.resolved, "libx264")
        self.assertTrue(result.used_fallback)
        self.assertIn("h264_amf unavailable", result.fallback_reason)


if __name__ == "__main__":
    unittest.main()
