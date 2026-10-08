from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from analysis.audio_service import AnalysisCancellation, AnalysisCancelled
from analysis.gameplay.hearthstone.analyzer import HearthstoneAnalyzer, HearthstoneSettings
from analysis.gameplay.hearthstone.visual.sampler import SampledFrame, VideoInfo
from analysis.gameplay.hearthstone.visual.templates import NormalizedROI
from analysis.gameplay.models import GameEvent, GameEventType, GameResult


class DenseSampler:
    def __init__(self, duration: float = 4.0) -> None:
        self.duration = duration
        self.calls: list[dict] = []
        self.closed: list[dict] = []

    def probe(self, path: str | Path, *, cancellation: AnalysisCancellation | None = None) -> VideoInfo:
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        return VideoInfo(self.duration, 1920, 1080)

    def sample(
        self, path: str | Path, *, fps: float, start_s: float = 0.0,
        end_s: float | None = None, max_width: int = 960,
        cancellation: AnalysisCancellation | None = None,
    ) -> Iterator[SampledFrame]:
        call = {"kind": "full", "fps": fps, "start_s": start_s, "end_s": end_s, "max_width": max_width}
        self.calls.append(call)
        finish = self.duration if end_s is None else end_s
        try:
            for index in range(int(np.ceil((finish - start_s) * fps))):
                timestamp = start_s + index / fps
                if timestamp >= finish:
                    break
                if cancellation is not None:
                    cancellation.raise_if_cancelled()
                yield SampledFrame(timestamp, np.zeros((8, 16, 3), dtype=np.uint8))
        finally:
            self.closed.append(call)

    def sample_roi(
        self, path: str | Path, *, roi: tuple[float, float, float, float],
        reference_size: tuple[int, int], fps: float, start_s: float = 0.0,
        end_s: float | None = None, output_size: tuple[int, int] | None = None,
        cancellation: AnalysisCancellation | None = None,
    ) -> Iterator[SampledFrame]:
        call = {"kind": "roi", "roi": roi, "reference_size": reference_size, "fps": fps,
                "start_s": start_s, "end_s": end_s, "output_size": output_size}
        self.calls.append(call)
        finish = self.duration if end_s is None else end_s
        width, height = output_size or (288, 120)
        try:
            for index in range(int(np.ceil((finish - start_s) * fps))):
                timestamp = start_s + index / fps
                if timestamp >= finish:
                    break
                if cancellation is not None:
                    cancellation.raise_if_cancelled()
                yield SampledFrame(timestamp, np.zeros((height, width, 3), dtype=np.uint8))
        finally:
            self.closed.append(call)


class DenseDetector:
    detector_version = "dense-test-detector"
    pack_fingerprint = "dense-test-pack"

    def __init__(
        self, *, vs_end: float = 1.5, result_times: tuple[float, ...] = (3.1, 3.15),
        viewport: tuple[float, float, float, float] = (0, 0, 1, 1),
        on_roi: Callable[[], None] | None = None,
    ) -> None:
        self.vs_end = vs_end
        self.result_times = result_times
        self.viewport = NormalizedROI.from_value(list(viewport))
        self.pack = SimpleNamespace(reference_size=(960, 540), viewport=self.viewport)
        self.full_calls: list[tuple[float, tuple[GameEventType, ...] | None]] = []
        self.roi_calls: list[tuple[float, tuple[int, ...], NormalizedROI, tuple[GameEventType, ...]]] = []
        self.on_roi = on_roi

    def describe(self) -> dict:
        return {"detector_version": self.detector_version, "pack_fingerprint": self.pack_fingerprint,
                "reference_size": [960, 540], "viewport": self.viewport.to_list()}

    def result_scan_roi(self) -> NormalizedROI:
        return NormalizedROI.from_value([0.35, 0.47, 0.3, 0.22])

    def events(self, timestamp: float) -> list[GameEvent]:
        if 1.0 - 1e-9 <= timestamp <= self.vs_end + 1e-9:
            return [GameEvent(timestamp, GameEventType.VS_SCREEN, 0.97)]
        if any(abs(timestamp - expected) < 1e-6 for expected in self.result_times):
            return [GameEvent(timestamp, GameEventType.DEFEAT, 0.98)]
        return []

    def detect(
        self, frame: np.ndarray, timestamp: float, *, event_types: tuple[GameEventType, ...] | None = None,
    ) -> list[GameEvent]:
        self.full_calls.append((timestamp, event_types))
        return [event for event in self.events(timestamp) if event_types is None or event.type in event_types]

    def detect_roi(
        self, frame: np.ndarray, timestamp: float, *, roi: NormalizedROI,
        event_types: tuple[GameEventType, ...],
    ) -> list[GameEvent]:
        self.roi_calls.append((timestamp, frame.shape, roi, event_types))
        if self.on_roi is not None:
            self.on_roi()
        return [event for event in self.events(timestamp) if event.type in event_types]


class LegacySampler:
    """Old injection contract deliberately has no sample_roi method."""

    def __init__(self) -> None:
        self.delegate = DenseSampler()

    def probe(self, path, **options):
        return self.delegate.probe(path, **options)

    def sample(self, path, **options):
        return self.delegate.sample(path, **options)


class LegacyDetector:
    detector_version = "legacy-test-detector"
    pack_fingerprint = "legacy-test-pack"

    def describe(self) -> dict:
        return {"detector_version": self.detector_version, "pack_fingerprint": self.pack_fingerprint}

    def detect(self, frame: np.ndarray, timestamp: float) -> list[GameEvent]:
        if 1 <= timestamp <= 1.5:
            return [GameEvent(timestamp, GameEventType.VS_SCREEN, 0.97)]
        if 3 <= timestamp <= 3.5:
            return [GameEvent(timestamp, GameEventType.DEFEAT, 0.98)]
        return []


class GameplayDenseAnalyzerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "dense VOD è 🎮.mp4"
        self.source.write_bytes(b"local fake video for dense orchestration")
        self.cache = self.root / "cache"

    def analyzer(self, *, sampler=None, detector=None, settings=None, use_cache=False) -> HearthstoneAnalyzer:
        return HearthstoneAnalyzer(
            sampler=sampler or DenseSampler(), detector=detector or DenseDetector(), settings=settings,
            use_cache=use_cache, cache_directory=self.cache,
        )

    def test_100ms_result_missed_by_coarse_is_recovered_by_dense_roi(self) -> None:
        sampler = DenseSampler()
        detector = DenseDetector()
        progress: list[int] = []
        result = self.analyzer(sampler=sampler, detector=detector).analyze(self.source, on_progress=progress.append)
        self.assertEqual([event.type for event in result.events], [GameEventType.VS_SCREEN, GameEventType.DEFEAT])
        self.assertEqual(len(result.games), 1)
        self.assertEqual(result.games[0].result, GameResult.LOSS)
        self.assertAlmostEqual(result.games[0].start, 1.0)
        self.assertAlmostEqual(result.games[0].end, 3.1)
        self.assertEqual(result.events[1].metadata["observation_count"], 2)
        self.assertAlmostEqual(result.events[1].metadata["temporal_span_s"], 0.05)
        self.assertEqual(result.metadata["dense_result_frames"], 80)
        self.assertEqual(result.metadata["dense_frame_size"], [288, 120])
        self.assertIn("sampling_strategy", result.metadata)
        self.assertTrue(detector.full_calls)
        self.assertTrue(all(kinds == (GameEventType.VS_SCREEN,) for _, kinds in detector.full_calls))
        self.assertTrue(all(kinds == (GameEventType.VICTORY, GameEventType.DEFEAT)
                            for _, _, _, kinds in detector.roi_calls))
        self.assertTrue(all(shape == (120, 288, 3) for _, shape, _, _ in detector.roi_calls))
        self.assertEqual(sampler.closed, sampler.calls)
        self.assertEqual(progress, sorted(set(progress)))
        self.assertEqual((progress[0], progress[-1]), (0, 100))
        self.assertFalse(hasattr(result, "cuts"))

    def test_dense_scan_covers_entire_video_without_coarse_candidates(self) -> None:
        sampler = DenseSampler()
        result = self.analyzer(sampler=sampler, detector=DenseDetector(vs_end=0)).analyze(self.source)
        self.assertEqual([event.type for event in result.events], [GameEventType.DEFEAT])
        self.assertEqual(result.metadata["fine_windows"], [])
        self.assertEqual(result.metadata["fine_frames"], 0)
        roi_calls = [call for call in sampler.calls if call["kind"] == "roi"]
        self.assertEqual(len(roi_calls), 1)
        self.assertEqual(roi_calls[0]["fps"], 20)
        self.assertEqual(roi_calls[0]["start_s"], 0)
        self.assertIn(roi_calls[0]["end_s"], (None, 4.0))
        self.assertIsNone(result.games[0].start)
        self.assertEqual(result.games[0].result, GameResult.LOSS)

    def test_single_dense_result_observation_is_rejected(self) -> None:
        result = self.analyzer(detector=DenseDetector(result_times=(3.1,))).analyze(self.source)
        self.assertEqual([event.type for event in result.events], [GameEventType.VS_SCREEN])
        self.assertEqual(result.games[0].result, GameResult.UNKNOWN)
        self.assertIsNone(result.games[0].end)
        self.assertGreaterEqual(result.metadata["rejected_clusters"], 1)

    def test_vs_still_requires_three_observations_and_250ms_stability(self) -> None:
        cases = [(HearthstoneSettings(), 1.2), (HearthstoneSettings(fine_fps=30), 1.08)]
        for settings, vs_end in cases:
            with self.subTest(settings=settings):
                result = self.analyzer(detector=DenseDetector(vs_end=vs_end), settings=settings).analyze(self.source)
                self.assertEqual([event.type for event in result.events], [GameEventType.DEFEAT])
                self.assertEqual(len(result.games), 1)
                self.assertIsNone(result.games[0].start)
                self.assertEqual(result.games[0].result, GameResult.LOSS)

    def test_result_stability_is_not_satisfied_by_two_too_close_observations(self) -> None:
        settings = HearthstoneSettings(result_fps=40, minimum_result_stability_s=0.05)
        result = self.analyzer(detector=DenseDetector(result_times=(3.1, 3.125)), settings=settings).analyze(self.source)
        self.assertEqual([event.type for event in result.events], [GameEventType.VS_SCREEN])
        self.assertEqual(result.games[0].result, GameResult.UNKNOWN)

    def test_rounded_reference_bounds_are_composed_through_viewport(self) -> None:
        sampler = DenseSampler()
        viewport = (0.1, 0.2, 0.8, 0.7)
        detector = DenseDetector(viewport=viewport)
        self.analyzer(sampler=sampler, detector=detector).analyze(self.source)
        call = next(call for call in sampler.calls if call["kind"] == "roi")
        expected = (0.1 + 336 / 960 * 0.8, 0.2 + 253 / 540 * 0.7,
                    (624 - 336) / 960 * 0.8, (373 - 253) / 540 * 0.7)
        np.testing.assert_allclose(call["roi"], expected, atol=1e-12)
        self.assertEqual(call["output_size"], (288, 120))
        self.assertEqual(call["reference_size"], (960, 540))
        self.assertTrue(all(item[2].pixel_bounds(960, 540) == (336, 253, 624, 373)
                            for item in detector.roi_calls))

    def test_cancellation_inside_roi_closes_all_generators_without_cache_and_retry_works(self) -> None:
        sampler = DenseSampler()
        token = AnalysisCancellation()
        detector = DenseDetector(on_roi=token.cancel)
        with self.assertRaises(AnalysisCancelled):
            self.analyzer(sampler=sampler, detector=detector, use_cache=True).analyze(self.source, cancellation=token)
        self.assertEqual(sampler.closed, sampler.calls)
        self.assertTrue(any(call["kind"] == "roi" for call in sampler.calls))
        self.assertEqual(list(self.cache.glob("*.json")), [])
        retried = self.analyzer(use_cache=True).analyze(self.source)
        self.assertEqual(retried.games[0].result, GameResult.LOSS)
        self.assertTrue(list(self.cache.glob("*.json")))

    def test_legacy_injected_protocol_remains_supported(self) -> None:
        sampler = LegacySampler()
        result = self.analyzer(sampler=sampler, detector=LegacyDetector()).analyze(self.source)
        self.assertEqual([event.type for event in result.events], [GameEventType.VS_SCREEN, GameEventType.DEFEAT])
        self.assertEqual(result.games[0].result, GameResult.LOSS)
        self.assertTrue(all(call["kind"] == "full" for call in sampler.delegate.calls))
        self.assertEqual(sampler.delegate.closed, sampler.delegate.calls)

    def test_result_fps_zero_explicitly_disables_dense_scan(self) -> None:
        sampler = DenseSampler()
        self.analyzer(sampler=sampler, settings=HearthstoneSettings(result_fps=0)).analyze(self.source)
        self.assertFalse(any(call["kind"] == "roi" for call in sampler.calls))

    def test_result_sampling_settings_are_in_cache_identity(self) -> None:
        first = self.analyzer(settings=HearthstoneSettings(result_fps=20), use_cache=True).analyze(self.source)
        different = self.analyzer(settings=HearthstoneSettings(result_fps=25), use_cache=True).analyze(self.source)
        self.assertNotEqual(first.metadata["cache_key"], different.metadata["cache_key"])
        sampler = DenseSampler()
        hit = self.analyzer(sampler=sampler, settings=HearthstoneSettings(result_fps=20), use_cache=True).analyze(self.source)
        self.assertTrue(hit.metadata["cache_hit"])
        self.assertEqual(sampler.calls, [])

    def test_effective_legacy_vs_dense_strategy_changes_cache_identity(self) -> None:
        legacy = self.analyzer(sampler=LegacySampler(), detector=DenseDetector(), use_cache=True).analyze(self.source)
        self.assertEqual(legacy.games[0].result, GameResult.UNKNOWN)
        sampler = DenseSampler()
        dense = self.analyzer(sampler=sampler, detector=DenseDetector(), use_cache=True).analyze(self.source)
        self.assertFalse(dense.metadata["cache_hit"])
        self.assertNotEqual(legacy.metadata["cache_key"], dense.metadata["cache_key"])
        self.assertNotEqual(legacy.settings["sampling_strategy"], dense.settings["sampling_strategy"])
        self.assertTrue(any(call["kind"] == "roi" for call in sampler.calls))
        self.assertEqual(dense.games[0].result, GameResult.LOSS)

    def test_single_global_seed_triggers_only_local_native_scan_and_rescues_second_frame(self) -> None:
        sampler = DenseSampler()
        detector = DenseDetector(result_times=(3.1, 3.1 + 1 / 60))
        result = self.analyzer(sampler=sampler, detector=detector).analyze(self.source)
        self.assertEqual(result.games[0].result, GameResult.LOSS)
        defeat = next(event for event in result.events if event.type == GameEventType.DEFEAT)
        self.assertEqual(defeat.metadata["observation_count"], 2)
        self.assertAlmostEqual(defeat.metadata["temporal_span_s"], 1 / 60)
        calls = [call for call in sampler.calls if call["kind"] == "roi"]
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["fps"], 20)
        self.assertEqual(calls[1]["fps"], 120)
        self.assertAlmostEqual(calls[1]["start_s"], 2.95)
        self.assertAlmostEqual(calls[1]["end_s"], 3.25)
        self.assertGreater(result.metadata["result_refine_frames"], 0)
        np.testing.assert_allclose(result.metadata["result_refine_windows"], [[2.95, 3.25]])
        self.assertEqual(sampler.closed, sampler.calls)

    def test_replayed_same_source_frame_during_refinement_is_not_second_support(self) -> None:
        detector = DenseDetector(result_times=(3.1,))
        result = self.analyzer(detector=detector).analyze(self.source)
        self.assertGreater(result.metadata["result_refine_frames"], 0)
        self.assertGreaterEqual(sum(abs(timestamp - 3.1) < 1e-6 for timestamp, _, _, _ in detector.roi_calls), 2)
        self.assertEqual(result.games[0].result, GameResult.UNKNOWN)
        rejected = [event for event in result.metadata["rejected_events"] if event["type"] == "DEFEAT"]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["metadata"]["observation_count"], 1)
        self.assertEqual(rejected[0]["metadata"]["temporal_span_s"], 0)

    def test_cancellation_during_native_refinement_closes_generator_without_cache(self) -> None:
        sampler = DenseSampler()
        token = AnalysisCancellation()
        calls = 0

        def cancel_after_global_scan() -> None:
            nonlocal calls
            calls += 1
            if calls > 80:
                token.cancel()

        detector = DenseDetector(result_times=(3.1,), on_roi=cancel_after_global_scan)
        with self.assertRaises(AnalysisCancelled):
            self.analyzer(sampler=sampler, detector=detector, use_cache=True).analyze(self.source, cancellation=token)
        roi_calls = [call for call in sampler.calls if call["kind"] == "roi"]
        self.assertEqual(len(roi_calls), 2)
        self.assertEqual(roi_calls[1]["fps"], 120)
        self.assertEqual(sampler.closed, sampler.calls)
        self.assertEqual(list(self.cache.glob("*.json")), [])
        retry = self.analyzer(use_cache=True).analyze(self.source)
        self.assertEqual(retry.games[0].result, GameResult.LOSS)

    def test_disabled_native_refinement_cannot_fabricate_missing_second_observation(self) -> None:
        settings = HearthstoneSettings(result_refine_fps=0)
        sampler = DenseSampler()
        result = self.analyzer(sampler=sampler, detector=DenseDetector(result_times=(3.1, 3.1 + 1 / 60)),
                               settings=settings).analyze(self.source)
        self.assertEqual(result.games[0].result, GameResult.UNKNOWN)
        self.assertEqual(len([call for call in sampler.calls if call["kind"] == "roi"]), 1)
        self.assertEqual(result.metadata["result_refine_frames"], 0)

    def test_native_debug_frame_can_revisit_time_before_later_global_class_event(self) -> None:
        detector = DenseDetector(result_times=(2.1, 2.1 + 1 / 60, 3.1, 3.15))
        with patch.object(HearthstoneAnalyzer, "_save_frame") as save:
            result = self.analyzer(detector=detector).analyze(
                self.source, debug_frames=self.root / "debug frames", max_debug_frames=20,
            )
        defeat_times = [call.args[2].timestamp for call in save.call_args_list
                        if call.args[2].type == GameEventType.DEFEAT]
        self.assertEqual(len(defeat_times), 3)
        np.testing.assert_allclose(defeat_times, [2.1, 3.1, 2.1])
        self.assertEqual(result.metadata["debug_frames_saved"], save.call_count)
        self.assertGreater(result.metadata["result_refine_frames"], 0)
        # Resetting the phase debounce must not reset the global file budget.
        with patch.object(HearthstoneAnalyzer, "_save_frame") as limited:
            limited_result = self.analyzer(detector=DenseDetector(
                result_times=(2.1, 2.1 + 1 / 60, 3.1, 3.15),
            )).analyze(self.source, debug_frames=self.root / "bounded debug", max_debug_frames=2)
        self.assertEqual(limited.call_count, 2)
        self.assertEqual(limited_result.metadata["debug_frames_saved"], 2)

    def test_invalid_dense_settings(self) -> None:
        for options in ({"result_fps": -1}, {"result_fps": float("nan")}, {"result_fps": float("inf")},
                        {"result_fps": True}, {"result_fps": 61}, {"minimum_result_observations": 1},
                        {"minimum_result_observations": True}, {"minimum_result_stability_s": 0},
                        {"minimum_result_stability_s": float("nan")}, {"result_refine_fps": -1},
                        {"result_refine_fps": float("inf")}, {"result_refine_fps": True},
                        {"result_refine_fps": 121}, {"result_refine_padding_s": 0},
                        {"result_refine_padding_s": float("nan")}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                HearthstoneSettings(**options)


if __name__ == "__main__":
    unittest.main()
