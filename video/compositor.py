from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass
class Layer:
    clip_id: str
    opacity: float = 1.0
    x: float = 0.0
    y: float = 0.0
    scale: float = 1.0


class VideoCompositor:
    """
    Placeholder compositing pipeline (CPU/GPU).
    """

    def __init__(self) -> None:
        self.layers: List[Layer] = []

    def add_layer(self, layer: Layer) -> None:
        self.layers.append(layer)
