from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

from automation.manager import PipelineManager
from automation.models import PipelineJob
from automation.twitch import TwitchApiClient, TwitchVideo
from automation.twitch_auth import TwitchSession


DiscoveredCallback = Callable[[PipelineJob, TwitchVideo], None]
ErrorCallback = Callable[[Exception], None]


@dataclass(frozen=True)
class VodPollResult:
    user_id: str
    videos_checked: int
    new_jobs: tuple[PipelineJob, ...]


class TwitchVodWatcher:
    """Optional polling service. Construction alone never starts network activity."""

    def __init__(
        self,
        *,
        client: TwitchApiClient,
        session: TwitchSession,
        manager: PipelineManager,
        poll_interval_s: float = 120.0,
        max_results: int = 1,
        max_backoff_s: float = 900.0,
        on_discovered: DiscoveredCallback | None = None,
        on_error: ErrorCallback | None = None,
    ) -> None:
        if poll_interval_s <= 0.0:
            raise ValueError("Twitch polling interval must be greater than zero.")
        self.client = client
        self.session = session
        self.manager = manager
        self.poll_interval_s = float(poll_interval_s)
        self.max_results = max(1, min(100, int(max_results)))
        self.max_backoff_s = max(self.poll_interval_s, float(max_backoff_s))
        self.on_discovered = on_discovered
        self.on_error = on_error
        self._stop_event = threading.Event()
        self._thread_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        with self._thread_lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop_event.clear()
            thread = threading.Thread(target=self._run, name="twitch-vod-watcher", daemon=True)
            self._thread = thread
            thread.start()
            return True

    def stop(self, timeout: float = 5.0) -> bool:
        self._stop_event.set()
        with self._thread_lock:
            thread = self._thread
        if thread is None:
            return True
        if thread is not threading.current_thread():
            thread.join(timeout=max(0.0, float(timeout)))
        stopped = not thread.is_alive()
        if stopped:
            with self._thread_lock:
                if self._thread is thread:
                    self._thread = None
        return stopped

    def poll_once(self) -> VodPollResult:
        token, identity = self.session.validated_context()
        videos = self.client.get_archived_videos(
            token.access_token,
            identity.user_id,
            first=self.max_results,
        )
        new_jobs: list[PipelineJob] = []

        # Helix returns newest first. Persist oldest first if more than one VOD
        # appeared between polls, keeping queue order chronological.
        for video in reversed(videos):
            if video.user_id != identity.user_id:
                continue
            job, created = self.manager.discover_vod(
                vod_id=video.id,
                vod_url=video.url,
                channel_id=identity.user_id,
                source_title=video.title,
                metadata={
                    "source": "twitch",
                    "twitch": {
                        "channel_login": identity.login,
                        "user_name": video.user_name,
                        "published_at": video.published_at,
                        "created_at": video.created_at,
                        "duration_s": video.duration_s,
                        "duration_text": video.duration_text,
                        "thumbnail_url": video.thumbnail_url,
                        "language": video.language,
                        "view_count": video.view_count,
                    },
                },
            )
            if not created:
                continue
            new_jobs.append(job)
            if self.on_discovered is not None:
                try:
                    self.on_discovered(job, video)
                except Exception as exc:
                    self._report_error(exc)

        return VodPollResult(
            user_id=identity.user_id,
            videos_checked=len(videos),
            new_jobs=tuple(new_jobs),
        )

    def _run(self) -> None:
        consecutive_failures = 0
        try:
            while not self._stop_event.is_set():
                try:
                    self.poll_once()
                    consecutive_failures = 0
                    delay = self.poll_interval_s
                except Exception as exc:
                    consecutive_failures += 1
                    self._report_error(exc)
                    delay = min(
                        self.max_backoff_s,
                        self.poll_interval_s * (2 ** min(consecutive_failures, 6)),
                    )
                self._stop_event.wait(delay)
        finally:
            with self._thread_lock:
                if self._thread is threading.current_thread():
                    self._thread = None

    def _report_error(self, error: Exception) -> None:
        if self.on_error is None:
            return
        try:
            self.on_error(error)
        except Exception:
            pass
