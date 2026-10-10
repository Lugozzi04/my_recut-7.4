"""Local search windows around game anchors; the only audio/video that gets analysed."""
from __future__ import annotations

from dataclasses import dataclass

from .models import BoundaryType, TimeRange


@dataclass(frozen=True)
class WindowSettings:
    """Seconds before/after each anchor. START and END are asymmetric on purpose.

    START: the run-up (queue talk, "vediamo cosa troviamo...") precedes VS, while
    the first seconds after VS (VS animation, mulligan) already belong to the game.
    END: the post-game comment follows the result banner; only a few seconds
    before it (the final play) can matter. About 70 s of context per removed game.
    Windows never cross the neighbouring games' anchors or the file bounds.
    """

    start_before_s: float = 25.0
    start_after_s: float = 5.0
    end_before_s: float = 5.0
    end_after_s: float = 35.0

    def __post_init__(self) -> None:
        for name in ("start_before_s", "start_after_s", "end_before_s", "end_after_s"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 600:
                raise ValueError(f"{name} must be within 0..600 seconds")


def search_window(boundary: BoundaryType, anchor: float, *, duration: float, settings: WindowSettings,
                  previous_game_end: float | None = None, next_game_start: float | None = None
                  ) -> tuple[TimeRange, tuple[str, ...]]:
    before, after = ((settings.start_before_s, settings.start_after_s) if boundary == BoundaryType.START
                     else (settings.end_before_s, settings.end_after_s))
    low, high = anchor - before, anchor + after
    clipped: list[str] = []
    if previous_game_end is not None and previous_game_end <= anchor and low < previous_game_end:
        low, clipped = previous_game_end, clipped + ["previous_game_end"]
    if next_game_start is not None and next_game_start >= anchor and high > next_game_start:
        high, clipped = next_game_start, clipped + ["next_game_start"]
    if low < 0.0:
        low, clipped = 0.0, clipped + ["start_of_file"]
    if high > duration:
        high, clipped = duration, clipped + ["end_of_file"]
    low, high = min(low, anchor), max(high, anchor)
    return TimeRange(low, high), tuple(clipped)
