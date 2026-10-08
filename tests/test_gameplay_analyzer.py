from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from analysis.audio_service import AnalysisCancellation, AnalysisCancelled
from analysis.gameplay.analyzer import GameplayAnalysisError
from analysis.gameplay.hearthstone.analyzer import HearthstoneAnalyzer, HearthstoneSettings, candidate_windows
from analysis.gameplay.hearthstone.visual.sampler import SampledFrame, VideoInfo
from analysis.gameplay.models import GameEvent, GameEventType, GameResult
from export.output_safety import atomic_promote_output


class FakeSampler:
    def __init__(self, *, duration: float = 60.0, coarse: tuple[float, ...] = (0, 10, 11, 30, 40, 41, 59)):
        self.duration = duration
        self.coarse = coarse
        self.probes = 0
        self.calls: list[dict] = []
        self.closed: list[dict] = []

    def probe(self, path: str | Path, *, cancellation: AnalysisCancellation | None = None) -> VideoInfo:
        self.probes += 1
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        return VideoInfo(self.duration, 8, 4)

    def sample(
        self, path: str | Path, *, fps: float, start_s: float = 0.0,
        end_s: float | None = None, max_width: int = 960,
        cancellation: AnalysisCancellation | None = None,
    ) -> Iterator[SampledFrame]:
        call = {"fps": fps, "start_s": start_s, "end_s": end_s, "max_width": max_width}
        self.calls.append(call)
        try:
            if fps == 1.0:
                timestamps = self.coarse
                marker = 1
            else:
                stop = self.duration if end_s is None else end_s
                timestamps = tuple(start_s + index / fps for index in range(int((stop - start_s) * fps)))
                marker = 2
            for timestamp in timestamps:
                if cancellation is not None:
                    cancellation.raise_if_cancelled()
                yield SampledFrame(timestamp, np.full((4, 8, 3), marker, dtype=np.uint8))
        finally:
            self.closed.append(call)


class FakeDetector:
    detector_version = "fake-visual-v1"
    pack_fingerprint = "fake-pack-v1"

    def __init__(self, resolver: Callable[[np.ndarray, float], list[GameEvent]] | None = None):
        self.resolver = resolver or self.stable_matches
        self.calls: list[float] = []

    @staticmethod
    def stable_matches(image: np.ndarray, timestamp: float) -> list[GameEvent]:
        if 10.0 <= timestamp <= 11.0:
            return [GameEvent(timestamp, GameEventType.VS_SCREEN, 0.96)]
        if 40.0 <= timestamp <= 41.0:
            return [GameEvent(timestamp, GameEventType.DEFEAT, 0.98)]
        return []

    def describe(self) -> dict:
        return {"detector_version": self.detector_version, "pack_fingerprint": self.pack_fingerprint}

    def detect(self, frame: np.ndarray, timestamp: float) -> list[GameEvent]:
        self.calls.append(timestamp)
        return self.resolver(frame, timestamp)


class GameplayAnalyzerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.video = self.root / "il VOD è pronto 🎮.mp4"
        self.video.write_bytes(b"fake local video fixture")
        self.cache = self.root / "cache"

    def analyzer(self, *, sampler: FakeSampler | None = None, detector: FakeDetector | None = None,
                 settings: HearthstoneSettings | None = None, use_cache: bool = False) -> HearthstoneAnalyzer:
        return HearthstoneAnalyzer(
            sampler=sampler or FakeSampler(), detector=detector or FakeDetector(), settings=settings,
            use_cache=use_cache, cache_directory=self.cache,
        )

    def test_coarse_candidates_merge_windows_and_fine_scan_only_local_ranges(self) -> None:
        sampler = FakeSampler()
        progress: list[int] = []
        result = self.analyzer(sampler=sampler).analyze(self.video, on_progress=progress.append)
        self.assertEqual(result.metadata["fine_windows"], [[8.0, 13.0], [38.0, 43.0]])
        self.assertEqual([(call["start_s"], call["end_s"]) for call in sampler.calls[1:]],
                         [(8.0, 13.0), (38.0, 43.0)])
        self.assertEqual([call["fps"] for call in sampler.calls], [1.0, 6.0, 6.0])
        self.assertEqual(result.metadata["fine_seconds"], 10.0)
        self.assertEqual(sampler.closed, sampler.calls)
        self.assertEqual(progress[0], 0)
        self.assertEqual(progress[-1], 100)
        self.assertEqual(progress, sorted(set(progress)))
        self.assertEqual([event.type for event in result.events], [GameEventType.VS_SCREEN, GameEventType.DEFEAT])
        self.assertEqual(len(result.games), 1)
        self.assertEqual(result.games[0].result, GameResult.LOSS)
        self.assertEqual((result.games[0].start, result.games[0].end), (10.0, 40.0))
        self.assertEqual(result.games[0].result_confidence, 0.98)
        self.assertFalse(hasattr(result, "cuts"))

    def test_transient_fine_match_is_rejected_without_inventing_a_game(self) -> None:
        detector = FakeDetector(lambda image, timestamp: (
            [GameEvent(timestamp, GameEventType.DEFEAT, 0.99)]
            if timestamp == 10.0 else []
        ))
        result = self.analyzer(detector=detector).analyze(self.video)
        self.assertEqual(result.events, [])
        self.assertEqual(result.games, [])
        self.assertEqual(result.metadata["rejected_clusters"], 1)
        self.assertTrue(any("unstable" in warning for warning in result.warnings))

    def test_observation_count_does_not_replace_required_temporal_stability(self) -> None:
        settings = HearthstoneSettings(minimum_stability_s=2.0)
        result = self.analyzer(settings=settings).analyze(self.video)
        self.assertEqual(result.events, [])
        self.assertEqual(result.metadata["rejected_clusters"], 2)

    def test_result_only_game_has_no_fabricated_start(self) -> None:
        detector = FakeDetector(lambda image, timestamp: (
            [GameEvent(timestamp, GameEventType.DEFEAT, 0.97)] if 40.0 <= timestamp <= 41.0 else []
        ))
        result = self.analyzer(detector=detector).analyze(self.video)
        self.assertEqual(len(result.games), 1)
        self.assertIsNone(result.games[0].start)
        self.assertEqual(result.games[0].start_confidence, 0.0)
        self.assertEqual(result.games[0].result, GameResult.LOSS)
        self.assertTrue(any("missing boundaries" in warning for warning in result.warnings))

    def test_no_coarse_candidates_avoids_fine_scan_and_warns_about_no_events(self) -> None:
        sampler = FakeSampler()
        result = self.analyzer(sampler=sampler, detector=FakeDetector(lambda image, timestamp: [])).analyze(self.video)
        self.assertEqual(len(sampler.calls), 1)
        self.assertEqual(result.metadata["fine_frames"], 0)
        self.assertEqual(result.metadata["fine_windows"], [])
        self.assertTrue(any("does not prove" in warning for warning in result.warnings))

    def test_cancellation_inside_detector_closes_sampler_without_cache(self) -> None:
        sampler = FakeSampler()
        token = AnalysisCancellation()

        def detect(image: np.ndarray, timestamp: float) -> list[GameEvent]:
            token.cancel()
            return []

        with self.assertRaises(AnalysisCancelled):
            self.analyzer(sampler=sampler, detector=FakeDetector(detect), use_cache=True).analyze(
                self.video, cancellation=token,
            )
        self.assertEqual(sampler.closed, sampler.calls)
        self.assertFalse(list(self.cache.glob("*.json")))

    def test_cancellation_inside_fine_detector_closes_both_generators(self) -> None:
        sampler = FakeSampler()
        token = AnalysisCancellation()

        def detect(image: np.ndarray, timestamp: float) -> list[GameEvent]:
            if image[0, 0, 0] == 2:
                token.cancel()
            return FakeDetector.stable_matches(image, timestamp)

        with self.assertRaises(AnalysisCancelled):
            self.analyzer(sampler=sampler, detector=FakeDetector(detect)).analyze(self.video, cancellation=token)
        self.assertEqual(len(sampler.calls), 2)
        self.assertEqual(sampler.closed, sampler.calls)

    def test_cancellation_inside_progress_closes_coarse_generator(self) -> None:
        sampler = FakeSampler()
        token = AnalysisCancellation()

        def progress(value: int) -> None:
            if value > 0:
                token.cancel()

        with self.assertRaises(AnalysisCancelled):
            self.analyzer(sampler=sampler, use_cache=True).analyze(
                self.video, cancellation=token, on_progress=progress,
            )
        self.assertEqual(sampler.closed, sampler.calls)
        self.assertFalse(list(self.cache.glob("*.json")))

    def test_cancelled_final_progress_does_not_publish_new_cache(self) -> None:
        token = AnalysisCancellation()

        def progress(value: int) -> None:
            if value == 100:
                token.cancel()

        with self.assertRaises(AnalysisCancelled):
            self.analyzer(use_cache=True).analyze(self.video, cancellation=token, on_progress=progress)
        self.assertFalse(list(self.cache.glob("*.json")))

    def test_pre_cancelled_request_does_not_probe_or_sample(self) -> None:
        sampler = FakeSampler()
        token = AnalysisCancellation()
        token.cancel()
        with self.assertRaises(AnalysisCancelled):
            self.analyzer(sampler=sampler).analyze(self.video, cancellation=token)
        self.assertEqual(sampler.probes, 0)
        self.assertEqual(sampler.calls, [])

    def test_cache_hit_avoids_all_frame_sampling_and_does_not_mutate_saved_result(self) -> None:
        original = self.analyzer(use_cache=True).analyze(self.video)
        sampler = FakeSampler()
        progress: list[int] = []
        cached = self.analyzer(sampler=sampler, use_cache=True).analyze(self.video, on_progress=progress.append)
        self.assertEqual(sampler.calls, [])
        self.assertEqual(progress, [0, 100])
        self.assertTrue(cached.metadata["cache_hit"])
        self.assertFalse(original.metadata["cache_hit"])
        self.assertEqual(cached.games, original.games)

    def test_source_changed_during_probe_does_not_return_stale_cache_hit(self) -> None:
        original = self.analyzer(use_cache=True).analyze(self.video)
        original_file = self.cache / (original.metadata["cache_key"] + ".json")
        original_bytes = original_file.read_bytes()
        source = self.video

        class SourceReplacingSampler(FakeSampler):
            def probe(self, path: str | Path, *, cancellation: AnalysisCancellation | None = None) -> VideoInfo:
                info = super().probe(path, cancellation=cancellation)
                source.write_bytes(b"source replaced while probe was running")
                return info

        sampler = SourceReplacingSampler()
        with self.assertRaises(GameplayAnalysisError) as caught:
            self.analyzer(sampler=sampler, use_cache=True).analyze(self.video)
        self.assertEqual(caught.exception.code, "source_changed")
        self.assertGreater(len(sampler.calls), 0)
        self.assertEqual(sampler.closed, sampler.calls)
        self.assertEqual(original_file.read_bytes(), original_bytes)
        self.assertEqual(len(list(self.cache.glob("*.json"))), 1)

    def test_corrupt_cache_is_missed_and_replaced_after_successful_analysis(self) -> None:
        original = self.analyzer(use_cache=True).analyze(self.video)
        cached_file = self.cache / (original.metadata["cache_key"] + ".json")
        cached_file.write_text("{interrupted JSON", encoding="utf-8")
        sampler = FakeSampler()
        result = self.analyzer(sampler=sampler, use_cache=True).analyze(self.video)
        self.assertGreater(len(sampler.calls), 0)
        self.assertFalse(result.metadata["cache_hit"])
        self.assertTrue(cached_file.read_text(encoding="utf-8").startswith("{"))

    def test_settings_detector_and_video_changes_invalidate_cached_analysis(self) -> None:
        original = self.analyzer(use_cache=True).analyze(self.video)
        changed_settings = self.analyzer(settings=HearthstoneSettings(fine_padding_s=3.0), use_cache=True).analyze(self.video)
        detector = FakeDetector()
        detector.pack_fingerprint = "new-template-bytes"
        changed_detector = self.analyzer(detector=detector, use_cache=True).analyze(self.video)
        self.video.write_bytes(b"replacement video with a different identity")
        changed_video = self.analyzer(use_cache=True).analyze(self.video)
        results = [original, changed_settings, changed_detector, changed_video]
        self.assertEqual(len({result.metadata["cache_key"] for result in results}), 4)
        self.assertTrue(all(result.metadata["cache_hit"] is False for result in results))

    def test_source_mutated_during_scan_is_rejected_without_cache(self) -> None:
        changed = False

        def detect(image: np.ndarray, timestamp: float) -> list[GameEvent]:
            nonlocal changed
            if not changed:
                self.video.write_bytes(b"changed while analysis was running")
                changed = True
            return FakeDetector.stable_matches(image, timestamp)

        with self.assertRaises(GameplayAnalysisError) as caught:
            self.analyzer(detector=FakeDetector(detect), use_cache=True).analyze(self.video)
        self.assertEqual(caught.exception.code, "source_changed")
        self.assertFalse(list(self.cache.glob("*.json")))

    def test_candidate_limit_closes_sampling_generator(self) -> None:
        sampler = FakeSampler()
        with self.assertRaises(GameplayAnalysisError) as caught:
            self.analyzer(sampler=sampler, settings=HearthstoneSettings(max_observations=1)).analyze(self.video)
        self.assertEqual(caught.exception.code, "candidate_limit")
        self.assertEqual(sampler.closed, sampler.calls)

    def test_cache_write_error_is_reported_without_discarding_successful_analysis(self) -> None:
        with patch("analysis.gameplay.cache.atomic_write_json", side_effect=OSError("disk full")):
            result = self.analyzer(use_cache=True).analyze(self.video)
        self.assertEqual(len(result.games), 1)
        self.assertTrue(any("cache could not be written" in warning for warning in result.warnings))

    def test_debug_frames_preserve_existing_file_and_publish_unique_name(self) -> None:
        event = GameEvent(10.0, GameEventType.VS_SCREEN, 0.96)
        directory = self.root / "debug frames 🎮"
        directory.mkdir()
        existing = directory / "00000010.000_vs_screen_0.960_0000.jpg"
        existing.write_bytes(b"previous user file")
        encoder = SimpleNamespace(imencode=lambda extension, image: (True, np.frombuffer(b"encoded JPEG", dtype=np.uint8)))
        with patch("analysis.gameplay.hearthstone.visual.templates.require_opencv", return_value=encoder):
            HearthstoneAnalyzer._save_frame(directory, np.zeros((4, 8, 3), dtype=np.uint8), event, 0)
        self.assertEqual(existing.read_bytes(), b"previous user file")
        self.assertEqual(existing.with_name(existing.stem + "_2.jpg").read_bytes(), b"encoded JPEG")
        self.assertEqual(list(directory.glob(".gameplay-frame-*")), [])

    def test_debug_frame_existing_hardlink_does_not_modify_source(self) -> None:
        event = GameEvent(10.0, GameEventType.VS_SCREEN, 0.96)
        directory = self.root / "debug frames"
        directory.mkdir()
        candidate = directory / "00000010.000_vs_screen_0.960_0000.jpg"
        try:
            os.link(self.video, candidate)
        except OSError as exc:
            self.skipTest(f"Hard links unavailable: {exc}")
        source_bytes = self.video.read_bytes()
        encoder = SimpleNamespace(imencode=lambda extension, image: (True, np.frombuffer(b"encoded JPEG", dtype=np.uint8)))
        with patch("analysis.gameplay.hearthstone.visual.templates.require_opencv", return_value=encoder):
            HearthstoneAnalyzer._save_frame(directory, np.zeros((4, 8, 3), dtype=np.uint8), event, 0)
        self.assertEqual(self.video.read_bytes(), source_bytes)
        self.assertEqual(candidate.read_bytes(), source_bytes)
        self.assertEqual(candidate.with_name(candidate.stem + "_2.jpg").read_bytes(), b"encoded JPEG")

    def test_debug_frame_race_uses_next_name_without_replacing_foreign_file(self) -> None:
        event = GameEvent(10.0, GameEventType.VS_SCREEN, 0.96)
        directory = self.root / "debug frames"
        candidate = directory / "00000010.000_vs_screen_0.960_0000.jpg"
        encoder = SimpleNamespace(imencode=lambda extension, image: (True, np.frombuffer(b"encoded JPEG", dtype=np.uint8)))

        def promote(temporary: Path, output: Path, *, overwrite: bool) -> None:
            self.assertFalse(overwrite)
            if output == candidate:
                candidate.write_bytes(b"foreign raced file")
            atomic_promote_output(temporary, output, overwrite=overwrite)

        with patch("analysis.gameplay.hearthstone.visual.templates.require_opencv", return_value=encoder), patch(
            "analysis.gameplay.hearthstone.analyzer.atomic_promote_output", side_effect=promote,
        ):
            HearthstoneAnalyzer._save_frame(directory, np.zeros((4, 8, 3), dtype=np.uint8), event, 0)
        self.assertEqual(candidate.read_bytes(), b"foreign raced file")
        self.assertEqual(candidate.with_name(candidate.stem + "_2.jpg").read_bytes(), b"encoded JPEG")
        self.assertEqual(list(directory.glob(".gameplay-frame-*")), [])

    def test_failed_debug_frame_publish_removes_temporary_file(self) -> None:
        event = GameEvent(10.0, GameEventType.VS_SCREEN, 0.96)
        directory = self.root / "debug frames"
        encoder = SimpleNamespace(imencode=lambda extension, image: (True, np.frombuffer(b"encoded JPEG", dtype=np.uint8)))
        with patch("analysis.gameplay.hearthstone.visual.templates.require_opencv", return_value=encoder), patch(
            "analysis.gameplay.hearthstone.analyzer.atomic_promote_output", side_effect=OSError("disk error"),
        ):
            with self.assertRaises(OSError):
                HearthstoneAnalyzer._save_frame(directory, np.zeros((4, 8, 3), dtype=np.uint8), event, 0)
        self.assertEqual(list(directory.iterdir()), [])

    def test_candidate_windows_clamp_to_video_edges_and_merge_touching_ranges(self) -> None:
        events = [GameEvent(0.5, GameEventType.VS_SCREEN, 0.9),
                  GameEvent(4.0, GameEventType.VS_SCREEN, 0.9),
                  GameEvent(9.5, GameEventType.DEFEAT, 0.9)]
        self.assertEqual(candidate_windows(events, duration=10.0, padding=2.0), [(0.0, 6.0), (7.5, 10.0)])


if __name__ == "__main__":
    unittest.main()
