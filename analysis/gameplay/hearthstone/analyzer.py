from __future__ import annotations

from collections.abc import Iterable
from contextlib import closing
from dataclasses import asdict, dataclass
import math
import os
from pathlib import Path
import tempfile
import time
from typing import Any

from analysis.audio_service import AnalysisCancellation
from analysis.gameplay.analyzer import GameplayAnalysisError, ProgressCallback
from analysis.gameplay.assembler import GameAssembler, consolidate_events
from analysis.gameplay.cache import GameplayCache, cache_key, video_identity
from analysis.gameplay.models import GameEvent, GameEventType, GameplayAnalysisResult
from analysis.gameplay.hearthstone.visual.detector import TemplateVisualDetector, VisualEventDetector
from analysis.gameplay.hearthstone.visual.sampler import FFmpegFrameSampler, FrameSampler
from analysis.gameplay.hearthstone.visual.templates import NormalizedROI, TemplateRepository
from export.output_safety import atomic_promote_output
from utils.runtime_paths import cache_root, config_root, resource_path


ANALYZER_VERSION = "hearthstone-visual-v3"


@dataclass(frozen=True)
class HearthstoneSettings:
    coarse_fps: float = 1.0
    fine_fps: float = 6.0
    fine_padding_s: float = 2.0
    result_fps: float = 20.0
    minimum_result_observations: int = 2
    minimum_result_stability_s: float = 1 / 120
    result_refine_fps: float = 120.0
    result_refine_padding_s: float = 0.15
    max_width: int = 960
    event_gap_s: float = 0.5
    minimum_observations: int = 3
    minimum_stability_s: float = 0.25
    max_observations: int = 100_000

    def __post_init__(self) -> None:
        for name in ("coarse_fps", "fine_fps", "fine_padding_s", "event_gap_s", "minimum_stability_s", "minimum_result_stability_s", "result_refine_padding_s"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if self.coarse_fps > 10 or self.fine_fps > 30 or self.fine_fps < self.coarse_fps:
            raise ValueError("Use coarse_fps <= 10 and coarse_fps <= fine_fps <= 30")
        if (isinstance(self.result_fps, bool) or not isinstance(self.result_fps, (int, float))
                or not math.isfinite(self.result_fps) or not 0 <= self.result_fps <= 60):
            raise ValueError("result_fps must be finite and within 0..60 (0 selects legacy diagnostics)")
        if (isinstance(self.result_refine_fps, bool) or not isinstance(self.result_refine_fps, (int, float))
                or not math.isfinite(self.result_refine_fps) or not 0 <= self.result_refine_fps <= 120):
            raise ValueError("result_refine_fps must be finite and within 0..120")
        for name in ("max_width", "minimum_observations", "minimum_result_observations", "max_observations"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if not 16 <= self.max_width <= 4096 or self.minimum_observations < 2 or self.minimum_result_observations < 2:
            raise ValueError("max_width must be 16..4096 and minimum_observations at least 2")


def default_template_pack() -> Path:
    custom = config_root() / "gameplay" / "hearthstone" / "templates.json"
    return custom if custom.is_file() else resource_path("resources", "gameplay", "hearthstone", "templates.json")


def candidate_windows(events: Iterable[GameEvent], duration: float, padding: float) -> list[tuple[float, float]]:
    intervals = sorted((max(0.0, event.timestamp - padding), min(duration, event.timestamp + padding)) for event in events)
    merged: list[tuple[float, float]] = []
    for start, end in intervals:
        if start >= end:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


class HearthstoneAnalyzer:
    """Sample -> visual observations -> temporal events -> games. Never produces cuts."""

    def __init__(
        self, *, detector: VisualEventDetector | None = None, sampler: FrameSampler | None = None,
        settings: HearthstoneSettings | None = None, template_pack: str | Path | None = None,
        use_cache: bool = True, cache_directory: str | Path | None = None,
    ) -> None:
        self.settings = settings or HearthstoneSettings()
        self.detector = detector or TemplateVisualDetector(TemplateRepository(template_pack or default_template_pack()).load())
        self.sampler = sampler or FFmpegFrameSampler()
        self.cache = GameplayCache(cache_directory or cache_root() / "gameplay" / "hearthstone") if use_cache else None

    def analyze(
        self, video_path: str | Path, *, cancellation: AnalysisCancellation | None = None,
        on_progress: ProgressCallback | None = None, debug_frames: str | Path | None = None,
        max_debug_frames: int = 100,
    ) -> GameplayAnalysisResult:
        token = cancellation or AnalysisCancellation()
        token.raise_if_cancelled()
        if isinstance(max_debug_frames, bool) or not isinstance(max_debug_frames, int) or max_debug_frames < 0:
            raise ValueError("max_debug_frames must be a nonnegative integer")
        video = Path(video_path).expanduser().resolve()
        identity = video_identity(video)
        info = self.sampler.probe(video, cancellation=token)
        token.raise_if_cancelled()
        if not math.isfinite(info.duration_s) or info.duration_s <= 0:
            raise GameplayAnalysisError("Video duration must be finite and positive", code="invalid_video")
        detector_info = self.detector.describe()
        # Injected older detector/sampler protocols retain their original path.
        # The production template detector advertises the additional ROI API.
        roi_factory: Any = getattr(self.detector, "result_scan_roi", None)
        roi_detect: Any = getattr(self.detector, "detect_roi", None)
        roi_sample: Any = getattr(self.sampler, "sample_roi", None)
        dense = self.settings.result_fps > 0 and all(callable(item) for item in (roi_factory, roi_detect, roi_sample))
        settings = {"analyzer_version": ANALYZER_VERSION, **asdict(self.settings), "detector": detector_info}
        settings["sampling_strategy"] = "coarse_vs_dense_result_roi" if dense else "legacy_coarse_to_fine"
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
            if (cached is not None and cached.video_path == str(video) and cached.duration == info.duration_s
                    and cached.settings == settings and cached.detector_version == self.detector.detector_version
                    and video_identity(video) == identity):
                cached.metadata["cache_hit"] = True
                progress(100)
                return cached
        started = time.perf_counter()
        coarse_end, fine_end = (40, 45) if dense else (75, 95)
        selected_detect: Any = self.detector.detect
        vs_types = (GameEventType.VS_SCREEN,)
        result_types = (GameEventType.VICTORY, GameEventType.DEFEAT)
        coarse: list[GameEvent] = []
        coarse_frames = fine_frames = dense_frames = debug_saved = 0
        consolidated: list[GameEvent] = []
        dropped = 0
        rejected_events: list[dict[str, Any]] = []
        refine_frames = 0
        refine_windows: list[tuple[float, float]] = []
        last_debug: dict[str, float] = {}

        def debug_matches(image: Any, matches: list[GameEvent]) -> None:
            nonlocal debug_saved
            if debug_frames is None:
                return
            for event in matches:
                if debug_saved >= max_debug_frames:
                    break
                label = event.type.value.lower()
                if event.timestamp - last_debug.get(label, -math.inf) < 0.5:
                    continue
                self._save_frame(Path(debug_frames), image, event, debug_saved)
                debug_saved += 1
                last_debug[label] = event.timestamp

        def accept_clusters(observations: list[GameEvent], *, fps: float, brief: bool = False) -> None:
            nonlocal dropped
            minimum_count = self.settings.minimum_result_observations if brief else self.settings.minimum_observations
            minimum_span = self.settings.minimum_result_stability_s if brief else self.settings.minimum_stability_s
            for event in consolidate_events(observations, max_gap_s=self.settings.event_gap_s):
                count = int(event.metadata.get("observation_count", 1))
                span = float(event.metadata.get("last_seen", event.timestamp)) - float(event.metadata.get("first_seen", event.timestamp))
                if count >= minimum_count and span + 1e-9 >= minimum_span:
                    event.metadata["temporal_span_s"] = span
                    event.metadata["sampling_fps"] = fps
                    consolidated.append(event)
                else:
                    dropped += 1
                    rejected_events.append(event.to_mapping())

        with closing(self.sampler.sample(
            video, fps=self.settings.coarse_fps, max_width=self.settings.max_width, cancellation=token,
        )) as frames:
            for frame in frames:
                token.raise_if_cancelled()
                coarse_frames += 1
                matches = selected_detect(frame.image, frame.timestamp, event_types=vs_types) if dense else selected_detect(frame.image, frame.timestamp)
                coarse.extend(matches)
                if len(coarse) > self.settings.max_observations:
                    raise GameplayAnalysisError("Too many visual candidates; narrow ROI or recalibrate templates", code="candidate_limit")
                progress(coarse_end * min(1.0, frame.timestamp / info.duration_s))
        coarse_seconds = time.perf_counter() - started
        token.raise_if_cancelled()
        windows = candidate_windows(coarse, info.duration_s, self.settings.fine_padding_s)
        progress(coarse_end)
        total_fine = sum(end - start for start, end in windows)
        done_fine = 0.0
        fine_started = time.perf_counter()
        for start, end in windows:
            observations: list[GameEvent] = []
            with closing(self.sampler.sample(
                video, fps=self.settings.fine_fps, start_s=start, end_s=end,
                max_width=self.settings.max_width, cancellation=token,
            )) as frames:
                for frame in frames:
                    token.raise_if_cancelled()
                    fine_frames += 1
                    matches = selected_detect(frame.image, frame.timestamp, event_types=vs_types) if dense else selected_detect(frame.image, frame.timestamp)
                    observations.extend(matches)
                    if len(observations) > self.settings.max_observations:
                        raise GameplayAnalysisError("Too many fine-scan detections; recalibrate templates", code="candidate_limit")
                    debug_matches(frame.image, matches)
                    progress(coarse_end + (fine_end - coarse_end) * min(1.0, (done_fine + max(0.0, frame.timestamp - start)) / max(total_fine, 1e-9)))
            accept_clusters(observations, fps=self.settings.fine_fps)
            done_fine += end - start
        fine_seconds = time.perf_counter() - fine_started
        progress(fine_end)
        dense_started = time.perf_counter()
        dense_geometry: dict[str, Any] | None = None
        if dense:
            roi: NormalizedROI = roi_factory()
            reference = tuple(detector_info["reference_size"])
            viewport = NormalizedROI.from_value(detector_info.get("viewport", [0, 0, 1, 1]))
            left, top, right, bottom = roi.pixel_bounds(*reference)
            # Round at reference geometry first, then map through the viewport.
            # This avoids a one-pixel phase shift for fractional normalized ROIs.
            source_roi = (
                viewport.x + left / reference[0] * viewport.width,
                viewport.y + top / reference[1] * viewport.height,
                (right - left) / reference[0] * viewport.width,
                (bottom - top) / reference[1] * viewport.height,
            )
            output_size = (right - left, bottom - top)
            dense_geometry = {"roi": roi.to_list(), "source_roi": list(source_roi),
                              "reference_size": list(reference), "output_size": list(output_size)}
            observations = []
            with closing(roi_sample(
                video, roi=source_roi, reference_size=reference, output_size=output_size,
                fps=self.settings.result_fps, cancellation=token,
            )) as frames:
                for frame in frames:
                    token.raise_if_cancelled()
                    dense_frames += 1
                    matches = roi_detect(frame.image, frame.timestamp, roi=roi, event_types=result_types)
                    observations.extend(matches)
                    if len(observations) > self.settings.max_observations:
                        raise GameplayAnalysisError("Too many dense-scan detections; recalibrate templates", code="candidate_limit")
                    debug_matches(frame.image, matches)
                    progress(fine_end + (90 - fine_end) * min(1.0, frame.timestamp / info.duration_s))
            # Only unstable dense candidates trigger native-rate local decoding.
            # Repeated source timestamps are deduplicated: replay is not support.
            if self.settings.result_refine_fps > 0:
                unstable = [event for event in consolidate_events(observations, max_gap_s=self.settings.event_gap_s)
                            if int(event.metadata["observation_count"]) < self.settings.minimum_result_observations
                            or float(event.metadata["temporal_span_s"]) + 1e-9 < self.settings.minimum_result_stability_s]
                refine_windows = candidate_windows(unstable, info.duration_s, self.settings.result_refine_padding_s)
                progress(90)
                # Native windows revisit earlier timestamps after the global
                # pass. Debounce per pass; the total debug-file budget remains
                # shared and bounded by max_debug_frames.
                last_debug.clear()
                for index, (start, end) in enumerate(refine_windows):
                    with closing(roi_sample(
                        video, roi=source_roi, reference_size=reference, output_size=output_size,
                        fps=self.settings.result_refine_fps, start_s=start, end_s=end, cancellation=token,
                    )) as frames:
                        for frame in frames:
                            token.raise_if_cancelled()
                            refine_frames += 1
                            matches = roi_detect(frame.image, frame.timestamp, roi=roi, event_types=result_types)
                            observations.extend(matches)
                            if len(observations) > self.settings.max_observations:
                                raise GameplayAnalysisError("Too many result-refinement detections", code="candidate_limit")
                            debug_matches(frame.image, matches)
                    progress(90 + 5 * (index + 1) / len(refine_windows))
                unique: dict[tuple[float, str, str], GameEvent] = {}
                for event in observations:
                    event_key = (round(event.timestamp, 6), event.type.value, event.source.value)
                    if event_key not in unique or event.confidence > unique[event_key].confidence:
                        unique[event_key] = event
                observations = list(unique.values())
            accept_clusters(observations, fps=self.settings.result_fps, brief=True)
        dense_seconds = time.perf_counter() - dense_started
        progress(95)
        events = sorted(consolidated, key=lambda event: (event.timestamp, event.type.value))
        games = GameAssembler().assemble(events)
        warnings = ["Template similarity is not a calibrated probability; no automatic cuts were generated."]
        if not events:
            warnings.append("No stable visual events detected; this does not prove the VOD contains no games.")
        if any(game.start is None or game.end is None for game in games):
            warnings.append("Some games have missing boundaries; no timestamps were inferred.")
        if dropped:
            warnings.append(f"Rejected {dropped} temporally unstable candidate clusters.")
        token.raise_if_cancelled()
        if video_identity(video) != identity:
            raise GameplayAnalysisError("Source changed during analysis; result was not cached", code="source_changed")
        result = GameplayAnalysisResult(
            video_path=str(video), duration=info.duration_s, events=events, games=games,
            detector_version=self.detector.detector_version, settings=settings, warnings=warnings,
            metadata={
                "analyzer_version": ANALYZER_VERSION, "cache_key": key, "cache_hit": False,
                "video_fingerprint": identity["fingerprint"],
                "coarse_frames": coarse_frames, "fine_frames": fine_frames,
                "sampling_strategy": "coarse_vs_dense_result_roi" if dense else "legacy_coarse_to_fine",
                "dense_result_frames": dense_frames, "dense_result_fps": self.settings.result_fps if dense else 0,
                "dense_geometry": dense_geometry,
                "result_refine_frames": refine_frames, "result_refine_windows": [list(window) for window in refine_windows],
                "result_refine_fps": self.settings.result_refine_fps if dense else 0,
                "rejected_events": rejected_events,
                "dense_frame_size": dense_geometry["output_size"] if dense_geometry else None,
                "coarse_elapsed_seconds": coarse_seconds, "fine_elapsed_seconds": fine_seconds,
                "dense_elapsed_seconds": dense_seconds,
                "fine_windows": [[start, end] for start, end in windows],
                "fine_seconds": total_fine, "elapsed_seconds": time.perf_counter() - started,
                "debug_frames_saved": debug_saved, "rejected_clusters": dropped,
            },
        )
        progress(100)
        if self.cache is not None:
            try:
                self.cache.save(key, result)
            except OSError as exc:
                result.warnings.append(f"Analysis succeeded, but cache could not be written: {exc}")
        return result

    @staticmethod
    def _save_frame(directory: Path, image: Any, event: GameEvent, sequence: int) -> None:
        from analysis.gameplay.hearthstone.visual.templates import require_opencv

        cv2 = require_opencv()
        directory = directory.expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True)
        success, encoded = cv2.imencode(".jpg", image)
        if not success:
            raise GameplayAnalysisError("Could not encode debug frame", code="debug_frame_failed")
        name = f"{event.timestamp:012.3f}_{event.type.value.lower()}_{event.confidence:.3f}_{sequence:04d}.jpg"
        # Debug output must never replace media, templates, or earlier reports.
        # Publish in the same directory and atomically refuse existing files,
        # including links and files created between selection and publication.
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(prefix=".gameplay-frame-", suffix=".tmp", dir=directory, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(encoded.tobytes())
                stream.flush()
                os.fsync(stream.fileno())
            base = directory / name
            for collision in range(1000):
                candidate = base if collision == 0 else base.with_name(f"{base.stem}_{collision + 1}{base.suffix}")
                try:
                    atomic_promote_output(temporary, candidate, overwrite=False)
                except FileExistsError:
                    continue
                break
            else:
                raise GameplayAnalysisError("Too many existing debug frames with the same name", code="debug_frame_failed")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def analyze_gameplay(video_path: str | Path, **options: Any) -> GameplayAnalysisResult:
    """Convenience entry point using the same reusable service as future frontends."""
    analyze_options = {key: options.pop(key) for key in tuple(options) if key in {
        "cancellation", "on_progress", "debug_frames", "max_debug_frames",
    }}
    return HearthstoneAnalyzer(**options).analyze(video_path, **analyze_options)
