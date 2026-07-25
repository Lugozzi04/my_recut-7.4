from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils.ffmpeg import _find_exe


class FFmpegResolutionTests(unittest.TestCase):
    def test_explicit_environment_override_has_highest_priority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            override = root / "override.exe"
            bundled = root / "bundled" / "ffmpeg.exe"
            override.touch()
            bundled.parent.mkdir()
            bundled.touch()

            with (
                patch.dict(os.environ, {"FFMPEG_BIN": str(override)}, clear=False),
                patch("utils.ffmpeg._candidate_dirs", return_value=[bundled.parent]),
                patch("utils.ffmpeg.shutil.which", return_value="C:\\path\\ffmpeg.exe"),
            ):
                self.assertEqual(
                    _find_exe("FFMPEG_BIN", ["ffmpeg.exe"]),
                    str(override),
                )

    def test_bundled_binary_is_preferred_over_system_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundled = Path(tmp) / "bin" / "ffmpeg.exe"
            bundled.parent.mkdir()
            bundled.touch()

            with (
                patch.dict(os.environ, {}, clear=False),
                patch("utils.ffmpeg._candidate_dirs", return_value=[bundled.parent]),
                patch("utils.ffmpeg.shutil.which", return_value="C:\\path\\ffmpeg.exe"),
            ):
                os.environ.pop("FFMPEG_BIN", None)
                self.assertEqual(
                    _find_exe("FFMPEG_BIN", ["ffmpeg.exe"]),
                    str(bundled),
                )

    def test_system_path_is_used_when_bundle_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing_dir = Path(tmp) / "missing"
            system_binary = "C:\\path\\ffmpeg.exe"

            with (
                patch.dict(os.environ, {}, clear=False),
                patch("utils.ffmpeg._candidate_dirs", return_value=[missing_dir]),
                patch("utils.ffmpeg.shutil.which", return_value=system_binary),
            ):
                os.environ.pop("FFMPEG_BIN", None)
                self.assertEqual(
                    _find_exe("FFMPEG_BIN", ["ffmpeg.exe"]),
                    system_binary,
                )


if __name__ == "__main__":
    unittest.main()
