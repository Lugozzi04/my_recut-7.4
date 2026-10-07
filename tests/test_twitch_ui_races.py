from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from automation.manager import PipelineManager
from automation.store import PipelineStore
from automation.twitch import DeviceAuthorization
from ui.twitch_integration import TwitchIntegration


class TwitchUiRaceTests(unittest.TestCase):
    def test_logout_during_authorization_does_not_emit_connected(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            class Session:
                token_store = SimpleNamespace(path=root / "token.bin")

                def begin_device_authorization(self, scopes):
                    return DeviceAuthorization("device", "CODE", "https://www.twitch.tv/activate", 30, 1)

                def complete_device_authorization(self, authorization, scopes):
                    entered.set()
                    release.wait(3)

                def validated_context(self):
                    return None, SimpleNamespace(login="stale-login")

                def disconnect(self):
                    pass

            session = Session()
            watcher = SimpleNamespace(stop=lambda **kwargs: True, is_running=False)
            integration = TwitchIntegration(
                manager=PipelineManager(PipelineStore(root / "jobs.json")),
                client_factory=lambda _: object(),
                session_factory=lambda _: session,
                watcher_factory=lambda *args: watcher,
                download_dir=root / "downloads",
            )
            integration.configure("client123")
            connected: list[str] = []
            integration.connected.connect(connected.append)
            cancel = integration._auth_cancel
            with patch.object(cancel, "wait", return_value=False):
                worker = threading.Thread(target=integration._run_device_authorization, args=((),))
                worker.start()
                try:
                    self.assertTrue(entered.wait(2))
                    integration.disconnect_account()
                finally:
                    release.set()
                    worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(connected, [])
            self.assertEqual(integration.connected_login, "")
            integration.shutdown()


if __name__ == "__main__":
    unittest.main()
