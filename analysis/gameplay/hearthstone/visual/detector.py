"""Deterministic OpenCV anchor matching with explicit ambiguity rejection."""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Protocol

import numpy as np

from analysis.gameplay.models import DetectionSource, GameEvent, GameEventType
from analysis.gameplay.hearthstone.visual.templates import NormalizedROI, TemplatePack, TemplatePackError, require_opencv


def normalize_frame(
    frame: np.ndarray, reference_size: tuple[int, int], viewport: NormalizedROI,
) -> np.ndarray:
    """Apply capture/detection geometry without requiring an existing template pack."""
    if not isinstance(frame, np.ndarray) or frame.dtype != np.uint8 or frame.ndim != 3 or frame.shape[2] != 3:
        raise ValueError("Visual detection requires a BGR uint8 frame with shape (height, width, 3)")
    if frame.shape[0] == 0 or frame.shape[1] == 0:
        raise ValueError("Visual detection cannot process an empty frame")
    cv = require_opencv()
    left, top, right, bottom = viewport.pixel_bounds(frame.shape[1], frame.shape[0])
    image = frame[top:bottom, left:right]
    if (image.shape[1], image.shape[0]) == reference_size:
        return image
    ref_width, ref_height = reference_size
    interpolation = cv.INTER_AREA if ref_width < image.shape[1] or ref_height < image.shape[0] else cv.INTER_LINEAR
    return cv.resize(image, (ref_width, ref_height), interpolation=interpolation)


class VisualEventDetector(Protocol):
    """A future detector can replace matching without changing game assembly."""

    detector_version: str
    pack_fingerprint: str

    def detect(self, frame: np.ndarray, timestamp: float) -> list[GameEvent]: ...

    def describe(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class _PreparedTemplate:
    template_id: str
    scale: float
    image: np.ndarray


@dataclass(frozen=True)
class _ClassMatch:
    event_type: GameEventType
    similarity: float
    threshold: float
    template_id: str
    scale: float
    rectangle: tuple[int, int, int, int]


class TemplateVisualDetector:
    """Choose the best class only if its raw correlation and margin suffice.

    Confidence is TM_CCOEFF_NORMED similarity, not a probability of correctness.
    Templates and scale variants are prepared once, then reused across frames.
    """

    detector_version = "hearthstone-template-v2"

    def __init__(self, pack: TemplatePack) -> None:
        self.pack = pack
        self.pack_fingerprint = pack.fingerprint
        self._cv = require_opencv()
        self._prepared: dict[GameEventType, tuple[_PreparedTemplate, ...]] = {}
        for config in pack.classes:
            left, top, right, bottom = config.roi.pixel_bounds(*pack.reference_size)
            prepared: list[_PreparedTemplate] = []
            for template in config.templates:
                seen_sizes: set[tuple[int, int]] = set()
                for scale in config.scales:
                    size = (round(template.image.shape[1] * scale), round(template.image.shape[0] * scale))
                    if size in seen_sizes or size[0] < 2 or size[1] < 2 or size[0] > right - left or size[1] > bottom - top:
                        continue
                    seen_sizes.add(size)
                    if size == (template.image.shape[1], template.image.shape[0]):
                        image = template.image
                    else:
                        interpolation = self._cv.INTER_AREA if scale < 1 else self._cv.INTER_LINEAR
                        image = self._cv.resize(template.image, size, interpolation=interpolation)
                    if float(np.std(image)) < 1.0:
                        continue
                    image.setflags(write=False)
                    prepared.append(_PreparedTemplate(template.id, scale, image))
            if not prepared:
                raise TemplatePackError(f"No usable template scale variants for {config.event_type.value}")
            self._prepared[config.event_type] = tuple(prepared)

    def describe(self) -> dict[str, Any]:
        return {
            "detector_version": self.detector_version,
            "opencv_version": str(self._cv.__version__),
            "method": "TM_CCOEFF_NORMED",
            "color_space": "grayscale",
            "confidence_meaning": "raw normalized template correlation; not a calibrated probability",
            **self.pack.describe(),
        }

    def normalize_frame(self, frame: np.ndarray) -> np.ndarray:
        """Crop the configured gameplay viewport and resize to the patch reference.

        Frame input and return value are BGR uint8. Use this same method when
        capturing patches so templates and detection share identical geometry.
        """
        return normalize_frame(frame, self.pack.reference_size, self.pack.viewport)

    def _event_types(self, event_types: tuple[GameEventType, ...] | None) -> tuple[GameEventType, ...]:
        selected = tuple(self._prepared) if event_types is None else event_types
        if not isinstance(selected, tuple) or not selected or any(
            not isinstance(kind, GameEventType) or kind not in self._prepared for kind in selected
        ) or len(set(selected)) != len(selected):
            raise ValueError("event_types must be a nonempty tuple of distinct configured GameEventType values")
        return selected

    @staticmethod
    def _timestamp(timestamp: float) -> float:
        if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp) or timestamp < 0:
            raise ValueError("Frame timestamp must be finite nonnegative seconds")
        return float(timestamp)

    def result_scan_roi(self) -> NormalizedROI:
        """Return the result-class search union within the normalized viewport.

        Convert this viewport-relative rectangle through pack.viewport when
        cropping the source video. The sampler must return exactly the dimensions
        given by roi.pixel_bounds(*pack.reference_size). This preserves template
        scale without transmitting or matching complete high-rate video frames.
        """
        regions = [
            item.roi for item in self.pack.classes
            if item.event_type in (GameEventType.VICTORY, GameEventType.DEFEAT)
        ]
        if len(regions) != 2:
            raise TemplatePackError("Dense result scanning requires VICTORY and DEFEAT classes")
        left = min(item.x for item in regions)
        top = min(item.y for item in regions)
        right = max(item.x + item.width for item in regions)
        bottom = max(item.y + item.height for item in regions)
        return NormalizedROI.from_value([left, top, right - left, bottom - top])

    def _matches_reference(
        self, gray: np.ndarray, bounds: tuple[int, int, int, int],
        event_types: tuple[GameEventType, ...],
    ) -> list[_ClassMatch]:
        """Match one full reference image or a reference-sized crop of it."""
        crop_left, crop_top, crop_right, crop_bottom = bounds
        matches: list[_ClassMatch] = []
        for config in self.pack.classes:
            if config.event_type not in event_types:
                continue
            left, top, right, bottom = config.roi.pixel_bounds(*self.pack.reference_size)
            if left < crop_left or top < crop_top or right > crop_right or bottom > crop_bottom:
                raise ValueError(f"Scan ROI must contain the entire configured {config.event_type.value} search ROI")
            image = np.ascontiguousarray(gray[top - crop_top:bottom - crop_top, left - crop_left:right - crop_left])
            best: _ClassMatch | None = None
            for template in self._prepared[config.event_type]:
                result = self._cv.matchTemplate(image, template.image, self._cv.TM_CCOEFF_NORMED)
                _minimum, maximum, _minimum_location, location = self._cv.minMaxLoc(result)
                score = float(maximum)
                if not math.isfinite(score):
                    continue
                # OpenCV may return a tiny floating-point overshoot around 1.
                score = max(-1.0, min(1.0, score))
                if best is None or score > best.similarity:
                    height, width = template.image.shape
                    best = _ClassMatch(
                        config.event_type, score, config.threshold, template.template_id, template.scale,
                        (left + location[0], top + location[1], width, height),
                    )
            if best is not None:
                matches.append(best)
        return sorted(matches, key=lambda item: item.similarity, reverse=True)

    def _full_matches(
        self, frame: np.ndarray, event_types: tuple[GameEventType, ...] | None,
    ) -> list[_ClassMatch]:
        selected = self._event_types(event_types)
        gray = self._cv.cvtColor(self.normalize_frame(frame), self._cv.COLOR_BGR2GRAY)
        width, height = self.pack.reference_size
        return self._matches_reference(gray, (0, 0, width, height), selected)

    def _roi_matches(
        self, frame: np.ndarray, roi: NormalizedROI, event_types: tuple[GameEventType, ...],
    ) -> list[_ClassMatch]:
        selected = self._event_types(event_types)
        if not isinstance(roi, NormalizedROI):
            raise ValueError("Scan ROI must be a NormalizedROI within the configured viewport")
        checked = NormalizedROI.from_value(roi.to_list())
        bounds = checked.pixel_bounds(*self.pack.reference_size)
        left, top, right, bottom = bounds
        if (not isinstance(frame, np.ndarray) or frame.dtype != np.uint8
                or frame.shape != (bottom - top, right - left, 3)):
            raise ValueError(
                "Dense ROI input must be a BGR uint8 reference-geometry crop with shape "
                f"({bottom - top}, {right - left}, 3); do not pass a full video frame"
            )
        gray = self._cv.cvtColor(frame, self._cv.COLOR_BGR2GRAY)
        return self._matches_reference(gray, bounds, selected)

    def _normalized_rectangle(self, match: _ClassMatch) -> list[float]:
        ref_width, ref_height = self.pack.reference_size
        x, y, width, height = match.rectangle
        viewport = self.pack.viewport
        return [
            viewport.x + (x / ref_width) * viewport.width,
            viewport.y + (y / ref_height) * viewport.height,
            (width / ref_width) * viewport.width,
            (height / ref_height) * viewport.height,
        ]

    def _scores(self, matches: list[_ClassMatch], timestamp: float) -> dict[str, Any]:
        """Expose measured scores even when a threshold or ambiguity rejects them."""
        classes: dict[str, Any] = {}
        for match in matches:
            second = max((item.similarity for item in matches if item.event_type != match.event_type), default=-1.0)
            margin = match.similarity - second
            classes[match.event_type.value] = {
                "similarity": match.similarity,
                "threshold": match.threshold,
                "margin": margin,
                "template_id": match.template_id,
                "scale": match.scale,
                "matched_rect_normalized": self._normalized_rectangle(match),
                "passes_threshold": match.similarity >= match.threshold,
            }
        best = matches[0] if matches else None
        best_scores = classes[best.event_type.value] if best is not None else None
        accepted = bool(
            best_scores is not None and best_scores["passes_threshold"]
            and best_scores["margin"] >= self.pack.ambiguity_margin and best_scores["margin"] > 0
        )
        return {
            "timestamp": timestamp,
            "classes": classes,
            "best_type": best.event_type.value if best is not None else None,
            "accepted_type": best.event_type.value if best is not None and accepted else None,
            "accepted": accepted,
            "ambiguity_margin": self.pack.ambiguity_margin,
            "detector_version": self.detector_version,
        }

    def _events(self, matches: list[_ClassMatch], timestamp: float) -> list[GameEvent]:
        scores = self._scores(matches, timestamp)
        if not scores["accepted"]:
            return []
        best = matches[0]
        second = matches[1].similarity if len(matches) > 1 else -1.0
        return [GameEvent(
            timestamp=timestamp,
            type=best.event_type,
            confidence=best.similarity,
            source=DetectionSource.TEMPLATE_MATCHING,
            metadata={
                "template_id": best.template_id,
                "similarity_method": "TM_CCOEFF_NORMED",
                "raw_similarity": best.similarity,
                "second_best_similarity": second,
                "class_margin": best.similarity - second,
                "class_scores": {item.event_type.value: item.similarity for item in matches},
                "threshold": best.threshold,
                "scale": best.scale,
                "matched_rect_normalized": self._normalized_rectangle(best),
                "detector_version": self.detector_version,
            },
        )]

    def detect(
        self, frame: np.ndarray, timestamp: float, *, event_types: tuple[GameEventType, ...] | None = None,
    ) -> list[GameEvent]:
        """Match a full video frame, optionally examining only selected anchors."""
        checked_timestamp = self._timestamp(timestamp)
        return self._events(self._full_matches(frame, event_types), checked_timestamp)

    def detect_roi(
        self, frame: np.ndarray, timestamp: float, *, roi: NormalizedROI,
        event_types: tuple[GameEventType, ...],
    ) -> list[GameEvent]:
        """Match a pre-cropped viewport-relative ROI without normalizing it again."""
        checked_timestamp = self._timestamp(timestamp)
        return self._events(self._roi_matches(frame, roi, event_types), checked_timestamp)

    def score(
        self, frame: np.ndarray, timestamp: float, *, event_types: tuple[GameEventType, ...] | None = None,
    ) -> dict[str, Any]:
        """Return class scores for full-frame diagnostics, including rejected matches."""
        checked_timestamp = self._timestamp(timestamp)
        return self._scores(self._full_matches(frame, event_types), checked_timestamp)

    def score_roi(
        self, frame: np.ndarray, timestamp: float, *, roi: NormalizedROI,
        event_types: tuple[GameEventType, ...],
    ) -> dict[str, Any]:
        """Return class scores from the same ROI matcher used by detect_roi."""
        checked_timestamp = self._timestamp(timestamp)
        return self._scores(self._roi_matches(frame, roi, event_types), checked_timestamp)
