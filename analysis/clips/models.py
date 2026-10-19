"""Plain data of the clips feature (JSON round-trip, no Qt)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

FORMAT = "recut_clips"
VERSION = 1
FORMATS = ("horizontal", "vertical")  # 16:9 1080p | Shorts 9:16 (webcam + board, captions)


class ClipError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class ClipCandidate:
    """A proposed clip on the EDITED timeline; parts are the kept source intervals that compose it."""

    id: str
    edited_start: float
    edited_end: float
    score: float
    reason: str
    text: str = ""
    highlight_id: str | None = None
    game_id: str | None = None
    result: str | None = None
    frame: str | None = None  # preview image, relative to the analysis folder
    parts: list[tuple[int, float, float]] = field(default_factory=list)  # (source index, start, end)
    selected: bool = False
    notes: list[str] = field(default_factory=list)
    signals: dict[str, float] = field(default_factory=dict)  # local scoring features (explainable ranking)
    title: str = ""  # AI rerank: short title / hook
    ai: dict[str, Any] = field(default_factory=dict)  # AI rerank: hook/flow/value/emotion/overall/reason

    @property
    def duration(self) -> float:
        return self.edited_end - self.edited_start

    def to_mapping(self) -> dict[str, Any]:
        out = asdict(self)
        out["parts"] = [[i, round(a, 3), round(b, 3)] for i, a, b in self.parts]
        out["edited_start"], out["edited_end"] = round(self.edited_start, 3), round(self.edited_end, 3)
        out["duration"] = round(self.duration, 3)
        out["score"] = round(self.score, 3)
        return out

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> ClipCandidate:
        known = {k: raw[k] for k in cls.__dataclass_fields__ if k in raw}
        known["parts"] = [(int(i), float(a), float(b)) for i, a, b in raw.get("parts") or []]
        return cls(**known)
