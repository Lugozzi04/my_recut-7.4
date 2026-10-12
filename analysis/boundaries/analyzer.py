"""GameSegment -> local windows -> evidence -> START/END BoundaryContext. No cut decision.

Usable from CLI and GUI threads: pure Python, cancellable, no Qt. Only the
search windows are decoded; transcripts for both windows are requested in one
engine call (one model load) and cached by the engine.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
import time
from typing import Any

from analysis.performance_profile import current_profile, measure, profiled, record
from analysis.audio_service import AnalysisCancellation
from analysis.gameplay.models import GameplayAnalysisResult
from .audio import AudioEvidence, SilenceSettings, analyze_audio_window
from .candidates import FEATURE_NAMES, CandidateSettings, Evidence, generate_candidates
from .gameplay import EofStateSettings, GameplayTimeline
from .models import (BoundaryContext, BoundaryType, CandidateSource, GameDisposition, GameInfo, GameplayMarker,
                     GameTermination, TimeRange)
from .transcript import FasterWhisperEngine, NullTranscriptionEngine, TranscriptionEngine, TranscriptSettings
from .visual import VisualSettings, analyze_visual_window
from .windows import WindowSettings, search_window

ANALYZER_VERSION = "boundary-analysis-v1"


@dataclass(frozen=True)
class BoundarySettings:
    windows: WindowSettings = field(default_factory=WindowSettings)
    silence: SilenceSettings = field(default_factory=SilenceSettings.from_preset)
    transcript: TranscriptSettings = field(default_factory=TranscriptSettings)
    visual: VisualSettings = field(default_factory=VisualSettings)
    candidates: CandidateSettings = field(default_factory=CandidateSettings)
    eof: EofStateSettings = field(default_factory=EofStateSettings)
    keep_policy: str = "remove-losses"  # "wins": only complete WINs are kept (see gameplay.py)

    def to_mapping(self) -> dict[str, Any]:
        return {"analyzer_version": ANALYZER_VERSION, **asdict(self)}


@dataclass
class GameBoundaryAnalysis:
    game: GameInfo
    contexts: dict[BoundaryType, BoundaryContext]
    profiling: dict[str, float]

    def to_mapping(self) -> dict[str, Any]:
        return {"format": "recut_game_boundary_analysis", "version": 1, "game": self.game.to_mapping(),
                "contexts": {k.value: v.to_mapping() for k, v in self.contexts.items()}, "profiling": self.profiling}


class BoundaryAnalyzer:
    def __init__(self, settings: BoundarySettings | None = None, *, transcription: TranscriptionEngine | None = None,
                 sampler: Any = None) -> None:
        self.settings = settings or BoundarySettings()
        if transcription is None:
            transcription = (FasterWhisperEngine(self.settings.transcript) if self.settings.transcript.enabled
                             else NullTranscriptionEngine("disabled"))
        self.transcription = transcription
        self.sampler = sampler

    def prefetch_kept_transcripts(self, video: str | Path, analysis: GameplayAnalysisResult,
                                  game_indices: list[int], *, cancellation: AnalysisCancellation) -> None:
        """One model/helper lifetime for the WIN boundary windows of this run.

        Custom engines and disabled caches retain their original per-game contract.
        Evidence preparation below reads these same source-time cached windows.
        """
        if (not self.settings.transcript.enabled or not isinstance(self.transcription, FasterWhisperEngine)
                or not self.transcription.settings.use_cache):
            return
        timeline = GameplayTimeline(analysis, self.settings.eof, policy=self.settings.keep_policy)
        windows: list[TimeRange] = []
        for index in game_indices:
            segment = timeline.game(index)
            info = timeline.info(segment)
            if info.disposition != GameDisposition.KEEP or segment.start is None or segment.end is None:
                continue
            for kind, anchor in ((BoundaryType.START, segment.start), (BoundaryType.END, segment.end)):
                window, _ = search_window(kind, anchor, duration=timeline.duration, settings=self.settings.windows,
                                           previous_game_end=timeline.previous_game_end(segment),
                                           next_game_start=timeline.next_game_start(segment))
                windows.append(window)
        if windows:
            cancellation.raise_if_cancelled()
            with measure("boundaries.transcript"):
                self.transcription.transcribe(video, windows, cancellation=cancellation)
            record("boundaries.transcript", prefetched_windows=len(windows), prefetch_batches=1)

    @profiled("boundaries.evidence")
    def analyze_game(self, video: str | Path, analysis: GameplayAnalysisResult, game_index: int, *,
                     require_removable: bool = True, cancellation: AnalysisCancellation | None = None
                     ) -> GameBoundaryAnalysis:
        token = cancellation or AnalysisCancellation()
        profile = current_profile()
        if profile:
            profile.modes.update(transcript_enabled=self.settings.transcript.enabled,
                transcript_engine=type(self.transcription).__name__,
                transcript_model=Path(self.settings.transcript.model).name,
                transcript_device=self.settings.transcript.device,
                visual_evidence_enabled=self.settings.visual.enabled,
                visual_evidence_fps=self.settings.visual.fps)
        timeline = GameplayTimeline(analysis, self.settings.eof, policy=self.settings.keep_policy)
        segment = timeline.game(game_index)
        info = timeline.info(segment)
        if require_removable and info.disposition == GameDisposition.KEEP:
            raise ValueError(f"Game {game_index} is KEEP ({info.termination.value}, {info.result}): {info.reason}")
        duration = timeline.duration
        previous_end, next_start = timeline.previous_game_end(segment), timeline.next_game_start(segment)
        anchors = {"game_start": segment.start, "game_end": segment.end, "previous_game_end": previous_end,
                   "next_game_start": next_start, "end_of_file": duration}
        markers = timeline.markers()
        targets: list[tuple[BoundaryType, float | None, str]]
        if info.disposition == GameDisposition.KEEP:
            end_anchor = duration if info.termination == GameTermination.IN_PROGRESS_AT_EOF else segment.end
            targets = [(BoundaryType.START, segment.start, "game_start"), (BoundaryType.END, end_anchor, "game_end")]
        else:
            region = timeline.removal_anchors(segment)
            # A region starting at file start needs no START decision (cut from 0).
            targets = [] if region.start_fixed else [(BoundaryType.START, region.start, region.start_kind)]
            targets.append((BoundaryType.END, region.end, region.end_kind))
            estimated = {"estimated_board_start": CandidateSource.BOARD_REACQUIRED,
                         "estimated_board_end": CandidateSource.BOARD_DISAPPEARED}
            for anchor_t, kind_name in ((region.start, region.start_kind), (region.end, region.end_kind)):
                if kind_name in estimated:  # make the estimated anchor itself a candidate
                    markers.append(GameplayMarker(anchor_t, estimated[kind_name].value, {"estimated_anchor": True}))
        plans: list[tuple[BoundaryType, float, str, TimeRange, tuple[str, ...]]] = []
        for kind, anchor, anchor_kind in targets:
            if anchor is not None:
                window, clipped = search_window(kind, anchor, duration=duration, settings=self.settings.windows,
                                                previous_game_end=previous_end, next_game_start=next_start)
                plans.append((kind, anchor, anchor_kind, window, clipped))
        profiling: dict[str, float] = {"audio_s": 0.0, "visual_s": 0.0, "transcript_s": 0.0, "candidates_s": 0.0,
                                       "window_seconds": sum(p[3].duration for p in plans)}
        started = time.perf_counter()
        record("boundaries.evidence", windows=len(plans), source_window_seconds=sum(p[3].duration for p in plans))
        with measure("boundaries.transcript"):
            transcript_status, transcripts = self.transcription.transcribe(video, [p[3] for p in plans],
                                                                           cancellation=token)
        profile = current_profile()
        if profile:
            profile.node("boundaries.transcript")["evidence_status"] = transcript_status.status
            statuses = profile.node("boundaries.transcript").setdefault("evidence_status_counts", {})
            statuses[transcript_status.status] = statuses.get(transcript_status.status, 0) + 1
            if transcript_status.status in ("disabled", "unavailable"):
                profile.skip_unexecuted("boundaries.transcript.inference", transcript_status.status)
                profile.skip_unexecuted("boundaries.transcript.audio_extraction", transcript_status.status)
            elif transcript_status.status == "cached":
                profile.skip_unexecuted("boundaries.transcript.inference", "transcript_cache_hit")
                profile.skip_unexecuted("boundaries.transcript.audio_extraction", "transcript_cache_hit")
            elif transcript_status.status == "failed":
                profile.node("boundaries.transcript")["status"] = "failed"
                profile.node("boundaries.transcript")["error_type"] = "ASRHelperFailure"
        profiling["transcript_s"] = time.perf_counter() - started
        neighbors = timeline.neighbor_summary(segment)
        video_info = {"path": str(Path(video)), "duration": duration,
                      "fingerprint": analysis.metadata.get("video_fingerprint"),
                      "gameplay_detector": analysis.detector_version}
        contexts: dict[BoundaryType, BoundaryContext] = {}
        for (kind, anchor, anchor_kind, window, clipped), transcript in zip(plans, transcripts):
            token.raise_if_cancelled()
            started = time.perf_counter()
            with measure("boundaries.audio_evidence"):
                audio: AudioEvidence = analyze_audio_window(video, window, self.settings.silence, cancellation=token)
            record("boundaries.audio_evidence", windows=1)
            profiling["audio_s"] += time.perf_counter() - started
            started = time.perf_counter()
            with measure("boundaries.visual_evidence"):
                visual_status, visual = analyze_visual_window(video, window, self.settings.visual,
                                                              sampler=self.sampler, cancellation=token)
            record("boundaries.visual_evidence", windows=1)
            profiling["visual_s"] += time.perf_counter() - started
            started = time.perf_counter()
            with measure("boundaries.candidate_generation"):
                local_markers = [m for m in markers if window.contains(m.timestamp)]
                evidence = Evidence(kind, window, anchor, anchors, audio.status, audio.silences, audio.speech,
                                    transcript_status, transcript, visual_status, visual, local_markers,
                                    audio.mean_db if audio.rms is not None else None)
                candidates = generate_candidates(evidence, self.settings.candidates)
            record("boundaries.candidate_generation", candidates=len(candidates), windows=1)
            profiling["candidates_s"] += time.perf_counter() - started
            contexts[kind] = BoundaryContext(
                kind, video_info, info, anchors, window, clipped, transcript_status, list(transcript),
                audio.status, list(audio.silences), list(audio.speech), visual_status, visual, local_markers,
                candidates, list(FEATURE_NAMES), {"anchor": anchor, "anchor_kind": anchor_kind,
                                                  **self.settings.to_mapping()}, neighbors)
        return GameBoundaryAnalysis(info, contexts, profiling)

    def removable_games(self, analysis: GameplayAnalysisResult) -> list[GameInfo]:
        timeline = GameplayTimeline(analysis, self.settings.eof)
        return [timeline.info(game) for game in timeline.removable()]
