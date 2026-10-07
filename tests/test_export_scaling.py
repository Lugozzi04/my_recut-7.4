from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from export.exporter import ExportWorker


class ExportScalingTests(unittest.TestCase):
    def _worker(
        self,
        *,
        method: str = "auto",
        input_paths: list[str] | None = None,
    ) -> ExportWorker:
        paths = input_paths or ["source.mp4"]
        return ExportWorker(
            ffmpeg_path="ffmpeg",
            input_path=paths[0],
            input_paths=paths,
            output_path="output.mp4",
            keeps=[],
            export_method=method,
            parallel_workers=2,
        )

    def test_auto_is_not_overridden_by_conservative_renderer(self) -> None:
        worker = self._worker(method="auto", input_paths=["a.mp4", "b.mp4", "c.mp4"])

        with patch.dict(os.environ, {"AUTO_CUTTER_EXPORT_MAX_CONSERVATIVE": "1"}):
            apply, reason = worker._max_conservative_should_apply(
                context="segments",
                item_count=226,
                total_output_s=2319.28,
                input_duration_s=7000.0,
                requested_method="auto",
            )

        self.assertFalse(apply)
        self.assertEqual(reason, "adaptive_respect_method_auto")

    def test_forced_conservative_renderer_still_overrides_auto(self) -> None:
        worker = self._worker(method="auto")

        with patch.dict(os.environ, {"AUTO_CUTTER_EXPORT_MAX_CONSERVATIVE": "force"}):
            apply, reason = worker._max_conservative_should_apply(
                context="segments",
                item_count=226,
                total_output_s=2319.28,
                input_duration_s=7000.0,
                requested_method="auto",
            )

        self.assertTrue(apply)
        self.assertEqual(reason, "env_force")

    def test_chunk_source_span_uses_media_time_not_output_timeline(self) -> None:
        worker = self._worker()
        segments = [
            {
                "start": 0.0,
                "end": 1.0,
                "duration": 1.0,
                "v_idx": 0,
                "v_in": 5.0,
                "v_out": 6.0,
            },
            {
                "start": 1000.0,
                "end": 1001.0,
                "duration": 1.0,
                "v_idx": 0,
                "v_in": 6.0,
                "v_out": 7.0,
            },
        ]

        chunks = worker._split_segment_dicts(
            segments,
            max_segments=120,
            max_chunk_seconds=2000.0,
            max_source_span=20.0,
        )

        self.assertEqual(chunks, [segments])

    def test_backward_source_jump_starts_a_new_chunk(self) -> None:
        worker = self._worker()
        first = {
            "start": 0.0,
            "end": 1.0,
            "duration": 1.0,
            "v_idx": 0,
            "v_in": 100.0,
            "v_out": 101.0,
        }
        second = {
            "start": 1.0,
            "end": 2.0,
            "duration": 1.0,
            "v_idx": 0,
            "v_in": 10.0,
            "v_out": 11.0,
        }

        chunks = worker._split_segment_dicts(
            [first, second],
            max_segments=120,
            max_chunk_seconds=2000.0,
            max_source_span=2000.0,
        )

        self.assertEqual(chunks, [[first], [second]])

    def test_filter_inputs_are_bounded_even_when_preseek_is_zero(self) -> None:
        worker = self._worker(input_paths=["zero.mp4", "late.mp4"])
        segments = [
            {
                "start": 0.0,
                "end": 5.0,
                "duration": 5.0,
                "v_idx": 0,
                "v_in": 0.0,
                "v_out": 5.0,
                "a_idx": 0,
                "a_in": 0.0,
                "a_out": 5.0,
            },
            {
                "start": 5.0,
                "end": 7.0,
                "duration": 2.0,
                "v_idx": 1,
                "v_in": 0.0,
                "v_out": 2.0,
                "a_idx": 1,
                "a_in": 0.0,
                "a_out": 2.0,
            },
        ]

        with tempfile.TemporaryDirectory() as tmp:
            temp_files: list[str] = []
            command, _total = worker._build_cmd_filter_segments(
                segments,
                temp_files,
                total=7.0,
                out_path=str(Path(tmp) / "chunk.mp4"),
                tmp_dir=tmp,
                input_preseek=worker._compute_segment_preseek(segments, preseek_pad=2.0),
            )

        zero_input = command.index("zero.mp4")
        late_input = command.index("late.mp4")
        self.assertEqual(command[zero_input - 3 : zero_input - 1], ["-t", "7.000"])
        self.assertEqual(
            command[late_input - 3 : late_input - 1],
            ["-t", "4.000"],
        )

    def test_windows_hwaccel_keeps_frames_in_system_memory(self) -> None:
        worker = self._worker()
        worker.use_hwaccel = True

        with patch("export.exporter.os.name", "nt"):
            args = worker._hwaccel_args_for_filtergraph()

        self.assertEqual(args, ["-hwaccel", "d3d11va"])
        self.assertNotIn("-hwaccel_output_format", args)

    def test_multitrack_filter_applies_hwaccel_to_every_input(self) -> None:
        worker = self._worker(input_paths=["zero.mp4", "one.mp4"])
        segments = [
            {
                "start": 0.0,
                "end": 1.0,
                "duration": 1.0,
                "v_idx": 0,
                "v_in": 0.0,
                "v_out": 1.0,
                "a_idx": 1,
                "a_in": 0.0,
                "a_out": 1.0,
            }
        ]

        with tempfile.TemporaryDirectory() as tmp:
            temp_files: list[str] = []
            with patch.object(
                worker,
                "_hwaccel_args_for_filtergraph",
                return_value=["-hwaccel", "d3d11va"],
            ):
                command, _total = worker._build_cmd_filter_segments(
                    segments,
                    temp_files,
                    total=1.0,
                    out_path=str(Path(tmp) / "chunk.mp4"),
                    tmp_dir=tmp,
                )

        self.assertEqual(command.count("-hwaccel"), 2)
        self.assertEqual(
            command[command.index("zero.mp4") - 3 : command.index("zero.mp4")],
            ["-hwaccel", "d3d11va", "-i"],
        )
        self.assertEqual(
            command[command.index("one.mp4") - 3 : command.index("one.mp4")],
            ["-hwaccel", "d3d11va", "-i"],
        )

    def test_hwaccel_failure_retries_once_in_software(self) -> None:
        worker = self._worker()
        worker.use_hwaccel = True
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], on_progress: object) -> tuple[int, str]:
            calls.append(list(cmd))
            return (1, "hardware failed") if len(calls) == 1 else (0, "")

        worker._run_ffmpeg_progress = fake_run  # type: ignore[method-assign]
        command = ["ffmpeg", "-hwaccel", "d3d11va", "-i", "source.mp4", "out.mp4"]

        rc, error = worker._run_ffmpeg_progress_with_hw_fallback(
            command,
            on_progress=lambda _seconds: None,
            context="unit_test",
        )

        self.assertEqual((rc, error), (0, ""))
        self.assertEqual(len(calls), 2)
        self.assertIn("-hwaccel", calls[0])
        self.assertNotIn("-hwaccel", calls[1])
        self.assertFalse(worker.use_hwaccel)

    def test_segment_cache_signature_ignores_unused_project_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            used = root / "used.mp4"
            unused_a = root / "unused_a.mp4"
            unused_b = root / "unused_b.mp4"
            for path, data in ((used, b"used"), (unused_a, b"a"), (unused_b, b"different")):
                path.write_bytes(data)
            segment = {
                "duration": 1.0,
                "v_idx": 0,
                "v_in": 0.0,
                "v_out": 1.0,
                "a_idx": 0,
                "a_in": 0.0,
                "a_out": 1.0,
            }
            first = self._worker(input_paths=[str(used), str(unused_a)])
            second = self._worker(input_paths=[str(used), str(unused_b)])

            first_key = first._chunk_signature_segments([segment], 25.0, "ts", False)
            second_key = second._chunk_signature_segments([segment], 25.0, "ts", False)

        self.assertEqual(first_key, second_key)

    def test_chunk_cache_persists_between_worker_instances(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_dir = root / "cache"
            rendered = root / "rendered.ts"
            rendered.write_bytes(b"cached media")
            with patch.dict(
                os.environ,
                {
                    "AUTO_CUTTER_CHUNK_CACHE": "1",
                    "AUTO_CUTTER_CHUNK_CACHE_DIR": str(cache_dir),
                },
            ):
                first = self._worker()
                stored = first._chunk_cache_store("stable-key", "ts", str(rendered))
                first._chunk_cache_flush()

                second = self._worker()
                hit = second._chunk_cache_lookup("stable-key", "ts")

            self.assertEqual(hit, stored)
            self.assertTrue(Path(stored).is_file())

    def test_adaptive_scheduler_decision_scales_both_directions(self) -> None:
        worker = self._worker()

        faster, fast_reason = worker._adaptive_worker_decision(
            3,
            5,
            [2.0, 2.5, 3.0],
            10,
            available_ram_gb=16.0,
            storage_tier="ssd",
        )
        slower, slow_reason = worker._adaptive_worker_decision(
            3,
            5,
            [0.20, 0.30, 0.40],
            10,
            available_ram_gb=16.0,
            storage_tier="ssd",
        )
        low_ram, ram_reason = worker._adaptive_worker_decision(
            3,
            5,
            [3.0, 3.0],
            10,
            available_ram_gb=2.0,
            storage_tier="ssd",
        )

        self.assertEqual(faster, 4)
        self.assertIn("fast_chunks", fast_reason)
        self.assertEqual(slower, 2)
        self.assertIn("slow_chunks", slow_reason)
        self.assertEqual(low_ram, 2)
        self.assertEqual(ram_reason, "low_available_ram")

    def test_adaptive_scheduler_executes_every_job_and_reports_adjustment(self) -> None:
        worker = self._worker()
        worker._parallel_auto = True
        logs: list[str] = []
        completed: list[int] = []
        worker._log = logs.append
        jobs = [{"rep": idx} for idx in range(8)]

        # Scaling requires headroom; runner CPU/RAM limits must not decide
        # whether this test exercises the adjustment branch.
        with (
            patch.dict(os.environ, {"AUTO_CUTTER_ADAPTIVE_SCHEDULER": "1"}),
            patch.object(worker, "_thread_budget", return_value=8),
            patch.object(worker, "_available_ram_gb", return_value=16.0),
            patch.object(worker, "_storage_tier", return_value="ssd"),
        ):
            worker._execute_parallel_jobs(
                jobs,
                lambda job: {
                    "rep": int(job["rep"]),
                    "elapsed_seconds": 0.1,
                },
                initial_workers=2,
                media_seconds=lambda _job: 1.0,
                on_result=lambda result: completed.append(int(result["rep"])),
                label="test",
            )

        self.assertEqual(sorted(completed), list(range(8)))
        self.assertTrue(any("adaptive_scheduler_adjust label=test from=2 to=3" in line for line in logs))

    def test_adaptive_scheduler_at_cpu_cap_executes_every_job_without_adjustment(self) -> None:
        worker = self._worker()
        worker._parallel_auto = True
        logs: list[str] = []
        completed: list[int] = []
        worker._log = logs.append
        jobs = [{"rep": idx} for idx in range(8)]

        with (
            patch.dict(os.environ, {"AUTO_CUTTER_ADAPTIVE_SCHEDULER": "1"}),
            patch.object(worker, "_thread_budget", return_value=4),
            patch.object(worker, "_available_ram_gb", return_value=16.0),
            patch.object(worker, "_storage_tier", return_value="ssd"),
        ):
            worker._execute_parallel_jobs(
                jobs,
                lambda job: {
                    "rep": int(job["rep"]),
                    "elapsed_seconds": 0.1,
                },
                initial_workers=2,
                media_seconds=lambda _job: 1.0,
                on_result=lambda result: completed.append(int(result["rep"])),
                label="test",
            )

        self.assertEqual(sorted(completed), list(range(8)))
        self.assertTrue(any("enabled=yes initial=2 cap=2 jobs=8" in line for line in logs))
        self.assertFalse(any("adaptive_scheduler_adjust" in line for line in logs))


if __name__ == "__main__":
    unittest.main()
