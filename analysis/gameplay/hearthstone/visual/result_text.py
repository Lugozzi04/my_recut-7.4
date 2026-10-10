"""Small, color-independent banner matcher for the experimental strategy."""
from __future__ import annotations

from typing import Any
import numpy as np

from analysis.gameplay.models import GameEvent, GameEventType
from .state_profile import StateAwareProfile
from .templates import require_opencv


class ResultTextDetector:
    detector_version = 'hearthstone-result-text-v1'

    def __init__(self, profile: StateAwareProfile) -> None:
        self.profile = profile
        self.cv = require_opencv()
        settings = profile.config['result']
        self.method = settings['representation']
        self.threshold = settings['threshold']
        self.margin = settings['margin']
        self.clahe = self.cv.createCLAHE(2.0, (4, 4))
        self.templates: list[tuple[str, str, float, np.ndarray]] = []
        left, top, right, bottom = profile.result_roi.pixel_bounds(*profile.reference_size)
        self.output_size = (right - left, bottom - top)
        for item in settings['templates']:
            image = profile.assets[item['path']]
            for scale in settings['scales']:
                size = (round(image.shape[1] * scale), round(image.shape[0] * scale))
                if min(size) < 2 or size[0] > self.output_size[0] or size[1] > self.output_size[1]:
                    raise ValueError('Text template exceeds the configured search ROI')
                resized = self.cv.resize(image, size, interpolation=self.cv.INTER_LINEAR)
                self.templates.append((item['class'], item.get('id', item['path']), scale, self._represent(resized)))

    def _represent(self, image: np.ndarray) -> np.ndarray:
        gray = self.cv.cvtColor(image, self.cv.COLOR_BGR2GRAY) if image.ndim == 3 else image
        if self.method == 'clahe':
            return self.clahe.apply(gray)
        if self.method == 'gradient':
            return self.cv.magnitude(self.cv.Sobel(gray, self.cv.CV_32F, 1, 0, ksize=3),
                                     self.cv.Sobel(gray, self.cv.CV_32F, 0, 1, ksize=3))
        return gray

    def score_roi(self, image: np.ndarray) -> dict[str, Any]:
        if image.shape[:2] != (self.output_size[1], self.output_size[0]):
            raise ValueError('Result ROI shape differs from the configured FFmpeg sampling geometry')
        roi = self._represent(image)
        scores = {'VICTORY': 0.0, 'DEFEAT': 0.0}
        best: dict[str, dict[str, Any]] = {}
        for kind, name, scale, template in self.templates:
            response = self.cv.matchTemplate(roi, template, self.cv.TM_CCOEFF_NORMED)
            _, score, _, location = self.cv.minMaxLoc(response)
            if np.isfinite(score) and score > scores[kind]:
                scores[kind] = float(score)
                best[kind] = {'template_id': name, 'scale': float(scale), 'location': list(location)}
        winner = max(scores, key=lambda key: scores[key])
        other = 'DEFEAT' if winner == 'VICTORY' else 'VICTORY'
        return {'scores': scores, 'winner': winner, 'score': scores[winner],
                'margin': scores[winner] - scores[other], 'best_templates': best}

    def event(self, evidence: dict[str, Any], timestamp: float) -> GameEvent | None:
        if evidence['score'] < self.threshold or evidence['margin'] <= 0 or evidence['margin'] < self.margin:
            return None
        winner = evidence['winner']
        return GameEvent(timestamp, GameEventType(winner), evidence['score'], metadata={
            **evidence['best_templates'][winner], 'raw_similarity': evidence['score'],
            'class_scores': dict(evidence['scores']), 'class_margin': evidence['margin'],
            'threshold': self.threshold, 'representation': self.method, 'roi': self.profile.result_roi.to_list(),
            'similarity_method': 'TM_CCOEFF_NORMED', 'detector_version': self.detector_version,
        })
