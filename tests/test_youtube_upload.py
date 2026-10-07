from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from automation.manager import PipelineManager
from automation.models import PipelineJob, PipelineState
from automation.store import PipelineStore
from automation.uploader import PipelineUploadQueue, PipelineUploadService
from integrations.youtube.auth import EncryptedJsonStore, YouTubeSession, check_cancelled
from integrations.youtube.uploader import (
    UploadCancellation, UploadResult, YouTubeUploadError, YouTubeUploader, _file_signature,
    _default_transport,
)
from tests.test_youtube_auth import FakeCredentials, ReverseCipher


URI = "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=synthetic-session"


class FakeResponse(dict):
    def __init__(self, status: int, **headers: str) -> None:
        super().__init__(headers)
        self.status = status


class FakeHttpError(Exception):
    def __init__(self, status: int, reason: str = "permanent") -> None:
        super().__init__("sensitive-uri-should-never-be-logged")
        self.resp = FakeResponse(status)
        self.content = json.dumps({"error": {"errors": [{"reason": reason}]}}).encode()


class ScriptedHttp:
    def __init__(self, steps: list[Any], *, before=None) -> None:
        self.steps = list(steps)
        self.calls: list[tuple[str, str, Any, Any]] = []
        self.before = before
        self.closed = False

    def request(self, uri: str, method: str = "GET", body: Any = None, headers: Any = None, **kwargs):
        self.calls.append((uri, method, body, headers))
        if self.before:
            self.before(uri, method, headers)
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step

    def close(self) -> None:
        self.closed = True


class FakeMedia:
    def __init__(self, path: str, **kwargs) -> None:
        self.path = Path(path)
        self.chunk = kwargs.get("chunksize", 262144)
        self.stream_file = self.path.open("rb")

    def size(self) -> int:
        return self.path.stat().st_size

    def stream(self):
        return self.stream_file


class FakeRequest:
    """Model the SDK's POST+first PUT and query+next PUT in one next_chunk call."""
    def __init__(self, http: Any, media: FakeMedia, uri: str = URI) -> None:
        self.http = http
        self.resumable = media
        self.resumable_uri: str | None = None
        self.resumable_progress = 0
        self._in_error_state = False
        self.uri = uri

    def _process(self, response: FakeResponse, content: bytes):
        if response.status in {200, 201}:
            return None, json.loads(content)
        if response.status == 308:
            value = response.get("range", "")
            self.resumable_progress = int(value.split("-")[-1]) + 1 if value else 0
            self._in_error_state = False
            class Status:
                def __init__(self, progress):
                    self.value = progress
                def progress(self):
                    return self.value
            return Status(self.resumable_progress / self.resumable.size()), None
        self._in_error_state = True
        raise FakeHttpError(response.status)

    def next_chunk(self, http=None, num_retries=0):
        assert num_retries == 0
        transport = http or self.http
        if self.resumable_uri is None:
            response, content = transport.request(self.uri, method="POST", body=b"metadata", headers={"X-Upload-Content-Length": str(self.resumable.size())})
            if response.status != 200:
                raise FakeHttpError(response.status)
            self.resumable_uri = response["location"]
        elif self._in_error_state:
            response, content = transport.request(self.resumable_uri, method="PUT", headers={"Content-Range": f"bytes */{self.resumable.size()}", "content-length": "0"})
            status, completed = self._process(response, content)
            if completed:
                return status, completed
        end = min(self.resumable_progress + self.resumable.chunk, self.resumable.size()) - 1
        try:
            response, content = transport.request(self.resumable_uri, method="PUT", body=b"chunk", headers={"Content-Range": f"bytes {self.resumable_progress}-{end}/{self.resumable.size()}"})
        except Exception:
            self._in_error_state = True
            raise
        return self._process(response, content)


class FakeService:
    def __init__(self, http: Any) -> None:
        self.http = http
        self.insertions: list[dict] = []
        self.thumbnails_set: list[str] = []
        self.thumbnail_error: Exception | None = None

    def videos(self):
        return self

    def insert(self, **kwargs):
        self.insertions.append(kwargs)
        assert kwargs["body"]["status"]["privacyStatus"] == "private"
        assert kwargs["notifySubscribers"] is False
        return FakeRequest(self.http, kwargs["media_body"])

    def thumbnails(self):
        return self

    def set(self, **kwargs):
        self.thumbnails_set.append(kwargs["videoId"])
        return self

    def execute(self, num_retries=0):
        assert num_retries == 0
        if self.thumbnail_error:
            raise self.thumbnail_error
        return {"items": []}


class FakeSession:
    def __init__(self, refresh_token: str = "synthetic-refresh") -> None:
        self.credentials = FakeCredentials(refresh_token=refresh_token)
        self.validations = 0

    def validated_credentials(self):
        self.validations += 1
        return self.credentials

    credential_binding = staticmethod(YouTubeSession.credential_binding)


def ready_job(tmp_path: Path) -> tuple[PipelineManager, PipelineJob]:
    manager = PipelineManager(PipelineStore(tmp_path / "jobs.json"))
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    output = tmp_path / "export.mp4"
    output.write_bytes(b"video" * 100000)
    project = tmp_path / "source.autocutter"
    project.write_text("{}", encoding="utf-8")
    job, _created = manager.create_configured_job(
        vod_id="test-vod", vod_url="https://twitch.tv/videos/1", start_s=0, end_s=10,
        source_title="Default <Twitch> title", local_source_path=source,
        metadata={"delivery": {"youtube": True}},
    )
    manager.mark_analyzed(job.id, project)
    manager.start_export(job.id)
    return manager, manager.mark_exported(job.id, output)


def uploader_for(tmp_path: Path, http: ScriptedHttp, *, session: FakeSession | None = None):
    session = session or FakeSession()
    store = EncryptedJsonStore(tmp_path / "upload-session.bin", cipher=ReverseCipher())
    services: list[FakeService] = []
    def service_factory(transport):
        service = FakeService(transport)
        services.append(service)
        return service
    uploader = YouTubeUploader(
        session, checkpoint_factory=lambda job_id: store, transport_factory=lambda credentials: http,
        service_factory=service_factory, media_factory=FakeMedia,
        restored_request_factory=lambda transport, uri, media: FakeRequest(transport, media, uri),
        chunk_bytes=262144, sleeper=lambda seconds: None,
    )
    return uploader, store, services


def test_private_success_persists_attempt_session_finalizing_and_id_before_done(tmp_path: Path) -> None:
    manager, job = ready_job(tmp_path)
    http = ScriptedHttp([(FakeResponse(200, location=URI), b""),
                         (FakeResponse(308, range="bytes=0-262143"), b""),
                         (FakeResponse(201), b'{"id":"synthetic-video"}')])
    uploader, store, services = uploader_for(tmp_path, http)
    observed: list[str] = []
    def before(uri, method, headers):
        persisted = manager.get(job.id)
        checkpoint = store.load()
        assert persisted.metadata["upload_attempt_started"] is True
        if method == "POST":
            assert checkpoint["phase"] == "initializing"
        else:
            assert checkpoint["uri"] == URI
            observed.append(checkpoint["phase"])
    http.before = before
    service = PipelineUploadService(manager, uploader_factory=lambda: uploader, artifact_validator=lambda job: None)
    result = service.execute(job.id)
    assert result.state == PipelineState.DONE
    assert result.youtube_video_id == "synthetic-video"
    assert result.metadata["youtube"]["privacy_status"] == "private"
    assert observed == ["uploading", "finalizing"]
    assert services[0].insertions[0]["body"]["snippet"]["title"] == "Default Twitch title"
    assert URI.encode() not in store.path.read_bytes()
    assert URI not in manager.store.path.read_text(encoding="utf-8")
    assert http.closed


def test_transient_failure_queries_authoritative_offset_then_resumes(tmp_path: Path) -> None:
    manager, job = ready_job(tmp_path)
    http = ScriptedHttp([(FakeResponse(200, location=URI), b""), TimeoutError("network"),
                         (FakeResponse(308, range="bytes=0-262143"), b""),
                         (FakeResponse(201), b'{"id":"resumed-video"}')])
    uploader, store, services = uploader_for(tmp_path, http)
    result = PipelineUploadService(manager, uploader_factory=lambda: uploader, artifact_validator=lambda job: None).execute(job.id)
    assert result.youtube_video_id == "resumed-video"
    assert len(services[0].insertions) == 1
    assert http.calls[2][3]["Content-Range"] == "bytes */500000"
    assert http.calls[3][3]["Content-Range"] == "bytes 262144-499999/500000"


def test_resume_queries_completed_session_and_never_posts_new_insert(tmp_path: Path) -> None:
    manager, job = ready_job(tmp_path)
    http = ScriptedHttp([(FakeResponse(201), b'{"id":"already-uploaded"}')])
    uploader, store, services = uploader_for(tmp_path, http)
    store.save({"version": 1, "binding": FakeSession.credential_binding(FakeCredentials()),
                "source_signature": _file_signature(Path(job.export_path)), "phase": "finalizing", "uri": URI,
                "received": 262144})
    manager.update_metadata(job.id, {"upload_attempt_started": True})
    result = PipelineUploadService(manager, uploader_factory=lambda: uploader, artifact_validator=lambda job: None).execute(job.id)
    assert result.youtube_video_id == "already-uploaded"
    assert len(http.calls) == 1 and http.calls[0][1] == "PUT"
    assert services == []


@pytest.mark.parametrize("checkpoint_kind", ["missing", "corrupt", "initializing", "other_account", "expired_final"])
def test_uncertain_attempt_never_creates_a_duplicate(tmp_path: Path, checkpoint_kind: str) -> None:
    manager, job = ready_job(tmp_path)
    http = ScriptedHttp([(FakeResponse(404), b"")])
    uploader, store, services = uploader_for(tmp_path, http)
    manager.update_metadata(job.id, {"upload_attempt_started": True})
    base = {"version": 1, "binding": FakeSession.credential_binding(FakeCredentials()),
            "source_signature": _file_signature(Path(job.export_path)), "phase": "finalizing", "uri": URI}
    if checkpoint_kind == "corrupt":
        store.path.write_bytes(b"corrupt")
    elif checkpoint_kind == "initializing":
        store.save({**base, "phase": "initializing", "uri": ""})
    elif checkpoint_kind == "other_account":
        store.save({**base, "binding": "another-account"})
    elif checkpoint_kind == "expired_final":
        store.save(base)
    service = PipelineUploadService(manager, uploader_factory=lambda: uploader, artifact_validator=lambda job: None)
    with pytest.raises(YouTubeUploadError) as error:
        service.execute(job.id)
    assert error.value.code == "upload_outcome_unknown"
    assert manager.get(job.id).state == PipelineState.FAILED
    assert services == []
    assert all(call[1] != "POST" for call in http.calls)


def test_permanent_failure_has_no_automatic_retry_and_preserves_checkpoint(tmp_path: Path) -> None:
    manager, job = ready_job(tmp_path)
    http = ScriptedHttp([(FakeResponse(200, location=URI), b""), (FakeResponse(400), b"invalid metadata")])
    uploader, store, services = uploader_for(tmp_path, http)
    with pytest.raises(YouTubeUploadError) as error:
        PipelineUploadService(manager, uploader_factory=lambda: uploader, artifact_validator=lambda job: None).execute(job.id)
    assert error.value.code == "upload_failed"
    assert not error.value.retryable
    assert len(http.calls) == 2
    assert store.load()["uri"] == URI
    assert "sensitive-uri" not in manager.get(job.id).error_message


def test_resume_known_video_id_never_calls_insert_auth_or_http(tmp_path: Path) -> None:
    manager, job = ready_job(tmp_path)
    manager.start_upload(job.id) if job.metadata.get("youtube") else None
    manager.set_upload_metadata(job.id, title="Known video")
    manager.start_upload(job.id)
    manager.record_youtube_video_id(job.id, "existing-video")
    manager.fail(job.id, "interrupted", "Crash after id persistence")
    http = ScriptedHttp([])
    session = FakeSession()
    uploader, store, services = uploader_for(tmp_path, http, session=session)
    result = PipelineUploadService(manager, uploader_factory=lambda: uploader).execute(job.id)
    assert result.state == PipelineState.DONE and result.youtube_video_id == "existing-video"
    assert session.validations == 0 and services == [] and http.calls == []


def test_invalid_export_stops_before_uploader_is_instantiated(tmp_path: Path) -> None:
    manager, job = ready_job(tmp_path)
    created: list[str] = []
    with pytest.raises(YouTubeUploadError):
        PipelineUploadService(manager, uploader_factory=lambda: created.append("uploader")).execute(job.id)
    assert created == []
    assert manager.get(job.id).state == PipelineState.FAILED


def test_thumbnail_failure_persists_id_and_resume_retries_only_thumbnail(tmp_path: Path) -> None:
    manager, job = ready_job(tmp_path)
    thumbnail = tmp_path / "thumbnail.png"
    thumbnail.write_bytes(b"synthetic-image")
    manager.update_metadata(job.id, {"delivery": {"thumbnail_path": str(thumbnail)}})
    http = ScriptedHttp([(FakeResponse(200, location=URI), b""), (FakeResponse(308, range="bytes=0-262143"), b""),
                         (FakeResponse(201), b'{"id":"thumbnail-video"}')])
    uploader, store, services = uploader_for(tmp_path, http)
    original_factory = uploader._service_factory
    def failing_factory(transport):
        service = original_factory(transport)
        service.thumbnail_error = FakeHttpError(403)
        return service
    uploader._service_factory = failing_factory
    service = PipelineUploadService(manager, uploader_factory=lambda: uploader, artifact_validator=lambda job: None)
    with pytest.raises(YouTubeUploadError) as error:
        service.execute(job.id)
    assert error.value.code == "thumbnail_failed"
    assert manager.get(job.id).youtube_video_id == "thumbnail-video"
    http.steps.clear()
    uploader._service_factory = original_factory
    completed = service.execute(job.id)
    assert completed.state == PipelineState.DONE
    assert sum(len(item.insertions) for item in services) == 1
    assert services[-1].thumbnails_set == ["thumbnail-video"]


def test_upload_queue_cancel_is_persisted_and_explicit_retry_is_possible(tmp_path: Path) -> None:
    manager, job = ready_job(tmp_path)
    started = threading.Event()
    finished = threading.Event()
    class BlockingUploader:
        def upload(self, job, *, cancellation, **kwargs):
            started.set()
            while not cancellation.is_cancelled:
                finished.wait(0.005)
            check_cancelled(cancellation)
    queue = PipelineUploadQueue(manager, uploader_factory=BlockingUploader, artifact_validator=lambda job: None,
                                on_failed=lambda job, message: finished.set())
    try:
        assert queue.enqueue(job.id)
        assert started.wait(3)
        assert queue.cancel_current()
        assert finished.wait(3)
    finally:
        assert queue.shutdown()
    assert manager.get(job.id).state == PipelineState.CANCELLED
    assert manager.retry(job.id).state == PipelineState.READY_UPLOAD


def test_upload_queue_shutdown_is_automatically_recoverable(tmp_path: Path) -> None:
    manager, job = ready_job(tmp_path)
    started = threading.Event()
    class BlockingUploader:
        def upload(self, job, *, cancellation, **kwargs):
            started.set()
            while not cancellation.is_cancelled:
                threading.Event().wait(0.005)
            check_cancelled(cancellation)
    queue = PipelineUploadQueue(manager, uploader_factory=BlockingUploader, artifact_validator=lambda job: None)
    assert queue.enqueue(job.id)
    assert started.wait(3)
    assert queue.shutdown()
    interrupted = manager.get(job.id)
    assert interrupted.state == PipelineState.FAILED and interrupted.error_code == "interrupted"
    queued: list[str] = []
    manager.reconstruct_queues(None, None, None, queued.append, upload_enabled=True)
    assert queued == [job.id]


def test_cancellation_closes_socket_and_http_transport() -> None:
    class FakeSocket:
        def __init__(self):
            self.closed = False
            self.shutdown_called = False
        def shutdown(self, how):
            self.shutdown_called = True
        def close(self):
            self.closed = True
    class Connection:
        sock = FakeSocket()
    http = ScriptedHttp([])
    http.connections = {"https": Connection()}
    token = UploadCancellation()
    token.attach(http)
    token.cancel()
    assert token.is_cancelled and http.closed
    assert Connection.sock.closed and Connection.sock.shutdown_called


def test_official_sdk_boundary_with_fake_transport_when_dependency_is_installed(tmp_path: Path) -> None:
    sdk = pytest.importorskip("googleapiclient.http")
    manager, job = ready_job(tmp_path)
    http = ScriptedHttp([(FakeResponse(200, location=URI), b""), (FakeResponse(308, range="bytes=0-262143"), b""),
                         (FakeResponse(201), b'{"id":"official-sdk-video"}')])
    uploader, store, services = uploader_for(tmp_path, http)
    uploader._media_factory = sdk.MediaFileUpload
    class SDKService:
        def videos(self):
            return self
        def insert(self, **kwargs):
            return sdk.HttpRequest(http, lambda response, content: json.loads(content), uri=URI, method="POST",
                                   body=b"metadata", resumable=kwargs["media_body"])
    uploader._service_factory = lambda adapter: SDKService()
    result = PipelineUploadService(manager, uploader_factory=lambda: uploader, artifact_validator=lambda job: None).execute(job.id)
    assert result.youtube_video_id == "official-sdk-video"


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_retryable_server_response_uses_same_resumable_session(tmp_path: Path, status: int) -> None:
    manager, job = ready_job(tmp_path)
    http = ScriptedHttp([(FakeResponse(200, location=URI), b""), (FakeResponse(status), b"temporary"),
                         (FakeResponse(308, range="bytes=0-262143"), b""),
                         (FakeResponse(201), b'{"id":"retried-video"}')])
    uploader, store, services = uploader_for(tmp_path, http)
    result = PipelineUploadService(manager, uploader_factory=lambda: uploader, artifact_validator=lambda job: None).execute(job.id)
    assert result.youtube_video_id == "retried-video"
    assert len(services[0].insertions) == 1
    assert sum(call[1] == "POST" for call in http.calls) == 1


def test_initialization_response_lost_never_blindly_retries_insert(tmp_path: Path) -> None:
    manager, job = ready_job(tmp_path)
    http = ScriptedHttp([TimeoutError("init response lost")])
    uploader, store, services = uploader_for(tmp_path, http)
    with pytest.raises(YouTubeUploadError) as error:
        PipelineUploadService(manager, uploader_factory=lambda: uploader, artifact_validator=lambda job: None).execute(job.id)
    assert error.value.code == "upload_outcome_unknown"
    assert len(http.calls) == 1
    assert store.load()["phase"] == "initializing"
    assert manager.get(job.id).metadata["upload_attempt_started"] is True


def test_expired_confirmed_incomplete_session_can_restart_safely(tmp_path: Path) -> None:
    manager, job = ready_job(tmp_path)
    http = ScriptedHttp([(FakeResponse(404), b"")])
    uploader, store, services = uploader_for(tmp_path, http)
    store.save({"version": 1, "binding": FakeSession.credential_binding(FakeCredentials()),
                "source_signature": _file_signature(Path(job.export_path)), "phase": "uploading", "uri": URI,
                "received": 262144})
    manager.update_metadata(job.id, {"upload_attempt_started": True})
    service = PipelineUploadService(manager, uploader_factory=lambda: uploader, artifact_validator=lambda job: None)
    with pytest.raises(YouTubeUploadError) as error:
        service.execute(job.id)
    assert error.value.code == "upload_session_expired"
    assert store.load()["phase"] == "expired_incomplete"
    http.steps = [(FakeResponse(200, location=URI), b""), (FakeResponse(308, range="bytes=0-262143"), b""),
                  (FakeResponse(201), b'{"id":"fresh-video"}')]
    completed = service.execute(job.id)
    assert completed.youtube_video_id == "fresh-video"
    assert sum(call[1] == "POST" for call in http.calls) == 1


def test_late_success_after_cancel_keeps_video_id_without_marking_done(tmp_path: Path) -> None:
    manager, job = ready_job(tmp_path)
    http = ScriptedHttp([(FakeResponse(200, location=URI), b""), (FakeResponse(308, range="bytes=0-262143"), b""),
                         (FakeResponse(201), b'{"id":"late-video"}')])
    uploader, store, services = uploader_for(tmp_path, http)
    token = UploadCancellation()
    def cancel_on_last_chunk(uri, method, headers):
        if method == "PUT" and headers.get("Content-Range") == "bytes 262144-499999/500000":
            token.cancel()
            manager.cancel(job.id)
    http.before = cancel_on_last_chunk
    with pytest.raises(Exception) as error:
        PipelineUploadService(manager, uploader_factory=lambda: uploader, artifact_validator=lambda job: None).execute(job.id, cancellation=token)
    assert getattr(error.value, "code", "") == "upload_cancelled"
    cancelled = manager.get(job.id)
    assert cancelled.state == PipelineState.CANCELLED
    assert cancelled.youtube_video_id == "late-video"
    assert store.load()["video_id"] == "late-video"


def test_authorized_http_never_treats_resumable_308_as_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    class Http:
        def __init__(self, *, timeout):
            self.timeout = timeout
            self.redirect_codes = {301, 302, 303, 307, 308}
    modules = {"httplib2": SimpleNamespace(Http=Http, debuglevel=4),
               "google_auth_httplib2": SimpleNamespace(AuthorizedHttp=lambda credentials, http: http)}
    monkeypatch.setattr("integrations.youtube.uploader.importlib.import_module", lambda name: modules[name])
    http = _default_transport(FakeCredentials())
    assert http.timeout == 30
    assert http.redirect_codes == {301, 302, 303, 307}
    assert modules["httplib2"].debuglevel == 0


def test_gui_upload_token_supports_shared_retry_after_chunk_retries_exhausted(tmp_path: Path) -> None:
    manager, job = ready_job(tmp_path)
    token = UploadCancellation()
    attempts: list[int] = []
    class ExhaustedChunkUploader:
        def upload(self, current_job, **kwargs):
            attempts.append(1)
            if len(attempts) < 3:
                raise YouTubeUploadError("upload_failed", "Temporary chunk retries exhausted.", retryable=True)
            kwargs["on_video_created"]("eventual-video")
            return UploadResult("eventual-video")
    service = PipelineUploadService(manager, uploader_factory=ExhaustedChunkUploader,
                                    artifact_validator=lambda current_job: None)
    completed = service.execute(job.id, cancellation=token)
    assert len(attempts) == 3
    assert completed.state == PipelineState.DONE
    assert completed.youtube_video_id == "eventual-video"


def test_gui_upload_token_can_cancel_during_shared_retry_backoff(tmp_path: Path) -> None:
    manager, job = ready_job(tmp_path)
    token = UploadCancellation()
    failed = threading.Event()
    errors: list[Exception] = []
    class ExhaustedChunkUploader:
        def upload(self, current_job, **kwargs):
            failed.set()
            raise YouTubeUploadError("upload_failed", "Temporary chunk retries exhausted.", retryable=True)
    service = PipelineUploadService(manager, uploader_factory=ExhaustedChunkUploader,
                                    artifact_validator=lambda current_job: None)
    def execute() -> None:
        try:
            service.execute(job.id, cancellation=token)
        except Exception as exc:
            errors.append(exc)
    worker = threading.Thread(target=execute)
    worker.start()
    assert failed.wait(3)
    token.cancel()
    worker.join(3)
    assert not worker.is_alive()
    assert getattr(errors[0], "code", "") == "upload_cancelled"


def test_unexpected_308_acknowledging_all_bytes_cannot_restart_after_expiry(tmp_path: Path) -> None:
    manager, job = ready_job(tmp_path)
    http = ScriptedHttp([(FakeResponse(200, location=URI), b""),
                         (FakeResponse(308, range="bytes=0-499999"), b"")])
    uploader, store, services = uploader_for(tmp_path, http)
    service = PipelineUploadService(manager, uploader_factory=lambda: uploader, artifact_validator=lambda current_job: None)
    def stop_after_checkpoint(progress: int) -> None:
        raise RuntimeError("Simulate process interruption after the full-byte 308 checkpoint")
    with pytest.raises(YouTubeUploadError):
        service.execute(job.id, on_progress=stop_after_checkpoint)
    checkpoint = store.load()
    assert checkpoint["received"] == checkpoint["total_bytes"] == 500000
    assert checkpoint["phase"] == "finalizing"
    http.steps = [(FakeResponse(404), b"")]
    with pytest.raises(YouTubeUploadError) as error:
        service.execute(job.id)
    assert error.value.code == "upload_outcome_unknown"
    assert sum(call[1] == "POST" for call in http.calls) == 1
    assert sum(len(item.insertions) for item in services) == 1
