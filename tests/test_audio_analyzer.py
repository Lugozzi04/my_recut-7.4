from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from analysis.audio_analyzer import AnalyzeWorker
from analysis.cut_engine import Segment
from core.track_state import TrackState
from ui.main_window import MainWindow
from utils.ffmpeg import ensure_ffmpeg
from utils.subprocess_utils import run_no_window


class AudioAnalyzerTests(unittest.TestCase):
    def test_analysis_result_follows_track_after_reorder(self) -> None:
        first = TrackState(path="C:/video/first.mp4")
        second = TrackState(path="C:/video/second.mp4")

        class WindowState:
            _tracks = [second, first]
            _analysis_job_ids = {1: 42}
            _is_current_analysis_job = MainWindow._is_current_analysis_job
            _same_local_path = MainWindow._same_local_path

        resolved = MainWindow._resolve_analysis_target_index(
            WindowState(),
            original_track_idx=1,
            job_id=42,
            target=second,
            expected_path="C:/video/second.mp4",
        )

        self.assertEqual(resolved, 0)

    def test_old_analysis_thread_cannot_remove_new_job(self) -> None:
        old_thread = object()
        current_thread = object()
        target = TrackState(path="C:/video/current.mp4")

        class WindowState:
            _analysis_threads = {1: current_thread}
            _analysis_workers = {1: object()}
            _analysis_expected_path = {1: target.path}
            _analysis_job_ids = {1: 8}
            _analysis_targets = {1: target}
            _analysis_progress = {1: 50}
            _is_current_analysis_job = MainWindow._is_current_analysis_job

        state = WindowState()
        MainWindow._cleanup_analysis_job_refs(state, 1, 7, old_thread)
        self.assertEqual(state._analysis_job_ids, {1: 8})
        self.assertIs(state._analysis_threads[1], current_thread)

        MainWindow._cleanup_analysis_job_refs(state, 1, 8, current_thread)
        self.assertEqual(state._analysis_threads, {})
        self.assertEqual(state._analysis_job_ids, {})
        self.assertEqual(state._analysis_targets, {})

    def test_non_active_analysis_completion_applies_cuts_and_syncs_ui(self) -> None:
        active = TrackState(path="C:/video/active.mp4", duration=2.0, rms=np.ones(60, dtype=np.float32))
        completed = TrackState(path="C:/video/completed.mp4")
        events: list[tuple[str, dict]] = []
        ui_syncs: list[bool] = []

        class SeekState:
            def setRange(self, _start: int, _end: int) -> None:
                return None

            def value(self) -> int:
                return 0

        class WindowState:
            _tracks = [active, completed]
            _active_track_index = 0
            _analysis_expected_path = {}
            _pending_workspace_reset = False
            _workspace_resetting = False
            _global_duration = 5.0
            duration = 2.0
            seek = SeekState()
            _same_local_path = MainWindow._same_local_path

            @staticmethod
            def _app_log(event: str, **fields: object) -> None:
                events.append((event, fields))

            @staticmethod
            def _update_segment_rms_stats(track: TrackState) -> None:
                track.rms_min = 0.01
                track.rms_max = 0.1
                track.rms_eps = 0.0001

            @staticmethod
            def _sync_project_from_track(_track: TrackState) -> None:
                return None

            @staticmethod
            def _compute_cuts_for_track(_track_idx: int) -> None:
                completed.cuts = [Segment(0.0, 0.5)]
                completed.keeps = [Segment(0.5, 3.0)]

            @staticmethod
            def _refresh_timeline_tracks(reset_view: bool = False) -> None:
                return None

            @staticmethod
            def _update_time_label(_value: float) -> None:
                return None

            @staticmethod
            def _update_split_button_state() -> None:
                return None

            @staticmethod
            def _sync_analysis_ui_for_active_track() -> None:
                ui_syncs.append(True)

        MainWindow._on_analysis_done(
            WindowState(),
            track_idx=1,
            duration=3.0,
            rms_np=np.ones(100, dtype=np.float32),
            hop_s=0.03,
            auto_thr=0.02,
            expected_path="C:/video/completed.mp4",
        )

        self.assertEqual(len(completed.cuts), 1)
        self.assertEqual(ui_syncs, [True])
        self.assertTrue(any(event == "analysis_applied" for event, _fields in events))

    def test_real_mp4_analysis_finishes_and_reports_progress(self) -> None:
        ffmpeg, _ffprobe = ensure_ffmpeg()
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "analysis_source.mp4"
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
                    "testsrc2=size=160x90:rate=25:duration=3",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:sample_rate=48000:duration=3",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-shortest",
                    str(source),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)

            completed: list[tuple[float, int]] = []
            errors: list[str] = []
            progress: list[int] = []
            worker = AnalyzeWorker(str(source))
            worker.done.connect(lambda _idx, duration, rms, _hop, _thr: completed.append((duration, len(rms))))
            worker.error.connect(lambda _idx, message: errors.append(message))
            worker.progress.connect(lambda _idx, percentage: progress.append(percentage))

            worker.run()

            self.assertEqual(errors, [])
            self.assertEqual(len(completed), 1)
            self.assertAlmostEqual(completed[0][0], 3.0, delta=0.15)
            self.assertGreater(completed[0][1], 90)
            self.assertEqual(progress[0], 0)
            self.assertEqual(progress[-1], 100)
            self.assertEqual(progress, sorted(progress))

    def test_export_cache_warming_is_not_analysis(self) -> None:
        class WindowState:
            _analysis_threads: dict[int, object] = {}
            _ai_threads: dict[int, object] = {}
            _ai_processing = False

            @staticmethod
            def _cleanup_orphan_analysis_refs() -> None:
                return None

            @staticmethod
            def _warm_cache_in_progress() -> bool:
                return True

        self.assertFalse(MainWindow._analysis_in_progress(WindowState()))

    def test_export_cache_warming_skips_large_or_long_media(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.mp4"
            source.write_bytes(b"media")
            with patch.dict(
                os.environ,
                {
                    "AUTO_CUTTER_WARM_CACHE_MAX_DURATION_S": "900",
                    "AUTO_CUTTER_WARM_CACHE_MAX_BYTES": "1024",
                },
            ):
                self.assertTrue(MainWindow._should_warm_export_cache(str(source), 120.0))
                self.assertFalse(MainWindow._should_warm_export_cache(str(source), 901.0))
                source.write_bytes(b"x" * 1025)
                self.assertFalse(MainWindow._should_warm_export_cache(str(source), 120.0))


if __name__ == "__main__":
    unittest.main()
