"""Experimental sparse-seek V2. Frozen matchers/refinement/assembly are inherited.

Scheduler "lattice" (default): the 30 s hierarchy is snapped to a 2 s lattice
(the keyframe interval of typical OBS recordings) and frames come from an
in-process decoder session per worker instead of one FFmpeg process per seek.
1 FPS monitor rows are read only where adjacent lattice samples disagree on
what the result planner uses (board high/low, weak result, coarse VS), so
transition rows are real; rows between agreeing samples are imputed for the
planner. Scheduler "legacy" keeps the first V2 (sequential local fallbacks).
"""
from __future__ import annotations

import bisect
from collections import Counter
from contextlib import closing
from dataclasses import asdict, dataclass
import math
import json
from pathlib import Path
import time
from typing import Any, Callable, Iterator

from analysis.audio_service import AnalysisCancellation, AnalysisCancelled
from analysis.gameplay.models import GameEvent, GameEventType
from analysis.performance_profile import current_profile, measure, record
from analysis.shared.frames import FrameSamplingService
from utils.runtime_paths import cache_root
from .adaptive_policy import RecoveryInterval, merge_intervals
from .fingerprint import GameFingerprint
from .result_decision import NO_RESULT, ResultEvidence, SingleFrameConfirmation
from .fingerprint_v2 import DEFAULT_EXCLUSIONS, ROIS, VERSION, SparseFingerprint, SparseFingerprintExtractor, discontinuities
from .state_aware import StateAwareHearthstoneAnalyzer
from .state_monitor import GameStateMonitor, MonitorObservation, ResultWindow, merge_windows
from .visual.detector import normalize_frame


@dataclass(frozen=True)
class SparseSettings:
    step_s: float = 30.
    leaf_s: float = 3.75
    overlap_s: float = 4.
    continuity_floor: float = .90
    recovery_period_s: float = 60.
    recovery_width_s: float = 8.
    force_full_fallback: bool = False
    exclusions: tuple[tuple[float, float, float, float], ...] = DEFAULT_EXCLUSIONS
    scheduler: str = "lattice"
    # Lattice spacing: at least two samples inside the shortest observed VS
    # (5.2 s at the frozen >= .80 matcher) and the usual 2 s keyframe interval.
    lattice_s: float = 2.
    workers: int = 4
    # END recovery window reaches back to the last solid board (hero explosion,
    # then a result banner possibly 0.2 s, then the dimmed board), at most this
    # far before the sustained disappearance: a cost bound, not a detector threshold.
    recovery_lookback_s: float = 10.

    def __post_init__(self) -> None:
        for name in ("step_s", "leaf_s", "overlap_s", "continuity_floor", "recovery_period_s", "recovery_width_s",
                     "lattice_s", "recovery_lookback_s"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
                raise ValueError("Sparse settings must be finite")
        if self.step_s != 30 or not .5 <= self.leaf_s <= 7.5 or not 2 <= self.overlap_s <= 10:
            raise ValueError("Invalid sparse grid/leaf/overlap")
        if not 0 < self.continuity_floor <= 1 or not 30 <= self.recovery_period_s <= 120:
            raise ValueError("Invalid continuity/period")
        if not 4 <= self.recovery_width_s <= self.recovery_period_s or type(self.force_full_fallback) is not bool:
            raise ValueError("Invalid recovery")
        for roi in self.exclusions:
            if len(roi) != 4 or any(isinstance(v, bool) or not math.isfinite(v) for v in roi):
                raise ValueError("Invalid exclusion")
            x,y,w,h = roi
            if x < 0 or y < 0 or w <= 0 or h <= 0 or x+w > 1 or y+h > 1:
                raise ValueError("Invalid exclusion")
        if self.scheduler not in {"lattice", "legacy"}:
            raise ValueError("scheduler must be lattice or legacy")
        if not .5 <= self.lattice_s <= 2.5 or abs(self.step_s/self.lattice_s - round(self.step_s/self.lattice_s)) > 1e-9:
            raise ValueError("lattice_s must be 0.5..2.5 and divide step_s")
        if isinstance(self.workers, bool) or not isinstance(self.workers, int) or not 1 <= self.workers <= 16:
            raise ValueError("workers must be an integer in 1..16")
        if not 2 <= self.recovery_lookback_s <= 30:
            raise ValueError("recovery_lookback_s must be 2..30")


class SparseAdaptiveHearthstoneAnalyzer(StateAwareHearthstoneAnalyzer):
    analyzer_version = "hearthstone-adaptive-sparse-v2.3"

    def __init__(self, *, sparse_settings: SparseSettings | None = None, **kwargs: Any) -> None:
        if kwargs.get("cache_directory") is None:
            kwargs["cache_directory"] = cache_root() / "gameplay" / "hearthstone" / "adaptive-sparse-v2"
        settings = sparse_settings or SparseSettings()
        if kwargs.get("sampler") is None:
            # Monitor frames are not persisted: ~1.5 MB each, cheaper to decode
            # again; the detection cache already stores the analysis outcome.
            kwargs["sampler"] = FrameSamplingService(enabled=False, in_process=settings.scheduler == "lattice",
                                                     workers=settings.workers)
        super().__init__(**kwargs)
        self.frame_service: Any = self.sampler if callable(getattr(self.sampler, "get_many", None)) else FrameSamplingService(self.sampler)
        self.sparse_settings = settings
        self.single_frame = SingleFrameConfirmation()
        self._planner_rows: list[MonitorObservation] | None = None
        self.fingerprint = SparseFingerprintExtractor(self.profile, exclusions=self.sparse_settings.exclusions)
        self.details: dict[str, Any] = {}

    def _extra_settings(self) -> dict[str, Any]:
        active = current_profile()
        if active:
            active.modes.update(detector_backend="adaptive-v2", sparse_settings=asdict(self.sparse_settings))
        return json.loads(json.dumps({"sparse_settings": asdict(self.sparse_settings), "fingerprint": VERSION,
                "single_frame_results": self.single_frame.to_mapping(),
                "rois": ROIS, "mana_pack": self.fingerprint.mana_reader.fingerprint,
                "delivery": ("in-process-lattice-hierarchy-with-planner-refill" if self.sparse_settings.scheduler == "lattice"
                             else "accurate-sparse-seeks-and-local-sequential-fallback")}))

    def _extra_metadata(self) -> dict[str, Any]:
        return {"strategy": "adaptive-v2-experimental", "adaptive": self.details}

    def _scan_monitor(self, video: Path, token: AnalysisCancellation, measured: Any, progress: Any,
                      duration: float, profiling: dict[str, Any], comparisons: dict[str, int]) -> Any:
        started = time.perf_counter()
        self.details = {}
        self.full_fallback = False
        self._planner_rows = None
        settings = self.sparse_settings
        rows: dict[float, MonitorObservation] = {}
        evidence: dict[float, dict[str, Any]] = {}
        fingerprints: dict[float, SparseFingerprint] = {}
        coarse: list[GameEvent] = []
        requests: list[RecoveryInterval] = []
        counts: Counter[str] = Counter()
        traces: list[dict[str, Any]] = []
        vs_calls = 0

        def score(frame: Any, *, fingerprint: bool) -> SparseFingerprint | None:
            nonlocal vs_calls
            before = time.perf_counter()
            key = round(frame.timestamp, 6)
            with measure("monitor.image_preparation"):
                normalized = normalize_frame(frame.image, self.profile.reference_size, self.profile.viewport)
                gray = self.board.cv.cvtColor(normalized, self.board.cv.COLOR_BGR2GRAY)
            if key not in rows:
                with measure("monitor.board"):
                    board, anchors = self.board.score(gray)
                record("monitor.board", comparisons=self.board.comparisons_per_frame, frames=1)
                comparisons["board"] += self.board.comparisons_per_frame
                l,t,r,b = self.profile.result_roi.pixel_bounds(*self.profile.reference_size)
                with measure("monitor.result_matching"):
                    result = self.text.score_roi(gray[t:b,l:r])
                record("monitor.result_matching", comparisons=len(self.text.templates), frames=1)
                comparisons["text_monitor"] += len(self.text.templates)
                with measure("monitor.vs"):
                    candidates = self.vs.detect_reference_candidates(gray, frame.timestamp,
                        event_types=(GameEventType.VS_SCREEN,), minimum_similarity=.80)
                record("monitor.vs", frames=1, candidates=len(candidates))
                vs_calls += 1
                coarse.extend(candidates)
                row = MonitorObservation(frame.timestamp, board, result["score"], result["margin"])
                rows[key] = row
                evidence[key] = {"timestamp": frame.timestamp, "board_score": board, "anchors": anchors,
                    "result_scores": result["scores"], "result_margin": result["margin"],
                    "weak_start": bool(candidates)}
            row = rows[key]
            if fingerprint:
                with measure("adaptive_v2.fingerprint"):
                    fp = self.fingerprint.extract(gray, frame.timestamp, row.board_score)
                record("adaptive_v2.fingerprint", frames=1)
                state = ("IN_GAME" if row.board_score >= self.monitor_settings.board_high else
                         "OUT_OF_GAME" if row.board_score <= self.monitor_settings.board_low else "UNKNOWN")
                routed = SparseFingerprint(fp,state,evidence[key]["weak_start"],
                    row.result_score >= self.monitor_settings.weak_score and row.result_margin >= self.monitor_settings.weak_margin)
                counts[state] += 1
                counts["mana_readings"] += int(fp.maximum_mana is not None)
                fingerprints[key] = routed
                profiling["monitor"]["matching_seconds"] += time.perf_counter()-before
                return routed
            profiling["monitor"]["matching_seconds"] += time.perf_counter()-before
            return None

        def fetch(times: list[float]) -> dict[float, SparseFingerprint]:
            # Bounded batches; no full-VOD frame sequence is retained in RAM.
            output = {}
            for index in range(0,len(times),8):
                token.raise_if_cancelled()
                group = times[index:index+8]
                before = time.perf_counter()
                with measure("adaptive_v2.frame_requests"):
                    frames = self.frame_service.get_many(video,group,max_width=self.profile.reference_size[0],cancellation=token)
                profiling["monitor"]["sampling_wait_seconds"] += time.perf_counter()-before
                counts["seek_requests"] += len(group)
                counts["sparse_frames_delivered"] += sum(f is not None for f in frames.values())
                record("monitor", frames=sum(f is not None for f in frames.values()))
                for requested in group:
                    frame = frames.get(requested)
                    if frame is None:
                        output[requested] = SparseFingerprint(GameFingerprint(requested,.3), "UNKNOWN")
                        counts["empty_requests"] += 1
                    else:
                        routed = score(frame, fingerprint=True)
                        assert routed is not None
                        output[requested] = routed
                active = current_profile()
                if active:
                    active.checkpoint()
            return output

        def add(a: float, b: float, reasons: tuple[str,...]) -> None:
            lo,hi = max(0.,float(math.floor(a-settings.overlap_s))), min(duration,float(math.ceil(b+settings.overlap_s)))
            if hi > lo:
                requests.append(RecoveryInterval(lo,hi,reasons))

        def full(reason: str) -> Any:
            self.full_fallback = True
            with measure("adaptive_v2.full_fallback"):
                output = super(SparseAdaptiveHearthstoneAnalyzer,self)._scan_monitor(
                    video,token,measured,progress,duration,profiling,comparisons)
            self.details.update(full_fallback=True, full_fallback_reason=reason, fallback_fraction=1.,
                                fallback_source_seconds=duration, scan_wall_seconds=time.perf_counter()-started,
                                counters=dict(counts))
            return output

        if settings.force_full_fallback:
            return full("explicit")
        if settings.scheduler == "lattice":
            with measure("monitor"):
                try:
                    self._scan_lattice(video, token, progress, duration, profiling, score, counts, traces, rows)
                except AnalysisCancelled:
                    raise
                except Exception as exc:
                    token.raise_if_cancelled()
                    self.details["sparse_error_type"] = type(exc).__name__
                    self._planner_rows = None
                    return full("sparse_access_or_matching_failure")
                finally:
                    closer = getattr(self.frame_service, "close", None)
                    if callable(closer):
                        closer()
            counts["vs_calls"] = vs_calls
            self.details.update(counters=dict(counts), scan_wall_seconds=time.perf_counter()-started,
                hierarchy=traces, invisible_transition_guarantee=False,
                mana_note="denominator-only; unsupported/ambiguous reads remain None",
                states=[{"timestamp":r.fingerprint.timestamp,"state":r.state,
                    "maximum_mana":r.fingerprint.maximum_mana,"mana_confidence":r.fingerprint.mana_confidence}
                    for r in sorted(fingerprints.values(),key=lambda r:r.fingerprint.timestamp)])
            record("adaptive_v2.routing", **dict(counts))
            return (coarse, sorted(rows.values(), key=lambda r: r.timestamp),
                    sorted(evidence.values(), key=lambda r: r["timestamp"]), vs_calls)
        with measure("monitor"):
            try:
                times = [float(t) for t in range(0,math.ceil(duration),int(settings.step_s))]
                tail = max(0.,duration-.1)
                if tail > times[-1]:
                    times.append(tail)
                with measure("adaptive_v2.reconnaissance"):
                    initial = fetch(times)
                progress(15)
                pending = [(a,b,0) for a,b in zip(times,times[1:])]
                observed = dict(initial)
                while pending:
                    token.raise_if_cancelled()
                    subdivide = []
                    for a,b,depth in pending:
                        reasons = discontinuities(observed[a],observed[b],settings.continuity_floor)
                        counts["intervals_examined"] += 1
                        if not reasons:
                            counts["plausible_intervals"] += 1
                            continue
                        traces.append({"start":a,"end":b,"depth":depth,"reasons":list(reasons),
                            "left_state":observed[a].state,"right_state":observed[b].state,
                            "mana_before":observed[a].fingerprint.maximum_mana,
                            "mana_after":observed[b].fingerprint.maximum_mana})
                        if b-a <= settings.leaf_s+1e-6:
                            add(a,b,reasons)
                            counts["uncertain_leaves"] += 1
                        else:
                            subdivide.append((a,b,depth))
                    if not subdivide:
                        break
                    midpoints = sorted({(a+b)/2 for a,b,_ in subdivide})
                    with measure("adaptive_v2.hierarchy"):
                        observed.update(fetch(midpoints))
                    # Explore both halves independently. A midpoint can reveal
                    # multiple transitions; mana never picks only one branch.
                    pending = [half for a,b,d in subdivide for half in ((a,(a+b)/2,d+1),((a+b)/2,b,d+1))]
                # Independent guards: identical heroes/map do not prove no
                # invisible transition. These are bounded checks, not a proof.
                add(0,settings.recovery_width_s,("leading_guard",))
                add(duration-settings.recovery_width_s,duration,("trailing_guard",))
                for t in range(int(settings.recovery_period_s),math.ceil(duration),int(settings.recovery_period_s)):
                    add(t-settings.recovery_width_s/2,t+settings.recovery_width_s/2,("periodic_guard",))
                windows = merge_intervals(requests)
                fallback_frames = 0
                with measure("adaptive_v2.local_fallback"):
                    for index,window in enumerate(windows):
                        with closing(self.sampler.sample(video,fps=self.monitor_settings.fps,start_s=window.start,
                                end_s=window.end,max_width=self.profile.reference_size[0],cancellation=token)) as stream:
                            for frame in measured(stream):
                                fallback_frames += 1
                                if round(frame.timestamp,6) not in rows:
                                    score(frame,fingerprint=False)
                        progress(30+30*(index+1)/len(windows))
            except AnalysisCancelled:
                raise
            except Exception as exc:
                token.raise_if_cancelled()
                self.details["sparse_error_type"] = type(exc).__name__
                return full("sparse_access_or_matching_failure")
        coverage = sum(w.end-w.start for w in windows)
        counts["fallback_frames_delivered"] = fallback_frames
        counts["monitor_frames_delivered"] = counts["sparse_frames_delivered"]+fallback_frames
        counts["vs_calls"] = vs_calls
        self.details.update(full_fallback=False, fallback_fraction=coverage/duration,
            fallback_source_seconds=coverage, recovery_intervals=[asdict(w) for w in windows],
            hierarchy=traces, counters=dict(counts), scan_wall_seconds=time.perf_counter()-started,
            invisible_transition_guarantee=False, mana_note="denominator-only; unsupported/ambiguous reads remain None",
            states=[{"timestamp":r.fingerprint.timestamp,"state":r.state,
                "maximum_mana":r.fingerprint.maximum_mana,"mana_confidence":r.fingerprint.mana_confidence}
                for r in sorted(fingerprints.values(),key=lambda r:r.fingerprint.timestamp)])
        record("adaptive_v2.routing", **dict(counts), fallback_source_seconds=coverage,
               fallback_percent=100*coverage/duration, recovery_windows=len(windows))
        return coarse, sorted(rows.values(),key=lambda r:r.timestamp), sorted(evidence.values(),key=lambda r:r["timestamp"]), vs_calls

    def _scan_lattice(self, video: Path, token: AnalysisCancellation, progress: Callable[[float], None],
                      duration: float, profiling: dict[str, Any],
                      score: Callable[..., SparseFingerprint | None], counts: Counter[str],
                      traces: list[dict[str, Any]], rows: dict[float, MonitorObservation]) -> None:
        """Hierarchy on the lattice; real 1 FPS rows only where the planner could differ."""
        settings, monitor = self.sparse_settings, self.monitor_settings
        period = 1.0 / monitor.fps
        ratio = max(1, round(settings.lattice_s / period))
        step = round(settings.step_s / period)
        if step % ratio:
            raise ValueError("step_s must be a multiple of the lattice")
        # Monitor rows as in the 1 FPS pass: first real frame at or after k*period.
        count = int(math.floor((duration - 1e-3) / period)) + 1
        last_lattice = (count - 1) // ratio * ratio
        observed: dict[int, SparseFingerprint] = {}
        keys: dict[int, float | None] = {}

        def frames(indices: list[int]) -> Iterator[tuple[int, Any]]:
            times = {k * period: k for k in indices}
            if not times:
                return
            width = self.profile.reference_size[0]
            iterate = getattr(self.frame_service, "iter_many", None)
            stream: Iterator[tuple[float, Any]]
            if callable(iterate):
                stream = iter(iterate(video, list(times), max_width=width, cancellation=token))
            else:
                stream = iter(self.frame_service.get_many(video, list(times), max_width=width,
                                                          cancellation=token).items())
            while True:
                before = time.perf_counter()
                with measure("adaptive_v2.frame_requests"):
                    item = next(stream, None)
                profiling["monitor"]["sampling_wait_seconds"] += time.perf_counter() - before
                if item is None:
                    return
                token.raise_if_cancelled()
                counts["seek_requests"] += 1
                yield times[item[0]], item[1]

        def fetch(indices: list[int], *, fingerprint: bool) -> None:
            for k, frame in frames(sorted(set(indices) - set(keys))):
                if frame is None:
                    keys[k] = None
                    counts["empty_requests"] += 1
                    if fingerprint:
                        observed[k] = SparseFingerprint(GameFingerprint(k * period, .3), "UNKNOWN")
                    continue
                counts["lattice_samples" if fingerprint else "refill_rows"] += 1
                record("monitor", frames=1)
                routed = score(frame, fingerprint=fingerprint)
                keys[k] = round(frame.timestamp, 6)
                if routed is not None:
                    observed[k] = routed
                active = current_profile()
                if active:
                    active.checkpoint()

        def lattice(a: float, b: float) -> list[int]:
            lo = max(0, math.ceil(a / period / ratio - 1e-9)) * ratio
            return list(range(lo, min(last_lattice, int(math.floor(b / period + 1e-9))) + 1, ratio))

        # Level 0 every step_s plus the independent guards, sampled on the lattice.
        initial = set(range(0, last_lattice + 1, step)) | {last_lattice}
        guards = lattice(0, settings.recovery_width_s) + lattice(duration - settings.recovery_width_s, duration)
        for centre in range(int(settings.recovery_period_s), math.ceil(duration), int(settings.recovery_period_s)):
            guards += lattice(centre - settings.recovery_width_s / 2, centre + settings.recovery_width_s / 2)
        counts["guard_samples"] = len(set(guards) - initial)
        with measure("adaptive_v2.reconnaissance"):
            fetch(sorted(initial | set(guards)), fingerprint=True)
        progress(15)
        known = sorted(observed)
        pending = list(zip(known, known[1:]))
        leaves: list[tuple[int, int]] = []
        plausible_seconds = 0.
        depth = 0
        while pending:
            token.raise_if_cancelled()
            split: list[tuple[int, int, int]] = []
            for a, b in pending:
                if b - a <= ratio:
                    leaves.append((a, b))
                    continue
                counts["intervals_examined"] += 1
                left, right = observed[a], observed[b]
                reasons = discontinuities(left, right, settings.continuity_floor)
                if not reasons and left.state != "IN_GAME":
                    # A VS screen (>= 5.2 s) fits between two out-of-game samples;
                    # only same-game continuity may skip lattice samples.
                    reasons = ("out_of_game_vs_watch",)
                if not reasons:
                    counts["plausible_intervals"] += 1
                    plausible_seconds += (b - a) * period
                    continue
                traces.append({"start": a * period, "end": b * period, "depth": depth, "reasons": list(reasons),
                               "left_state": left.state, "right_state": right.state,
                               "mana_before": left.fingerprint.maximum_mana,
                               "mana_after": right.fingerprint.maximum_mana})
                split.append((a, a + (b - a) // ratio // 2 * ratio, b))
            if not split:
                break
            with measure("adaptive_v2.hierarchy"):
                fetch([m for _, m, _ in split], fingerprint=True)
            # Explore both halves independently; a midpoint can reveal several transitions.
            pending = [half for a, m, b in split for half in ((a, m), (m, b))]
            depth += 1
            progress(15 + 15 * min(1., depth / 5))
        vs_hits = {keys[k]: observed[k].weak_start for k in observed if keys.get(k) is not None}

        def planner_class(k: int) -> tuple[bool, ...] | None:
            key = keys.get(k)
            if key is None:
                return None
            row = rows[key]
            weak = row.result_score >= monitor.weak_score and row.result_margin >= monitor.weak_margin
            return (row.board_score >= monitor.board_high, row.board_score <= monitor.board_low, weak,
                    bool(vs_hits.get(key)))

        refill = set(range(last_lattice + 1, count))  # tail rows after the last lattice sample
        for a, b in leaves:
            first, second = planner_class(a), planner_class(b)
            if first is None or first != second:
                refill.update(range(a + 1, b))
                counts["refill_leaves"] += 1
            else:
                counts["agreeing_leaves"] += 1
        with measure("adaptive_v2.refill"):
            fetch(sorted(refill), fingerprint=False)
            # While a board is present, the first row at or below board_low starts
            # the result window (-before_s/+after_s). End-of-game boards flicker in
            # the dead band for single seconds, and result screens can last 0.2 s,
            # so that first low row must be real: imputing it delays the window
            # past the result. Arming is VS-independent here (a superset of the
            # planner's VS arming); solid-board intervals stay imputed.
            for _ in range(8):
                needed = self._present_rows(keys, rows, count)
                if not needed:
                    break
                counts["present_refill_rows"] += len(needed)
                fetch(sorted(needed), fingerprint=False)
        progress(60)
        # Planner rows: real where read, otherwise the left real sample of an
        # interval whose two ends agree on every planner input.
        real = sorted(k for k, key in keys.items() if key is not None)
        planner: list[MonitorObservation] = []
        for k in range(count):
            key = keys.get(k)
            if key is not None:
                planner.append(rows[key])
                continue
            at = bisect.bisect_left(real, k) - 1
            if at < 0:
                continue
            origin = keys[real[at]]
            assert origin is not None
            source = rows[origin]
            planner.append(MonitorObservation(k * period, source.board_score, source.result_score, source.result_margin))
        self._planner_rows = planner
        counts["planner_rows"] = len(planner)
        counts["imputed_rows"] = len(planner) - len(real)
        counts["monitor_frames_delivered"] = len(real)
        access = getattr(self.frame_service, "access_counters", None)
        self.details.update(full_fallback=False, scheduler="lattice", lattice_s=ratio * period,
            fallback_fraction=len(refill) / max(1, count), fallback_source_seconds=len(refill) * period,
            real_row_fraction=len(real) / max(1, count), plausible_skip_seconds=plausible_seconds,
            access=access() if callable(access) else {})

    def _confirm_result_decisions(self, video: Path, decisions: list[Any], token: AnalysisCancellation) -> list[Any]:
        """Re-measure the 60 FPS neighbourhood of one-PTS result clusters only.

        Frames below the evidence floor are not retained by the result windows,
        so each single-frame candidate costs one short local ROI read
        (2 x neighbourhood_s at high_fps); no global scan.
        """
        rule = self.single_frame
        roi, viewport = self.profile.result_roi, self.profile.viewport
        source = (viewport.x + roi.x * viewport.width, viewport.y + roi.y * viewport.height,
                  roi.width * viewport.width, roi.height * viewport.height)
        roi_sample: Any = getattr(self.sampler, "sample_roi")
        out = []
        for decision in decisions:
            if decision.outcome != NO_RESULT or decision.reason != "insufficient_support" or len(decision.frames) != 1:
                out.append(decision)
                continue
            token.raise_if_cancelled()
            centre = decision.peak.timestamp
            neighbourhood: list[ResultEvidence] = []
            with measure("result_windows.single_frame_check"):
                with closing(roi_sample(video, roi=source, reference_size=self.profile.reference_size,
                        fps=self.monitor_settings.high_fps, start_s=max(0., centre - rule.neighbourhood_s),
                        end_s=centre + rule.neighbourhood_s + 1e-3, output_size=self.text.output_size,
                        cancellation=token)) as frames:
                    for frame in frames:
                        neighbourhood.append(ResultEvidence.from_scores(frame.timestamp, self.text.score_roi(frame.image)))
            confirmed = rule.confirm(decision, neighbourhood)
            record("result_windows.single_frame_check", candidates=1, frames=len(neighbourhood),
                   confirmed=int(confirmed.classified))
            out.append(confirmed)
        return out

    def _present_rows(self, keys: dict[int, float | None], rows: dict[float, MonitorObservation],
                      count: int) -> set[int]:
        """Unread rows that could start a board disappearance while a board is present.

        Forward pass over real and imputed rows (imputed = previous real row).
        A row is needed when the board is present and the latest real sample is
        not a solid board (>= board_high). Presence ends only at a sustained
        disappearance (two consecutive rows <= board_low): a one-frame dip must
        not hide the real end of the game (see _sustained_windows).
        """
        monitor = self.monitor_settings
        needed: set[int] = set()
        high, low, present = 0, 0, False
        last: MonitorObservation | None = None
        for k in range(count):
            key = keys.get(k)
            if key is not None:
                last = rows[key]
            elif last is None:
                continue
            elif present and last.board_score < monitor.board_high:
                needed.add(k)
                low = 0
                continue  # unknown value: the state after it is decided once read
            high = high + 1 if last.board_score >= monitor.board_high else 0
            low = low + 1 if last.board_score <= monitor.board_low else 0
            if not present and high >= monitor.board_observations:
                present = True
            elif present and low >= 2:
                present, high = False, 0
        return needed

    def _sustained_windows(self, rows: list[MonitorObservation], duration: float) -> list[ResultWindow]:
        """END recovery: the first sustained disappearance after a present board.

        The frozen planner triggers on the first row <= board_low; a one-frame
        dip earlier in the game consumes that trigger and the actual end (board
        gone for good, result screen possibly 0.2 s) gets no window. Here only
        a run of >= 2 low rows ends presence; single dips do not. The board
        often dims seconds after the result banner, so the window starts at the
        last solid board (>= board_high), within recovery_lookback_s.
        """
        monitor = self.monitor_settings
        lookback = self.sparse_settings.recovery_lookback_s
        windows: list[ResultWindow] = []
        high, present, solid = 0, False, 0.
        for row, following in zip(rows, rows[1:]):
            high = high + 1 if row.board_score >= monitor.board_high else 0
            if row.board_score >= monitor.board_high:
                solid = row.timestamp
            if not present and high >= monitor.board_observations:
                present = True
            elif present and row.board_score <= monitor.board_low and following.board_score <= monitor.board_low:
                start = min(row.timestamp - monitor.before_s, max(solid, row.timestamp - lookback))
                windows.append(ResultWindow(max(0., start), min(duration, row.timestamp + monitor.after_s),
                                            ((row.timestamp, "sustained_board_disappeared"),)))
                present, high = False, 0
        return windows

    def _plan_result_windows(self, rows: list[MonitorObservation], starts: list[GameEvent], duration: float) -> Any:
        if getattr(self, "full_fallback", False):
            return super()._plan_result_windows(rows, starts, duration)
        recovery: list[ResultWindow] = []
        if self._planner_rows is not None:
            # Complete 1 FPS sequence: identical planner state, one run.
            rows = self._planner_rows
            recovery = self._sustained_windows(rows, duration)
        # Reset temporal stability across unobserved gaps. Never treat two sparse
        # board hits 30s apart as consecutive 1 FPS observations.
        runs: list[list[MonitorObservation]] = []
        for row in rows:
            if not runs or row.timestamp-runs[-1][-1].timestamp > 1.5/self.monitor_settings.fps:
                runs.append([row])
            else:
                runs[-1].append(row)
        windows, barriers = list(recovery), []
        for run in runs:
            planned,reacquired = GameStateMonitor(self.monitor_settings).plan(run,starts,duration=duration)
            windows.extend(planned)
            barriers.extend(reacquired)
            high,armed = 0,False
            for row in run:
                high = high+1 if row.board_score >= self.monitor_settings.board_high else 0
                if high >= self.monitor_settings.board_observations:
                    armed = True
                if armed and row.board_score <= self.monitor_settings.board_low:
                    windows.append(ResultWindow(max(0.,row.timestamp-self.monitor_settings.before_s),
                        min(duration,row.timestamp+self.monitor_settings.after_s),((row.timestamp,"orphan_board_disappeared"),)))
                    armed = False
        return merge_windows(windows),sorted(set(barriers))
