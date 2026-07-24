from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QObject, Signal, Slot

from .ai_pipeline import (
    AiPipelineConfig,
    run_ai_pipeline,
    find_spleeter_python,
    find_silero_python,
)


def ai_available() -> bool:
    ok, _ = ai_dependency_status()
    return ok


def ai_dependency_status() -> tuple[bool, str]:
    spleeter_py = find_spleeter_python()
    silero_py = find_silero_python()
    missing: list[str] = []
    if not spleeter_py:
        missing.append("Spleeter python not found (set AUTO_CUTTER_SPLEETER_PY or create .venv_spleeter).")
    if not silero_py:
        missing.append("Silero not found (set AUTO_CUTTER_SILERO_PY or install silero-vad in the app Python).")
    if missing:
        return False, "\n".join(missing)
    return True, ""


class AiAnalyzeWorker(QObject):
    done = Signal(int, float, object)  # track_idx, duration, speech_segments
    error = Signal(int, str)

    def __init__(self, path: str, track_idx: int, cfg: Optional[AiPipelineConfig] = None):
        super().__init__()
        self.path = str(path)
        self.track_idx = int(track_idx)
        self.cfg = cfg
        self._cancel_requested = False

    @Slot()
    def cancel(self) -> None:
        # Best-effort: current pipeline call is not interruptible mid-run.
        self._cancel_requested = True

    @Slot()
    def run(self):
        try:
            if self._cancel_requested:
                raise RuntimeError("__CANCELLED__")
            res = run_ai_pipeline(self.path, self.cfg)
            if self._cancel_requested:
                raise RuntimeError("__CANCELLED__")
            payload = {
                "speech": list(res.speech),
                "speaker_ids": list(res.speaker_ids) if res.speaker_ids is not None else None,
            }
            if self._cancel_requested:
                raise RuntimeError("__CANCELLED__")
            self.done.emit(int(self.track_idx), float(res.duration), payload)
        except Exception as e:
            self.error.emit(int(self.track_idx), str(e))
