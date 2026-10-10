"""Timestamped transcript of the KEPT content only, through the shared faster-whisper engine.

Kept pieces of each source are merged across short cuts into spans, the spans are
split on a fixed source-time grid (with a small overlap) and every chunk goes
through analysis.boundaries.transcript.FasterWhisperEngine, which caches each
chunk by video identity + window + engine options. Grid-aligned chunks keep their
cache when the edit changes elsewhere. Words are then clipped to the keeps: a word
whose midpoint falls in removed content is dropped, and a cut splits a sentence
into separate lines, so nothing removed from the video leaks into the transcript.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

from analysis.audio_service import AnalysisCancellation
from analysis.boundaries.models import TimeRange, TranscriptSegment
from analysis.boundaries.transcript import FasterWhisperEngine, TranscriptionEngine, TranscriptSettings

from .cache import PackagingCache, stable_key
from .models import TranscriptLine
from .timeline import EditMap

GRID_S = 120.0
OVERLAP_S = 3.0
MERGE_GAP_S = 20.0
LINES_FORMAT = 2  # v2: line text keeps word spacing when the engine strips word text


@dataclass(frozen=True)
class Chunk:
    source_index: int
    window: TimeRange
    own_start: float
    own_end: float


def plan_chunks(edit_map: EditMap, *, grid_s: float = GRID_S, overlap_s: float = OVERLAP_S,
                merge_gap_s: float = MERGE_GAP_S) -> list[Chunk]:
    chunks: list[Chunk] = []
    for index in range(len(edit_map.sources)):
        for a, b in edit_map.spans(index, merge_gap=merge_gap_s):
            cell = math.floor(a / grid_s)
            while cell * grid_s < b:
                own_a, own_b = max(a, cell * grid_s), min(b, (cell + 1) * grid_s)
                if own_b - own_a > 0.05:
                    window = TimeRange(max(a, own_a - overlap_s), min(b, own_b + overlap_s))
                    chunks.append(Chunk(index, window, own_a, own_b))
                cell += 1
    return chunks


def _words(segment: TranscriptSegment) -> list[tuple[float, float, str, float | None]]:
    if segment.words:
        return [(w.start, w.end, w.text, w.probability) for w in segment.words]
    return [(segment.start, segment.end, segment.text, None)]  # engines without word timing


def clip_to_keeps(edit_map: EditMap, chunk: Chunk, segments: Sequence[TranscriptSegment]) -> list[dict[str, Any]]:
    """Kept words of one chunk grouped into lines (dicts without ids)."""
    lines: list[dict[str, Any]] = []
    for segment in segments:
        current: dict[str, Any] | None = None
        for start, end, text, probability in _words(segment):
            middle = (start + end) / 2.0
            if not chunk.own_start <= middle < chunk.own_end:
                continue  # owned by the neighbouring chunk (overlap)
            piece = edit_map.piece_at_source(middle, chunk.source_index)
            if piece is None:
                current = None  # removed content splits the sentence
                continue
            if current is None or current["piece"] is not piece:
                current = {"piece": piece, "segment": segment, "words": []}
                lines.append(current)
            s, e = max(start, piece.source_start), min(end, piece.source_end)
            current["words"].append({"source_start": round(s, 3), "source_end": round(max(s, e), 3),
                                     "edited_start": round(piece.edited_start + s - piece.source_start, 3),
                                     "edited_end": round(piece.edited_start + max(s, e) - piece.source_start, 3),
                                     "text": text, "probability": None if probability is None else round(probability, 3)})
    out = []
    for line in lines:
        words = line["words"]
        segment = line["segment"]
        if not segment.words or len(words) == len(segment.words):
            text = segment.text.strip()  # whole sentence kept: ASR text with its original spacing
        elif any(w["text"][:1].isspace() for w in words):
            text = "".join(w["text"] for w in words).strip()
        else:  # engines that strip word text (the shared helper does)
            text = " ".join(w["text"].strip() for w in words)
        if not text:
            continue
        out.append({"source_index": chunk.source_index, "source_start": words[0]["source_start"],
                    "source_end": words[-1]["source_end"], "edited_start": words[0]["edited_start"],
                    "edited_end": words[-1]["edited_end"], "text": text,
                    "confidence": line["segment"].confidence, "words": words})
    return out


def build_transcript(edit_map: EditMap, settings: TranscriptSettings, *, cache: PackagingCache,
                     engine: TranscriptionEngine | None = None,
                     cancellation: AnalysisCancellation | None = None) -> tuple[dict[str, Any], list[TranscriptLine]]:
    """Return (status, lines sorted by edited time). A missing ASR engine yields no lines, never fake text."""
    options = settings.engine_options()
    key = stable_key("transcript", LINES_FORMAT, edit_map.digest(), options, GRID_S, OVERLAP_S, MERGE_GAP_S)
    cached = cache.load_json("transcript", key)
    if isinstance(cached, dict) and isinstance(cached.get("lines"), list):
        return ({**cached["status"], "packaging_cache": "hit"},
                [TranscriptLine.from_mapping(item) for item in cached["lines"]])
    if not settings.enabled:
        return {"status": "disabled", "engine": "", "detail": "transcription disabled"}, []
    engine = engine or FasterWhisperEngine(settings)
    chunks = plan_chunks(edit_map)
    raw_lines: list[dict[str, Any]] = []
    statuses: list[dict[str, Any]] = []
    for index, source in enumerate(edit_map.sources):
        mine = [c for c in chunks if c.source_index == index]
        if not mine:
            continue
        status, per_window = engine.transcribe(source, [c.window for c in mine], cancellation=cancellation)
        statuses.append(status.to_mapping())
        for chunk, segments in zip(mine, per_window):
            raw_lines.extend(clip_to_keeps(edit_map, chunk, segments))
    raw_lines.sort(key=lambda item: item["edited_start"])
    lines = [TranscriptLine(f"T{i + 1:04d}", item["source_start"], item["source_end"], item["edited_start"],
                            item["edited_end"], item["text"], item["confidence"], tuple(item["words"]),
                            item["source_index"]) for i, item in enumerate(raw_lines)]
    worst = "ok"
    for item in statuses:
        if item["status"] in ("failed", "unavailable"):
            worst = item["status"]
        elif item["status"] == "cached" and worst == "ok":
            worst = "cached"
    summary = {"status": worst if statuses else "ok", "engine": "faster-whisper", "chunks": len(chunks),
               "detail": "; ".join(s.get("detail", "") for s in statuses if s.get("detail")),
               "options": options}
    if worst not in ("failed", "unavailable"):
        cache.store_json("transcript", key, {"status": summary, "lines": [line.to_mapping() for line in lines]})
    return summary, lines
