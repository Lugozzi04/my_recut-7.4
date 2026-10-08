"""UI-independent video gameplay understanding; this package never edits cuts."""
from analysis.gameplay.models import (
    DetectionSource, GameEvent, GameEventType, GameResult, GameSegment, GameplayAnalysisResult,
)

__all__ = ["DetectionSource", "GameEvent", "GameEventType", "GameResult", "GameSegment", "GameplayAnalysisResult"]
