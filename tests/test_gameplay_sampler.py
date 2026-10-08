from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from analysis.audio_service import AnalysisCancellation, AnalysisCancelled
from analysis.gameplay.hearthstone.visual.sampler import (
    FFmpegFrameSampler, FrameSamplingError, VideoInfo,
)
from utils.ffmpeg import ensure_ffmpeg
from utils.subprocess_utils import popen_no_window, run_no_window


class GameplaySamplerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "a video è 🎮.mp4"
        self.source.write_bytes(b"fixture")

    def fake_process(self, script: str):
        started = threading.Event()
        processes = []

        def spawn(_command, **kwargs):
            process = popen_no_window(
                [getattr(sys, "_base_executable", sys.executable), "-u", "-c", script], **kwargs,
            )
            processes.append(process)
            started.set()
            return process

        return spawn, started, processes

    def sample_with_process(self, sampler, spawn, **options):
        return patch.multiple(
            "analysis.gameplay.hearthstone.visual.sampler",
            popen_no_window=spawn, ensure_ffmpeg=lambda: ("ffmpeg", "ffprobe"),
        )

    def assert_reaped(self, process) -> None:
        self.assertIsNotNone(process.poll())
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)
        self.assertFalse(any(t.name.endswith(f"-{process.pid}") and t.name.startswith("gameplay-")
                             for t in threading.enumerate()))

    def test_import_is_headless(self) -> None:
        result = run_no_window(
            [sys.executable, "-c", "import sys; import analysis.gameplay.hearthstone.visual.sampler; "
             "assert not any(n.startswith('PySide6') for n in sys.modules)"],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_pre_cancelled_token_never_spawns(self) -> None:
        token = AnalysisCancellation()
        token.cancel()
        with patch("analysis.gameplay.hearthstone.visual.sampler.popen_no_window") as spawn:
            with self.assertRaises(AnalysisCancelled):
                list(FFmpegFrameSampler().sample(self.source, fps=1, cancellation=token))
        spawn.assert_not_called()

    def test_cancel_interrupts_blocked_read_reaps_and_allows_fresh_token(self) -> None:
        sampler = FFmpegFrameSampler()
        token = AnalysisCancellation()
        spawn, started, processes = self.fake_process("import time; time.sleep(30)")
        failures = []

        def invoke():
            try:
                list(sampler.sample(self.source, fps=1, cancellation=token))
            except BaseException as exc:
                failures.append(exc)

        with self.sample_with_process(sampler, spawn), patch.object(sampler, "probe", return_value=VideoInfo(2, 2, 2)):
            thread = threading.Thread(target=invoke, daemon=True)
            thread.start()
            self.assertTrue(started.wait(3))
            before = time.monotonic()
            token.cancel()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - before, 3)
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], AnalysisCancelled)
        self.assert_reaped(processes[0])
        # A cancelled analysis must not poison the next independent request.
        spawn, _started, processes = self.fake_process(
            "import sys; sys.stderr.write('n: 0 pts: 0 pts_time:0\\n'); sys.stderr.flush(); "
            "sys.stdout.buffer.write(bytes(range(12))); sys.stdout.flush()"
        )
        with self.sample_with_process(sampler, spawn), patch.object(sampler, "probe", return_value=VideoInfo(2, 2, 2)):
            frames = list(sampler.sample(self.source, fps=1))
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].timestamp, 0)
        np.testing.assert_array_equal(frames[0].image.reshape(-1), np.arange(12))
        self.assert_reaped(processes[0])

    def test_early_generator_close_terminates_its_child(self) -> None:
        sampler = FFmpegFrameSampler()
        spawn, _started, processes = self.fake_process(
            "import sys,time; sys.stderr.write('n: 0 pts: 0 pts_time:0\\n'); sys.stderr.flush(); "
            "sys.stdout.buffer.write(bytes(range(12))); sys.stdout.flush(); time.sleep(30)"
        )
        with self.sample_with_process(sampler, spawn), patch.object(sampler, "probe", return_value=VideoInfo(2, 2, 2)):
            iterator = sampler.sample(self.source, fps=1)
            self.assertEqual(next(iterator).image.shape, (2, 2, 3))
            iterator.close()
        self.assert_reaped(processes[0])

    def test_stalled_decoder_times_out_and_is_reaped(self) -> None:
        sampler = FFmpegFrameSampler(stall_timeout_s=0.1)
        spawn, _started, processes = self.fake_process("import time; time.sleep(30)")
        with self.sample_with_process(sampler, spawn), patch.object(sampler, "probe", return_value=VideoInfo(2, 2, 2)):
            with self.assertRaises(FrameSamplingError) as caught:
                list(sampler.sample(self.source, fps=1))
        self.assertEqual(caught.exception.code, "sampling_timeout")
        self.assert_reaped(processes[0])

    def test_truncated_frame_is_not_accepted(self) -> None:
        sampler = FFmpegFrameSampler()
        spawn, _started, processes = self.fake_process("import sys; sys.stdout.buffer.write(b'12345')")
        with self.sample_with_process(sampler, spawn), patch.object(sampler, "probe", return_value=VideoInfo(2, 2, 2)):
            with self.assertRaisesRegex(FrameSamplingError, "truncated"):
                list(sampler.sample(self.source, fps=1))
        self.assert_reaped(processes[0])

    def test_probe_metadata_is_reused_but_changed_media_invalidates_it(self) -> None:
        sampler = FFmpegFrameSampler()
        completed = subprocess.CompletedProcess([], 0,
            '{"streams":[{"width":160,"height":90,"duration":"3","start_time":"0"}],'
            '"format":{"start_time":"0","duration":"3"}}', "")
        with patch("analysis.gameplay.hearthstone.visual.sampler.run_cmd", return_value=completed) as probe:
            first = sampler.probe(self.source)
            self.assertIs(sampler.probe(self.source), first)
            self.assertEqual(probe.call_count, 1)
            self.source.write_bytes(b"changed media with a different size")
            self.assertEqual(sampler.probe(self.source), first)
            self.assertEqual(probe.call_count, 2)
            stat = self.source.stat()
            os.utime(self.source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
            self.assertEqual(sampler.probe(self.source), first)
            self.assertEqual(probe.call_count, 3)
            cancelled = AnalysisCancellation()
            cancelled.cancel()
            with self.assertRaises(AnalysisCancelled):
                sampler.probe(self.source, cancellation=cancelled)
            self.assertEqual(probe.call_count, 3)

    def test_failed_probe_is_never_cached(self) -> None:
        sampler = FFmpegFrameSampler()
        with patch("analysis.gameplay.hearthstone.visual.sampler.run_cmd", return_value=subprocess.CompletedProcess([], 1, "", "bad media")) as probe:
            for _attempt in range(2):
                with self.assertRaises(FrameSamplingError):
                    sampler.probe(self.source)
            self.assertEqual(probe.call_count, 2)

    def test_missing_source_and_invalid_options(self) -> None:
        sampler = FFmpegFrameSampler()
        with self.assertRaises(FrameSamplingError) as caught:
            sampler.probe(self.root / "missing.mp4")
        self.assertEqual(caught.exception.code, "source_not_found")
        for options in ({"fps": 0}, {"fps": float("nan")}, {"fps": 1, "start_s": -1},
                        {"fps": 1, "end_s": 0}, {"fps": 1, "max_width": 0}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                list(sampler.sample(self.source, **options))

    def generate(self, *, nonzero_vfr: bool = False) -> None:
        ffmpeg, _ = ensure_ffmpeg()
        command = [ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-y", "-f", "lavfi", "-i",
                   "testsrc2=size=160x90:rate=10:duration=3"]
        if nonzero_vfr:
            command.extend(["-vf", "select='not(eq(n,3)+eq(n,4)+eq(n,11))',setpts=PTS+5/TB",
                            "-fps_mode", "vfr"])
        command.extend(["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(self.source)])
        result = run_no_window(command, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_real_ffmpeg_coarse_fine_sampling_uses_video_relative_timestamps(self) -> None:
        self.generate()
        sampler = FFmpegFrameSampler()
        info = sampler.probe(self.source)
        self.assertEqual((info.width, info.height), (160, 90))
        self.assertAlmostEqual(info.duration_s, 3.0, places=3)
        coarse = list(sampler.sample(self.source, fps=1, max_width=80))
        self.assertEqual([frame.timestamp for frame in coarse], [0.0, 1.0, 2.0])
        self.assertTrue(all(frame.image.shape == (45, 80, 3) for frame in coarse))
        self.assertTrue(all(frame.image.dtype == np.uint8 for frame in coarse))
        fine = list(sampler.sample(self.source, fps=5, start_s=0.45, end_s=1.05))
        np.testing.assert_allclose([frame.timestamp for frame in fine], [0.5, 0.7, 0.9], atol=1e-6)

    def test_video_start_after_audio_keeps_container_relative_timestamps(self) -> None:
        ffmpeg, _ = ensure_ffmpeg()
        result = run_no_window([
            ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-y", "-itsoffset", "2", "-f", "lavfi", "-i",
            "testsrc2=size=160x90:rate=10:duration=3", "-f", "lavfi", "-i",
            "sine=frequency=440:sample_rate=48000:duration=5", "-c:v", "libx264", "-preset", "ultrafast",
            "-fps_mode", "passthrough", "-c:a", "aac", str(self.source),
        ], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        sampler = FFmpegFrameSampler()
        self.assertAlmostEqual(sampler.probe(self.source).duration_s, 5.0, places=3)
        self.assertEqual([frame.timestamp for frame in sampler.sample(self.source, fps=1)], [2.0, 3.0, 4.0])

    def test_matroska_nonzero_pts_duration_does_not_include_origin(self) -> None:
        self.source = self.root / "offset.mkv"
        self.generate(nonzero_vfr=True)
        sampler = FFmpegFrameSampler()
        info = sampler.probe(self.source)
        self.assertAlmostEqual(info.start_time_s, 5.0, places=3)
        self.assertAlmostEqual(info.duration_s, 3.0, places=3)
        self.assertEqual([frame.timestamp for frame in sampler.sample(self.source, fps=1)], [0.0, 1.0, 2.0])

    def test_real_nonzero_pts_vfr_seek_never_invents_duplicate_frames(self) -> None:
        self.generate(nonzero_vfr=True)
        sampler = FFmpegFrameSampler()
        info = sampler.probe(self.source)
        self.assertAlmostEqual(info.start_time_s, 5.0, places=3)
        self.assertAlmostEqual(info.duration_s, 3.0, places=3)
        frames = list(sampler.sample(self.source, fps=10, start_s=0.25, end_s=0.95))
        # Source frames at .3/.4 were removed. fps synthesis would invent them;
        # these are actual decoded PTS relative to the video's start at 5s.
        np.testing.assert_allclose([frame.timestamp for frame in frames], [.5, .6, .7, .8, .9], atol=1e-6)

    def generate_rgb_roi_fixture(self) -> np.ndarray:
        """A lossless RGB fixture with a 100 ms marker only inside the ROI."""
        self.source = self.root / "lossless ROI è 🎮.mkv"
        frames = np.zeros((40, 90, 160, 3), dtype=np.uint8)
        frames[:, :, :, 0] = np.arange(160, dtype=np.uint8)[None, None, :]
        frames[:, :, :, 1] = np.arange(90, dtype=np.uint8)[None, :, None]
        frames[:, :, :, 2] = 20
        frames[9:13, 30:60, 40:80, 2] = 250
        ffmpeg, _ = ensure_ffmpeg()
        generated = run_no_window([
            ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-y",
            "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", "160x90", "-r", "40",
            "-i", "pipe:0", "-an", "-c:v", "ffv1", "-pix_fmt", "bgr0", str(self.source),
        ], input=frames.tobytes(), capture_output=True, timeout=30)
        self.assertEqual(generated.returncode, 0, generated.stderr)
        return frames

    def test_dense_roi_scan_observes_100ms_event_missed_by_coarse_sampling(self) -> None:
        original = self.generate_rgb_roi_fixture()
        sampler = FFmpegFrameSampler()
        coarse = list(sampler.sample(self.source, fps=1, max_width=160))
        self.assertEqual(len(coarse), 1)
        self.assertFalse(any(int(frame.image[:, :, 2].max()) > 200 for frame in coarse))
        frames = list(sampler.sample_roi(
            self.source, roi=(0.25, 1 / 3, 0.25, 1 / 3), reference_size=(160, 90), fps=20,
        ))
        np.testing.assert_allclose([frame.timestamp for frame in frames], np.r_[0, np.arange(0.025, 1, 0.05)], atol=1e-6)
        self.assertTrue(all(frame.image.shape == (30, 40, 3) for frame in frames))
        marked = [frame for frame in frames if int(frame.image[:, :, 2].min()) > 200]
        np.testing.assert_allclose([frame.timestamp for frame in marked], [0.225, 0.275], atol=1e-6)
        # No full-frame transfer or coordinate drift: each image is exactly the
        # expected source crop, and its timestamp identifies an actual frame.
        for frame in frames:
            index = round(frame.timestamp * 40)
            np.testing.assert_array_equal(frame.image, original[index, 30:60, 40:80])

    def test_roi_crop_precedes_scale_and_respects_reference_pixel_rounding(self) -> None:
        self.generate_rgb_roi_fixture()
        sampler = FFmpegFrameSampler()
        commands = []

        def spawn(command, **kwargs):
            commands.append(command)
            return popen_no_window(command, **kwargs)

        roi = (0.253, 0.211, 0.241, 0.337)
        with patch("analysis.gameplay.hearthstone.visual.sampler.popen_no_window", side_effect=spawn):
            frames = list(sampler.sample_roi(
                self.source, roi=roi, reference_size=(100, 70), fps=20, start_s=0.22, end_s=0.34,
            ))
        self.assertEqual(len(commands), 1)
        filters = commands[0][commands[0].index("-vf") + 1]
        self.assertIn("crop=40:32:40:18:exact=1,scale=25:25", filters)
        self.assertLess(filters.index("crop="), filters.index("scale="))
        self.assertTrue(all(frame.image.shape == (25, 25, 3) for frame in frames))
        np.testing.assert_allclose([frame.timestamp for frame in frames], [0.225, 0.275, 0.325], atol=1e-6)
        # Viewport composition can round differently in source/reference space;
        # the detector-provided dimensions take precedence when specified.
        custom = list(sampler.sample_roi(
            self.source, roi=roi, reference_size=(100, 70), output_size=(17, 13), fps=1,
        ))
        self.assertEqual(custom[0].image.shape, (13, 17, 3))

    def test_roi_sampling_preserves_real_nonzero_vfr_pts(self) -> None:
        self.generate(nonzero_vfr=True)
        sampler = FFmpegFrameSampler()
        frames = list(sampler.sample_roi(
            self.source, roi=(0.25, 0.2, 0.5, 0.6), reference_size=(160, 90),
            fps=20, start_s=0.25, end_s=0.95,
        ))
        np.testing.assert_allclose([frame.timestamp for frame in frames], [.5, .6, .7, .8, .9], atol=1e-6)
        self.assertTrue(all(frame.image.shape == (54, 80, 3) for frame in frames))

    def test_roi_options_are_rejected_before_probe_or_decode(self) -> None:
        sampler = FFmpegFrameSampler()
        invalid = [
            {"roi": (-0.1, 0, 0.5, 0.5)}, {"roi": (0, 0, 0, 0.5)},
            {"roi": (0.75, 0, 0.5, 0.5)}, {"roi": (0, 0, float("nan"), 0.5)},
            {"roi": (0, 0, True, 0.5)}, {"roi": (0, 0, 1)},
            {"roi": (0, 0, "0.5", 0.5)}, {"roi": (1, 0, 1e-13, 0.5)},
            {"reference_size": (0, 90)}, {"reference_size": (160.0, 90)},
            {"reference_size": (True, 90)}, {"reference_size": (160,)},
            {"output_size": (0, 30)}, {"output_size": (40, 4097)},
            {"output_size": (40, True)},
        ]
        with patch.object(sampler, "probe") as probe:
            for changed in invalid:
                options = {"roi": (0.25, 0.25, 0.5, 0.5), "reference_size": (160, 90), "fps": 20}
                options.update(changed)
                with self.subTest(options=options), self.assertRaises(ValueError):
                    list(sampler.sample_roi(self.source, **options))
        probe.assert_not_called()

    def test_roi_cancellation_reaps_blocked_decoder_and_allows_retry(self) -> None:
        sampler = FFmpegFrameSampler()
        token = AnalysisCancellation()
        spawn, started, processes = self.fake_process("import time; time.sleep(30)")
        failures = []
        options = {"roi": (0.25, 0.25, 0.5, 0.5), "reference_size": (4, 4), "fps": 20}

        def invoke():
            try:
                list(sampler.sample_roi(self.source, cancellation=token, **options))
            except BaseException as exc:
                failures.append(exc)

        with self.sample_with_process(sampler, spawn), patch.object(sampler, "probe", return_value=VideoInfo(2, 4, 4)):
            thread = threading.Thread(target=invoke, daemon=True)
            thread.start()
            self.assertTrue(started.wait(3))
            token.cancel()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], AnalysisCancelled)
        self.assert_reaped(processes[0])
        spawn, _started, processes = self.fake_process(
            "import sys; sys.stderr.write('n: 0 pts: 0 pts_time:0\\n'); sys.stderr.flush(); "
            "sys.stdout.buffer.write(bytes(range(12))); sys.stdout.flush()"
        )
        with self.sample_with_process(sampler, spawn), patch.object(sampler, "probe", return_value=VideoInfo(2, 4, 4)):
            frames = list(sampler.sample_roi(self.source, **options))
        self.assertEqual(frames[0].image.shape, (2, 2, 3))
        self.assert_reaped(processes[0])

    def test_dense_roi_20fps_does_not_degrade_to_15fps_on_30fps_source(self) -> None:
        ffmpeg, _ = ensure_ffmpeg()
        generated = run_no_window([
            ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-y", "-f", "lavfi", "-i",
            "testsrc2=size=160x90:rate=30:duration=1", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", str(self.source),
        ], capture_output=True, text=True, timeout=30)
        self.assertEqual(generated.returncode, 0, generated.stderr)
        frames = list(FFmpegFrameSampler().sample_roi(
            self.source, roi=(0.25, 0.2, 0.5, 0.6), reference_size=(160, 90), fps=20,
        ))
        self.assertEqual(len(frames), 20)
        actual = np.array([frame.timestamp for frame in frames])
        target = np.arange(20) / 20
        self.assertLess(float(np.max(np.abs(actual - target))), 1 / 30 + 1e-6)
        self.assertEqual(len(set(actual)), 20)

    def test_dense_roi_vfr_gap_does_not_create_catch_up_burst(self) -> None:
        ffmpeg, _ = ensure_ffmpeg()
        generated = run_no_window([
            ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-y", "-f", "lavfi", "-i",
            "testsrc2=size=160x90:rate=40:duration=2", "-vf", "select='lt(t,0.2)+gte(t,1.5)'",
            "-fps_mode", "vfr", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", str(self.source),
        ], capture_output=True, text=True, timeout=30)
        self.assertEqual(generated.returncode, 0, generated.stderr)
        frames = list(FFmpegFrameSampler().sample_roi(
            self.source, roi=(0.25, 0.2, 0.5, 0.6), reference_size=(160, 90), fps=20,
        ))
        expected = [0, .025, .075, .125, .175, 1.5, 1.525, 1.575, 1.625, 1.675, 1.725, 1.775, 1.825, 1.875, 1.925, 1.975]
        np.testing.assert_allclose([frame.timestamp for frame in frames], expected, atol=1e-6)

    def test_dense_roi_native60_keeps_millisecond_quantized_real_pts(self) -> None:
        self.source = self.root / "quantized 60fps.mkv"
        ffmpeg, ffprobe = ensure_ffmpeg()
        generated = run_no_window([
            ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-y", "-f", "lavfi", "-i",
            "testsrc2=size=160x90:rate=60:duration=1", "-c:v", "ffv1", str(self.source),
        ], capture_output=True, text=True, timeout=30)
        self.assertEqual(generated.returncode, 0, generated.stderr)
        inspected = run_no_window([
            ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
            "frame=best_effort_timestamp_time", "-of", "json", str(self.source),
        ], capture_output=True, text=True, timeout=30)
        self.assertEqual(inspected.returncode, 0, inspected.stderr)
        native_pts = [float(item["best_effort_timestamp_time"]) for item in json.loads(inspected.stdout)["frames"]]
        self.assertEqual(len(native_pts), 60)
        # Like the real VOD, Matroska quantizes frame PTS to milliseconds. A
        # floor bin with only floating epsilon would discard .033 after .017.
        self.assertIn(round(native_pts[2] - native_pts[1], 3), (0.016, 0.017))
        sampler = FFmpegFrameSampler()
        for fps in (60, 120):
            with self.subTest(fps=fps):
                frames = list(sampler.sample_roi(
                    self.source, roi=(0.25, 0.2, 0.5, 0.6), reference_size=(160, 90), fps=fps,
                ))
                self.assertEqual(len(frames), 60)
                np.testing.assert_allclose([frame.timestamp for frame in frames], native_pts, atol=1e-6)
                self.assertEqual(len({frame.timestamp for frame in frames}), 60)


if __name__ == "__main__":
    unittest.main()
