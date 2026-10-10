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
            "game_index": game.get("index"), "result": game.get("result", "WIN"),
            "start_source": island.get("start_source"), "end_source": island.get("end_source"),
            "resolvers": {k: (v or {}).get("resolver") for k, v in decisions.items()},
            "adjustments": list(island.get("adjustments") or []),
        })
    return out


class GameplayEditWorker(QObject):
    """Runs GameplayEditingService on a QThread. Results are plain data (thread-safe to hand over)."""

    progress = Signal(int)
    finished = Signal(object)  # {"video": str, "keeps": [(start, end)], "report": dict}
    failed = Signal(str)

    def __init__(self, video: str, boundary_mode: str = "exact") -> None:
        super().__init__()
        self.video = str(video)
        self.boundary_mode = str(boundary_mode)
        self._token = AnalysisCancellation()

    def cancel(self) -> None:
        self._token.cancel()

    def run(self) -> None:
        try:
            from analysis.gameplay_editing import GameplayEditingService, GameplayEditSettings

            settings = GameplayEditSettings(game="hearthstone", keep_games="wins", boundary_mode=self.boundary_mode)
            result = GameplayEditingService(settings).run(self.video, cancellation=self._token,
                                                          on_progress=self.progress.emit)
            keeps = [(float(k.start), float(k.end)) for k in result.gameplay_keeps]
            self.finished.emit({"video": self.video, "keeps": keeps, "report": result.report()})
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI as a readable message
            if self._token.is_cancelled or exc.__class__.__name__ in ("AnalysisCancelled", "PipelineAnalysisCancelled"):
                self.failed.emit("cancelled")
            else:
                self.failed.emit(f"{exc.__class__.__name__}: {exc}")
