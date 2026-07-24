from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional


@dataclass
class AudioFrame:
    pts: float
    samples: bytes
    sample_rate: int
    channels: int
    format: str = "f32"


class AudioDecoder:
    """
    Placeholder for a real FFmpeg/PyAV decoder.
    """

    def __init__(self, path: str) -> None:
        self.path = path

    def decode(self) -> Iterable[AudioFrame]:
        if False:
            yield AudioFrame(0.0, b"", 48000, 2)
