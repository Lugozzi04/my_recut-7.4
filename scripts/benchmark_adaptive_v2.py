"""One explicitly authorized sparse V2 run on videoprova; no ASR or providers."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
from typing import Any

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from analysis.gameplay.cache import atomic_write_json, video_identity
from analysis.gameplay.hearthstone.adaptive_compare import compare_analyses
from analysis.gameplay.hearthstone.adaptive_v2 import SparseAdaptiveHearthstoneAnalyzer, SparseSettings
from analysis.gameplay.models import GameplayAnalysisResult
from analysis.gameplay_editing import default_detector_assets
from analysis.performance_profile import GameplayProfiler
from analysis.shared.frames import FrameSamplingService


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source",type=Path)
    parser.add_argument("--reference",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--full-videoprova",action="store_true")
    parser.add_argument("--scheduler",choices=("lattice","legacy"),default="lattice")
    args=parser.parse_args()
    if not args.full_videoprova or args.source.name.lower()!="videoprova.mp4":
        parser.error("Only --full-videoprova is authorized")
    if args.output.exists():
        parser.error("Choose a new output; existing evidence is preserved")
    options: dict[str,Any] = dict(default_detector_assets("hearthstone"))
    settings=SparseSettings(scheduler=args.scheduler)
    detector=SparseAdaptiveHearthstoneAnalyzer(**options,use_cache=False,sparse_settings=settings,
        sampler=FrameSamplingService(enabled=False,in_process=args.scheduler=="lattice",workers=settings.workers))
    info=detector.sampler.probe(args.source)
    if not 0<info.duration_s<=3800:
        parser.error("Only the one-hour reference video is authorized")
    raw=json.loads(args.reference.read_text(encoding="utf-8"))
    previous=raw["result"]
    if previous["metadata"]["video_fingerprint"]!=video_identity(args.source)["fingerprint"]:
        parser.error("Historical reference is from a different source")
    previous["video_path"]=str(args.source.resolve())
    reference=GameplayAnalysisResult.from_mapping(previous)
    profiler=GameplayProfiler(str(args.source),save=False)
    started=time.perf_counter()
    report: dict[str,Any]={"format":"recut_adaptive_v2_benchmark_v1","scheduler":args.scheduler,
        "source":args.source.name,"duration_s":info.duration_s,"reference":args.reference.name,
        "reference_wall_seconds_historical":raw["wall_seconds"],
        "cache":"detection and selected-frame persistence disabled; OS cache uncontrolled",
        "scope":"video detection only; no ASR, agents, boundaries or packaging",
        "implementation_sha256":{name:hashlib.sha256((Path(__file__).resolve().parents[1]/"analysis/gameplay/hearthstone"/name).read_bytes()).hexdigest()
            for name in ("adaptive_v2.py","fingerprint_v2.py","state_aware.py")}}
    root=Path(__file__).resolve().parents[1]
    report["implementation_sha256"].update({name:hashlib.sha256((root/"analysis/shared"/name).read_bytes()).hexdigest()
        for name in ("frames.py","inprocess_frames.py")})
    try:
        with profiler.activate():
            result=detector.analyze(args.source,on_progress=lambda p:print(f"V2 {p}%",flush=True))
        report["wall_seconds"]=time.perf_counter()-started
        report["comparison"]=compare_analyses(reference,result)
        if (result.settings["detector"]!=reference.settings["detector"]
                or result.settings["result_decision"]!=reference.settings["result_decision"]):
            raise RuntimeError("Reference asset/policy mismatch")
        payload=result.to_mapping();payload["video_path"]=args.source.name
        report.update(status="completed",result=payload)
    except BaseException as exc:
        report.update(status="failed",error_type=type(exc).__name__,wall_seconds=time.perf_counter()-started)
        raise
    finally:
        report["phases"]=profiler.phases
        atomic_write_json(args.output,report)
    print(json.dumps({"seconds":report["wall_seconds"],"comparison":report["comparison"]}),flush=True)


if __name__=="__main__":
    main()
