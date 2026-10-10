"""Precise source <-> edited timestamp map for the content that survives the edit.

A project is mapped through the exporter's own ProjectExportPlanner, so the
packaging timeline is exactly the timeline of the rendered video (same final
keeps, same order, same clamping). A plain MP4 maps 1:1 onto itself.
"""
from __future__ import annotations

import hashlib
import json
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, cast

from .models import PackagingError, r3

EPS = 1e-6


@dataclass(frozen=True)
class KeepPiece:
    source_index: int
    source_start: float
    source_end: float
    edited_start: float

    @property
    def duration(self) -> float:
        return self.source_end - self.source_start

    @property
    def edited_end(self) -> float:
        return self.edited_start + self.duration

    def to_mapping(self) -> dict[str, Any]:
        return {"source_index": self.source_index, "source_start": r3(self.source_start),
                "source_end": r3(self.source_end), "edited_start": r3(self.edited_start),
                "edited_end": r3(self.edited_end)}


class EditMap:
    def __init__(self, sources: list[str], pieces: list[KeepPiece], *, origin: str,
                 project_path: str | None = None, identities: list[dict[str, Any]] | None = None) -> None:
        if not pieces:
            raise PackagingError("empty_edit", "The edit keeps no content: nothing to package.")
        self.sources = list(sources)
        self.pieces = list(pieces)
        self.origin = origin
        self.project_path = project_path
        self.identities = identities or []
        self._edited_starts = [p.edited_start for p in self.pieces]

    # ---- construction ----------------------------------------------------------------------------
    @classmethod
    def from_project(cls, project: str | Path, *, planner: Any = None) -> EditMap:
        from automation.exporter import PipelineExportError, ProjectExportPlanner

        path = Path(project).expanduser().resolve()
        if not path.is_file():
            raise PackagingError("project_not_found", f"Project not found: {path}")
        try:
            plan = (planner or ProjectExportPlanner()).build(cast(Any, SimpleNamespace(project_path=str(path))))
        except PipelineExportError as exc:
            raise PackagingError(exc.code, f"Could not read the project edit: {exc}") from exc
        sources = [str(Path(p).resolve()) for p in plan.input_paths]
        pieces: list[KeepPiece] = []
        cursor = 0.0
        if plan.keeps:
            spans = [(0, float(k.start), float(k.end)) for k in plan.keeps]
        else:
            spans = [(int(s["v_idx"]), float(s["v_in"]), float(s["v_out"])) for s in plan.segments]
        for index, start, end in spans:
            if end - start <= EPS:
                continue
            pieces.append(KeepPiece(index, start, end, cursor))
            cursor += end - start
        return cls(sources, pieces, origin="project", project_path=str(path), identities=_identities(sources))

    @classmethod
    def from_video(cls, video: str | Path, *, duration_probe: Callable[[str], float] | None = None) -> EditMap:
        path = Path(video).expanduser().resolve()
        if not path.is_file():
            raise PackagingError("source_not_found", f"Video not found: {path}")
        if duration_probe is None:
            from utils.ffmpeg import ffprobe_duration_seconds
            duration_probe = ffprobe_duration_seconds
        duration = float(duration_probe(str(path)))
        if not duration > 0:
            raise PackagingError("invalid_video", f"Could not read the duration of {path.name}.")
        return cls([str(path)], [KeepPiece(0, 0.0, duration, 0.0)], origin="video", identities=_identities([str(path)]))

    # ---- mapping ---------------------------------------------------------------------------------
    @property
    def duration(self) -> float:
        return self.pieces[-1].edited_end

    def piece_at_source(self, source_t: float, source_index: int = 0) -> KeepPiece | None:
        for piece in self.pieces:
            if piece.source_index == source_index and piece.source_start - EPS <= source_t < piece.source_end + EPS:
                return piece
        return None

    def to_edited(self, source_t: float, source_index: int = 0) -> float | None:
        """Edited time of a kept source instant; None when that instant was cut."""
        piece = self.piece_at_source(source_t, source_index)
        if piece is None:
            return None
        return piece.edited_start + min(max(source_t, piece.source_start), piece.source_end) - piece.source_start

    def to_edited_nearest(self, source_t: float, source_index: int = 0) -> float:
        """Edited time of the closest kept instant (a cut instant maps to the following splice)."""
        exact = self.to_edited(source_t, source_index)
        if exact is not None:
            return exact
        best: tuple[float, float] | None = None
        for piece in self.pieces:
            if piece.source_index != source_index:
                continue
            for edge, edited in ((piece.source_start, piece.edited_start), (piece.source_end, piece.edited_end)):
                distance = abs(edge - source_t)
                if best is None or distance < best[0]:
                    best = (distance, edited)
        if best is None:
            raise PackagingError("unmapped_time", f"source {source_index} has no kept content")
        return best[1]

    def to_source(self, edited_t: float) -> tuple[int, float]:
        edited_t = min(max(0.0, edited_t), self.duration)
        index = max(0, bisect_right(self._edited_starts, edited_t) - 1)
        piece = self.pieces[index]
        offset = min(edited_t - piece.edited_start, piece.duration)
        return piece.source_index, piece.source_start + offset

    def kept_parts(self, source_index: int, start: float, end: float) -> list[tuple[float, float]]:
        parts = []
        for piece in self.pieces:
            if piece.source_index != source_index:
                continue
            a, b = max(start, piece.source_start), min(end, piece.source_end)
            if b - a > EPS:
                parts.append((a, b))
        return parts

    def kept_seconds(self, source_index: int, start: float, end: float) -> float:
        return sum(b - a for a, b in self.kept_parts(source_index, start, end))

    def edited_range(self, source_index: int, start: float, end: float) -> tuple[float, float] | None:
        """Edited span covering the kept part of [start, end]; None when all of it was cut."""
        parts = self.kept_parts(source_index, start, end)
        if not parts:
            return None
        first, last = self.to_edited(parts[0][0], source_index), self.to_edited(parts[-1][1] - EPS, source_index)
        assert first is not None and last is not None
        return first, last

    def spans(self, source_index: int, *, merge_gap: float) -> list[tuple[float, float]]:
        """Kept pieces of one source merged across cuts shorter than merge_gap (ASR/RMS windows)."""
        merged: list[list[float]] = []
        for piece in sorted((p for p in self.pieces if p.source_index == source_index), key=lambda p: p.source_start):
            if merged and piece.source_start - merged[-1][1] <= merge_gap:
                merged[-1][1] = max(merged[-1][1], piece.source_end)
            else:
                merged.append([piece.source_start, piece.source_end])
        return [(a, b) for a, b in merged]

    # ---- identity --------------------------------------------------------------------------------
    def digest(self) -> str:
        value = {"sources": self.identities, "pieces": [p.to_mapping() for p in self.pieces]}
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()

    def range_digest(self, source_index: int, start: float, end: float) -> str:
        """Identity of the keeps inside one source range: unchanged ranges keep their caches."""
        value = {"source": self.identities[source_index] if source_index < len(self.identities) else None,
                 "parts": [[r3(a), r3(b)] for a, b in self.kept_parts(source_index, start, end)]}
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()

    def to_mapping(self) -> dict[str, Any]:
        return {"origin": self.origin, "project": self.project_path, "sources": self.sources,
                "edited_duration": r3(self.duration), "pieces": [p.to_mapping() for p in self.pieces],
                "digest": self.digest()}


def _identities(sources: list[str]) -> list[dict[str, Any]]:
    from analysis.gameplay.cache import video_identity

    return [video_identity(Path(source)) for source in sources]
