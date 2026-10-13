"""Experimental single-decode adaptive scan; the traditional backend remains default."""
from __future__ import annotations

from collections import deque
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
import time
from typing import Any

from analysis.audio_service import AnalysisCancellation, AnalysisCancelled
from analysis.gameplay.models import GameEvent, GameEventType
from analysis.gameplay.assembler import GameAssembler, consolidate_events
from analysis.gameplay.result_events import deduplicate_result_events
from analysis.gameplay.analyzer import GameplayAnalysisError
from analysis.performance_profile import current_profile, measure, record, profiled
from analysis.shared.frames import FrameSamplingService
from utils.runtime_paths import cache_root
from .adaptive_policy import AdaptivePlanner, AdaptiveSettings, RoutingObservation, merge_intervals
from .fingerprint import FingerprintExtractor, VERSION, assess_continuity
from .analyzer import candidate_windows
from .result_decision import decide_results
from .state_aware import StateAwareHearthstoneAnalyzer
from .state_monitor import MonitorObservation, ResultWindow, merge_windows
from .visual.detector import normalize_frame


class AdaptiveHearthstoneAnalyzer(StateAwareHearthstoneAnalyzer):
    analyzer_version = "hearthstone-adaptive-experimental-v2"

    def __init__(self, *, adaptive_settings: AdaptiveSettings | None = None, **kwargs: Any) -> None:
        if kwargs.get("cache_directory") is None:
            kwargs["cache_directory"] = cache_root() / "gameplay" / "hearthstone" / "adaptive"
        if kwargs.get("sampler") is None:
            kwargs["sampler"] = FrameSamplingService()
        super().__init__(**kwargs)
        self.adaptive_settings = adaptive_settings or AdaptiveSettings()
        self.fingerprint = FingerprintExtractor(self.profile)
        self.details: dict[str, Any] = {}
        self.full_fallback = False
        self.autonomous_rows: list[MonitorObservation] = []
        self.autonomous_vs: list[tuple[float, float]] = []
        self.autonomous_result: list[ResultWindow] = []
        self.autonomous_barriers: list[float] = []
        self.autonomous_intervals: list[Any] = []

    @profiled("adaptive.total")
    def analyze(self, *args: Any, **kwargs: Any) -> Any:
        result = super().analyze(*args, **kwargs)
        result.warnings.append("Adaptive experimental: identical sparse fingerprints cannot exclude an invisible game transition.")
        return result

    def _extra_settings(self) -> dict[str, Any]:
        active = current_profile()
        if active:
            active.modes.update(detector_backend="adaptive", adaptive_settings=asdict(self.adaptive_settings))
        return {"adaptive": asdict(self.adaptive_settings), "fingerprint_version": VERSION,
                "routing_version": "hysteresis-independent-evidence-v2", "delivery": "single-decode-bounded-buffer"}

    def _extra_metadata(self) -> dict[str, Any]:
        return {"strategy": "adaptive-experimental", "adaptive": self.details}

    def _scan_monitor(self, video: Path, token: AnalysisCancellation, measured: Any, progress: Any,
                      duration: float, profiling: dict[str, Any], comparisons: dict[str, int]
                      ) -> tuple[list[GameEvent], list[MonitorObservation], list[dict[str, Any]], int]:
        settings = self.adaptive_settings
        self.details = {}
        self.full_fallback = False
        self.autonomous_rows, self.autonomous_vs, self.autonomous_result = [], [], []
        self.autonomous_barriers = []
        self.autonomous_intervals = []
        before_scan = time.perf_counter()
        coarse: list[GameEvent] = []
        autonomous_coarse: list[GameEvent] = []
        observations: dict[float, MonitorObservation] = {}
        evidence: dict[float, dict[str, Any]] = {}
        routing: list[RoutingObservation] = []
        vs_scored: set[float] = set()
        vs_calls = 0
        planner = AdaptivePlanner(settings, board_high=self.monitor_settings.board_high,
                                  board_low=self.monitor_settings.board_low)
        planner.begin(duration)

        def score(frame: Any, *, reconnaissance: bool, gray: Any = None) -> Any:
            nonlocal vs_calls
            before = time.perf_counter()
            key = round(frame.timestamp, 6)
            if gray is None:
                with measure("monitor.image_preparation"):
                    normalized = normalize_frame(frame.image, self.profile.reference_size, self.profile.viewport)
                    gray = self.board.cv.cvtColor(normalized, self.board.cv.COLOR_BGR2GRAY)
            if key not in observations:
                with measure("monitor.board"):
                    board, anchors = self.board.score(gray)
                record("monitor.board", comparisons=self.board.comparisons_per_frame, frames=1)
                comparisons["board"] += self.board.comparisons_per_frame
                left, top, right, bottom = self.profile.result_roi.pixel_bounds(*self.profile.reference_size)
                with measure("monitor.result_matching"):
                    result = self.text.score_roi(gray[top:bottom, left:right])
                record("monitor.result_matching", comparisons=len(self.text.templates), frames=1)
                comparisons["text_monitor"] += len(self.text.templates)
                observations[key] = MonitorObservation(frame.timestamp, board, result["score"], result["margin"])
                evidence[key] = {"timestamp": frame.timestamp, "board_score": board, "anchors": anchors,
                                 "result_scores": result["scores"], "result_margin": result["margin"]}
                record("monitor", weak_result_candidate_frames=int(result["score"] >= self.monitor_settings.weak_score
                        and result["margin"] >= self.monitor_settings.weak_margin),
                       board_below_low_frames=int(board <= self.monitor_settings.board_low))
                record("adaptive.routing", scored_frames=1)
            row = observations[key]
            weak_result = row.result_score >= self.monitor_settings.weak_score and row.result_margin >= self.monitor_settings.weak_margin
            fp = None
            search_vs = True
            if reconnaissance:
                with measure("adaptive.fingerprint"):
                    fp = self.fingerprint.extract(gray, frame.timestamp, row.board_score)
                    if routing and row.board_score >= self.monitor_settings.board_high and routing[-1].fingerprint.board_score >= self.monitor_settings.board_high:
                        assessment = assess_continuity(routing[-1].fingerprint, fp, similarity_floor=settings.continuity_floor)
                        search_vs = assessment.category != "CONTINUITY_PLAUSIBLE" or weak_result
                record("adaptive.fingerprint", frames=1)
            candidates = []
            if search_vs and key not in vs_scored:
                with measure("monitor.vs"):
                    candidates = self.vs.detect_reference_candidates(gray, frame.timestamp,
                        event_types=(GameEventType.VS_SCREEN,), minimum_similarity=.80)
                coarse.extend(candidates)
                vs_scored.add(key)
                record("monitor.vs", frames=1, candidates=len(candidates))
                vs_calls += 1
            if fp is not None:
                autonomous_coarse.extend(candidates)
                self.autonomous_rows.append(row)
                routed = RoutingObservation(fp, bool(candidates), weak_result)
                routing.append(routed)
                with measure("adaptive.planning"):
                    planner.observe(routed)
            if len(observations) > 100000:
                raise GameplayAnalysisError("Too many adaptive monitor frames", code="candidate_limit")
            profiling["monitor"]["matching_seconds"] += time.perf_counter()-before
            return gray

        def traditional(reason: str) -> Any:
            self.full_fallback = True
            self.details.update(full_fallback=True, fallback_fraction=1.0, fallback_source_seconds=duration,
                                full_fallback_reason=reason)
            with measure("adaptive.recovery"):
                result = super(AdaptiveHearthstoneAnalyzer, self)._scan_monitor(
                    video, token, measured, progress, duration, profiling, comparisons)
            self.details["scan_wall_seconds"] = time.perf_counter()-before_scan
            return result

        if settings.force_full_fallback:
            return traditional("explicit")
        # Decode once at the existing 1 FPS grid. Delay matching until the next
        # coarse endpoint reveals which buffered frames require traditional work.
        # Holding one extra coarse interval includes both sides of overlap.
        pending: Any = deque()
        next_coarse = 0.0
        max_pending = delivered = discarded = reused = 0

        def flush(cutoff: float) -> None:
            nonlocal discarded, reused
            while pending and pending[0][0].timestamp < cutoff:
                frame, gray = pending.popleft()
                if any(w.start <= frame.timestamp < w.end for w in planner.requests):
                    with measure("adaptive.recovery"):
                        score(frame, reconnaissance=False, gray=gray)
                    record("adaptive.recovery", frames=1)
                    reused += 1
                else:
                    discarded += 1

        with measure("monitor"):
            try:
                with closing(self.sampler.sample(video, fps=self.monitor_settings.fps,
                        max_width=self.profile.reference_size[0], cancellation=token)) as frames:
                    for frame in measured(frames):
                        delivered += 1
                        if delivered > 100000:
                            raise GameplayAnalysisError("Too many adaptive delivered frames", code="candidate_limit")
                        gray = None
                        if frame.timestamp+1e-7 >= next_coarse:
                            with measure("adaptive.reconnaissance"):
                                gray = score(frame, reconnaissance=True)
                            record("adaptive.reconnaissance", frames=1)
                            next_coarse = frame.timestamp+settings.coarse_step_s
                        pending.append((frame, gray))
                        max_pending = max(max_pending, len(pending))
                        flush(next_coarse-2*settings.coarse_step_s-settings.overlap_s-1)
                        progress(60*frame.timestamp/duration)
                    flush(float("inf"))
            except AnalysisCancelled:
                raise
            except Exception as exc:
                token.raise_if_cancelled()
                # A failed/incomplete stream never becomes successful sparse output.
                self.details["reconnaissance_error_type"] = type(exc).__name__
                return traditional("stream_or_matching_failure")
        windows = merge_intervals(planner.requests)
        trigger_reasons = {"board_transition","weak_start","weak_result","possible_mana_reset"}
        self.autonomous_intervals = merge_intervals(w for w in planner.requests
            if trigger_reasons.intersection(w.reasons)
            or {"changed_player_portrait","changed_opponent_portrait"}.issubset(w.reasons))
        coarse_times = {round(r.timestamp,6) for r in self.autonomous_rows}
        self.autonomous_rows = [r for r in observations.values() if round(r.timestamp,6) in coarse_times
            or any(w.start <= r.timestamp < w.end for w in self.autonomous_intervals)]
        self.autonomous_rows.sort(key=lambda r:r.timestamp)
        own_candidates = [e for e in coarse if any(w.start <= e.timestamp < w.end for w in self.autonomous_intervals)]
        self.autonomous_vs = candidate_windows([*autonomous_coarse,*own_candidates],duration=duration,padding=2.0)
        self.details.update({
            "coarse_step_s": settings.coarse_step_s, "reconnaissance_frames": len(routing),
            "states": planner.states, "diagnostics": planner.diagnostics(),
            "recovery_intervals": [asdict(w) for w in windows],
            "fallback_source_seconds": sum(w.end-w.start for w in windows),
            "fallback_fraction": sum(w.end-w.start for w in windows)/duration, "full_fallback": False,
            "full_promotion": "not needed: local recovery uses buffered first-pass frames even above cost threshold",
            "delivered_frames": delivered, "buffer_max_frames": max_pending,
            "recovery_frames_reused": reused, "unmatched_delivered_frames": discarded,
            "autonomous_vs_windows": self.autonomous_vs,
            "autonomous_trigger_intervals": [asdict(w) for w in self.autonomous_intervals],
            "invisible_transition_guarantee": False, "mana_reader": "unavailable; no numeric mana inferred",
            "scan_wall_seconds": time.perf_counter()-before_scan,
        })
        record("adaptive.routing", intervals_refined=len(windows), fallback_source_seconds=self.details["fallback_source_seconds"],
               fallback_percent=100*self.details["fallback_fraction"], delivered_frames=delivered,
               unmatched_frames=discarded, buffer_max_frames=max_pending)
        record("adaptive.recovery", windows=len(windows), reused_frames=reused)
        record("adaptive.routing", vs_calls_avoided=max(0,delivered-vs_calls),
               board_calls_avoided=delivered-len(observations), result_calls_avoided=delivered-len(observations))
        return coarse, sorted(observations.values(), key=lambda r: r.timestamp), sorted(evidence.values(), key=lambda r: r["timestamp"]), vs_calls

    def _plan_result_windows(self, rows: list[MonitorObservation], starts: list[GameEvent], duration: float) -> Any:
        windows, barriers = super()._plan_result_windows(rows, starts, duration)
        if self.full_fallback:
            return windows, barriers
        autonomous_starts = [e for e in starts if any(a <= e.timestamp < b for a,b in self.autonomous_vs)]
        autonomous, auto_barriers = super()._plan_result_windows(self.autonomous_rows, autonomous_starts, duration)
        # Use the already-buffered 1 FPS frames to narrow coarse trigger gaps.
        # Do not double the 60 FPS window just to compute autonomous diagnostics.
        def orphan_windows(observed: list[MonitorObservation]) -> list[ResultWindow]:
            output = []
            highs, armed, previous_time = 0, False, None
            for row in observed:
                if previous_time is not None and row.timestamp-previous_time > 1.5/self.monitor_settings.fps:
                    highs, armed = 0, False
                highs = highs+1 if row.board_score >= self.monitor_settings.board_high else 0
                if highs >= self.monitor_settings.board_observations:
                    armed = True
                if armed and row.board_score <= self.monitor_settings.board_low:
                    output.append(ResultWindow(max(0,row.timestamp-self.monitor_settings.before_s),
                        min(duration,row.timestamp+self.monitor_settings.after_s),
                        ((row.timestamp,"orphan_board_disappeared"),)))
                    armed = False
                previous_time = row.timestamp
            return output
        autonomous.extend(orphan_windows(self.autonomous_rows))
        self.autonomous_result, self.autonomous_barriers = merge_windows(autonomous), auto_barriers
        self.details["autonomous_result_windows"] = [asdict(w) for w in self.autonomous_result]
        windows.extend(self.autonomous_result)
        windows.extend(orphan_windows(rows))
        return merge_windows(windows), barriers

    def _refinement_diagnostics(self, vs_observations: list[GameEvent], window_evidence: Any, duration: float) -> None:
        if self.full_fallback:
            self.details["autonomous"] = {"available": False, "reason": "full fallback; no autonomous claim"}
            return
        with measure("adaptive.diagnostics"):
            own_observations = [e for e in vs_observations if any(a <= e.timestamp < b for a,b in self.autonomous_vs)]
            own_starts = [e for e in consolidate_events(own_observations,max_gap_s=.5)
                          if e.metadata["observation_count"] >= 3 and e.metadata["temporal_span_s"]+1e-9 >= .25]
            own_evidence = [e for e in window_evidence if any(w.start <= e.timestamp < w.end for w in self.autonomous_result)]
            accepted = [event for d in decide_results(own_evidence,self.result_policy)
                        if (event := d.to_event({"scope":"autonomous_diagnostic"})) is not None]
            events = deduplicate_result_events([*own_starts,*accepted],barriers=self.autonomous_barriers,maximum_gap_s=45.0)
            games = GameAssembler(maximum_game_duration_s=1800.0).assemble(events)
            self.details["autonomous"] = {
                "available": True,
                "scope": "coarse triggers + directed buffered 1 FPS searches + shared refinements; excludes periodic and identity/state uncertainty fallback",
                "events": [e.to_mapping() for e in events], "games": [g.to_mapping() for g in games],
                "result_evidence_frames": len(own_evidence),
                "separate_runtime": None, "runtime_note": "derived from shared refinements, not a separate timed run"}
