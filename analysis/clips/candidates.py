"""Packaging highlights -> clip candidates.

A highlight is a 15-25 s window around an audio/visual peak. A clip must also be watchable on its own:
it starts at the beginning of a sentence, ends after a sentence, lasts 20-60 s of edited content, stays
inside one game and avoids the channel intro/outro (greetings, "lasciate un like").
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import ClipCandidate, ClipError

LEAD_S = 0.15  # breath before the first word
TAIL_S = 0.35  # room after the last word
SENTENCE_GRACE_S = 6.0  # how far a sentence may be followed beyond the highlight (sparse ASR punctuation)
MIN_S, MAX_S = 20.0, 60.0
SENTENCE_END = re.compile(r"[.!?…]['\")\]]*$")
CTA = re.compile(r"\b(like|follow|iscriv\w*|buona visione|campanella|ci vediamo|alla prossima|"
                 r"bentornat\w*|ciao a tutti|grazie per la visione)\b", re.IGNORECASE)
MAX_OVERLAP = 0.3  # share of the shorter clip


@dataclass
class Line:
    start: float  # edited seconds
    end: float
    text: str
    words: list[tuple[float, float, str]] = field(default_factory=list)  # edited times (Whisper word timestamps)


@dataclass
class Analysis:
    """The packaging analysis of one edit, read from its output folder."""

    folder: Path
    sources: list[str]
    pieces: list[tuple[int, float, float, float]]  # (source index, source start, source end, edited start)
    digest: str
    lines: list[Line]
    highlights: list[dict[str, Any]]
    games: dict[str, dict[str, Any]] = field(default_factory=dict)
    frames: dict[str, str] = field(default_factory=dict)  # frame id -> path relative to the folder
    webcam_roi: tuple[float, float, float, float] | None = None  # normalized x, y, w, h of the stream webcam

    @property
    def duration(self) -> float:
        return max((p[3] + p[2] - p[1] for p in self.pieces), default=0.0)

    @classmethod
    def load(cls, folder: str | Path) -> Analysis:
        folder = Path(folder)

        def read(name: str) -> Any:
            path = folder / name
            if not path.is_file():
                raise ClipError("analysis_missing", f"{name} is missing in {folder}")
            return json.loads(path.read_text(encoding="utf-8"))

        content = read("content_analysis.json")
        timeline = content["timeline"]
        pieces = [(int(p["source_index"]), float(p["source_start"]), float(p["source_end"]), float(p["edited_start"]))
                  for p in timeline["pieces"]]
        lines = [Line(float(raw["edited_start"]), float(raw["edited_end"]), " ".join(str(raw.get("text", "")).split()),
                      [(float(w["edited_start"]), float(w["edited_end"]), str(w.get("text", "")).strip())
                       for w in raw.get("words") or []
                       if w.get("edited_start") is not None and w.get("edited_end") is not None])
                 for raw in (read("transcript.json").get("lines") or [])
                 if raw.get("edited_start") is not None and raw.get("edited_end") is not None]
        games: dict[str, dict[str, Any]] = {}
        if (folder / "game_contexts.json").is_file():
            games = {str(g["game_id"]): g for g in read("game_contexts.json").get("games") or []}
        frames = {str(f["id"]): str(f["path"]) for f in content.get("frames") or [] if f.get("path")}
        roi = ((content.get("evidence") or {}).get("webcam") or {}).get("roi")
        webcam = tuple(float(v) for v in roi) if isinstance(roi, list) and len(roi) == 4 else None
        return cls(folder, list(timeline["sources"]), pieces, str(timeline.get("digest") or ""),
                   sorted(lines, key=lambda line: line.start), list(read("highlights.json").get("highlights") or []),
                   games, frames, webcam)  # type: ignore[arg-type]

    def source_parts(self, start: float, end: float) -> list[tuple[int, float, float]]:
        """Kept source intervals composing the edited range [start, end], in edited order."""
        parts = []
        for index, s0, s1, e0 in self.pieces:
            e1 = e0 + (s1 - s0)
            a, b = max(start, e0), min(end, e1)
            if b - a > 1e-3:
                parts.append((index, s0 + a - e0, s0 + b - e0))
        return parts


def _covering(lines: list[Line], t: float) -> int | None:
    for n, line in enumerate(lines):
        if line.start - 1e-3 <= t < line.end + 1e-3:
            return n
    return None


def _snap(lines: list[Line], start: float, end: float, *, lo: float, hi: float,
          min_s: float, max_s: float) -> tuple[float, float]:
    """[start, end] widened to whole sentences inside [lo, hi], then fitted into [min_s, max_s]."""
    inside = [n for n, line in enumerate(lines) if line.end > lo and line.start < hi]
    if not inside:
        return max(lo, start), min(hi, max(end, start + min_s))
    window = [lines[n] for n in inside]
    first = _covering(window, start)
    if first is None:
        first = next((n for n, line in enumerate(window) if line.start >= start), len(window) - 1)
    last = _covering(window, end)
    if last is None:
        last = max((n for n, line in enumerate(window) if line.end <= end), default=first)
    last = max(last, first)
    while first > 0 and not SENTENCE_END.search(window[first - 1].text) and \
            window[first - 1].start >= start - SENTENCE_GRACE_S and \
            window[last].end - window[first - 1].start <= max_s:
        first -= 1  # do not start in the middle of a sentence
    while last + 1 < len(window) and not SENTENCE_END.search(window[last].text) and \
            window[last + 1].end <= end + SENTENCE_GRACE_S and \
            window[last + 1].end - window[first].start <= max_s:
        last += 1  # finish the sentence
    while window[last].end - window[first].start < min_s:
        after = window[last + 1].end - window[first].start if last + 1 < len(window) else None
        before = window[last].end - window[first - 1].start if first > 0 else None
        if after is not None and after <= max_s:
            last += 1
        elif before is not None and before <= max_s:
            first -= 1
        else:
            break
    while last > first and window[last].end - window[first].start > max_s:
        last -= 1
    a = max(lo, window[first].start - LEAD_S)
    b = min(hi, window[last].end + TAIL_S)
    if b - a < min_s:  # too little speech: pad with the surrounding action
        b = min(hi, a + min_s)
        a = max(lo, b - min_s)
    return a, min(b, a + max_s)


MIN_TRIM_S = 3.0


def retime(analysis: Analysis, clip: ClipCandidate, start: float, end: float) -> ClipCandidate:
    """A user trim: new edited range (clamped to the edit, at least MIN_TRIM_S), its kept parts and text."""
    total = analysis.duration
    start = min(max(0.0, start), max(0.0, total - MIN_TRIM_S))
    end = min(max(end, start + MIN_TRIM_S), total)
    clip.edited_start, clip.edited_end = start, end
    clip.parts = analysis.source_parts(start, end)
    clip.text = " ".join(line.text for line in analysis.lines if line.end > start and line.start < end)[:400]
    return clip


def build_candidates(analysis: Analysis, *, count: int = 10, min_s: float = MIN_S,
                     max_s: float = MAX_S) -> list[ClipCandidate]:
    if not 5 <= min_s < max_s:
        raise ClipError("invalid_arguments", "Clip length must satisfy 5 <= min < max seconds.")
    total = analysis.duration
    found: list[ClipCandidate] = []
    for h in analysis.highlights:
        game = analysis.games.get(str(h.get("game_id")))
        lo, hi = 0.0, total
        if game and game.get("edited_start") is not None and game.get("edited_end") is not None:
            lo, hi = float(game["edited_start"]), float(game["edited_end"])  # never across two games
        start, end = _snap(analysis.lines, float(h["edited_start"]), float(h["edited_end"]), lo=lo, hi=hi,
                           min_s=min_s, max_s=max_s)
        if end - start < min(min_s, 10.0):
            continue
        text = " ".join(line.text for line in analysis.lines if line.end > start and line.start < end)
        score, notes = float(h.get("local_score") or 0.0), []
        if CTA.search(text):
            score -= 0.6
            notes.append("greeting / call to action")
        if total and (end < total * 0.04 or start > total * 0.97):
            score -= 0.4
            notes.append("channel intro or outro")
        frame_ids = list(h.get("frame_ids") or [])
        found.append(ClipCandidate(
            id="", edited_start=start, edited_end=end, score=score, reason=str(h.get("reason") or ""),
            text=text[:400], highlight_id=h.get("id"), game_id=h.get("game_id"),
            result=(game or {}).get("result"), frame=analysis.frames.get(frame_ids[0]) if frame_ids else None,
            parts=analysis.source_parts(start, end), notes=notes))
    chosen: list[ClipCandidate] = []
    for clip in sorted(found, key=lambda c: -c.score):
        overlap = any(min(clip.edited_end, o.edited_end) - max(clip.edited_start, o.edited_start)
                      > MAX_OVERLAP * min(clip.duration, o.duration) for o in chosen)
        if not overlap:
            chosen.append(clip)
        if len(chosen) >= count:
            break
    for n, clip in enumerate(chosen, start=1):
        clip.id = f"C{n:02d}"
    return chosen
