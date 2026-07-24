from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass
class AudioTrackState:
    gain_db: float = 0.0
    pan: float = 0.0  # -1..+1
    muted: bool = False
    solo: bool = False


class AudioMixer:
    """
    Placeholder mixer. A real implementation would mix PCM frames to a single output buffer.
    """

    def __init__(self, sample_rate: int = 48000, channels: int = 2) -> None:
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.tracks: List[AudioTrackState] = []

    def add_track(self, state: AudioTrackState) -> None:
        self.tracks.append(state)
