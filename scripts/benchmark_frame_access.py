"""Bounded FFmpeg access benchmark; no detection, ASR or persistent frame cache."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import statistics
import sys
import time
from typing import Any, Iterator
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analysis.gameplay.hearthstone.visual import sampler as sampler_module
from analysis.gameplay.hearthstone.visual.sampler import FFmpegFrameSampler, SampledFrame
from analysis.shared.frames import FrameSamplingService
from utils.ffmpeg import ensure_ffmpeg
from utils.subprocess_utils import run_no_window


@contextmanager
def count_processes() -> Iterator[dict[str, Any]]:
    original = sampler_module.popen_no_window
    counts: dict[str, Any] = {"ffmpeg_processes": 0, "popen_return_seconds": 0.0}

    def spawn(*args: Any, **kwargs: Any) -> Any:
        start = time.perf_counter()
        process = original(*args, **kwargs)
        counts["popen_return_seconds"] += time.perf_counter() - start
        counts["ffmpeg_processes"] += 1
        return process

    with patch.object(sampler_module, "popen_no_window", side_effect=spawn):
        yield counts


def describe(frame: SampledFrame | None) -> dict[str, Any] | None:
    if frame is None:
        return None
    return {"pts_s": frame.timestamp, "shape": list(frame.image.shape),
            "pixel_sha256": hashlib.sha256(frame.image.tobytes()).hexdigest()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sampler = FFmpegFrameSampler()
    start = time.perf_counter()
    info = sampler.probe(args.video)
    probe_seconds = time.perf_counter() - start
    # Fixed, short intervals at separated positions; never scan the complete VOD.
    starts = [600.0, 1500.0, 2900.0]
    if info.duration_s < starts[-1] + 61:
        parser.error("This bounded benchmark expects a video longer than 2961 seconds.")
    single = FrameSamplingService(sampler, enabled=False, max_gap_s=0)
    default = FrameSamplingService(sampler, enabled=False)
    wide = FrameSamplingService(sampler, enabled=False, max_gap_s=31, max_span_s=61)
    ffmpeg, _ = ensure_ffmpeg()
    version = run_no_window([ffmpeg, "-version"], capture_output=True, text=True, timeout=10)
    report: dict[str, Any] = {
        "format": "recut_ffmpeg_frame_access_v1",
        "video": args.video.name, "size_bytes": args.video.stat().st_size,
        "duration_s": info.duration_s, "resolution": [info.width, info.height],
        "ffmpeg_version": version.stdout.splitlines()[0],
        "max_width": 960, "repeats": 2, "probe_seconds_excluded": probe_seconds,
        "cache": "application frame cache disabled; OS disk cache not flushed",
        "scope": "three 61-second windows only; no detector or ASR",
        "runs": [],
    }
    modes = ["sequential_1fps", "sparse_30s_seeks", "sparse_30s_forced_batch",
             "nearby_single_seeks", "nearby_default_batch"]
    references: dict[float, dict[str, Any] | None] = {}
    for repeat in range(2):
        for base in starts:
            sparse = [base, base + 30, base + 60]
            nearby = [base, base + 1, base + 2]
            for mode in modes if repeat == 0 else reversed(modes):
                times = nearby if mode.startswith("nearby") else sparse
                with count_processes() as counters:
                    started = time.perf_counter()
                    first_delivery: float | None = None
                    if mode == "sequential_1fps":
                        kept = {}
                        delivered = 0
                        for frame in sampler.sample(args.video, fps=1, start_s=base,
                                                    end_s=base + 61, max_width=960):
                            if first_delivery is None:
                                first_delivery = time.perf_counter() - started
                            delivered += 1
                            for t in sorted(set(sparse + nearby)):
                                if t not in kept and t - 1e-6 <= frame.timestamp < t + .1 + 1e-6:
                                    kept[t] = frame
                        returned = {t: kept.get(t) for t in sorted(set(sparse + nearby))}
                    else:
                        service = (wide if mode.endswith("forced_batch") else
                                   default if mode.endswith("default_batch") else single)
                        returned = service.get_many(args.video, times, max_width=960)
                        delivered = sum(f is not None for f in returned.values())
                    elapsed = time.perf_counter() - started
                frames = {str(t): describe(f) for t, f in returned.items()}
                row = {"repeat": repeat + 1, "window_start_s": base, "mode": mode,
                       "wall_seconds": elapsed, "first_frame_seconds": first_delivery,
                       "frames_returned": delivered, **counters, "requested_frames": frames}
                report["runs"].append(row)
                if mode == "sequential_1fps" and repeat == 0:
                    references.update({float(t): f for t, f in frames.items()})
                print(json.dumps({k: v for k, v in row.items() if k != "requested_frames"}), flush=True)
    for row in report["runs"]:
        row["same_pts_and_pixels_as_sequential"] = all(
            value is not None and value == references[float(t)]
            for t, value in row["requested_frames"].items())
    summary = {}
    for mode in modes:
        rows = [row for row in report["runs"] if row["mode"] == mode]
        summary[mode] = {
            "total_wall_seconds": sum(row["wall_seconds"] for row in rows),
            "median_window_seconds": statistics.median(row["wall_seconds"] for row in rows),
            "ffmpeg_processes": sum(row["ffmpeg_processes"] for row in rows),
            "frames_returned": sum(row["frames_returned"] for row in rows),
            "popen_return_seconds": sum(row["popen_return_seconds"] for row in rows),
            "same_pts_and_pixels_as_sequential": all(row["same_pts_and_pixels_as_sequential"] for row in rows),
        }
    report["summary"] = summary
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    main()
