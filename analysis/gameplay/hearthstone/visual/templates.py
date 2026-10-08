"""Versioned, read-only template packs for the offline Hearthstone detector.

A pack contains measured image patches, not a classifier or calibrated probabilities.
Images are decoded through bytes so Windows Unicode paths work with OpenCV too.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib
import json
import math
from pathlib import Path, PureWindowsPath
from typing import Any

import numpy as np

from analysis.gameplay.models import GameEventType


class TemplatePackError(ValueError):
    """A template pack is missing, unsafe, or cannot support detection."""


class GameplayVisionDependencyError(RuntimeError):
    """The optional OpenCV gameplay dependency is unavailable."""


def require_opencv() -> Any:
    """Import OpenCV only when visual analysis is requested."""
    try:
        return importlib.import_module("cv2")
    except (ImportError, OSError) as exc:
        raise GameplayVisionDependencyError(
            "Gameplay analysis requires OpenCV. Install requirements-gameplay.txt with the selected Python interpreter."
        ) from exc


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise TemplatePackError(f"{name} must be a finite number")
    return float(value)


@dataclass(frozen=True)
class NormalizedROI:
    """Coordinates are x, y, width, height within their parent image."""

    x: float
    y: float
    width: float
    height: float

    @classmethod
    def from_value(cls, value: Any, *, name: str = "roi") -> NormalizedROI:
        if not isinstance(value, (list, tuple)) or len(value) != 4:
            raise TemplatePackError(f"{name} must contain [x, y, width, height]")
        x, y, width, height = (_number(item, name) for item in value)
        if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 1.000000001 or y + height > 1.000000001:
            raise TemplatePackError(f"{name} must be a nonempty rectangle inside normalized bounds [0, 1]")
        return cls(x, y, width, height)

    def to_list(self) -> list[float]:
        return [self.x, self.y, self.width, self.height]

    def pixel_bounds(self, width: int, height: int) -> tuple[int, int, int, int]:
        """Return inclusive-start/exclusive-end bounds without an empty rounding crop."""
        left = max(0, min(width - 1, math.floor(self.x * width)))
        top = max(0, min(height - 1, math.floor(self.y * height)))
        right = min(width, max(left + 1, math.ceil((self.x + self.width) * width)))
        bottom = min(height, max(top + 1, math.ceil((self.y + self.height) * height)))
        return left, top, right, bottom


@dataclass(frozen=True)
class TemplateImage:
    id: str
    relative_path: str
    image: np.ndarray
    sha256: str


@dataclass(frozen=True)
class TemplateClass:
    event_type: GameEventType
    roi: NormalizedROI
    threshold: float
    scales: tuple[float, ...]
    directory: str
    templates: tuple[TemplateImage, ...]


@dataclass(frozen=True)
class TemplatePack:
    path: Path
    reference_size: tuple[int, int]
    viewport: NormalizedROI
    ambiguity_margin: float
    classes: tuple[TemplateClass, ...]
    fingerprint: str
    config: dict[str, Any]

    def describe(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "profile": "hearthstone",
            "reference_size": list(self.reference_size),
            "viewport": self.viewport.to_list(),
            "ambiguity_margin": self.ambiguity_margin,
            "threshold_calibration": "uncalibrated",
            "pack_fingerprint": self.fingerprint,
            "classes": {
                item.event_type.value: {
                    "roi": item.roi.to_list(),
                    "threshold": item.threshold,
                    "scales": list(item.scales),
                    "directory": item.directory,
                    "templates": [
                        {"id": template.id, "path": template.relative_path, "sha256": template.sha256}
                        for template in item.templates
                    ],
                }
                for item in self.classes
            },
        }


class TemplateRepository:
    """Load all three anchor classes from a JSON pack and class directories."""

    _extensions = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
    _max_image_bytes = 16 * 1024 * 1024
    _max_templates = 512

    def __init__(self, pack_path: str | Path) -> None:
        self.path = Path(pack_path).expanduser().resolve()

    def resolve_directory(self, relative: str) -> Path:
        """Resolve a safe pack-relative directory, including for first-time capture."""
        portable = PureWindowsPath(relative)
        if not relative or Path(relative).is_absolute() or portable.is_absolute() or portable.drive:
            raise TemplatePackError("Template directories must be relative to the template pack")
        if ".." in portable.parts or ".." in Path(relative).parts:
            raise TemplatePackError("Template directories cannot contain parent traversal")
        result = (self.path.parent / relative).resolve()
        try:
            result.relative_to(self.path.parent)
        except ValueError as exc:
            raise TemplatePackError("Template path escapes the template pack") from exc
        return result

    def load(self) -> TemplatePack:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise TemplatePackError(f"Cannot load template pack {self.path}: {exc}") from exc
        if (not isinstance(raw, dict) or type(raw.get("schema_version")) is not int
                or raw.get("schema_version") != 1 or raw.get("profile") != "hearthstone"):
            raise TemplatePackError("Expected Hearthstone template pack schema_version=1")
        size = raw.get("reference_size", [960, 540])
        if (
            not isinstance(size, list) or len(size) != 2
            or any(isinstance(value, bool) or not isinstance(value, int) or not 16 <= value <= 4096 for value in size)
        ):
            raise TemplatePackError("reference_size must contain integer width and height between 16 and 4096")
        reference_size = (size[0], size[1])
        viewport = NormalizedROI.from_value(raw.get("viewport", [0, 0, 1, 1]), name="viewport")
        margin = _number(raw.get("ambiguity_margin", 0.04), "ambiguity_margin")
        if not 0 <= margin <= 1:
            raise TemplatePackError("ambiguity_margin must be between 0 and 1")
        raw_classes = raw.get("classes")
        expected = (GameEventType.VS_SCREEN, GameEventType.VICTORY, GameEventType.DEFEAT)
        if not isinstance(raw_classes, dict) or set(raw_classes) != {kind.value for kind in expected}:
            raise TemplatePackError("Template pack must define VS_SCREEN, VICTORY, and DEFEAT classes")
        parsed: list[tuple[GameEventType, NormalizedROI, float, tuple[float, ...], str, list[Path]]] = []
        for kind in expected:
            item = raw_classes[kind.value]
            if not isinstance(item, dict):
                raise TemplatePackError(f"Invalid configuration for {kind.value}")
            roi = NormalizedROI.from_value(item.get("roi", [0, 0, 1, 1]), name=f"{kind.value}.roi")
            threshold = _number(item.get("threshold", 0.92), f"{kind.value}.threshold")
            if not 0 < threshold <= 1:
                raise TemplatePackError(f"{kind.value}.threshold must be greater than 0 and at most 1")
            raw_scales = item.get("scales", [0.9, 1.0, 1.1])
            if not isinstance(raw_scales, list) or not 1 <= len(raw_scales) <= 32:
                raise TemplatePackError(f"{kind.value}.scales must contain 1 to 32 scale factors")
            scales = tuple(_number(value, f"{kind.value}.scales") for value in raw_scales)
            if any(not 0.1 <= value <= 4 for value in scales) or len(set(scales)) != len(scales):
                raise TemplatePackError(f"{kind.value}.scales must be unique values between 0.1 and 4")
            relative = item.get("directory")
            if not isinstance(relative, str):
                raise TemplatePackError(f"{kind.value}.directory must name a relative template directory")
            directory = self.resolve_directory(relative)
            try:
                files = sorted(
                    (entry for entry in directory.iterdir() if entry.is_file() and entry.suffix.lower() in self._extensions),
                    key=lambda entry: entry.name,
                )
            except OSError as exc:
                raise TemplatePackError(f"Template directory missing for {kind.value}: {directory}") from exc
            if not files:
                raise TemplatePackError(
                    f"No real {kind.value} templates found in {directory}. "
                    "Capture anchor patches from a representative VOD before analysis; see docs/GAMEPLAY.md."
                )
            if len(files) > self._max_templates:
                raise TemplatePackError(f"Too many {kind.value} templates (maximum {self._max_templates})")
            parsed.append((kind, roi, threshold, scales, relative, files))

        cv = require_opencv()
        classes: list[TemplateClass] = []
        digest = hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        for kind, roi, threshold, scales, relative, files in parsed:
            templates: list[TemplateImage] = []
            left, top, right, bottom = roi.pixel_bounds(*reference_size)
            for file in files:
                resolved = file.resolve()
                try:
                    relative_file = resolved.relative_to(self.path.parent).as_posix()
                except ValueError as exc:
                    raise TemplatePackError("Template image escapes the template pack") from exc
                try:
                    if resolved.stat().st_size > self._max_image_bytes:
                        raise TemplatePackError(f"Template image is too large: {relative_file}")
                    data = resolved.read_bytes()
                except OSError as exc:
                    raise TemplatePackError(f"Cannot read template image {relative_file}") from exc
                if not data:
                    raise TemplatePackError(f"Template image is empty: {relative_file}")
                image = cv.imdecode(np.frombuffer(data, dtype=np.uint8), cv.IMREAD_GRAYSCALE)
                if image is None or image.ndim != 2 or image.size == 0:
                    raise TemplatePackError(f"Cannot decode template image: {relative_file}")
                if max(image.shape) > 4096 or image.size > 4096 * 4096:
                    raise TemplatePackError(f"Template image dimensions are too large: {relative_file}")
                if min(image.shape) < 2 or float(np.std(image)) < 1.0:
                    raise TemplatePackError(f"Template image lacks visual variation for normalized correlation: {relative_file}")
                if not any(
                    2 <= round(image.shape[1] * scale) <= right - left
                    and 2 <= round(image.shape[0] * scale) <= bottom - top
                    for scale in scales
                ):
                    raise TemplatePackError(f"Template does not fit its configured ROI at any scale: {relative_file}")
                image.setflags(write=False)
                image_hash = hashlib.sha256(data).hexdigest()
                digest.update(relative_file.encode("utf-8"))
                digest.update(b"\0")
                digest.update(data)
                templates.append(TemplateImage(relative_file, relative_file, image, image_hash))
            classes.append(TemplateClass(kind, roi, threshold, scales, relative, tuple(templates)))
        return TemplatePack(self.path, reference_size, viewport, margin, tuple(classes), digest.hexdigest(), raw)
