from __future__ import annotations

import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from automation.downloader import (
    DownloadRequest,
    DownloadResult,
    FfmpegRangeDownloader,
    PipelineDownloadCancelled,
    PipelineDownloadError,
    PipelineDownloadQueue,
    PipelineDownloadService,
    ResolvedMedia,
    YtDlpMediaResolver,
)
from automation.manager import PipelineManager
from automation.models import PipelineState
from automation.store import PipelineStore
from utils.ffmpeg import ensure_ffmpeg, ffprobe_duration_seconds
from utils.subprocess_utils import run_no_window


class FakeResolver:
    def __init__(self, media_url: str) -> None:
        self.media_url = media_url
        self.calls = 0

    def resolve(self, _url: str) -> ResolvedMedia:
        self.calls += 1
        return ResolvedMedia(self.media_url, {"User-Agent": "Auto Cutter Test"})


class SuccessfulDownloader:
    def download(self, request, *, cancellation=None, on_progress=None):
        _ = cancellation
        request.output_dir.mkdir(parents=True, exist_ok=True)
        output = request.output_dir / "downloaded.mp4"
        output.write_bytes(b"downloaded")
        if on_progress is not None:
            on_progress(25)
            on_progress(100)
        return DownloadResult(output, request.duration_s)


class FailingDownloader:
    def download(self, _request, *, cancellation=None, on_progress=None):
        _ = (cancellation, on_progress)
        raise PipelineDownloadError("network_failed", "temporary network failure")


class BlockingDownloader:
    started = threading.Event()

    def download(self, _request, *, cancellation=None, on_progress=None):
        _ = on_progress
        self.started.set()
        assert cancellation is not None
        while not cancellation.cancelled:
            time.sleep(0.01)
        raise PipelineDownloadCancelled()


class PipelineDownloaderTests(unittest.TestCase):
    def _manager_with_download_job(self, root: Path) -> tuple[PipelineManager, str]:
        manager = PipelineManager(PipelineStore(root / "jobs.json"))
        job, _created = manager.discover_vod(
            vod_id="12345",
            vod_url="https://www.twitch.tv/videos/12345",
            source_title="Test VOD",
        )
        manager.request_range(job.id)
        manager.select_range(job.id, 1.0, 3.0)
        return manager, job.id

    def test_request_rejects_non_twitch_and_invalid_ranges(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(PipelineDownloadError):
                DownloadRequest("job", "1", "https://example.com/videos/1", 0, 1, Path(tmp)).validate()
            with self.assertRaises(PipelineDownloadError):
                DownloadRequest(
                    "job",
                    "1",
                    "https://www.twitch.tv/videos/1",
                    2,
                    1,
                    Path(tmp),
                ).validate()

    def test_yt_dlp_resolver_returns_stream_without_downloading(self) -> None:
        seen_options: list[dict[str, object]] = []

        class FakeYoutubeDl:
            def __init__(self, options):
                seen_options.append(options)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def extract_info(self, url, *, download):
                self.request = (url, download)
                return {
                    "url": "https://cdn.example/video.m3u8?token=secret",
                    "http_headers": {"User-Agent": "test-agent"},
                    "title": "Resolved title",
                }

        module = SimpleNamespace(YoutubeDL=FakeYoutubeDl)
        resolver = YtDlpMediaResolver(lambda: module)

        media = resolver.resolve("https://www.twitch.tv/videos/12345")

        self.assertEqual(media.url, "https://cdn.example/video.m3u8?token=secret")
        self.assertEqual(media.headers["User-Agent"], "test-agent")
        self.assertEqual(media.title, "Resolved title")
        self.assertEqual(seen_options[0]["format"], "best")
        self.assertTrue(seen_options[0]["noplaylist"])

    def test_resolver_errors_redact_signed_media_urls(self) -> None:
        class FailingYoutubeDl:
            def __init__(self, _options):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def extract_info(self, _url, *, download):
                _ = download
                raise RuntimeError("request failed https://cdn.example/vod.m3u8?token=super-secret")

        resolver = YtDlpMediaResolver(
            lambda: SimpleNamespace(YoutubeDL=FailingYoutubeDl)
        )

        with self.assertRaises(PipelineDownloadError) as caught:
            resolver.resolve("https://www.twitch.tv/videos/12345")

        self.assertNotIn("super-secret", str(caught.exception))
        self.assertNotIn("cdn.example", str(caught.exception))

    def test_real_ffmpeg_downloads_only_selected_range_and_reuses_cache(self) -> None:
        ffmpeg, _ffprobe = ensure_ffmpeg()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.mp4"
            generated = run_no_window(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-v",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=160x90:rate=25:duration=4",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:sample_rate=48000:duration=4",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-g",
                    "25",
                    "-keyint_min",
                    "25",
                    "-sc_threshold",
                    "0",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-movflags",
                    "+faststart",
                    "-shortest",
                    str(source),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)
            resolver = FakeResolver(str(source))
            downloader = FfmpegRangeDownloader(resolver, ffmpeg_path=ffmpeg)
            request = DownloadRequest(
                "job-1",
                "12345",
                "https://www.twitch.tv/videos/12345",
                1.0,
                3.0,
                root / "downloads",
            )
            progress: list[int] = []

            result = downloader.download(request, on_progress=progress.append)

            self.assertTrue(result.path.is_file())
            self.assertAlmostEqual(ffprobe_duration_seconds(str(result.path)), 2.0, delta=0.6)
            self.assertEqual(progress[0], 0)
            self.assertEqual(progress[-1], 100)
            self.assertEqual(progress, sorted(progress))
            cached = downloader.download(request)
            self.assertTrue(cached.reused)
            self.assertEqual(resolver.calls, 1)

    def test_service_marks_successful_download_ready_for_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, job_id = self._manager_with_download_job(root)
            progress: list[int] = []
            service = PipelineDownloadService(
                manager,
                root / "downloads",
                downloader_factory=SuccessfulDownloader,
            )

            completed = service.execute(job_id, on_progress=progress.append)

            self.assertEqual(completed.state, PipelineState.ANALYZING)
            self.assertTrue(Path(completed.local_source_path or "").is_file())
            self.assertEqual(progress, [25, 100])

    def test_service_failure_is_persisted_as_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, job_id = self._manager_with_download_job(root)
            service = PipelineDownloadService(
                manager,
                root / "downloads",
                downloader_factory=FailingDownloader,
            )

            with self.assertRaises(PipelineDownloadError):
                service.execute(job_id)

            failed = manager.get(job_id)
            self.assertEqual(failed.state, PipelineState.FAILED)
            self.assertEqual(failed.retry_state, PipelineState.DOWNLOADING)
            self.assertEqual(failed.error_code, "network_failed")

    def test_queue_cancellation_is_retryable_and_does_not_leave_a_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, job_id = self._manager_with_download_job(root)
            failed_event = threading.Event()
            BlockingDownloader.started.clear()
            queue = PipelineDownloadQueue(
                manager,
                root / "downloads",
                downloader_factory=BlockingDownloader,
                on_failed=lambda _job, _message: failed_event.set(),
            )

            self.assertTrue(queue.enqueue(job_id))
            self.assertTrue(BlockingDownloader.started.wait(1.0))
            self.assertTrue(queue.cancel_current())
            self.assertTrue(failed_event.wait(2.0))
            self.assertTrue(queue.shutdown(timeout=2.0))

            failed = manager.get(job_id)
            self.assertEqual(failed.state, PipelineState.FAILED)
            self.assertEqual(failed.retry_state, PipelineState.DOWNLOADING)
            self.assertEqual(failed.error_code, "download_cancelled")


if __name__ == "__main__":
    unittest.main()
