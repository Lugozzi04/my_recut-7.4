from __future__ import annotations

import sys
import threading
import time
import unittest

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


if __name__ == "__main__":
    unittest.main()
