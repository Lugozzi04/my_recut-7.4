from __future__ import annotations

import hashlib
import importlib
import json
import os
import secrets
import socket
import threading
import time
import uuid
import webbrowser
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from automation.locking import ProcessFileLock
from automation.twitch_auth import SecretCipher, WindowsDpapiCipher
from utils.runtime_paths import credentials_root


YOUTUBE_SCOPES = ("https://www.googleapis.com/auth/youtube.upload",)


class YouTubeAuthError(RuntimeError):
    exit_code = 50

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class YouTubeCredentialStoreError(YouTubeAuthError):
    exit_code = 60


class YouTubeCancelled(YouTubeAuthError):
    exit_code = 70

    def __init__(self) -> None:
        super().__init__("upload_cancelled", "YouTube operation was cancelled.")


def check_cancelled(cancellation: Any = None) -> None:
    if cancellation is not None and bool(getattr(cancellation, "is_cancelled", getattr(cancellation, "cancelled", False))):
        raise YouTubeCancelled()


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class EncryptedJsonStore:
    """DPAPI encrypted, atomic, process-safe storage for credentials and sessions."""

    def __init__(self, path: Path | str, *, cipher: SecretCipher | None = None) -> None:
        self.path = Path(path).expanduser().resolve()
        self.cipher = cipher or WindowsDpapiCipher()
        self._file_lock = ProcessFileLock(self.path.with_name(self.path.name + ".lock"))

    def load(self) -> dict[str, Any] | None:
        with self._file_lock.hold():
            return self._load()

    def _load(self) -> dict[str, Any] | None:
        try:
            if not self.path.exists():
                return None
            payload = json.loads(self.cipher.unprotect(self.path.read_bytes()).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Not an object")
            return payload
        except Exception as exc:
            raise YouTubeCredentialStoreError("youtube_store_invalid", "Cannot read the encrypted YouTube state.") from exc

    def save(self, payload: Mapping[str, Any]) -> None:
        with self._file_lock.hold():
            self._save(payload)

    def _save(self, payload: Mapping[str, Any]) -> None:
        try:
            plaintext = json.dumps(dict(payload), ensure_ascii=True, sort_keys=True, allow_nan=False).encode("utf-8")
            encrypted = self.cipher.protect(plaintext)
            if not encrypted:
                raise ValueError("Empty encryption result")
            _atomic_bytes(self.path, encrypted)
        except Exception as exc:
            raise YouTubeCredentialStoreError("youtube_store_write", "Cannot persist the encrypted YouTube state.") from exc

    def delete(self) -> None:
        with self._file_lock.hold():
            self.path.unlink(missing_ok=True)


class YouTubeTokenStore(EncryptedJsonStore):
    """A durable logout epoch rejects completions from another GUI/CLI process."""

    def __init__(self, path: Path | str | None = None, *, cipher: SecretCipher | None = None) -> None:
        super().__init__(path or credentials_root() / "youtube-token.bin", cipher=cipher)
        self._epoch_path = self.path.with_name(self.path.name + ".epoch")

    def _epoch(self) -> str:
        if not self._epoch_path.exists():
            _atomic_bytes(self._epoch_path, uuid.uuid4().hex.encode("ascii"))
        try:
            epoch = self._epoch_path.read_text(encoding="ascii").strip()
            if len(epoch) != 32:
                raise ValueError("Invalid epoch")
            int(epoch, 16)
            return epoch
        except (OSError, UnicodeError, ValueError) as exc:
            raise YouTubeCredentialStoreError("youtube_epoch_invalid", "Cannot validate the YouTube authorization session.") from exc

    def epoch(self) -> str:
        with self._file_lock.hold():
            return self._epoch()

    def begin_authorization(self) -> str:
        """Invalidate older consent requests, retaining the current account meanwhile."""
        with self._file_lock.hold():
            previous = self._load()
            current_epoch = self._epoch()
            epoch = uuid.uuid4().hex
            _atomic_bytes(self._epoch_path, epoch.encode("ascii"))
            if previous is not None and previous.get("epoch") == current_epoch:
                previous["epoch"] = epoch
                self._save(previous)
            return epoch

    def load(self) -> dict[str, Any] | None:
        with self._file_lock.hold():
            raw = self._load()
            if raw is None or raw.get("epoch") != self._epoch():
                return None
            credentials = raw.get("credentials")
            if raw.get("version") != 1 or not isinstance(credentials, dict):
                raise YouTubeCredentialStoreError("youtube_store_invalid", "Unsupported YouTube credential payload.")
            return credentials

    def save_if_current(self, credentials: Mapping[str, Any], epoch: str) -> None:
        with self._file_lock.hold():
            if epoch != self._epoch():
                raise YouTubeAuthError("youtube_auth_invalidated", "YouTube authorization was cancelled or the account changed.")
            self._save({"version": 1, "epoch": epoch, "credentials": dict(credentials)})

    def delete(self) -> None:
        with self._file_lock.hold():
            # Advance first: a crash before unlink still makes the old file unusable.
            _atomic_bytes(self._epoch_path, uuid.uuid4().hex.encode("ascii"))
            self.path.unlink(missing_ok=True)


class _LoopbackServer(HTTPServer):
    allow_reuse_address = False

    def server_bind(self) -> None:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def get_request(self) -> tuple[Any, Any]:
        connection, address = super().get_request()
        # A browser or local client that connects without sending its request
        # must not hold cancellation indefinitely in the HTTP parser.
        connection.settimeout(1.0)
        return connection, address


def _default_flow_factory(config: Mapping[str, Any]) -> Any:
    try:
        flow_module = importlib.import_module("google_auth_oauthlib.flow")
        return flow_module.InstalledAppFlow.from_client_config(
            config, scopes=YOUTUBE_SCOPES, autogenerate_code_verifier=True,
        )
    except ImportError as exc:
        raise YouTubeAuthError("youtube_dependency_missing", "Install google-auth-oauthlib to connect YouTube.") from exc


def _default_credentials_factory(payload: Mapping[str, Any]) -> Any:
    module = importlib.import_module("google.oauth2.credentials")
    return module.Credentials.from_authorized_user_info(dict(payload), scopes=YOUTUBE_SCOPES)


class YouTubeSession:
    def __init__(
        self, client_config_path: Path | str | None = None, token_store: YouTubeTokenStore | None = None,
        *, flow_factory: Callable[[Mapping[str, Any]], Any] = _default_flow_factory,
        credentials_factory: Callable[[Mapping[str, Any]], Any] = _default_credentials_factory,
        browser_open: Callable[[str], object] = webbrowser.open, timeout_s: float = 300.0,
    ) -> None:
        configured = client_config_path or os.environ.get("AUTO_CUTTER_YOUTUBE_CLIENT_CONFIG")
        self.client_config_path = Path(configured).expanduser().resolve() if configured else None
        self.token_store = token_store or YouTubeTokenStore()
        self._flow_factory = flow_factory
        self._credentials_factory = credentials_factory
        self._browser_open = browser_open
        self._timeout_s = max(0.25, float(timeout_s))
        self._lock = threading.RLock()
        self._generation = 0
        self._validation_lock = threading.Lock()

    def _client_config(self) -> dict[str, Any]:
        try:
            if self.client_config_path is not None:
                raw = json.loads(self.client_config_path.read_text(encoding="utf-8"))
            else:
                credentials = self.token_store.load()
                if credentials is None:
                    raise YouTubeAuthError("youtube_client_config_missing", "Use auth youtube --client-config PATH with a Google Desktop OAuth client JSON.")
                raw = {"installed": {
                    "client_id": credentials["client_id"], "client_secret": credentials["client_secret"],
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost"],
                }}
            installed = raw.get("installed") if isinstance(raw, dict) else None
            if not isinstance(installed, dict) or not all(installed.get(key) for key in ("client_id", "client_secret", "auth_uri", "token_uri")):
                raise ValueError("Not a Desktop client")
            for key, hosts in (("auth_uri", {"accounts.google.com"}), ("token_uri", {"oauth2.googleapis.com", "accounts.google.com"})):
                parsed = urlsplit(str(installed[key]))
                if parsed.scheme != "https" or parsed.hostname not in hosts or parsed.username:
                    raise ValueError("Unexpected OAuth endpoint")
            return {"installed": installed}
        except YouTubeAuthError:
            raise
        except Exception as exc:
            raise YouTubeAuthError("youtube_client_config_invalid", "Cannot load a valid Google Desktop OAuth client JSON.") from exc

    def _check_attempt(self, generation: int, epoch: str, cancellation: Any) -> None:
        check_cancelled(cancellation)
        with self._lock:
            if generation != self._generation:
                raise YouTubeCancelled()
        if self.token_store.epoch() != epoch:
            raise YouTubeAuthError("youtube_auth_invalidated", "YouTube authorization was cancelled or the account changed.")

    def connect(self, cancellation: Any = None, on_authorization_url: Callable[[str], None] | None = None) -> Any:
        config = self._client_config()
        with self._lock:
            self._generation += 1
            generation = self._generation
        epoch = self.token_store.begin_authorization()
        flow = self._flow_factory(config)
        result: dict[str, str] = {}
        expected_state = secrets.token_urlsafe(32)

        class CallbackHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                # Callback URLs contain authorization codes and must never enter logs.
                pass

            def do_GET(self) -> None:
                parsed = urlsplit(self.path)
                query = parse_qs(parsed.query)
                received_state = query.get("state", [""])[0]
                valid = parsed.path == "/" and secrets.compare_digest(received_state, expected_state)
                code = query.get("code", [""])[0]
                error = query.get("error", [""])[0]
                if valid and (code or error):
                    result.update({"code": code, "error": error})
                self.send_response(200 if valid else 400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(b"Return to Auto Cutter to finish authorization." if valid else b"Invalid OAuth callback.")

        try:
            with _LoopbackServer(("127.0.0.1", 0), CallbackHandler) as server:
                server.timeout = 0.25
                flow.redirect_uri = f"http://127.0.0.1:{server.server_port}/"
                url, _state = flow.authorization_url(state=expected_state, access_type="offline", prompt="consent")
                self._check_attempt(generation, epoch, cancellation)
                if on_authorization_url is not None:
                    on_authorization_url(url)
                self._browser_open(url)
                deadline = time.monotonic() + self._timeout_s
                while not result:
                    self._check_attempt(generation, epoch, cancellation)
                    if time.monotonic() >= deadline:
                        raise YouTubeAuthError("youtube_auth_timeout", "Timed out waiting for Google OAuth consent.")
                    server.handle_request()
                if result.get("error"):
                    raise YouTubeAuthError("youtube_auth_denied", "Google OAuth authorization was denied.")
                self._check_attempt(generation, epoch, cancellation)
                flow.fetch_token(code=result["code"], timeout=30)
                credentials = flow.credentials
                if not credentials.refresh_token:
                    raise YouTubeAuthError("youtube_refresh_missing", "Google did not return a refresh token; grant offline access again.")
                with self._lock:
                    self._check_attempt(generation, epoch, cancellation)
                    self.token_store.save_if_current(json.loads(credentials.to_json()), epoch)
                return credentials
        except YouTubeAuthError:
            raise
        except Exception as exc:
            raise YouTubeAuthError("youtube_auth_failed", "Google OAuth could not be completed; check the client configuration and network.") from exc

    def validated_credentials(self) -> Any:
        with self._lock:
            generation = self._generation
        epoch = self.token_store.epoch()
        # Other processes can log out while refresh is blocked on the network.
        refresh_lock = ProcessFileLock(self.token_store.path.with_name(self.token_store.path.name + ".refresh.lock"))
        with self._validation_lock, refresh_lock.hold():
            self._check_attempt(generation, epoch, None)
            payload = self.token_store.load()
            if payload is None:
                raise YouTubeAuthError("youtube_auth_required", "Run auth youtube before requesting an automatic upload.")
            try:
                credentials = self._credentials_factory(payload)
                if not credentials.refresh_token:
                    raise YouTubeAuthError("youtube_auth_required", "YouTube offline credentials are unavailable; authenticate again.")
                if not credentials.valid:
                    request_module = importlib.import_module("google.auth.transport.requests")
                    request = request_module.Request()
                    try:
                        credentials.refresh(lambda *args, **kwargs: request(*args, **{**kwargs, "timeout": 30}))
                    finally:
                        request.session.close()
                    with self._lock:
                        self._check_attempt(generation, epoch, None)
                        self.token_store.save_if_current(json.loads(credentials.to_json()), epoch)
                self._check_attempt(generation, epoch, None)
                return credentials
            except YouTubeAuthError:
                raise
            except Exception as exc:
                raise YouTubeAuthError("youtube_refresh_failed", "YouTube credentials could not be refreshed; check authorization and network.") from exc

    @staticmethod
    def credential_binding(credentials: Any) -> str:
        value = f"{credentials.client_id}\0{credentials.refresh_token}".encode("utf-8")
        return hashlib.sha256(value).hexdigest()

    def cancel_authorization(self) -> None:
        with self._lock:
            self._generation += 1

    def disconnect(self) -> None:
        with self._lock:
            self._generation += 1
            self.token_store.delete()
