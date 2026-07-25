from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils.ffmpeg import _keyframe_cache_root, _splice_cache_root
from utils.runtime_paths import cache_root


class RuntimeCacheTests(unittest.TestCase):
    def test_ffmpeg_caches_use_configured_runtime_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            configured = Path(tmp) / "custom-cache"
            with patch.dict(
                os.environ,
                {"AUTO_CUTTER_CACHE_DIR": str(configured)},
                clear=False,
            ):
                self.assertEqual(cache_root(), configured.resolve())
                self.assertEqual(
                    _keyframe_cache_root(),
                    configured.resolve() / "keyframes",
                )
                self.assertEqual(
                    _splice_cache_root(),
                    configured.resolve() / "splice_points",
                )

            self.assertTrue((configured / "keyframes").is_dir())
            self.assertTrue((configured / "splice_points").is_dir())


if __name__ == "__main__":
    unittest.main()
