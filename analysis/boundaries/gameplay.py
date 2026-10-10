"""Read gameplay anchors from an existing GameplayAnalysisResult; never re-detects.

Game-agnostic part: GameSegment start/end/result and neighbouring games.
Optional state evidence (board presence/reacquisition, result-window triggers,
1 FPS monitor board scores) is read from the state-aware analyzer's metadata
when present and ignored otherwise. Nothing here changes or re-runs the detector.

Termination (completeness) is decided from evidence, never from the result enum
alone; the keep policy then maps termination + result to a disposition.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math
from typing import Any

from analysis.gameplay.models import GameResult, GameSegment, GameplayAnalysisResult
from .models import CandidateSource, GameDisposition, GameInfo, GameTermination, GameplayMarker

KEEP_POLICIES = ("remove-losses", "wins")


@dataclass(frozen=True)
class EofStateSettings:
    """Board-presence evidence used for truncation and for missing-anchor estimates.

    Defaults mirror the state-aware monitor: board present means score >= the
    profile's board_high for board_observations consecutive 1 FPS samples.
    The margins widen ESTIMATED anchors (never observed ones) so a removal
    region covers the VS/mulligan before the board appears and the result
    screen after it disappears.
    """

    minimum_present_observations: int | None = None  # None: analysis settings' board_observations
    board_high: float | None = None  # None: analysis settings' board_high
    eof_tolerance_s: float = 2.0  # first/last observation must be this close to file start/EOF
    maximum_game_duration_s: float | None = None  # None: analysis settings' maximum_game_duration_s
    estimated_start_margin_s: float = 10.0
    estimated_end_margin_s: float = 5.0


@dataclass(frozen=True)
class RemovalAnchors:
    """Where a removed game's region is anchored; `fixed` boundaries need no resolver."""

    start: float
    start_kind: str  # game_start | estimated_board_start | previous_game_end | start_of_file
    end: float
    end_kind: str  # game_end | estimated_board_end | next_game_start | end_of_file

    @property
    def start_fixed(self) -> bool:
        return self.start_kind == "start_of_file"

    @property
    def end_fixed(self) -> bool:
        return self.end_kind == "end_of_file"


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


class GameplayTimeline:
    def __init__(self, analysis: GameplayAnalysisResult, eof: EofStateSettings | None = None, *,
                 policy: str = "remove-losses") -> None:
        if policy not in KEEP_POLICIES:
            raise ValueError(f"Unknown keep policy '{policy}' (choices: {', '.join(KEEP_POLICIES)})")
        self.analysis = analysis
        self.duration = float(analysis.duration)
        self.games = sorted(analysis.games, key=lambda game: game.index)
        self.eof = eof or EofStateSettings()
        self.policy = policy

    def game(self, index: int) -> GameSegment:
        for game in self.games:
            if game.index == index:
                return game
        raise KeyError(f"Game {index} not found (available: {[g.index for g in self.games]})")

    def _boundary(self, game: GameSegment) -> float | None:
        return game.start if game.start is not None else game.end

    def previous_game_end(self, game: GameSegment) -> float | None:
        reference = self._boundary(game)
        raw = [g.end if g.end is not None else g.start for g in self.games if g.index < game.index]
        ends = [t for t in raw if t is not None and (reference is None or t <= reference)]
        return max(ends) if ends else None

    def next_game_start(self, game: GameSegment) -> float | None:
        reference = game.end if game.end is not None else game.start
        raw = [g.start if g.start is not None else g.end for g in self.games if g.index > game.index]
        starts = [t for t in raw if t is not None and (reference is None or t >= reference)]
        return min(starts) if starts else None

    def markers(self) -> list[GameplayMarker]:
        """Board transitions recorded by the state-aware monitor, if present."""
        meta = self.analysis.metadata
        out: list[GameplayMarker] = []
        for t in meta.get("board_reacquisitions", []) or []:
            value = _number(t)
            if value is not None:
                out.append(GameplayMarker(value, CandidateSource.BOARD_REACQUIRED.value))
        for window in meta.get("result_windows", []) or []:
            for trigger in window.get("triggers", []) if isinstance(window, Mapping) else []:
                if isinstance(trigger, (list, tuple)) and len(trigger) == 2 and trigger[1] == "board_disappeared":
                    value = _number(trigger[0])
                    if value is not None:
                        out.append(GameplayMarker(value, CandidateSource.BOARD_DISAPPEARED.value))
        return sorted(out, key=lambda marker: (marker.timestamp, marker.kind))

    # ---- board evidence ----------------------------------------------------------------------
    def _settings(self, name: str) -> float | None:
        monitor = self.analysis.settings.get("monitor", {})
        value = monitor.get(name) if isinstance(monitor, Mapping) else None
        return _number(value if value is not None else self.analysis.settings.get(name))

    def _board(self) -> tuple[list[tuple[float, float]], float | None, int]:
        evidence = [r for r in self.analysis.metadata.get("monitor_evidence", []) or [] if isinstance(r, Mapping)]
        rows = sorted((float(r["timestamp"]), float(r["board_score"])) for r in evidence
                      if _number(r.get("timestamp")) is not None and _number(r.get("board_score")) is not None)
        high = self.eof.board_high if self.eof.board_high is not None else self._settings("board_high")
        count = self.eof.minimum_present_observations or int(self._settings("board_observations") or 0)
        return rows, high, count

    def board_present_at_eof(self) -> tuple[bool | None, str]:
        """True/False from monitor evidence; None when the analysis has none."""
        rows, high, count = self._board()
        if not rows or high is None or count <= 0:
            return None, "no board monitor evidence in the analysis"
        if self.duration - rows[-1][0] > self.eof.eof_tolerance_s:
            return False, f"last monitor sample {rows[-1][0]:.3f}s is far from EOF {self.duration:.3f}s"
        tail = rows[-count:]
        present = len(tail) == count and all(score >= high for _, score in tail)
        return present, f"last {len(tail)} board scores {[round(s, 3) for _, s in tail]} vs board_high {high}"

    def board_present_at_start(self) -> tuple[bool | None, str]:
        rows, high, count = self._board()
        if not rows or high is None or count <= 0:
            return None, "no board monitor evidence in the analysis"
        if rows[0][0] > self.eof.eof_tolerance_s:
            return False, f"first monitor sample {rows[0][0]:.3f}s is far from file start"
        head = rows[:count]
        present = len(head) == count and all(score >= high for _, score in head)
        return present, f"first {len(head)} board scores {[round(s, 3) for _, s in head]} vs board_high {high}"

    def _board_runs(self, low: float, high_t: float) -> list[tuple[float, float]]:
        rows, high, count = self._board()
        if high is None or count <= 0:
            return []
        runs: list[tuple[float, float]] = []
        current: list[float] = []
        for t, score in rows:
            if not low < t < high_t:
                continue
            if score >= high:
                current.append(t)
            else:
                if len(current) >= count:
                    runs.append((current[0], current[-1]))
                current = []
        if len(current) >= count:
            runs.append((current[0], current[-1]))
        return runs

    # ---- termination / policy ----------------------------------------------------------------
    def termination(self, game: GameSegment) -> tuple[GameTermination, str]:
        if game.start is None and game.end is None:
            return GameTermination.MISSING_START, "neither start nor end observed"
        if game.start is None:
            if not any(g.index < game.index for g in self.games):
                present, detail = self.board_present_at_start()
                if present:
                    return GameTermination.TRUNCATED_AT_START, "file begins mid-game: " + detail
                return GameTermination.MISSING_START, "first game without VS; no evidence the file begins mid-game: " + detail
            return GameTermination.MISSING_START, "game start (VS) not observed"
        if game.end is not None:
            if game.result == GameResult.UNKNOWN:
                return GameTermination.AMBIGUOUS_RESULT, "start and end observed, result ambiguous"
            return GameTermination.COMPLETE, "start and result observed"
        if any(g.index > game.index for g in self.games):
            return GameTermination.MISSING_RESULT, "a later game exists, so the VOD did not end during this game"
        limit = self.eof.maximum_game_duration_s or self._settings("maximum_game_duration_s")
        if limit is not None and self.duration - game.start > limit:
            return GameTermination.MISSING_RESULT, f"EOF is {self.duration - game.start:.1f}s after start (> {limit}s)"
        present, detail = self.board_present_at_eof()
        if present:
            return GameTermination.IN_PROGRESS_AT_EOF, "board still present at EOF: " + detail
        return GameTermination.MISSING_RESULT, "no positive evidence of play at EOF: " + detail

    def _is_last(self, game: GameSegment) -> bool:
        return not any(g.index > game.index for g in self.games)

    def disposition(self, game: GameSegment, termination: GameTermination) -> tuple[GameDisposition, str]:
        if self.policy == "remove-losses":  # legacy analysis default: only certain removals
            if termination == GameTermination.COMPLETE and game.result == GameResult.LOSS:
                return GameDisposition.REMOVE, "complete LOSS"
            if termination == GameTermination.IN_PROGRESS_AT_EOF:
                return GameDisposition.REMOVE_TO_EOF, "video ends during the game"
            return GameDisposition.KEEP, "not a certain removal"
        # wins: only a COMPLETE WIN may be published; every other fragment is removed.
        if termination == GameTermination.COMPLETE and game.result == GameResult.WIN:
            return GameDisposition.KEEP, "complete WIN"
        if termination == GameTermination.TRUNCATED_AT_START:
            return GameDisposition.REMOVE_FROM_START, "file begins mid-game"
        if termination in (GameTermination.IN_PROGRESS_AT_EOF, GameTermination.MISSING_RESULT) and self._is_last(game):
            return GameDisposition.REMOVE_TO_EOF, "last game without a result"
        return GameDisposition.REMOVE, ("complete LOSS" if termination == GameTermination.COMPLETE
                                        else f"not a complete WIN ({termination.value})")

    def info(self, game: GameSegment) -> GameInfo:
        termination, reason = self.termination(game)
        disposition, why = self.disposition(game, termination)
        if game.user_override:
            reason = "user override present; " + reason
        return GameInfo(game.id, game.index, game.start, game.end, game.result.value, termination, disposition,
                        f"{why}; {reason}")

    def removable(self) -> list[GameSegment]:
        return [game for game in self.games if self.info(game).disposition != GameDisposition.KEEP]

    def removal_anchors(self, game: GameSegment) -> RemovalAnchors:
        """Hard anchors of a removed region: observed when available, else estimated (with margins)."""
        info = self.info(game)
        previous, following = self.previous_game_end(game), self.next_game_start(game)
        if info.disposition == GameDisposition.REMOVE_FROM_START:
            start, start_kind = 0.0, "start_of_file"
        elif game.start is not None:
            start, start_kind = float(game.start), "game_start"
        else:
            low = previous if previous is not None else 0.0
            high = game.end if game.end is not None else (following if following is not None else self.duration)
            runs = self._board_runs(low, high)
            if runs:
                start, start_kind = max(low, runs[0][0] - self.eof.estimated_start_margin_s), "estimated_board_start"
            else:
                start, start_kind = low, ("previous_game_end" if previous is not None else "start_of_file")
        if info.disposition == GameDisposition.REMOVE_TO_EOF:
            end, end_kind = self.duration, "end_of_file"
        elif game.end is not None:
            end, end_kind = float(game.end), "game_end"
        else:
            high = following if following is not None else self.duration
            runs = self._board_runs(start, high)
            if runs:
                end, end_kind = min(high, runs[-1][1] + self.eof.estimated_end_margin_s), "estimated_board_end"
            else:
                end, end_kind = high, ("next_game_start" if following is not None else "end_of_file")
        return RemovalAnchors(start, start_kind, max(start, end), end_kind)

    def neighbor_summary(self, game: GameSegment) -> dict[str, Any]:
        def summary(other: GameSegment | None) -> dict[str, Any] | None:
            if other is None:
                return None
            item = self.info(other)
            return {"index": item.index, "start": item.start, "end": item.end, "result": item.result,
                    "termination": item.termination.value, "disposition": item.disposition.value}

        before = [g for g in self.games if g.index < game.index]
        after = [g for g in self.games if g.index > game.index]
        return {"previous": summary(before[-1] if before else None), "next": summary(after[0] if after else None),
                "policy": self.policy}

    def kept_spans(self) -> list[tuple[float, float]]:
        """[start, end] of games the policy keeps; removal regions must never cut into them."""
        spans = []
        for game in self.games:
            item = self.info(game)
            if item.disposition == GameDisposition.KEEP and game.start is not None and game.end is not None:
                spans.append((float(game.start), float(game.end)))
        return spans
