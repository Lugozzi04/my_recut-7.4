"""Chronological, cheap state evidence only schedules local result scans."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Iterable

from analysis.gameplay.models import GameEvent, GameEventType


@dataclass(frozen=True)
class MonitorSettings:
    fps: float = 1.0
    board_high: float = 0.38
    board_low: float = 0.20
    board_observations: int = 2
    weak_score: float = 0.65
    weak_margin: float = 0.04
    before_s: float = 2.0
    after_s: float = 5.0
    high_fps: float = 60.0

    def __post_init__(self) -> None:
        for name in ('fps', 'board_high', 'board_low', 'weak_score', 'weak_margin', 'before_s', 'after_s', 'high_fps'):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f'{name} must be finite')
        if not 0.25 <= self.fps <= 2 or not 10 <= self.high_fps <= 120:
            raise ValueError('Monitor FPS must be 0.25..2 and local FPS 10..120')
        if not 0 <= self.board_low < self.board_high <= 1:
            raise ValueError('Board hysteresis needs 0 <= low < high <= 1')
        if not 0 <= self.weak_score <= 1 or not 0 <= self.weak_margin <= 1:
            raise ValueError('Weak score and margin must be in 0..1')
        if not 0 <= self.before_s <= 30 or not 0 < self.after_s <= 30:
            raise ValueError('Local padding must be 0..30 seconds, with positive future coverage')
        if type(self.board_observations) is not int or not 1 <= self.board_observations <= 10:
            raise ValueError('board_observations must be an integer in 1..10')

    def to_mapping(self) -> dict[str, float | int]:
        return asdict(self)


@dataclass(frozen=True)
class MonitorObservation:
    timestamp: float
    board_score: float
    result_score: float
    result_margin: float


@dataclass(frozen=True)
class ResultWindow:
    start: float
    end: float
    triggers: tuple[tuple[float, str], ...]


def merge_windows(windows: Iterable[ResultWindow]) -> list[ResultWindow]:
    merged: list[ResultWindow] = []
    for window in sorted(windows, key=lambda item: item.start):
        if merged and window.start <= merged[-1].end:
            previous = merged.pop()
            merged.append(ResultWindow(previous.start, max(previous.end, window.end), previous.triggers + window.triggers))
        else:
            merged.append(window)
    return merged


class GameStateMonitor:
    """VS arms board transitions; independent weak results provide a second trigger.

    A low board score alone never starts a game or declares its result. A board
    disappearance triggers on its falling edge, rearming only after actual board
    reacquisition. Weak candidates use a separate rising edge and can recover an
    orphan result even when VS was missing. Missing boundaries remain explicit.
    """

    def __init__(self, settings: MonitorSettings) -> None:
        self.settings = settings

    def plan(self, observations: Iterable[MonitorObservation], starts: Iterable[GameEvent], *, duration: float
             ) -> tuple[list[ResultWindow], list[float]]:
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError('duration must be finite and positive')
        rows = list(observations)
        if any(not all(math.isfinite(v) for v in (row.timestamp, row.board_score, row.result_score, row.result_margin))
               or not 0 <= row.timestamp <= duration or not 0 <= row.board_score <= 1
               or not 0 <= row.result_score <= 1 or not 0 <= row.result_margin <= 1 for row in rows):
            raise ValueError('Invalid monitor evidence')
        if any(b.timestamp <= a.timestamp for a, b in zip(rows, rows[1:])):
            raise ValueError('Monitor observations must have strictly increasing source timestamps')
        anchors = sorted(event.timestamp for event in starts if event.type == GameEventType.VS_SCREEN)
        anchor_index = 0
        armed = board_present = weak_present = False
        high_count = 0
        windows: list[ResultWindow] = []
        reacquisitions: list[float] = []
        for row in rows:
            while anchor_index < len(anchors) and anchors[anchor_index] <= row.timestamp:
                armed, board_present, high_count = True, False, 0
                anchor_index += 1
            high_count = high_count + 1 if row.board_score >= self.settings.board_high else 0
            if armed and not board_present and high_count >= self.settings.board_observations:
                board_present = True
                reacquisitions.append(row.timestamp)
            causes: list[str] = []
            if armed and board_present and row.board_score <= self.settings.board_low:
                causes.append('board_disappeared')
                board_present = False
                high_count = 0
            weak = row.result_score >= self.settings.weak_score and row.result_margin >= self.settings.weak_margin
            if weak and not weak_present:
                causes.append('weak_result')
            weak_present = weak
            for cause in causes:
                windows.append(ResultWindow(max(0.0, row.timestamp - self.settings.before_s),
                                            min(duration, row.timestamp + self.settings.after_s),
                                            ((row.timestamp, cause),)))
        return merge_windows(windows), reacquisitions
