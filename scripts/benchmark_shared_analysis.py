"""Offline, short synthetic benchmark. Never accepts or opens a user VOD or ASR model.
Run: python scripts/benchmark_shared_analysis.py --output docs/performance_reports/shared_fixture.json
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time
from typing import Any, Callable
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from analysis.audio_service import AnalysisRequest, analyze_audio
from analysis.boundaries.models import TimeRange, TranscriptSegment, TranscriptWord
from analysis.boundaries.transcript import FasterWhisperEngine
from analysis.gameplay.cache import atomic_write_json, video_identity
from analysis.gameplay.hearthstone.state_aware import StateAwareHearthstoneAnalyzer
from analysis.gameplay.hearthstone.visual.detector import normalize_frame
from analysis.gameplay.hearthstone.visual.sampler import FFmpegFrameSampler
from analysis.gameplay.models import GameEventType
from analysis.gameplay_editing import default_detector_assets
from analysis.packaging.cache import PackagingCache
from analysis.packaging.transcript import build_transcript
from analysis.shared.frames import FrameSamplingService
from analysis.shared.timeline import KeepPiece, TimelineMapper
from analysis.shared.transcript import TranscriptCoverage
from utils.ffmpeg import ensure_ffmpeg
from utils.subprocess_utils import run_no_window


def timed(action: Callable[[], Any]) -> tuple[float, Any]:
    start = time.perf_counter()
    value = action()
    return time.perf_counter() - start, value


def main() -> None:
    import cv2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--hero-spacing", action="store_true", help="72s fixture with ten samples across 60s")
    args = parser.parse_args()
    duration = 72 if args.hero_spacing else 16
    ffmpeg, _ = ensure_ffmpeg()
    report: dict[str, Any] = {"fixture": f"generated {duration}s 1080p60 H264 + sine", "logical_cores": os.cpu_count(),
                              "opencv": cv2.__version__, "scope": "short synthetic fixture, not VOD accuracy or 7h speedup"}
    with tempfile.TemporaryDirectory(prefix="recut-shared-benchmark-") as folder:
        root = Path(folder)
        video = root / "fixture.mp4"
        command = [ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-f", "lavfi", "-i",
                   f"testsrc2=size=1920x1080:rate=60:duration={duration}", "-f", "lavfi", "-i",
                   f"sine=frequency=440:sample_rate=48000:duration={duration}", "-c:v", "libx264", "-preset", "ultrafast",
                   "-crf", "28", "-threads", "4", "-c:a", "aac", "-shortest", "-y", str(video)]
        generated = run_no_window(command, capture_output=True, timeout=60)
        if generated.returncode:
            raise RuntimeError(generated.stderr)
        times = [round(float(t), 3) for t in np.linspace(5, 65, 10)] if args.hero_spacing else [.23, 2.91, 5.59, 8.27, 10.95, 13.63]
        sampler = FFmpegFrameSampler()
        sampler.probe(video)
        def singles():
            out = {}
            for t in times:
                with closing(sampler.sample(video, fps=1, start_s=t, end_s=t+.1, max_width=960)) as stream:
                    out[t] = next(stream, None)
            return out
        service = FrameSamplingService(sampler, directory=root / "frames")
        old_s, old = timed(singles)
        from analysis.gameplay.hearthstone.visual import sampler as sampler_module
        spawn = sampler_module.popen_no_window
        with patch.object(sampler_module, "popen_no_window", side_effect=spawn) as processes:
            new_s, new = timed(lambda: service.get_many(video, times))
        process_count = processes.call_count
        warm_s, warm = timed(lambda: service.get_many(video, times))
        for t in times:
            assert old[t].timestamp == new[t].timestamp == warm[t].timestamp
            np.testing.assert_array_equal(old[t].image, new[t].image)
            np.testing.assert_array_equal(old[t].image, warm[t].image)
        report["selected_frames"] = {"requests": len(times), "legacy_processes": len(times), "batch_processes": process_count,
                                     "legacy_seconds": old_s, "batch_cold_including_persistence_seconds": new_s,
                                     "warm_seconds": warm_s, "pixels_and_timestamps_equal": True}
        with patch.dict(os.environ, {"AUTO_CUTTER_CACHE_DIR": str(root / "audio-cache")}):
            cold_s, cold = timed(lambda: analyze_audio(AnalysisRequest(str(video))))
            cached_s, cached = timed(lambda: analyze_audio(AnalysisRequest(str(video))))
            np.testing.assert_array_equal(cold.rms, cached.rms)
            report["classic"] = {"cold_seconds": cold_s, "warm_seconds": cached_s, "rms_equal": True}
        analyzer = StateAwareHearthstoneAnalyzer(**default_detector_assets("hearthstone"), use_cache=False)
        images = [new[t].image for t in times]
        kinds = (GameEventType.VS_SCREEN,)
        def matches(shared):
            results = []
            for frame in images:
                normalized = normalize_frame(frame, analyzer.profile.reference_size, analyzer.profile.viewport)
                gray = cv2.cvtColor(normalized, cv2.COLOR_BGR2GRAY) if shared else normalized
                board = analyzer.board.score(gray)
                l, t, r, b = analyzer.profile.result_roi.pixel_bounds(*analyzer.profile.reference_size)
                result = analyzer.text.score_roi(gray[t:b, l:r])
                events = (analyzer.vs.detect_reference_candidates(gray, 1., event_types=kinds, minimum_similarity=.8)
                          if shared else analyzer.vs.detect_candidates(frame, 1., event_types=kinds, minimum_similarity=.8))
                results.append((board, result, [e.to_mapping() for e in events]))
            return results
        legacy_times, shared_times = [], []
        for _ in range(3):
            elapsed, before = timed(lambda: matches(False)); legacy_times.append(elapsed)
            elapsed, after = timed(lambda: matches(True)); shared_times.append(elapsed)
            assert before == after
        report["matchers"] = {"frames_per_repeat": len(images), "repeats": 3,
                              "legacy_median_seconds": statistics.median(legacy_times),
                              "shared_gray_median_seconds": statistics.median(shared_times), "scores_and_events_equal": True}
        # Mock-free cache reads with deliberately pre-populated ASR; model execution is forbidden.
        engine = FasterWhisperEngine(cache_directory=root / "asr")
        identity = video_identity(video)
        span = TimeRange(0, duration)
        segments = [TranscriptSegment(1, 14.5, "one two three", words=tuple(
                    TranscriptWord(t, t+.5, str(t)) for t in (1., 6., 14.)))]
        key = engine._key(identity, span)
        atomic_write_json(engine.cache_directory / (key + ".json"), {"key": key, "segments": [s.to_mapping() for s in segments]})
        TranscriptCoverage(engine.cache_directory, identity, engine.settings.engine_options()).register(key, span)
        outputs = [TimelineMapper([str(video)], [KeepPiece(0, 0, 7, 0)], origin="project", identities=[identity]),
                   TimelineMapper([str(video)], [KeepPiece(0, 13, 16, 0)], origin="project", identities=[identity])]
        cache = PackagingCache(root / "packaging")
        with patch("analysis.boundaries.transcript.find_whisper_python", side_effect=AssertionError("duplicate ASR")):
            timings = []
            for mapping in outputs:
                seconds, (status, lines) = timed(lambda: build_transcript(mapping, engine.settings, cache=cache, engine=engine))
                timings.append({"seconds": seconds, "status": status["status"], "lines": len(lines)})
        report["packaging_two_groups"] = {"runs": timings, "asr_invocations": 0, "raw_transcript_source_time": True}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
