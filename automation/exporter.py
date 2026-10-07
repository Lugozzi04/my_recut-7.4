from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
import threading
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from analysis.cut_engine import Segment, invert_to_keeps, merge_overlaps
from automation.manager import PipelineManager
from automation.execution import job_execution
from automation.locking import ProcessFileLock
from automation.models import PipelineJob, PipelineState
from automation.retry import automatic_retry_allowed
from automation.paths import output_execution_lock, reserve_output
from core.project_file import normalize_project_payload, resolve_project_items
from export.output_safety import (
    OutputSourceCollisionError,
    atomic_promote_output,
    choose_output_path,
    ensure_output_is_safe,
)
from export.settings import ExportSettings
from utils.codec_detection import CodecSelection, resolve_video_codec
from utils.ffmpeg import ensure_ffmpeg, ffprobe_duration_seconds, has_audio_stream, has_video_stream


ProgressCallback = Callable[[int], None]
DetailCallback = Callable[[str], None]
StartedCallback = Callable[[str], None]
FinishedCallback = Callable[[PipelineJob], None]
FailedCallback = Callable[[PipelineJob, str], None]


def _default_worker_factory(**kwargs: Any) -> CancellableExportWorker:
    module = importlib.import_module("export.exporter")
    worker_class = getattr(module, "ExportWorker")
    return worker_class(**kwargs)


class CancellableExportWorker(Protocol):
    progress: Any
    detail: Any
    error: Any
    debug: bool

    def run(self) -> None: ...

    def cancel(self) -> None: ...


class ProjectExporter(Protocol):
    def export(
        self,
        job: PipelineJob,
        *,
        cancellation: ExportCancellation | None = None,
        on_progress: ProgressCallback | None = None,
        on_detail: DetailCallback | None = None,
    ) -> ExportResult: ...


class PipelineExportError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = str(code or "export_failed")


class PipelineExportCancelled(PipelineExportError):
    def __init__(self) -> None:
        super().__init__("export_cancelled", "Automatic export was cancelled.")


class ExportCancellation:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._worker: CancellableExportWorker | None = None

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()
        with self._lock:
            worker = self._worker
        if worker is not None:
            worker.cancel()

    def attach(self, worker: CancellableExportWorker) -> None:
        with self._lock:
            self._worker = worker
            cancelled = self._event.is_set()
        if cancelled:
            worker.cancel()

    def detach(self, worker: CancellableExportWorker) -> None:
        with self._lock:
            if self._worker is worker:
                self._worker = None

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled:
            raise PipelineExportCancelled()


@dataclass(frozen=True)
class ExportPlan:
    project_path: Path
    input_paths: tuple[str, ...]
    keeps: tuple[Segment, ...]
    segments: tuple[dict[str, Any], ...]
    requested_settings: ExportSettings
    audio_gain_db: float
    normalize_lufs: bool
    lufs_target: float
    apply_limiter: bool
    cut_hq_enabled: bool
    cut_hq_max_seconds: float
    expected_duration_s: float
    source_has_audio: bool
    source_signature: str


@dataclass(frozen=True)
class ExportResult:
    path: Path
    duration_s: float
    codec: str
    reused: bool = False


class ProjectExportPlanner:
    """Converts a saved editor project into the inputs accepted by ExportWorker."""

    def __init__(
        self,
        *,
        duration_probe: Callable[[str], float] = ffprobe_duration_seconds,
        audio_probe: Callable[[str], bool] = has_audio_stream,
    ) -> None:
        self._duration_probe = duration_probe
        self._audio_probe = audio_probe

    def build(self, job: PipelineJob) -> ExportPlan:
        project_path = self._project_for_job(job)
        try:
            raw = json.loads(project_path.read_text(encoding="utf-8"))
            payload = normalize_project_payload(raw)
        except (OSError, TypeError, ValueError) as exc:
            raise PipelineExportError(
                "invalid_project",
                f"Could not read the automatic project: {exc}",
            ) from exc

        available, missing = resolve_project_items(payload["tracks"], project_path)
        if missing:
            raise PipelineExportError(
                "source_not_found",
                "One or more media files referenced by the automatic project are missing.",
            )
        if not available:
            raise PipelineExportError("source_not_found", "The automatic project has no available media.")

        input_paths: list[str] = []
        input_indexes: dict[str, int] = {}
        segments: list[dict[str, Any]] = []
        timeline_cursor = 0.0
        first_config: Mapping[str, Any] = {}

        for item in available:
            source = str(Path(str(item.get("path", ""))).resolve())
            source_key = os.path.normcase(source)
            if source_key not in input_indexes:
                input_indexes[source_key] = len(input_paths)
                input_paths.append(source)
            source_index = input_indexes[source_key]

            source_in = self._safe_float(item.get("segment_source_in"), 0.0)
            source_out = self._safe_float(item.get("segment_source_out"), 0.0)
            duration = self._safe_float(item.get("duration"), 0.0)
            if duration <= 0.0 and source_out > source_in:
                duration = source_out - source_in
            if duration <= 0.0:
                duration = max(0.0, float(self._duration_probe(source)) - source_in)
            if duration <= 0.0 or not math.isfinite(duration):
                raise PipelineExportError("invalid_project", f"Invalid media duration in {project_path.name}.")

            if not first_config and isinstance(item.get("cfg"), Mapping):
                first_config = item["cfg"]
            keeps = self._item_keeps(item, duration)
            for keep in keeps:
                local_start = max(0.0, min(duration, float(keep.start)))
                local_end = max(local_start, min(duration, float(keep.end)))
                if local_end - local_start <= 1e-6:
                    continue
                source_start = source_in + local_start
                source_end = source_in + local_end
                segments.append(
                    {
                        "start": timeline_cursor + local_start,
                        "end": timeline_cursor + local_end,
                        "duration": local_end - local_start,
                        "v_idx": source_index,
                        "v_in": source_start,
                        "v_out": source_end,
                        "a_idx": source_index,
                        "a_in": source_start,
                        "a_out": source_end,
                    }
                )
            timeline_cursor += duration

        if not segments:
            raise PipelineExportError("empty_export", "Automatic cuts removed the entire selected VOD range.")

        settings = self._settings_from_payload(payload)
        settings = replace(settings, output_mode="single", range_start=0.0, range_end=0.0).normalized()
        cut_hq_enabled, cut_hq_max_seconds = self._cut_quality(settings.cut_quality)
        keeps, flat_segments = self._select_worker_plan(input_paths, segments)
        expected_duration = sum(float(item["duration"]) for item in segments)
        source_signature = self._source_signature(project_path, input_paths)
        try:
            source_has_audio = any(self._audio_probe(path) for path in input_paths)
        except Exception as exc:
            raise PipelineExportError("source_probe_failed", f"Could not inspect source audio: {exc}") from exc

        return ExportPlan(
            project_path=project_path,
            input_paths=tuple(input_paths),
            keeps=tuple(keeps),
            segments=tuple(flat_segments),
            requested_settings=settings,
            audio_gain_db=self._safe_float(first_config.get("gain_db"), 0.0),
            normalize_lufs=bool(first_config.get("normalize_lufs", False)),
            lufs_target=self._safe_float(first_config.get("lufs_target"), -14.0),
            apply_limiter=bool(first_config.get("limiter", True)),
            cut_hq_enabled=cut_hq_enabled,
            cut_hq_max_seconds=cut_hq_max_seconds,
            expected_duration_s=expected_duration,
            source_has_audio=source_has_audio,
            source_signature=source_signature,
        )

    @staticmethod
    def _project_for_job(job: PipelineJob) -> Path:
        raw_path = str(job.project_path or "").strip()
        if not raw_path:
            raise PipelineExportError("project_not_found", "The pipeline job has no automatic project.")
        project = Path(raw_path).expanduser().resolve()
        if not project.is_file():
            raise PipelineExportError("project_not_found", f"Automatic project not found: {project}")
        return project

    def _item_keeps(self, item: Mapping[str, Any], duration: float) -> list[Segment]:
        if not bool(item.get("cuts_enabled", False)):
            return [Segment(0.0, duration)]
        if "keeps" in item:
            return self._bounded_segments(item.get("keeps"), duration)
        cuts = self._bounded_segments(item.get("cuts"), duration)
        return invert_to_keeps(duration, cuts, min_keep=0.0)

    @staticmethod
    def _bounded_segments(raw: object, duration: float) -> list[Segment]:
        if not isinstance(raw, list):
            return []
        parsed: list[Segment] = []
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            try:
                start = max(0.0, min(duration, float(item.get("start", 0.0))))
                end = max(0.0, min(duration, float(item.get("end", 0.0))))
            except (TypeError, ValueError):
                continue
            if math.isfinite(start) and math.isfinite(end) and end > start:
                parsed.append(Segment(start, end))
        return merge_overlaps(parsed)

    @staticmethod
    def _settings_from_payload(payload: Mapping[str, Any]) -> ExportSettings:
        global_state = payload.get("global")
        if not isinstance(global_state, Mapping):
            return ExportSettings.defaults()
        raw_settings = global_state.get("export_settings")
        if isinstance(raw_settings, Mapping):
            return ExportSettings.from_mapping(raw_settings)
        defaults = ExportSettings.defaults()
        return replace(
            defaults,
            codec=str(global_state.get("codec", defaults.codec) or defaults.codec),
            method=str(global_state.get("export_method", defaults.method) or defaults.method),
            parallel_workers=int(global_state.get("parallel_workers", 0) or 0),
            chunk_count=int(global_state.get("chunk_count", 0) or 0),
            hwaccel_decode=bool(global_state.get("hwaccel_decode", defaults.hwaccel_decode)),
        ).normalized()

    @staticmethod
    def _select_worker_plan(
        input_paths: list[str],
        segments: list[dict[str, Any]],
    ) -> tuple[list[Segment], list[dict[str, Any]]]:
        previous_end = -1.0
        keeps: list[Segment] = []
        for item in segments:
            start = float(item["v_in"])
            end = float(item["v_out"])
            is_single_source = int(item["v_idx"]) == 0 and int(item["a_idx"]) == 0
            aligned_av = abs(start - float(item["a_in"])) <= 1e-6 and abs(end - float(item["a_out"])) <= 1e-6
            if len(input_paths) != 1 or not is_single_source or not aligned_av or start < previous_end - 1e-6:
                return [], [dict(value) for value in segments]
            keeps.append(Segment(start, end))
            previous_end = end
        return keeps, []

    @staticmethod
    def _cut_quality(value: str) -> tuple[bool, float]:
        return {
            "balanced": (True, 6.0),
            "higher": (True, 12.0),
            "faster": (True, 4.0),
            "maximum_speed": (False, 0.0),
        }.get(str(value), (True, 6.0))

    @staticmethod
    def _safe_float(value: object, default: float) -> float:
        try:
            candidate = value if isinstance(value, (str, int, float)) else default
            result = float(candidate)
        except (TypeError, ValueError):
            return float(default)
        return result if math.isfinite(result) else float(default)

    @staticmethod
    def _source_signature(project_path: Path, input_paths: list[str]) -> str:
        digest = hashlib.sha256(project_path.read_bytes())
        for value in input_paths:
            path = Path(value)
            stat = path.stat()
            digest.update(str(path).encode("utf-8", errors="surrogatepass"))
            digest.update(str(int(stat.st_size)).encode("ascii"))
            digest.update(str(int(stat.st_mtime_ns)).encode("ascii"))
        return digest.hexdigest()


class PipelineProjectExporter:
    """Renders an automatic project with the same engine used by the manual editor."""

    def __init__(
        self,
        *,
        planner: ProjectExportPlanner | None = None,
        worker_factory: Callable[..., CancellableExportWorker] = _default_worker_factory,
        ffmpeg_provider: Callable[[], tuple[str, str]] = ensure_ffmpeg,
        codec_resolver: Callable[[str, str | None], CodecSelection] = resolve_video_codec,
        duration_probe: Callable[[str], float] = ffprobe_duration_seconds,
        video_probe: Callable[[str], bool] = has_video_stream,
        audio_probe: Callable[[str], bool] = has_audio_stream,
    ) -> None:
        self._planner = planner or ProjectExportPlanner()
        self._worker_factory = worker_factory
        self._ffmpeg_provider = ffmpeg_provider
        self._codec_resolver = codec_resolver
        self._duration_probe = duration_probe
        self._video_probe = video_probe
        self._audio_probe = audio_probe

    def _render_setup(self, job: PipelineJob) -> tuple[ExportPlan, str, CodecSelection, ExportSettings, str]:
        plan = self._planner.build(job)
        ffmpeg_path, _ffprobe_path = self._ffmpeg_provider()
        selection = self._codec_resolver(ffmpeg_path, plan.requested_settings.codec)
        effective = replace(plan.requested_settings, codec=selection.resolved).normalized()
        return plan, ffmpeg_path, selection, effective, self._render_signature(plan, effective)

    def expected_signature(self, job: PipelineJob) -> str:
        """Identify the exact planned render before FFmpeg can publish its output."""
        return self._render_setup(job)[4]

    def _select_output(self, job: PipelineJob, plan: ExportPlan, settings: ExportSettings, signature: str) -> Path:
        output = self._output_path(job, plan, settings)
        protected = self._protected_paths(job, plan)
        self._check_output_paths(output, protected)
        raw_delivery = job.metadata.get("delivery", {})
        delivery = raw_delivery if isinstance(raw_delivery, Mapping) else {}
        if not any(delivery.get(key) for key in ("output_path", "output_dir", "resolved_output_path")):
            requested = output
            index = 2
            while (not bool(delivery.get("force", False))
                   and (os.path.lexists(output) or os.path.lexists(self._sidecar_path(output)))
                   and not self._owns_sidecar(output, signature)):
                output = requested.with_name(f"{requested.stem}_{index}{requested.suffix}")
                index += 1
            self._check_output_paths(output, protected)
        return output

    def prepare_destination(self, job: PipelineJob, manager: PipelineManager) -> Path:
        """Reserve a legacy destination before rendering, retaining owned cache paths."""
        plan, _ffmpeg, _selection, settings, signature = self._render_setup(job)
        candidate = self._select_output(job, plan, settings, signature)
        protected = self._protected_paths(job, plan)
        allocation = ProcessFileLock(candidate.parent / ".autocutter-locks" / "allocation.lock")
        with allocation.hold(timeout=10):
            current = manager.get(job.id)
            delivery = current.metadata.get("delivery", {})
            if isinstance(delivery, Mapping) and delivery.get("resolved_output_path"):
                chosen = Path(str(delivery["resolved_output_path"])).expanduser().resolve()
                self._check_output_paths(chosen, protected)
                return chosen
            candidate = self._select_output(current, plan, settings, signature)
            requested = self._output_path(current, plan, settings)
            reserved = {
                os.path.normcase(str(Path(str(value)).resolve()))
                for other in manager.list_jobs()
                if other.id != job.id
                for value in [other.metadata.get("delivery", {}).get("resolved_output_path")]
                if value
            }
            index = 2
            while os.path.normcase(str(candidate.resolve())) in reserved:
                if isinstance(delivery, Mapping) and delivery.get("force") is True:
                    raise PipelineExportError("output_reserved", "Export output is reserved by another job.")
                candidate = choose_output_path(
                    requested.with_name(f"{requested.stem}_{index}{requested.suffix}"), protected=protected,
                )
                index += 1
            self._check_output_paths(candidate, protected)
            manager.update_metadata(job.id, {"delivery": {"resolved_output_path": str(candidate)}})
            return candidate

    def export(
        self,
        job: PipelineJob,
        *,
        cancellation: ExportCancellation | None = None,
        on_progress: ProgressCallback | None = None,
        on_detail: DetailCallback | None = None,
    ) -> ExportResult:
        token = cancellation or ExportCancellation()
        token.raise_if_cancelled()
        try:
            plan, ffmpeg_path, selection, effective, signature = self._render_setup(job)
            output = self._select_output(job, plan, effective, signature)
            protected = self._protected_paths(job, plan)
            self._check_output_paths(output, protected)
            raw_delivery = job.metadata.get("delivery", {})
            delivery = raw_delivery if isinstance(raw_delivery, Mapping) else {}
            cached = self._cached_result(
                output,
                signature,
                plan.source_has_audio,
                selection.resolved,
                expected_duration_s=plan.expected_duration_s,
            )
            if cached is not None:
                token.raise_if_cancelled()
                metadata = self._matching_metadata(output, signature)
                if metadata is not None and metadata.get("promotion_pending") is True:
                    self._write_sidecar(
                        output, signature, cached.duration_s, effective,
                        require_audio=plan.source_has_audio, promotion_pending=False,
                    )
                self._emit_detail(on_detail, f"automation_export_cache_hit path={output}")
                self._emit_progress(on_progress, 100)
                return cached

            output.parent.mkdir(parents=True, exist_ok=True)
            temporary = output.with_name(f".{output.stem}.{uuid.uuid4().hex}.partial{output.suffix}")
            errors: list[str] = []
            last_progress = -1

            def report(progress: int, _label: str = "") -> None:
                nonlocal last_progress
                value = max(last_progress, min(100, int(progress)))
                if value <= last_progress:
                    return
                last_progress = value
                self._emit_progress(on_progress, value)

            self._emit_detail(
                on_detail,
                "automation_export_config requested="
                + json.dumps(plan.requested_settings.to_mapping(), sort_keys=True, separators=(",", ":")),
            )
            self._emit_detail(
                on_detail,
                "automation_export_config effective="
                + json.dumps(effective.to_mapping(), sort_keys=True, separators=(",", ":")),
            )
            if selection.fallback_reason:
                self._emit_detail(on_detail, f"automation_export_codec_fallback {selection.fallback_reason}")

            worker = self._worker_factory(
                ffmpeg_path=ffmpeg_path,
                input_path=plan.input_paths[0],
                output_path=str(temporary),
                keeps=list(plan.keeps),
                codec=effective.codec,
                use_hwaccel=effective.hwaccel_decode,
                export_method=effective.method,
                parallel_workers=effective.parallel_workers,
                chunk_count=effective.chunk_count,
                audio_gain_db=plan.audio_gain_db,
                normalize_lufs=plan.normalize_lufs,
                lufs_target=plan.lufs_target,
                apply_limiter=plan.apply_limiter,
                cut_hq_enabled=plan.cut_hq_enabled,
                cut_hq_max_seconds=plan.cut_hq_max_seconds,
                input_paths=list(plan.input_paths),
                segments=[dict(item) for item in plan.segments],
                export_settings=effective,
                requested_export_settings=plan.requested_settings,
            )
            worker.debug = False
            worker.progress.connect(report)
            worker.detail.connect(lambda message: self._emit_detail(on_detail, str(message)))
            worker.error.connect(lambda message: errors.append(str(message)))
            token.attach(worker)
            try:
                worker.run()
            finally:
                token.detach(worker)
            token.raise_if_cancelled()
            if errors:
                raise PipelineExportError("render_failed", errors[-1])

            duration = self._validate_output(
                temporary,
                plan.source_has_audio,
                expected_duration_s=plan.expected_duration_s,
            )
            token.raise_if_cancelled()
            self._check_output_paths(output, protected)
            delivery_options = delivery
            # A matching manifest identifies a previous attempt of this exact
            # render, including a corrupt file that needs a same-path retry.
            owned_output = self._owns_output(output, signature)
            overwrite = bool(delivery_options.get("force", False)) or owned_output
            overwrite_sidecar = bool(delivery_options.get("force", False)) or self._owns_sidecar(output, signature)
            previous_owned_identity = None
            if owned_output:
                try:
                    previous_stat = output.stat()
                    previous_owned_identity = (previous_stat.st_size, previous_stat.st_mtime_ns)
                except OSError:
                    pass
            if not overwrite_sidecar and os.path.lexists(self._sidecar_path(output)):
                raise PipelineExportError("output_exists", f"Export metadata already exists: {output}")
            if not overwrite and os.path.lexists(output):
                raise PipelineExportError("output_exists", f"Export destination already exists: {output}")
            # Persist the partial file identity first: a crash before promotion
            # cannot mistake a previous output for this newly rendered version.
            rendered_stat = temporary.stat()
            rendered_identity = (rendered_stat.st_size, rendered_stat.st_mtime_ns)
            self._write_sidecar(
                output, signature, duration, effective, rendered_path=temporary,
                promotion_pending=True, overwrite=overwrite_sidecar, require_audio=plan.source_has_audio,
                previous_owned_identity=previous_owned_identity,
            )
            token.raise_if_cancelled()
            self._check_output_paths(output, protected)
            try:
                atomic_promote_output(temporary, output, overwrite=overwrite)
            except FileExistsError as exc:
                raise PipelineExportError("output_exists", f"Export destination already exists: {output}") from exc
            self._write_sidecar(
                output, signature, duration, effective, promotion_pending=False,
                require_audio=plan.source_has_audio, expected_identity=rendered_identity,
            )
            self._emit_progress(on_progress, 100)
            return ExportResult(output.resolve(), duration, selection.resolved, reused=False)
        except PipelineExportError:
            raise
        except Exception as exc:
            raise PipelineExportError("export_failed", f"Could not export the automatic project: {exc}") from exc
        finally:
            temporary_path = locals().get("temporary")
            if isinstance(temporary_path, Path):
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _output_path(job: PipelineJob, plan: ExportPlan, settings: ExportSettings) -> Path:
        raw_source = str(job.local_source_path or plan.input_paths[0])
        source = Path(raw_source).expanduser().resolve()
        default = source.with_name(f"{source.stem}.youtube-ready{settings.output_extension()}")
        delivery = job.metadata.get("delivery")
        if not isinstance(delivery, Mapping):
            return default
        resolved = str(delivery.get("resolved_output_path", "") or "").strip()
        if resolved:
            return Path(resolved).expanduser().resolve()
        raw_output = str(delivery.get("output_path", "") or "").strip()
        raw_directory = str(delivery.get("output_dir", "") or "").strip()
        if not raw_output and not raw_directory:
            return default
        requested = Path(raw_output) if raw_output else Path(raw_directory) / default.name
        if not requested.suffix:
            requested = requested.with_suffix(settings.output_extension())
        try:
            chosen = choose_output_path(
                requested,
                force=bool(delivery.get("force", False)),
                protected=PipelineProjectExporter._protected_paths(job, plan),
            )
        except OutputSourceCollisionError as exc:
            raise PipelineExportError("source_collision", str(exc)) from exc
        # The coordinator persists this decision before export; direct callers also
        # keep a stable choice for repeated calls with the same in-memory job.
        job.metadata["delivery"] = {**delivery, "resolved_output_path": str(chosen)}
        return chosen

    @staticmethod
    def _protected_paths(job: PipelineJob, plan: ExportPlan) -> tuple[str | Path, ...]:
        paths: list[str | Path] = [*plan.input_paths, plan.project_path]
        if job.local_source_path:
            paths.append(job.local_source_path)
        return tuple(paths)

    @staticmethod
    def _check_output_paths(output: Path, protected: tuple[str | Path, ...]) -> None:
        try:
            ensure_output_is_safe(output, protected)
            ensure_output_is_safe(PipelineProjectExporter._sidecar_path(output), protected)
        except OutputSourceCollisionError as exc:
            raise PipelineExportError("source_collision", str(exc)) from exc

    @staticmethod
    def _render_signature(plan: ExportPlan, settings: ExportSettings) -> str:
        digest = hashlib.sha256(plan.source_signature.encode("ascii"))
        digest.update(json.dumps(settings.to_mapping(), sort_keys=True, separators=(",", ":")).encode("utf-8"))
        digest.update(b"automation-export-v1")
        return digest.hexdigest()

    def validate_artifact(self, output: str | Path, *, expected_signature: str | None = None) -> dict[str, Any]:
        """Validate a delivered render without needing a retained source video.

        Automatic delivery always has a manifest. Older finalized manifests may
        lack a fingerprint, but still require full stream/duration validation.
        """
        path = Path(output).expanduser().resolve()
        try:
            raw = json.loads(self._sidecar_path(path).read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or raw.get("format") != "autocutter_automation_export" or raw.get("version") != 1:
                raise ValueError("Export manifest is missing or invalid.")
            signature = raw.get("signature")
            if not isinstance(signature, str) or not signature:
                raise ValueError("Export manifest has no render signature.")
            if expected_signature is not None and signature != expected_signature:
                raise ValueError("Export render signature differs from this job.")
            expected = float(raw["duration_s"])
            if not math.isfinite(expected) or expected <= 0:
                raise ValueError("Export manifest duration is invalid.")
            stat = path.stat()
            pending = raw.get("promotion_pending") is True
            if pending and ("output_size" not in raw or "output_mtime_ns" not in raw):
                raise ValueError("Uncommitted export manifest has no file fingerprint.")
            if "output_size" in raw and int(raw["output_size"]) != stat.st_size:
                raise ValueError("Export file size differs from its validated render.")
            if "output_mtime_ns" in raw and int(raw["output_mtime_ns"]) != stat.st_mtime_ns:
                raise ValueError("Export file timestamp differs from its validated render.")
            require_audio = raw.get("require_audio", raw.get("source_has_audio", False)) is True
            self._validate_output(path, require_audio, expected_duration_s=expected)
            return raw
        except PipelineExportError:
            raise
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise PipelineExportError("invalid_output", f"Automatic export artifact validation failed: {exc}") from exc

    def _cached_result(
        self,
        output: Path,
        signature: str,
        require_audio: bool,
        codec: str,
        *,
        expected_duration_s: float | None = None,
    ) -> ExportResult | None:
        if not output.is_file():
            return None
        try:
            metadata = self._matching_metadata(output, signature)
            if metadata is None:
                return None
            output_stat = output.stat()
            if "output_size" in metadata and int(metadata["output_size"]) != output_stat.st_size:
                return None
            if "output_mtime_ns" in metadata and int(metadata["output_mtime_ns"]) != output_stat.st_mtime_ns:
                return None
            duration = self._validate_output(output, require_audio, expected_duration_s=expected_duration_s)
            return ExportResult(output.resolve(), duration, codec, reused=True)
        except (OSError, TypeError, ValueError, PipelineExportError):
            return None

    def _matching_metadata(self, output: Path, signature: str) -> dict[str, Any] | None:
        try:
            metadata = json.loads(self._sidecar_path(output).read_text(encoding="utf-8"))
            if isinstance(metadata, dict) and metadata.get("signature") == signature:
                return metadata
        except (OSError, TypeError, ValueError):
            pass
        return None

    @staticmethod
    def _matches_file_identity(output: Path, metadata: Mapping[str, Any]) -> bool:
        try:
            stat = output.stat()
            return (int(metadata["output_size"]) == stat.st_size
                    and int(metadata["output_mtime_ns"]) == stat.st_mtime_ns)
        except (OSError, TypeError, ValueError, KeyError):
            return False

    def _owns_output(self, output: Path, signature: str) -> bool:
        metadata = self._matching_metadata(output, signature)
        if metadata is None:
            return False
        # An unfinished attempt owns the promoted render, or the exact previous
        # owned file being repaired. A later foreign file cannot gain ownership
        # merely by occupying a destination with this render's manifest.
        if metadata.get("promotion_pending") is True:
            if self._matches_file_identity(output, metadata):
                return True
            previous = metadata.get("previous_owned_output_identity")
            return isinstance(previous, Mapping) and self._matches_file_identity(output, previous)
        return True

    def _owns_sidecar(self, output: Path, signature: str) -> bool:
        metadata = self._matching_metadata(output, signature)
        if metadata is None:
            return False
        if metadata.get("promotion_pending") is True:
            return not os.path.lexists(output) or self._owns_output(output, signature)
        return True

    def _validate_output(
        self,
        path: Path,
        require_audio: bool,
        *,
        expected_duration_s: float | None = None,
    ) -> float:
        if not path.is_file() or path.stat().st_size <= 1024:
            raise PipelineExportError("invalid_output", "Automatic export did not create a valid video file.")
        try:
            duration = float(self._duration_probe(str(path)))
            if not math.isfinite(duration) or duration <= 0.0:
                raise ValueError("invalid duration")
            if not self._video_probe(str(path)):
                raise ValueError("video stream missing")
            if require_audio and not self._audio_probe(str(path)):
                raise ValueError("audio stream missing")
            if expected_duration_s is not None and expected_duration_s > 0.0:
                # Allow normal frame/audio padding and legacy smart-render drift;
                # reject clearly truncated or unrelated files even if playable.
                tolerance = max(0.5, min(25.0, expected_duration_s * 0.08))
                if abs(duration - expected_duration_s) > tolerance:
                    raise ValueError(
                        f"duration mismatch (expected {expected_duration_s:.3f}s, got {duration:.3f}s)"
                    )
        except Exception as exc:
            raise PipelineExportError("invalid_output", f"Automatic export validation failed: {exc}") from exc
        return duration

    def _write_sidecar(
        self,
        output: Path,
        signature: str,
        duration: float,
        settings: ExportSettings,
        *,
        rendered_path: Path | None = None,
        promotion_pending: bool = False,
        overwrite: bool = True,
        require_audio: bool = False,
        expected_identity: tuple[int, int] | None = None,
        previous_owned_identity: tuple[int, int] | None = None,
    ) -> None:
        sidecar = self._sidecar_path(output)
        temporary = sidecar.with_name(f".{sidecar.name}.{uuid.uuid4().hex}.tmp")
        payload = {
            "format": "autocutter_automation_export",
            "version": 1,
            "signature": signature,
            "duration_s": duration,
            "settings": settings.to_mapping(),
            "promotion_pending": bool(promotion_pending),
            "require_audio": bool(require_audio),
        }
        if promotion_pending and previous_owned_identity is not None:
            payload["previous_owned_output_identity"] = {
                "output_size": previous_owned_identity[0],
                "output_mtime_ns": previous_owned_identity[1],
            }
        try:
            rendered_stat = (rendered_path or output).stat()
            if expected_identity is not None and (rendered_stat.st_size, rendered_stat.st_mtime_ns) != expected_identity:
                raise PipelineExportError("output_replaced", "Export output changed before metadata was committed.")
            payload["output_size"] = int(rendered_stat.st_size)
            payload["output_mtime_ns"] = int(rendered_stat.st_mtime_ns)
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(payload, indent=2) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            atomic_promote_output(temporary, sidecar, overwrite=overwrite)
        except FileExistsError as exc:
            raise PipelineExportError("output_exists", "Export metadata was created by another writer.") from exc
        except OSError as exc:
            raise PipelineExportError("export_metadata_failed", f"Could not save export metadata: {exc}") from exc
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _sidecar_path(output: Path) -> Path:
        return output.with_suffix(output.suffix + ".automation.json")

    @staticmethod
    def _emit_progress(callback: ProgressCallback | None, value: int) -> None:
        if callback is not None:
            callback(max(0, min(100, int(value))))

    @staticmethod
    def _emit_detail(callback: DetailCallback | None, message: str) -> None:
        if callback is not None:
            callback(str(message))


class PipelineExportService:
    def __init__(
        self,
        manager: PipelineManager,
        *,
        exporter_factory: Callable[[], ProjectExporter] = PipelineProjectExporter,
    ) -> None:
        self.manager = manager
        self._exporter_factory = exporter_factory

    @job_execution
    def execute(
        self,
        job_id: str,
        *,
        cancellation: ExportCancellation | None = None,
        on_progress: ProgressCallback | None = None,
        on_detail: DetailCallback | None = None,
    ) -> PipelineJob:
        token = cancellation or ExportCancellation()
        job = self.manager.get(job_id)
        if (job.state == PipelineState.CANCELLED
                and job.metadata.get("cancelled_from") in {"ready_export", "exporting"}):
            job = self.manager.resume_cancelled(job.id)
        retry_states = {PipelineState.READY_EXPORT, PipelineState.EXPORTING}
        if job.state == PipelineState.FAILED and job.retry_state in retry_states:
            job = self.manager.retry(job.id)
        if job.state == PipelineState.READY_EXPORT:
            job = self.manager.start_export(job.id)
        if job.state != PipelineState.EXPORTING:
            raise PipelineExportError(
                "invalid_export_state",
                f"Pipeline job {job.id} is not waiting for automatic export.",
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
            delivery = job.metadata.get("delivery", {})
            exporter = self._exporter_factory()
            if isinstance(exporter, PipelineProjectExporter):
                self.manager.update_metadata(job.id, {"export_artifact": {"signature": exporter.expected_signature(job)}})
                job = self.manager.get(job.id)
            if isinstance(delivery, Mapping) and any(delivery.get(k) for k in (
                "output_path", "output_dir", "resolved_output_path",
            )):
                destination = reserve_output(self.manager, job)
                job = self.manager.get(job.id)
                with output_execution_lock(destination).hold(blocking=False):
                    result = exporter.export(
                        job, cancellation=token, on_progress=report, on_detail=on_detail,
                    )
            elif isinstance(exporter, PipelineProjectExporter):
                destination = exporter.prepare_destination(job, self.manager)
                job = self.manager.get(job.id)
                with output_execution_lock(destination).hold(blocking=False):
                    result = exporter.export(
                        job, cancellation=token, on_progress=report, on_detail=on_detail,
                    )
            else:
                result = exporter.export(
                    job, cancellation=token, on_progress=report, on_detail=on_detail,
                )
            if isinstance(exporter, PipelineProjectExporter):
                manifest = exporter.validate_artifact(result.path)
                self.manager.update_metadata(job.id, {"export_artifact": {"signature": manifest["signature"]}})
            return self.manager.mark_exported(job.id, result.path)
        except PipelineExportCancelled as exc:
            self._fail_active_job(job.id, exc.code, str(exc))
            raise
        except PipelineExportError as exc:
            self._fail_active_job(job.id, exc.code, str(exc), retryable=getattr(exc, "retryable", False))
            raise
        except Exception as exc:
            message = f"Unexpected automatic export failure: {exc}"
            self._fail_active_job(job.id, "export_failed", message)
            raise PipelineExportError("export_failed", message) from exc

    def _fail_active_job(self, job_id: str, code: str, message: str, *, retryable: bool = False) -> None:
        try:
            if self.manager.get(job_id).state == PipelineState.EXPORTING:
                self.manager.fail(job_id, code, message, automatic_retry=retryable and automatic_retry_allowed())
        except Exception:
            pass


class PipelineExportQueue:
    """Single-worker persistent automatic-export queue."""

    def __init__(
        self,
        manager: PipelineManager,
        *,
        exporter_factory: Callable[[], ProjectExporter] = PipelineProjectExporter,
        on_started: StartedCallback | None = None,
        on_progress: Callable[[str, int], None] | None = None,
        on_detail: Callable[[str, str], None] | None = None,
        on_finished: FinishedCallback | None = None,
        on_failed: FailedCallback | None = None,
    ) -> None:
        self.manager = manager
        self.service = PipelineExportService(manager, exporter_factory=exporter_factory)
        self.on_started = on_started
        self.on_progress = on_progress
        self.on_detail = on_detail
        self.on_finished = on_finished
        self.on_failed = on_failed
        self._condition = threading.Condition()
        self._pending: deque[str] = deque()
        self._queued: set[str] = set()
        self._active_job_id = ""
        self._active_cancellation: ExportCancellation | None = None
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
        retry_states = {PipelineState.READY_EXPORT, PipelineState.EXPORTING}
        retryable = job.state == PipelineState.FAILED and job.retry_state in retry_states
        retryable = retryable or (job.state == PipelineState.CANCELLED
                                  and job.metadata.get("cancelled_from") in {"ready_export", "exporting"})
        if job.state != PipelineState.READY_EXPORT and not retryable:
            raise PipelineExportError(
                "invalid_export_state",
                f"Pipeline job {job.id} cannot be queued for export from {job.state.value}.",
            )
        with self._condition:
            if self._stopping:
                raise PipelineExportError("export_queue_stopped", "The export queue is shutting down.")
            if job.id == self._active_job_id or job.id in self._queued:
                return False
            self._pending.append(job.id)
            self._queued.add(job.id)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run,
                    name="pipeline-export-queue",
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
                cancellation = ExportCancellation()
                self._active_job_id = job_id
                self._active_cancellation = cancellation

            self._notify(self.on_started, job_id)
            try:
                def report_progress(value: int) -> None:
                    self._notify(self.on_progress, job_id, value)

                def report_detail(message: str) -> None:
                    self._notify(self.on_detail, job_id, message)

                completed = self.service.execute(
                    job_id,
                    cancellation=cancellation,
                    on_progress=report_progress,
                    on_detail=report_detail,
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
