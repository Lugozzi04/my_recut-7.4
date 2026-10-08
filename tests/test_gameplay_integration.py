"""Synthetic visual fixtures only: these do not validate Hearthstone accuracy."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from analysis.gameplay.hearthstone.analyzer import HearthstoneAnalyzer, HearthstoneSettings
from analysis.gameplay.hearthstone.visual.detector import TemplateVisualDetector
from analysis.gameplay.hearthstone.visual.sampler import FFmpegFrameSampler
from analysis.gameplay.hearthstone.visual.templates import TemplateRepository
from analysis.gameplay.models import GameEvent, GameEventType, GameResult
from utils.ffmpeg import ensure_ffmpeg
from utils.subprocess_utils import run_no_window


class NumpyColorFixtureDetector:
    """Test double classifying decoded pixels; it never uses timestamps as labels.

    This deliberately verifies FFmpeg/orchestration/assembly/cache independently
    of OpenCV. It is not a Hearthstone detector or template-matching benchmark.
    """

    detector_version = "test-only-numpy-color-v1"
    pack_fingerprint = "synthetic-red-vs-green-victory-blue-defeat"

    def describe(self) -> dict:
        return {"detector_version": self.detector_version, "pack_fingerprint": self.pack_fingerprint,
                "test_only": True, "method": "normalized BGR channel dominance"}

    def detect(self, frame: np.ndarray, timestamp: float) -> list[GameEvent]:
        channels = frame.mean(axis=(0, 1)) / 255.0
        order = np.argsort(channels)
        strongest = int(order[-1])
        confidence = float(channels[strongest] - channels[int(order[-2])])
        if confidence < 0.5:
            return []
        kind = {0: GameEventType.DEFEAT, 1: GameEventType.VICTORY, 2: GameEventType.VS_SCREEN}[strongest]
        return [GameEvent(timestamp, kind, confidence, metadata={"test_only": True})]


class GameplayIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.video = self.root / "il VOD è pronto 🎮.mp4"
        self.settings = HearthstoneSettings(fine_fps=10.0, fine_padding_s=0.75, max_width=160)

    @staticmethod
    def anchor(timestamp: float) -> GameEventType | None:
        if 1.0 <= timestamp < 2.0 or 7.0 <= timestamp < 8.0:
            return GameEventType.VS_SCREEN
        if 5.0 <= timestamp < 6.0:
            return GameEventType.VICTORY
        if 10.0 <= timestamp < 11.0:
            return GameEventType.DEFEAT
        return None

    def assert_two_games(self, analysis) -> None:
        self.assertEqual([game.result for game in analysis.games], [GameResult.WIN, GameResult.LOSS])
        self.assertEqual([event.type for event in analysis.events], [
            GameEventType.VS_SCREEN, GameEventType.VICTORY, GameEventType.VS_SCREEN, GameEventType.DEFEAT,
        ])
        for event, expected in zip(analysis.events, (1.0, 5.0, 7.0, 10.0)):
            self.assertAlmostEqual(event.timestamp, expected, delta=1 / self.settings.fine_fps + 1e-6)
            # The fixture's actual source PTS lie on a 20 FPS grid. Fine-scan
            # nominal indices would start at .25 and are not event timestamps.
            self.assertAlmostEqual(event.timestamp * 20, round(event.timestamp * 20), places=5)
        for game, boundaries in zip(analysis.games, ((1.0, 5.0), (7.0, 10.0))):
            self.assertTrue(game.complete)
            self.assertAlmostEqual(game.start, boundaries[0], delta=0.100001)
            self.assertAlmostEqual(game.end, boundaries[1], delta=0.100001)
        self.assertFalse(hasattr(analysis, "cuts"))
        self.assertLess(analysis.metadata["fine_seconds"], analysis.duration)
        self.assertGreater(analysis.metadata["fine_frames"], 0)

    def test_real_ffmpeg_pixels_to_games_and_cache_reuse_without_source_changes(self) -> None:
        ffmpeg, _ = ensure_ffmpeg()
        filters = (
            "drawbox=color=0xFF0000:t=fill:enable='gte(t,1)*lt(t,2)+gte(t,7)*lt(t,8)',"
            "drawbox=color=0x00FF00:t=fill:enable='gte(t,5)*lt(t,6)',"
            "drawbox=color=0x0000FF:t=fill:enable='gte(t,10)*lt(t,11)'"
        )
        generated = run_no_window([
            ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-y", "-f", "lavfi", "-i",
            "color=c=black:s=160x96:r=20:d=12", "-vf", filters, "-c:v", "libx264",
            "-preset", "ultrafast", "-crf", "0", "-pix_fmt", "yuv444p", str(self.video),
        ], capture_output=True, text=True, timeout=30)
        self.assertEqual(generated.returncode, 0, generated.stderr)
        original_bytes = hashlib.sha256(self.video.read_bytes()).hexdigest()
        original_stat = self.video.stat()
        cache = self.root / "isolated cache"
        progress: list[int] = []
        analyzer = HearthstoneAnalyzer(
            detector=NumpyColorFixtureDetector(), sampler=FFmpegFrameSampler(),
            settings=self.settings, cache_directory=cache,
        )
        result = analyzer.analyze(self.video, on_progress=progress.append)
        self.assert_two_games(result)
        self.assertEqual(progress[0], 0)
        self.assertEqual(progress[-1], 100)
        self.assertEqual(progress, sorted(set(progress)))
        self.assertFalse(result.metadata["cache_hit"])
        resumed_sampler = FFmpegFrameSampler()
        resumed_analyzer = HearthstoneAnalyzer(
            detector=NumpyColorFixtureDetector(), sampler=resumed_sampler,
            settings=self.settings, cache_directory=cache,
        )
        with patch.object(resumed_sampler, "sample", side_effect=AssertionError("Cache hit must not decode frames")) as sample:
            resumed = resumed_analyzer.analyze(self.video)
        sample.assert_not_called()
        self.assertTrue(resumed.metadata["cache_hit"])
        self.assertEqual(resumed.events, result.events)
        self.assertEqual(resumed.games, result.games)
        self.assertEqual(hashlib.sha256(self.video.read_bytes()).hexdigest(), original_bytes)
        self.assertEqual((self.video.stat().st_size, self.video.stat().st_mtime_ns),
                         (original_stat.st_size, original_stat.st_mtime_ns))
        self.assertEqual({path.name for path in self.root.iterdir()}, {cache.name, self.video.name})

    def test_real_opencv_templates_through_ffmpeg_on_synthetic_visual_anchors(self) -> None:
        try:
            import cv2
        except (ImportError, OSError):
            self.skipTest("Optional OpenCV gameplay dependency is unavailable")
        # Random patches model only visually distinct classes. They are not
        # Hearthstone artwork and cannot establish production precision/recall.
        rng = np.random.default_rng(42)
        patches = {kind: rng.integers(0, 256, (12, 16), dtype=np.uint8) for kind in GameEventType}
        pack = self.root / "synthetic templates è 🎮"
        classes = {}
        for kind, image in patches.items():
            directory = pack / kind.value.lower()
            directory.mkdir(parents=True)
            encoded, png = cv2.imencode(".png", image)
            self.assertTrue(encoded)
            (directory / "anchor.png").write_bytes(png.tobytes())
            classes[kind.value] = {"roi": [0, 0, 1, 1], "threshold": 0.98,
                                   "scales": [1.0], "directory": directory.name}
        config = {"schema_version": 1, "profile": "hearthstone", "reference_size": [160, 96],
                  "viewport": [0, 0, 1, 1], "ambiguity_margin": 0.05, "classes": classes}
        pack_path = pack / "templates.json"
        pack_path.write_text(json.dumps(config), encoding="utf-8")
        frames = bytearray()
        for index in range(240):
            image = np.full((96, 160, 3), 20, dtype=np.uint8)
            kind = self.anchor(index / 20)
            if kind is not None:
                image[32:44, 64:80] = patches[kind][:, :, None]
            frames.extend(image.tobytes())
        self.video = self.video.with_suffix(".mkv")
        ffmpeg, _ = ensure_ffmpeg()
        generated = run_no_window([
            ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-y", "-f", "rawvideo",
            "-pix_fmt", "bgr24", "-s", "160x96", "-r", "20", "-i", "pipe:0", "-an",
            "-c:v", "ffv1", "-level", "3", "-pix_fmt", "bgr0", str(self.video),
        ], input=bytes(frames), capture_output=True, timeout=30)
        self.assertEqual(generated.returncode, 0, generated.stderr.decode("utf-8", errors="replace"))
        detector = TemplateVisualDetector(TemplateRepository(pack_path).load())
        result = HearthstoneAnalyzer(detector=detector, settings=self.settings, use_cache=False).analyze(self.video)
        self.assert_two_games(result)
        self.assertEqual(result.settings["detector"]["method"], "TM_CCOEFF_NORMED")
        self.assertEqual(result.settings["detector"]["opencv_version"], cv2.__version__)
        self.assertTrue(all(event.confidence > 0.99 for event in result.events))


if __name__ == "__main__":
    unittest.main()
