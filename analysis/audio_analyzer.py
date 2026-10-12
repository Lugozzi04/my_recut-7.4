from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QObject, Signal, Slot

from analysis.performance_profile import GameplayProfiler

from analysis.audio_service import AnalysisCancellation, AnalysisRequest, analyze_audio


class AnalyzeWorker(QObject):
    """Qt adapter for the UI-independent audio analysis service."""

    done = Signal(int, float, object, float, float)
    error = Signal(int, str)
    progress = Signal(int, int)

    def __init__(self, path: str, hop_s: float = 0.03, sr: int = 16000, track_idx: int = 0):
        super().__init__()
        self.path = path
        self.hop_s = float(hop_s)
        self.sr = int(sr)
        self.track_idx = int(track_idx)
        self._cancellation = AnalysisCancellation()

    @Slot()
    def cancel(self) -> None:
        self._cancellation.cancel()

    @Slot()
    def run(self) -> None:
        from analysis.performance_profile import remember_audio

        profile = GameplayProfiler(self.path, {"frontend": "GUI_classic"}, save=False)
        with profile.activate():
            self._run_profiled(profile, remember_audio)

    def _run_profiled(self, profile: GameplayProfiler,
                      remember_audio: Callable[[str, dict[str, Any]], None]) -> None:
        try:
            result = analyze_audio(
                AnalysisRequest(path=self.path, hop_s=self.hop_s, sample_rate=self.sr),
                cancellation=self._cancellation,
                on_progress=lambda value: self.progress.emit(self.track_idx, value),
            )
            profile.record("classic", rms_blocks=int(result.rms.size), media_seconds=result.duration)
            profile.video["duration_s"] = result.duration
            profile.finish("completed")
            remember_audio(self.path, profile.snapshot())
            self.done.emit(
                self.track_idx,
                result.duration,
                result.rms,
                result.hop_s,
                result.auto_threshold,
            )
        except Exception as exc:
            profile.finish("cancelled" if self._cancellation.is_cancelled else "failed", exc)
            remember_audio(self.path, profile.snapshot())
            self.error.emit(self.track_idx, str(exc))
