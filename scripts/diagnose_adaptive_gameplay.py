"""Offline video-only diagnostic. No ASR, agents, grouping, export or cache writes."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path
import sys
import time
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analysis.gameplay.cache import atomic_write_json
from analysis.gameplay.hearthstone.adaptive import AdaptiveHearthstoneAnalyzer
from analysis.gameplay.hearthstone.adaptive_compare import compare_analyses
from analysis.gameplay.hearthstone.adaptive_policy import AdaptivePlanner, AdaptiveSettings, RoutingObservation
from analysis.gameplay.hearthstone.fingerprint import FingerprintExtractor, continuity
from analysis.gameplay.hearthstone.state_aware import StateAwareHearthstoneAnalyzer
from analysis.gameplay.models import GameEvent, GameSegment, GameplayAnalysisResult
from analysis.gameplay_editing import default_detector_assets
from analysis.performance_profile import GameplayProfiler


class DiagnosticReference(StateAwareHearthstoneAnalyzer):
    """Collect sparse descriptors from frames already delivered to the reference."""
    def _scan_monitor(self, *args: Any, **kwargs: Any) -> Any:
        extractor = FingerprintExtractor(self.profile)
        fingerprints = []
        original = self.board.score
        sample_index = 0

        def score(gray: Any) -> Any:
            nonlocal sample_index
            result = original(gray)
            # Actual timestamps are assigned from the returned monitor rows below.
            if sample_index % 5 == 0:
                fingerprints.append((sample_index, extractor.extract(gray, 0, result[0])))
            sample_index += 1
            return result

        setattr(self.board, "score", score)
        try:
            output = super()._scan_monitor(*args, **kwargs)
        finally:
            setattr(self.board, "score", original)
        candidates, rows, _, _ = output
        routing = []
        for index, fp in fingerprints:
            fp.timestamp = rows[index].timestamp
            routing.append(RoutingObservation(fp,
                any(abs(e.timestamp-fp.timestamp) < .001 for e in candidates),
                rows[index].result_score >= self.monitor_settings.weak_score
                and rows[index].result_margin >= self.monitor_settings.weak_margin))
        duration = args[4]
        planner = AdaptivePlanner(AdaptiveSettings())
        windows = planner.plan(routing, duration)
        traces = []
        for previous, current in zip(routing, routing[1:]):
            _, changes = continuity(previous.fingerprint, current.fingerprint)
            traces.append({"start": previous.fingerprint.timestamp, "end": current.fingerprint.timestamp,
                "board_before": previous.fingerprint.board_score, "board_after": current.fingerprint.board_score,
                "region_similarities": {key: float((previous.fingerprint.regions[key]*current.fingerprint.regions[key]).sum())
                    for key in previous.fingerprint.regions.keys() & current.fingerprint.regions.keys()},
                "changes": list(changes)})
        self.diagnostic_reference = {"scope": "policy replay on every fifth delivered traditional frame, not a timed sparse backend",
            "states": planner.states, "windows": [asdict(w) for w in windows], "pairs": traces,
            "reason_counts": dict(Counter(reason for row in planner.states for reason in row["reasons"]))}
        return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--backend", choices=("traditional", "adaptive"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--full-videoprova", action="store_true",
                        help="Explicitly authorize this full <=3800s videoprova benchmark only.")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output already exists; use a distinct report path")
    if not args.full_videoprova or args.source.name.lower() != "videoprova.mp4":
        parser.error("This runner requires --full-videoprova and videoprova.mp4")
    options: dict[str, Any] = dict(default_detector_assets("hearthstone"))
    options.pop("strategy", None)
    # The public factory returns exactly the two asset paths.
    options = {k: v for k, v in options.items() if k in ("state_profile", "template_pack")}
    detector = (DiagnosticReference(**options, use_cache=False) if args.backend == "traditional" else
        AdaptiveHearthstoneAnalyzer(**options, use_cache=False,
            adaptive_settings=AdaptiveSettings()))
    info = detector.sampler.probe(args.source)
    if not 0 < info.duration_s <= 3800:
        parser.error("Only the one-hour reference VOD is authorized")
    profiler = GameplayProfiler(str(args.source), save=False)
    started = time.perf_counter()
    with profiler.activate():
        result = detector.analyze(args.source, on_progress=lambda p: print(f"{args.backend}: {p}%", flush=True))
    elapsed = time.perf_counter()-started
    payload = result.to_mapping()
    payload["video_path"] = args.source.name
    files = ("adaptive.py","adaptive_policy.py","fingerprint.py","state_aware.py")
    implementation = {name:hashlib.sha256((Path(__file__).resolve().parents[1]/"analysis/gameplay/hearthstone"/name).read_bytes()).hexdigest() for name in files}
    report = {"implementation_sha256":implementation,"source_name": args.source.name, "duration_s": result.duration, "backend": args.backend,
        "cache": "detection disabled; no ASR, agents or packaging", "wall_seconds": elapsed,
        "phases": profiler.phases, "result": payload}
    if isinstance(detector,DiagnosticReference):
        report["diagnostic_reference"] = detector.diagnostic_reference
    if args.reference:
        raw = json.loads(args.reference.read_text(encoding="utf-8"))["result"]
        raw["video_path"] = result.video_path
        reference = GameplayAnalysisResult.from_mapping(raw)
        report["comparison"] = compare_analyses(reference, result)
        own = result.metadata.get("adaptive", {}).get("autonomous", {})
        if own.get("available"):
            autonomous = GameplayAnalysisResult(result.video_path,result.duration,
                [GameEvent.from_mapping(e) for e in own["events"]],
                [GameSegment.from_mapping(g) for g in own["games"]],"autonomous-diagnostic",
                metadata={"result_windows":result.metadata["adaptive"]["autonomous_result_windows"],
                          "adaptive":{"vs_candidate_windows":result.metadata["adaptive"]["autonomous_vs_windows"]}})
            report["autonomous_comparison"] = compare_analyses(reference,autonomous)
            report["fallback_only_events"] = [e for e in report["autonomous_comparison"]["events"] if e["status"] == "missing"
                and any(r["status"] == "matched" and r["type"] == e["type"] and r["reference_timestamp"] == e["reference_timestamp"]
                    for r in report["comparison"]["events"])]
            states = result.metadata["adaptive"].get("states",[])
            missing = []
            for event in report["autonomous_comparison"]["events"]:
                if event["status"] != "missing":
                    continue
                t = event["reference_timestamp"]
                before = next((r for r in reversed(states) if r["timestamp"] <= t),None)
                after = next((r for r in states if r["timestamp"] > t),None)
                missing.append({**event,"coarse_before":before,"coarse_after":after,
                    "recovery_intervals":[w for w in result.metadata["adaptive"]["recovery_intervals"] if w["start"] <= t < w["end"]]})
            report["autonomous_missing_diagnosis"] = missing
    atomic_write_json(args.output, report)
    print(f"FINISHED {elapsed:.3f}s {len(result.events)} events", flush=True)


if __name__ == "__main__":
    main()
