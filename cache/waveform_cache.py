from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class WaveformCacheEntry:
    media_id: str
    path: Path
    sample_rate: int
    channels: int


class WaveformCache:
    """
    Placeholder cache for per-clip waveform data.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def get(self, media_id: str) -> Optional[WaveformCacheEntry]:
        return None

    def put(self, entry: WaveformCacheEntry) -> None:
        return
