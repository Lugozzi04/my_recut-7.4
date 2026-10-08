"""Synthetic patches validate matching mechanics, not real-game performance."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from analysis.gameplay.models import DetectionSource, GameEventType
from analysis.gameplay.hearthstone.visual.detector import TemplateVisualDetector, normalize_frame
from analysis.gameplay.hearthstone.visual.templates import NormalizedROI, TemplateRepository, require_opencv


@unittest.skipUnless(importlib.util.find_spec("cv2") is not None, "Install requirements-gameplay.txt for real OpenCV tests")
class GameplayDetectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.path = self.root / "templates.json"
        self.cv = require_opencv()
        self.config = {
            "schema_version": 1, "profile": "hearthstone", "reference_size": [96, 64],
            "viewport": [0, 0, 1, 1], "ambiguity_margin": 0.04,
            "classes": {
                kind: {"roi": [0, 0, 1, 1], "threshold": 0.92, "scales": [1.0], "directory": kind.lower()}
                for kind in ("VS_SCREEN", "VICTORY", "DEFEAT")
            },
        }
        self.images: dict[str, np.ndarray] = {}
        for index, kind in enumerate(self.config["classes"]):
            (self.root / kind.lower()).mkdir()
            image = np.random.default_rng(100 + index).integers(0, 256, (12, 16), dtype=np.uint8)
            self.images[kind] = image
            self.save_image(kind, "anchor.png", image)
        self.save_config()

    def save_image(self, kind: str, name: str, image: np.ndarray) -> None:
        success, encoded = self.cv.imencode(".png", image)
        self.assertTrue(success)
        (self.root / kind.lower() / name).write_bytes(encoded.tobytes())

    def save_config(self) -> None:
        self.path.write_text(json.dumps(self.config), encoding="utf-8")

    def detector(self) -> TemplateVisualDetector:
        return TemplateVisualDetector(TemplateRepository(self.path).load())

    def frame(self, kind: str | None = None, *, image: np.ndarray | None = None, x: int = 40, y: int = 24) -> np.ndarray:
        frame = np.full((64, 96, 3), 30, dtype=np.uint8)
        if image is None and kind is not None:
            image = self.images[kind]
        if image is not None:
            height, width = image.shape
            frame[y:y + height, x:x + width] = image[:, :, None]
        return frame

    def test_each_anchor_emits_measured_event(self) -> None:
        detector = self.detector()
        for kind in self.images:
            with self.subTest(kind=kind):
                events = detector.detect(self.frame(kind), 12.3)
                self.assertEqual(len(events), 1)
                event = events[0]
                self.assertEqual(event.type, GameEventType(kind))
                self.assertEqual(event.source, DetectionSource.TEMPLATE_MATCHING)
                self.assertEqual(event.timestamp, 12.3)
                self.assertAlmostEqual(event.confidence, 1.0, places=5)
                self.assertEqual(event.confidence, event.metadata["raw_similarity"])
                self.assertGreater(event.metadata["class_margin"], 0.04)

    def test_blank_frame_has_no_event(self) -> None:
        self.assertEqual(self.detector().detect(self.frame(), 0), [])

    def test_unrelated_random_frame_has_no_event(self) -> None:
        noise = np.random.default_rng(99).integers(0, 256, (64, 96, 3), dtype=np.uint8)
        self.assertEqual(self.detector().detect(noise, 0), [])

    def test_identical_victory_and_defeat_templates_refuse_ambiguity(self) -> None:
        self.save_image("DEFEAT", "anchor.png", self.images["VICTORY"])
        self.assertEqual(self.detector().detect(self.frame("VICTORY"), 0), [])

    def test_zero_margin_still_rejects_exact_tie(self) -> None:
        self.config["ambiguity_margin"] = 0
        self.save_config()
        self.save_image("DEFEAT", "anchor.png", self.images["VICTORY"])
        self.assertEqual(self.detector().detect(self.frame("VICTORY"), 0), [])

    def test_multiple_templates_use_best_matching_patch(self) -> None:
        variant = np.random.default_rng(321).integers(0, 256, (12, 16), dtype=np.uint8)
        self.save_image("VICTORY", "variant.png", variant)
        event = self.detector().detect(self.frame(image=variant), 4)[0]
        self.assertEqual(event.type, GameEventType.VICTORY)
        self.assertTrue(event.metadata["template_id"].endswith("variant.png"))

    def test_class_roi_excludes_patch_outside_game_region(self) -> None:
        for item in self.config["classes"].values():
            item["roi"] = [0.25, 0.25, 0.5, 0.5]
        self.save_config()
        detector = self.detector()
        self.assertEqual(detector.detect(self.frame("DEFEAT", x=0, y=0), 0), [])
        self.assertEqual(detector.detect(self.frame("DEFEAT"), 1)[0].type, GameEventType.DEFEAT)

    def test_resolution_normalization_recovers_identical_patch(self) -> None:
        larger = np.repeat(np.repeat(self.frame("VS_SCREEN"), 2, axis=0), 2, axis=1)
        event = self.detector().detect(larger, 0)[0]
        self.assertEqual(event.type, GameEventType.VS_SCREEN)
        self.assertAlmostEqual(event.confidence, 1, places=5)

    def test_viewport_and_match_rectangle_are_normalized_to_full_video(self) -> None:
        self.config["viewport"] = [0.25, 0.25, 0.5, 0.5]
        self.save_config()
        full = np.zeros((128, 192, 3), dtype=np.uint8)
        full[32:96, 48:144] = self.frame("DEFEAT")
        event = self.detector().detect(full, 0)[0]
        rectangle = event.metadata["matched_rect_normalized"]
        self.assertAlmostEqual(rectangle[0], 0.25 + (40 / 96) * 0.5)
        self.assertAlmostEqual(rectangle[1], 0.25 + (24 / 64) * 0.5)
        self.assertAlmostEqual(rectangle[2], (16 / 96) * 0.5)

    def test_scale_variants_are_reused_for_matching(self) -> None:
        self.config["classes"]["VS_SCREEN"]["scales"] = [0.5, 1, 1.5]
        self.save_config()
        small = self.cv.resize(self.images["VS_SCREEN"], (8, 6), interpolation=self.cv.INTER_AREA)
        event = self.detector().detect(self.frame(image=small), 0)[0]
        self.assertEqual(event.type, GameEventType.VS_SCREEN)
        self.assertEqual(event.metadata["scale"], 0.5)

    def test_threshold_is_not_silently_lowered(self) -> None:
        self.config["classes"]["VICTORY"]["threshold"] = 1.0
        self.save_config()
        altered = self.images["VICTORY"].copy()
        altered[0, 0] ^= 255
        self.assertEqual(self.detector().detect(self.frame(image=altered), 0), [])

    def test_repeated_matching_is_deterministic(self) -> None:
        detector = self.detector()
        self.assertEqual(detector.detect(self.frame("VICTORY"), 1), detector.detect(self.frame("VICTORY"), 1))

    def test_invalid_frame_and_timestamp_are_rejected(self) -> None:
        detector = self.detector()
        for invalid in (np.zeros((3, 3), dtype=np.uint8), np.zeros((3, 3, 3), dtype=np.float32)):
            with self.assertRaises(ValueError):
                detector.detect(invalid, 0)
        for timestamp in (-1, float("nan"), float("inf"), True):
            with self.assertRaises(ValueError):
                detector.detect(self.frame(), timestamp)

    def test_description_records_method_versions_and_uncalibrated_thresholds(self) -> None:
        detector = self.detector()
        metadata = detector.describe()
        self.assertEqual(metadata["method"], "TM_CCOEFF_NORMED")
        self.assertEqual(metadata["opencv_version"], self.cv.__version__)
        self.assertEqual(metadata["pack_fingerprint"], detector.pack_fingerprint)
        self.assertIn("not a calibrated probability", metadata["confidence_meaning"])

    def test_capture_normalizer_does_not_require_existing_templates(self) -> None:
        image = self.frame("VS_SCREEN")
        result = normalize_frame(image, (96, 64), NormalizedROI.from_value([0, 0, 1, 1]))
        np.testing.assert_array_equal(image, result)

    def result_detector(self) -> TemplateVisualDetector:
        for kind in ("VICTORY", "DEFEAT"):
            self.config["classes"][kind]["roi"] = [0.25, 0.25, 0.5, 0.5]
        self.save_config()
        return self.detector()

    @staticmethod
    def crop_reference(detector: TemplateVisualDetector, frame: np.ndarray, roi: NormalizedROI) -> np.ndarray:
        left, top, right, bottom = roi.pixel_bounds(*detector.pack.reference_size)
        return detector.normalize_frame(frame)[top:bottom, left:right].copy()

    def test_dense_roi_matches_full_reference_without_resizing_crop(self) -> None:
        detector = self.result_detector()
        roi = detector.result_scan_roi()
        kinds = (GameEventType.VICTORY, GameEventType.DEFEAT)
        for kind in ("VICTORY", "DEFEAT"):
            with self.subTest(kind=kind):
                full = self.frame(kind)
                expected = detector.detect(full, 1.25, event_types=kinds)
                actual = detector.detect_roi(self.crop_reference(detector, full, roi), 1.25, roi=roi, event_types=kinds)
                self.assertEqual(actual, expected)
                self.assertEqual(actual[0].metadata["matched_rect_normalized"], [40 / 96, 24 / 64, 16 / 96, 12 / 64])

    def test_dense_roi_preserves_viewport_coordinate_transform(self) -> None:
        self.config["viewport"] = [0.25, 0.25, 0.5, 0.5]
        detector = self.result_detector()
        roi = detector.result_scan_roi()
        full = np.zeros((128, 192, 3), dtype=np.uint8)
        full[32:96, 48:144] = self.frame("DEFEAT")
        events = detector.detect_roi(
            self.crop_reference(detector, full, roi), 12.3, roi=roi,
            event_types=(GameEventType.VICTORY, GameEventType.DEFEAT),
        )
        self.assertEqual(events, detector.detect(full, 12.3, event_types=(GameEventType.VICTORY, GameEventType.DEFEAT)))
        rectangle = events[0].metadata["matched_rect_normalized"]
        self.assertAlmostEqual(rectangle[0], 0.25 + 40 / 96 * 0.5)
        self.assertAlmostEqual(rectangle[1], 0.25 + 24 / 64 * 0.5)
        self.assertAlmostEqual(rectangle[2], 16 / 96 * 0.5)
        self.assertAlmostEqual(rectangle[3], 12 / 64 * 0.5)

    def test_result_union_covers_different_class_rois_and_keeps_search_boundaries(self) -> None:
        self.config["classes"]["VICTORY"]["roi"] = [0.25, 0.25, 0.25, 0.5]
        self.config["classes"]["DEFEAT"]["roi"] = [0.5, 0.25, 0.25, 0.5]
        self.save_config()
        detector = self.detector()
        roi = detector.result_scan_roi()
        self.assertEqual(roi.to_list(), [0.25, 0.25, 0.5, 0.5])
        for kind, x in (("VICTORY", 28), ("DEFEAT", 52)):
            with self.subTest(kind=kind):
                frame = self.frame(kind, x=x)
                found = detector.detect_roi(
                    self.crop_reference(detector, frame, roi), 1,
                    roi=roi, event_types=(GameEventType.VICTORY, GameEventType.DEFEAT),
                )
                self.assertEqual(found[0].type, GameEventType(kind))
                self.assertAlmostEqual(found[0].metadata["matched_rect_normalized"][0], x / 96)
        outside = self.frame("VICTORY", x=60)
        self.assertEqual(detector.detect_roi(
            self.crop_reference(detector, outside, roi), 1, roi=roi,
            event_types=(GameEventType.VICTORY, GameEventType.DEFEAT),
        ), [])

    def test_dense_roi_rejects_full_frame_wrong_dtype_and_wrong_shape(self) -> None:
        detector = self.result_detector()
        roi = detector.result_scan_roi()
        valid = self.crop_reference(detector, self.frame("DEFEAT"), roi)
        for frame in (self.frame("DEFEAT"), valid.astype(np.float32), valid[:, :, 0], valid[:-1], np.zeros((0, 0, 3), dtype=np.uint8)):
            with self.subTest(shape=frame.shape, dtype=frame.dtype), self.assertRaisesRegex(ValueError, "reference-geometry"):
                detector.detect_roi(frame, 0, roi=roi, event_types=(GameEventType.VICTORY, GameEventType.DEFEAT))

    def test_dense_roi_must_contain_each_requested_class_search_region(self) -> None:
        detector = self.result_detector()
        roi = NormalizedROI.from_value([0.25, 0.25, 0.25, 0.5])
        crop = self.crop_reference(detector, self.frame("DEFEAT"), roi)
        with self.assertRaisesRegex(ValueError, "entire configured"):
            detector.detect_roi(crop, 0, roi=roi, event_types=(GameEventType.VICTORY, GameEventType.DEFEAT))

    def test_selected_class_scan_does_not_match_or_compete_with_other_classes(self) -> None:
        self.save_image("VICTORY", "anchor.png", self.images["VS_SCREEN"])
        detector = self.detector()
        full = self.frame("VS_SCREEN")
        self.assertEqual(detector.detect(full, 0), [])
        detected = detector.detect(full, 0, event_types=(GameEventType.VS_SCREEN,))
        self.assertEqual(detected[0].type, GameEventType.VS_SCREEN)
        self.assertEqual(set(detected[0].metadata["class_scores"]), {"VS_SCREEN"})

    def test_score_roi_reports_below_threshold_without_emitting_event(self) -> None:
        self.config["classes"]["VICTORY"]["threshold"] = 1.0
        detector = self.result_detector()
        altered = self.images["VICTORY"].copy()
        altered[0, 0] ^= 255
        frame = self.frame(image=altered)
        roi = detector.result_scan_roi()
        crop = self.crop_reference(detector, frame, roi)
        kinds = (GameEventType.VICTORY, GameEventType.DEFEAT)
        scores = detector.score_roi(crop, 42, roi=roi, event_types=kinds)
        self.assertFalse(scores["accepted"])
        self.assertIsNone(scores["accepted_type"])
        self.assertEqual(scores["best_type"], "VICTORY")
        self.assertLess(scores["classes"]["VICTORY"]["similarity"], 1)
        self.assertGreater(scores["classes"]["VICTORY"]["similarity"], 0.9)
        self.assertEqual(scores["classes"]["VICTORY"]["threshold"], 1)
        self.assertFalse(scores["classes"]["VICTORY"]["passes_threshold"])
        self.assertEqual(detector.detect_roi(crop, 42, roi=roi, event_types=kinds), [])
        self.assertEqual(scores, detector.score(frame, 42, event_types=kinds))

    def test_score_roi_identifies_ambiguity_even_when_both_thresholds_pass(self) -> None:
        self.save_image("DEFEAT", "anchor.png", self.images["VICTORY"])
        detector = self.result_detector()
        roi = detector.result_scan_roi()
        crop = self.crop_reference(detector, self.frame("VICTORY"), roi)
        scores = detector.score_roi(crop, 0, roi=roi, event_types=(GameEventType.VICTORY, GameEventType.DEFEAT))
        self.assertFalse(scores["accepted"])
        for data in scores["classes"].values():
            self.assertTrue(data["passes_threshold"])
            self.assertAlmostEqual(data["margin"], 0)
            self.assertAlmostEqual(data["similarity"], 1, places=5)
        self.assertEqual(detector.detect_roi(crop, 0, roi=roi, event_types=(GameEventType.VICTORY, GameEventType.DEFEAT)), [])

    def test_roi_matching_validates_time_and_event_types(self) -> None:
        detector = self.result_detector()
        roi = detector.result_scan_roi()
        crop = self.crop_reference(detector, self.frame("DEFEAT"), roi)
        for timestamp in (-1, float("nan"), float("inf"), True):
            with self.subTest(timestamp=timestamp), self.assertRaises(ValueError):
                detector.score_roi(crop, timestamp, roi=roi, event_types=(GameEventType.DEFEAT,))
        for kinds in ((), ("DEFEAT",), (GameEventType.DEFEAT, GameEventType.DEFEAT)):
            with self.subTest(kinds=kinds), self.assertRaises(ValueError):
                detector.detect_roi(crop, 0, roi=roi, event_types=kinds)
