from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass
class VideoFrame:
    pts: float
    width: int
    height: int
    data: bytes
    format: str = "rgba"


class VideoDecoder:
    """
    Placeholder for a real FFmpeg/PyAV decoder.
    """

    def __init__(self, path: str) -> None:
        self.path = path

    def decode(self) -> Iterable[VideoFrame]:
        if False:
            yield VideoFrame(0.0, 0, 0, b"")
