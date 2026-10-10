"""Smart gameplay editing: detection -> publishable games -> smart boundaries -> gameplay KEEPS.

UI-independent service shared by the automation CLI (and later the GUI).
`keep-games wins` is defined by what is KEPT: one editorial island per
complete WIN, [smart START, smart END]; everything else is removed. Gameplay
cuts are derived as the complement of the keeps, so classic silence cuts and
manual/suppressed overrides compose with them through the existing
analysis.classic.compute_classic_cuts; export is untouched.

Work is limited to what keeps need: boundary analysis (local audio, ASR,
visual) and one resolver call per boundary, only around complete WINs, with
detector/ASR/agent caches reused. Losses and incomplete games need no
boundary work at all: they are simply outside every island.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
import time
from typing import Any

from analysis.audio_service import AnalysisCancellation
from analysis.boundaries.analyzer import BoundaryAnalyzer, BoundarySettings
from analysis.boundaries.keep_policy import GameKeepPolicy, GameplayKeep
from analysis.boundaries.gameplay import GameplayTimeline
from analysis.boundaries.models import BoundaryContext, BoundaryDecision, BoundaryType, GameDisposition
from analysis.boundaries.resolvers import BOUNDARY_MODES, REMOTE_MODES, ResolverOptions, create_resolver
from analysis.boundaries.windows import WindowSettings
from analysis.cut_engine import Segment
from analysis.gameplay.models import GameplayAnalysisResult

REPORT_FORMAT = "recut_gameplay_edit_report"
REPORT_VERSION = 2  # v2: keep-based (islands around complete WINs)
GAMES = ("hearthstone",)
KEEP_MODES = ("wins",)

# Editorial context: enough speech around each anchor for semantic reasoning (quality over tokens).
EDITORIAL_WINDOWS = WindowSettings(start_before_s=45.0, start_after_s=10.0, end_before_s=10.0, end_after_s=60.0)


def default_detector_assets(game: str) -> dict[str, Path]:
    """Validated, frozen detector configuration (see docs/gameplay_validation)."""
    from utils.runtime_paths import project_root

    if game != "hearthstone":
        raise ValueError(f"Unsupported gameplay '{game}'")
    root = project_root() / "docs" / "gameplay_validation"
    return {"state_profile": root / "state_aware" / "iteration5_frozen" / "profile.json",
            "template_pack": root / "86" / "iteration4_frozen" / "templates.json"}


@dataclass(frozen=True)
class GameplayEditSettings:
    game: str = "hearthstone"
    keep_games: str = "wins"
    boundary_mode: str = "exact"
    resolver: ResolverOptions = field(default_factory=ResolverOptions)
    boundary: BoundarySettings = field(default_factory=lambda: BoundarySettings(windows=EDITORIAL_WINDOWS,
                                                                                keep_policy="wins"))
    state_profile: str | None = None
    template_pack: str | None = None
    use_detector_cache: bool = True

    def __post_init__(self) -> None:
        if self.game not in GAMES:
            raise ValueError(f"Unsupported gameplay '{self.game}' (choices: {', '.join(GAMES)})")
        if self.keep_games not in KEEP_MODES:
            raise ValueError(f"Unsupported --keep-games '{self.keep_games}' (choices: {', '.join(KEEP_MODES)})")
        if self.boundary_mode not in BOUNDARY_MODES:
            raise ValueError(f"Unknown boundary mode '{self.boundary_mode}' (choices: {', '.join(BOUNDARY_MODES)})")

    @property
    def sends_remote_context(self) -> bool:
        return self.boundary_mode in REMOTE_MODES

    def to_mapping(self) -> dict[str, Any]:
        resolver = {k: (str(v) if isinstance(v, Path) else v) for k, v in asdict(self.resolver).items()}
        return {"game": self.game, "keep_games": self.keep_games, "boundary_mode": self.boundary_mode,
                "resolver": resolver, "state_profile": self.state_profile, "template_pack": self.template_pack,
                "windows": asdict(self.boundary.windows)}

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> GameplayEditSettings:
        resolver = ResolverOptions(**{k: v for k, v in dict(raw.get("resolver", {})).items()
                                      if k in ResolverOptions.__dataclass_fields__})
        boundary = BoundarySettings(windows=WindowSettings(**raw["windows"]) if raw.get("windows") else EDITORIAL_WINDOWS,
                                    keep_policy="wins")
        return cls(str(raw.get("game", "hearthstone")), str(raw.get("keep_games", "wins")),
                   str(raw.get("boundary_mode", "exact")), resolver, boundary,
                   raw.get("state_profile"), raw.get("template_pack"))


@dataclass
class GameplayEditResult:
    analysis: GameplayAnalysisResult
    timeline: GameplayTimeline
    contexts: dict[int, dict[BoundaryType, BoundaryContext]]
    decisions: dict[int, dict[BoundaryType, BoundaryDecision]]
    keeps: list[GameplayKeep]
    timings: dict[str, float]
    warnings: list[str]
    settings: GameplayEditSettings

    @property
    def gameplay_keeps(self) -> list[Segment]:
        """Source of truth: merged editorial islands of the complete WINs."""
        return GameKeepPolicy.segments(self.keeps)

    @property
    def gameplay_cuts(self) -> list[Segment]:
        """Derived: complement of the keeps over the whole file."""
        return GameKeepPolicy.cuts(self.keeps, float(self.analysis.duration))

    def report(self) -> dict[str, Any]:
        games = []
        by_game = {keep.game.index: keep for keep in self.keeps}
        for game in self.timeline.games:
            info = self.timeline.info(game)
            publishable = info.disposition == GameDisposition.KEEP and game.start is not None and game.end is not None
            item: dict[str, Any] = {**info.to_mapping(), "complete": game.start is not None and game.end is not None,
                                    "publishable": publishable,
                                    "raw_anchors": {"start": game.start, "end": game.end}}
            if publishable:
                keep = by_game.get(game.index)
                item["smart"] = {"start": keep.segment.start if keep else None, "end": keep.segment.end if keep else None,
                                 "adjustments": keep.adjustments if keep else ["no keep island"]}
                item["boundaries"] = {kind.value: self._decision_entry(game.index, kind, decision)
                                      for kind, decision in self.decisions.get(game.index, {}).items()}
            games.append(item)
        keeps = self.gameplay_keeps
        return {"format": REPORT_FORMAT, "version": REPORT_VERSION, "settings": self.settings.to_mapping(),
                "semantics": "keep only editorial islands of complete WINs; everything else is removed",
                "remote_context_sent": self.settings.sends_remote_context and any(
                    d.resolver in REMOTE_MODES for per in self.decisions.values() for d in per.values()),
                "detector": {"version": self.analysis.detector_version,
                             "fingerprint": self.analysis.metadata.get("video_fingerprint")},
                "duration": self.analysis.duration, "games": games,
                "keep_islands": [keep.to_mapping() for keep in self.keeps],
                "gameplay_keeps": [{"start": k.start, "end": k.end} for k in keeps],
                "gameplay_cuts": [{"start": c.start, "end": c.end} for c in self.gameplay_cuts],
                "kept_seconds": sum(k.dur for k in keeps), "timings": dict(self.timings),
                "warnings": list(self.warnings)}

    def _decision_entry(self, index: int, kind: BoundaryType, decision: BoundaryDecision) -> dict[str, Any]:
        entry = decision.to_mapping()
        context = self.contexts.get(index, {}).get(kind)
        if context is not None and decision.candidate_index is not None:
            candidate = context.candidates[decision.candidate_index]
            entry["candidate"] = {"timestamp": candidate.timestamp, "sources": [s.value for s in candidate.sources]}
            entry["window"] = context.window.to_mapping()
            entry["candidates_total"] = len(context.candidates)
            labels = decision.metadata.get("segment_labels")
            if isinstance(labels, list):  # agent's reasoning trace: which speech it attributed to the game
                entry["segment_labels"] = [{"start": s.start, "end": s.end, "label": label}
                                           for s, label in zip(context.transcript, labels)]
        return entry


Detector = Callable[[Path, AnalysisCancellation, Callable[[int], None] | None], GameplayAnalysisResult]


def hearthstone_detector(settings: GameplayEditSettings) -> Detector:
    from analysis.gameplay.hearthstone.state_aware import StateAwareHearthstoneAnalyzer

    assets = default_detector_assets("hearthstone")
    profile = Path(settings.state_profile) if settings.state_profile else assets["state_profile"]
    pack = Path(settings.template_pack) if settings.template_pack else assets["template_pack"]
    for path in (profile, pack):
        if not path.is_file():
            raise FileNotFoundError(f"Gameplay detector asset not found: {path}")
    detector = StateAwareHearthstoneAnalyzer(state_profile=profile, template_pack=pack,
                                             use_cache=settings.use_detector_cache)

    def run(video: Path, token: AnalysisCancellation, on_progress: Callable[[int], None] | None) -> GameplayAnalysisResult:
        return detector.analyze(video, cancellation=token, on_progress=on_progress)

    return run


class GameplayEditingService:
    def __init__(self, settings: GameplayEditSettings, *, detector: Detector | None = None,
                 boundary_analyzer: BoundaryAnalyzer | None = None, resolver: Any = None) -> None:
        self.settings = settings
        self._detector = detector
        self._analyzer = boundary_analyzer
        self._resolver = resolver

    def run(self, video: str | Path, *, cancellation: AnalysisCancellation | None = None,
            on_progress: Callable[[int], None] | None = None,
            analysis: GameplayAnalysisResult | None = None) -> GameplayEditResult:
        token = cancellation or AnalysisCancellation()
        source = Path(video).expanduser().resolve()
        timings: dict[str, float] = {}
        warnings: list[str] = []

        def progress(value: float) -> None:
            if on_progress is not None:
                on_progress(max(0, min(100, int(value))))

        started = time.perf_counter()
        if analysis is None:
            detector = self._detector or hearthstone_detector(self.settings)
            analysis = detector(source, token, lambda v: progress(v * 0.7))
        timings["gameplay_detection_s"] = round(time.perf_counter() - started, 3)
        token.raise_if_cancelled()
        boundary = replace(self.settings.boundary, keep_policy="wins")
        timeline = GameplayTimeline(analysis, boundary.eof, policy=boundary.keep_policy)
        policy = GameKeepPolicy(timeline)
        kept = policy.publishable()
        if not kept:
            warnings.append("no complete WIN: nothing is publishable")
        analyzer = self._analyzer or BoundaryAnalyzer(boundary)
        resolver = self._resolver or create_resolver(self.settings.boundary_mode, self.settings.resolver)
        contexts: dict[int, dict[BoundaryType, BoundaryContext]] = {}
        decisions: dict[int, dict[BoundaryType, BoundaryDecision]] = {}
        boundary_s = resolve_s = 0.0
        agent_calls = cache_hits = fallbacks = 0
        for position, game in enumerate(kept):
            token.raise_if_cancelled()
            started = time.perf_counter()
            try:
                analysed = analyzer.analyze_game(source, analysis, game.index, require_removable=False,
                                                 cancellation=token)
                contexts[game.index] = analysed.contexts
            except Exception as exc:
                if exc.__class__.__name__ in ("AnalysisCancelled", "PipelineAnalysisCancelled"):
                    raise
                warnings.append(f"game {game.index}: boundary analysis failed ({exc}); anchors used")
                contexts[game.index] = {}
            boundary_s += time.perf_counter() - started
            started = time.perf_counter()
            for kind in (BoundaryType.START, BoundaryType.END):
                context = contexts[game.index].get(kind)
                if context is None:
                    continue
                decision = resolver.resolve(context)
                decisions.setdefault(game.index, {})[kind] = decision
                if decision.requested_resolver in REMOTE_MODES:
                    if decision.metadata.get("cache_hit"):
                        cache_hits += 1
                    elif not decision.fallback or decision.metadata.get("error_code") != "cli_missing":
                        agent_calls += 1
                fallbacks += int(decision.fallback)
            resolve_s += time.perf_counter() - started
            progress(70 + 30 * (position + 1) / max(1, len(kept)))
        keeps = policy.keeps(decisions)
        timings.update({"boundary_analysis_s": round(boundary_s, 3), "resolver_s": round(resolve_s, 3),
                        "agent_calls": float(agent_calls), "agent_cache_hits": float(cache_hits),
                        "resolver_fallbacks": float(fallbacks)})
        progress(100)
        return GameplayEditResult(analysis, timeline, contexts, decisions, keeps, timings, warnings, self.settings)
