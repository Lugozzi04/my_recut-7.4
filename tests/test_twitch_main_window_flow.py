from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from automation.manager import PipelineManager
from automation.models import PipelineState
from automation.store import PipelineStore
from ui.main_window import MainWindow


class TwitchMainWindowFlowTests(unittest.TestCase):
    def _manager_with_vod(self, root: Path) -> tuple[PipelineManager, str]:
        manager = PipelineManager(PipelineStore(root / "jobs.json"))
        job, created = manager.discover_vod(
            vod_id="vod-1",
            vod_url="https://www.twitch.tv/videos/1",
            source_title="Test stream",
            metadata={"source": "twitch", "twitch": {"duration_s": 600}},
        )
        self.assertTrue(created)
        return manager, job.id

    @staticmethod
    def _window_stub(manager: PipelineManager):
        return SimpleNamespace(
            _twitch_integration=SimpleNamespace(
                manager=manager,
                queue_download=Mock(return_value=True),
                queue_analysis=Mock(return_value=True),
                queue_export=Mock(return_value=True),
            ),
            _twitch_download_dir=Path("downloads"),
            _app_log=Mock(),
        )

    def test_range_selection_moves_discovered_vod_to_download_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager, job_id = self._manager_with_vod(Path(tmp))
            window = self._window_stub(manager)

            with (
                patch("ui.main_window.pro_get_int", side_effect=[(30, True), (300, True)]),
                patch("ui.main_window.QMessageBox.information"),
                patch("ui.main_window.QMessageBox.warning") as warning,
            ):
                MainWindow._configure_twitch_job(window, job_id)

            queued = manager.get(job_id)
            self.assertEqual(queued.state, PipelineState.DOWNLOADING)
            self.assertEqual(queued.range_start_s, 30.0)
            self.assertEqual(queued.range_end_s, 300.0)
            self.assertEqual(queued.metadata["preset"]["name"], "Balanced (Default)")
            self.assertEqual(queued.metadata["preset"]["config"]["threshold_pct"], 6)
            self.assertIn("export_settings", queued.metadata)
            window._twitch_integration.queue_download.assert_called_once_with(job_id)
            warning.assert_not_called()

    def test_cancelled_range_dialog_keeps_job_waiting_for_user(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager, job_id = self._manager_with_vod(Path(tmp))
            window = self._window_stub(manager)

            with patch("ui.main_window.pro_get_int", return_value=(0, False)):
                MainWindow._configure_twitch_job(window, job_id)

            waiting = manager.get(job_id)
            self.assertEqual(waiting.state, PipelineState.WAITING_RANGE)
            self.assertIsNone(waiting.range_start_s)

    def test_pending_list_excludes_jobs_already_queued_for_download(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager, job_id = self._manager_with_vod(Path(tmp))
            window = self._window_stub(manager)

            pending = MainWindow._pending_twitch_jobs(window)
            self.assertEqual([job.id for job in pending], [job_id])

            manager.request_range(job_id)
            manager.select_range(job_id, 0, 120)
            self.assertEqual(MainWindow._pending_twitch_jobs(window), [])

    def test_failed_download_is_exposed_for_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager, job_id = self._manager_with_vod(Path(tmp))
            manager.request_range(job_id)
            manager.select_range(job_id, 0, 120)
            manager.fail(job_id, "network_failed", "temporary failure")
            window = self._window_stub(manager)

            failed = MainWindow._failed_twitch_download_jobs(window)

            self.assertEqual([job.id for job in failed], [job_id])
            self.assertEqual(failed[0].retry_state, PipelineState.DOWNLOADING)

    def test_failed_analysis_is_exposed_for_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, job_id = self._manager_with_vod(root)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            manager.request_range(job_id)
            manager.select_range(job_id, 0, 120)
            manager.mark_downloaded(job_id, source)
            manager.fail(job_id, "analysis_failed", "temporary failure")
            window = self._window_stub(manager)

            failed = MainWindow._failed_twitch_analysis_jobs(window)

            self.assertEqual([job.id for job in failed], [job_id])
            self.assertEqual(failed[0].retry_state, PipelineState.ANALYZING)

    def test_ready_automatic_project_is_exposed_for_opening(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, job_id = self._manager_with_vod(root)
            source = root / "source.mp4"
            project = root / "source.autocutter"
            source.write_bytes(b"source")
            project.write_text("{}", encoding="utf-8")
            manager.request_range(job_id)
            manager.select_range(job_id, 0, 120)
            manager.mark_downloaded(job_id, source)
            manager.mark_analyzed(job_id, project)
            window = self._window_stub(manager)

            ready = MainWindow._ready_twitch_project_jobs(window)

            self.assertEqual([job.id for job in ready], [job_id])

    def test_failed_export_is_exposed_for_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, job_id = self._manager_with_vod(root)
            source = root / "source.mp4"
            project = root / "source.autocutter"
            source.write_bytes(b"source")
            project.write_text("{}", encoding="utf-8")
            manager.request_range(job_id)
            manager.select_range(job_id, 0, 120)
            manager.mark_downloaded(job_id, source)
            manager.mark_analyzed(job_id, project)
            manager.start_export(job_id)
            manager.fail(job_id, "render_failed", "temporary failure")
            window = self._window_stub(manager)

            failed = MainWindow._failed_twitch_export_jobs(window)

            self.assertEqual([job.id for job in failed], [job_id])
            self.assertEqual(failed[0].retry_state, PipelineState.EXPORTING)

    def test_ready_upload_is_exposed_for_opening(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, job_id = self._manager_with_vod(root)
            source = root / "source.mp4"
            project = root / "source.autocutter"
            output = root / "source.youtube-ready.mp4"
            source.write_bytes(b"source")
            project.write_text("{}", encoding="utf-8")
            output.write_bytes(b"output")
            manager.request_range(job_id)
            manager.select_range(job_id, 0, 120)
            manager.mark_downloaded(job_id, source)
            manager.mark_analyzed(job_id, project)
            manager.start_export(job_id)
            manager.mark_exported(job_id, output)
            window = self._window_stub(manager)

            ready = MainWindow._ready_twitch_upload_jobs(window)

            self.assertEqual([job.id for job in ready], [job_id])

    def test_device_code_is_copied_and_activation_page_is_opened(self) -> None:
        window = SimpleNamespace()
        clipboard = Mock()

        with (
            patch("ui.main_window.QApplication.clipboard", return_value=clipboard),
            patch("ui.main_window.QDesktopServices.openUrl", return_value=True) as open_url,
            patch("ui.main_window.QMessageBox.information") as information,
        ):
            MainWindow._on_twitch_device_code(
                window,
                "ABCD1234",
                "https://www.twitch.tv/activate",
                300,
            )

        clipboard.setText.assert_called_once_with("ABCD1234")
        self.assertEqual(open_url.call_args.args[0].toString(), "https://www.twitch.tv/activate")
        information.assert_called_once()


if __name__ == "__main__":
    unittest.main()
