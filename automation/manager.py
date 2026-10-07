from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any

from automation.locking import FileLockBusyError
from automation.models import (
    PipelineJob,
    PipelineState,
    PipelineTransitionError,
    utc_now,
)
from automation.store import PipelineStore


class PipelineJobNotFoundError(LookupError):
    pass


class PipelineArtifactError(ValueError):
    pass


class PipelineJobBusyError(RuntimeError):
    code = "job_busy"


_RECOVERY_RETRY_STATE: dict[PipelineState, PipelineState] = {
    PipelineState.DOWNLOADING: PipelineState.DOWNLOADING,
    PipelineState.ANALYZING: PipelineState.ANALYZING,
    PipelineState.EXPORTING: PipelineState.READY_EXPORT,
    PipelineState.UPLOADING: PipelineState.READY_UPLOAD,
}


def _detached(job: PipelineJob) -> PipelineJob:
    return PipelineJob.from_mapping(job.to_mapping())


def _existing_file(path: str | Path, label: str) -> str:
    target = Path(path).expanduser()
    if not target.is_file():
        raise PipelineArtifactError(f"{label} does not exist or is not a file: {target}")
    return str(target.resolve())


class PipelineManager:
    """Coordinates durable, validated mutations of automation jobs."""

    def __init__(self, store: PipelineStore | None = None) -> None:
        self.store = store or PipelineStore()

    @contextmanager
    def execution_lock(
        self,
        job_id: str,
        *,
        blocking: bool = False,
        timeout: float | None = None,
        reentrant: bool = True,
    ) -> Iterator[None]:
        """Claim one job for an executor, without blocking work on other jobs."""
        try:
            with self.store.job_lock(job_id).hold(blocking=blocking, timeout=timeout, reentrant=reentrant):
                yield
        except FileLockBusyError as exc:
            raise PipelineJobBusyError(f"Pipeline job {job_id} is already being executed.") from exc

    def list_jobs(self, *, include_terminal: bool = True) -> list[PipelineJob]:
        jobs = self.store.load()
        if not include_terminal:
            jobs = [job for job in jobs if not job.state.is_terminal]
        return jobs

    def get(self, job_id: str) -> PipelineJob:
        job = self.store.get(job_id)
        if job is None:
            raise PipelineJobNotFoundError(f"Pipeline job not found: {job_id}")
        return job

    def discover_vod(
        self,
        *,
        vod_id: str,
        vod_url: str,
        channel_id: str = "",
        source_title: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[PipelineJob, bool]:
        requested_vod_id = str(vod_id).strip()
        def apply(jobs: list[PipelineJob]) -> tuple[PipelineJob, bool]:
            for existing in jobs:
                if existing.vod_id == requested_vod_id:
                    return _detached(existing), False

            job = PipelineJob.create(
                vod_id=requested_vod_id,
                vod_url=vod_url,
                channel_id=channel_id,
                source_title=source_title,
                metadata=metadata,
            )
            jobs.append(job)
            return _detached(job), True
        return self.store.transaction(apply)

    def request_range(self, job_id: str) -> PipelineJob:
        return self._mutate(job_id, lambda job: job.transition(PipelineState.WAITING_RANGE))

    def create_configured_job(
        self,
        *,
        vod_id: str,
        vod_url: str,
        start_s: float,
        end_s: float,
        source_title: str = "",
        channel_id: str = "",
        metadata: Mapping[str, Any] | None = None,
        local_source_path: str | Path | None = None,
    ) -> tuple[PipelineJob, bool]:
        """Commit a complete unattended request without an unqueued range gap."""
        source = _existing_file(local_source_path, "Local source") if local_source_path is not None else None

        def apply(jobs: list[PipelineJob]) -> tuple[PipelineJob, bool]:
            for existing in jobs:
                if existing.vod_id == vod_id:
                    return _detached(existing), False
            job = PipelineJob.create(
                vod_id=vod_id, vod_url=vod_url, source_title=source_title,
                channel_id=channel_id, metadata=metadata,
            )
            job.transition(PipelineState.WAITING_RANGE)
            job.select_range(start_s, end_s)
            if source is not None:
                job.local_source_path = source
                job.transition(PipelineState.ANALYZING)
            jobs.append(job)
            return _detached(job), True

        return self.store.transaction(apply)

    def reconcile_stage(self, job_id: str, target: PipelineState) -> PipelineJob:
        """Requeue a missing artifact while retaining the immutable request snapshot.

        Callers must hold the execution lock and validate artifacts first. This
        deliberately permits only the backwards repairs needed by recovery.
        """
        allowed = {
            PipelineState.DOWNLOADING: {PipelineState.ANALYZING, PipelineState.READY_EXPORT, PipelineState.EXPORTING},
            PipelineState.ANALYZING: {PipelineState.READY_EXPORT, PipelineState.EXPORTING},
            PipelineState.READY_EXPORT: {PipelineState.READY_UPLOAD},
        }

        def apply(job: PipelineJob) -> None:
            if job.state not in allowed.get(target, set()):
                raise PipelineTransitionError(f"Cannot reconcile {job.state.value} to {target.value}.")
            if target == PipelineState.DOWNLOADING and job.metadata.get("source_kind") == "local":
                raise PipelineArtifactError("An original local source cannot be downloaded again.")
            job.state = target
            job.progress = 0.0
            job.retry_state = None
            job.error_code = None
            job.error_message = None
            job.updated_at = utc_now()
            if target == PipelineState.DOWNLOADING:
                job.local_source_path = None
            if target in {PipelineState.DOWNLOADING, PipelineState.ANALYZING}:
                job.project_path = None
            job.export_path = None
            job.validate()

        return self._mutate(job_id, apply)

    def select_range(self, job_id: str, start_s: float, end_s: float) -> PipelineJob:
        return self._mutate(job_id, lambda job: job.select_range(start_s, end_s))

    def update_progress(self, job_id: str, progress: float) -> PipelineJob:
        return self._mutate(job_id, lambda job: job.update_progress(progress))

    def mark_downloaded(self, job_id: str, source_path: str | Path) -> PipelineJob:
        resolved = _existing_file(source_path, "Downloaded source")

        def apply(job: PipelineJob) -> None:
            job.transition(PipelineState.ANALYZING)
            job.local_source_path = resolved

        return self._mutate(job_id, apply)

    def mark_analyzed(self, job_id: str, project_path: str | Path) -> PipelineJob:
        resolved = _existing_file(project_path, "Auto Cutter project")

        def apply(job: PipelineJob) -> None:
            job.transition(PipelineState.READY_EXPORT)
            job.project_path = resolved

        return self._mutate(job_id, apply)

    def start_export(self, job_id: str) -> PipelineJob:
        return self._mutate(job_id, lambda job: job.transition(PipelineState.EXPORTING))

    def mark_exported(self, job_id: str, export_path: str | Path) -> PipelineJob:
        resolved = _existing_file(export_path, "Exported video")

        def apply(job: PipelineJob) -> None:
            job.transition(PipelineState.READY_UPLOAD)
            job.export_path = resolved

        return self._mutate(job_id, apply)

    def set_upload_metadata(
        self,
        job_id: str,
        *,
        title: str,
        description: str = "",
        thumbnail_path: str | Path | None = None,
    ) -> PipelineJob:
        clean_title = str(title).strip()
        if not clean_title:
            raise PipelineArtifactError("A YouTube title is required.")
        thumbnail = _existing_file(thumbnail_path, "Thumbnail") if thumbnail_path is not None else None

        def apply(job: PipelineJob) -> None:
            if job.state != PipelineState.READY_UPLOAD:
                raise PipelineTransitionError("Upload metadata can only be set when the export is ready.")
            youtube = {
                "title": clean_title,
                "description": str(description),
                "thumbnail_path": thumbnail,
            }
            job.metadata["youtube"] = youtube
            job.updated_at = utc_now()
            job.validate()

        return self._mutate(job_id, apply)

    def start_upload(self, job_id: str) -> PipelineJob:
        def apply(job: PipelineJob) -> None:
            youtube = job.metadata.get("youtube")
            title = youtube.get("title") if isinstance(youtube, dict) else None
            if not str(title or "").strip():
                raise PipelineArtifactError("Set a YouTube title before starting the upload.")
            if not job.youtube_video_id and (not job.export_path or not Path(job.export_path).is_file()):
                raise PipelineArtifactError("The exported video is missing.")
            job.transition(PipelineState.UPLOADING)

        return self._mutate(job_id, apply)

    def mark_uploaded(self, job_id: str, youtube_video_id: str) -> PipelineJob:
        video_id = str(youtube_video_id).strip()
        if not video_id:
            raise PipelineArtifactError("YouTube video id is required.")

        def apply(job: PipelineJob) -> None:
            job.transition(PipelineState.DONE)
            job.youtube_video_id = video_id

        return self._mutate(job_id, apply)

    def record_youtube_video_id(self, job_id: str, youtube_video_id: str) -> PipelineJob:
        """Persist an accepted upload before thumbnail work, without completing it."""
        video_id = str(youtube_video_id).strip()
        if not video_id:
            raise PipelineArtifactError("YouTube video id is required.")

        def apply(job: PipelineJob) -> None:
            upload_retry = job.state == PipelineState.FAILED and job.retry_state in {
                PipelineState.UPLOADING, PipelineState.READY_UPLOAD,
            }
            cancelled_upload = job.state == PipelineState.CANCELLED and job.metadata.get("cancelled_from") in {
                PipelineState.READY_UPLOAD.value, PipelineState.UPLOADING.value,
            }
            if job.state not in {PipelineState.READY_UPLOAD, PipelineState.UPLOADING} and not upload_retry and not cancelled_upload:
                raise PipelineTransitionError("A YouTube video id can only be recorded during upload delivery.")
            if job.youtube_video_id and job.youtube_video_id != video_id:
                raise PipelineArtifactError("This job already has a different YouTube video id.")
            job.youtube_video_id = video_id
            job.updated_at = utc_now()

        return self._mutate(job_id, apply)

    def complete_without_upload(self, job_id: str) -> PipelineJob:
        return self._mutate(job_id, lambda job: job.complete_without_upload())

    def update_metadata(
        self,
        job_id: str,
        updates: Mapping[str, Any],
        *,
        deep_merge: bool = True,
    ) -> PipelineJob:
        """Atomically merge a detached configuration/artifact snapshot into metadata."""
        if not isinstance(updates, Mapping):
            raise PipelineArtifactError("Metadata updates must be an object.")
        copied = deepcopy(dict(updates))

        def merge(target: dict[str, Any], values: Mapping[str, Any]) -> None:
            for key, value in values.items():
                existing = target.get(key)
                if deep_merge and isinstance(existing, dict) and isinstance(value, Mapping):
                    merge(existing, value)
                else:
                    target[key] = deepcopy(value)

        def apply(job: PipelineJob) -> None:
            merge(job.metadata, copied)
            job.updated_at = utc_now()

        return self._mutate(job_id, apply)

    def fail(self, job_id: str, code: str, message: str, *, automatic_retry: bool = False) -> PipelineJob:
        def apply(job: PipelineJob) -> None:
            job.fail(code, message)
            if automatic_retry and job.retry_state in _RECOVERY_RETRY_STATE:
                job.metadata["automatic_retry_pending"] = True
            else:
                job.metadata.pop("automatic_retry_pending", None)
        return self._mutate(job_id, apply)

    def retry(self, job_id: str) -> PipelineJob:
        def apply(job: PipelineJob) -> None:
            if job.state == PipelineState.CANCELLED:
                job.resume_cancelled()
            else:
                job.retry()
            job.metadata.pop("automatic_retry_pending", None)
        return self._mutate(job_id, apply)

    def resume_cancelled(self, job_id: str) -> PipelineJob:
        def apply(job: PipelineJob) -> None:
            job.resume_cancelled()
            job.metadata.pop("automatic_retry_pending", None)
        return self._mutate(job_id, apply)

    def cancel(self, job_id: str) -> PipelineJob:
        return self._mutate(job_id, lambda job: job.cancel())

    def prepare_automatic_retry(self, job_id: str) -> PipelineJob:
        """Durably record the known transient retry before entering backoff."""
        def apply(job: PipelineJob) -> None:
            if job.state == PipelineState.FAILED and job.retry_state in _RECOVERY_RETRY_STATE:
                job.metadata["automatic_retry_pending"] = True
                job.updated_at = utc_now()
        return self._mutate(job_id, apply)

    def interrupt(self, job_id: str) -> PipelineJob:
        """Persist shutdown interruption without turning user cancellation into retry."""
        def apply(job: PipelineJob) -> None:
            if job.state in _RECOVERY_RETRY_STATE:
                previous = job.state
                job.fail("interrupted", f"Auto Cutter closed while the job was {previous.value}.")
                job.retry_state = _RECOVERY_RETRY_STATE[previous]
            elif job.state == PipelineState.FAILED and (
                str(job.error_code or "").endswith("cancelled") or job.metadata.get("automatic_retry_pending") is True
            ):
                retry_stage = job.retry_state
                if retry_stage is None or retry_stage not in _RECOVERY_RETRY_STATE:
                    return
                job.error_code = "interrupted"
                job.error_message = f"Auto Cutter closed while the job was {retry_stage.value}."
                job.retry_state = _RECOVERY_RETRY_STATE[retry_stage]
                job.updated_at = utc_now()
            if job.error_code == "interrupted":
                job.metadata.pop("automatic_retry_pending", None)
            job.validate()
        return self._mutate(job_id, apply)

    def delete(self, job_id: str) -> bool:
        return self.store.delete(job_id)

    def recover_interrupted_jobs(self) -> list[PipelineJob]:
        recovered: list[PipelineJob] = []
        for snapshot in self.list_jobs():
            retry_pending = snapshot.state == PipelineState.FAILED and snapshot.metadata.get("automatic_retry_pending") is True
            if snapshot.state not in _RECOVERY_RETRY_STATE and not retry_pending:
                continue
            try:
                with self.execution_lock(snapshot.id, reentrant=False):
                    current = self.get(snapshot.id)
                    still_pending = current.state == PipelineState.FAILED and current.metadata.get("automatic_retry_pending") is True
                    if current.state not in _RECOVERY_RETRY_STATE and not still_pending:
                        continue
                    job = self.interrupt(current.id)
                    if job.to_mapping() != current.to_mapping():
                        recovered.append(job)
            except (PipelineJobBusyError, PipelineJobNotFoundError):
                continue
        return recovered

    def reconcile_artifacts(
        self, job_id: str, *, strict_legacy: bool = False,
        duration_probe: Callable[[str], float] | None = None,
        video_probe: Callable[[str], bool] | None = None,
        audio_probe: Callable[[str], bool] | None = None,
    ) -> PipelineJob:
        """Repair queue stages from artifacts using the shared engines' validators.

        Legacy queue jobs retain their old stage-service validation behavior;
        configured requests opt in through source_kind/preset snapshots. CLI
        resume can request the stricter reconciliation for older jobs as well.
        """
        from automation.analyzer import PipelineProjectAnalyzer
        from automation.downloader import FfmpegRangeDownloader
        from automation.exporter import PipelineProjectExporter
        from utils.ffmpeg import ffprobe_duration_seconds, has_audio_stream, has_video_stream

        probe = duration_probe or ffprobe_duration_seconds
        video = video_probe or has_video_stream
        audio = audio_probe or has_audio_stream
        with self.execution_lock(job_id):
            job = self.get(job_id)
            stage = job.retry_state if job.state == PipelineState.FAILED else job.state
            if job.state.is_terminal and not (job.state == PipelineState.FAILED and job.error_code == "interrupted"):
                return job
            delivery = job.metadata.get("delivery", {})
            artifact = job.metadata.get("export_artifact", {})
            signature = artifact.get("signature") if isinstance(artifact, Mapping) else None
            if stage == PipelineState.READY_UPLOAD and isinstance(delivery, Mapping) and delivery.get("youtube") is False:
                try:
                    PipelineProjectExporter(duration_probe=probe, video_probe=video, audio_probe=audio).validate_artifact(
                        str(job.export_path or ""), expected_signature=str(signature) if signature else None,
                    )
                except Exception:
                    if job.state == PipelineState.FAILED:
                        job = self.retry(job.id)
                    job = self.reconcile_stage(job.id, PipelineState.READY_EXPORT)
                    stage = job.state
                else:
                    if job.state == PipelineState.FAILED:
                        job = self.retry(job.id)
                    return self.complete_without_upload(job.id)
            if stage not in {PipelineState.ANALYZING, PipelineState.READY_EXPORT, PipelineState.EXPORTING}:
                return job
            output = job.export_path or (delivery.get("resolved_output_path") if isinstance(delivery, Mapping) else None)
            if stage in {PipelineState.READY_EXPORT, PipelineState.EXPORTING} and output and signature:
                try:
                    PipelineProjectExporter(duration_probe=probe, video_probe=video, audio_probe=audio).validate_artifact(
                        str(output), expected_signature=str(signature),
                    )
                except Exception:
                    pass
                else:
                    if job.state == PipelineState.FAILED:
                        job = self.retry(job.id)
                    if job.state == PipelineState.READY_EXPORT:
                        job = self.start_export(job.id)
                    completed = self.mark_exported(job.id, str(output))
                    if isinstance(delivery, Mapping) and delivery.get("youtube") is False:
                        return self.complete_without_upload(job.id)
                    return completed
            configured = job.metadata.get("source_kind") in {"local", "twitch"} or isinstance(job.metadata.get("preset"), Mapping)
            if not strict_legacy and not configured:
                return job
            local = job.metadata.get("source_kind") == "local"
            expected = None if local else (job.range_end_s or 0.0) - (job.range_start_s or 0.0)
            source_valid = False
            if job.local_source_path:
                try:
                    FfmpegRangeDownloader(duration_probe=probe, video_probe=video).validate_artifact(job.local_source_path, expected)
                    source_valid = True
                except Exception:
                    pass
            if not source_valid:
                if job.state == PipelineState.FAILED:
                    job = self.retry(job.id)
                if local:
                    return self.fail(job.id, "source_missing", "Original local source is missing or invalid; restore the source before resuming.")
                return self.reconcile_stage(job.id, PipelineState.DOWNLOADING)
            if stage in {PipelineState.READY_EXPORT, PipelineState.EXPORTING} and PipelineProjectAnalyzer().cached_result(job) is None:
                if job.state == PipelineState.FAILED:
                    job = self.retry(job.id)
                return self.reconcile_stage(job.id, PipelineState.ANALYZING)
            return job

    def reconstruct_queues(
        self,
        download: Callable[[str], object] | None,
        analysis: Callable[[str], object] | None,
        export: Callable[[str], object] | None,
        upload: Callable[[str], object] | None = None,
        *,
        upload_enabled: bool = False,
    ) -> list[PipelineJob]:
        """Recover orphaned work and enqueue pending or interrupted stages only.

        The callback runs after releasing the job lock so a queue's worker can
        acquire it. Each executor still claims/rechecks its stage before work.
        """
        self.recover_interrupted_jobs()
        callbacks = {
            PipelineState.DOWNLOADING: download,
            PipelineState.ANALYZING: analysis,
            PipelineState.READY_EXPORT: export,
            PipelineState.READY_UPLOAD: upload if upload_enabled else None,
        }
        queued: list[PipelineJob] = []
        for snapshot in self.list_jobs():
            try:
                with self.execution_lock(snapshot.id, reentrant=False):
                    job = self.reconcile_artifacts(snapshot.id)
                    stage = job.state
                    if stage == PipelineState.FAILED:
                        if job.error_code != "interrupted" or job.retry_state is None:
                            continue
                        stage = job.retry_state
                    delivery = job.metadata.get("delivery")
                    callback = callbacks.get(stage)
                    if callback is None:
                        continue
                    if stage == PipelineState.READY_UPLOAD:
                        if not isinstance(delivery, Mapping) or delivery.get("youtube") is not True:
                            continue
                if callback(job.id) is not False:
                    queued.append(_detached(job))
            except (PipelineJobBusyError, PipelineJobNotFoundError):
                continue
        return queued

    def _mutate(self, job_id: str, mutation: Callable[[PipelineJob], None]) -> PipelineJob:
        requested = str(job_id).strip()
        def apply(jobs: list[PipelineJob]) -> PipelineJob:
            for job in jobs:
                if job.id != requested:
                    continue
                mutation(job)
                return _detached(job)
            raise PipelineJobNotFoundError(f"Pipeline job not found: {job_id}")
        return self.store.transaction(apply)
