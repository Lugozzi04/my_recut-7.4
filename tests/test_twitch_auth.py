from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from automation.twitch import TwitchApiError, TwitchIdentity, TwitchToken
from automation.twitch_auth import (
    TwitchAuthRequired,
    TwitchSession,
    TwitchTokenStore,
    TwitchTokenStoreError,
    WindowsDpapiCipher,
)


class ReverseCipher:
    def protect(self, plaintext: bytes) -> bytes:
        return b"encrypted:" + plaintext[::-1]

    def unprotect(self, ciphertext: bytes) -> bytes:
        if not ciphertext.startswith(b"encrypted:"):
            raise TwitchTokenStoreError("invalid test ciphertext")
        return ciphertext.removeprefix(b"encrypted:")[::-1]


class FakeClient:
    def __init__(self) -> None:
        self.validate_calls = 0
        self.refresh_calls = 0
        self.validation_error: TwitchApiError | None = None

    def validate_access_token(self, _access_token: str) -> TwitchIdentity:
        self.validate_calls += 1
        if self.validation_error is not None:
            error = self.validation_error
            self.validation_error = None
            raise error
        return TwitchIdentity(
            client_id="client-1",
            user_id="user-1",
            login="streamer",
            scopes=(),
            expires_in=3600,
        )

    def refresh_access_token(self, _refresh_token: str) -> TwitchToken:
        self.refresh_calls += 1
        return TwitchToken(
            access_token=f"new-access-{self.refresh_calls}",
            refresh_token=f"new-refresh-{self.refresh_calls}",
            expires_at=9999.0,
        )


class TwitchAuthTests(unittest.TestCase):
    def make_token(self, *, expires_at: float = 5000.0) -> TwitchToken:
        return TwitchToken(
            access_token="secret-access",
            refresh_token="secret-refresh",
            expires_at=expires_at,
            scopes=(),
        )

    def test_encrypted_store_round_trip_contains_no_plaintext_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "token.bin"
            store = TwitchTokenStore(path, cipher=ReverseCipher())
            token = self.make_token()

            store.save(token)

            self.assertEqual(store.load(), token)
            encrypted = path.read_bytes()
            self.assertNotIn(token.access_token.encode("utf-8"), encrypted)
            self.assertNotIn(token.refresh_token.encode("utf-8"), encrypted)

    def test_corrupt_encrypted_store_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "token.bin"
            path.write_bytes(b"not-encrypted")
            store = TwitchTokenStore(path, cipher=ReverseCipher())

            with self.assertRaises(TwitchTokenStoreError):
                store.load()

    def test_failed_atomic_replace_keeps_previous_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "token.bin"
            store = TwitchTokenStore(path, cipher=ReverseCipher())
            store.save(self.make_token())
            before = path.read_bytes()

            with patch("automation.twitch_auth.os.replace", side_effect=OSError("locked")):
                with self.assertRaises(TwitchTokenStoreError):
                    store.save(self.make_token(expires_at=6000.0))

            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(path.parent.glob(".*.tmp")), [])

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI test")
    def test_windows_dpapi_round_trip(self) -> None:
        cipher = WindowsDpapiCipher()
        plaintext = b"local-user-secret"

        encrypted = cipher.protect(plaintext)

        self.assertNotEqual(encrypted, plaintext)
        self.assertEqual(cipher.unprotect(encrypted), plaintext)

    def test_session_refreshes_expired_token_then_validates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TwitchTokenStore(Path(tmp) / "token.bin", cipher=ReverseCipher())
            store.save(self.make_token(expires_at=90.0))
            client = FakeClient()
            session = TwitchSession(client, store, clock=lambda: 100.0)  # type: ignore[arg-type]

            token, identity = session.validated_context()

            self.assertEqual(client.refresh_calls, 1)
            self.assertEqual(client.validate_calls, 1)
            self.assertEqual(token.access_token, "new-access-1")
            self.assertEqual(identity.user_id, "user-1")
            self.assertEqual(store.load(), token)

    def test_session_validates_at_most_once_per_hour(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TwitchTokenStore(Path(tmp) / "token.bin", cipher=ReverseCipher())
            store.save(self.make_token())
            client = FakeClient()
            now = [100.0]
            session = TwitchSession(client, store, clock=lambda: now[0])  # type: ignore[arg-type]

            session.validated_context()
            now[0] = 200.0
            session.validated_context()
            now[0] = 3800.0
            session.validated_context()

            self.assertEqual(client.validate_calls, 2)

    def test_unauthorized_validation_refreshes_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TwitchTokenStore(Path(tmp) / "token.bin", cipher=ReverseCipher())
            store.save(self.make_token())
            client = FakeClient()
            client.validation_error = TwitchApiError(401, "invalid access token")
            session = TwitchSession(client, store, clock=lambda: 100.0)  # type: ignore[arg-type]

            token, _identity = session.validated_context()

            self.assertEqual(client.refresh_calls, 1)
            self.assertEqual(client.validate_calls, 2)
            self.assertEqual(token.access_token, "new-access-1")

    def test_missing_token_requires_connection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TwitchTokenStore(Path(tmp) / "missing.bin", cipher=ReverseCipher())
            session = TwitchSession(FakeClient(), store)  # type: ignore[arg-type]

            with self.assertRaises(TwitchAuthRequired):
                session.validated_context()


if __name__ == "__main__":
    unittest.main()
