from __future__ import annotations

import hashlib
import importlib
import json
import mimetypes
import re
import socket
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from automation.models import PipelineJob
from integrations.youtube.auth import (
    EncryptedJsonStore, YouTubeAuthError, YouTubeCancelled, YouTubeSession, check_cancelled,
)
from utils.runtime_paths import credentials_root


class YouTubeUploadError(RuntimeError):
    exit_code = 40

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class UploadCancellation:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._transport: Any = None

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        check_cancelled(self)

    def attach(self, transport: Any) -> None:
        with self._lock:
            self._transport = transport
        if self.is_cancelled:
            self._close_transport(transport)

    def detach(self, transport: Any) -> None:
        with self._lock:
            if self._transport is transport:
                self._transport = None

    def cancel(self) -> None:
        self._event.set()
        with self._lock:
            transport = self._transport
        if transport is not None:
            self._close_transport(transport)

    @staticmethod
    def _close_transport(transport: Any) -> None:
        http = getattr(transport, "http", transport)
        connections = getattr(http, "connections", {})
        for connection in list(connections.values()):
            connection_socket = getattr(connection, "sock", None)
            if connection_socket is not None:
                try:
                    connection_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    connection_socket.close()
                except OSError:
                    pass
        close = getattr(http, "close", None)
        if close is not None:
            try:
                close()
            except Exception:
                pass


@dataclass(frozen=True)
class UploadResult:
    video_id: str
    thumbnail_applied: bool = False


def _valid_session_uri(uri: str) -> bool:
    parsed = urlsplit(uri)
    return (parsed.scheme == "https" and parsed.hostname in {"www.googleapis.com", "youtube.googleapis.com"}
            and parsed.path.startswith(("/upload/youtube/", "/resumable/upload/youtube/"))
            and not parsed.username and parsed.port in {None, 443})


def _file_signature(path: Path) -> str:
    info = path.stat()
    serialized = f"{path.resolve()}\0{info.st_size}\0{info.st_mtime_ns}".encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _response_status(response: Any) -> int:
    return int(getattr(response, "status", response.get("status", 0)))


def _header(response: Any, name: str) -> str:
    return next((str(value) for key, value in response.items() if str(key).lower() == name.lower()), "")


def _video_id(content: Any) -> str:
    try:
        raw = json.loads(content)
        value = str(raw.get("id", "")) if isinstance(raw, dict) else ""
        return value if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) else ""
    except (TypeError, ValueError, UnicodeError):
        return ""


class _CheckpointHttp:
    """Wrap the official client's HTTP transport at its crash-sensitive boundary."""

    def __init__(
        self, http: Any, store: EncryptedJsonStore, checkpoint: dict[str, Any],
        on_attempt: Callable[[], None], on_created: Callable[[str], None], cancellation: Any,
    ) -> None:
        self.http = http
        self.store = store
        self.checkpoint = checkpoint
        self.on_attempt = on_attempt
        self.on_created = on_created
        self.cancellation = cancellation

    def request(self, uri: str, method: str = "GET", body: Any = None, headers: Any = None, **kwargs: Any) -> Any:
        check_cancelled(self.cancellation)
        clean_headers = {str(key).lower(): str(value) for key, value in (headers or {}).items()}
        init = method.upper() == "POST" and "x-upload-content-length" in clean_headers
        content_range = clean_headers.get("content-range", "")
        final = False
        if init:
            # The plain durable marker survives even loss of the secret checkpoint.
            self.on_attempt()
            self.checkpoint["phase"] = "initializing"
            self.checkpoint["total_bytes"] = int(clean_headers["x-upload-content-length"])
            self.store.save(self.checkpoint)
        elif method.upper() == "PUT":
            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
            final = bool(match and int(match[2]) + 1 == int(match[3]))
            if final:
                self.checkpoint["phase"] = "finalizing"
                self.store.save(self.checkpoint)
        response, content = self.http.request(uri, method=method, body=body, headers=headers, **kwargs)
        status = _response_status(response)
        if init:
            location = _header(response, "location")
            if status == 200 and _valid_session_uri(location):
                self.checkpoint.update({"uri": location, "phase": "uploading", "received": 0})
                # Persist before the SDK sends even the first chunk in this call.
                self.store.save(self.checkpoint)
            elif status >= 400:
                self.checkpoint["phase"] = "init_rejected"
                self.store.save(self.checkpoint)
            else:
                raise YouTubeUploadError("upload_outcome_unknown", "YouTube did not return a usable resumable session; resolve the upload manually before retrying.")
        elif method.upper() == "PUT":
            if status in {200, 201}:
                identifier = _video_id(content)
                if not identifier:
                    raise YouTubeUploadError("upload_outcome_unknown", "YouTube accepted the upload but returned no video id; resolve the upload manually.")
                self.on_created(identifier)
                self.checkpoint.update({"video_id": identifier, "phase": "complete"})
                self.store.save(self.checkpoint)
            elif status == 308:
                received_range = _header(response, "range")
                match = re.fullmatch(r"bytes=0-(\d+)", received_range)
                if received_range and not match:
                    raise YouTubeUploadError("upload_response_invalid", "YouTube returned an invalid resumable byte range.")
                received = int(match[1]) + 1 if match else 0
                range_total = content_range.rsplit("/", 1)[-1]
                total = int(range_total) if range_total.isdigit() else int(self.checkpoint.get("total_bytes", 0))
                # An unexpected 308 acknowledging every byte is not proof that
                # no video was created. Preserve uncertainty until a video id
                # or an authoritative incomplete range is received.
                phase = "finalizing" if total > 0 and received >= total else "uploading"
                self.checkpoint.update({"received": received, "total_bytes": total, "phase": phase})
                location = _header(response, "location")
                if location:
                    if not _valid_session_uri(location):
                        raise YouTubeUploadError("upload_response_invalid", "YouTube returned an invalid resumable location.")
                    self.checkpoint["uri"] = location
                self.store.save(self.checkpoint)
            elif status in {404, 410}:
                if self.checkpoint.get("phase") == "finalizing":
                    raise YouTubeUploadError("upload_outcome_unknown", "The resumable session expired after the last chunk may have completed; resolve the video id manually.")
                self.checkpoint.update({"phase": "expired_incomplete", "uri": ""})
                self.store.save(self.checkpoint)
                raise YouTubeUploadError("upload_session_expired", "The incomplete YouTube session expired. Resume the job to start a fresh session safely.")
        return response, content


def _default_transport(credentials: Any) -> Any:
    try:
        auth_http = importlib.import_module("google_auth_httplib2")
        httplib2 = importlib.import_module("httplib2")
        setattr(httplib2, "debuglevel", 0)
        http = httplib2.Http(timeout=30)
        # The resumable protocol's 308 is progress, not an HTTP redirect.
        # Match googleapiclient.http.build_http while retaining our timeout.
        if hasattr(http, "redirect_codes"):
            http.redirect_codes = http.redirect_codes - {308}
        return auth_http.AuthorizedHttp(credentials, http=http)
    except ImportError as exc:
        raise YouTubeAuthError("youtube_dependency_missing", "Install the official Google API client runtime dependencies.") from exc


def _default_service(http: Any) -> Any:
    module = importlib.import_module("googleapiclient.discovery")
    return module.build("youtube", "v3", http=http, cache_discovery=False, static_discovery=True)


def _default_media(path: str, **kwargs: Any) -> Any:
    return importlib.import_module("googleapiclient.http").MediaFileUpload(path, **kwargs)


def _restored_request(http: Any, uri: str, media: Any) -> Any:
    module = importlib.import_module("googleapiclient.http")
    return module.HttpRequest(http, lambda response, content: json.loads(content), uri=uri, resumable=media)


def _retryable_exception(exc: Exception) -> bool:
    response = getattr(exc, "resp", None)
    if response is not None:
        status = _response_status(response)
        if status in {429, 500, 502, 503, 504}:
            return True
        if status == 403:
            try:
                details = json.loads(getattr(exc, "content", b"{}"))
                errors = details.get("error", {}).get("errors", [])
                return any(item.get("reason") in {"rateLimitExceeded", "userRateLimitExceeded"} for item in errors)
            except (ValueError, TypeError):
                return False
        return False
    return isinstance(exc, (OSError, TimeoutError, ConnectionError)) or type(exc).__name__ in {
        "TransportError", "ServerNotFoundError", "HttpLib2Error", "RemoteDisconnected", "IncompleteRead",
    }


class YouTubeUploader:
    def __init__(
        self, session: YouTubeSession | None = None, *,
        checkpoint_factory: Callable[[str], EncryptedJsonStore] | None = None,
        transport_factory: Callable[[Any], Any] = _default_transport,
        service_factory: Callable[[Any], Any] = _default_service,
        media_factory: Callable[..., Any] = _default_media,
        restored_request_factory: Callable[[Any, str, Any], Any] = _restored_request,
        chunk_bytes: int = 8 * 1024 * 1024, max_retries: int = 5,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.session = session or YouTubeSession()
        self._checkpoint_factory = checkpoint_factory or self._default_checkpoint
        self._transport_factory = transport_factory
        self._service_factory = service_factory
        self._media_factory = media_factory
        self._restored_request_factory = restored_request_factory
        self._chunk_bytes = max(256 * 1024, (int(chunk_bytes) // (256 * 1024)) * (256 * 1024))
        self._max_retries = max(0, int(max_retries))
        self._sleep = sleeper

    @staticmethod
    def _default_checkpoint(job_id: str) -> EncryptedJsonStore:
        safe_id = hashlib.sha256(job_id.encode("utf-8")).hexdigest()
        return EncryptedJsonStore(credentials_root() / "youtube-uploads" / f"{safe_id}.bin")

    def upload(
        self, job: PipelineJob, *, cancellation: Any = None,
        on_progress: Callable[[int], None] | None = None,
        on_video_created: Callable[[str], None], on_attempt_started: Callable[[], None],
    ) -> UploadResult:
        check_cancelled(cancellation)
        metadata = job.metadata.get("youtube", {})
        thumbnail = metadata.get("thumbnail_path") if isinstance(metadata, Mapping) else None
        if job.youtube_video_id and not thumbnail:
            return UploadResult(job.youtube_video_id)
        credentials = self.session.validated_credentials()
        http = self._transport_factory(credentials)
        internal_cancellation = UploadCancellation()
        internal_cancellation.attach(http)
        parent_attach = getattr(cancellation, "attach", None)
        parent_detach = getattr(cancellation, "detach", None)
        if parent_attach is not None and not isinstance(cancellation, UploadCancellation):
            parent_attach(internal_cancellation)
        elif isinstance(cancellation, UploadCancellation):
            cancellation.attach(http)
        media: Any = None
        try:
            if job.youtube_video_id:
                identifier = job.youtube_video_id
            else:
                path = Path(job.export_path or "").resolve()
                if not path.is_file() or path.stat().st_size <= 0:
                    raise YouTubeUploadError("upload_source_missing", "The validated export is unavailable for YouTube upload.")
                store = self._checkpoint_factory(job.id)
                try:
                    checkpoint = store.load()
                except YouTubeAuthError as exc:
                    if job.metadata.get("upload_attempt_started"):
                        raise YouTubeUploadError("upload_outcome_unknown", "The upload checkpoint is unreadable; resolve the prior YouTube attempt before retrying.") from exc
                    raise
                binding = self.session.credential_binding(credentials)
                signature = _file_signature(path)
                if checkpoint is not None:
                    if checkpoint.get("binding") != binding or checkpoint.get("source_signature") != signature:
                        raise YouTubeUploadError("upload_outcome_unknown", "The upload account or export changed; resolve the prior YouTube attempt before retrying.")
                    if checkpoint.get("video_id"):
                        identifier = str(checkpoint["video_id"])
                        on_video_created(identifier)
                        return self._finish(identifier, thumbnail, http, cancellation)
                    phase = checkpoint.get("phase")
                    if not checkpoint.get("uri") and phase not in {"init_rejected", "expired_incomplete"}:
                        raise YouTubeUploadError("upload_outcome_unknown", "A previous YouTube initialization has an unknown outcome; resolve it manually.")
                elif job.metadata.get("upload_attempt_started"):
                    raise YouTubeUploadError("upload_outcome_unknown", "A previous upload was started but its checkpoint is missing; resolve it manually.")
                checkpoint = checkpoint or {"version": 1, "binding": binding, "source_signature": signature, "received": 0}
                media = self._media_factory(str(path), mimetype="application/octet-stream", chunksize=self._chunk_bytes, resumable=True)
                adapter = _CheckpointHttp(http, store, checkpoint, on_attempt_started, on_video_created, cancellation)
                uri = str(checkpoint.get("uri", ""))
                if uri:
                    if not _valid_session_uri(uri):
                        raise YouTubeUploadError("upload_outcome_unknown", "The saved resumable upload location is invalid.")
                    request = self._restored_request_factory(adapter, uri, media)
                    request.resumable_uri = uri
                    request.resumable_progress = int(checkpoint.get("received", 0))
                    request._in_error_state = True
                else:
                    service = self._service_factory(adapter)
                    title = str(metadata.get("title", job.source_title) or "Auto Cutter export").strip()
                    if not title or len(title) > 100 or any(character in title for character in "<>"):
                        raise YouTubeUploadError("upload_title_invalid", "YouTube titles must contain 1–100 characters and no angle brackets.")
                    request = service.videos().insert(
                        part="snippet,status", notifySubscribers=False, media_body=media,
                        body={"snippet": {"title": title, "description": str(metadata.get("description", "")), "categoryId": "22"},
                              "status": {"privacyStatus": "private"}},
                    )
                retries = 0
                while True:
                    check_cancelled(cancellation)
                    try:
                        status, response = request.next_chunk(http=adapter, num_retries=0)
                        if response is not None:
                            identifier = str(response.get("id", ""))
                            if not identifier:
                                raise YouTubeUploadError("upload_outcome_unknown", "YouTube completed upload without a video id.")
                            # The adapter already persists before returning; keep custom fake requests safe too.
                            on_video_created(identifier)
                            break
                        retries = 0
                        if status is not None and on_progress is not None:
                            on_progress(max(0, min(99, int(status.progress() * 100))))
                    except YouTubeUploadError:
                        raise
                    except Exception as exc:
                        check_cancelled(cancellation)
                        if checkpoint.get("phase") == "initializing" and not checkpoint.get("uri"):
                            raise YouTubeUploadError("upload_outcome_unknown", "The YouTube session initialization response was lost; resolve it manually.") from exc
                        retryable = _retryable_exception(exc)
                        if not retryable or retries >= self._max_retries:
                            response = getattr(exc, "resp", None)
                            if response is not None and _response_status(response) == 401:
                                raise YouTubeAuthError("youtube_auth_required", "YouTube rejected authorization; authenticate again.") from exc
                            raise YouTubeUploadError("upload_failed", "YouTube upload failed; resumable state was preserved.", retryable=retryable) from exc
                        retries += 1
                        request._in_error_state = bool(request.resumable_uri)
                        self._backoff(min(32.0, float(2 ** retries)), cancellation)
                check_cancelled(cancellation)
                if on_progress is not None:
                    on_progress(100)
            return self._finish(identifier, thumbnail, http, cancellation)
        finally:
            if media is not None:
                stream = getattr(media, "stream", lambda: None)()
                if stream is not None:
                    stream.close()
            internal_cancellation.detach(http)
            if parent_detach is not None and not isinstance(cancellation, UploadCancellation):
                parent_detach(internal_cancellation)
            elif isinstance(cancellation, UploadCancellation):
                cancellation.detach(http)
            UploadCancellation._close_transport(http)

    def _finish(self, identifier: str, thumbnail: Any, http: Any, cancellation: Any) -> UploadResult:
        check_cancelled(cancellation)
        if not thumbnail:
            return UploadResult(identifier)
        path = Path(str(thumbnail)).expanduser().resolve()
        if not path.is_file():
            raise YouTubeUploadError("thumbnail_missing", "The optional thumbnail file is missing; the uploaded video id was retained.")
        media = self._media_factory(str(path), mimetype=mimetypes.guess_type(path.name)[0] or "application/octet-stream", resumable=False)
        try:
            request = self._service_factory(http).thumbnails().set(videoId=identifier, media_body=media)
            for retry in range(self._max_retries + 1):
                check_cancelled(cancellation)
                try:
                    request.execute(num_retries=0)
                    return UploadResult(identifier, thumbnail_applied=True)
                except Exception as exc:
                    check_cancelled(cancellation)
                    if retry >= self._max_retries or not _retryable_exception(exc):
                        raise YouTubeUploadError("thumbnail_failed", "The private video was uploaded but its optional thumbnail could not be set; resume retries only the thumbnail.") from exc
                    self._backoff(min(32.0, float(2 ** (retry + 1))), cancellation)
            raise AssertionError("unreachable")
        finally:
            stream = getattr(media, "stream", lambda: None)()
            if stream is not None:
                stream.close()

    def _backoff(self, seconds: float, cancellation: Any) -> None:
        remaining = seconds
        while remaining > 0:
            check_cancelled(cancellation)
            pause = min(0.25, remaining)
            self._sleep(pause)
            remaining -= pause
