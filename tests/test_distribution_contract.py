from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DistributionContractTests(unittest.TestCase):
    def test_runtime_dependencies_are_exactly_pinned(self) -> None:
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        packages = [line.strip() for line in requirements if line.strip() and not line.startswith("#")]

        self.assertTrue(packages)
        self.assertTrue(all("==" in package for package in packages))

    def test_version_has_release_shape(self) -> None:
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()

        self.assertRegex(version, r"^\d+\.\d+\.\d+$")

    def test_installer_does_not_copy_a_developer_virtualenv(self) -> None:
        installer = (ROOT / "installer" / "AutoCutter.iss").read_text(encoding="utf-8").lower()

        self.assertNotIn(".venv", installer)
        self.assertNotIn("ai_runtime", installer)

    def test_core_bundle_excludes_optional_ai_and_dev_stacks(self) -> None:
        spec = (ROOT / "build" / "AutoCutter.spec").read_text(encoding="utf-8").lower()

        for package in ("pytest", "scipy", "silero_vad", "soundfile", "speechbrain"):
            self.assertIn(f'"{package}"', spec)
        self.assertNotIn('for optional_folder in ("pretrained_models",)', spec)

    def test_twitch_downloader_is_pinned_and_packaged(self) -> None:
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        spec = (ROOT / "build" / "AutoCutter.spec").read_text(encoding="utf-8")

        self.assertIn("yt-dlp==2026.8.19", requirements)
        self.assertIn('collect_data_files("yt_dlp")', spec)
        self.assertIn('collect_submodules("yt_dlp")', spec)

    def test_build_runs_packaged_smoke_test(self) -> None:
        script = (ROOT / "build" / "build.ps1").read_text(encoding="utf-8")
        main = (ROOT / "main.py").read_text(encoding="utf-8")

        self.assertIn("-FilePath $AppExe", script)
        self.assertIn("$SmokeProcess.ExitCode -ne 0", script)
        self.assertIn('"--smoke-test" in sys.argv', main)

    def test_ffmpeg_record_matches_bundled_binaries(self) -> None:
        record = (ROOT / "docs" / "FFMPEG_DISTRIBUTION.md").read_text(encoding="utf-8").lower()

        for binary in ("ffmpeg.exe", "ffprobe.exe"):
            self.assertIn(binary, record)
        self.assertEqual(len(re.findall(r"`[0-9a-f]{64}`", record)), 3)


if __name__ == "__main__":
    unittest.main()
