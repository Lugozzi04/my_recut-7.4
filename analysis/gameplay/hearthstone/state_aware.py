"""Opt-in state-aware candidate. The established global detector is unchanged."""
from __future__ import annotations

from contextlib import closing
from dataclasses import asdict
import math
from pathlib import Path
import time
from typing import Any, Iterator

from analysis.performance_profile import current_profile, measure, profiled, record
from analysis.audio_service import AnalysisCancellation
from analysis.gameplay.analyzer import GameplayAnalysisError, ProgressCallback
from analysis.gameplay.assembler import GameAssembler, consolidate_events
from analysis.gameplay.cache import GameplayCache, cache_key, video_identity
from analysis.gameplay.models import GameEvent, GameEventType, GameplayAnalysisResult
from analysis.gameplay.result_events import deduplicate_result_events
from utils.runtime_paths import cache_root
from .analyzer import HearthstoneAnalyzer, candidate_windows, default_template_pack
from .result_decision import ResultDecisionPolicy, ResultEvidence, decide_results
from .state_monitor import GameStateMonitor, MonitorObservation, MonitorSettings
from .visual.board import BoardPresenceDetector
from analysis.shared.visual import VisualEvidenceStore

from .visual.detector import TemplateVisualDetector, normalize_frame
from .visual.result_text import ResultTextDetector
from .visual.sampler import FFmpegFrameSampler, FrameSampler
from .visual.state_profile import StateAwareProfile
from .visual.templates import TemplateRepository


ANALYZER_VERSION = 'hearthstone-state-aware-v2'


class StateAwareHearthstoneAnalyzer:
    """One low-rate pass, unchanged local VS refinement, bounded high-rate ROIs.

    Board/weak evidence only schedules searches. Only distinct high-rate source
    frames can support a result event: a class-agnostic cluster confirms the
    result screen, then its peak frame decides WIN/LOSS (result_decision.py).
    Neither starts nor outcomes are inferred.
    """

    def __init__(self, *, state_profile: str | Path, template_pack: str | Path | None = None,
                 sampler: FrameSampler | None = None, monitor_settings: MonitorSettings | None = None,
                 result_policy: ResultDecisionPolicy | None = None,
                 use_cache: bool = True, cache_directory: str | Path | None = None) -> None:
        self.result_policy = result_policy or ResultDecisionPolicy()
        self.profile = StateAwareProfile(state_profile)
        self.monitor_settings = monitor_settings or MonitorSettings(**self.profile.config.get('monitor', {}))
        self.vs = TemplateVisualDetector(TemplateRepository(template_pack or default_template_pack()).load())
        if self.vs.pack.reference_size != self.profile.reference_size or self.vs.pack.viewport != self.profile.viewport:
            raise ValueError('VS and state profile must share reference size and viewport')
        self.board = BoardPresenceDetector(self.profile)
        self.text = ResultTextDetector(self.profile)
        self.sampler = sampler or FFmpegFrameSampler()
        if not callable(getattr(self.sampler, 'sample_roi', None)):
            raise ValueError('State-aware analysis requires a native ROI sampler')
        self.cache = GameplayCache(cache_directory or cache_root() / 'gameplay' / 'hearthstone' / 'state-aware') if use_cache else None

    analyzer_version = ANALYZER_VERSION

    def _scan_monitor(self, video: Path, token: AnalysisCancellation, measured: Any, progress: Any,
                      duration: float, profiling: dict[str, Any], comparisons: dict[str, int]
                      ) -> tuple[list[GameEvent], list[MonitorObservation], list[dict[str, Any]], int]:
        coarse: list[GameEvent] = []
        rows: list[MonitorObservation] = []
        evidence: list[dict[str, Any]] = []
        coarse_frames = 0
        phase = 'monitor'
        next_vs = 0.0
        with measure("monitor"):
            with closing(self.sampler.sample(video, fps=self.monitor_settings.fps,
                                            max_width=self.profile.reference_size[0], cancellation=token)) as frames:
                for frame in measured(frames):
                    before = time.perf_counter()
                    with measure("monitor.image_preparation"):
                        normalized = normalize_frame(frame.image, self.profile.reference_size, self.profile.viewport)
                        gray = self.board.cv.cvtColor(normalized, self.board.cv.COLOR_BGR2GRAY)
                    with measure("monitor.board"):
                        board_score, anchors = self.board.score(gray)
                    record("monitor.board", comparisons=self.board.comparisons_per_frame, frames=1)
                    left, top, right, bottom = self.profile.result_roi.pixel_bounds(*self.profile.reference_size)
                    with measure("monitor.result_matching"):
                        result_evidence = self.text.score_roi(gray[top:bottom, left:right])
                    record("monitor.result_matching", comparisons=len(self.text.templates), frames=1)
                    record("monitor", weak_result_candidate_frames=int(
                        result_evidence["score"] >= self.monitor_settings.weak_score
                        and result_evidence["margin"] >= self.monitor_settings.weak_margin),
                        board_below_low_frames=int(board_score <= self.monitor_settings.board_low))
                    rows.append(MonitorObservation(frame.timestamp, board_score, result_evidence['score'], result_evidence['margin']))
                    evidence.append({'timestamp': frame.timestamp, 'board_score': board_score, 'anchors': anchors,
                                     'result_scores': result_evidence['scores'], 'result_margin': result_evidence['margin']})
                    comparisons['board'] += self.board.comparisons_per_frame
                    comparisons['text_monitor'] += len(self.text.templates)
                    if frame.timestamp + 1e-7 >= next_vs:
                        coarse_frames += 1
                        candidate_count = len(coarse)
                        with measure("monitor.vs"):
                            if (self.vs.pack.reference_size == self.profile.reference_size
                                    and self.vs.pack.viewport == self.profile.viewport):
                                coarse.extend(self.vs.detect_reference_candidates(gray, frame.timestamp,
                                              event_types=(GameEventType.VS_SCREEN,), minimum_similarity=0.80))
                            else:
                                coarse.extend(self.vs.detect_candidates(frame.image, frame.timestamp,
                                              event_types=(GameEventType.VS_SCREEN,), minimum_similarity=0.80))
                        record("monitor.vs", frames=1, candidates=len(coarse) - candidate_count)
                        next_vs = frame.timestamp + 1.0
                    if len(rows) > 100000:
                        raise GameplayAnalysisError('Too many monitor frames', code='candidate_limit')
                    profiling[phase]['matching_seconds'] += time.perf_counter() - before
                    progress(60 * frame.timestamp / duration)
        return coarse, rows, evidence, coarse_frames

    def _plan_result_windows(self, rows: list[MonitorObservation], starts: list[GameEvent], duration: float) -> Any:
        return GameStateMonitor(self.monitor_settings).plan(rows, starts, duration=duration)

    def _refinement_diagnostics(self, starts: list[GameEvent], window_evidence: Any, duration: float) -> None:
        pass

    def _confirm_result_decisions(self, video: Path, decisions: list[Any], token: AnalysisCancellation) -> list[Any]:
        """Extension point for experimental backends; the state-aware detector keeps its decisions."""
        return decisions

    def _extra_settings(self) -> dict[str, Any]:
        return {}

    def _extra_metadata(self) -> dict[str, Any]:
        return {}

    @profiled("detection")
    def analyze(self, video_path: str | Path, *, cancellation: AnalysisCancellation | None = None,
                on_progress: ProgressCallback | None = None, debug_frames: str | Path | None = None,
                max_debug_frames: int = 100) -> GameplayAnalysisResult:
        token = cancellation or AnalysisCancellation()
        token.raise_if_cancelled()
        if type(max_debug_frames) is not int or max_debug_frames < 0:
            raise ValueError('max_debug_frames must be a nonnegative integer')
        video = Path(video_path).expanduser().resolve()
        profile = current_profile()
        if profile:
            profile.node("detection")["cache"] = "pending" if self.cache is not None else "disabled"
            profile.modes.update({"monitor_fps": self.monitor_settings.fps,
                                  "result_window_fps": self.monitor_settings.high_fps,
                                  "detector_version": self.analyzer_version,
                                  "state_profile_fingerprint": self.profile.fingerprint,
                                  "vs_pack_fingerprint": self.vs.pack_fingerprint,
                                  "opencv_version": str(self.text.cv.__version__),
                                  "monitor_settings": asdict(self.monitor_settings),
                                  "result_policy": self.result_policy.to_mapping()})
        with measure("detection.identity_probe"):
            identity = video_identity(video)
            info = self.sampler.probe(video, cancellation=token)
        if profile:
            profile.video.update(duration_s=info.duration_s, width=info.width, height=info.height)
        if not math.isfinite(info.duration_s) or info.duration_s <= 0:
            raise GameplayAnalysisError('Invalid video duration', code='invalid_video')
        detector_info = {'state_profile': self.profile.describe(), 'vs': self.vs.describe()}
        settings = {'analyzer_version': self.analyzer_version, 'monitor': asdict(self.monitor_settings),
                    'vs_coarse_fps': 1.0, 'vs_fine_fps': 6.0, 'vs_padding_s': 2.0,
                    'vs_weak_score': 0.80, 'vs_minimum_observations': 3, 'vs_minimum_span_s': 0.25,
                    'result_decision': self.result_policy.to_mapping(), 'result_episode_gap_s': 45.0, 'single_frame_policy': 'disabled',
                    'maximum_game_duration_s': 1800.0, 'detector': detector_info}
        settings.update(self._extra_settings())
        key = cache_key(identity, detector_info, settings)
        previous_progress = -1

        def progress(value: float) -> None:
            nonlocal previous_progress
            token.raise_if_cancelled()
            value = max(previous_progress, min(100, int(value)))
            if value > previous_progress:
                previous_progress = value
                if on_progress is not None:
                    on_progress(value)
            token.raise_if_cancelled()

        progress(0)
        if self.cache is not None and debug_frames is None:
            with measure("detection.cache_lookup"):
                cached = VisualEvidenceStore.gameplay(self.cache, key)
            if (cached is not None and cached.settings == settings and cached.video_path == str(video)
                    and cached.duration == info.duration_s and video_identity(video) == identity):
                cached.metadata['cache_hit'] = True
                if profile:
                    profile.cache('detection', 'hit')
                    for name in ('monitor', 'vs_refinement', 'result_windows', 'event_consolidation', 'game_assembly'):
                        profile.skip(name, 'detector_cache_hit')
                    profile.record('detection', cached_games=len(cached.games), cached_events=len(cached.events),
                                   cached_wins=sum(g.result.value == 'WIN' for g in cached.games),
                                   cached_losses=sum(g.result.value == 'LOSS' for g in cached.games))
                progress(100)
                return cached
        if profile and self.cache is not None:
            profile.cache("detection", "miss")
        started = time.perf_counter()
        profiling = {name: {'sampling_wait_seconds': 0.0, 'matching_seconds': 0.0, 'streams': 0}
                     for name in ('monitor', 'vs_refinement', 'result_windows')}
        phase = 'monitor'

        def measured(frames: Any) -> Iterator[Any]:
            profiling[phase]['streams'] += 1
            iterator = iter(frames)
            while True:
                before = time.perf_counter()
                try:
                    with measure(f"{phase}.frame_delivery_wait"):
                        frame = next(iterator)
                except StopIteration:
                    profiling[phase]['sampling_wait_seconds'] += time.perf_counter() - before
                    return
                profiling[phase]['sampling_wait_seconds'] += time.perf_counter() - before
                token.raise_if_cancelled()
                record(phase, frames=1)
                if profile:
                    profile.checkpoint()
                yield frame

        coarse: list[GameEvent] = []
        rows: list[MonitorObservation] = []
        evidence: list[dict[str, Any]] = []
        comparisons = {'board': 0, 'text_monitor': 0, 'text_windows': 0}
        coarse_frames = fine_frames = result_frames = debug_saved = 0
        coarse, rows, evidence, coarse_frames = self._scan_monitor(
            video, token, measured, progress, info.duration_s, profiling, comparisons)
        vs_observations: list[GameEvent] = []
        vs_windows = candidate_windows(coarse, duration=info.duration_s, padding=2.0)
        if hasattr(self, 'details'):
            self.details['vs_candidate_windows'] = [list(w) for w in vs_windows]
        phase = 'vs_refinement'
        with measure("vs_refinement"):
            for index, (start, end) in enumerate(vs_windows):
                record("vs_refinement", windows=1)
                with closing(self.sampler.sample(video, fps=6.0, start_s=start, end_s=end,
                                                max_width=self.profile.reference_size[0], cancellation=token)) as frames:
                    for frame in measured(frames):
                        fine_frames += 1
                        before = time.perf_counter()
                        previous_count = len(vs_observations)
                        with measure("vs_refinement.vs"):
                            vs_observations.extend(self.vs.detect(frame.image, frame.timestamp, event_types=(GameEventType.VS_SCREEN,)))
                        record("vs_refinement.vs", frames=1, candidates=len(vs_observations) - previous_count)
                        profiling[phase]['matching_seconds'] += time.perf_counter() - before
                        if len(vs_observations) > 100000:
                            raise GameplayAnalysisError('Too many VS observations', code='candidate_limit')
                record("vs_refinement", completed_windows=1)
                progress(60 + 5 * (index + 1) / len(vs_windows))
        if profile and not vs_windows:
            profile.skip("vs_refinement", "no_candidate_windows")
        with measure("window_planning"):
            starts = [event for event in consolidate_events(vs_observations, max_gap_s=0.5)
                      if event.metadata['observation_count'] >= 3 and event.metadata['temporal_span_s'] + 1e-9 >= 0.25]
            windows, reacquisitions = self._plan_result_windows(rows, starts, info.duration_s)
        record("window_planning", start_events=len(starts), result_windows=len(windows),
               board_reacquisitions=len(reacquisitions))
        phase = 'result_windows'
        window_evidence: list[ResultEvidence] = []
        roi = self.profile.result_roi
        viewport = self.profile.viewport
        source_roi = (viewport.x + roi.x * viewport.width, viewport.y + roi.y * viewport.height,
                      roi.width * viewport.width, roi.height * viewport.height)
        roi_sample: Any = getattr(self.sampler, 'sample_roi')
        total_window_seconds = sum(window.end - window.start for window in windows)
        processed_seconds = 0.0
        with measure("result_windows"):
            for window in windows:
                record("result_windows", windows=1, source_window_seconds=window.end - window.start)
                with closing(roi_sample(video, roi=source_roi, reference_size=self.profile.reference_size,
                                       fps=self.monitor_settings.high_fps, start_s=window.start, end_s=window.end,
                                       output_size=self.text.output_size, cancellation=token)) as frames:
                    for frame in measured(frames):
                        result_frames += 1
                        before = time.perf_counter()
                        with measure("result_windows.result_matching"):
                            scores = self.text.score_roi(frame.image)
                        record("result_windows.result_matching", comparisons=len(self.text.templates), frames=1)
                        comparisons['text_windows'] += len(self.text.templates)
                        profiling[phase]['matching_seconds'] += time.perf_counter() - before
                        if scores['score'] >= self.result_policy.evidence_score:
                            window_evidence.append(ResultEvidence.from_scores(frame.timestamp, scores))
                            record("result_windows", candidates=1)
                            if debug_frames is not None and debug_saved < max_debug_frames and scores['margin'] > 0 and (
                                    debug_saved == 0 or result_frames % 10 == 0):
                                # Save bounded samples; frame data never enters the job/project model.
                                HearthstoneAnalyzer._save_frame(Path(debug_frames), frame.image, GameEvent(
                                    frame.timestamp, GameEventType(scores['winner']), scores['score']), debug_saved)
                                debug_saved += 1
                        if len(window_evidence) > 100000:
                            raise GameplayAnalysisError('Too many result observations', code='candidate_limit')
                        progress(65 + 34 * (processed_seconds + max(0.0, frame.timestamp-window.start)) /
                                 max(total_window_seconds, 1e-9))
                record("result_windows", completed_windows=1)
                processed_seconds += window.end - window.start
        if profile and not windows:
            profile.skip("result_windows", "no_trigger_windows")
        self._refinement_diagnostics(vs_observations, window_evidence, info.duration_s)
        with measure("event_consolidation"):
            decisions = self._confirm_result_decisions(video, decide_results(window_evidence, self.result_policy), token)
            static = {'threshold': self.result_policy.evidence_score, 'representation': self.text.method,
                      'roi': self.profile.result_roi.to_list(), 'similarity_method': 'TM_CCOEFF_NORMED',
                      'detector_version': self.text.detector_version}
            accepted = [event for event in (decision.to_event(static) for decision in decisions) if event is not None]
            rejected = [decision.to_mapping() for decision in decisions if not decision.classified]
            events = deduplicate_result_events([*starts, *accepted], barriers=reacquisitions, maximum_gap_s=45.0)
        record("event_consolidation", input_candidates=len(window_evidence), output_events=len(events),
               rejected_clusters=len(rejected), accepted_result_clusters=len(accepted))
        with measure("game_assembly"):
            games = GameAssembler(maximum_game_duration_s=1800.0).assemble(events)
        record("game_assembly", games=len(games), complete_games=sum(g.complete for g in games),
               wins=sum(g.result.value == "WIN" for g in games),
               losses=sum(g.result.value == "LOSS" for g in games),
               unknown=sum(g.result.value == "UNKNOWN" for g in games))
        token.raise_if_cancelled()
        if video_identity(video) != identity:
            raise GameplayAnalysisError('Source changed during analysis', code='source_changed')
        result = GameplayAnalysisResult(str(video), info.duration_s, events, games, self.analyzer_version, settings=settings,
            metadata={'analyzer_version': self.analyzer_version, 'strategy': 'state-aware-candidate',
                      'cache_key': key, 'cache_hit': False, 'video_fingerprint': identity['fingerprint'],
                      'monitor_frames': len(rows), 'coarse_frames': coarse_frames, 'fine_frames': fine_frames,
                      'result_window_frames': result_frames, 'dense_result_frames': 0,
                      'result_windows': [{'start': window.start, 'end': window.end, 'triggers': list(window.triggers)} for window in windows],
                      'result_window_seconds': total_window_seconds, 'board_reacquisitions': reacquisitions,
                      'monitor_evidence': evidence, 'comparisons': comparisons, 'profiling': profiling,
                      'result_clusters_before_deduplication': len(accepted), 'rejected_events': rejected,
                      'result_decisions': [decision.to_mapping() for decision in decisions],
                      'result_evidence_frames': len(window_evidence),
                      'elapsed_seconds': time.perf_counter() - started, 'debug_frames_saved': debug_saved,
                       **self._extra_metadata()},
            warnings=['Experimental state-aware strategy; template similarity is not a calibrated probability. No cuts generated.'])
        if any(not game.complete for game in games):
            result.warnings.append('Some boundaries are missing; no timestamps were inferred.')
        if self.cache is not None:
            try:
                with measure('detection.cache_write'):
                    self.cache.save(key, result)
            except OSError as exc:
                result.warnings.append(f'Analysis succeeded, but cache could not be written: {exc}')
        progress(100)
        return result
