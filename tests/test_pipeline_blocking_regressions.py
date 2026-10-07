from __future__ import annotations

import subprocess
import sys
import threading
import time
import unittest
from collections.abc import Callable, Sequence
from typing import BinaryIO
from unittest.mock import Mock

from analysis.audio_service import AnalysisCancellation, _terminate_process
from automation.twitch import DeviceAuthorization, TwitchApiClient, TwitchIdentity, TwitchToken
from automation.twitch_auth import TwitchAuthRequired, TwitchSession, TwitchTokenStore
from utils.subprocess_utils import popen_no_window


class MemoryTokenStore(TwitchTokenStore):
    """Keep synthetic credentials in memory so race tests never use user data."""

    def __init__(self, token: TwitchToken | None = None) -> None:
        self._lock = threading.RLock()
        self._token = token

    def load(self) -> TwitchToken | None:
        with self._lock:
            return self._token

    def save(self, token: TwitchToken) -> None:
        with self._lock:
            self._token = token

    def delete(self) -> None:
        with self._lock:
            self._token = None


class BlockingTwitchClient(TwitchApiClient):
    def __init__(self, blocked_operation: str) -> None:
        super().__init__("test-client")
        self.blocked_operation = blocked_operation
        self.request_started = threading.Event()
        self.release_request = threading.Event()
        self.poll_calls = 0
        self.refresh_calls = 0
        self.authorization = DeviceAuthorization("test-device", "TESTCODE", "https://example.invalid", 60, 1)
        self.authorized_token = TwitchToken("new-account-access", "new-account-refresh", 5000.0)

    def _block(self, operation: str) -> None:
        if operation == self.blocked_operation:
            self.request_started.set()
            if not self.release_request.wait(timeout=5.0):
                raise TimeoutError("The test did not release the synthetic Twitch response.")

    def start_device_authorization(self, scopes: Sequence[str] = ()) -> DeviceAuthorization:
        self._block("begin")
        return self.authorization

    def poll_device_authorization(
        self,
        authorization: DeviceAuthorization,
        scopes: Sequence[str] = (),
    ) -> TwitchToken:
        self.poll_calls += 1
        self._block("poll")
        return self.authorized_token

    def validate_access_token(self, access_token: str) -> TwitchIdentity:
        self._block("validation")
        return TwitchIdentity("test-client", "test-user", "test-login", (), 3600)

    def refresh_access_token(self, refresh_token: str) -> TwitchToken:
        self.refresh_calls += 1
        self._block("refresh")
        return TwitchToken("rotated-access", "rotated-refresh", 5000.0)


def run_in_background(callback: Callable[[], object]) -> tuple[threading.Thread, list[object], list[BaseException]]:
    results: list[object] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            results.append(callback())
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, results, errors


class PipelineBlockingRegressions(unittest.TestCase):
    def test_audio_cancel_releases_both_blocked_pipe_readers(self) -> None:
        process = popen_no_window(
            [getattr(sys, "_base_executable", sys.executable), "-u", "-c", "import time; print('ready', flush=True); time.sleep(30)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        cancellation = AnalysisCancellation()
        cancellation.attach_process(process)
        readers: list[threading.Thread] = []
        reader_started = [threading.Event(), threading.Event()]
        stopper: threading.Thread | None = None
        stopped_without_external_kill = False
        cancel_returned = False

        def read_pipe(stream: BinaryIO, started: threading.Event) -> None:
            started.set()
            try:
                stream.read(1)
            except (OSError, ValueError):
                pass

        try:
            assert process.stdout is not None
            assert process.stderr is not None
            self.assertEqual(process.stdout.readline().strip(), b"ready")
            for stream, started in zip((process.stdout, process.stderr), reader_started):
                reader = threading.Thread(target=read_pipe, args=(stream, started), daemon=True)
                readers.append(reader)
                reader.start()
            self.assertTrue(all(started.wait(timeout=1.0) for started in reader_started))
            time.sleep(0.05)
            stopper = threading.Thread(target=cancellation.cancel, daemon=True)
            stopper.start()
            stopper.join(timeout=2.0)
            cancel_returned = not stopper.is_alive()
            stopped_without_external_kill = process.poll() is not None
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3.0)
            if stopper is not None:
                stopper.join(timeout=3.0)
            for reader in readers:
                reader.join(timeout=3.0)
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()
            cancellation.detach_process(process)

        self.assertTrue(cancel_returned, "Cancellation blocked on a pipe reader until the external cleanup kill.")
        self.assertTrue(stopped_without_external_kill)
        self.assertFalse(any(reader.is_alive() for reader in readers))

    def test_audio_cancel_kills_process_when_termination_times_out(self) -> None:
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("ffmpeg", 0.75), 0]

        _terminate_process(process)

        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertEqual(process.wait.call_count, 2)
        process.stdout.close.assert_not_called()
        process.stderr.close.assert_not_called()

    def test_logout_rejects_inflight_device_authorization(self) -> None:
        client = BlockingTwitchClient("poll")
        store = MemoryTokenStore()
        session = TwitchSession(client, store)
        authorization = session.begin_device_authorization()
        thread, results, errors = run_in_background(lambda: session.complete_device_authorization(authorization))
        try:
            self.assertTrue(client.request_started.wait(timeout=1.0))
            session.disconnect()
            self.assertIsNone(store.load())
        finally:
            client.release_request.set()
            thread.join(timeout=2.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(results, [])
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], TwitchAuthRequired)
        self.assertIsNone(store.load())

    def test_cancel_authorization_preserves_existing_credentials(self) -> None:
        client = BlockingTwitchClient("poll")
        previous = TwitchToken("existing-access", "existing-refresh", 5000.0)
        store = MemoryTokenStore(previous)
        session = TwitchSession(client, store)
        authorization = session.begin_device_authorization()
        thread, results, errors = run_in_background(lambda: session.complete_device_authorization(authorization))
        try:
            self.assertTrue(client.request_started.wait(timeout=1.0))
            session.cancel_authorization()
            self.assertEqual(store.load(), previous)
        finally:
            client.release_request.set()
            thread.join(timeout=2.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(results, [])
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], TwitchAuthRequired)
        self.assertEqual(store.load(), previous)

    def test_cancelled_begin_cannot_return_a_device_code(self) -> None:
        client = BlockingTwitchClient("begin")
        store = MemoryTokenStore()
        session = TwitchSession(client, store)
        thread, results, errors = run_in_background(session.begin_device_authorization)
        try:
            self.assertTrue(client.request_started.wait(timeout=1.0))
            session.cancel_authorization()
        finally:
            client.release_request.set()
            thread.join(timeout=2.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(results, [])
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], TwitchAuthRequired)

    def test_device_code_from_before_logout_is_rejected_before_poll(self) -> None:
        client = BlockingTwitchClient("")
        store = MemoryTokenStore()
        session = TwitchSession(client, store)
        authorization = session.begin_device_authorization()
        session.disconnect()

        with self.assertRaises(TwitchAuthRequired):
            session.complete_device_authorization(authorization)

        self.assertEqual(client.poll_calls, 0)
        self.assertIsNone(store.load())

    def test_logout_does_not_wait_for_validation_or_refresh_network(self) -> None:
        for operation, expires_at in (("validation", 5000.0), ("refresh", 90.0)):
            with self.subTest(operation=operation):
                client = BlockingTwitchClient(operation)
                store = MemoryTokenStore(TwitchToken("existing-access", "existing-refresh", expires_at))
                session = TwitchSession(client, store, clock=lambda: 100.0)
                thread, results, errors = run_in_background(session.validated_context)
                logout: threading.Thread | None = None
                logout_errors: list[BaseException] = []
                logout_returned = False
                try:
                    self.assertTrue(client.request_started.wait(timeout=1.0))
                    logout, _logout_results, logout_errors = run_in_background(session.disconnect)
                    logout.join(timeout=0.75)
                    logout_returned = not logout.is_alive()
                finally:
                    client.release_request.set()
                    thread.join(timeout=2.0)
                    if logout is not None:
                        logout.join(timeout=2.0)

                self.assertTrue(logout_returned, "Logout waited for the pending network response.")
                self.assertEqual(logout_errors, [])
                self.assertFalse(thread.is_alive())
                self.assertEqual(results, [])
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], TwitchAuthRequired)
                self.assertIsNone(store.load())

    def test_old_validation_cannot_overwrite_newly_authorized_account(self) -> None:
        client = BlockingTwitchClient("validation")
        store = MemoryTokenStore(TwitchToken("existing-access", "existing-refresh", 5000.0))
        session = TwitchSession(client, store, clock=lambda: 100.0)
        thread, results, errors = run_in_background(session.validated_context)
        try:
            self.assertTrue(client.request_started.wait(timeout=1.0))
            authorization = session.begin_device_authorization()
            token = session.complete_device_authorization(authorization)
            self.assertEqual(store.load(), token)
        finally:
            client.release_request.set()
            thread.join(timeout=2.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(results, [])
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], TwitchAuthRequired)
        self.assertEqual(store.load(), client.authorized_token)

    def test_parallel_validation_does_not_rotate_refresh_token_twice(self) -> None:
        client = BlockingTwitchClient("refresh")
        store = MemoryTokenStore(TwitchToken("existing-access", "existing-refresh", 90.0))
        session = TwitchSession(client, store, clock=lambda: 100.0)
        first, first_results, first_errors = run_in_background(session.validated_context)
        second: threading.Thread | None = None
        second_results: list[object] = []
        second_errors: list[BaseException] = []
        try:
            self.assertTrue(client.request_started.wait(timeout=1.0))
            second, second_results, second_errors = run_in_background(session.validated_context)
        finally:
            client.release_request.set()
            first.join(timeout=2.0)
            if second is not None:
                second.join(timeout=2.0)

        self.assertFalse(first.is_alive())
        self.assertIsNotNone(second)
        assert second is not None
        self.assertFalse(second.is_alive())
        self.assertEqual(first_errors + second_errors, [])
        self.assertEqual(len(first_results), 1)
        self.assertEqual(second_results, first_results)
        self.assertEqual(client.refresh_calls, 1)


if __name__ == "__main__":
    unittest.main()
