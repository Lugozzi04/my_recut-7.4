"""Reject the Italian mulligan confirmation overlay, never classify a hero.

Reference: videoprova, archived samples_initial.jpg, game1/sample0, crop
[0:64,25:68] of the normalized portrait. Used only for sample readability.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import numpy as np
from analysis.gameplay.hearthstone.visual.templates import require_opencv


@lru_cache(maxsize=1)
def confirmation_template() -> np.ndarray:
    cv = require_opencv()
    data = (Path(__file__).parent / "assets" / "confirmation.png").read_bytes()
    image = cv.imdecode(np.frombuffer(data, np.uint8), cv.IMREAD_GRAYSCALE)
    if image is None:
        raise ValueError("Missing portrait sample-quality asset.")
    return image


def confirmation_visible(image: np.ndarray) -> bool:
    cv = require_opencv()
    gray = cv.cvtColor(cv.resize(image, (64, 80)), cv.COLOR_BGR2GRAY)
    return float(cv.matchTemplate(gray, confirmation_template(), cv.TM_CCOEFF_NORMED).max()) >= 0.85
