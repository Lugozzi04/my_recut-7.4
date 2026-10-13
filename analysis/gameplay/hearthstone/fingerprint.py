"""Cheap continuity evidence. Similarity never proves game identity or a result."""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any

import numpy as np

from analysis.hero_groups.recognition import PortraitSettings, crop
from .visual.templates import require_opencv

VERSION = "game-fingerprint-v1"


@dataclass
class GameFingerprint:
    timestamp: float
    board_score: float
    regions: dict[str, np.ndarray] = field(default_factory=dict)
    # Reserved for a separately validated reader; no guessed OCR/numeric mana.
    maximum_mana: float | None = None
    mana_confidence: float = 0.0


class FingerprintExtractor:
    def __init__(self, profile: Any) -> None:
        self.cv = require_opencv()
        self.profile = profile
        settings = PortraitSettings()
        self.rois = {
            "player_portrait": settings.portrait_roi,
            "opponent_portrait": (0.466, 0.143, 0.068, 0.128),
            "player_power": settings.power_roi,
            "opponent_power": (0.565625, 0.144, 0.08125, 0.144444444),
        }
        # Reuse structural anchor geometry, with artwork/text masked out.
        self.anchors = profile.config["board"]["anchors"]

    def extract(self, gray: np.ndarray, timestamp: float, board_score: float) -> GameFingerprint:
        regions = {}
        for name, roi in self.rois.items():
            descriptor = self._descriptor(crop(gray, roi))
            if descriptor is not None:
                regions[name] = descriptor
        for anchor in self.anchors:
            image = crop(gray, tuple(anchor["roi"])).copy()
            if not image.size:
                continue
            x, y, w, h = anchor["exclude"]
            image[round(y * image.shape[0]):round((y+h) * image.shape[0]),
                  round(x * image.shape[1]):round((x+w) * image.shape[1])] = round(float(image.mean()))
            descriptor = self._descriptor(image)
            if descriptor is not None:
                regions["structure_" + anchor["id"]] = descriptor
        return GameFingerprint(timestamp, board_score, regions)

    def _descriptor(self, roi: np.ndarray) -> np.ndarray | None:
        if not roi.size or min(roi.shape[:2]) < 4:
            return None
        small = self.cv.resize(roi, (16, 20), interpolation=self.cv.INTER_AREA).astype(np.float32)
        if float(small.std()) < 8 or float(np.mean((small < 5) | (small > 250))) > .65:
            return None
        centered = small - small.mean()
        return centered / max(float(np.linalg.norm(centered)), 1e-6)


def continuity(left: GameFingerprint, right: GameFingerprint, *, similarity_floor: float = .90
               ) -> tuple[float, tuple[str, ...]]:
    """Conservative routing score, NOT a calibrated probability or class score."""
    if not 0 <= similarity_floor <= 1:
        raise ValueError("Invalid continuity floor")
    common = sorted(left.regions.keys() & right.regions.keys())
    similarities = {key: max(-1.0, min(1.0, float(np.sum(left.regions[key] * right.regions[key]))))
                    for key in common if left.regions[key].shape == right.regions[key].shape
                    and np.isfinite(left.regions[key]).all() and np.isfinite(right.regions[key]).all()}
    common = sorted(similarities)
    reasons = ["changed_" + key for key, score in similarities.items() if score < similarity_floor]
    if not {"player_portrait", "opponent_portrait"}.issubset(common):
        reasons.append("unreadable_portrait")
    if not any(key.startswith("structure_") for key in common):
        reasons.append("unreadable_structure")
    # Mana changes only request recovery. They never close/open a game.
    mana = (left.maximum_mana, right.maximum_mana)
    if (all(v is not None and math.isfinite(v) for v in mana)
            and min(left.mana_confidence, right.mana_confidence) >= .9):
        before, after = mana
        assert before is not None and after is not None
        if before >= 6 and after <= 3 and before - after >= 4:
            reasons.append("possible_mana_reset")
    return max(0.0, min(similarities.values(), default=0.0)), tuple(reasons)


@dataclass(frozen=True)
class ContinuityAssessment:
    """Independent evidence groups, not a probability of identical game identity."""
    category: str
    similarities: dict[str, float]
    stable: tuple[str, ...]
    changed: tuple[str, ...]
    missing: tuple[str, ...]


def assess_continuity(left: GameFingerprint, right: GameFingerprint, *, similarity_floor: float = .90
                      ) -> ContinuityAssessment:
    # Keep the raw per-region diagnostics. A used power/one animated portrait is
    # neither loss of board state nor, by itself, proof of a new match.
    _, reasons = continuity(left, right, similarity_floor=similarity_floor)
    similarities = {key: max(-1.0, min(1.0, float(np.sum(left.regions[key] * right.regions[key]))))
                    for key in sorted(left.regions.keys() & right.regions.keys())
                    if left.regions[key].shape == right.regions[key].shape
                    and np.isfinite(left.regions[key]).all() and np.isfinite(right.regions[key]).all()}
    stable = tuple(key for key, score in similarities.items() if score >= similarity_floor)
    changed = tuple(key for key, score in similarities.items() if score < similarity_floor)
    missing = tuple(sorted((left.regions.keys() | right.regions.keys()) - similarities.keys()))
    portraits_changed = {"player_portrait", "opponent_portrait"}.issubset(changed)
    player_supported = any(key in stable for key in ("player_portrait", "player_power"))
    opponent_supported = any(key in stable for key in ("opponent_portrait", "opponent_power"))
    structure_supported = any(key.startswith("structure_") for key in stable)
    if portraits_changed or "possible_mana_reset" in reasons:
        category = "TRANSITION_SUSPECTED"
    elif player_supported and opponent_supported and structure_supported:
        category = "CONTINUITY_PLAUSIBLE"
    else:
        category = "IDENTITY_UNCERTAIN"
    return ContinuityAssessment(category, similarities, stable, changed, missing)
