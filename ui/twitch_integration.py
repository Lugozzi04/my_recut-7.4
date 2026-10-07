from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal

from automation.analyzer import PipelineAnalysisQueue
from automation.downloader import PipelineDownloadQueue, default_download_dir
from automation.exporter import PipelineExportQueue
from automation.manager import PipelineManager
from automation.models import PipelineState
from automation.twitch import (
    TwitchApiClient,
    TwitchAuthorizationPending,
)
from automation.twitch_auth import TwitchSession
from automation.twitch_watcher import TwitchVodWatcher


_CLIENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{8,64}$")


def normalize_twitch_client_id(value: str) -> str:
    client_id = str(value or "").strip()
    if not _CLIENT_ID_PATTERN.fullmatch(client_id):
        raise ValueError("Twitch Client ID must contain 8-64 letters or numbers.")
    return client_id


def _default_session_factory(client: TwitchApiClient) -> TwitchSession:
    return TwitchSession(client)


def _default_watcher_factory(
    client: TwitchApiClient,
    session: TwitchSession,
    manager: PipelineManager,
    on_discovered: Callable[..., None],
    on_error: Callable[[Exception], None],
    poll_interval_s: float,
) -> TwitchVodWatcher:
    return TwitchVodWatcher(
        client=client,
        session=session,
        manager=manager,
        poll_interval_s=poll_interval_s,
        on_discovered=on_discovered,
        on_error=on_error,
    )


class TwitchIntegration(QObject):
    deviceCodeReady = Signal(str, str, int)
    connected = Signal(str)
    disconnected = Signal()
    vodDiscovered = Signal(object, object)
    error = Signal(str)
    statusChanged = Signal(str)
    enabledChanged = Signal(bool)
    downloadStarted = Signal(str)
    downloadProgress = Signal(str, int)
    downloadFinished = Signal(object)
    downloadFailed = Signal(object, str)
    analysisStarted = Signal(str)
    analysisProgress = Signal(str, int)
    analysisFinished = Signal(object)
    analysisFailed = Signal(object, str)
    exportStarted = Signal(str)
    exportProgress = Signal(str, int)
    exportDetail = Signal(str, str)
    exportFinished = Signal(object)
    exportFailed = Signal(object, str)
    uploadStarted = Signal(str)
    uploadProgress = Signal(str, int)
    uploadFinished = Signal(object)
    uploadFailed = Signal(object, str)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        manager: PipelineManager | None = None,
        client_factory: Callable[[str], TwitchApiClient] = TwitchApiClient,
        session_factory: Callable[[TwitchApiClient], TwitchSession] = _default_session_factory,
        watcher_factory: Callable[..., TwitchVodWatcher] = _default_watcher_factory,
        download_queue_factory: Callable[..., PipelineDownloadQueue] = PipelineDownloadQueue,
        analysis_queue_factory: Callable[..., PipelineAnalysisQueue] = PipelineAnalysisQueue,
        export_queue_factory: Callable[..., PipelineExportQueue] = PipelineExportQueue,
        download_dir: str | Path | None = None,
    ) -> None:
        super().__init__(parent)
        self.manager = manager or PipelineManager()
        self._client_factory = client_factory
        self._session_factory = session_factory
        self._watcher_factory = watcher_factory
        self._download_queue = download_queue_factory(
            self.manager,
            download_dir or default_download_dir(),
            on_started=self._on_download_started,
            on_progress=self._on_download_progress,
            on_finished=self._on_download_finished,
            on_failed=self._on_download_failed,
        )
        self._analysis_queue = analysis_queue_factory(
            self.manager,
            on_started=self._on_analysis_started,
            on_progress=self._on_analysis_progress,
            on_finished=self._on_analysis_finished,
            on_failed=self._on_analysis_failed,
        )
        self._export_queue = export_queue_factory(
            self.manager,
            on_started=self._on_export_started,
            on_progress=self._on_export_progress,
            on_detail=self._on_export_detail,
            on_finished=self._on_export_finished,
            on_failed=self._on_export_failed,
        )
        self._upload_queue: Any = None
        self._client: TwitchApiClient | None = None
        self._session: TwitchSession | None = None
        self._watcher: TwitchVodWatcher | None = None
        self._client_id = ""
        self._connected_login = ""
        self._enabled = False
        self._auth_cancel = threading.Event()
        self._auth_lock = threading.RLock()
        self._auth_generation = 0
        self._auth_thread: threading.Thread | None = None
        self._poll_lock = threading.Lock()
        self._poll_thread: threading.Thread | None = None

    @property
    def client_id(self) -> str:
        return self._client_id

    @property
    def connected_login(self) -> str:
        return self._connected_login

    @property
    def is_configured(self) -> bool:
        return bool(self._client_id and self._client and self._session and self._watcher)

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    @property
    def is_running(self) -> bool:
        return bool(self._watcher is not None and self._watcher.is_running)

    @property
    def is_authenticating(self) -> bool:
        with self._auth_lock:
            return self._auth_thread is not None and self._auth_thread.is_alive()

    @property
    def download_output_dir(self) -> Path:
        return self._download_queue.output_dir

    @property
    def active_download_job_id(self) -> str:
        return self._download_queue.active_job_id

    @property
    def is_downloading(self) -> bool:
        return self._download_queue.is_running

    @property
    def active_analysis_job_id(self) -> str:
        return self._analysis_queue.active_job_id

    @property
    def is_analyzing(self) -> bool:
        return self._analysis_queue.is_running

    @property
    def active_export_job_id(self) -> str:
        return self._export_queue.active_job_id

    @property
    def is_exporting(self) -> bool:
        return self._export_queue.is_running

    @property
    def is_uploading(self) -> bool:
        return bool(self._upload_queue is not None and self._upload_queue.is_running)

    @property
    def active_upload_job_id(self) -> str:
        return str(self._upload_queue.active_job_id) if self._upload_queue is not None else ""

    @property
    def has_stored_token(self) -> bool:
        if self._session is None:
            return False
        try:
            return bool(self._session.token_store.path.is_file())
        except Exception:
            return False

    def configure(self, client_id: str, *, poll_interval_s: float = 120.0) -> None:
        normalized = normalize_twitch_client_id(client_id)
        self._shutdown_twitch_workers()
        self._enabled = False
        self._connected_login = ""
        client = self._client_factory(normalized)
        session = self._session_factory(client)
        watcher = self._watcher_factory(
            client,
            session,
            self.manager,
            self._on_vod_discovered,
            self._on_watcher_error,
            float(poll_interval_s),
        )
        self._client_id = normalized
        self._client = client
        self._session = session
        self._watcher = watcher
        with self._auth_lock:
            self._auth_cancel = threading.Event()
        self.statusChanged.emit("configured")

    def begin_connect(self, scopes: tuple[str, ...] = ()) -> bool:
        if self._session is None:
            self.error.emit("Configure a Twitch Client ID first.")
            return False
        with self._auth_lock:
            if self._auth_thread is not None and self._auth_thread.is_alive():
                return False
            self._auth_generation += 1
            self._auth_cancel = threading.Event()
            thread = threading.Thread(
                target=self._run_device_authorization,
                args=(tuple(scopes), self._session, self._auth_cancel, self._auth_generation),
                name="twitch-device-auth",
                daemon=True,
            )
            self._auth_thread = thread
            thread.start()
        self.statusChanged.emit("connecting")
        return True

    def set_enabled(self, enabled: bool) -> bool:
        requested = bool(enabled)
        if self._watcher is None:
            self.error.emit("Configure a Twitch Client ID first.")
            return False
        if requested and not self.has_stored_token:
            self.error.emit("Connect a Twitch account before enabling the VOD watcher.")
            return False

        self._enabled = requested
        if requested:
            self._watcher.start()
            self.statusChanged.emit("watching")
        else:
            self._watcher.stop(timeout=2.0)
            self.statusChanged.emit("disabled")
        self.enabledChanged.emit(self._enabled)
        return True

    def poll_now(self) -> bool:
        if self._watcher is None or not self.has_stored_token:
            self.error.emit("Connect a Twitch account before checking for VODs.")
            return False
        with self._poll_lock:
            if self._poll_thread is not None and self._poll_thread.is_alive():
                return False
            thread = threading.Thread(target=self._run_poll_once, name="twitch-vod-check", daemon=True)
            self._poll_thread = thread
            thread.start()
        self.statusChanged.emit("checking")
        return True

    def configure_download_dir(self, path: str | Path) -> None:
        self._download_queue.set_output_dir(path)

    def queue_download(self, job_id: str) -> bool:
        return self._download_queue.enqueue(job_id)

    def recover_pipeline(self) -> list[Any]:
        """Reconstruct work from durable state, including missed handoffs."""
        upload_requested = any(
            job.metadata.get("delivery", {}).get("youtube") is True
            for job in self.manager.list_jobs(include_terminal=True)
            if job.state not in {PipelineState.DONE, PipelineState.CANCELLED}
        )
        return self.manager.reconstruct_queues(
            self._download_queue.enqueue,
            self._analysis_queue.enqueue,
            self._export_queue.enqueue,
            self._ensure_upload_queue().enqueue if upload_requested else None,
            upload_enabled=upload_requested,
        )

    def _ensure_upload_queue(self) -> Any:
        if self._upload_queue is None:
            from automation.uploader import PipelineUploadQueue

            self._upload_queue = PipelineUploadQueue(
                self.manager, on_started=self.uploadStarted.emit,
                on_progress=self.uploadProgress.emit, on_finished=self.uploadFinished.emit,
                on_failed=self.uploadFailed.emit,
            )
        return self._upload_queue

    def queue_upload(self, job_id: str) -> bool:
        return bool(self._ensure_upload_queue().enqueue(job_id))

    def cancel_upload(self) -> bool:
        return bool(self._upload_queue is not None and self._cancel_queue_job(self._upload_queue))

    def cancel_download(self) -> bool:
        return self._cancel_queue_job(self._download_queue)

    def queue_analysis(self, job_id: str) -> bool:
        return self._analysis_queue.enqueue(job_id)

    def cancel_analysis(self) -> bool:
        return self._cancel_queue_job(self._analysis_queue)

    def queue_export(self, job_id: str) -> bool:
        return self._export_queue.enqueue(job_id)

    def cancel_export(self) -> bool:
        return self._cancel_queue_job(self._export_queue)

    def _cancel_queue_job(self, queue: Any) -> bool:
        job_id = str(queue.active_job_id or "")
        cancelled = bool(queue.cancel_current())
        if cancelled and job_id:
            job = self.manager.get(job_id)
            if job.state not in {PipelineState.CANCELLED, PipelineState.DONE}:
                self.manager.cancel(job_id)
        return cancelled

    def disconnect_account(self) -> None:
        with self._auth_lock:
            self._enabled = False
            self._auth_generation += 1
            self._auth_cancel.set()
            self._connected_login = ""
        if self._watcher is not None:
            self._watcher.stop(timeout=2.0)
        if self._session is not None:
            self._session.disconnect()
        self.enabledChanged.emit(False)
        self.disconnected.emit()
        self.statusChanged.emit("disconnected")

    def clear_configuration(self) -> None:
        self.disconnect_account()
        self._client_id = ""
        self._client = None
        self._session = None
        self._watcher = None
        self.statusChanged.emit("unconfigured")

    def shutdown(self) -> None:
        self._shutdown_twitch_workers()
        for queue in (self._download_queue, self._analysis_queue, self._export_queue, self._upload_queue):
            if queue is None:
                continue
            active = str(queue.active_job_id or "")
            queue.shutdown(timeout=3.0)
            if active:
                self.manager.interrupt(active)

    def _shutdown_twitch_workers(self) -> None:
        with self._auth_lock:
            self._auth_generation += 1
            self._auth_cancel.set()
        session = self._session
        invalidate = getattr(session, "cancel_authorization", None)
        if callable(invalidate):
            invalidate()
        watcher = self._watcher
        if watcher is not None:
            watcher.stop(timeout=2.0)
        with self._auth_lock:
            auth_thread = self._auth_thread
        if auth_thread is not None and auth_thread is not threading.current_thread():
            auth_thread.join(timeout=2.0)
        with self._poll_lock:
            poll_thread = self._poll_thread
        if poll_thread is not None and poll_thread is not threading.current_thread():
            poll_thread.join(timeout=2.0)

    def _auth_attempt_current(self, session: Any, cancel: threading.Event, generation: int) -> bool:
        with self._auth_lock:
            return (session is self._session and cancel is self._auth_cancel
                    and generation == self._auth_generation and not cancel.is_set())

    def _run_device_authorization(
        self,
        scopes: tuple[str, ...],
        session: Any = None,
        cancel: threading.Event | None = None,
        generation: int | None = None,
    ) -> None:
        session = self._session if session is None else session
        cancel = self._auth_cancel if cancel is None else cancel
        generation = self._auth_generation if generation is None else generation
        if session is None:
            return
        try:
            if not self._auth_attempt_current(session, cancel, generation):
                return
            authorization = session.begin_device_authorization(scopes)
            if not self._auth_attempt_current(session, cancel, generation):
                return
            self.deviceCodeReady.emit(
                authorization.user_code,
                authorization.verification_uri,
                authorization.expires_in,
            )
            deadline = time.monotonic() + max(1, authorization.expires_in)
            while not cancel.wait(max(1, authorization.interval)):
                if not self._auth_attempt_current(session, cancel, generation):
                    return
                if time.monotonic() >= deadline:
                    raise TimeoutError("Twitch authorization code expired.")
                try:
                    session.complete_device_authorization(authorization, scopes)
                    if not self._auth_attempt_current(session, cancel, generation):
                        return
                    _token, identity = session.validated_context()
                    with self._auth_lock:
                        if not self._auth_attempt_current(session, cancel, generation):
                            return
                        self._connected_login = identity.login
                        self.connected.emit(identity.login)
                        self.statusChanged.emit("connected")
                        if self._enabled and self._watcher is not None:
                            self._watcher.start()
                    return
                except TwitchAuthorizationPending:
                    continue
        except Exception as exc:
            if self._auth_attempt_current(session, cancel, generation):
                self.error.emit(str(exc))
                self.statusChanged.emit("error")
        finally:
            with self._auth_lock:
                if self._auth_thread is threading.current_thread():
                    self._auth_thread = None

    def _run_poll_once(self) -> None:
        watcher = self._watcher
        if watcher is None:
            return
        try:
            watcher.poll_once()
            self.statusChanged.emit("watching" if self._enabled else "connected")
        except Exception as exc:
            self.error.emit(str(exc))
            self.statusChanged.emit("error")
        finally:
            with self._poll_lock:
                if self._poll_thread is threading.current_thread():
                    self._poll_thread = None

    def _on_vod_discovered(self, job: Any, video: Any) -> None:
        self.vodDiscovered.emit(job, video)

    def _on_watcher_error(self, error: Exception) -> None:
        self.error.emit(str(error))
        self.statusChanged.emit("error")

    def _on_download_started(self, job_id: str) -> None:
        self.downloadStarted.emit(job_id)
        self.statusChanged.emit("downloading")

    def _on_download_progress(self, job_id: str, progress: int) -> None:
        self.downloadProgress.emit(job_id, progress)

    def _on_download_finished(self, job: Any) -> None:
        self.downloadFinished.emit(job)
        try:
            self.statusChanged.emit("analysis_queued")
            self._analysis_queue.enqueue(str(job.id))
        except Exception as exc:
            failed = job
            try:
                if self.manager.get(str(job.id)).state == PipelineState.ANALYZING:
                    failed = self.manager.fail(str(job.id), "analysis_queue_failed", str(exc))
            except Exception:
                pass
            self.analysisFailed.emit(failed, str(exc))
            self.statusChanged.emit("analysis_failed")

    def _on_download_failed(self, job: Any, message: str) -> None:
        self.downloadFailed.emit(job, message)
        self.statusChanged.emit("download_failed")

    def _on_analysis_started(self, job_id: str) -> None:
        self.analysisStarted.emit(job_id)
        self.statusChanged.emit("analyzing")

    def _on_analysis_progress(self, job_id: str, progress: int) -> None:
        self.analysisProgress.emit(job_id, progress)

    def _on_analysis_finished(self, job: Any) -> None:
        self.analysisFinished.emit(job)
        try:
            self.statusChanged.emit("export_queued")
            self._export_queue.enqueue(str(job.id))
        except Exception as exc:
            failed = job
            try:
                if self.manager.get(str(job.id)).state == PipelineState.READY_EXPORT:
                    failed = self.manager.fail(str(job.id), "export_queue_failed", str(exc))
            except Exception:
                pass
            self.exportFailed.emit(failed, str(exc))
            self.statusChanged.emit("export_failed")

    def _on_analysis_failed(self, job: Any, message: str) -> None:
        self.analysisFailed.emit(job, message)
        self.statusChanged.emit("analysis_failed")

    def _on_export_started(self, job_id: str) -> None:
        self.exportStarted.emit(job_id)
        self.statusChanged.emit("exporting")

    def _on_export_progress(self, job_id: str, progress: int) -> None:
        self.exportProgress.emit(job_id, progress)

    def _on_export_detail(self, job_id: str, message: str) -> None:
        self.exportDetail.emit(job_id, message)

    def _on_export_finished(self, job: Any) -> None:
        delivery = job.metadata.get("delivery", {})
        if delivery.get("youtube") is False and job.state == PipelineState.READY_UPLOAD:
            job = self.manager.complete_without_upload(job.id)
        self.exportFinished.emit(job)
        self.statusChanged.emit("done" if job.state == PipelineState.DONE else "ready_upload")
        if delivery.get("youtube") is True:
            try:
                self.queue_upload(job.id)
            except Exception as exc:
                current = self.manager.get(job.id)
                if current.state == PipelineState.READY_UPLOAD:
                    current = self.manager.fail(job.id, "upload_queue_failed", str(exc))
                self.uploadFailed.emit(current, str(exc))

    def _on_export_failed(self, job: Any, message: str) -> None:
        self.exportFailed.emit(job, message)
        self.statusChanged.emit("export_failed")
