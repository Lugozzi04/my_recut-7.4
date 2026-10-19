"""Clip finder evaluation: the proposals against moments the creator marked by hand.

A marks file lists, one per line, the moments you would have clipped, in the time of the EDITED video (the
exported MP4 you watch): `12:34`, `1:02:03` or `754` for an instant, `12:30-13:05` for a range, followed by an
optional note; `#` starts a comment. Each scoring profile proposes `count` clips on the same analysis and
signals, and is measured by:
  recall     share of the marked moments that a proposal covers,
  precision  share of the proposals that cover a marked moment,
  ranks      for every mark, the rank of the first proposal covering it (None: missed).
An instant is covered when it falls inside the clip (POINT_SLACK_S of tolerance); a range when the overlap is at
least half of the shorter of the range and the clip.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .models import ClipCandidate, ClipError

POINT_SLACK_S = 2.0
_TIME = r"(?:\d+:)?\d{1,2}:\d{2}(?:\.\d+)?|\d+(?:\.\d+)?"
_MARK = re.compile(rf"^\s*({_TIME})(?:\s*-\s*({_TIME}))?\s*(.*)$")


@dataclass
class Mark:
    start: float
    end: float | None  # None: an instant
    note: str = ""

    def label(self) -> str:
        span = clock(self.start) + (f"-{clock(self.end)}" if self.end is not None else "")
        return f"{span} {self.note}".strip()


def seconds(text: str) -> float:
    value = 0.0
    for part in text.split(":"):
        value = value * 60 + float(part)
    return value


def clock(t: float) -> str:
    t = int(round(t))
    return f"{t // 3600}:{t // 60 % 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


def parse_marks(text: str) -> list[Mark]:
    marks = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        match = _MARK.match(line)
        if match is None:
            raise ClipError("invalid_marks", f"Line {number}: '{raw.strip()}' is not a time (12:34 or 12:30-13:05)")
        start = seconds(match.group(1))
        end = seconds(match.group(2)) if match.group(2) else None
        if end is not None and end <= start:
            raise ClipError("invalid_marks", f"Line {number}: the range ends before it starts")
        marks.append(Mark(start, end, match.group(3).strip()))
    if not marks:
        raise ClipError("invalid_marks", "The marks file lists no moment")
    return marks


def load_marks(path: str | Path) -> list[Mark]:
    try:
        return parse_marks(Path(path).expanduser().read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise ClipError("invalid_marks", f"Cannot read the marks file {path}: {exc}") from exc


def covers(clip: ClipCandidate, mark: Mark) -> bool:
    if mark.end is None:
        return clip.edited_start - POINT_SLACK_S <= mark.start <= clip.edited_end + POINT_SLACK_S
    overlap = min(clip.edited_end, mark.end) - max(clip.edited_start, mark.start)
    return overlap >= 0.5 * min(mark.end - mark.start, clip.duration)


def score(clips: Sequence[ClipCandidate], marks: Sequence[Mark]) -> dict[str, Any]:
    ranks = [next((n for n, clip in enumerate(clips, start=1) if covers(clip, mark)), None) for mark in marks]
    useful = sum(1 for clip in clips if any(covers(clip, mark) for mark in marks))
    found = [r for r in ranks if r is not None]
    return {"proposals": len(clips), "recall": round(len(found) / len(marks), 3),
            "precision": round(useful / len(clips), 3) if clips else 0.0,
            "found": len(found), "marks": len(marks), "ranks": ranks,
            "mean_rank": round(sum(found) / len(found), 2) if found else None}


def report(results: dict[str, dict[str, Any]], marks: Sequence[Mark]) -> str:
    """Plain-text table: one row per profile, then which proposal found each mark."""
    names = list(results)
    rows = [f"{'profile':<10} {'recall':>7} {'precision':>9} {'found':>7} {'mean rank':>9}"]
    for name in names:
        r = results[name]
        rows.append(f"{name:<10} {r['recall']:>7.0%} {r['precision']:>9.0%} {r['found']:>3}/{r['marks']:<3} "
                    f"{r['mean_rank'] if r['mean_rank'] is not None else '-':>9}")
    rows.append("")
    rows.append(f"{'mark':<34}" + "".join(f"{name:>10}" for name in names))
    for n, mark in enumerate(marks):
        rows.append(f"{mark.label()[:33]:<34}" + "".join(
            f"{'#' + str(results[name]['ranks'][n]) if results[name]['ranks'][n] else 'missed':>10}" for name in names))
    return "\n".join(rows)
