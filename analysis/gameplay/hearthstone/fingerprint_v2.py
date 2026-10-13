"""Sparse V2 routing descriptors; never produce START, END or class labels."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from pathlib import Path
from typing import Any

import numpy as np

from analysis.hero_groups.recognition import crop
from utils.runtime_paths import resource_path
from .fingerprint import FingerprintExtractor, GameFingerprint, assess_continuity

VERSION = "sparse-fingerprint-v2"
# Normalized reference-frame coordinates. No play area, hand or minions.
ROIS = {
    "player_portrait": (.466, .707, .068, .128),
    "opponent_portrait": (.466, .085, .068, .128),
    "map_top_left": (.235, .045, .105, .075),
    "map_top_right": (.675, .065, .10, .075),
    "map_bottom_left": (.235, .735, .10, .06),
    "map_bottom_right": (.685, .745, .09, .055),
    "map_left": (.221, .25, .023, .22),
    "map_right": (.854, .27, .022, .22),
}
# Known streamer webcam, scoreboard, deck/chat margins. Configurable overrides
# are cache-keyed; intersecting descriptors are omitted, never filled with pixels.
DEFAULT_EXCLUSIONS = ((0., .50, .218, .40), (.80, 0., .20, .20), (.90, .20, .10, .80))
MANA_ROI = (634 / 960, 496 / 540, 6 / 960, 9 / 540)


class MaximumManaReader:
    """Tiny optional denominator-only visual references; unsupported glyphs -> None.

    Reads total mana (right of '/'), not lit/spent crystals or the numerator.
    A near-exact match + competing-value margin is required. No general OCR.
    """
    def __init__(self, file: Path | None = None) -> None:
        self.file = file or resource_path("resources", "gameplay", "hearthstone", "adaptive_v2", "mana.npz")
        self.templates: dict[int, np.ndarray] = {}
        self.fingerprint = "unavailable"
        if self.file.is_file():
            self.fingerprint = hashlib.sha256(self.file.read_bytes()).hexdigest()
            with np.load(self.file, allow_pickle=False) as data:
                for name in data.files:
                    if name.isdigit() and 0 <= int(name) <= 10:
                        self.templates[int(name)] = data[name].astype(np.float32)

    def read(self, gray: np.ndarray) -> tuple[float | None, float]:
        image = crop(gray, MANA_ROI).astype(np.float32)
        if image.shape != (9, 6) or float(image.std()) < 18:
            return None, 0.
        def feature(im: np.ndarray) -> np.ndarray:
            centered = im - im.mean()
            return centered / max(float(np.linalg.norm(centered)), 1e-6)
        descriptor = feature(image)
        scores = sorted(((float(np.sum(descriptor * feature(t))), value)
                         for value, t in self.templates.items() if t.shape == image.shape), reverse=True)
        if len(scores) < 2:
            return None, 0.
        best, value = scores[0]
        # Deliberately almost-exact: tiny font, incomplete coverage, no guessing.
        if best < .995 or best - scores[1][0] < .05:
            return None, 0.
        return float(value), min(1., best)


@dataclass
class SparseFingerprint:
    fingerprint: GameFingerprint
    state: str
    weak_start: bool = False
    weak_result: bool = False
    diagnostics: dict[str, Any] = field(default_factory=dict)


class SparseFingerprintExtractor(FingerprintExtractor):
    def __init__(self, profile: Any, *, exclusions: tuple[tuple[float, float, float, float], ...] = DEFAULT_EXCLUSIONS,
                 mana_reader: MaximumManaReader | None = None) -> None:
        super().__init__(profile)
        self.rois = dict(ROIS)
        self.exclusions = exclusions
        self.mana_reader = mana_reader or MaximumManaReader()

    def extract(self, gray: np.ndarray, timestamp: float, board_score: float) -> GameFingerprint:
        regions = {}
        for name, roi in self.rois.items():
            x, y, w, h = roi
            if any(x < a+c and a < x+w and y < b+d and b < y+h for a,b,c,d in self.exclusions):
                continue
            image = crop(gray, roi)
            # Low-pass before existing centered/unit-norm descriptor; small
            # portrait animation/stream noise is not exact pixel identity.
            descriptor = self._descriptor(self.cv.GaussianBlur(image, (3, 3), 0)) if image.size else None
            if descriptor is not None:
                regions[name] = descriptor
        mana, confidence = self.mana_reader.read(gray) if board_score >= .38 else (None, 0.)
        return GameFingerprint(timestamp, board_score, regions, mana, confidence)


def discontinuities(left: SparseFingerprint, right: SparseFingerprint, floor: float) -> tuple[str, ...]:
    if left.weak_start or right.weak_start or left.weak_result or right.weak_result:
        return ("visual_event_candidate",)
    if "UNKNOWN" in (left.state, right.state):
        return ("state_unknown",)
    if left.state != right.state:
        return ("board_transition",)
    if left.state == "OUT_OF_GAME":
        return ()
    a, b = left.fingerprint, right.fingerprint
    # Chronological direction only: cards can reduce mana; this schedules a
    # search, never asserts a new game or prunes the opposite half.
    if (a.maximum_mana is not None and b.maximum_mana is not None
            and min(a.mana_confidence, b.mana_confidence) >= .995 and b.maximum_mana < a.maximum_mana):
        return ("possible_mana_reset",)
    assessment = assess_continuity(a, b, similarity_floor=floor)
    stable = set(assessment.stable)
    maps = sum(key.startswith("map_") for key in stable)
    if {"player_portrait", "opponent_portrait"}.issubset(stable) and maps >= 2:
        return ()
    if {"player_portrait", "opponent_portrait"} & set(assessment.changed):
        return ("identity_transition_suspected",)
    return ("identity_uncertain",)
