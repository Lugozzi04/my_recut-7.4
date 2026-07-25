from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from analysis.cut_engine import Segment
from export.exporter import ExportWorker
from utils.ffmpeg import ensure_ffmpeg, ffprobe_duration_seconds
from utils.subprocess_utils import run_no_window


class ExportIntegrationTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
