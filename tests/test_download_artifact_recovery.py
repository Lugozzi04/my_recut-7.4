from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from automation.downloader import FfmpegRangeDownloader


class DownloadArtifactRecoveryTests(unittest.TestCase):
    def test_truncated_playable_download_is_not_reused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.mp4"
            source.write_bytes(b"x" * 2048)
            downloader = FfmpegRangeDownloader(duration_probe=lambda _: 10.0)
            with patch("automation.downloader.has_video_stream", return_value=True):
                self.assertIsNone(downloader._valid_existing_duration(source, 120.0))
                self.assertEqual(downloader._valid_existing_duration(source, 10.0), 10.0)

    def test_invalid_duration_or_missing_video_is_not_reused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.mp4"
            source.write_bytes(b"x" * 2048)
            for duration, video in ((float("nan"), True), (float("inf"), True), (10.0, False)):
                with self.subTest(duration=duration, video=video):
                    downloader = FfmpegRangeDownloader(duration_probe=lambda _: duration)
                    with patch("automation.downloader.has_video_stream", return_value=video):
                        self.assertIsNone(downloader._valid_existing_duration(source, 10.0))


if __name__ == "__main__":
    unittest.main()
