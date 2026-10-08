"""Synthetic image tests exercise mechanics, not Hearthstone accuracy."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from analysis.gameplay.hearthstone.visual.templates import (
    GameplayVisionDependencyError, NormalizedROI, TemplatePackError, TemplateRepository, require_opencv,
)


class GameplayTemplateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / "template con accenti è 🎮"
        self.root.mkdir()
        self.path = self.root / "templates.json"
        self.config = {
            "schema_version": 1, "profile": "hearthstone", "reference_size": [96, 64],
            "viewport": [0, 0, 1, 1], "ambiguity_margin": 0.04,
            "classes": {
                kind: {"roi": [0, 0, 1, 1], "threshold": 0.92, "scales": [1.0], "directory": kind.lower()}
                for kind in ("VS_SCREEN", "VICTORY", "DEFEAT")
            },
        }
        for kind in self.config["classes"]:
            (self.root / kind.lower()).mkdir()
        self.save_config()

    def save_config(self) -> None:
        self.path.write_text(json.dumps(self.config), encoding="utf-8")

    def populate(self) -> None:
        if importlib.util.find_spec("cv2") is None:
            self.skipTest("Optional OpenCV gameplay dependency unavailable")
        cv = require_opencv()
        for index, kind in enumerate(self.config["classes"]):
            image = np.random.default_rng(index + 100).integers(0, 256, (12, 16), dtype=np.uint8)
            success, encoded = cv.imencode(".png", image)
            self.assertTrue(success)
            (self.root / kind.lower() / "ancora è 🎮.png").write_bytes(encoded.tobytes())

    def test_empty_pack_reports_real_templates_missing(self) -> None:
        with self.assertRaisesRegex(TemplatePackError, "No real VS_SCREEN templates"):
            TemplateRepository(self.path).load()

    def test_default_resource_pack_declares_all_three_anchor_classes(self) -> None:
        from utils.runtime_paths import resource_path
        manifest = resource_path("resources", "gameplay", "hearthstone", "templates.json")
        config = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(set(config["classes"]), {"VS_SCREEN", "VICTORY", "DEFEAT"})
        self.assertEqual(config["reference_size"], [960, 540])

    def test_boolean_schema_version_is_rejected_before_cv_import(self) -> None:
        self.config["schema_version"] = True
        self.save_config()
        with patch("analysis.gameplay.hearthstone.visual.templates.require_opencv") as dependency:
            with self.assertRaisesRegex(TemplatePackError, "schema_version=1"):
                TemplateRepository(self.path).load()
            dependency.assert_not_called()

    def test_all_three_classes_required(self) -> None:
        del self.config["classes"]["DEFEAT"]
        self.save_config()
        with self.assertRaisesRegex(TemplatePackError, "must define"):
            TemplateRepository(self.path).load()

    def test_invalid_roi_rejected_before_decode(self) -> None:
        self.config["classes"]["VS_SCREEN"]["roi"] = [0.8, 0, 0.3, 1]
        self.save_config()
        with self.assertRaisesRegex(TemplatePackError, "normalized bounds"):
            TemplateRepository(self.path).load()

    def test_parent_traversal_rejected(self) -> None:
        self.config["classes"]["VS_SCREEN"]["directory"] = "../foreign"
        self.save_config()
        with self.assertRaisesRegex(TemplatePackError, "parent traversal"):
            TemplateRepository(self.path).load()

    def test_windows_drive_prefix_rejected_even_off_windows(self) -> None:
        self.config["classes"]["VS_SCREEN"]["directory"] = "C:/foreign"
        self.save_config()
        with self.assertRaisesRegex(TemplatePackError, "relative"):
            TemplateRepository(self.path).load()

    def test_nonfinite_threshold_rejected(self) -> None:
        self.config["classes"]["VS_SCREEN"]["threshold"] = float("nan")
        self.save_config()
        with self.assertRaisesRegex(TemplatePackError, "finite"):
            TemplateRepository(self.path).load()

    def test_duplicate_scales_rejected(self) -> None:
        self.config["classes"]["VS_SCREEN"]["scales"] = [1, 1]
        self.save_config()
        with self.assertRaisesRegex(TemplatePackError, "unique"):
            TemplateRepository(self.path).load()

    def test_unicode_multiple_templates_and_readonly_pixels(self) -> None:
        self.populate()
        original = self.root / "victory" / "ancora è 🎮.png"
        original.with_name("second.png").write_bytes(original.read_bytes())
        pack = TemplateRepository(self.path).load()
        victory = next(item for item in pack.classes if item.event_type.value == "VICTORY")
        self.assertEqual(len(victory.templates), 2)
        self.assertFalse(victory.templates[0].image.flags.writeable)
        self.assertEqual(pack.reference_size, (96, 64))
        self.assertEqual(pack.describe()["threshold_calibration"], "uncalibrated")

    def test_template_content_changes_fingerprint(self) -> None:
        self.populate()
        first = TemplateRepository(self.path).load().fingerprint
        source = self.root / "vs_screen" / "ancora è 🎮.png"
        image = require_opencv().imdecode(np.frombuffer(source.read_bytes(), dtype=np.uint8), 0)
        image[0, 0] ^= 255
        source.write_bytes(require_opencv().imencode(".png", image)[1].tobytes())
        self.assertNotEqual(first, TemplateRepository(self.path).load().fingerprint)

    def test_configuration_changes_fingerprint(self) -> None:
        self.populate()
        first = TemplateRepository(self.path).load().fingerprint
        self.config["ambiguity_margin"] = 0.1
        self.save_config()
        self.assertNotEqual(first, TemplateRepository(self.path).load().fingerprint)

    def test_invalid_image_rejected(self) -> None:
        self.populate()
        (self.root / "defeat" / "broken.png").write_bytes(b"not an image")
        with self.assertRaisesRegex(TemplatePackError, "Cannot decode"):
            TemplateRepository(self.path).load()

    def test_constant_template_rejected(self) -> None:
        self.populate()
        encoded = require_opencv().imencode(".png", np.full((12, 16), 127, dtype=np.uint8))[1]
        (self.root / "defeat" / "constant.png").write_bytes(encoded.tobytes())
        with self.assertRaisesRegex(TemplatePackError, "lacks visual variation"):
            TemplateRepository(self.path).load()

    def test_template_must_fit_roi(self) -> None:
        self.populate()
        self.config["classes"]["VS_SCREEN"]["roi"] = [0, 0, 0.05, 0.05]
        self.save_config()
        with self.assertRaisesRegex(TemplatePackError, "does not fit"):
            TemplateRepository(self.path).load()

    def test_optional_dependency_error_is_actionable(self) -> None:
        with patch("analysis.gameplay.hearthstone.visual.templates.importlib.import_module", side_effect=ImportError):
            with self.assertRaisesRegex(GameplayVisionDependencyError, "requirements-gameplay.txt"):
                require_opencv()

    def test_normalized_roi_rounding_stays_inside_image(self) -> None:
        roi = NormalizedROI.from_value([0.999, 0.999, 0.001, 0.001])
        self.assertEqual(roi.pixel_bounds(10, 10), (9, 9, 10, 10))
