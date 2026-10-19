"""Opt-in class-focused sparse preselection. Frozen detection runs unchanged inside local windows.

A 60 s scout plus 30 s recovery samples is not a proof that no hidden short game
occurred. This backend is deliberately explicit/experimental; all-class detection
remains the default. Missing/weak Hero Power evidence opens the traditional scan.
"""
from __future__ import annotations

from copy import copy, deepcopy
from dataclasses import asdict, replace
import hashlib
import json
import math
from pathlib import Path
import time
from typing import Any

from analysis.audio_service import AnalysisCancellation
from analysis.gameplay.cache import GameplayCache, cache_key, video_identity
from analysis.gameplay.models import GameplayAnalysisResult
from analysis.performance_profile import current_profile, measure, record
from analysis.shared.frames import FrameSamplingService
from analysis.hero_groups.labels import class_id
from analysis.hero_groups.recognition import HeroGroupingService, VERSION, crop
from utils.runtime_paths import cache_root
from .state_aware import StateAwareHearthstoneAnalyzer

VERSION_FOCUS = "class-focus-v2"


def focus_windows(points: list[dict[str, Any]], target: str, duration: float,
                  padding: float = 60.0) -> list[tuple[float, float]]:
    """Skip only adjacent strong votes for the SAME non-target class; uncertainty scans."""
    if len({p["timestamp"] for p in points}) < 2:
        return [(0., duration)] if duration > 0 else []
    requests = []
    for left, right in zip(points, points[1:]):
        skip = (left.get("status") == right.get("status") == "matched"
                and left.get("class") == right.get("class") and left.get("class") not in (None, target))
        if not skip:
            requests.append((max(0., left["timestamp"] - padding), min(duration, right["timestamp"] + padding)))
    merged: list[tuple[float, float]] = []
    for a, b in sorted(requests):
        if b <= a:
            continue
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    return merged


class WindowSampler:
    """Adapter: real source seeks, detector-local clock; no temporary video or re-encode."""
    def __init__(self, service: Any, info: Any, start: float, end: float) -> None:
        self.service, self.info, self.start, self.end = service, info, start, end

    def probe(self, *args: Any, **kwargs: Any) -> Any:
        return replace(self.info, duration_s=self.end - self.start, start_time_s=0.)

    def _sample(self, method: str, source: Any, **kwargs: Any) -> Any:
        start = float(kwargs.pop("start_s", 0.))
        end = kwargs.pop("end_s", None)
        kwargs["start_s"] = self.start + start
        kwargs["end_s"] = min(self.end, self.start + float(end)) if end is not None else self.end
        frames = getattr(self.service, method)(source, **kwargs)
        try:
            for frame in frames:
                if self.start <= frame.timestamp < self.end:
                    yield replace(frame, timestamp=frame.timestamp - self.start)
        finally:
            close = getattr(frames, "close", None)
            if close:
                close()

    def sample(self, source: Any, **kwargs: Any) -> Any:
        return self._sample("sample", source, **kwargs)

    def sample_roi(self, source: Any, **kwargs: Any) -> Any:
        return self._sample("sample_roi", source, **kwargs)


def relocate(analysis: GameplayAnalysisResult, offset: float, index: int) -> tuple[list[Any], list[Any]]:
    """Never assemble events across discarded gaps."""
    events = [replace(e, timestamp=e.timestamp + offset, metadata=deepcopy(e.metadata)) for e in analysis.events]
    games: list[Any] = []
    for game in analysis.games:
        markers = [replace(e, timestamp=e.timestamp + offset, metadata=deepcopy(e.metadata)) for e in game.markers]
        # Same identifier recipe as GameAssembler, using ORIGINAL timestamps.
        # A shifted scout window must not erase an existing manual override.
        signature = "|".join(f"{e.type.value}:{e.timestamp:.9f}:{e.source.value}" for e in markers)
        if not markers:
            signature = f"{game.start + offset if game.start is not None else None}|{game.end + offset if game.end is not None else None}|{game.result.value}"
        identity = hashlib.sha256(signature.encode()).hexdigest()[:20]
        games.append(replace(game, id="game-" + identity, index=index + len(games),
            start=None if game.start is None else game.start + offset,
            end=None if game.end is None else game.end + offset,
            markers=markers,
            metadata={**deepcopy(game.metadata), "class_focus_window_start": offset}))
    return events, games


class ClassFocusedAnalyzer:
    def __init__(self, target: str, *, state_profile: Path, template_pack: Path, use_cache: bool = True,
                 service: Any = None, grouping: Any = None, detector: Any = None,
                 cache_dir: Path | None = None, previous_grouping: dict[str, Any] | None = None) -> None:
        canonical = class_id(target)
        if canonical is None:
            raise ValueError("Choose a known Hearthstone class.")
        self.target: str = canonical
        self.service = service or FrameSamplingService(enabled=False, in_process=True)
        self.owns_service = service is None
        self.grouping = grouping or HeroGroupingService(sampler=self.service)
        self.base = detector or StateAwareHearthstoneAnalyzer(state_profile=state_profile,
                                                             template_pack=template_pack, use_cache=use_cache)
        self.cache = GameplayCache(cache_dir or cache_root() / "gameplay" / "class_focus") if use_cache else None
        self.previous = previous_grouping or {}

    def _classify(self, analysis: GameplayAnalysisResult, token: Any, progress: Any) -> GameplayAnalysisResult:
        grouping = self.grouping.analyze(Path(analysis.video_path), [g.to_mapping() for g in analysis.games],
            duration=analysis.duration, previous=self.previous, cancellation=token,
            on_progress=lambda p: progress(85 + int(p * .15)))
        grouping["selected"] = [self.target] if self.target in grouping.get("groups", {}) else []
        analysis.metadata["target_game_ids"] = [r["key"] for r in grouping.get("games", [])
                                                if class_id(r.get("group")) == self.target]
        analysis.metadata["focused_grouping"] = grouping
        uncertain = sum(r.get("class_status") != "matched" for r in grouping.get("games", []))
        if uncertain:
            analysis.warnings.append(f"{uncertain} uncertain class assignments: review before export.")
        return analysis

    def analyze(self, video: str | Path, *, cancellation: Any = None, on_progress: Any = None) -> GameplayAnalysisResult:
        token = cancellation or AnalysisCancellation()
        progress = on_progress or (lambda p: None)
        video = Path(video).expanduser().resolve()
        token.raise_if_cancelled()
        started = time.perf_counter()
        try:
            identity = video_identity(video)
            info = self.service.probe(video, cancellation=token)
            refs = self.grouping._references()
            options = {"version": VERSION_FOCUS, "target": self.target, "step_s": 60., "recovery_s": 30.,
                       "padding_s": 60., "hero_version": VERSION, "portrait": asdict(self.grouping.settings),
                       "refs": {k: hashlib.sha256(v.tobytes()).hexdigest() for k, v in refs.items()},
                       "profile": self.base.profile.fingerprint, "pack": self.base.vs.pack_fingerprint,
                       "detector": self.base.analyzer_version}
            options = json.loads(json.dumps(options))  # persisted tuples become lists; compare the same schema
            key = cache_key(identity, {"backend": VERSION_FOCUS}, options)
            active = current_profile()
            if active:
                active.modes.update(target_class=self.target, class_search="experimental_sparse_60s_with_30s_recovery")
            cached = self.cache.load(key) if self.cache else None
            if (cached is not None and cached.duration == info.duration_s and cached.video_path == str(video)
                    and cached.settings == options and video_identity(video) == identity):
                cached.metadata["cache_hit"] = True
                if active:
                    active.cache("class_focus", "hit")
                return self._classify(cached, token, progress)
            if active:
                active.cache("class_focus", "miss" if self.cache else "disabled")
            if self.target not in refs:
                # Cannot prove negatives without the requested reference: do the ordinary scan.
                result = self.base.analyze(video, cancellation=token, on_progress=lambda p: progress(int(p * .85)))
                result.warnings.append("Target Hero Power reference missing; full traditional detection used.")
                result.metadata["class_focus"] = {"target": self.target, "scanned_fraction": 1., "fallback_reason": "missing_reference"}
                return self._classify(result, token, progress)
            duration = float(info.duration_s)
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError("Invalid video duration.")
            scout_options = {k: v for k, v in options.items() if k != "target"}
            scout_key = cache_key(identity, {"backend": VERSION_FOCUS + "-scout"}, scout_options)
            scout = self.cache.load(scout_key) if self.cache else None
            if (scout is not None and scout.settings == scout_options and scout.duration == duration
                    and scout.video_path == str(video)):
                points = scout.metadata["points"]
                if active:
                    active.cache("class_focus.scout", "hit")
                record("class_focus.scout", cached_points=len(points))
                progress(20)
            else:
                if active:
                    active.cache("class_focus.scout", "miss" if self.cache else "disabled")
                times = [float(t) for t in range(0, math.ceil(duration), 30)]
                times.append(max(0., duration - .05))
                points = []
                with measure("class_focus.scout"):
                    for i in range(0, len(times), 8):
                        token.raise_if_cancelled()
                        group = times[i:i+8]
                        requests = sorted({max(0., min(duration - .01, t + delta))
                                           for t in group for delta in (0., .25, .5)})
                        frames = self.service.get_many(video, requests, max_width=960, cancellation=token)
                        record("class_focus.scout", requested_frames=len(requests),
                               delivered_frames=sum(f is not None for f in frames.values()))
                        for t in group:
                            powers = []
                            for delta in (0., .25, .5):
                                requested = max(0., min(duration - .01, t + delta))
                                frame = frames.get(requested)
                                if frame is not None and self.grouping.board_gate(frame.image) >= self.grouping.settings.board_min:
                                    powers.append(crop(frame.image, self.grouping.settings.power_roi))
                            vote = self.grouping._power_evidence(powers, refs)
                            # A clamped final request is not three independent supporting observations.
                            if len({max(0., min(duration - .01, t+d)) for d in (0., .25, .5)}) < 3:
                                vote = {**vote, "status": "estimated"}
                            points.append({"timestamp": t, "class": vote.get("class"), "status": vote.get("status"),
                                           "score": vote.get("scores", {}).get(vote.get("class"), 0.),
                                           "margin": vote.get("margin", 0.)})
                        progress(int(20 * min(i + 8, len(times)) / len(times)))
                if self.cache:
                    token.raise_if_cancelled()
                    if video_identity(video) != identity:
                        raise ValueError("Source changed during class reconnaissance.")
                    self.cache.save(scout_key, GameplayAnalysisResult(str(video), duration, [], [],
                        VERSION_FOCUS + "-scout", settings=scout_options, metadata={"points": points}))
            windows = focus_windows(points, self.target, duration)
            events: list[Any] = []
            games: list[Any] = []
            with measure("class_focus.local_detection"):
                for i, (a, b) in enumerate(windows):
                    token.raise_if_cancelled()
                    local = copy(self.base)
                    local.analyzer_version = VERSION_FOCUS + "-window-" + self.base.analyzer_version
                    local.sampler = WindowSampler(self.service, info, a, b)
                    def window_settings(a: float = a, b: float = b) -> dict[str, Any]:
                        return {"clock_adapter": "source-window-v1", "window": [a, b]}
                    setattr(local, "_extra_settings", window_settings)
                    def window_progress(value: int) -> None:
                        progress(20 + int(65 * (i + value/100) / len(windows)))
                    result = local.analyze(video, cancellation=token, on_progress=window_progress)
                    shifted_events, shifted_games = relocate(result, a, len(games) + 1)
                    events.extend(shifted_events)
                    games.extend(shifted_games)
            token.raise_if_cancelled()
            if video_identity(video) != identity:
                raise ValueError("Source changed during class-focused analysis.")
            scanned = sum(b-a for a, b in windows)
            record("class_focus", scout_points=len(points), local_windows=len(windows),
                   scanned_source_seconds=scanned, skipped_source_seconds=duration-scanned)
            result = GameplayAnalysisResult(str(video), duration, events, games, VERSION_FOCUS, settings=options,
                metadata={"cache_hit": False, "class_focus": {"target": self.target, "points": points, "windows": windows,
                          "scanned_fraction": scanned/duration, "wall_s": time.perf_counter()-started}},
                warnings=["Experimental sparse class search: a short game entirely between recovery samples can be missed. "
                          "Use All classes for exhaustive detection."])
            if self.cache:
                self.cache.save(key, result)
            return self._classify(result, token, progress)
        finally:
            active = current_profile()
            if active and "info" in locals():
                active.video.update(duration_s=info.duration_s, width=info.width, height=info.height)
            if self.owns_service:
                self.service.close()
