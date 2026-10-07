from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ui.main_window import MainWindow


class GuiCloseLifecycleTests(unittest.TestCase):
    def test_close_waits_for_qthread_after_bounded_cancellation(self) -> None:
        window = SimpleNamespace(
            input_path=None,
            _tracks=[],
            _app_log=Mock(),
            _abort_export=Mock(),
            _abort_analysis_tasks=Mock(),
            _background_qthreads_running=lambda: True,
            close=Mock(),
        )
        event = Mock()
        with patch("ui.main_window.QTimer.singleShot") as later:
            MainWindow.closeEvent(window, event)
        event.ignore.assert_called_once()
        later.assert_called_once_with(250, window.close)

    def test_detached_running_threads_are_still_considered(self) -> None:
        thread = SimpleNamespace(isRunning=lambda: True)
        window = SimpleNamespace(_orphan_analysis_threads=[thread])
        self.assertTrue(MainWindow._background_qthreads_running(window))
        window._orphan_analysis_threads = []
        self.assertFalse(MainWindow._background_qthreads_running(window))


if __name__ == "__main__":
    unittest.main()
