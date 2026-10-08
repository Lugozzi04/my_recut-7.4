from __future__ import annotations

import math
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


ANALYSIS_FORMAT = "recut_gameplay_analysis"
ANALYSIS_SCHEMA_VERSION = 1


class GameplayFormatError(ValueError):
    """An event, game or diagnostic artifact contains invalid data."""


class GameEventType(str, Enum):
    VS_SCREEN = "VS_SCREEN"
    VICTORY = "VICTORY"
    DEFEAT = "DEFEAT"


class GameResult(str, Enum):
    WIN = "WIN"
    LOSS = "LOSS"
    UNKNOWN = "UNKNOWN"


class DetectionSource(str, Enum):
    TEMPLATE_MATCHING = "template_matching"
    MANUAL = "manual"


def _number(value: object, name: str, *, minimum: float = 0.0, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GameplayFormatError(f"{name} must be a number.")
    number = float(value)
    if not math.isfinite(number) or number < minimum or (maximum is not None and number > maximum):
        raise GameplayFormatError(f"{name} is outside its valid range.")
    return number


def _optional_timestamp(value: object, name: str) -> float | None:
    return None if value is None else _number(value, name)


def _mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise GameplayFormatError(f"{name} must be an object with string keys.")
    return deepcopy(dict(value))


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GameplayFormatError(f"{name} must be non-empty text.")
    return value


def _index(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise GameplayFormatError("game.index must be a positive integer.")
    return value


def _list(value: object, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise GameplayFormatError(f"{name} must be an array.")
    return value


@dataclass
class GameEvent:
    """A visual observation; confidence is similarity, not a calibrated probability."""

    timestamp: float
    type: GameEventType
    confidence: float
    source: DetectionSource = DetectionSource.TEMPLATE_MATCHING
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        _number(self.timestamp, "event.timestamp")
        _number(self.confidence, "event.confidence", maximum=1.0)
        if not isinstance(self.type, GameEventType):
            raise GameplayFormatError("event.type must be a GameEventType.")
        if not isinstance(self.source, DetectionSource):
            raise GameplayFormatError("event.source must be a DetectionSource.")
        _mapping(self.metadata, "event.metadata")

    def to_mapping(self) -> dict[str, Any]:
        self.validate()
        return {
            "timestamp": float(self.timestamp),
            "type": self.type.value,
            "confidence": float(self.confidence),
            "source": self.source.value,
            "metadata": deepcopy(self.metadata),
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> GameEvent:
        try:
            return cls(
                timestamp=_number(raw.get("timestamp"), "event.timestamp"),
                type=GameEventType(raw.get("type")),
                confidence=_number(raw.get("confidence"), "event.confidence", maximum=1.0),
                source=DetectionSource(raw.get("source", DetectionSource.TEMPLATE_MATCHING.value)),
                metadata=_mapping(raw.get("metadata", {}), "event.metadata"),
            )
        except GameplayFormatError:
            raise
        except (TypeError, ValueError) as exc:
            raise GameplayFormatError(f"Invalid gameplay event: {exc}") from exc


@dataclass
class GameSegment:
    """Content understanding, independent of removal/cut segments."""

    id: str
    index: int
    start: float | None
    end: float | None
    result: GameResult
    start_confidence: float = 0.0
    end_confidence: float = 0.0
    result_confidence: float = 0.0
    start_source: DetectionSource | None = None
    end_source: DetectionSource | None = None
    result_source: DetectionSource | None = None
    markers: list[GameEvent] = field(default_factory=list)
    user_override: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.validate()

    @property
    def complete(self) -> bool:
        return self.start is not None and self.end is not None

    def validate(self) -> None:
        _text(self.id, "game.id")
        if isinstance(self.index, bool) or not isinstance(self.index, int) or self.index < 1:
            raise GameplayFormatError("game.index must be a positive integer.")
        start = _optional_timestamp(self.start, "game.start")
        end = _optional_timestamp(self.end, "game.end")
        if start is not None and end is not None and end < start:
            raise GameplayFormatError("game.end cannot precede game.start.")
        if not isinstance(self.result, GameResult):
            raise GameplayFormatError("game.result must be a GameResult.")
        for name in ("start_confidence", "end_confidence", "result_confidence"):
            _number(getattr(self, name), "game." + name, maximum=1.0)
        for name in ("start_source", "end_source", "result_source"):
            source = getattr(self, name)
            if source is not None and not isinstance(source, DetectionSource):
                raise GameplayFormatError("game." + name + " must be a DetectionSource or null.")
        if start is None and (self.start_confidence != 0.0 or self.start_source is not None):
            raise GameplayFormatError("A missing game start cannot have detection confidence/source.")
        if end is None and (self.end_confidence != 0.0 or self.end_source is not None):
            raise GameplayFormatError("A missing game end cannot have detection confidence/source.")
        if not isinstance(self.markers, list) or any(not isinstance(item, GameEvent) for item in self.markers):
            raise GameplayFormatError("game.markers must contain GameEvent objects.")
        for event in self.markers:
            event.validate()
        if not isinstance(self.user_override, bool):
            raise GameplayFormatError("game.user_override must be a boolean.")
        _mapping(self.metadata, "game.metadata")

    def to_mapping(self) -> dict[str, Any]:
        self.validate()
        return {
            "id": self.id,
            "index": self.index,
            "start": self.start,
            "end": self.end,
            "result": self.result.value,
            "start_confidence": float(self.start_confidence),
            "end_confidence": float(self.end_confidence),
            "result_confidence": float(self.result_confidence),
            "start_source": self.start_source.value if self.start_source is not None else None,
            "end_source": self.end_source.value if self.end_source is not None else None,
            "result_source": self.result_source.value if self.result_source is not None else None,
            "markers": [event.to_mapping() for event in self.markers],
            "user_override": self.user_override,
            "metadata": deepcopy(self.metadata),
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> GameSegment:
        try:
            sources = {
                name: DetectionSource(raw[name]) if raw.get(name) is not None else None
                for name in ("start_source", "end_source", "result_source")
            }
            markers = [GameEvent.from_mapping(_mapping(item, "game.marker"))
                       for item in _list(raw.get("markers", []), "game.markers")]
            return cls(
                id=_text(raw.get("id"), "game.id"),
                index=_index(raw.get("index")),
                start=_optional_timestamp(raw.get("start"), "game.start"),
                end=_optional_timestamp(raw.get("end"), "game.end"),
                result=GameResult(raw.get("result")),
                start_confidence=_number(raw.get("start_confidence", 0.0), "game.start_confidence", maximum=1.0),
                end_confidence=_number(raw.get("end_confidence", 0.0), "game.end_confidence", maximum=1.0),
                result_confidence=_number(raw.get("result_confidence", 0.0), "game.result_confidence", maximum=1.0),
                start_source=sources["start_source"],
                end_source=sources["end_source"],
                result_source=sources["result_source"],
                markers=markers,
                user_override=raw.get("user_override", False),
                metadata=_mapping(raw.get("metadata", {}), "game.metadata"),
            )
        except GameplayFormatError:
            raise
        except (TypeError, ValueError) as exc:
            raise GameplayFormatError(f"Invalid gameplay game: {exc}") from exc


@dataclass
class GameplayAnalysisResult:
    video_path: str
    duration: float
    events: list[GameEvent]
    games: list[GameSegment]
    detector_version: str
    settings: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        _text(self.video_path, "video_path")
        _number(self.duration, "duration")
        _text(self.detector_version, "detector_version")
        if not isinstance(self.events, list) or any(not isinstance(event, GameEvent) for event in self.events):
            raise GameplayFormatError("events must contain GameEvent objects.")
        if not isinstance(self.games, list) or any(not isinstance(game, GameSegment) for game in self.games):
            raise GameplayFormatError("games must contain GameSegment objects.")
        for event in self.events:
            event.validate()
        for game in self.games:
            game.validate()
        _mapping(self.settings, "settings")
        _mapping(self.metadata, "metadata")
        if not isinstance(self.warnings, list) or any(not isinstance(item, str) for item in self.warnings):
            raise GameplayFormatError("warnings must contain strings.")

    def to_mapping(self) -> dict[str, Any]:
        self.validate()
        return {
            "format": ANALYSIS_FORMAT,
            "schema_version": ANALYSIS_SCHEMA_VERSION,
            "video_path": self.video_path,
            "duration": float(self.duration),
            "events": [event.to_mapping() for event in self.events],
            "games": [game.to_mapping() for game in self.games],
            "detector_version": self.detector_version,
            "settings": deepcopy(self.settings),
            "metadata": deepcopy(self.metadata),
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> GameplayAnalysisResult:
        if raw.get("format", ANALYSIS_FORMAT) != ANALYSIS_FORMAT:
            raise GameplayFormatError("Unsupported gameplay analysis format.")
        version = raw.get("schema_version", ANALYSIS_SCHEMA_VERSION)
        if isinstance(version, bool) or not isinstance(version, int) or version != ANALYSIS_SCHEMA_VERSION:
            raise GameplayFormatError("Unsupported gameplay analysis schema version.")
        return cls(
            video_path=_text(raw.get("video_path"), "video_path"),
            duration=_number(raw.get("duration"), "duration"),
            events=[GameEvent.from_mapping(_mapping(item, "event"))
                    for item in _list(raw.get("events", []), "events")],
            games=[GameSegment.from_mapping(_mapping(item, "game"))
                   for item in _list(raw.get("games", []), "games")],
            detector_version=_text(raw.get("detector_version"), "detector_version"),
            settings=_mapping(raw.get("settings", {}), "settings"),
            metadata=_mapping(raw.get("metadata", {}), "metadata"),
            warnings=_list(raw.get("warnings", []), "warnings"),
        )
