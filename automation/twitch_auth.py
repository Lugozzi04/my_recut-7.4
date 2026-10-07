from __future__ import annotations

import ctypes
import json
import os
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from collections.abc import Iterator
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol

from automation.store import default_pipeline_store_path
from automation.locking import FileLockError, ProcessFileLock
from automation.twitch import (
    DeviceAuthorization,
    TwitchApiClient,
    TwitchApiError,
    TwitchIdentity,
    TwitchProtocolError,
    TwitchToken,
)
from utils.credential_files import migrate_credential_file
from utils.runtime_paths import credentials_root


TOKEN_STORE_FORMAT = "auto_cutter_twitch_token"
TOKEN_STORE_VERSION = 1
_DPAPI_ENTROPY = b"Auto Cutter Twitch OAuth v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class TwitchTokenStoreError(RuntimeError):
    pass


class TwitchAuthRequired(RuntimeError):
    pass


class SecretCipher(Protocol):
    def protect(self, plaintext: bytes) -> bytes: ...

    def unprotect(self, ciphertext: bytes) -> bytes: ...


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _blob(value: bytes) -> tuple[_DataBlob, object]:
    buffer = ctypes.create_string_buffer(value, len(value))
    blob = _DataBlob(
        len(value),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
    )
    return blob, buffer


class WindowsDpapiCipher:
    """Encrypts secrets for the current Windows user without a bundled key."""

    def protect(self, plaintext: bytes) -> bytes:
        return self._crypt(bytes(plaintext), protect=True)

    def unprotect(self, ciphertext: bytes) -> bytes:
        return self._crypt(bytes(ciphertext), protect=False)

    @staticmethod
    def _crypt(value: bytes, *, protect: bool) -> bytes:
        if os.name != "nt":
            raise TwitchTokenStoreError("Windows DPAPI is only available on Windows.")
        if not value:
            raise TwitchTokenStoreError("Cannot protect an empty Twitch credential payload.")

        ctypes_runtime: Any = ctypes
        crypt32: Any = ctypes_runtime.WinDLL("crypt32", use_last_error=True)
        kernel32: Any = ctypes_runtime.WinDLL("kernel32", use_last_error=True)
        input_blob, input_buffer = _blob(value)
        entropy_blob, entropy_buffer = _blob(_DPAPI_ENTROPY)
        output_blob = _DataBlob()
        _keep_alive = (input_buffer, entropy_buffer)

        function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
        result = function(
            ctypes.byref(input_blob),
            None,
            ctypes.byref(entropy_blob),
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )
        if not result:
            error_code = int(ctypes_runtime.get_last_error())
            operation = "protect" if protect else "unprotect"
            raise TwitchTokenStoreError(f"Windows DPAPI could not {operation} Twitch credentials ({error_code}).")
        try:
            return ctypes.string_at(output_blob.pbData, int(output_blob.cbData))
        finally:
            kernel32.LocalFree(output_blob.pbData)


def default_twitch_token_path() -> Path:
    custom = str(os.environ.get("AUTO_CUTTER_TWITCH_TOKEN_FILE", "") or "").strip()
    if custom:
        return Path(custom).expanduser().resolve()
    return credentials_root() / "twitch-token.bin"


class TwitchTokenStore:
    def __init__(
        self,
        path: Path | str | None = None,
        *,
        cipher: SecretCipher | None = None,
    ) -> None:
        self.path = (Path(path) if path is not None else default_twitch_token_path()).expanduser().resolve()
        self._epoch_path = self.path.with_name(f"{self.path.name}.epoch")
        self._credential_lock = ProcessFileLock(self.path.with_name(f"{self.path.name}.lock"))
        self._legacy_path: Path | None = None
        if path is None and not str(os.environ.get("AUTO_CUTTER_TWITCH_TOKEN_FILE", "") or "").strip():
            self._legacy_path = default_pipeline_store_path().with_name("twitch-token.bin")
            try:
                migrate_credential_file(self._legacy_path, self.path)
            except OSError as exc:
                raise TwitchTokenStoreError("Cannot migrate the encrypted Twitch credentials.") from exc
        self.cipher = cipher or WindowsDpapiCipher()
        self._lock = threading.RLock()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._lock:
            try:
                with self._credential_lock.hold(timeout=10):
                    yield
            except FileLockError as exc:
                raise TwitchTokenStoreError("Cannot lock the encrypted Twitch credentials.") from exc

    def _epoch_unlocked(self) -> int:
        try:
            if not self._epoch_path.exists():
                return 0
            value = int(self._epoch_path.read_text(encoding="ascii").strip())
            if value < 0:
                raise ValueError("negative epoch")
            return value
        except (OSError, UnicodeError, ValueError) as exc:
            raise TwitchTokenStoreError("Cannot read the Twitch credential session epoch.") from exc

    def epoch(self) -> int:
        with self._locked():
            return self._epoch_unlocked()

    def _write_epoch_unlocked(self, epoch: int) -> None:
        temporary = self._epoch_path.with_name(f".{self._epoch_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="ascii", newline="\n") as stream:
                stream.write(f"{epoch}\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._epoch_path)
        except OSError as exc:
            raise TwitchTokenStoreError("Cannot persist the Twitch credential session epoch.") from exc
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def load(self) -> TwitchToken | None:
        with self._locked():
            epoch = self._epoch_unlocked()
            if not self.path.exists():
                return None
            try:
                encrypted = self.path.read_bytes()
                plaintext = self.cipher.unprotect(encrypted)
                raw = json.loads(plaintext.decode("utf-8"))
                if not isinstance(raw, dict):
                    raise TwitchTokenStoreError("Twitch token payload root must be an object.")
                if raw.get("format") != TOKEN_STORE_FORMAT:
                    raise TwitchTokenStoreError("Unsupported Twitch token store format.")
                if int(raw.get("version", 0)) != TOKEN_STORE_VERSION:
                    raise TwitchTokenStoreError("Unsupported Twitch token store version.")
                if int(raw.get("auth_epoch", 0)) != epoch:
                    # Logout persists its epoch first. Even a crash before the
                    # old encrypted token is removed cannot restore that login.
                    return None
                token_raw = raw.get("token")
                if not isinstance(token_raw, dict):
                    raise TwitchTokenStoreError("Twitch token payload is missing.")
                return TwitchToken.from_mapping(token_raw)
            except TwitchTokenStoreError:
                raise
            except (OSError, UnicodeError, json.JSONDecodeError, TwitchProtocolError, TypeError, ValueError) as exc:
                raise TwitchTokenStoreError("Cannot read the encrypted Twitch credentials.") from exc

    def save(self, token: TwitchToken) -> None:
        with self._locked():
            self._save_unlocked(token, self._epoch_unlocked() + 1, advance_epoch=True)

    def save_if_epoch(self, token: TwitchToken, expected: int, *, advance_epoch: bool = False) -> bool:
        """Publish only a response owned by the still-current login session."""
        with self._locked():
            epoch = self._epoch_unlocked()
            if epoch != expected:
                return False
            self._save_unlocked(token, epoch + 1 if advance_epoch else epoch, advance_epoch=advance_epoch)
            return True

    def _save_unlocked(self, token: TwitchToken, epoch: int, *, advance_epoch: bool) -> None:
        try:
            payload = {
                "format": TOKEN_STORE_FORMAT,
                "version": TOKEN_STORE_VERSION,
                "token": token.to_mapping(),
                "auth_epoch": epoch,
            }
            plaintext = (json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n").encode("utf-8")
            encrypted = self.cipher.protect(plaintext)
            if not encrypted:
                raise TwitchTokenStoreError("Credential encryption returned an empty payload.")
        except TwitchTokenStoreError:
            raise
        except (TypeError, ValueError) as exc:
            raise TwitchTokenStoreError("Cannot serialize Twitch credentials.") from exc

        self.path.parent.mkdir(parents=True, exist_ok=True)
        if advance_epoch:
            self._write_epoch_unlocked(epoch)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            raise TwitchTokenStoreError("Cannot save encrypted Twitch credentials.") from exc
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def delete(self) -> None:
        with self._locked():
            try:
                self._write_epoch_unlocked(self._epoch_unlocked() + 1)
                self.path.unlink(missing_ok=True)
                if self._legacy_path is not None:
                    # A conflicting legacy file must not restore credentials
                    # during the next startup after an explicit logout.
                    self._legacy_path.unlink(missing_ok=True)
            except OSError as exc:
                raise TwitchTokenStoreError("Cannot remove encrypted Twitch credentials.") from exc


class TwitchSession:
    """Maintains a validated OAuth session and rotates public-client refresh tokens."""

    def __init__(
        self,
        client: TwitchApiClient,
        token_store: TwitchTokenStore | None = None,
        *,
        validation_interval_s: float = 3600.0,
        refresh_margin_s: float = 120.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.client = client
        self.token_store = token_store or TwitchTokenStore()
        self.validation_interval_s = max(60.0, float(validation_interval_s))
        self.refresh_margin_s = max(0.0, float(refresh_margin_s))
        self._clock = clock
        self._lock = threading.RLock()
        self._validation_lock = threading.Lock()
        self._generation = 0
        self._authorization_generations: dict[str, tuple[int, int | None]] = {}
        self._last_validated_at = 0.0
        self._identity: TwitchIdentity | None = None
        self._identity_epoch: int | None = None

    def begin_device_authorization(self, scopes: Sequence[str] = ()) -> DeviceAuthorization:
        with self._lock:
            generation = self._generation
            epoch = self._store_epoch()
        authorization = self.client.start_device_authorization(scopes)
        with self._lock:
            self._check_generation(generation)
            self._check_epoch(epoch)
            self._authorization_generations[authorization.device_code] = (generation, epoch)
        return authorization

    def complete_device_authorization(
        self,
        authorization: DeviceAuthorization,
        scopes: Sequence[str] = (),
    ) -> TwitchToken:
        with self._lock:
            generation, epoch = self._authorization_generations.get(
                authorization.device_code, (self._generation, self._store_epoch()),
            )
            self._check_generation(generation)
            self._check_epoch(epoch)
        token = self.client.poll_device_authorization(authorization, scopes)
        with self._lock:
            self._check_generation(generation)
            self._save_token(token, epoch, advance_epoch=True)
            # An account change also invalidates validation of the previous
            # token, even when both requests overlapped within this session.
            self._generation += 1
            self._identity = None
            self._identity_epoch = None
            self._last_validated_at = 0.0
        return token

    def validated_context(self) -> tuple[TwitchToken, TwitchIdentity]:
        with self._lock:
            generation = self._generation
        # Serialize rotating refresh tokens without holding the lock needed by
        # logout. A response from before logout must never recreate credentials.
        with self._validation_lock:
            with self._lock:
                self._check_generation(generation)
                epoch = self._store_epoch()
                token = self.token_store.load()
                self._check_epoch(epoch)
                if token is None:
                    raise TwitchAuthRequired("Connect a Twitch account before enabling the VOD watcher.")

            now = float(self._clock())
            if token.expires_within(self.refresh_margin_s, now=now):
                token = self._refresh(token, generation=generation, epoch=epoch)

            with self._lock:
                self._check_generation(generation)
                self._check_epoch(epoch)
                validation_due = (self._identity is None or self._identity_epoch != epoch
                                  or now - self._last_validated_at >= self.validation_interval_s)
            if validation_due:
                try:
                    identity = self.client.validate_access_token(token.access_token)
                except TwitchApiError as exc:
                    with self._lock:
                        self._check_generation(generation)
                        self._check_epoch(epoch)
                    if exc.status != 401:
                        raise
                    token = self._refresh(token, generation=generation, epoch=epoch)
                    identity = self.client.validate_access_token(token.access_token)

                with self._lock:
                    self._check_generation(generation)
                    token = replace(
                        token,
                        expires_at=now + max(0, identity.expires_in),
                        scopes=identity.scopes,
                    )
                    self._save_token(token, epoch)
                    self._identity = identity
                    self._identity_epoch = epoch
                    self._last_validated_at = now

            with self._lock:
                self._check_generation(generation)
                self._check_epoch(epoch)
                assert self._identity is not None
                return token, self._identity

    def cancel_authorization(self) -> None:
        """Invalidate pending OAuth requests while keeping stored credentials."""
        with self._lock:
            self._generation += 1

    def disconnect(self) -> None:
        with self._lock:
            self._generation += 1
            self.token_store.delete()
            self._identity = None
            self._identity_epoch = None
            self._last_validated_at = 0.0

    def _check_generation(self, generation: int) -> None:
        if generation != self._generation:
            raise TwitchAuthRequired("Twitch authorization was cancelled or the account changed.")

    def _store_epoch(self) -> int | None:
        # Historical injected test stores sometimes subclass this store while
        # implementing only load/save/delete, without filesystem initialization.
        if isinstance(self.token_store, TwitchTokenStore) and not hasattr(self.token_store, "_credential_lock"):
            return None
        reader = getattr(self.token_store, "epoch", None)
        return int(reader()) if callable(reader) else None

    def _check_epoch(self, epoch: int | None) -> None:
        if epoch is not None and self._store_epoch() != epoch:
            raise TwitchAuthRequired("Twitch authorization was cancelled or the account changed.")

    def _save_token(self, token: TwitchToken, epoch: int | None, *, advance_epoch: bool = False) -> None:
        writer = getattr(self.token_store, "save_if_epoch", None)
        if epoch is None or not callable(writer):
            self.token_store.save(token)
        elif not writer(token, epoch, advance_epoch=advance_epoch):
            raise TwitchAuthRequired("Twitch authorization was cancelled or the account changed.")

    def _refresh(self, token: TwitchToken, *, generation: int, epoch: int | None) -> TwitchToken:
        with self._lock:
            self._check_generation(generation)
            self._check_epoch(epoch)
        try:
            refreshed = self.client.refresh_access_token(token.refresh_token)
        except Exception:
            with self._lock:
                self._check_generation(generation)
                self._check_epoch(epoch)
            raise
        with self._lock:
            self._check_generation(generation)
            self._save_token(refreshed, epoch)
            self._identity = None
            self._identity_epoch = None
            self._last_validated_at = 0.0
        return refreshed
