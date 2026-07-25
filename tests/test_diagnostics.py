from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from utils.diagnostics import create_support_bundle


class DiagnosticsTests(unittest.TestCase):
    def test_bundle_contains_metadata_and_redacted_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = root / "logs"
            logs.mkdir()
            crashes = root / "crashes"
            crashes.mkdir()
            (logs / "session.log").write_text(
                f"opened={Path.home() / 'private' / 'video.mp4'}",
                encoding="utf-8",
            )
            (crashes / "crash.log").write_text("RuntimeError: example", encoding="utf-8")

            bundle = create_support_bundle(
                root / "diagnostics.zip",
                session_logs_dir=logs,
                crash_logs_dir=crashes,
                consistency_errors=["example"],
            )

            with zipfile.ZipFile(bundle) as archive:
                details = json.loads(archive.read("diagnostics.json"))
                log = archive.read("logs/session-1.log").decode("utf-8")
                self.assertEqual(details["project_consistency_errors"], ["example"])
                self.assertIn("%USERPROFILE%", log)
                self.assertNotIn(str(Path.home()), log)
                self.assertIn("RuntimeError: example", archive.read("crashes/crash-1.log").decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
