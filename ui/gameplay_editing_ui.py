"""GUI integration of Gameplay Editing.

Runs the same `analysis.gameplay_editing.GameplayEditingService` the CLI uses (no detector, boundary or
cut logic here) on a background thread, and provides the pure helpers the editor needs to map its
result onto tracks and to edit island boundaries:

* `gameplay_keeps` (WIN islands) are the source of truth; `gameplay_cuts` are always derived as their
  complement, exactly like `GameKeepPolicy.cuts`, and compose with classic/manual/suppressed cuts in
  `analysis.classic.compute_classic_cuts`.
* Tracks may be segments of a source file (after a split): results are sliced to the segment and
  shifted into the segment-local domain the editor uses for cuts.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal

from analysis.audio_service import AnalysisCancellation
from analysis.cut_engine import Segment, invert_to_keeps, merge_overlaps

BOUNDARY_CHOICES: tuple[tuple[str, str, str], ...] = (
    ("exact", "Exact", "Deterministic smart boundaries from local audio, speech and visuals. Nothing leaves this PC."),
    ("claude", "Claude", "Asks the Claude CLI to pick each START/END from the transcript context (sends speech "
                         "text around each boundary). Falls back to Exact if unavailable."),
    ("codex", "Codex", "Asks the Codex CLI to pick each START/END from the transcript context (sends speech "
                       "text around each boundary). Falls back to Exact if unavailable."),
)
MIN_ISLAND_S = 1.0


def complement(keeps: list[Segment], length: float) -> list[Segment]:
    """Gameplay cuts = everything outside the islands (same rule as GameKeepPolicy.cuts)."""
    islands = merge_overlaps([Segment(float(k.start), float(k.end)) for k in keeps if k.end > k.start])
    if length <= 0:
        return []
    if not islands:
        return [Segment(0.0, float(length))]
    return [Segment(s.start, s.end) for s in invert_to_keeps(float(length), islands, min_keep=0.0)]


def to_segment_domain(segments: list[Segment], seg_in: float, seg_out: float) -> list[Segment]:
    """Slice source-time segments to [seg_in, seg_out] and shift them to segment-local time."""
    out = []
    for s in segments:
        a, b = max(float(s.start), seg_in), min(float(s.end), seg_out)
        if b > a:
            out.append(Segment(a - seg_in, b - seg_in))
    return out


def resize_island(keeps: list[Segment], index: int, start: float, end: float, length: float,
                  min_len: float = MIN_ISLAND_S) -> list[Segment]:
    """Return keeps with island `index` moved to [start, end], clamped to its neighbours and the track."""
    keeps = sorted((Segment(float(k.start), float(k.end)) for k in keeps), key=lambda s: s.start)
    if not (0 <= index < len(keeps)):
        return keeps
    lo = keeps[index - 1].end if index > 0 else 0.0
    hi = keeps[index + 1].start if index + 1 < len(keeps) else float(length)
    a = max(lo, min(float(start), hi - min_len))
    b = min(hi, max(float(end), a + min_len))
    keeps[index] = Segment(a, b)
    return keeps


def island_metadata(report: dict[str, Any], seg_in: float, seg_out: float) -> list[dict[str, Any]]:
    """JSON-safe per-island info (game index, result, smart boundaries, resolver) in segment time."""
    out = []
    for island in report.get("keep_islands", []) or []:
        a, b = max(float(island["start"]), seg_in), min(float(island["end"]), seg_out)
        if b <= a:
            continue
        game = island.get("game") or {}
        decisions = island.get("decisions") or {}
        out.append({
            "start": a - seg_in, "end": b - seg_in,
            "game_id": game.get("id"), "game_index": game.get("index"), "result": game.get("result", "WIN"),
            "start_source": island.get("start_source"), "end_source": island.get("end_source"),
            "resolvers": {k: (v or {}).get("resolver") for k, v in decisions.items()},
            "adjustments": list(island.get("adjustments") or []),
        })
    return out


def remove_gameplay_island(tracks: list[Any], track_index: int, island_index: int) -> list[int]:
    """Remove a kept game, including its split fragments; retain original detection for reset/undo."""
    from analysis.classic import subtract_segments

    if not 0 <= track_index < len(tracks):
        return []
    target = tracks[track_index]
    if not 0 <= island_index < len(target.gameplay_keeps):
        return []

    def metadata(track: Any) -> list[dict[str, Any]]:
        rows = list(track.gameplay_islands or [])
        if len(rows) == len(track.gameplay_keeps):
            return rows
        return [max(rows, key=lambda m: max(0.0, min(k.end, float(m.get("end", 0)))
                                            - max(k.start, float(m.get("start", 0)))), default={})
                for k in track.gameplay_keeps]

    def identity(row: dict[str, Any]) -> tuple[str, str] | None:
        if row.get("game_id") is not None:
            return "id", str(row["game_id"])
        if row.get("game_index") is not None:
            return "index", str(row["game_index"])
        return None

    key = identity(metadata(target)[island_index])
    source = Path(target.path).resolve() if target.path else None
    affected = []
    for ts, track in enumerate(tracks):
        if ts != track_index and (source is None or not track.path or Path(track.path).resolve() != source):
            continue
        rows = metadata(track)
        remove = {i for i, row in enumerate(rows) if (ts == track_index and i == island_index)
                  or (key is not None and identity(row) == key)}
        if not remove:
            continue
        removed = [k for i, k in enumerate(track.gameplay_keeps) if i in remove]
        track.gameplay_keeps = [k for i, k in enumerate(track.gameplay_keeps) if i not in remove]
        track.gameplay_islands = [row for i, row in enumerate(rows) if i not in remove]
        length = float(track.segment_source_out) - float(track.segment_source_in)
        if length <= 0:
            length = float(track.duration)
        track.gameplay_mode = "keep"
        track.gameplay_cuts_enabled = True
        track.gameplay_cuts = complement(track.gameplay_keeps, length)
        # A previous Restore-cut action must not reintroduce footage of the removed game.
        track.suppressed_cuts = subtract_segments(track.suppressed_cuts or [], removed)
        affected.append(ts)
    return affected


class GameplayEditWorker(QObject):
    """Runs GameplayEditingService on a QThread. Results are plain data (thread-safe to hand over)."""

    progress = Signal(int)
    phase = Signal(str)
    finished = Signal(object)  # {"video": str, "keeps": [(start, end)], "report": dict}
    failed = Signal(str)

    def __init__(self, video: str, boundary_mode: str = "exact", end_policy: str = "balanced",
                 *, previous_grouping: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.video = str(video)
        self.boundary_mode = str(boundary_mode)
        self.end_policy = str(end_policy)
        self.previous_grouping = deepcopy(previous_grouping or {})
        self._token = AnalysisCancellation()

    def cancel(self) -> None:
        self._token.cancel()

    def run(self) -> None:
        try:
            from analysis.gameplay_editing import GameplayEditingService, GameplayEditSettings

            from analysis.boundaries.resolvers import ResolverOptions

            settings = GameplayEditSettings(game="hearthstone", keep_games="wins", boundary_mode=self.boundary_mode,
                                            resolver=ResolverOptions(end_policy=self.end_policy))
            result = GameplayEditingService(settings).run(
                self.video, cancellation=self._token,
                on_progress=lambda value: self.progress.emit(int(value * .9)),
            )
            keeps = [(float(k.start), float(k.end)) for k in result.gameplay_keeps]
            report = result.report()
            payload: dict[str, Any] = {"video": self.video, "keeps": keeps, "report": report}
            self._token.raise_if_cancelled()
            if report.get("games"):
                self.phase.emit("Analyzing player classes…")
                try:
                    from analysis.hero_groups.recognition import HeroGroupingService

                    payload["grouping"] = HeroGroupingService().analyze(
                        Path(self.video), report["games"], duration=float(report["duration"]),
                        previous=self.previous_grouping, cancellation=self._token,
                        on_progress=lambda value: self.progress.emit(90 + int(value * .1)),
                    )
                except Exception as exc:
                    self._token.raise_if_cancelled()
                    if exc.__class__.__name__ in ("AnalysisCancelled", "PipelineAnalysisCancelled"):
                        raise
                    # A class-reference/media problem must not discard detected games.
                    payload["grouping_error"] = f"Player classes: {exc.__class__.__name__}: {exc}"
            self._token.raise_if_cancelled()
            self.progress.emit(100)
            self.finished.emit(payload)
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI as a readable message
            if self._token.is_cancelled or exc.__class__.__name__ in ("AnalysisCancelled", "PipelineAnalysisCancelled"):
                self.failed.emit("cancelled")
            else:
                self.failed.emit(f"{exc.__class__.__name__}: {exc}")
