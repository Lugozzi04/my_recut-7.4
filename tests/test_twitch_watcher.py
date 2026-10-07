from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from automation.manager import PipelineManager
from automation.models import PipelineState
from automation.store import PipelineStore
from automation.twitch import TwitchIdentity, TwitchNetworkError, TwitchToken, TwitchVideo
from automation.twitch_watcher import TwitchVodWatcher


class FakeSession:
    def __init__(self) -> None:
        self.calls = 0
        self.token = TwitchToken(
            access_token="access",
            refresh_token="refresh",
            expires_at=99999.0,
        )
        self.identity = TwitchIdentity(
            client_id="client-1",
            user_id="user-1",
            login="streamer",
            scopes=(),
            expires_in=3600,
        )

    def validated_context(self):
        self.calls += 1
        return self.token, self.identity


class FakeClient:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, int]] = []

    def get_archived_videos(self, access_token: str, user_id: str, *, first: int = 1):
        self.calls.append((access_token, user_id, first))
        if not self.responses:
            return []
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def make_video(video_id: str, published_at: str, *, user_id: str = "user-1") -> TwitchVideo:
    return TwitchVideo(
        id=video_id,
        user_id=user_id,
        user_login="streamer",
        user_name="Streamer",
        title=f"Live {video_id}",
        description="",
        created_at=published_at,
        published_at=published_at,
        url=f"https://www.twitch.tv/videos/{video_id}",
        thumbnail_url=f"https://example.invalid/{video_id}.jpg",
        duration_text="2h3m4s",
        duration_s=7384,
        language="it",
        view_count=10,
    )


class TwitchWatcherTests(unittest.TestCase):
    def make_manager(self, root: Path) -> PipelineManager:
        return PipelineManager(PipelineStore(root / "jobs.json"))

    def test_construction_is_inert_until_explicit_poll_or_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient([[make_video("1", "2026-09-28T10:00:00Z")]])
            session = FakeSession()
            TwitchVodWatcher(
                client=client,  # type: ignore[arg-type]
                session=session,  # type: ignore[arg-type]
                manager=self.make_manager(Path(tmp)),
            )

            self.assertEqual(client.calls, [])
            self.assertEqual(session.calls, 0)

    def test_new_vod_creates_discovered_job_and_notification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = make_video("987", "2026-09-28T10:00:00Z")
            client = FakeClient([[video]])
            session = FakeSession()
            notifications: list[str] = []
            watcher = TwitchVodWatcher(
                client=client,  # type: ignore[arg-type]
                session=session,  # type: ignore[arg-type]
                manager=self.make_manager(root),
                on_discovered=lambda job, _video: notifications.append(job.id),
            )

            result = watcher.poll_once()

            self.assertEqual(result.videos_checked, 1)
            self.assertEqual(len(result.new_jobs), 1)
            job = result.new_jobs[0]
            self.assertEqual(job.state, PipelineState.DISCOVERED)
            self.assertEqual(job.vod_id, "987")
            self.assertEqual(job.metadata["source"], "twitch")
            self.assertEqual(job.metadata["twitch"]["duration_s"], 7384)
            self.assertEqual(notifications, [job.id])
            self.assertEqual(client.calls, [("access", "user-1", 1)])

    def test_repeated_poll_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            video = make_video("987", "2026-09-28T10:00:00Z")
            client = FakeClient([[video], [video]])
            notifications: list[str] = []
            manager = self.make_manager(Path(tmp))
            watcher = TwitchVodWatcher(
                client=client,  # type: ignore[arg-type]
                session=FakeSession(),  # type: ignore[arg-type]
                manager=manager,
                on_discovered=lambda job, _video: notifications.append(job.id),
            )

            first = watcher.poll_once()
            second = watcher.poll_once()

            self.assertEqual(len(first.new_jobs), 1)
            self.assertEqual(second.new_jobs, ())
            self.assertEqual(len(manager.list_jobs()), 1)
            self.assertEqual(len(notifications), 1)

    def test_multiple_new_vods_are_queued_in_chronological_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            newest = make_video("new", "2026-09-28T12:00:00Z")
            oldest = make_video("old", "2026-09-28T10:00:00Z")
            manager = self.make_manager(Path(tmp))
            watcher = TwitchVodWatcher(
                client=FakeClient([[newest, oldest]]),  # type: ignore[arg-type]
                session=FakeSession(),  # type: ignore[arg-type]
                manager=manager,
                max_results=2,
            )

            result = watcher.poll_once()

            self.assertEqual([job.vod_id for job in result.new_jobs], ["old", "new"])
            self.assertEqual([job.vod_id for job in manager.list_jobs()], ["old", "new"])

    def test_background_watcher_recovers_from_transient_error_and_stops(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            discovered = threading.Event()
            errors: list[Exception] = []
            client = FakeClient(
                [
                    TwitchNetworkError("offline"),
                    [make_video("987", "2026-09-28T10:00:00Z")],
                ]
            )
            watcher = TwitchVodWatcher(
                client=client,  # type: ignore[arg-type]
                session=FakeSession(),  # type: ignore[arg-type]
                manager=self.make_manager(Path(tmp)),
                poll_interval_s=0.01,
                max_backoff_s=0.02,
                on_discovered=lambda _job, _video: discovered.set(),
                on_error=errors.append,
            )

            self.assertTrue(watcher.start())
            self.assertTrue(discovered.wait(timeout=2.0))
            self.assertTrue(watcher.stop(timeout=1.0))

            self.assertFalse(watcher.is_running)
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], TwitchNetworkError)

    def test_foreign_video_is_ignored_defensively(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = self.make_manager(Path(tmp))
            watcher = TwitchVodWatcher(
                client=FakeClient([[make_video("other", "2026-09-28T10:00:00Z", user_id="user-2")]]),
                session=FakeSession(),  # type: ignore[arg-type]
                manager=manager,
            )

            result = watcher.poll_once()

            self.assertEqual(result.new_jobs, ())
            self.assertEqual(manager.list_jobs(), [])


if __name__ == "__main__":
    unittest.main()
