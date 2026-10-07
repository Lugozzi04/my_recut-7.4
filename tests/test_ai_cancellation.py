from __future__ import annotations

import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from analysis.cancellation import AiCancellationToken, AiPipelineCancelled


class AiCancellationTests(unittest.TestCase):
    def test_cancel_terminates_active_process(self) -> None:
        token = AiCancellationToken()
        outcome: list[BaseException] = []

        def target() -> None:
            try:
                token.run([sys.executable, "-c", "import time; time.sleep(30)"])
            except BaseException as exc:
                outcome.append(exc)

        thread = threading.Thread(target=target)
        thread.start()
        time.sleep(0.2)
        token.cancel()
        thread.join(timeout=5)

        self.assertFalse(thread.is_alive())
        self.assertTrue(any(isinstance(exc, AiPipelineCancelled) for exc in outcome))

    def test_cancel_before_run_prevents_process_start(self) -> None:
        token = AiCancellationToken()
        token.cancel()

        with self.assertRaises(AiPipelineCancelled):
            token.run([sys.executable, "-c", "print('should not run')"])

    def test_failed_taskkill_falls_back_to_direct_termination(self) -> None:
        process = Mock()
        process.pid = 123
        process.poll.return_value = None
        process.wait.return_value = 0

        with (
            patch("analysis.cancellation.os.name", "nt"),
            patch(
                "analysis.cancellation.run_no_window",
                return_value=SimpleNamespace(returncode=1),
            ),
        ):
            AiCancellationToken._terminate_process(process)

        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=0.75)
        process.kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
