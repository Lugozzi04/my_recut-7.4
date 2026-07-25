from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from analysis.cut_engine import Segment
from ui.main_window_components import (
    MiniTimelineWidget,
    NoWheelDoubleSpinBox,
    NoWheelSpinBox,
    ToggleSwitch,
)


class MainWindowComponentsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_integer_spinbox_clamps_typed_value(self) -> None:
        spin = NoWheelSpinBox()
        spin.setRange(0, 100)
        spin.lineEdit().setText("250")
        spin._commit_text_value()
        self.assertEqual(spin.value(), 100)

    def test_decimal_spinbox_accepts_comma_and_clamps(self) -> None:
        spin = NoWheelDoubleSpinBox()
        spin.setRange(-10.0, 10.0)
        spin.lineEdit().setText("3,5")
        spin._commit_text_value()
        self.assertAlmostEqual(spin.value(), 3.5)

    def test_toggle_and_mini_timeline_keep_state(self) -> None:
        toggle = ToggleSwitch()
        toggle.setChecked(True)
        self.assertTrue(toggle.isChecked())

        timeline = MiniTimelineWidget()
        segments = [Segment(1.0, 2.0)]
        timeline.set_data(5.0, segments, [1])
        self.assertEqual(timeline._duration, 5.0)
        self.assertEqual(timeline._segments, segments)
        self.assertEqual(timeline._speaker_ids, [1])


if __name__ == "__main__":
    unittest.main()
