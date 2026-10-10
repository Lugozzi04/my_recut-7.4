"""Opt-in state-aware candidate. The established global detector is unchanged."""
from __future__ import annotations

from contextlib import closing
from dataclasses import asdict
import math
from pathlib import Path
import time
from typing import Any, Iterator

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

    def analyze(self, video_path: str | Path, *, cancellation: AnalysisCancellation | None = None,
                on_progress: ProgressCallback | None = None, debug_frames: str | Path | None = None,
                max_debug_frames: int = 100) -> GameplayAnalysisResult:
        token = cancellation or AnalysisCancellation()
        token.raise_if_cancelled()
        if type(max_debug_frames) is not int or max_debug_frames < 0:
            raise ValueError('max_debug_frames must be a nonnegative integer')
        video = Path(video_path).expanduser().resolve()
        identity = video_identity(video)
        info = self.sampler.probe(video, cancellation=token)
        if not math.isfinite(info.duration_s) or info.duration_s <= 0:
            raise GameplayAnalysisError('Invalid video duration', code='invalid_video')
        detector_info = {'state_profile': self.profile.describe(), 'vs': self.vs.describe()}
        settings = {'analyzer_version': ANALYZER_VERSION, 'monitor': asdict(self.monitor_settings),
                    'vs_coarse_fps': 1.0, 'vs_fine_fps': 6.0, 'vs_padding_s': 2.0,
                    'vs_weak_score': 0.80, 'vs_minimum_observations': 3, 'vs_minimum_span_s': 0.25,
                    'result_decision': self.result_policy.to_mapping(), 'result_episode_gap_s': 45.0, 'single_frame_policy': 'disabled',
                    'maximum_game_duration_s': 1800.0, 'detector': detector_info}
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
            cached = self.cache.load(key)
            if (cached is not None and cached.settings == settings and cached.video_path == str(video)
                    and cached.duration == info.duration_s and video_identity(video) == identity):
                cached.metadata['cache_hit'] = True
                progress(100)
                return cached
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
                    frame = next(iterator)
                except StopIteration:
                    profiling[phase]['sampling_wait_seconds'] += time.perf_counter() - before
                    return
                profiling[phase]['sampling_wait_seconds'] += time.perf_counter() - before
                token.raise_if_cancelled()
                yield frame

        coarse: list[GameEvent] = []
        rows: list[MonitorObservation] = []
        evidence: list[dict[str, Any]] = []
        comparisons = {'board': 0, 'text_monitor': 0, 'text_windows': 0}
        coarse_frames = fine_frames = result_frames = debug_saved = 0
        next_vs = 0.0
        with closing(self.sampler.sample(video, fps=self.monitor_settings.fps,
                                        max_width=self.profile.reference_size[0], cancellation=token)) as frames:
            for frame in measured(frames):
                before = time.perf_counter()
                normalized = normalize_frame(frame.image, self.profile.reference_size, self.profile.viewport)
                board_score, anchors = self.board.score(normalized)
                left, top, right, bottom = self.profile.result_roi.pixel_bounds(*self.profile.reference_size)
                result_evidence = self.text.score_roi(normalized[top:bottom, left:right])
                rows.append(MonitorObservation(frame.timestamp, board_score, result_evidence['score'], result_evidence['margin']))
                evidence.append({'timestamp': frame.timestamp, 'board_score': board_score, 'anchors': anchors,
                                 'result_scores': result_evidence['scores'], 'result_margin': result_evidence['margin']})
                comparisons['board'] += self.board.comparisons_per_frame
                comparisons['text_monitor'] += len(self.text.templates)
                if frame.timestamp + 1e-7 >= next_vs:
                    coarse_frames += 1
                    coarse.extend(self.vs.detect_candidates(frame.image, frame.timestamp,
                                  event_types=(GameEventType.VS_SCREEN,), minimum_similarity=0.80))
                    next_vs = frame.timestamp + 1.0
                if len(rows) > 100000:
                    raise GameplayAnalysisError('Too many monitor frames', code='candidate_limit')
                profiling[phase]['matching_seconds'] += time.perf_counter() - before
                progress(60 * frame.timestamp / info.duration_s)
        vs_observations: list[GameEvent] = []
        vs_windows = candidate_windows(coarse, duration=info.duration_s, padding=2.0)
        phase = 'vs_refinement'
        for index, (start, end) in enumerate(vs_windows):
            with closing(self.sampler.sample(video, fps=6.0, start_s=start, end_s=end,
                                            max_width=self.profile.reference_size[0], cancellation=token)) as frames:
                for frame in measured(frames):
                    fine_frames += 1
                    before = time.perf_counter()
                    vs_observations.extend(self.vs.detect(frame.image, frame.timestamp, event_types=(GameEventType.VS_SCREEN,)))
                    profiling[phase]['matching_seconds'] += time.perf_counter() - before
                    if len(vs_observations) > 100000:
                        raise GameplayAnalysisError('Too many VS observations', code='candidate_limit')
            progress(60 + 5 * (index + 1) / len(vs_windows))
        starts = [event for event in consolidate_events(vs_observations, max_gap_s=0.5)
                  if event.metadata['observation_count'] >= 3 and event.metadata['temporal_span_s'] + 1e-9 >= 0.25]
        windows, reacquisitions = GameStateMonitor(self.monitor_settings).plan(rows, starts, duration=info.duration_s)
        phase = 'result_windows'
        window_evidence: list[ResultEvidence] = []
        roi = self.profile.result_roi
        viewport = self.profile.viewport
        source_roi = (viewport.x + roi.x * viewport.width, viewport.y + roi.y * viewport.height,
                      roi.width * viewport.width, roi.height * viewport.height)
        roi_sample: Any = getattr(self.sampler, 'sample_roi')
        total_window_seconds = sum(window.end - window.start for window in windows)
        processed_seconds = 0.0
        for window in windows:
            with closing(roi_sample(video, roi=source_roi, reference_size=self.profile.reference_size,
                                   fps=self.monitor_settings.high_fps, start_s=window.start, end_s=window.end,
                                   output_size=self.text.output_size, cancellation=token)) as frames:
                for frame in measured(frames):
                    result_frames += 1
                    before = time.perf_counter()
                    scores = self.text.score_roi(frame.image)
                    comparisons['text_windows'] += len(self.text.templates)
                    profiling[phase]['matching_seconds'] += time.perf_counter() - before
                    if scores['score'] >= self.result_policy.evidence_score:
                        window_evidence.append(ResultEvidence.from_scores(frame.timestamp, scores))
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
            processed_seconds += window.end - window.start
        decisions = decide_results(window_evidence, self.result_policy)
        static = {'threshold': self.result_policy.evidence_score, 'representation': self.text.method,
                  'roi': self.profile.result_roi.to_list(), 'similarity_method': 'TM_CCOEFF_NORMED',
                  'detector_version': self.text.detector_version}
        accepted = [event for event in (decision.to_event(static) for decision in decisions) if event is not None]
        rejected = [decision.to_mapping() for decision in decisions if not decision.classified]
        events = deduplicate_result_events([*starts, *accepted], barriers=reacquisitions, maximum_gap_s=45.0)
        games = GameAssembler(maximum_game_duration_s=1800.0).assemble(events)
        token.raise_if_cancelled()
        if video_identity(video) != identity:
            raise GameplayAnalysisError('Source changed during analysis', code='source_changed')
        result = GameplayAnalysisResult(str(video), info.duration_s, events, games, ANALYZER_VERSION, settings=settings,
            metadata={'analyzer_version': ANALYZER_VERSION, 'strategy': 'state-aware-candidate',
                      'cache_key': key, 'cache_hit': False, 'video_fingerprint': identity['fingerprint'],
                      'monitor_frames': len(rows), 'coarse_frames': coarse_frames, 'fine_frames': fine_frames,
                      'result_window_frames': result_frames, 'dense_result_frames': 0,
                      'result_windows': [{'start': window.start, 'end': window.end, 'triggers': list(window.triggers)} for window in windows],
                      'result_window_seconds': total_window_seconds, 'board_reacquisitions': reacquisitions,
                      'monitor_evidence': evidence, 'comparisons': comparisons, 'profiling': profiling,
                      'result_clusters_before_deduplication': len(accepted), 'rejected_events': rejected,
                      'result_decisions': [decision.to_mapping() for decision in decisions],
                      'result_evidence_frames': len(window_evidence),
                      'elapsed_seconds': time.perf_counter() - started, 'debug_frames_saved': debug_saved},
            warnings=['Experimental state-aware strategy; template similarity is not a calibrated probability. No cuts generated.'])
        if any(not game.complete for game in games):
            result.warnings.append('Some boundaries are missing; no timestamps were inferred.')
        if self.cache is not None:
            try:
                self.cache.save(key, result)
            except OSError as exc:
                result.warnings.append(f'Analysis succeeded, but cache could not be written: {exc}')
        progress(100)
        return result
