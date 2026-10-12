"""Serializable boundary-analysis records; no UI, player or detector state.

A BoundaryContext is everything a resolver may look at for ONE editorial
boundary (START or END of one game): anchors, local evidence (transcript,
silence/speech, visual change, gameplay markers) and candidate timestamps with
raw evidence kept apart from derived numeric features. All timestamps are
absolute seconds in the source video.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
import math
from typing import Any

CONTEXT_FORMAT = "recut_boundary_context"
CONTEXT_VERSION = 1


class BoundaryFormatError(ValueError):
    pass


class BoundaryType(str, Enum):
    START = "START"  # first moment that belongs to the game being removed
    END = "END"  # first moment that no longer belongs to it


class CandidateSource(str, Enum):
    GAME_START_ANCHOR = "GAME_START_ANCHOR"
    RESULT_ANCHOR = "RESULT_ANCHOR"
    PREVIOUS_GAME_END = "PREVIOUS_GAME_END"
    NEXT_GAME_START = "NEXT_GAME_START"
    BOARD_DISAPPEARED = "BOARD_DISAPPEARED"
    BOARD_REACQUIRED = "BOARD_REACQUIRED"
    SILENCE_START = "SILENCE_START"
    SILENCE_END = "SILENCE_END"
    SILENCE_MID = "SILENCE_MID"
    TRANSCRIPT_SEGMENT_START = "TRANSCRIPT_SEGMENT_START"
    TRANSCRIPT_SEGMENT_END = "TRANSCRIPT_SEGMENT_END"
    SENTENCE_START = "SENTENCE_START"
    SENTENCE_END = "SENTENCE_END"
    VISUAL_CHANGE = "VISUAL_CHANGE"
    WORD_GAP = "WORD_GAP"  # gap between two words inside speech (END after the result only): a cut that may end a sentence early
    START_OF_FILE = "START_OF_FILE"
    END_OF_FILE = "END_OF_FILE"


GAMEPLAY_SOURCES = frozenset({
    CandidateSource.GAME_START_ANCHOR, CandidateSource.RESULT_ANCHOR, CandidateSource.PREVIOUS_GAME_END,
    CandidateSource.NEXT_GAME_START, CandidateSource.BOARD_DISAPPEARED, CandidateSource.BOARD_REACQUIRED,
})


class GameTermination(str, Enum):
    """Completeness of a detected game segment, independent of its result enum."""

    COMPLETE = "COMPLETE"  # start and result both observed, result WIN or LOSS
    AMBIGUOUS_RESULT = "AMBIGUOUS_RESULT"  # start and end observed, result UNKNOWN (conflicting outcomes)
    IN_PROGRESS_AT_EOF = "IN_PROGRESS_AT_EOF"  # last game, positive evidence it is still being played at EOF
    MISSING_RESULT = "MISSING_RESULT"  # start observed, end/result not observed (missing end)
    TRUNCATED_AT_START = "TRUNCATED_AT_START"  # first game, positive evidence the file begins mid-game
    MISSING_START = "MISSING_START"  # end observed, start not observed for an unknown reason


class GameDisposition(str, Enum):
    REMOVE = "REMOVE"  # remove between a smart START and a smart END
    REMOVE_FROM_START = "REMOVE_FROM_START"  # file begins mid-game: remove 0 -> smart END
    REMOVE_TO_EOF = "REMOVE_TO_EOF"  # remove smart START -> end of file
    KEEP = "KEEP"


def finite(value: Any, name: str, *, allow_none: bool = False) -> float | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise BoundaryFormatError(f"{name} must be a finite number")
    return float(value)


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise BoundaryFormatError(f"{name} must be an object")
    return value


def _list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise BoundaryFormatError(f"{name} must be a list")
    return value


@dataclass(frozen=True)
class TimeRange:
    start: float
    end: float

    def __post_init__(self) -> None:
        start, end = finite(self.start, "start"), finite(self.end, "end")
        assert start is not None and end is not None
        if end < start:
            raise BoundaryFormatError("range end precedes start")

    @property
    def duration(self) -> float:
        return self.end - self.start

    def contains(self, t: float) -> bool:
        return self.start <= t <= self.end

    def to_mapping(self) -> dict[str, float]:
        return {"start": self.start, "end": self.end}

    @classmethod
    def from_mapping(cls, raw: Any) -> TimeRange:
        raw = _mapping(raw, "range")
        return cls(raw.get("start"), raw.get("end"))


@dataclass(frozen=True)
class TranscriptWord:
    start: float
    end: float
    text: str
    probability: float | None = None

    def to_mapping(self) -> dict[str, Any]:
        return {"start": self.start, "end": self.end, "text": self.text, "probability": self.probability}

    @classmethod
    def from_mapping(cls, raw: Any) -> TranscriptWord:
        raw = _mapping(raw, "word")
        return cls(float(raw["start"]), float(raw["end"]), str(raw["text"]),
                   finite(raw.get("probability"), "probability", allow_none=True))


SENTENCE_FINAL = (".", "?", "!", "…")


@dataclass(frozen=True)
class TranscriptSegment:
    """One ASR segment. confidence is exp(avg_logprob) when available, not a calibrated probability."""

    start: float
    end: float
    text: str
    confidence: float | None = None
    no_speech_probability: float | None = None
    words: tuple[TranscriptWord, ...] = ()

    def __post_init__(self) -> None:
        TimeRange(self.start, self.end)

    @property
    def ends_sentence(self) -> bool:
        return self.text.rstrip().endswith(SENTENCE_FINAL)

    def sentence_boundaries(self) -> list[tuple[float, float]]:
        """(end of sentence-final word, start of the following word) inside this segment."""
        out = []
        for word, following in zip(self.words, self.words[1:]):
            if word.text.rstrip().endswith(SENTENCE_FINAL):
                out.append((word.end, following.start))
        return out

    def to_mapping(self) -> dict[str, Any]:
        return {"start": self.start, "end": self.end, "text": self.text, "confidence": self.confidence,
                "no_speech_probability": self.no_speech_probability,
                "words": [word.to_mapping() for word in self.words]}

    @classmethod
    def from_mapping(cls, raw: Any) -> TranscriptSegment:
        raw = _mapping(raw, "transcript segment")
        return cls(float(raw["start"]), float(raw["end"]), str(raw.get("text", "")),
                   finite(raw.get("confidence"), "confidence", allow_none=True),
                   finite(raw.get("no_speech_probability"), "no_speech_probability", allow_none=True),
                   tuple(TranscriptWord.from_mapping(item) for item in _list(raw.get("words", []), "words")))


@dataclass(frozen=True)
class EvidenceStatus:
    """Whether a modality was analysed; 'unavailable'/'disabled'/'failed' never fabricate data."""

    status: str  # ok | disabled | unavailable | failed | cached
    engine: str = ""
    detail: str = ""
    settings: Mapping[str, Any] = field(default_factory=dict)

    @property
    def available(self) -> bool:
        return self.status in ("ok", "cached")

    def to_mapping(self) -> dict[str, Any]:
        return {"status": self.status, "engine": self.engine, "detail": self.detail, "settings": dict(self.settings)}

    @classmethod
    def from_mapping(cls, raw: Any) -> EvidenceStatus:
        raw = _mapping(raw, "status")
        return cls(str(raw.get("status", "")), str(raw.get("engine", "")), str(raw.get("detail", "")),
                   dict(_mapping(raw.get("settings", {}), "settings")))


@dataclass(frozen=True)
class GameplayMarker:
    timestamp: float
    kind: str  # a CandidateSource value for gameplay-derived markers
    detail: Mapping[str, Any] = field(default_factory=dict)

    def to_mapping(self) -> dict[str, Any]:
        return {"timestamp": self.timestamp, "kind": self.kind, "detail": dict(self.detail)}

    @classmethod
    def from_mapping(cls, raw: Any) -> GameplayMarker:
        raw = _mapping(raw, "marker")
        return cls(float(raw["timestamp"]), str(raw["kind"]), dict(_mapping(raw.get("detail", {}), "detail")))


@dataclass(frozen=True)
class VisualSample:
    """Change between the previous sampled frame and this one (0 = identical, 1 = maximal)."""

    timestamp: float
    change: float

    def to_mapping(self) -> dict[str, float]:
        return {"timestamp": self.timestamp, "change": self.change}

    @classmethod
    def from_mapping(cls, raw: Any) -> VisualSample:
        raw = _mapping(raw, "visual sample")
        return cls(float(raw["timestamp"]), float(raw["change"]))


@dataclass
class CandidateBoundary:
    timestamp: float
    boundary_type: BoundaryType
    sources: tuple[CandidateSource, ...]
    evidence: dict[str, Any] = field(default_factory=dict)  # raw, human/agent readable
    features: dict[str, float | None] = field(default_factory=dict)  # derived numeric, None = unavailable

    def to_mapping(self) -> dict[str, Any]:
        return {"timestamp": self.timestamp, "boundary_type": self.boundary_type.value,
                "sources": [source.value for source in self.sources], "evidence": dict(self.evidence),
                "features": dict(self.features)}

    @classmethod
    def from_mapping(cls, raw: Any) -> CandidateBoundary:
        raw = _mapping(raw, "candidate")
        try:
            return cls(float(raw["timestamp"]), BoundaryType(raw["boundary_type"]),
                       tuple(CandidateSource(item) for item in _list(raw.get("sources", []), "sources")),
                       dict(_mapping(raw.get("evidence", {}), "evidence")),
                       {str(k): finite(v, f"feature {k}", allow_none=True)
                        for k, v in _mapping(raw.get("features", {}), "features").items()})
        except (KeyError, ValueError) as exc:
            raise BoundaryFormatError(f"Invalid candidate: {exc}") from exc


@dataclass
class GameInfo:
    id: str
    index: int
    start: float | None
    end: float | None
    result: str
    termination: GameTermination
    disposition: GameDisposition
    reason: str = ""

    def to_mapping(self) -> dict[str, Any]:
        return {"id": self.id, "index": self.index, "start": self.start, "end": self.end, "result": self.result,
                "termination": self.termination.value, "disposition": self.disposition.value, "reason": self.reason}

    @classmethod
    def from_mapping(cls, raw: Any) -> GameInfo:
        raw = _mapping(raw, "game")
        return cls(str(raw["id"]), int(raw["index"]), finite(raw.get("start"), "start", allow_none=True),
                   finite(raw.get("end"), "end", allow_none=True), str(raw["result"]),
                   GameTermination(raw["termination"]), GameDisposition(raw["disposition"]), str(raw.get("reason", "")))


@dataclass
class BoundaryContext:
    boundary_type: BoundaryType
    video: dict[str, Any]  # path, duration, optional fingerprint
    game: GameInfo
    anchors: dict[str, float | None]  # game_start, game_end, previous_game_end, next_game_start, end_of_file
    window: TimeRange
    window_clipped_by: tuple[str, ...]
    transcript_status: EvidenceStatus
    transcript: list[TranscriptSegment]
    audio_status: EvidenceStatus
    silences: list[TimeRange]
    speech: list[TimeRange]
    visual_status: EvidenceStatus
    visual: list[VisualSample]
    gameplay_markers: list[GameplayMarker]
    candidates: list[CandidateBoundary]
    feature_names: list[str]
    settings: dict[str, Any] = field(default_factory=dict)
    neighbors: dict[str, Any] = field(default_factory=dict)  # previous/next game summaries (optional, v1-compatible)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "format": CONTEXT_FORMAT, "version": CONTEXT_VERSION, "boundary_type": self.boundary_type.value,
            "video": dict(self.video), "game": self.game.to_mapping(), "anchors": dict(self.anchors),
            "window": self.window.to_mapping(), "window_clipped_by": list(self.window_clipped_by),
            "transcript": {"status": self.transcript_status.to_mapping(),
                           "segments": [item.to_mapping() for item in self.transcript]},
            "audio": {"status": self.audio_status.to_mapping(), "silences": [r.to_mapping() for r in self.silences],
                      "speech": [r.to_mapping() for r in self.speech]},
            "visual": {"status": self.visual_status.to_mapping(), "samples": [v.to_mapping() for v in self.visual]},
            "gameplay_markers": [m.to_mapping() for m in self.gameplay_markers],
            "candidates": [c.to_mapping() for c in self.candidates],
            "feature_names": list(self.feature_names), "settings": dict(self.settings),
            "neighbors": dict(self.neighbors),
        }

    @classmethod
    def from_mapping(cls, raw: Any) -> BoundaryContext:
        raw = _mapping(raw, "context")
        if raw.get("format") != CONTEXT_FORMAT or raw.get("version") != CONTEXT_VERSION:
            raise BoundaryFormatError("Unsupported boundary context format/version")
        try:
            transcript, audio, visual = (_mapping(raw[k], k) for k in ("transcript", "audio", "visual"))
            return cls(
                BoundaryType(raw["boundary_type"]), dict(_mapping(raw["video"], "video")),
                GameInfo.from_mapping(raw["game"]),
                {str(k): finite(v, k, allow_none=True) for k, v in _mapping(raw["anchors"], "anchors").items()},
                TimeRange.from_mapping(raw["window"]), tuple(str(v) for v in _list(raw.get("window_clipped_by", []), "clip")),
                EvidenceStatus.from_mapping(transcript["status"]),
                [TranscriptSegment.from_mapping(item) for item in _list(transcript["segments"], "segments")],
                EvidenceStatus.from_mapping(audio["status"]),
                [TimeRange.from_mapping(item) for item in _list(audio["silences"], "silences")],
                [TimeRange.from_mapping(item) for item in _list(audio["speech"], "speech")],
                EvidenceStatus.from_mapping(visual["status"]),
                [VisualSample.from_mapping(item) for item in _list(visual["samples"], "samples")],
                [GameplayMarker.from_mapping(item) for item in _list(raw["gameplay_markers"], "markers")],
                [CandidateBoundary.from_mapping(item) for item in _list(raw["candidates"], "candidates")],
                [str(v) for v in _list(raw["feature_names"], "feature_names")],
                dict(_mapping(raw.get("settings", {}), "settings")),
                dict(_mapping(raw.get("neighbors", {}), "neighbors")),
            )
        except (KeyError, ValueError) as exc:
            raise BoundaryFormatError(f"Invalid boundary context: {exc}") from exc

    def candidate_index(self, sources: Sequence[CandidateSource]) -> int | None:
        for index, candidate in enumerate(self.candidates):
            if any(source in candidate.sources for source in sources):
                return index
        return None


@dataclass(frozen=True)
class BoundaryDecision:
    """One chosen candidate. `resolver` produced it; `requested_resolver` was asked first.

    fallback is True when the requested resolver failed (missing CLI, timeout,
    invalid output...) and `resolver` is the fallback; the reason is in metadata.
    """

    boundary_type: BoundaryType
    timestamp: float
    candidate_index: int | None
    confidence: float
    resolver: str
    detail: str = ""
    requested_resolver: str = ""
    fallback: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def selected_candidate(self) -> int | None:
        return self.candidate_index

    def to_mapping(self) -> dict[str, Any]:
        return {"boundary_type": self.boundary_type.value, "timestamp": self.timestamp,
                "candidate_index": self.candidate_index, "confidence": self.confidence,
                "resolver": self.resolver, "requested_resolver": self.requested_resolver or self.resolver,
                "fallback": self.fallback, "detail": self.detail, "metadata": dict(self.metadata)}
