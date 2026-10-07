from __future__ import annotations

import hashlib
import importlib
import json
import math
import threading
import uuid
from collections.abc import Callable, Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from analysis.audio_service import AnalysisCancellation
from automation.analyzer import PipelineAnalysisService, PipelineProjectAnalyzer
from automation.downloader import (
    DownloadCancellation, DownloadRequest, PipelineDownloadService,
    YtDlpMediaResolver, default_download_dir,
)
from automation.exporter import ExportCancellation, PipelineExportService, PipelineProjectExporter
from automation.manager import PipelineJobBusyError, PipelineManager
from automation.locking import FileLockBusyError
from automation.models import PipelineJob, PipelineState
from automation.paths import output_execution_lock, reserve_output, safe_output_name
from core.config import get_setting
from core.presets import PRESET_VERSION, PresetRepository, default_preset_name
from export.output_safety import ensure_output_is_safe
from export.settings import VIDEO_QUALITY_CRF, ExportSettings
from utils.ffmpeg import cancellable_probe_scope, ffprobe_duration_seconds, has_audio_stream, has_video_stream
from utils.redaction import redact_secrets
from utils.runtime_paths import exports_root


class PipelineRuntimeError(RuntimeError):
    def __init__(self, code: str, message: str, exit_code: int) -> None:
        super().__init__(redact_secrets(message))
        self.code = code
        self.exit_code = exit_code


class CancellationTarget(Protocol):
    def cancel(self) -> None: ...


class PipelineCancellation:
    """One frontend cancellation request propagated to the current shared stage."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.RLock()
        self._active: CancellationTarget | None = None

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def is_cancelled(self) -> bool:
        return self.cancelled

    def cancel(self) -> None:
        self._event.set()
        with self._lock:
            target = self._active
        if target is not None:
            target.cancel()

    def attach(self, target: CancellationTarget) -> None:
        with self._lock:
            self._active = target
            cancelled = self.cancelled
        if cancelled:
            target.cancel()

    def detach(self, target: CancellationTarget) -> None:
        with self._lock:
            if self._active is target:
                self._active = None

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise PipelineRuntimeError("cancelled", "Pipeline cancelled.", 70)


EventCallback = Callable[[dict[str, Any]], None]
ServiceFactory = Callable[[PipelineManager], Any]


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise PipelineRuntimeError("invalid_range", f"{label} must be a number of seconds.", 2)
    result = float(value)
    if not math.isfinite(result):
        raise PipelineRuntimeError("invalid_range", f"{label} must be finite.", 2)
    return result


def validate_range(
    duration_s: float, *, start: float | None = None, end: float | None = None,
    duration: float | None = None,
) -> tuple[float, float, list[str]]:
    total = _number(duration_s, "Source duration")
    if total <= 0:
        raise PipelineRuntimeError("invalid_media", "Source duration must be positive.", 2)
    if end is not None and duration is not None:
        raise PipelineRuntimeError("invalid_range", "Use either --end or --duration, not both.", 2)
    first = _number(start, "Start") if start is not None else 0.0
    if first < 0 or first >= total:
        raise PipelineRuntimeError("invalid_range", "Start must be nonnegative and before the source ends.", 2)
    if duration is not None:
        length = _number(duration, "Duration")
        if length <= 0:
            raise PipelineRuntimeError("invalid_range", "Duration must be positive.", 2)
        last = first + length
    else:
        last = _number(end, "End") if end is not None else total
    if last <= first:
        raise PipelineRuntimeError("invalid_range", "End must be after start.", 2)
    warnings: list[str] = []
    if last > total:
        tolerance = max(2.0, min(30.0, total * 0.01))
        if last - total > tolerance:
            raise PipelineRuntimeError("invalid_range", f"End exceeds source duration ({total:.3f}s).", 2)
        warnings.append(f"End clamped from {last:.3f}s to source duration {total:.3f}s.")
        last = total
    return first, last, warnings


class PipelineRuntime:
    """Headless orchestrator of the manager and the GUI's existing stage services.

    Planning has no writes or processing. Execution holds one per-job OS lock,
    and every stage/artifact still goes through the shared manager and engines.
    """

    def __init__(
        self, manager: PipelineManager | None = None, presets: PresetRepository | None = None,
        on_event: EventCallback | None = None, *,
        metadata_resolver: Any | None = None,
        download_service_factory: ServiceFactory = PipelineDownloadService,
        analysis_service_factory: ServiceFactory = PipelineAnalysisService,
        export_service_factory: ServiceFactory = PipelineExportService,
        upload_service_factory: ServiceFactory | None = None,
        duration_probe: Callable[[str], float] = ffprobe_duration_seconds,
        video_probe: Callable[[str], bool] = has_video_stream,
        audio_probe: Callable[[str], bool] = has_audio_stream,
        settings_loader: Callable[[], Mapping[str, Any]] | None = None,
    ) -> None:
        self.manager = manager or PipelineManager()
        self.presets = presets or PresetRepository()
        self.on_event = on_event
        self._resolver = metadata_resolver or YtDlpMediaResolver()
        self._download_factory = download_service_factory
        self._analysis_factory = analysis_service_factory
        self._export_factory = export_service_factory
        self._upload_factory = upload_service_factory
        self._duration_probe = duration_probe
        self._video_probe = video_probe
        self._audio_probe = audio_probe
        self._artifact_validator = PipelineProjectExporter(duration_probe=duration_probe, video_probe=video_probe, audio_probe=audio_probe)
        self._settings_loader = settings_loader or self._saved_export_settings

    @staticmethod
    def _saved_export_settings() -> Mapping[str, Any]:
        raw = get_setting("export/settings_v1", "")
        if isinstance(raw, Mapping):
            return raw
        try:
            decoded = json.loads(str(raw))
            return decoded if isinstance(decoded, dict) else {}
        except (TypeError, ValueError):
            return {}

    def _preset(self, name: str | None) -> dict[str, Any]:
        try:
            selected = name or default_preset_name(self.presets.load())
            if not selected:
                raise KeyError("No presets are available.")
            return {"name": selected, "config": self.presets.resolve(selected), "version": PRESET_VERSION}
        except (KeyError, ValueError) as exc:
            raise PipelineRuntimeError("unknown_preset", str(exc), 2) from exc

    def plan_vod(self, url: str, **options: Any) -> dict[str, Any]:
        # Validate the same URL/range structure used by the real downloader before I/O.
        try:
            DownloadRequest("metadata", "metadata", str(url), 0.0, 1.0, Path(".")).validate()
        except Exception as exc:
            raise PipelineRuntimeError("invalid_url", str(exc), 2) from exc
        parsed = urlsplit(str(url))
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise PipelineRuntimeError("invalid_url", "Use a plain Twitch VOD URL without credentials or query parameters.", 2)
        try:
            metadata = self._resolver.metadata(str(url))
        except Exception as exc:
            raise PipelineRuntimeError(getattr(exc, "code", "twitch_metadata_failed"), str(exc), 10) from exc
        if not isinstance(metadata, Mapping):
            raise PipelineRuntimeError("invalid_metadata", "Twitch returned invalid VOD metadata.", 10)
        try:
            total = float(metadata.get("duration_s") or 0)
        except (TypeError, ValueError) as exc:
            raise PipelineRuntimeError("invalid_metadata", "Twitch VOD duration is unavailable.", 10) from exc
        if not math.isfinite(total) or total <= 0:
            raise PipelineRuntimeError("invalid_metadata", "Twitch VOD duration is unavailable.", 10)
        vod_id = parsed.path.strip("/").split("/")[-1]
        clean = {
            "id": vod_id, "title": str(metadata.get("title") or f"Twitch VOD {vod_id}"),
            "duration_s": total, "channel_id": str(metadata.get("channel_id") or ""),
            "channel": str(metadata.get("channel") or ""),
        }
        return self._plan("twitch", str(url), clean, **options)

    def plan_local(self, source: str | Path, **options: Any) -> dict[str, Any]:
        target = Path(source).expanduser().resolve()
        if not target.is_file():
            raise PipelineRuntimeError("source_missing", f"Local video does not exist: {target}", 60)
        try:
            total = float(self._duration_probe(str(target)))
            valid = self._video_probe(str(target))
        except Exception as exc:
            raise PipelineRuntimeError("invalid_media", f"Cannot probe local video: {exc}", 20) from exc
        if not valid or not math.isfinite(total) or total <= 0:
            raise PipelineRuntimeError("invalid_media", "Local source must contain a valid video stream and duration.", 20)
        return self._plan("local", str(target), {"title": target.stem, "duration_s": total}, **options)

    def _plan(
        self, kind: str, source: str, media: Mapping[str, Any], *,
        start: float | None = None, end: float | None = None, duration: float | None = None,
        preset: str | None = None, output: str | Path | None = None,
        output_dir: str | Path | None = None, youtube: bool = False, title: str | None = None,
        description: str = "", thumbnail: str | Path | None = None, keep_source: bool = False,
        force: bool = False, quality: str | None = None,
    ) -> dict[str, Any]:
        first, last, warnings = validate_range(float(media["duration_s"]), start=start, end=end, duration=duration)
        if output is not None and output_dir is not None:
            raise PipelineRuntimeError("invalid_output", "Use either --output or --output-dir.", 2)
        resolved_preset = self._preset(preset)
        settings = dict(ExportSettings.from_mapping(self._settings_loader()).to_mapping())
        # Automation exports one deliverable; stale GUI range/per-cut settings do not apply.
        settings.update(output_mode="single", range_start=0.0, range_end=0.0)
        if quality:
            if quality not in VIDEO_QUALITY_CRF:
                raise PipelineRuntimeError("invalid_quality", f"Unknown video quality: {quality}", 2)
            settings.update(quality=quality, custom_quality=VIDEO_QUALITY_CRF[quality])
        container = str(settings["container"])
        folder = Path(output_dir).expanduser().resolve() if output_dir is not None else exports_root().resolve()
        destination = Path(output).expanduser().resolve() if output is not None else folder / f"{safe_output_name(str(media['title']))}.{container}"
        if not destination.suffix:
            destination = destination.with_suffix(f".{container}")
        if destination.suffix.lower() != f".{container}":
            if destination.suffix.lower() in {".mp4", ".mkv", ".mov"}:
                settings["container"] = destination.suffix[1:].lower()
            else:
                raise PipelineRuntimeError("invalid_output", "Output extension must be .mp4, .mkv or .mov.", 2)
        protected = (Path(source),) if kind == "local" else ()
        try:
            ensure_output_is_safe(destination, protected)
        except ValueError as exc:
            raise PipelineRuntimeError("invalid_output", str(exc), 60) from exc
        thumbnail_path = None
        if thumbnail is not None:
            target = Path(thumbnail).expanduser().resolve()
            if not target.is_file():
                raise PipelineRuntimeError("thumbnail_missing", f"Thumbnail does not exist: {target}", 60)
            thumbnail_path = str(target)
        delivery = {
            "output_path": str(destination), "force": bool(force), "youtube": bool(youtube),
            "title": str(title or media["title"]).strip() or "Auto Cutter video",
            "title_explicit": title is not None,
            "description": str(description), "thumbnail_path": thumbnail_path,
            "keep_source": bool(keep_source),
        }
        requested = {"start_s": start, "end_s": end, "duration_s": duration}
        return {
            "source_kind": kind, "source": source, "media": dict(media),
            "requested_range": requested, "range": {"start_s": first, "end_s": last},
            "preset": resolved_preset, "export_settings": settings,
            "delivery": delivery, "warnings": warnings,
        }

    def create_job(self, plan: Mapping[str, Any]) -> PipelineJob:
        configured = deepcopy(dict(plan))
        kind = configured["source_kind"]
        source = str(configured["source"])
        media = configured["media"]
        identity = deepcopy(configured)
        identity.pop("warnings", None)
        identity.pop("requested_range", None)
        if kind == "local":
            stat = Path(source).stat()
            identity["source_stat"] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
        fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=True).encode("utf-8")).hexdigest()
        if configured["delivery"].get("force"):
            fingerprint += "-" + uuid.uuid4().hex[:12]
        delivery = configured["delivery"]
        delivery["project_path"] = str(self.manager.store.path.parent / "artifacts" / f"{fingerprint}.autocutter")
        if kind == "twitch":
            delivery["download_dir"] = str(default_download_dir() / fingerprint[:24])
        metadata = {
            "source_kind": kind, "request_fingerprint": fingerprint,
            "source_duration_s": media["duration_s"], "preset": configured["preset"],
            "export_settings": configured["export_settings"], "delivery": delivery,
        }
        if kind == "local":
            metadata["source_range"] = configured["range"]
        else:
            metadata["twitch"] = media
        job, _created = self.manager.create_configured_job(
            vod_id=f"{kind}:{media.get('id', '')}:{fingerprint}",
            vod_url=Path(source).as_uri() if kind == "local" else source,
            start_s=configured["range"]["start_s"], end_s=configured["range"]["end_s"],
            source_title=media["title"], channel_id=media.get("channel_id", ""),
            metadata=metadata, local_source_path=source if kind == "local" else None,
        )
        for warning in configured.get("warnings", []):
            self._event(job.id, "warning", message=warning)
        return job

    def run(
        self, job_id: str, *, resume: bool = False, cancellation: PipelineCancellation | None = None,
    ) -> PipelineJob:
        token = cancellation or PipelineCancellation()
        with self.manager.execution_lock(job_id), cancellable_probe_scope(token):
            job = self.manager.get(job_id)
            if job.state == PipelineState.DONE:
                self._validate_completed(job)
                self._cleanup_download(job)
                return self.manager.get(job.id)
            if job.state in {PipelineState.FAILED, PipelineState.CANCELLED}:
                if job.error_code == "upload_outcome_unknown" and not job.youtube_video_id:
                    raise PipelineRuntimeError("upload_outcome_unknown", "Upload outcome requires remote reconciliation before a new upload can be attempted. The existing session was retained.", 40)
                if not resume and job.error_code != "interrupted":
                    code = "cancelled" if job.state == PipelineState.CANCELLED else (job.error_code or "job_failed")
                    raise PipelineRuntimeError(code, "Use resume to retry this job.", 70 if code == "cancelled" else self._exit_for(job.retry_state))
                job = self.manager.retry(job.id)
            try:
                while job.state != PipelineState.DONE:
                    token.raise_if_cancelled()
                    if job.state in {PipelineState.ANALYZING, PipelineState.READY_EXPORT, PipelineState.EXPORTING, PipelineState.READY_UPLOAD}:
                        job = self.manager.reconcile_artifacts(
                            job.id, strict_legacy=True, duration_probe=self._duration_probe,
                            video_probe=self._video_probe, audio_probe=self._audio_probe,
                        )
                        if job.state == PipelineState.FAILED:
                            raise PipelineRuntimeError(job.error_code or "artifact_invalid", job.error_message or "Job artifacts are unavailable.", 60 if job.error_code == "source_missing" else self._exit_for(job.retry_state))
                        if job.state == PipelineState.DONE:
                            break
                    if job.state == PipelineState.DOWNLOADING:
                        job = self._stage(job, "download", self._download_factory, DownloadCancellation(), token)
                        self._record_download(job)
                    elif job.state == PipelineState.ANALYZING:
                        job = self._stage(job, "analysis", self._analysis_factory, AnalysisCancellation(), token)
                        self._analysis_metrics(job)
                    elif job.state in {PipelineState.READY_EXPORT, PipelineState.EXPORTING}:
                        destination = reserve_output(self.manager, job)
                        with output_execution_lock(destination).hold(blocking=False):
                            job = self._stage(job, "export", self._export_factory, ExportCancellation(), token)
                        self._record_export(job)
                        self._event(job.id, "validation", message="Export validated.", progress=100)
                    elif job.state in {PipelineState.READY_UPLOAD, PipelineState.UPLOADING}:
                        if not job.youtube_video_id and not self._valid_export(job):
                            if job.state == PipelineState.UPLOADING:
                                self.manager.fail(job.id, "upload_outcome_unknown", "Export is invalid during an interrupted upload; reconcile the retained upload session before repairing it.")
                                raise PipelineRuntimeError("upload_outcome_unknown", "Upload outcome requires remote reconciliation. The export and encrypted upload session were retained.", 40)
                            job = self.manager.reconcile_stage(job.id, PipelineState.READY_EXPORT)
                            continue
                        delivery = job.metadata.get("delivery", {})
                        if delivery.get("youtube") is True:
                            factory = self._upload_factory or self._default_upload_factory
                            job = self._stage(job, "youtube", factory, token, token)
                        else:
                            job = self.manager.complete_without_upload(job.id)
                    else:
                        raise PipelineRuntimeError("incomplete_request", f"Job needs a selected range ({job.state.value}).", 2)
                self._cleanup_download(job)
                self._event(job.id, "done", progress=100, output=job.export_path, youtube_video_id=job.youtube_video_id)
                return self.manager.get(job.id)
            except BaseException as exc:
                if token.cancelled or isinstance(exc, KeyboardInterrupt) or getattr(exc, "code", "").endswith("cancelled"):
                    token.cancel()
                    if self.manager.get(job.id).state != PipelineState.DONE:
                        self.manager.cancel(job.id)
                    raise PipelineRuntimeError("cancelled", "Pipeline cancelled.", 70) from exc
                if isinstance(exc, PipelineJobBusyError):
                    raise
                if isinstance(exc, FileLockBusyError):
                    raise PipelineJobBusyError(str(exc)) from exc
                if isinstance(exc, Exception):
                    current = self.manager.get(job.id)
                    if not current.state.is_terminal:
                        self.manager.fail(job.id, getattr(exc, "code", "pipeline_failed"), redact_secrets(str(exc)))
                    if isinstance(exc, PipelineRuntimeError):
                        raise
                    retry_stage = current.retry_state if current.state == PipelineState.FAILED else current.state
                    explicit_exit = getattr(exc, "exit_code", None)
                    exit_code = explicit_exit if isinstance(explicit_exit, int) else self._exit_for(retry_stage)
                    raise PipelineRuntimeError(getattr(exc, "code", "pipeline_failed"), redact_secrets(str(exc)), exit_code) from exc
                raise

    @staticmethod
    def _default_upload_factory(manager: PipelineManager) -> Any:
        try:
            module = importlib.import_module("automation.uploader")
            return module.PipelineUploadService(manager)
        except ImportError as exc:
            raise PipelineRuntimeError("youtube_dependency_missing", "Install the YouTube runtime dependencies before uploading.", 50) from exc

    def _stage(
        self, job: PipelineJob, stage: str, factory: ServiceFactory,
        stage_token: CancellationTarget, cancellation: PipelineCancellation,
    ) -> PipelineJob:
        self._event(job.id, stage, progress=0, message="Starting")
        if stage_token is not cancellation:
            cancellation.attach(stage_token)
        try:
            cancellation.raise_if_cancelled()
            kwargs: dict[str, Any] = {
                "cancellation": stage_token,
                "on_progress": lambda value: self._event(job.id, stage, progress=value),
            }
            if stage == "export":
                kwargs["on_detail"] = lambda text: self._event(job.id, stage, message=text)
            completed: PipelineJob = factory(self.manager).execute(job.id, **kwargs)
            cancellation.raise_if_cancelled()
            return completed
        finally:
            if stage_token is not cancellation:
                cancellation.detach(stage_token)

    @staticmethod
    def _valid_project(job: PipelineJob) -> bool:
        return PipelineProjectAnalyzer().cached_result(job) is not None

    def _valid_export(self, job: PipelineJob) -> bool:
        if not job.export_path:
            return False
        try:
            artifact = job.metadata.get("export_artifact", {})
            self._artifact_validator.validate_artifact(job.export_path, expected_signature=artifact.get("signature"))
            return True
        except Exception:
            return False

    def _record_export(self, job: PipelineJob) -> None:
        if job.export_path and not job.metadata.get("export_artifact", {}).get("signature"):
            manifest = self._artifact_validator.validate_artifact(job.export_path)
            self.manager.update_metadata(job.id, {"export_artifact": {"signature": manifest["signature"]}})

    def _analysis_metrics(self, job: PipelineJob) -> None:
        try:
            payload = json.loads(Path(str(job.project_path)).read_text(encoding="utf-8"))
            cuts = [cut for track in payload.get("tracks", []) for cut in track.get("cuts", [])]
            removed = sum(max(0.0, float(cut["end"]) - float(cut["start"])) for cut in cuts)
            self._event(job.id, "cuts", cuts_count=len(cuts), removed_s=removed,
                        preset=job.metadata.get("preset", {}).get("name", "Classic"))
        except (OSError, ValueError, TypeError, KeyError):
            pass

    def _validate_completed(self, job: PipelineJob) -> None:
        if not job.youtube_video_id and not self._valid_export(job):
            raise PipelineRuntimeError("output_missing", "Completed job output is missing or invalid; create a new job to render it again.", 30)
        self._event(job.id, "done", progress=100, output=job.export_path, youtube_video_id=job.youtube_video_id)

    def _record_download(self, job: PipelineJob) -> None:
        if job.metadata.get("source_kind") != "twitch" or not job.local_source_path:
            return
        stat = Path(job.local_source_path).stat()
        self.manager.update_metadata(job.id, {"owned_download": {
            "path": job.local_source_path, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
        }})

    def _cleanup_download(self, job: PipelineJob) -> None:
        delivery = job.metadata.get("delivery", {})
        owned = job.metadata.get("owned_download")
        if delivery.get("keep_source") or job.metadata.get("source_kind") != "twitch" or not isinstance(owned, dict):
            return
        try:
            source = Path(owned["path"]).resolve()
            root = Path(delivery["download_dir"]).resolve()
            if source.parent != root or str(source) != job.local_source_path:
                return
            if job.export_path and source == Path(job.export_path).resolve():
                return
            stat = source.stat()
            if stat.st_size == owned["size"] and stat.st_mtime_ns == owned["mtime_ns"]:
                source.unlink()
                self.manager.update_metadata(job.id, {"source_cleaned": True})
        except (OSError, KeyError, TypeError):
            self._event(job.id, "warning", message="Downloaded source could not be cleaned; it was retained.")

    def _event(self, job_id: str, stage: str, **values: Any) -> None:
        if self.on_event is not None:
            if "message" in values:
                values["message"] = redact_secrets(str(values["message"]))
            self.on_event({"job_id": job_id, "stage": stage, **values})

    @staticmethod
    def _exit_for(state: PipelineState | None) -> int:
        if state is None:
            return 2
        return {
            PipelineState.DOWNLOADING: 10, PipelineState.ANALYZING: 20,
            PipelineState.READY_EXPORT: 30, PipelineState.EXPORTING: 30,
            PipelineState.READY_UPLOAD: 40, PipelineState.UPLOADING: 40,
        }.get(state, 2)
