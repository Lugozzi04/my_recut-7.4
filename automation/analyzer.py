from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from analysis.audio_service import (
    AnalysisCancellation,
    AnalysisCancelled,
    AnalysisRequest,
    AnalysisResult,
    AudioAnalysisError,
    analyze_audio,
)
from analysis.classic import compute_classic_cuts, threshold_amp_to_pct
from analysis.cut_engine import Segment
from automation.manager import PipelineManager
from automation.execution import job_execution
from automation.models import PipelineJob, PipelineState, utc_now
from core.project_file import (
    PROJECT_FORMAT,
    PROJECT_VERSION,
    make_payload_portable,
    normalize_project_payload,
    resolve_project_items,
)
from core.presets import PRESET_VERSION, normalize_preset_cfg
from export.output_safety import OutputSourceCollisionError, ensure_output_is_safe
from export.settings import ExportSettings


ProgressCallback = Callable[[int], None]
StartedCallback = Callable[[str], None]
FinishedCallback = Callable[[PipelineJob], None]
FailedCallback = Callable[[PipelineJob, str], None]


class AudioAnalyzer(Protocol):
    def __call__(
        self,
        request: AnalysisRequest,
        *,
        cancellation: AnalysisCancellation | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> AnalysisResult: ...


class ProjectAnalyzer(Protocol):
    def analyze(
        self,
        job: PipelineJob,
        *,
        cancellation: AnalysisCancellation | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> AnalysisProjectResult: ...


class PipelineAnalysisError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = str(code or "analysis_failed")


class PipelineAnalysisCancelled(PipelineAnalysisError):
    def __init__(self) -> None:
        super().__init__("analysis_cancelled", "Automatic analysis was cancelled.")


@dataclass(frozen=True)
class ClassicAnalysisProfile:
    name: str = "Balanced (Default)"
    version: int = 1
    intensity: int = 50
    pre_pad_s: float = 0.25
    post_pad_s: float = 0.62
    min_cut_s: float = 0.10
    attack_ms: int = 120
    release_ms: int = 250
    smoothing_mode: str = "Medium"
    merge_pauses_ms: int = 300

    def to_project_config(self, threshold_pct: int) -> dict[str, Any]:
        return {
            "intensity": self.intensity,
            "threshold_pct": threshold_pct,
            "pre_pad_s": self.pre_pad_s,
            "post_pad_s": self.post_pad_s,
            "min_cut_s": self.min_cut_s,
            "gain_db": 0.0,
            "gain_affects_detection": False,
            "attack_ms": self.attack_ms,
            "release_ms": self.release_ms,
            "smoothing_mode": self.smoothing_mode,
            "merge_pauses_ms": self.merge_pauses_ms,
            "normalize_lufs": False,
            "lufs_target": -14.0,
            "limiter": True,
        }


@dataclass(frozen=True)
class AnalysisProjectResult:
    path: Path
    duration_s: float
    cuts_count: int
    reused: bool = False


BALANCED_ANALYSIS_PROFILE = ClassicAnalysisProfile()


def _segments_payload(segments: list[Segment]) -> list[dict[str, float]]:
    return [
        {"start": round(float(segment.start), 6), "end": round(float(segment.end), 6)}
        for segment in segments
        if float(segment.end) > float(segment.start)
    ]


def _threshold_percent(result: AnalysisResult) -> int:
    if result.rms.size == 0:
        return 45
    rms_min = float(np.min(result.rms))
    rms_max = float(np.max(result.rms))
    return threshold_amp_to_pct(float(result.auto_threshold), rms_min, rms_max)


def _compute_classic_cuts(
    result: AnalysisResult,
    profile: ClassicAnalysisProfile,
    manual_cuts: Iterable[Segment] = (),
) -> tuple[list[Segment], list[Segment]]:
    config = profile.to_project_config(_threshold_percent(result))
    config["intensity"] = max(0, min(100, int(profile.intensity)))
    for key in ("pre_pad_s", "min_cut_s", "attack_ms", "release_ms", "merge_pauses_ms"):
        config[key] = max(0.0, float(config[key]))
    # Existing jobs predate GUI preset snapshots and retain their auto threshold.
    return compute_classic_cuts(
        result.rms, float(result.duration), float(result.hop_s), config,
        manual_cuts=manual_cuts,
        threshold_amp=float(result.auto_threshold),
    )


class PipelineProjectAnalyzer:
    """Runs classic audio analysis and writes an editor-compatible project."""

    def __init__(
        self,
        *,
        analyze_fn: AudioAnalyzer = analyze_audio,
        profile: ClassicAnalysisProfile = BALANCED_ANALYSIS_PROFILE,
    ) -> None:
        self._analyze_fn = analyze_fn
        self.profile = profile

    def analyze(
        self,
        job: PipelineJob,
        *,
        cancellation: AnalysisCancellation | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> AnalysisProjectResult:
        source = self._source_for_job(job)
        delivery = job.metadata.get("delivery")
        raw_output = str(delivery.get("project_path", "") or "").strip() if isinstance(delivery, Mapping) else ""
        output = Path(raw_output).expanduser().resolve() if raw_output else source.with_suffix(".autocutter")
        try:
            ensure_output_is_safe(output, (source,))
        except OutputSourceCollisionError as exc:
            raise PipelineAnalysisError("source_collision", str(exc)) from exc
        preset = self._preset_for_job(job)
        request_signature = self._request_signature(job, preset)
        token = cancellation or AnalysisCancellation()
        if token.is_cancelled:
            raise PipelineAnalysisCancelled()
        cached = self._cached_result(output, source, job.id, request_signature=request_signature)
        if cached is not None:
            self._emit_progress(on_progress, 100)
            return cached

        last_progress = -1

        def report_audio(progress: int) -> None:
            nonlocal last_progress
            value = max(0, min(95, int(max(0, min(100, int(progress))) * 0.95)))
            if value <= last_progress:
                return
            last_progress = value
            self._emit_progress(on_progress, value)

        try:
            result = self._analyze_fn(
                AnalysisRequest(str(source)),
                cancellation=token,
                on_progress=report_audio,
            )
            token.raise_if_cancelled()
            if not math.isfinite(float(result.duration)) or float(result.duration) <= 0.0:
                raise PipelineAnalysisError("invalid_analysis", "Audio analysis returned an invalid duration.")
            range_cuts = self._range_cuts(job, float(result.duration))
            if preset is None:
                cuts, keeps = _compute_classic_cuts(result, self.profile, range_cuts)
            else:
                cuts, keeps = compute_classic_cuts(
                    result.rms, float(result.duration), float(result.hop_s), preset["config"],
                    manual_cuts=range_cuts,
                )
            payload = self._project_payload(
                job, source, result, cuts, keeps,
                preset=preset, request_signature=request_signature,
                manual_cuts=range_cuts,
            )
            portable = make_payload_portable(payload, output)
            token.raise_if_cancelled()
            self._write_project(output, portable)
            validated = self._cached_result(output, source, job.id, request_signature=request_signature)
            if validated is None:
                raise PipelineAnalysisError(
                    "invalid_project",
                    "The generated Auto Cutter project could not be validated.",
                )
            if last_progress < 100:
                self._emit_progress(on_progress, 100)
            return AnalysisProjectResult(
                path=validated.path,
                duration_s=validated.duration_s,
                cuts_count=validated.cuts_count,
                reused=False,
            )
        except AnalysisCancelled as exc:
            raise PipelineAnalysisCancelled() from exc
        except PipelineAnalysisError:
            raise
        except AudioAnalysisError as exc:
            raise PipelineAnalysisError(exc.code, str(exc)) from exc
        except Exception as exc:
            raise PipelineAnalysisError(
                "analysis_failed",
                f"Could not analyze the downloaded video: {exc}",
            ) from exc

    @staticmethod
    def _source_for_job(job: PipelineJob) -> Path:
        raw_path = str(job.local_source_path or "").strip()
        if not raw_path:
            raise PipelineAnalysisError("source_not_found", "The pipeline job has no downloaded source.")
        source = Path(raw_path).expanduser().resolve()
        if not source.is_file():
            raise PipelineAnalysisError("source_not_found", f"Downloaded source not found: {source}")
        return source

    def cached_result(self, job: PipelineJob) -> AnalysisProjectResult | None:
        """Validate a persisted project using the analysis cache's exact rules."""
        try:
            source = self._source_for_job(job)
            if not job.project_path:
                return None
            preset = self._preset_for_job(job)
            return self._cached_result(
                Path(job.project_path).resolve(), source, job.id,
                request_signature=self._request_signature(job, preset),
            )
        except (OSError, PipelineAnalysisError, TypeError, ValueError):
            return None

    @staticmethod
    def _preset_for_job(job: PipelineJob) -> dict[str, Any] | None:
        raw = job.metadata.get("preset")
        if raw is None:
            return None
        if not isinstance(raw, Mapping) or not isinstance(raw.get("config"), Mapping):
            raise PipelineAnalysisError("invalid_preset", "The job's preset snapshot has no configuration.")
        try:
            version = int(raw.get("version", PRESET_VERSION))
            if version < 1:
                raise ValueError("Preset version must be positive.")
            return {
                "name": str(raw.get("name", "Balanced (Default)") or "Balanced (Default)"),
                "version": version,
                "config": normalize_preset_cfg(raw["config"]),
            }
        except (TypeError, ValueError) as exc:
            raise PipelineAnalysisError("invalid_preset", f"Invalid preset snapshot: {exc}") from exc

    @staticmethod
    def _request_signature(job: PipelineJob, preset: dict[str, Any] | None) -> str | None:
        raw_export = job.metadata.get("export_settings")
        source_range = PipelineProjectAnalyzer._source_range(job)
        if preset is None and not isinstance(raw_export, Mapping) and source_range is None:
            return None
        settings = ExportSettings.from_mapping(raw_export).to_mapping() if isinstance(raw_export, Mapping) else None
        serialized = json.dumps(
            {"preset": preset, "export_settings": settings, "source_range": source_range},
            sort_keys=True, separators=(",", ":"),
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def _source_range(job: PipelineJob) -> dict[str, float | None] | None:
        raw = job.metadata.get("source_range")
        if raw is None:
            return None
        try:
            if not isinstance(raw, Mapping):
                raise ValueError("Source range must be an object.")
            start = float(raw.get("start_s", 0.0) or 0.0)
            end_value = raw.get("end_s")
            end = float(end_value) if end_value is not None else None
            if not math.isfinite(start) or start < 0.0:
                raise ValueError("Range start must be finite and non-negative.")
            if end is not None and (not math.isfinite(end) or end <= start):
                raise ValueError("Range end must be finite and after its start.")
            return {"start_s": start, "end_s": end}
        except (TypeError, ValueError) as exc:
            raise PipelineAnalysisError("invalid_source_range", str(exc)) from exc

    @staticmethod
    def _range_cuts(job: PipelineJob, duration: float) -> list[Segment]:
        selected = PipelineProjectAnalyzer._source_range(job)
        if selected is None:
            return []
        start = float(selected["start_s"] or 0.0)
        end = min(duration, float(selected["end_s"])) if selected["end_s"] is not None else duration
        if start >= end:
            raise PipelineAnalysisError("invalid_source_range", "The selected range is outside the source video.")
        cuts = [Segment(0.0, start)] if start > 0.0 else []
        if end < duration:
            cuts.append(Segment(end, duration))
        return cuts

    def _project_payload(
        self,
        job: PipelineJob,
        source: Path,
        result: AnalysisResult,
        cuts: list[Segment],
        keeps: list[Segment],
        *,
        preset: dict[str, Any] | None = None,
        request_signature: str | None = None,
        manual_cuts: list[Segment] | None = None,
    ) -> dict[str, Any]:
        threshold_pct = _threshold_percent(result)
        config = self.profile.to_project_config(threshold_pct) if preset is None else preset["config"]
        profile_name = self.profile.name if preset is None else str(preset["name"])
        profile_version = self.profile.version if preset is None else int(preset["version"])
        cuts_payload = _segments_payload(cuts)
        keeps_payload = _segments_payload(keeps)
        manual_payload = _segments_payload(manual_cuts or [])
        duration = float(result.duration)
        track = {
            "path": str(source),
            "cfg": config,
            "cuts_enabled": True,
            "cuts": cuts_payload,
            "keeps": keeps_payload,
            "manual_cuts": manual_payload,
            "suppressed_cuts": [],
            "segment_group_id": None,
            "segment_index": 1,
            "copy_source_id": None,
            "copy_index": 0,
            "segment_source_in": 0.0,
            "segment_source_out": duration,
            "duration": duration,
            "classic_cuts_enabled": True,
            "classic_cuts": cuts_payload,
            "classic_keeps": keeps_payload,
            "classic_manual_cuts": manual_payload,
            "classic_suppressed_cuts": [],
            "ai_cuts_enabled": False,
            "ai_cuts": [],
            "ai_keeps": [],
            "ai_manual_cuts": [],
            "ai_suppressed_cuts": [],
        }
        raw_export = job.metadata.get("export_settings")
        export_settings = ExportSettings.from_mapping(raw_export) if isinstance(raw_export, Mapping) else ExportSettings.defaults()
        source_stat = source.stat()
        automation: dict[str, Any] = {
            "job_id": job.id,
            "vod_id": job.vod_id,
            "profile": profile_name,
            "profile_version": profile_version,
            "source_size": int(source_stat.st_size),
            "source_mtime_ns": int(source_stat.st_mtime_ns),
        }
        if request_signature is not None:
            automation["analysis_request_signature"] = request_signature
        selected_range = self._source_range(job)
        if selected_range is not None:
            automation["source_range"] = selected_range
        if preset is not None:
            automation["preset"] = preset
            serialized = json.dumps(preset, sort_keys=True, separators=(",", ":"))
            automation["preset_signature"] = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        return {
            "format": PROJECT_FORMAT,
            "version": PROJECT_VERSION,
            "saved_at": utc_now(),
            "project_name": str(job.source_title or source.stem),
            "automation": automation,
            "global": {
                "analysis_mode": "classic",
                "active_track_index": 0,
                "skip_preview": False,
                "preset_name": profile_name,
                "preset_dirty": False,
                "preset_source_name": profile_name,
                "codec": export_settings.codec,
                "export_method": export_settings.method,
                "cut_quality_index": 0,
                "parallel_workers": export_settings.parallel_workers,
                "chunk_count": export_settings.chunk_count,
                "hwaccel_decode": export_settings.hwaccel_decode,
                "export_settings": export_settings.to_mapping(),
            },
            "tracks": [track],
        }

    @staticmethod
    def _write_project(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(serialized)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except OSError as exc:
            raise PipelineAnalysisError("project_write_failed", f"Could not save project {path}: {exc}") from exc
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _cached_result(
        self, project: Path, source: Path, job_id: str,
        *, request_signature: str | None = None,
    ) -> AnalysisProjectResult | None:
        if not project.is_file():
            return None
        try:
            payload = normalize_project_payload(json.loads(project.read_text(encoding="utf-8")))
            automation = payload.get("automation")
            if not isinstance(automation, dict) or str(automation.get("job_id", "")) != job_id:
                return None
            if automation.get("analysis_request_signature") != request_signature:
                return None
            source_stat = source.stat()
            if int(automation.get("source_size", -1)) != int(source_stat.st_size):
                return None
            if int(automation.get("source_mtime_ns", -1)) != int(source_stat.st_mtime_ns):
                return None
            available, missing = resolve_project_items(payload["tracks"], project)
            if missing or len(available) != 1:
                return None
            if Path(str(available[0].get("path", ""))).resolve() != source:
                return None
            duration = float(available[0].get("duration", 0.0) or 0.0)
            cuts = available[0].get("cuts")
            if not math.isfinite(duration) or duration <= 0.0 or not isinstance(cuts, list):
                return None
            return AnalysisProjectResult(project.resolve(), duration, len(cuts), reused=True)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    @staticmethod
    def _emit_progress(callback: ProgressCallback | None, value: int) -> None:
        if callback is not None:
            callback(max(0, min(100, int(value))))


class PipelineAnalysisService:
    def __init__(
        self,
        manager: PipelineManager,
        *,
        analyzer_factory: Callable[[], ProjectAnalyzer] = PipelineProjectAnalyzer,
    ) -> None:
        self.manager = manager
        self._analyzer_factory = analyzer_factory

    @job_execution
    def execute(
        self,
        job_id: str,
        *,
        cancellation: AnalysisCancellation | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> PipelineJob:
        token = cancellation or AnalysisCancellation()
        job = self.manager.get(job_id)
        if job.state == PipelineState.CANCELLED and job.metadata.get("cancelled_from") == "analyzing":
            job = self.manager.resume_cancelled(job.id)
        if job.state == PipelineState.FAILED and job.retry_state == PipelineState.ANALYZING:
            job = self.manager.retry(job.id)
        if job.state != PipelineState.ANALYZING:
            raise PipelineAnalysisError(
                "invalid_analysis_state",
                f"Pipeline job {job.id} is not waiting for analysis.",
            )

        last_persisted = int(job.progress)

        def report(progress: int) -> None:
            nonlocal last_persisted
            value = max(last_persisted, min(100, int(progress)))
            if value > last_persisted:
                self.manager.update_progress(job.id, value)
                last_persisted = value
            if on_progress is not None:
                on_progress(value)

        try:
            result = self._analyzer_factory().analyze(
                job,
                cancellation=token,
                on_progress=report,
            )
            return self.manager.mark_analyzed(job.id, result.path)
        except PipelineAnalysisCancelled as exc:
            self._fail_active_job(job.id, exc.code, str(exc))
            raise
        except PipelineAnalysisError as exc:
            self._fail_active_job(job.id, exc.code, str(exc))
            raise
        except Exception as exc:
            message = f"Unexpected automatic analysis failure: {exc}"
            self._fail_active_job(job.id, "analysis_failed", message)
            raise PipelineAnalysisError("analysis_failed", message) from exc

    def _fail_active_job(self, job_id: str, code: str, message: str) -> None:
        try:
            if self.manager.get(job_id).state == PipelineState.ANALYZING:
                self.manager.fail(job_id, code, message)
        except Exception:
            pass


class PipelineAnalysisQueue:
    """Single-worker persistent automatic-analysis queue."""

    def __init__(
        self,
        manager: PipelineManager,
        *,
        analyzer_factory: Callable[[], ProjectAnalyzer] = PipelineProjectAnalyzer,
        on_started: StartedCallback | None = None,
        on_progress: Callable[[str, int], None] | None = None,
        on_finished: FinishedCallback | None = None,
        on_failed: FailedCallback | None = None,
    ) -> None:
        self.manager = manager
        self.service = PipelineAnalysisService(manager, analyzer_factory=analyzer_factory)
        self.on_started = on_started
        self.on_progress = on_progress
        self.on_finished = on_finished
        self.on_failed = on_failed
        self._condition = threading.Condition()
        self._pending: deque[str] = deque()
        self._queued: set[str] = set()
        self._active_job_id = ""
        self._active_cancellation: AnalysisCancellation | None = None
        self._stopping = False
        self._thread: threading.Thread | None = None

    @property
    def active_job_id(self) -> str:
        with self._condition:
            return self._active_job_id

    @property
    def is_running(self) -> bool:
        return bool(self.active_job_id)

    def enqueue(self, job_id: str) -> bool:
        job = self.manager.get(job_id)
        retryable = job.state == PipelineState.FAILED and job.retry_state == PipelineState.ANALYZING
        retryable = retryable or (job.state == PipelineState.CANCELLED
                                  and job.metadata.get("cancelled_from") == "analyzing")
        if job.state != PipelineState.ANALYZING and not retryable:
            raise PipelineAnalysisError(
                "invalid_analysis_state",
                f"Pipeline job {job.id} cannot be queued for analysis from {job.state.value}.",
            )
        with self._condition:
            if self._stopping:
                raise PipelineAnalysisError("analysis_queue_stopped", "The analysis queue is shutting down.")
            if job.id == self._active_job_id or job.id in self._queued:
                return False
            self._pending.append(job.id)
            self._queued.add(job.id)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run,
                    name="pipeline-analysis-queue",
                    daemon=True,
                )
                self._thread.start()
            self._condition.notify_all()
            return True

    def cancel_current(self) -> bool:
        with self._condition:
            cancellation = self._active_cancellation
        if cancellation is None:
            return False
        cancellation.cancel()
        return True

    def shutdown(self, timeout: float = 3.0) -> bool:
        with self._condition:
            self._stopping = True
            self._pending.clear()
            self._queued.clear()
            cancellation = self._active_cancellation
            thread = self._thread
            self._condition.notify_all()
        if cancellation is not None:
            cancellation.cancel()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, float(timeout)))
        return thread is None or not thread.is_alive()

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._pending and not self._stopping:
                    self._condition.wait()
                if self._stopping:
                    return
                job_id = self._pending.popleft()
                self._queued.discard(job_id)
                cancellation = AnalysisCancellation()
                self._active_job_id = job_id
                self._active_cancellation = cancellation

            self._notify(self.on_started, job_id)
            try:
                def report_progress(value: int) -> None:
                    self._notify(self.on_progress, job_id, value)

                completed = self.service.execute(
                    job_id,
                    cancellation=cancellation,
                    on_progress=report_progress,
                )
                self._notify(self.on_finished, completed)
            except Exception as exc:
                try:
                    failed = self.manager.get(job_id)
                except Exception:
                    failed = None
                if failed is not None:
                    self._notify(self.on_failed, failed, str(exc))
            finally:
                with self._condition:
                    if self._active_job_id == job_id:
                        self._active_job_id = ""
                        self._active_cancellation = None
                    self._condition.notify_all()

    @staticmethod
    def _notify(callback: Callable[..., None] | None, *args: object) -> None:
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:
            pass
