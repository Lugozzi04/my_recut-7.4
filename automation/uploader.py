from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from automation.execution import job_execution
from automation.manager import PipelineManager
from automation.models import PipelineJob, PipelineState
from automation.retry import automatic_retry_allowed
from integrations.youtube.auth import YouTubeAuthError, YouTubeSession, check_cancelled
from integrations.youtube.uploader import UploadCancellation, UploadResult, YouTubeUploadError, YouTubeUploader


class ProjectUploader(Protocol):
    def upload(self, job: PipelineJob, *, cancellation: Any = None,
               on_progress: Callable[[int], None] | None = None,
               on_video_created: Callable[[str], None],
               on_attempt_started: Callable[[], None]) -> UploadResult: ...


class PipelineUploadService:
    """Shared GUI/CLI upload stage, using the existing job manager and store."""

    def __init__(self, manager: PipelineManager, *, session: YouTubeSession | None = None,
                 uploader_factory: Callable[[], ProjectUploader] | None = None,
                 artifact_validator: Callable[[PipelineJob], object] | None = None) -> None:
        self.manager = manager
        self.session = session
        self._uploader_factory = uploader_factory or (lambda: YouTubeUploader(self.session))
        self._artifact_validator = artifact_validator or self._validate_export

    @staticmethod
    def _validate_export(job: PipelineJob) -> object:
        from automation.exporter import PipelineProjectExporter
        artifact = job.metadata.get("export_artifact", {})
        signature = artifact.get("signature") if isinstance(artifact, Mapping) else None
        return PipelineProjectExporter().validate_artifact(job.export_path or "", expected_signature=signature)

    @job_execution
    def execute(self, job_id: str, *, cancellation: Any = None,
                on_progress: Callable[[int], None] | None = None) -> PipelineJob:
        token = cancellation or UploadCancellation()
        job = self.manager.get(job_id)
        upload_states = {PipelineState.READY_UPLOAD, PipelineState.UPLOADING}
        if job.state == PipelineState.CANCELLED and job.metadata.get("cancelled_from") in {state.value for state in upload_states}:
            job = self.manager.resume_cancelled(job.id)
        if job.state == PipelineState.FAILED and job.retry_state in upload_states:
            if job.error_code == "upload_outcome_unknown" and not job.youtube_video_id:
                raise YouTubeUploadError("upload_outcome_unknown", "Resolve the prior YouTube upload and record its video id before resuming this job.")
            job = self.manager.retry(job.id)
        if job.state not in upload_states:
            raise YouTubeUploadError("invalid_upload_state", f"Job {job.id} cannot upload from {job.state.value}.")
        delivery = job.metadata.get("delivery", {})
        if not isinstance(delivery, Mapping) or delivery.get("youtube") is not True:
            raise YouTubeUploadError("upload_not_requested", "This job has not opted in to YouTube delivery.")
        if job.state == PipelineState.READY_UPLOAD:
            youtube = job.metadata.get("youtube")
            if not isinstance(youtube, Mapping) or not youtube.get("title"):
                title = str(delivery.get("title") or job.source_title or "Auto Cutter export")
                if delivery.get("title_explicit") is not True:
                    title = title.replace("<", "").replace(">", "").strip()[:100] or "Auto Cutter export"
                self.manager.set_upload_metadata(
                    job.id, title=title, description=str(delivery.get("description", "")),
                    thumbnail_path=delivery.get("thumbnail_path") or delivery.get("thumbnail") or None,
                )
            job = self.manager.start_upload(job.id)
        persisted = int(job.progress)

        def report(progress: int) -> None:
            nonlocal persisted
            value = max(persisted, min(100, int(progress)))
            if value > persisted:
                self.manager.update_progress(job.id, value)
                persisted = value
            if on_progress is not None:
                on_progress(value)

        def created(identifier: str) -> None:
            self.manager.record_youtube_video_id(job.id, identifier)

        def attempted() -> None:
            self.manager.update_metadata(job.id, {"upload_attempt_started": True})

        try:
            check_cancelled(token)
            if not job.youtube_video_id:
                self._artifact_validator(job)
            result = self._uploader_factory().upload(
                job, cancellation=token, on_progress=report,
                on_video_created=created, on_attempt_started=attempted,
            )
            created(result.video_id)
            self.manager.update_metadata(job.id, {"youtube": {"privacy_status": "private", "thumbnail_applied": result.thumbnail_applied}})
            check_cancelled(token)
            return self.manager.mark_uploaded(job.id, result.video_id)
        except (YouTubeAuthError, YouTubeUploadError) as exc:
            self._fail_active_job(job.id, exc.code, str(exc), retryable=getattr(exc, "retryable", False))
            raise
        except Exception as exc:
            message = "Unexpected YouTube delivery failure; the persisted video id and resumable checkpoint were retained."
            self._fail_active_job(job.id, "upload_failed", message)
            raise YouTubeUploadError("upload_failed", message) from exc

    def _fail_active_job(self, job_id: str, code: str, message: str, *, retryable: bool = False) -> None:
        try:
            if self.manager.get(job_id).state == PipelineState.UPLOADING:
                self.manager.fail(job_id, code, message, automatic_retry=retryable and automatic_retry_allowed())
        except Exception:
            pass


class PipelineUploadQueue:
    """Single worker adapter matching the existing persistent stage queues."""

    def __init__(self, manager: PipelineManager, *, session: YouTubeSession | None = None,
                 uploader_factory: Callable[[], ProjectUploader] | None = None,
                 artifact_validator: Callable[[PipelineJob], object] | None = None,
                 on_started: Callable[[str], None] | None = None,
                 on_progress: Callable[[str, int], None] | None = None,
                 on_finished: Callable[[PipelineJob], None] | None = None,
                 on_failed: Callable[[PipelineJob, str], None] | None = None) -> None:
        self.manager = manager
        self.service = PipelineUploadService(manager, session=session, uploader_factory=uploader_factory,
                                            artifact_validator=artifact_validator)
        self.on_started, self.on_progress = on_started, on_progress
        self.on_finished, self.on_failed = on_finished, on_failed
        self._condition = threading.Condition()
        self._pending: deque[str] = deque()
        self._queued: set[str] = set()
        self._active_job_id = ""
        self._active_cancellation: UploadCancellation | None = None
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
        states = {PipelineState.READY_UPLOAD, PipelineState.UPLOADING}
        allowed = job.state == PipelineState.READY_UPLOAD or (job.state == PipelineState.FAILED and job.retry_state in states)
        allowed = allowed or (job.state == PipelineState.CANCELLED and job.metadata.get("cancelled_from") in {state.value for state in states})
        if not allowed:
            raise YouTubeUploadError("invalid_upload_state", f"Job {job.id} cannot be queued from {job.state.value}.")
        with self._condition:
            if self._stopping:
                raise YouTubeUploadError("upload_queue_stopped", "The upload queue is shutting down.")
            if job.id == self._active_job_id or job.id in self._queued:
                return False
            self._pending.append(job.id)
            self._queued.add(job.id)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="pipeline-youtube-queue", daemon=True)
                self._thread.start()
            self._condition.notify_all()
            return True

    def cancel_current(self) -> bool:
        with self._condition:
            token, job_id = self._active_cancellation, self._active_job_id
        if token is None:
            return False
        token.cancel()
        if job_id and self.manager.get(job_id).state != PipelineState.DONE:
            self.manager.cancel(job_id)
        return True

    def shutdown(self, timeout: float = 3.0) -> bool:
        with self._condition:
            self._stopping = True
            self._pending.clear()
            self._queued.clear()
            token, thread, active_id = self._active_cancellation, self._thread, self._active_job_id
            self._condition.notify_all()
        if active_id:
            self.manager.interrupt(active_id)
        if token is not None:
            token.cancel()
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
                token = UploadCancellation()
                self._active_job_id, self._active_cancellation = job_id, token
            self._notify(self.on_started, job_id)
            try:
                completed = self.service.execute(job_id, cancellation=token,
                    on_progress=lambda value: self._notify(self.on_progress, job_id, value))
                self._notify(self.on_finished, completed)
            except Exception as exc:
                self._notify(self.on_failed, self.manager.get(job_id), str(exc))
            finally:
                with self._condition:
                    self._active_job_id, self._active_cancellation = "", None
                    self._condition.notify_all()

    @staticmethod
    def _notify(callback: Callable[..., None] | None, *args: object) -> None:
        if callback is not None:
            try:
                callback(*args)
            except Exception:
                pass
