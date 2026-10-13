"""Bounded offline A/B benchmark. Only a <=180s clip is analyzed; no ASR/agents/export."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path
import sys
import tempfile
import time
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analysis.audio_service import AnalysisCancellation
from analysis.gameplay.cache import atomic_write_json
from analysis.gameplay.hearthstone.adaptive import AdaptiveHearthstoneAnalyzer
from analysis.gameplay.hearthstone.adaptive_policy import AdaptiveSettings
from analysis.gameplay.hearthstone.adaptive_compare import compare_analyses
from analysis.gameplay.hearthstone.state_aware import StateAwareHearthstoneAnalyzer
from analysis.gameplay_editing import default_detector_assets
from analysis.performance_profile import GameplayProfiler
from utils.ffmpeg import ensure_ffmpeg
from utils.subprocess_utils import run_no_window


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--start", type=float, required=True)
    parser.add_argument("--duration", type=float, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--steps", type=int, nargs="+", choices=(5,10,20,30), default=[5,10,20,30])
    args = parser.parse_args()
    if not 0 <= args.start < float("inf") or not 1 <= args.duration <= 180:
        parser.error("Require finite start >= 0 and duration 1..180 seconds")
    ffmpeg, _ = ensure_ffmpeg()
    assets = default_detector_assets("hearthstone")
    report: dict[str, Any] = {"source_name": args.source.name, "source_start_s": args.start,
              "requested_duration_s": args.duration, "scope": "bounded clip; no full-VOD accuracy/speed claim",
              "cache": "detector disabled, no ASR", "runs": []}
    with tempfile.TemporaryDirectory(prefix="recut-adaptive-") as folder:
        clip = Path(folder) / "clip.mp4"
        extracted = run_no_window([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
            "-ss", str(args.start), "-i", str(args.source), "-t", str(args.duration), "-map", "0:v:0",
            "-an", "-c", "copy", "-avoid_negative_ts", "make_zero", "-y", str(clip)], capture_output=True, timeout=120)
        if extracted.returncode:
            raise RuntimeError("Clip extraction failed")
        options: dict[str, Any] = {"state_profile": assets["state_profile"], "template_pack": assets["template_pack"], "use_cache": False}
        reference = None
        for step in (None, *args.steps):
            detector = (StateAwareHearthstoneAnalyzer(**options) if step is None else
                AdaptiveHearthstoneAnalyzer(**options, adaptive_settings=AdaptiveSettings(coarse_step_s=step)))
            profiler = GameplayProfiler(str(clip), save=False)
            started = time.perf_counter()
            with profiler.activate():
                result = detector.analyze(clip, cancellation=AnalysisCancellation())
            elapsed = time.perf_counter() - started
            phases = {k: {"wall_seconds": v["wall_seconds"], "counters": v["counters"]}
                      for k, v in profiler.phases.items() if v["calls"] or v["counters"]}
            row = {"backend": "traditional" if step is None else "adaptive", "coarse_step_s": step,
                   "wall_seconds": elapsed, "monitor_seconds": profiler.phases["monitor"]["wall_seconds"],
                   "events": [{"type": e.type.value, "timestamp": e.timestamp} for e in result.events],
                   "games": [{"start": g.start, "end": g.end, "result": g.result.value} for g in result.games],
                   "phases": phases, "adaptive": result.metadata.get("adaptive")}
            if reference is None:
                reference = result
            else:
                row["comparison"] = compare_analyses(reference, result)
                row["scan_change_percent"] = 100 * (row["monitor_seconds"] / report["runs"][0]["monitor_seconds"] - 1)
            report["runs"].append(row)
            atomic_write_json(args.output, report)
            print(f"{row['backend']} step={step}: {elapsed:.3f}s, {len(result.events)} events", flush=True)


if __name__ == "__main__":
    main()
