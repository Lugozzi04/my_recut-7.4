"""Evidence and proposal records shared by the packaging stages (all JSON-serialisable)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# Bump when the meaning of a stored artifact changes; part of every cache key.
EVIDENCE_VERSION = "packaging_evidence_v1"


class PackagingError(RuntimeError):
    """Recoverable packaging failure with a stable code (CLI exit code 20 = analysis)."""

    exit_code = 20

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        if code in ("invalid_arguments", "output_exists", "no_previous_run", "creator_not_found"):
            self.exit_code = 2  # CLI usage errors share automation.cli.EXIT_ARGUMENTS


def r3(value: float) -> float:
    return round(float(value), 3)


@dataclass(frozen=True)
class TranscriptLine:
    """One transcribed sentence/segment restricted to kept content."""

    id: str
    source_start: float
    source_end: float
    edited_start: float
    edited_end: float
    text: str
    confidence: float | None = None
    words: tuple[dict[str, Any], ...] = ()
    source_index: int = 0

    def to_mapping(self, *, words: bool = True) -> dict[str, Any]:
        value = {"id": self.id, "source_start": r3(self.source_start), "source_end": r3(self.source_end),
                 "edited_start": r3(self.edited_start), "edited_end": r3(self.edited_end), "text": self.text,
                 "confidence": None if self.confidence is None else round(self.confidence, 3),
                 "source_index": self.source_index}
        if words:
            value["words"] = [dict(w) for w in self.words]
        return value

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> TranscriptLine:
        return cls(str(raw["id"]), float(raw["source_start"]), float(raw["source_end"]), float(raw["edited_start"]),
                   float(raw["edited_end"]), str(raw["text"]),
                   None if raw.get("confidence") is None else float(raw["confidence"]),
                   tuple(dict(w) for w in raw.get("words", ())), int(raw.get("source_index", 0)))


@dataclass
class FrameRecord:
    """A full-resolution frame saved under frames/ (or a webcam/card crop)."""

    id: str
    kind: str  # gameplay | webcam | card | deepen
    source_index: int
    source_time: float
    edited_time: float | None
    path: str  # relative to the output directory
    width: int
    height: int
    quality: dict[str, float] = field(default_factory=dict)
    highlight_id: str | None = None
    role: str = ""  # before | peak | after | coverage | best
    notes: str = ""

    def to_mapping(self) -> dict[str, Any]:
        value = asdict(self)
        value["source_time"] = r3(self.source_time)
        value["edited_time"] = None if self.edited_time is None else r3(self.edited_time)
        value["quality"] = {k: round(float(v), 4) for k, v in self.quality.items()}
        return value

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> FrameRecord:
        return cls(**{k: raw[k] for k in ("id", "kind", "source_index", "source_time", "edited_time", "path",
                                          "width", "height")},
                   quality=dict(raw.get("quality", {})), highlight_id=raw.get("highlight_id"),
                   role=str(raw.get("role", "")), notes=str(raw.get("notes", "")))


@dataclass
class HighlightCandidate:
    id: str
    game_id: str | None
    source_index: int
    source_start: float
    source_end: float
    edited_start: float
    edited_end: float
    peak_source: float
    evidence_sources: list[str]
    audio: dict[str, float]
    transcript_ids: list[str]
    transcript_context: str
    reason: str
    local_score: float
    visual: dict[str, Any] = field(default_factory=dict)
    frame_ids: list[str] = field(default_factory=list)
    webcam_frame_ids: list[str] = field(default_factory=list)
    coverage: bool = False  # added so that every kept game has at least one candidate

    def to_mapping(self) -> dict[str, Any]:
        value = asdict(self)
        for key in ("source_start", "source_end", "edited_start", "edited_end", "peak_source"):
            value[key] = r3(value[key])
        value["audio"] = {k: round(float(v), 3) for k, v in self.audio.items()}
        value["local_score"] = round(self.local_score, 3)
        return value

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> HighlightCandidate:
        names = cls.__dataclass_fields__.keys()
        return cls(**{k: raw[k] for k in names if k in raw})


@dataclass
class GameContext:
    game_id: str
    index: int | None
    result: str | None
    source_index: int
    source_start: float
    source_end: float
    edited_start: float | None
    edited_end: float | None
    kept_seconds: float
    origin: str  # project_gameplay | detector | whole_video
    transcript_ids: list[str] = field(default_factory=list)
    transcript_excerpt: str = ""
    highlight_ids: list[str] = field(default_factory=list)
    frame_ids: list[str] = field(default_factory=list)
    markers: dict[str, Any] = field(default_factory=dict)
    interpretation: dict[str, Any] = field(default_factory=dict)  # filled from the VideoStory (agent)

    def to_mapping(self) -> dict[str, Any]:
        value = asdict(self)
        for key in ("source_start", "source_end", "edited_start", "edited_end", "kept_seconds"):
            if value[key] is not None:
                value[key] = r3(value[key])
        return value

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> GameContext:
        names = cls.__dataclass_fields__.keys()
        return cls(**{k: raw[k] for k in names if k in raw})
