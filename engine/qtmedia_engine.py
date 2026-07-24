from __future__ import annotations

from typing import Optional

from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput

from core.project import Project
from .playback_engine import PlaybackEngine, PlaybackState


class QtMediaEngine(PlaybackEngine):
    """
    Temporary playback adapter (single video + single audio).
    This is a placeholder to allow a future swap with a real multi-track engine.
    """

    def __init__(self) -> None:
        self._player = QMediaPlayer()
        self._audio = QAudioOutput()
        self._player.setAudioOutput(self._audio)
        self._project: Optional[Project] = None
        self._duration = 0.0

    def load_project(self, project: Project) -> None:
        self._project = project
        self._duration = float(project.timeline_duration() or 0.0)

    def set_position(self, t: float) -> None:
        self._player.setPosition(int(max(0.0, t) * 1000))

    def play(self) -> None:
        self._player.play()

    def pause(self) -> None:
        self._player.pause()

    def state(self) -> PlaybackState:
        return PlaybackState(
            position=float(self._player.position()) / 1000.0,
            duration=float(self._duration),
            playing=self._player.playbackState() == QMediaPlayer.PlayingState,
        )
