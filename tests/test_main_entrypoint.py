from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]


class MainEntrypointTests(unittest.TestCase):
    def test_smoke_mode_starts_and_exits_cleanly(self) -> None:
        env = os.environ.copy()
        env["QT_QPA_PLATFORM"] = "offscreen"
        env["QTWEBENGINE_CHROMIUM_FLAGS"] = "--disable-gpu --no-sandbox"

        completed = subprocess.run(
            [sys.executable, str(ROOT / "main.py"), "--smoke-test"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )

        details = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
        self.assertEqual(completed.returncode, 0, details)


if __name__ == "__main__":
    unittest.main()
