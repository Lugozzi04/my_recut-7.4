from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

from core.project import Project


@dataclass
class PlaybackState:
    position: float = 0.0
    duration: float = 0.0
    playing: bool = False


class PlaybackEngine(Protocol):
    def load_project(self, project: Project) -> None:
        ...

    def set_position(self, t: float) -> None:
        ...

    def play(self) -> None:
        ...

    def pause(self) -> None:
        ...

    def state(self) -> PlaybackState:
        ...
