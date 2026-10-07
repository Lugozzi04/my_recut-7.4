from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class MainEntrypointTests(unittest.TestCase):
    def test_smoke_mode_starts_and_exits_cleanly(self) -> None:
        env = os.environ.copy()
        env["QT_QPA_PLATFORM"] = "offscreen"
        env["QTWEBENGINE_CHROMIUM_FLAGS"] = "--disable-gpu --no-sandbox"

        with tempfile.TemporaryDirectory(prefix="autocutter-gui-smoke-") as tmp:
            env.update({
                "AUTO_CUTTER_CONFIG_DIR": str(Path(tmp) / "config"),
                "AUTO_CUTTER_DATA_DIR": str(Path(tmp) / "data"),
                "AUTO_CUTTER_PIPELINE_STORE": str(Path(tmp) / "pipeline" / "jobs.json"),
                "AUTO_CUTTER_CACHE_DIR": str(Path(tmp) / "cache"),
                "AUTO_CUTTER_CREDENTIALS_DIR": str(Path(tmp) / "credentials"),
            })
            completed = subprocess.run(
                [sys.executable, str(ROOT / "main.py"), "--smoke-test"],
                cwd=ROOT, env=env, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=30, check=False,
            )

        details = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
        self.assertEqual(completed.returncode, 0, details)


if __name__ == "__main__":
    unittest.main()
