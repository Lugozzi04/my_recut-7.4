from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import pytest

from integrations.youtube.auth import (
    EncryptedJsonStore, YouTubeAuthError, YouTubeCancelled, YouTubeCredentialStoreError,
    YouTubeSession, YouTubeTokenStore,
)


class ReverseCipher:
    def protect(self, value: bytes) -> bytes:
        return b"ENCRYPTED:" + value[::-1]

    def unprotect(self, value: bytes) -> bytes:
        if not value.startswith(b"ENCRYPTED:"):
            raise ValueError("Invalid synthetic ciphertext")
        return value[len(b"ENCRYPTED:"):][::-1]


class FakeCredentials:
    def __init__(self, *, valid: bool = True, refresh_token: str = "synthetic-refresh") -> None:
        self.valid = valid
        self.refresh_token = refresh_token
        self.client_id = "synthetic-client"
        self.token = "synthetic-access"

    def to_json(self) -> str:
        return json.dumps({"token": self.token, "refresh_token": self.refresh_token,
                           "client_id": self.client_id, "client_secret": "synthetic-desktop-secret"})

    def refresh(self, request) -> None:
        self.valid = True


class FakeFlow:
    def __init__(self) -> None:
        self.redirect_uri = ""
        self.credentials = FakeCredentials()
        self.state = ""
        self.code = ""
        self.started = threading.Event()
        self.release: threading.Event | None = None

    def authorization_url(self, **kwargs):
        self.state = kwargs["state"]
        assert kwargs["access_type"] == "offline"
        return "https://accounts.google.com/oauth?" + urlencode({"redirect_uri": self.redirect_uri}), self.state

    def fetch_token(self, **kwargs) -> None:
        self.code = kwargs["code"]
        assert kwargs["timeout"] == 30
        self.started.set()
        if self.release is not None:
            assert self.release.wait(3)


def make_config(tmp_path: Path) -> Path:
    path = tmp_path / "desktop-client.json"
    path.write_text(json.dumps({"installed": {
        "client_id": "synthetic-client", "client_secret": "synthetic-secret",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token",
    }}), encoding="utf-8")
    return path


def consent_browser(flow: FakeFlow, requests: list[threading.Thread]):
    def open_browser(_url: str) -> None:
        def consent() -> None:
            parsed = urlsplit(flow.redirect_uri)
            connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=3)
            try:
                connection.request("GET", "/?" + urlencode({"state": "wrong-state", "code": "wrong-code"}))
                response = connection.getresponse()
                assert response.status == 400
                response.read()
                connection.request("GET", "/?" + urlencode({"state": flow.state, "code": "synthetic-code"}))
                response = connection.getresponse()
                assert response.status == 200
                response.read()
            finally:
                connection.close()
        thread = threading.Thread(target=consent)
        requests.append(thread)
        thread.start()
    return open_browser


def test_credential_store_is_encrypted_and_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = YouTubeTokenStore(tmp_path / "credentials è 🎬" / "youtube.bin", cipher=ReverseCipher())
    epoch = store.epoch()
    credentials = json.loads(FakeCredentials().to_json())
    store.save_if_current(credentials, epoch)
    previous = store.path.read_bytes()
    assert b"synthetic-refresh" not in previous
    assert store.load() == credentials
    monkeypatch.setattr("integrations.youtube.auth.os.replace", lambda *args: (_ for _ in ()).throw(OSError("locked")))
    with pytest.raises(YouTubeCredentialStoreError):
        store.save_if_current({"token": "replacement"}, epoch)
    assert store.path.read_bytes() == previous
    assert not list(store.path.parent.glob("*.tmp"))


def test_logout_invalidates_save_from_another_store_instance(tmp_path: Path) -> None:
    path = tmp_path / "youtube.bin"
    first = YouTubeTokenStore(path, cipher=ReverseCipher())
    second = YouTubeTokenStore(path, cipher=ReverseCipher())
    captured = first.epoch()
    first.save_if_current(json.loads(FakeCredentials().to_json()), captured)
    second.delete()
    with pytest.raises(YouTubeAuthError, match="account changed"):
        first.save_if_current(json.loads(FakeCredentials().to_json()), captured)
    assert first.load() is None


def test_crash_after_logout_epoch_before_unlink_cannot_reuse_old_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = YouTubeTokenStore(tmp_path / "youtube.bin", cipher=ReverseCipher())
    store.save_if_current(json.loads(FakeCredentials().to_json()), store.epoch())
    original_unlink = Path.unlink
    def locked_unlink(path: Path, *args, **kwargs) -> None:
        if path == store.path:
            raise OSError("locked")
        original_unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", locked_unlink)
    with pytest.raises(OSError):
        store.delete()
    assert store.path.exists()
    assert store.load() is None
    # A later consent attempt must not rewrap stale credentials from the file
    # left behind by the interrupted logout.
    store.begin_authorization()
    assert store.load() is None


def test_connect_uses_loopback_state_offline_flow_and_encrypted_persistence(tmp_path: Path, capsys) -> None:
    flow = FakeFlow()
    requests: list[threading.Thread] = []
    store = YouTubeTokenStore(tmp_path / "youtube.bin", cipher=ReverseCipher())
    session = YouTubeSession(make_config(tmp_path), store, flow_factory=lambda config: flow,
                             browser_open=consent_browser(flow, requests))
    session.connect()
    for thread in requests:
        thread.join(3)
        assert not thread.is_alive()
    assert flow.code == "synthetic-code"
    assert flow.redirect_uri.startswith("http://127.0.0.1:")
    assert store.load()["refresh_token"] == "synthetic-refresh"
    assert "synthetic-code" not in capsys.readouterr().err


def test_inflight_authorization_cannot_resurrect_credentials_after_cli_logout(tmp_path: Path) -> None:
    flow = FakeFlow()
    flow.release = threading.Event()
    requests: list[threading.Thread] = []
    path = tmp_path / "youtube.bin"
    store = YouTubeTokenStore(path, cipher=ReverseCipher())
    session = YouTubeSession(make_config(tmp_path), store, flow_factory=lambda config: flow,
                             browser_open=consent_browser(flow, requests))
    errors: list[Exception] = []
    def connect() -> None:
        try:
            session.connect()
        except Exception as exc:
            errors.append(exc)
    worker = threading.Thread(target=connect)
    worker.start()
    try:
        assert flow.started.wait(3)
        YouTubeTokenStore(path, cipher=ReverseCipher()).delete()
    finally:
        flow.release.set()
        worker.join(3)
        for request in requests:
            request.join(3)
    assert not worker.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], YouTubeAuthError)
    assert store.load() is None


def test_cancel_closes_oauth_listener_without_waiting_for_consent(tmp_path: Path) -> None:
    flow = FakeFlow()
    store = YouTubeTokenStore(tmp_path / "youtube.bin", cipher=ReverseCipher())
    opened = threading.Event()
    session = YouTubeSession(make_config(tmp_path), store, flow_factory=lambda config: flow,
                             browser_open=lambda url: opened.set())
    errors: list[Exception] = []
    def connect() -> None:
        try:
            session.connect()
        except Exception as exc:
            errors.append(exc)
    worker = threading.Thread(target=connect)
    worker.start()
    assert opened.wait(3)
    session.cancel_authorization()
    worker.join(2)
    assert not worker.is_alive()
    assert isinstance(errors[0], YouTubeCancelled)
    assert store.load() is None


def test_missing_credentials_and_wrong_client_type_are_explicit(tmp_path: Path) -> None:
    store = YouTubeTokenStore(tmp_path / "youtube.bin", cipher=ReverseCipher())
    session = YouTubeSession(token_store=store)
    with pytest.raises(YouTubeAuthError) as required:
        session.validated_credentials()
    assert required.value.code == "youtube_auth_required"
    with pytest.raises(YouTubeAuthError) as configured:
        session.connect()
    assert configured.value.code == "youtube_client_config_missing"
    config = tmp_path / "web.json"
    config.write_text('{"web": {"client_id": "wrong"}}', encoding="utf-8")
    with pytest.raises(YouTubeAuthError) as wrong:
        YouTubeSession(config, store).connect()
    assert wrong.value.code == "youtube_client_config_invalid"


def test_corrupt_checkpoint_never_returns_untrusted_data(tmp_path: Path) -> None:
    store = EncryptedJsonStore(tmp_path / "session.bin", cipher=ReverseCipher())
    store.path.write_bytes(b"corrupt")
    with pytest.raises(YouTubeCredentialStoreError):
        store.load()


def test_new_authorization_invalidates_old_attempt_but_preserves_existing_account(tmp_path: Path) -> None:
    store = YouTubeTokenStore(tmp_path / "youtube.bin", cipher=ReverseCipher())
    credentials = json.loads(FakeCredentials().to_json())
    previous = store.epoch()
    store.save_if_current(credentials, previous)
    current = store.begin_authorization()
    assert current != previous and store.load() == credentials
    with pytest.raises(YouTubeAuthError):
        store.save_if_current({**credentials, "token": "old-request"}, previous)


def test_logout_during_refresh_cannot_resurrect_credentials(tmp_path: Path) -> None:
    path = tmp_path / "youtube.bin"
    store = YouTubeTokenStore(path, cipher=ReverseCipher())
    store.save_if_current(json.loads(FakeCredentials().to_json()), store.epoch())
    started, release = threading.Event(), threading.Event()
    credentials = FakeCredentials(valid=False)
    def refresh(request) -> None:
        started.set()
        assert release.wait(3)
        credentials.valid = True
    credentials.refresh = refresh
    session = YouTubeSession(token_store=store, credentials_factory=lambda payload: credentials)
    errors: list[Exception] = []
    def validate() -> None:
        try:
            session.validated_credentials()
        except Exception as exc:
            errors.append(exc)
    worker = threading.Thread(target=validate)
    worker.start()
    try:
        assert started.wait(3)
        YouTubeTokenStore(path, cipher=ReverseCipher()).delete()
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert isinstance(errors[0], YouTubeAuthError)
    assert store.load() is None
