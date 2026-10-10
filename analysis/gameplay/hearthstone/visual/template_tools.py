"""Capture-time duplicate review; never changes detector acceptance thresholds."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from analysis.gameplay.hearthstone.visual.templates import TemplateRepository, require_opencv


def similar_templates(patch: np.ndarray, directory: Path) -> list[dict[str, Any]]:
    """Report pixel duplicates and same-aspect normalized correlations.

    Near-duplicate similarity is a curation hint, NOT a recognition score or a
    license to delete animation variants. The caller must explicitly review it.
    """
    cv = require_opencv()
    gray = cv.cvtColor(patch, cv.COLOR_BGR2GRAY)
    found: list[dict[str, Any]] = []
    if not directory.exists():
        return found
    for file in sorted(directory.iterdir()):
        if file.suffix.lower() not in TemplateRepository._extensions or not file.is_file():
            continue
        if file.is_symlink() or file.stat().st_size > TemplateRepository._max_image_bytes:
            raise ValueError('Unsafe existing template during duplicate review')
        image = cv.imdecode(np.frombuffer(file.read_bytes(), dtype=np.uint8), cv.IMREAD_GRAYSCALE)
        if image is None or max(image.shape) > 4096 or float(image.std()) < 1:
            continue
        exact = bool(np.array_equal(gray, image))
        aspect_ratio = (gray.shape[1] / gray.shape[0]) / (image.shape[1] / image.shape[0])
        if not exact and not 0.95 <= aspect_ratio <= 1.05:
            continue
        resized = image if image.shape == gray.shape else cv.resize(image, (gray.shape[1], gray.shape[0]), interpolation=cv.INTER_AREA)
        correlation = float(cv.matchTemplate(gray, resized, cv.TM_CCOEFF_NORMED)[0, 0])
        found.append({'path': str(file), 'pixel_duplicate': exact, 'similarity': correlation})
    return sorted(found, key=lambda item: item['similarity'], reverse=True)
