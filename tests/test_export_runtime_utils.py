from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from export.runtime_utils import env_value, parse_out_time_hms, safe_int


class ExportRuntimeUtilsTests(unittest.TestCase):
    def test_current_environment_name_wins_over_legacy_name(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AUTO_CUTTER_SAMPLE": "current",
                "RECUT_SAMPLE": "legacy",
            },
            clear=False,
        ):
            self.assertEqual(env_value("AUTO_CUTTER_SAMPLE", "default"), "current")

    def test_legacy_environment_name_is_supported(self) -> None:
        with patch.dict(os.environ, {"RECUT_SAMPLE": "legacy"}, clear=False):
            os.environ.pop("AUTO_CUTTER_SAMPLE", None)
            self.assertEqual(env_value("AUTO_CUTTER_SAMPLE", "default"), "legacy")

    def test_safe_int_handles_ffmpeg_progress_values(self) -> None:
        self.assertEqual(safe_int("42"), 42)
        self.assertIsNone(safe_int("N/A"))
        self.assertIsNone(safe_int("not-a-number"))

    def test_progress_timestamp_parser(self) -> None:
        self.assertAlmostEqual(parse_out_time_hms("01:02:03.5"), 3723.5)
        self.assertIsNone(parse_out_time_hms("N/A"))
        self.assertIsNone(parse_out_time_hms("broken"))


if __name__ == "__main__":
    unittest.main()
