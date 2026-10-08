from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from analysis.audio_service import AnalysisCancellation
from analysis.gameplay.models import GameplayAnalysisResult

GameplayCancellation = AnalysisCancellation
ProgressCallback = Callable[[int], None]


class GameplayAnalysisError(RuntimeError):
    def __init__(self, message: str, *, code: str = "gameplay_analysis_failed") -> None:
        super().__init__(message)
        self.code = code


class GameplayAnalyzer(Protocol):
    def analyze(
        self, video_path: str | Path, *, cancellation: AnalysisCancellation | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> GameplayAnalysisResult: ...
