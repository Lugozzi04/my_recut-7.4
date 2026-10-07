from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from automation.manager import PipelineManager
from automation.store import PipelineStore
from automation.twitch import DeviceAuthorization, TwitchIdentity, TwitchToken
from ui.twitch_integration import TwitchIntegration, normalize_twitch_client_id


class FakeTokenStore:
    def __init__(self, path: Path) -> None:
        self.path = path


class FakeSession:
    def __init__(self, token_path: Path) -> None:
        self.token_store = FakeTokenStore(token_path)
        self.begin_calls = 0
        self.complete_calls = 0
        self.disconnected = False

    def begin_device_authorization(self, _scopes=()):
        self.begin_calls += 1
        return DeviceAuthorization(
            device_code="device",
            user_code="ABCD1234",
            verification_uri="https://www.twitch.tv/activate",
            expires_in=30,
            interval=1,
        )

    def complete_device_authorization(self, _authorization, _scopes=()):
        self.complete_calls += 1
        self.token_store.path.write_bytes(b"encrypted")
        return TwitchToken("access", "refresh", 99999.0)

    def validated_context(self):
        return (
            TwitchToken("access", "refresh", 99999.0),
            TwitchIdentity("client123", "user-1", "streamer", (), 3600),
        )

    def disconnect(self) -> None:
        self.disconnected = True
        self.token_store.path.unlink(missing_ok=True)


class FakeWatcher:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0
        self.polled = 0
        self.is_running = False

    def start(self) -> bool:
        self.started += 1
        self.is_running = True
        return True

    def stop(self, timeout: float = 5.0) -> bool:
        _ = timeout
        self.stopped += 1
        self.is_running = False
        return True

    def poll_once(self) -> None:
        self.polled += 1


class FakePipelineQueue:
    def __init__(self, _manager, *_args, **callbacks) -> None:
        self.callbacks = callbacks
        self.enqueued: list[str] = []
        self.cancelled = 0
        self.stopped = 0
        self.active_job_id = ""
        self.is_running = False
        self.output_dir = Path("downloads")

    def enqueue(self, job_id: str) -> bool:
        self.enqueued.append(job_id)
        return True

    def cancel_current(self) -> bool:
        self.cancelled += 1
        return True

    def shutdown(self, timeout: float = 3.0) -> bool:
        _ = timeout
        self.stopped += 1
        return True

    def set_output_dir(self, path: str | Path) -> None:
        self.output_dir = Path(path)


class TwitchUiIntegrationTests(unittest.TestCase):
    def test_client_id_validation(self) -> None:
        self.assertEqual(normalize_twitch_client_id(" client123 "), "client123")
        with self.assertRaises(ValueError):
            normalize_twitch_client_id("bad id")
        with self.assertRaises(ValueError):
            normalize_twitch_client_id("short")

    def test_configuration_is_network_inert_and_watcher_is_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sessions: list[FakeSession] = []
            watchers: list[FakeWatcher] = []

            def session_factory(_client):
                session = FakeSession(root / "token.bin")
                sessions.append(session)
                return session

            def watcher_factory(*_args):
                watcher = FakeWatcher()
                watchers.append(watcher)
                return watcher

            integration = TwitchIntegration(
                manager=PipelineManager(PipelineStore(root / "jobs.json")),
                client_factory=lambda _client_id: object(),  # type: ignore[arg-type,return-value]
                session_factory=session_factory,  # type: ignore[arg-type]
                watcher_factory=watcher_factory,  # type: ignore[arg-type]
            )

            integration.configure("client123")

            self.assertEqual(len(sessions), 1)
            self.assertEqual(len(watchers), 1)
            self.assertEqual(sessions[0].begin_calls, 0)
            self.assertEqual(watchers[0].started, 0)
            self.assertFalse(integration.is_running)

    def test_watcher_requires_stored_credentials_and_stops_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = FakeSession(root / "token.bin")
            watcher = FakeWatcher()
            integration = TwitchIntegration(
                manager=PipelineManager(PipelineStore(root / "jobs.json")),
                client_factory=lambda _client_id: object(),  # type: ignore[arg-type,return-value]
                session_factory=lambda _client: session,  # type: ignore[arg-type]
                watcher_factory=lambda *_args: watcher,  # type: ignore[arg-type]
            )
            integration.configure("client123")

            self.assertFalse(integration.set_enabled(True))
            session.token_store.path.write_bytes(b"encrypted")
            self.assertTrue(integration.set_enabled(True))
            self.assertTrue(integration.is_running)
            self.assertTrue(integration.set_enabled(False))
            self.assertFalse(integration.is_running)
            self.assertEqual(watcher.started, 1)
            self.assertGreaterEqual(watcher.stopped, 1)

    def test_device_authorization_emits_code_and_connected_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = FakeSession(root / "token.bin")
            integration = TwitchIntegration(
                manager=PipelineManager(PipelineStore(root / "jobs.json")),
                client_factory=lambda _client_id: object(),  # type: ignore[arg-type,return-value]
                session_factory=lambda _client: session,  # type: ignore[arg-type]
                watcher_factory=lambda *_args: FakeWatcher(),  # type: ignore[arg-type]
            )
            codes: list[tuple[str, str, int]] = []
            logins: list[str] = []
            integration.deviceCodeReady.connect(lambda code, uri, ttl: codes.append((code, uri, ttl)))
            integration.connected.connect(logins.append)
            integration.configure("client123")

            with patch.object(integration._auth_cancel, "wait", return_value=False):
                integration._run_device_authorization(())

            self.assertEqual(
                codes,
                [("ABCD1234", "https://www.twitch.tv/activate", 30)],
            )
            self.assertEqual(logins, ["streamer"])
            self.assertEqual(integration.connected_login, "streamer")
            self.assertTrue(integration.has_stored_token)

    def test_poll_now_runs_outside_caller_and_disconnects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = FakeSession(root / "token.bin")
            session.token_store.path.write_bytes(b"encrypted")
            watcher = FakeWatcher()
            integration = TwitchIntegration(
                manager=PipelineManager(PipelineStore(root / "jobs.json")),
                client_factory=lambda _client_id: object(),  # type: ignore[arg-type,return-value]
                session_factory=lambda _client: session,  # type: ignore[arg-type]
                watcher_factory=lambda *_args: watcher,  # type: ignore[arg-type]
            )
            integration.configure("client123")

            self.assertTrue(integration.poll_now())
            deadline = time.monotonic() + 1.0
            while watcher.polled == 0 and time.monotonic() < deadline:
                time.sleep(0.01)
            integration.disconnect_account()

            self.assertEqual(watcher.polled, 1)
            self.assertTrue(session.disconnected)
            self.assertFalse(session.token_store.path.exists())

    def test_clear_configuration_removes_credentials_and_runtime_objects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = FakeSession(root / "token.bin")
            session.token_store.path.write_bytes(b"encrypted")
            watcher = FakeWatcher()
            integration = TwitchIntegration(
                manager=PipelineManager(PipelineStore(root / "jobs.json")),
                client_factory=lambda _client_id: object(),  # type: ignore[arg-type,return-value]
                session_factory=lambda _client: session,  # type: ignore[arg-type]
                watcher_factory=lambda *_args: watcher,  # type: ignore[arg-type]
            )
            integration.configure("client123")

            integration.clear_configuration()

            self.assertFalse(integration.is_configured)
            self.assertEqual(integration.client_id, "")
            self.assertFalse(session.token_store.path.exists())

    def test_completed_download_is_queued_for_automatic_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queues: list[FakePipelineQueue] = []

            def queue_factory(*args, **kwargs):
                queue = FakePipelineQueue(*args, **kwargs)
                queues.append(queue)
                return queue

            integration = TwitchIntegration(
                manager=PipelineManager(PipelineStore(root / "jobs.json")),
                client_factory=lambda _client_id: object(),  # type: ignore[arg-type,return-value]
                session_factory=lambda _client: FakeSession(root / "token.bin"),  # type: ignore[arg-type]
                watcher_factory=lambda *_args: FakeWatcher(),  # type: ignore[arg-type]
                download_queue_factory=queue_factory,  # type: ignore[arg-type]
                analysis_queue_factory=queue_factory,  # type: ignore[arg-type]
            )
            finished: list[object] = []
            integration.downloadFinished.connect(finished.append)
            job = SimpleNamespace(id="job-1")

            integration._on_download_finished(job)
            integration.shutdown()

            self.assertEqual(finished, [job])
            self.assertEqual(len(queues), 2)
            self.assertEqual(queues[1].enqueued, ["job-1"])
            self.assertEqual([queue.stopped for queue in queues], [1, 1])

    def test_completed_analysis_is_queued_for_automatic_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queues: list[FakePipelineQueue] = []

            def queue_factory(*args, **kwargs):
                queue = FakePipelineQueue(*args, **kwargs)
                queues.append(queue)
                return queue

            integration = TwitchIntegration(
                manager=PipelineManager(PipelineStore(root / "jobs.json")),
                client_factory=lambda _client_id: object(),  # type: ignore[arg-type,return-value]
                session_factory=lambda _client: FakeSession(root / "token.bin"),  # type: ignore[arg-type]
                watcher_factory=lambda *_args: FakeWatcher(),  # type: ignore[arg-type]
                download_queue_factory=queue_factory,  # type: ignore[arg-type]
                analysis_queue_factory=queue_factory,  # type: ignore[arg-type]
                export_queue_factory=queue_factory,  # type: ignore[arg-type]
            )
            finished: list[object] = []
            integration.analysisFinished.connect(finished.append)
            job = SimpleNamespace(id="job-2")

            integration._on_analysis_finished(job)
            integration.shutdown()

            self.assertEqual(finished, [job])
            self.assertEqual(len(queues), 3)
            self.assertEqual(queues[2].enqueued, ["job-2"])
            self.assertEqual([queue.stopped for queue in queues], [1, 1, 1])


if __name__ == "__main__":
    unittest.main()
