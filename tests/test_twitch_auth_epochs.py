from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from unittest.mock import patch

import pytest

from automation.twitch import DeviceAuthorization, TwitchApiError, TwitchApiClient, TwitchIdentity, TwitchToken
from automation.twitch_auth import (
    TOKEN_STORE_FORMAT, TOKEN_STORE_VERSION, TwitchAuthRequired, TwitchSession, TwitchTokenStore, TwitchTokenStoreError,
)
from utils.subprocess_utils import run_no_window


class FakeCipher:
    def protect(self, value: bytes) -> bytes:
        return b"fake-encrypted:" + value[::-1]

    def unprotect(self, value: bytes) -> bytes:
        return value.removeprefix(b"fake-encrypted:")[::-1]


def token(name: str, expires_at: float = 5000.0) -> TwitchToken:
    return TwitchToken(f"fake-{name}-access", f"fake-{name}-refresh", expires_at)


class BlockingClient(TwitchApiClient):
    def __init__(self, operation: str = "") -> None:
        super().__init__("fake-client")
        self.operation = operation
        self.started = threading.Event()
        self.release = threading.Event()
        self.authorization = DeviceAuthorization("fake-device", "FAKE-CODE", "https://example.invalid", 60, 1)
        self.refresh_error: TwitchApiError | None = None
        self.validate_calls = 0

    def _block(self, operation: str) -> None:
        if self.operation == operation:
            self.started.set()
            if not self.release.wait(3):
                raise RuntimeError("fake response was not released")

    def start_device_authorization(self, scopes: Sequence[str] = ()) -> DeviceAuthorization:
        self._block("begin")
        return self.authorization

    def poll_device_authorization(self, authorization: DeviceAuthorization, scopes: Sequence[str] = ()) -> TwitchToken:
        self._block("poll")
        return token("old-response")

    def refresh_access_token(self, refresh_token: str) -> TwitchToken:
        self._block("refresh")
        if self.refresh_error is not None:
            raise self.refresh_error
        return token("old-refresh-response")

    def validate_access_token(self, access_token: str) -> TwitchIdentity:
        self.validate_calls += 1
        self._block("validation")
        login = "new-account" if access_token == token("new-account").access_token else "old-account"
        return TwitchIdentity("fake-client", login, login, (), 3600)


def in_background(callback: Callable[[], object]) -> tuple[threading.Thread, list[object], list[BaseException]]:
    results: list[object] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            results.append(callback())
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, results, errors


@pytest.mark.parametrize("operation", ["begin", "poll", "validation", "refresh"])
def test_logout_from_another_store_invalidates_inflight_response(tmp_path: Path, operation: str) -> None:
    store = TwitchTokenStore(tmp_path / "credentials.bin", cipher=FakeCipher())
    other = TwitchTokenStore(store.path, cipher=FakeCipher())
    store.save(token("old-account", 90.0 if operation == "refresh" else 5000.0))
    client = BlockingClient(operation)
    session = TwitchSession(client, store, clock=lambda: 100.0)
    if operation == "begin":
        callback = session.begin_device_authorization
    elif operation == "poll":
        authorization = session.begin_device_authorization()
        callback = lambda: session.complete_device_authorization(authorization)
    else:
        callback = session.validated_context
    thread, results, errors = in_background(callback)
    try:
        assert client.started.wait(1)
        other.delete()
        assert other.load() is None
    finally:
        client.release.set()
        thread.join(3)
    assert not thread.is_alive()
    assert results == []
    assert len(errors) == 1 and isinstance(errors[0], TwitchAuthRequired)
    assert store.load() is None


@pytest.mark.parametrize("operation,fail", [("poll", False), ("validation", False), ("refresh", False), ("refresh", True)])
def test_old_response_or_error_cannot_change_new_account(tmp_path: Path, operation: str, fail: bool) -> None:
    store = TwitchTokenStore(tmp_path / "credentials.bin", cipher=FakeCipher())
    other = TwitchTokenStore(store.path, cipher=FakeCipher())
    store.save(token("old-account", 90.0 if operation == "refresh" else 5000.0))
    client = BlockingClient(operation)
    if fail:
        client.refresh_error = TwitchApiError(400, "fake expired old refresh token")
    session = TwitchSession(client, store, clock=lambda: 100.0)
    authorization = session.begin_device_authorization() if operation == "poll" else None
    thread, results, errors = in_background(
        lambda: session.complete_device_authorization(authorization) if authorization else session.validated_context(),
    )
    try:
        assert client.started.wait(1)
        other.delete()
        other.save(token("new-account"))
        new_encrypted = other.path.read_bytes()
    finally:
        client.release.set()
        thread.join(3)
    assert not thread.is_alive()
    assert results == []
    assert len(errors) == 1 and isinstance(errors[0], TwitchAuthRequired)
    assert other.path.read_bytes() == new_encrypted
    assert store.load() == token("new-account")


def test_new_login_in_another_session_invalidates_cached_identity(tmp_path: Path) -> None:
    store = TwitchTokenStore(tmp_path / "credentials.bin", cipher=FakeCipher())
    store.save(token("old-account"))
    client = BlockingClient()
    session = TwitchSession(client, store, clock=lambda: 100.0)
    _, old_identity = session.validated_context()
    assert old_identity.login == "old-account"
    TwitchTokenStore(store.path, cipher=FakeCipher()).save(token("new-account"))
    current, identity = session.validated_context()
    assert current.access_token == token("new-account").access_token
    assert identity.login == "new-account"
    assert client.validate_calls == 2


def test_epoch_prevents_logout_crash_from_restoring_old_file(tmp_path: Path) -> None:
    store = TwitchTokenStore(tmp_path / "credentials.bin", cipher=FakeCipher())
    store.save(token("old-account"))
    original = Path.unlink

    def crash_before_token_delete(path: Path, *args, **kwargs) -> None:
        if path == store.path:
            raise OSError("fake process failure before token removal")
        original(path, *args, **kwargs)

    with patch.object(Path, "unlink", crash_before_token_delete):
        with pytest.raises(TwitchTokenStoreError):
            store.delete()
    assert store.path.exists()
    assert TwitchTokenStore(store.path, cipher=FakeCipher()).load() is None


def test_failed_new_account_publish_never_restores_old_session(tmp_path: Path) -> None:
    store = TwitchTokenStore(tmp_path / "credentials.bin", cipher=FakeCipher())
    store.save(token("old-account"))
    original = os.replace

    def fail_token_publish(source, destination) -> None:
        if Path(destination) == store.path:
            raise OSError("fake token publication crash")
        original(source, destination)

    with patch("automation.twitch_auth.os.replace", side_effect=fail_token_publish):
        with pytest.raises(TwitchTokenStoreError):
            store.save(token("new-account"))
    assert store.path.exists()
    assert store.load() is None


def test_legacy_v1_without_epoch_is_backward_compatible(tmp_path: Path) -> None:
    store = TwitchTokenStore(tmp_path / "credentials.bin", cipher=FakeCipher())
    payload = {"format": TOKEN_STORE_FORMAT, "version": TOKEN_STORE_VERSION, "token": token("legacy").to_mapping()}
    store.path.write_bytes(FakeCipher().protect(json.dumps(payload).encode("utf-8")))
    assert store.load() == token("legacy")
    store.delete()
    assert store.load() is None


def test_stale_compare_and_save_from_another_process_is_rejected(tmp_path: Path) -> None:
    store = TwitchTokenStore(tmp_path / "credentials.bin", cipher=FakeCipher())
    epoch = store.epoch()
    store.delete()
    code = """
import sys
from pathlib import Path
from automation.twitch import TwitchToken
from automation.twitch_auth import TwitchTokenStore
class Cipher:
    def protect(self, value): return b'fake-encrypted:' + value[::-1]
    def unprotect(self, value): return value.removeprefix(b'fake-encrypted:')[::-1]
store = TwitchTokenStore(Path(sys.argv[1]), cipher=Cipher())
print(store.save_if_epoch(TwitchToken('fake-old-access', 'fake-old-refresh', 5000), int(sys.argv[2])))
"""
    result = run_no_window(
        [sys.executable, "-c", code, str(store.path), str(epoch)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"
    assert store.load() is None


def test_invalid_epoch_is_explicit_and_does_not_overwrite_credentials(tmp_path: Path) -> None:
    store = TwitchTokenStore(tmp_path / "credentials.bin", cipher=FakeCipher())
    store.save(token("old-account"))
    before = store.path.read_bytes()
    store.path.with_name(f"{store.path.name}.epoch").write_text("broken", encoding="ascii")
    with pytest.raises(TwitchTokenStoreError):
        store.save(token("new-account"))
    assert store.path.read_bytes() == before
