"""Cheap masked structural anchors; board evidence is never outcome evidence."""
from __future__ import annotations

from analysis.performance_profile import submeasure
import numpy as np

from .state_profile import StateAwareProfile, number
from .templates import NormalizedROI, require_opencv


class BoardPresenceDetector:
    def __init__(self, profile: StateAwareProfile) -> None:
        self.profile = profile
        self.cv = require_opencv()
        self.anchors = []
        for anchor in profile.config['board']['anchors']:
            roi = NormalizedROI.from_value(anchor['roi'])
            width, height = profile.reference_size
            left, top = round(roi.x * width), round(roi.y * height)
            right, bottom = left + round(roi.width * width), top + round(roi.height * height)
            exclusion = NormalizedROI.from_value(anchor['exclude'], name='board.anchor.exclude')
            prepared = []
            for asset in anchor['templates']:
                image = profile.assets[asset['path']]
                if image.shape != (bottom - top, right - left):
                    raise ValueError('Board anchor template shape differs from its reference ROI')
                mask = np.full(image.shape, 255, np.uint8)
                x1, y1 = round(exclusion.x * image.shape[1]), round(exclusion.y * image.shape[0])
                x2 = x1 + round(exclusion.width * image.shape[1])
                y2 = y1 + round(exclusion.height * image.shape[0])
                mask[y1:y2, x1:x2] = 0
                if np.count_nonzero(mask) < 8:
                    raise ValueError('Board mask must retain structural context')
                prepared.append((self.cv.Canny(image, 50, 120), mask))
            padding = int(number(anchor.get('padding_px', 8), 'board.anchor.padding_px', 0, 32))
            self.anchors.append((anchor['id'], (left, top, right, bottom), padding, prepared))

    @property
    def comparisons_per_frame(self) -> int:
        return sum(len(prepared) for _, _, _, prepared in self.anchors)

    def score(self, normalized: np.ndarray) -> tuple[float, dict[str, float]]:
        if normalized.shape[:2] != (self.profile.reference_size[1], self.profile.reference_size[0]):
            raise ValueError('Board frame must use profile reference geometry')
        with submeasure("image_preparation"):
            gray = self.cv.cvtColor(normalized, self.cv.COLOR_BGR2GRAY) if normalized.ndim == 3 else normalized
        scores = {}
        for name, (left, top, right, bottom), padding, templates in self.anchors:
            roi = gray[max(0, top-padding):min(gray.shape[0], bottom+padding),
                       max(0, left-padding):min(gray.shape[1], right+padding)]
            with submeasure("image_preparation"):
                edge = self.cv.Canny(roi, 50, 120)
            best = 0.0
            with submeasure("template_matching"):
                for template, mask in templates:
                    response = self.cv.matchTemplate(edge, template, self.cv.TM_CCORR_NORMED, mask=mask)
                    finite = response[np.isfinite(response)]
                    if finite.size:
                        best = max(best, float(finite.max()))
            scores[name] = min(1.0, best)
        # Conjunction, not a learned/probabilistic weighted sum. A single matching
        # button or portrait cannot assert that the entire board is still active.
        return min(scores.values()), scores
