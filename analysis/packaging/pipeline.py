"""PackagingPipeline: edited project / final MP4 -> evidence -> agent -> rendered thumbnails + titles.

Stages (each timed; optional evidence that fails is reported and skipped):
  timeline -> transcript -> audio -> visual -> games -> highlights -> frames/webcam
  -> contact sheets -> [agent] phase A VideoStory (+ bounded deepening) -> cards
  -> [agent] phase B creative concepts -> thumbnail rendering -> report

--analyze-only stops before the agent (nothing leaves the machine).
--only titles|thumbnails|render reuses the analysis already in the output folder:
a new thumbnail text or provider never re-runs the local analysis.
Outputs are never deleted: replaced files are first copied to history/<stamp>/.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np

from analysis.audio_service import AnalysisCancellation

from . import creative, story
from .agent import PRIVACY_NOTE, AgentSettings, PackagingAgent
from .audio import AudioProfile, analyze_kept_audio
from .cache import PackagingCache
from .cards import extract_cards
from .extensions import extension_status
from .frames import FrameExtractor, contact_sheet, read_image, require_cv2, write_image
from .games import add_uncovered_segments, attach_transcript, games_from_detector, games_from_project
from .highlights import build_candidates, collect_signals
from .models import FrameRecord, GameContext, HighlightCandidate, PackagingError, TranscriptLine
from .render import RENDERER_VERSION, ThumbnailRenderer, plan_from_mapping
from .style import CreatorStyleStore, prompt_summary
from .timeline import EditMap
from .transcript import build_transcript
from .visual import VisualProfile, analyze_kept_visual
from .pipeline_v3 import V3Stages
from .webcam import RoiCheck, detect_webcam_roi, parse_roi, webcam_quality

EFFORTS = ("low", "medium", "high")  # exposed levels; xhigh is deliberately not offered
REPORT_FORMAT = "recut_packaging_report"
REPORT_VERSION = 1
ANALYSIS_FILES = ("content_analysis.json", "transcript.json", "game_contexts.json", "highlights.json",
                  "visual_assets.json", "opportunities.json")
AGENT_FILES = ("video_story.json", "title_candidates.json", "thumbnail_concepts.json", "packaging_report.json",
               "generation_request.json",
               "portfolio_strategy.json", "portfolio_quality_report.json")
OUTPUT_DIRS = ("frames", "webcam", "cards", "contact_sheets", "thumbnails", "subjects", "regions")
Progress = Callable[[str, str], None]


@dataclass
class PackagingSettings:
    output: str
    project: str | None = None
    video: str | None = None
    provider: str = "codex"
    language: str = "it"
    asr_language: str | None = None  # default: same as language; "auto" = detect
    asr_model: str = "small"
    titles: int = 6
    thumbnails: int = 4
    thumbnail_engine: str = "gen"  # gen: asset packages composed by the image model (local render = fallback)
    #                                v3: assets + portfolio + local scene graphs | v2: archetype layouts
    generator_timeout: float = 1500.0
    mode: str = "fast"  # fast: 1 agent call (brief -> story + concepts) | deep: story, deepen, creative
    webcam_roi: str | None = "auto"  # auto | none | x,y,w,h
    creator: str | None = None
    use_style: bool = True
    games: str = "auto"  # auto | project | detect | none
    only: str | None = None  # titles | thumbnails | render
    analyze_only: bool = False
    use_cache: bool = True
    overwrite: bool = False
    max_highlights: int = 14
    max_deepen: int | None = None  # default: 1 (fast, patch call only on request) / 2 (deep)
    image_format: str = "png"
    agent_model: str | None = None
    agent_effort: str | None = None
    agent_timeout: float = 900.0
    agent_max_input: int | None = None  # prompt character budget (default 60000 fast / 120000 deep)
    agent_budget_usd: float | None = None  # claude --max-budget-usd per call; codex has no cost cap
    cache_dir: str | None = None
    style_dir: str | None = None

    def validate(self) -> None:
        if bool(self.project) == bool(self.video) and self.only is None:
            raise PackagingError("invalid_arguments", "Use exactly one of --project or --video.")
        if self.provider not in ("claude", "codex"):
            raise PackagingError("invalid_arguments", "--provider must be claude or codex.")
        if not 5 <= self.titles <= 10:
            raise PackagingError("invalid_arguments", "--titles must be between 5 and 10.")
        if not 3 <= self.thumbnails <= 6:
            raise PackagingError("invalid_arguments", "--thumbnails must be between 3 and 6.")
        if self.titles < self.thumbnails:
            raise PackagingError("invalid_arguments", "--titles must be at least --thumbnails (one title per concept).")
        if self.only not in (None, "titles", "thumbnails", "render"):
            raise PackagingError("invalid_arguments", "--only must be titles, thumbnails or render.")
        if self.games not in ("auto", "project", "detect", "none"):
            raise PackagingError("invalid_arguments", "--games must be auto, project, detect or none.")
        if self.image_format not in ("png", "jpg"):
            raise PackagingError("invalid_arguments", "--format must be png or jpg.")
        if self.thumbnail_engine not in ("gen", "v3", "v2"):
            raise PackagingError("invalid_arguments", "--thumbnail-engine must be gen, v3 or v2.")
        if self.mode not in ("fast", "deep"):
            raise PackagingError("invalid_arguments", "--mode must be fast or deep.")
        if self.agent_effort is not None and self.agent_effort not in EFFORTS:
            raise PackagingError("invalid_arguments", f"--agent-effort must be one of {', '.join(EFFORTS)}.")

    @property
    def asset_engine(self) -> bool:
        """gen and v3 share assets, opportunities, the portfolio call and the scene renderer."""
        return self.thumbnail_engine in ("gen", "v3")

    @property
    def deepen_limit(self) -> int:
        return self.max_deepen if self.max_deepen is not None else (1 if self.mode == "fast" else 2)

    @property
    def effective_effort(self) -> str | None:
        """codex: medium by default (never the CLI's xhigh default); claude: the CLI default unless set."""
        if self.agent_effort:
            return self.agent_effort
        return "medium" if self.provider == "codex" else None


@dataclass
class Evidence:
    edit_map: EditMap
    payload: dict[str, Any] | None
    lines: list[TranscriptLine]
    transcript_status: dict[str, Any]
    audio: AudioProfile
    visual: VisualProfile
    games: list[GameContext]
    highlights: list[HighlightCandidate]
    roi: RoiCheck
    roi_evidence: dict[str, Any] = field(default_factory=dict)
    sheets: list[dict[str, Any]] = field(default_factory=list)


class PackagingPipeline(V3Stages):
    def __init__(self, settings: PackagingSettings, *, progress: Progress | None = None,
                 cancellation: AnalysisCancellation | None = None, agent: PackagingAgent | None = None,
                 transcription_engine: Any = None, rms_extractor: Any = None, keyframe_scanner: Any = None,
                 frame_sampler: Any = None, game_detector: Any = None, duration_probe: Any = None,
                 planner: Any = None, generator: Any = None) -> None:
        settings.validate()
        self.settings = settings
        self.out = Path(settings.output).expanduser().resolve()
        self.progress = progress or (lambda stage, message: None)
        self.cancellation = cancellation or AnalysisCancellation()
        self.cache = PackagingCache(settings.cache_dir, enabled=settings.use_cache)
        self.timings: dict[str, float] = {}
        self.warnings: list[str] = []
        self._agent = agent
        self._engine = transcription_engine
        self._rms = rms_extractor
        self._scanner = keyframe_scanner
        self._sampler = frame_sampler
        self._detector = game_detector
        self._duration_probe = duration_probe
        self._planner = planner
        self._generator = generator
        self.frames: FrameExtractor | None = None
        self._stamp = time.strftime("%Y%m%d-%H%M%S")

    # ---- helpers ---------------------------------------------------------------------------------
    def _timed(self, name: str, fn: Callable[[], Any]) -> Any:
        self.cancellation.raise_if_cancelled()
        self.progress(name, "start")
        started = time.perf_counter()
        try:
            return fn()
        finally:
            self.timings[name] = round(self.timings.get(name, 0.0) + time.perf_counter() - started, 3)
            self.progress(name, f"done in {self.timings[name]:.1f}s")

    def _write(self, name: str, payload: Any) -> Path:
        from analysis.gameplay.cache import atomic_write_json

        return atomic_write_json(self.out / name, payload if isinstance(payload, dict) else {"items": payload})

    def _archive(self, names: tuple[str, ...], dirs: tuple[str, ...] = ()) -> Path | None:
        """Copy existing outputs to history/<stamp>/ before they are replaced (never delete)."""
        present = [n for n in names if (self.out / n).exists()] + [d for d in dirs if (self.out / d).is_dir()]
        if not present:
            return None
        target = self.out / "history" / self._stamp
        target.mkdir(parents=True, exist_ok=True)
        for name in present:
            source = self.out / name
            if source.is_dir():
                shutil.copytree(source, target / name, dirs_exist_ok=True)
            else:
                shutil.copy2(source, target / name)
        return target

    def _prepare_output(self) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        if self.settings.only is not None:
            if not (self.out / "content_analysis.json").is_file():
                raise PackagingError("no_previous_run", f"--only needs a previous packaging run in {self.out}")
            return
        existing = [n for n in ANALYSIS_FILES + AGENT_FILES if (self.out / n).exists()]
        if existing and not self.settings.overwrite:
            raise PackagingError("output_exists", f"{self.out} already contains a packaging run ({existing[0]}). "
                                 "Choose another --output, use --only to regenerate part of it, or --overwrite "
                                 "(previous files are kept in history/).")
        if existing:
            archived = self._archive(ANALYSIS_FILES + AGENT_FILES, OUTPUT_DIRS)
            self.warnings.append(f"previous run archived to {archived}")

    # ---- run -------------------------------------------------------------------------------------
    def run(self) -> dict[str, Any]:
        early = self.prepare()
        if early is not None:
            return early
        self.plan()
        return self.finish()

    # Three steps so a caller can overlap the waiting with other work (process --packaging: plan() runs in a
    # background thread while the video export renders):
    #   prepare()  local analysis (ASR, audio, frames, assets)       CPU, main thread
    #   plan()     provider calls only (planning, image generation)  waiting, no Qt: background-safe
    #   finish()   local renders (Qt), finals, report                main thread
    def prepare(self) -> dict[str, Any] | None:
        """Local analysis. Returns a finished report for --only / --analyze-only (nothing left to plan)."""
        self._total = time.perf_counter()
        self._prepare_output()
        if self.settings.only is not None:
            return self._regenerate(self._total)
        self._evidence = self._analyze()
        if self.settings.analyze_only:
            return self._report(self._evidence, status="analyzed", total=self._total)
        return None

    def plan(self) -> None:
        """Agent planning + (engine gen) the image-model call. Errors are kept and re-raised by finish()."""
        evidence = self._evidence
        self._planned: tuple[Any, Any, Any] | None = None
        self._plan_error: BaseException | None = None
        agent: Any = None
        try:
            agent = self._make_agent()  # inside: a missing CLI must surface in finish(), not die in a thread
            if self.settings.mode == "fast":
                if self.settings.asset_engine:
                    video_story, proposals = self._fast_v3(agent, evidence)
                else:
                    video_story, proposals = self._fast(agent, evidence)
            elif self.settings.asset_engine:
                story = self._story(agent, evidence)  # deep: story (+ deepen), then ONE portfolio call
                video_story, proposals = self._fast_v3(agent, evidence, story=story, task="creative_portfolio_v3")
            else:
                video_story = self._story(agent, evidence)
                cards = self._timed("cards", lambda: extract_cards(video_story.get("card_observations", []),
                                                                   self._frames(), evidence.lines))
                self._write("cards.json", {"cards": [c.to_mapping() for c in cards]})
                proposals = self._creative(agent, evidence, video_story, [c.to_mapping() for c in cards])
            if self.settings.thumbnail_engine == "gen":
                self._prefetched_generation = self._generate_images(proposals, evidence)
            self._planned = (agent, video_story, proposals)
        except BaseException as exc:  # finish() reports it on the main thread
            self._planned = (agent, None, None)
            self._plan_error = exc

    def finish(self) -> dict[str, Any]:
        evidence, total = self._evidence, self._total
        agent, video_story, proposals = self._planned or (None, None, None)
        error = getattr(self, "_plan_error", None)
        if error is not None:
            if isinstance(error, PackagingError):
                raise error
            from analysis.boundaries.agents import AgentCallError
            if isinstance(error, AgentCallError):
                self._report(evidence, status="agent_failed", total=total, error={"code": error.code,
                                                                                   "detail": error.detail},
                             agent=agent)
                raise PackagingError(f"agent_{error.code}", f"{self.settings.provider} failed ({error.code}): "
                                     f"{error.detail}. Local analysis is saved; re-run the same command to retry "
                                     "(cached evidence is reused).") from error
            raise error
        assert proposals is not None
        renders = self._render_all(proposals, evidence)
        return self._report(evidence, status="done", total=total, agent=agent, story_=video_story,
                            proposals=proposals, renders=renders)

    def _make_agent(self) -> PackagingAgent:
        if self._agent is not None:
            return self._agent
        s = self.settings
        budget = s.agent_max_input or (60000 if s.mode == "fast" else 120000)
        agent = PackagingAgent(AgentSettings(provider=s.provider, model=s.agent_model, effort=s.effective_effort,
                                             timeout_s=s.agent_timeout, use_cache=s.use_cache,
                                             max_images=6 if s.mode == "fast" else 16, max_prompt_chars=budget,
                                             max_budget_usd=s.agent_budget_usd,
                                             log_path=str(self.out / "agent_calls.jsonl")), self.cache)
        agent.require()  # explicit error before any expensive call when the CLI is missing
        self.progress("agent", PRIVACY_NOTE.format(provider=s.provider))
        return agent

    # ---- local analysis --------------------------------------------------------------------------
    def _load_edit(self) -> tuple[EditMap, dict[str, Any] | None]:
        s = self.settings
        if s.project:
            from core.project_file import normalize_project_payload

            edit_map = EditMap.from_project(s.project, planner=self._planner)
            payload = normalize_project_payload(json.loads(Path(s.project).read_text(encoding="utf-8")))
            return edit_map, payload
        return EditMap.from_video(s.video or "", duration_probe=self._duration_probe), None

    def _frames(self) -> FrameExtractor:
        assert self.frames is not None
        return self.frames

    def _analyze(self) -> Evidence:
        s = self.settings
        edit_map, payload = self._timed("timeline", self._load_edit)
        self.progress("timeline", f"{len(edit_map.pieces)} kept pieces, {edit_map.duration:.0f} s edited")
        from analysis.boundaries.transcript import TranscriptSettings

        asr_language = s.asr_language or s.language
        settings = TranscriptSettings(model=s.asr_model, language=None if asr_language == "auto" else asr_language,
                                      use_cache=s.use_cache)
        status, lines = self._timed("transcription", lambda: build_transcript(
            edit_map, settings, cache=self.cache, engine=self._engine, cancellation=self.cancellation))
        if status["status"] in ("failed", "unavailable"):
            self.warnings.append(f"transcript {status['status']}: {status.get('detail')}")
        audio = self._timed("audio_analysis", lambda: analyze_kept_audio(
            edit_map, cache=self.cache, extractor=self._rms, cancellation=self.cancellation))
        if not audio.available:
            self.warnings.append(f"audio evidence {audio.status}: {audio.detail}")
        visual = self._timed("visual_analysis", lambda: analyze_kept_visual(
            edit_map, cache=self.cache, scanner=self._scanner, cancellation=self.cancellation))
        if visual.status != "ok":
            self.warnings.append(f"visual evidence {visual.status}: {visual.detail}")
        games = self._timed("games", lambda: self._games(edit_map, payload))
        attach_transcript(games, lines)
        signals = collect_signals(edit_map, lines, audio, visual, games)
        highlights = self._timed("highlight_discovery", lambda: build_candidates(
            edit_map, signals, lines, audio, visual, games, max_candidates=s.max_highlights))
        self.frames = FrameExtractor(edit_map, self.out, self.cache, sampler=self._sampler,
                                     cancellation=self.cancellation)
        evidence = Evidence(edit_map, payload, lines, status, audio, visual, games, highlights,
                            RoiCheck("absent", None, "", ""))
        self._timed("frame_extraction", lambda: self._extract_frames(evidence))
        self._timed("contact_sheets", lambda: self._sheets(evidence))
        if self.settings.asset_engine:
            self._timed("asset_preparation", lambda: self._prepare_assets(evidence))
        self._write_analysis(evidence)
        return evidence

    def _games(self, edit_map: EditMap, payload: dict[str, Any] | None) -> list[GameContext]:
        mode = self.settings.games
        contexts: list[GameContext] = []
        if mode in ("auto", "project") and payload is not None:
            contexts = games_from_project(payload, edit_map)
            if not contexts and mode == "project":
                self.warnings.append("the project has no gameplay data with kept games")
        if mode == "detect":
            try:
                contexts = games_from_detector(edit_map, detector=self._detector, cancellation=self.cancellation)
            except Exception as exc:  # optional: the rest of the pipeline works without games
                from analysis.audio_service import AnalysisCancelled
                if isinstance(exc, AnalysisCancelled):
                    raise
                self.warnings.append(f"game detection failed: {exc}")
        return add_uncovered_segments(contexts, edit_map)

    def _resolve_roi(self, sample: list[np.ndarray]) -> tuple[RoiCheck, dict[str, Any]]:
        value = self.settings.webcam_roi
        if value in (None, "", "none"):
            return RoiCheck("absent", None, "webcam disabled", "cli"), {}
        if value == "auto":
            profile = self._style_profile()
            stored = (profile or {}).get("brand", {}).get("webcam_roi") if profile else None
            if stored:
                return parse_roi(stored, origin="profile"), {}
            return detect_webcam_roi(sample)
        return parse_roi(value, origin="cli"), {}

    def _extract_frames(self, evidence: Evidence) -> None:
        """Per highlight: the best settled frame + one visually different alternative from a 4 s burst, plus
        before/after context; webcam crops ranked by face size/sharpness and de-duplicated across the video."""
        from .frames import pick_diverse, rank_gameplay

        frames = self._frames()
        edit_map = evidence.edit_map
        bursts: dict[str, list[tuple[float, np.ndarray]]] = {}
        self._bursts = bursts  # reused by V3 subject extraction (webcam background plates)
        taken: list[int] = []  # visual hashes of every selected gameplay frame (no near-duplicates)

        def kept_frames(source_index: int, items: list[tuple[float, np.ndarray]]) -> list[tuple[float, np.ndarray]]:
            return [(t, im) for t, im in items if edit_map.to_edited(t, source_index) is not None]

        for h in evidence.highlights:
            self.cancellation.raise_if_cancelled()
            burst = frames.burst(h.source_index, max(0.0, h.peak_source - 2.0), h.peak_source + 2.0, fps=4.0)
            bursts[h.id] = burst
            picks = pick_diverse(rank_gameplay(kept_frames(h.source_index, burst), target=h.peak_source), k=2,
                                 taken=taken)
            for n, (score, t, image) in enumerate(picks):
                record = frames.save(image, source_index=h.source_index, source_time=t, highlight_id=h.id,
                                     role="peak" if n == 0 else "alt")
                record.quality["selection"] = score
                h.frame_ids.append(record.id)
            peak_edited = edit_map.to_edited(h.peak_source, h.source_index) or 0.0
            for role, edited in (("before", h.edited_start + 1.0), ("after", h.edited_end - 1.0)):
                if abs(edited - peak_edited) < 2.5:
                    continue
                source_index, t = edit_map.to_source(edited)
                context = frames.burst(source_index, max(0.0, t - 0.75), t + 0.75, fps=4.0)
                chosen = pick_diverse(rank_gameplay(kept_frames(source_index, context), target=t), k=1, taken=taken)
                if chosen:
                    score, ft, image = chosen[0]
                    record = frames.save(image, source_index=source_index, source_time=ft, highlight_id=h.id, role=role)
                    record.quality["selection"] = score
                    h.frame_ids.append(record.id)
        for game in evidence.games:  # one representative board frame per game (coverage)
            if game.edited_start is None or game.edited_end is None:
                continue
            source_index, t = edit_map.to_source((game.edited_start + game.edited_end) / 2)
            context = frames.burst(source_index, max(0.0, t - 1.0), t + 1.0, fps=3.0)
            chosen = pick_diverse(rank_gameplay(kept_frames(source_index, context), target=t), k=1, taken=taken)
            if chosen:
                record = frames.save(chosen[0][2], source_index=source_index, source_time=chosen[0][1],
                                     role="coverage", notes=f"middle of {game.game_id}")
                game.frame_ids.append(record.id)
        for game in evidence.games:
            game.frame_ids = [f for h in evidence.highlights if h.game_id == game.game_id for f in h.frame_ids] + game.frame_ids
        gameplay = [frames.images[r.id] for r in frames.records.values() if r.kind == "gameplay"]
        sample = gameplay[::max(1, len(gameplay) // 24)][:24]  # evenly across the whole video
        evidence.roi, evidence.roi_evidence = self._resolve_roi(sample)
        if evidence.roi.status == "invalid":
            self.warnings.append(f"webcam disabled: {evidence.roi.message}")
        elif evidence.roi.status == "clamped":
            self.warnings.append(evidence.roi.message)
        if evidence.roi.rect is None:
            return
        from .quality import dhash, hamming

        rect = evidence.roi.rect
        taken_cam: list[int] = []
        for h in evidence.highlights:
            scored = []
            previous = None
            for t, image in bursts.get(h.id, []):
                if edit_map.to_edited(t, h.source_index) is None:
                    continue
                crop = rect.crop(image)
                if crop.size == 0:
                    continue
                scored.append((webcam_quality(crop, previous), t, crop))
                previous = crop
            if any(q["face"] for q, _, _ in scored):
                scored = [item for item in scored if item[0]["face"]]  # a reaction needs a visible face
            scored.sort(key=lambda item: item[0]["score"], reverse=True)
            picked = 0
            for quality, t, crop in scored:
                visual = dhash(crop)
                if picked >= 2 or any(hamming(visual, other) < 10 for other in taken_cam):
                    continue  # skip near-identical expressions
                taken_cam.append(visual)
                picked += 1
                record = frames.save(crop, source_index=h.source_index, source_time=t, kind="webcam",
                                     highlight_id=h.id, role="reaction", prefix="W", folder="webcam",
                                     notes="face visible" if quality["face"] else "no face detected")
                record.quality = quality
                h.webcam_frame_ids.append(record.id)

    def _sheets(self, evidence: Evidence) -> None:
        frames = self._frames()
        sheets: list[dict[str, Any]] = []
        for game in evidence.games:
            records = [frames.records[f] for f in dict.fromkeys(game.frame_ids) if f in frames.records][:36]
            parts = [records[i:i + 12] for i in range(0, len(records), 12)]  # every highlight frame is shown
            for n, part in enumerate(parts, start=1):
                suffix = f"_{n}" if len(parts) > 1 else ""
                path = self.out / "contact_sheets" / f"{game.game_id}{suffix}.jpg"
                title = f"{game.game_id} result {game.result or 'n/a'} ({n}/{len(parts)}) - frame id + edited time"
                contact_sheet(part, frames.images, path, columns=3, title=title)
                sheets.append({"path": str(path.relative_to(self.out)), "description":
                               f"contact sheet {n}/{len(parts)} of {game.game_id} ({game.result or 'no result'}): "
                               f"frames {', '.join(r.id for r in part)} labelled with id and edited time"})
        webcam = [r for r in frames.records.values() if r.kind == "webcam"]
        if webcam:
            path = self.out / "contact_sheets" / "webcam.jpg"
            contact_sheet(webcam[:24], frames.images, path, columns=6, cell_width=320, title="webcam crops")
            sheets.append({"path": str(path.relative_to(self.out)),
                           "description": f"webcam crops {', '.join(r.id for r in webcam[:24])} (id + edited time)"})
        evidence.sheets = sheets

    def _write_analysis(self, evidence: Evidence) -> None:
        frames = self._frames()
        self._write("transcript.json", {"status": evidence.transcript_status,
                                        "lines": [line.to_mapping() for line in evidence.lines]})
        self._write("game_contexts.json", {"games": [g.to_mapping() for g in evidence.games]})
        self._write("highlights.json", {"highlights": [h.to_mapping() for h in evidence.highlights]})
        self._write("content_analysis.json", {
            "format": "recut_packaging_content_analysis", "version": REPORT_VERSION,
            "input": {"project": self.settings.project, "video": self.settings.video},
            "timeline": evidence.edit_map.to_mapping(),
            "evidence": {"transcript": evidence.transcript_status, "audio": evidence.audio.summary(),
                         "visual": evidence.visual.summary(),
                         "webcam": {"status": evidence.roi.status, "origin": evidence.roi.origin,
                                    "roi": evidence.roi.rect.to_list() if evidence.roi.rect else None,
                                    "message": evidence.roi.message, "detection": evidence.roi_evidence}},
            "frames": [r.to_mapping() for r in frames.records.values()],
            "contact_sheets": evidence.sheets, "timings_s": dict(self.timings), "warnings": list(self.warnings)})

    # ---- agent phases ----------------------------------------------------------------------------
    def _agent_images(self, folder: Path, evidence: Evidence, *, peaks: int) -> list[dict[str, Any]]:
        """Contact sheets + a few full peak frames with a 10x10 reference grid (for card boxes)."""
        cv2 = require_cv2()
        frames = self._frames()
        images: list[dict[str, Any]] = []
        for sheet in evidence.sheets:
            images.append({"path": self.out / sheet["path"], "description": sheet["description"]})
        ranked = sorted(evidence.highlights, key=lambda h: h.local_score, reverse=True)
        for h in ranked[:peaks]:
            peak = next((f for f in h.frame_ids if frames.records[f].role == "peak"), None)
            if peak is None or peak not in frames.images:
                continue
            image = cv2.resize(frames.images[peak], (1600, 900), interpolation=cv2.INTER_AREA)
            for k in range(1, 10):
                cv2.line(image, (160 * k, 0), (160 * k, 899), (0, 255, 255), 1)
                cv2.line(image, (0, 90 * k), (1599, 90 * k), (0, 255, 255), 1)
                cv2.putText(image, f"{k / 10:.1f}", (160 * k + 3, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 255), 1)
                cv2.putText(image, f"{k / 10:.1f}", (3, 90 * k - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 255), 1)
            path = folder / f"grid_{peak}.jpg"
            write_image(path, image, quality=90)
            images.append({"path": path, "description": f"frame {peak} (peak of {h.id}) with a grid: lines every 0.1 "
                                                        "of width/height, for normalized boxes"})
        return images

    def _story(self, agent: PackagingAgent, evidence: Evidence) -> dict[str, Any]:
        s = self.settings
        frames = self._frames()
        frame_maps = [r.to_mapping() for r in frames.records.values()]
        status = {"transcript": evidence.transcript_status["status"], "audio": evidence.audio.status,
                  "visual": evidence.visual.status, "webcam": evidence.roi.status}

        def validator(raw: Any) -> dict[str, Any]:
            return story.validate_story(raw, highlight_ids={h.id for h in evidence.highlights},
                                        frame_ids=set(frames.records), transcript_ids={line.id for line in evidence.lines},
                                        game_ids={g.game_id for g in evidence.games}, max_deepen=s.deepen_limit,
                                        edited_duration=evidence.edit_map.duration)

        with tempfile.TemporaryDirectory(prefix="recut-pack-inputs-") as folder:
            images = self._agent_images(Path(folder), evidence, peaks=6)
            prompt = story.build_prompt(language=s.language, edited_duration=evidence.edit_map.duration,
                                        games=evidence.games, highlights=evidence.highlights, lines=evidence.lines,
                                        frames=frame_maps, images=images, evidence_status=status,
                                        max_deepen=s.deepen_limit)
            from analysis.hero_groups.packaging import group_context
            prompt += "\n" + "\n".join(group_context(evidence.payload))
            result = self._timed("agent_story", lambda: agent.run(
                "video_story", prompt=prompt, schema=story.RESPONSE_SCHEMA, system_prompt=story.SYSTEM_PROMPT,
                prompt_version=story.PROMPT_VERSION, images=[Path(i["path"]) for i in images], validate=validator))
            video_story = result.answer
            requests = video_story.get("deepen_requests", [])[: s.deepen_limit]
            if requests:
                extra = self._timed("deepen_extraction", lambda: self._deepen(evidence, requests, Path(folder)))
                frame_maps = [r.to_mapping() for r in frames.records.values()]
                prompt = story.build_prompt(language=s.language, edited_duration=evidence.edit_map.duration,
                                            games=evidence.games, highlights=evidence.highlights, lines=evidence.lines,
                                            frames=frame_maps, images=extra, evidence_status=status, max_deepen=0,
                                            previous_story=video_story, include_transcript=False)

                def validator2(raw: Any) -> dict[str, Any]:
                    return story.validate_story(raw, highlight_ids={h.id for h in evidence.highlights},
                                                frame_ids=set(frames.records),
                                                transcript_ids={line.id for line in evidence.lines},
                                                game_ids={g.game_id for g in evidence.games}, max_deepen=0,
                                                edited_duration=evidence.edit_map.duration)
                prompt += "\n" + "\n".join(group_context(evidence.payload))
                refined = self._timed("agent_story_deepened", lambda: agent.run(
                    "video_story_deepened", prompt=prompt, schema=story.RESPONSE_SCHEMA,
                    system_prompt=story.SYSTEM_PROMPT, prompt_version=story.PROMPT_VERSION,
                    images=[Path(i["path"]) for i in extra], validate=validator2))
                video_story = {**refined.answer, "deepened": requests}
        for game in evidence.games:
            game.interpretation = next((g for g in video_story.get("games", []) if g.get("game_id") == game.game_id), {})
        self._write("video_story.json", {"format": "recut_video_story", "prompt_version": story.PROMPT_VERSION,
                                         "provider": s.provider, "story": video_story})
        self._write("game_contexts.json", {"games": [g.to_mapping() for g in evidence.games]})
        self._write("content_analysis.json", {**json.loads((self.out / "content_analysis.json").read_text(encoding="utf-8")),
                                              "frames": [r.to_mapping() for r in frames.records.values()]})
        return video_story

    def _fast_context(self, evidence: Evidence) -> dict[str, Any]:
        frames = self._frames()
        games = {g.game_id: g.to_mapping() for g in evidence.games}
        supported = {str(len([g for g in evidence.games if g.index is not None])),
                     str(len([g for g in evidence.games if g.result == "WIN"]))}
        supported |= {str(g.index) for g in evidence.games if g.index is not None}
        return dict(n_titles=self.settings.titles, n_concepts=self.settings.thumbnails,
                    highlight_ids={h.id for h in evidence.highlights},
                    frame_ids={r.id for r in frames.records.values() if r.kind == "gameplay"},
                    webcam_ids={r.id for r in frames.records.values() if r.kind == "webcam"},
                    transcript={line.id: line.text for line in evidence.lines}, games=games,
                    highlight_games={h.id: h.game_id for h in evidence.highlights}, supported_numbers=supported,
                    edited_duration=evidence.edit_map.duration)

    def _fast(self, agent: PackagingAgent, evidence: Evidence) -> tuple[dict[str, Any], dict[str, Any]]:
        """FAST path: PackagingBrief -> ONE call (story + concepts + titles) -> optional ONE patch call."""
        from . import fast
        from .brief import build_brief

        s = self.settings
        frames = self._frames()
        context = self._fast_context(evidence)
        profile = self._style_profile()
        from analysis.hero_groups.packaging import group_context
        style_lines = prompt_summary(profile, max_examples=6) + group_context(evidence.payload)
        valid = {"highlights": sorted(context["highlight_ids"]), "gameplay frames": sorted(context["frame_ids"]),
                 "webcam frames": sorted(context["webcam_ids"]), "games": sorted(context["games"])}

        def validator(raw: Any) -> dict[str, Any]:
            return fast.validate(raw, max_deepen=s.deepen_limit, **context)

        def repair(previous: Any, error: str) -> str:
            return fast.repair_prompt(previous, error, valid=valid, n_titles=s.titles, n_concepts=s.thumbnails)

        with tempfile.TemporaryDirectory(prefix="recut-pack-inputs-") as folder:
            brief = self._timed("brief", lambda: build_brief(
                games=evidence.games, highlights=evidence.highlights, lines=evidence.lines, frames=frames,
                sheets_dir=self.out / "contact_sheets", work_dir=Path(folder)))
            self.brief_stats = brief.stats()
            prompt = fast.build_prompt(language=s.language, brief=brief, n_titles=s.titles, n_concepts=s.thumbnails,
                                       style_lines=style_lines, images=brief.images, max_deepen=s.deepen_limit,
                                       webcam_available=bool(context["webcam_ids"]))
            result = self._timed("agent_fast", lambda: agent.run(
                "packaging_fast", prompt=prompt, schema=fast.RESPONSE_SCHEMA, system_prompt=fast.SYSTEM_PROMPT,
                prompt_version=fast.PROMPT_VERSION, images=[Path(i["path"]) for i in brief.images],
                validate=validator, repair=repair))
            raw = dict(result.answer)
            requests = raw.get("deepen_requests") or []
            if requests and s.deepen_limit > 0:
                raw = self._fast_patch(agent, evidence, raw, requests[0], Path(folder), context)
        observations = list(raw.get("card_observations") or [])
        cards = self._timed("cards", lambda: extract_cards(observations, frames, evidence.lines))
        by_obs = {str(c.provenance.get("obs_id")): c for c in cards}
        for concept in raw["concepts"]:
            mapped = [by_obs[o].id for o in concept.get("card_asset_ids") or [] if o in by_obs]
            if len(mapped) != len(concept.get("card_asset_ids") or []):
                self.warnings.append(f"{concept['concept_id']}: a card reading could not be cropped and was dropped")
            concept["card_asset_ids"] = mapped
        self._write("cards.json", {"cards": [c.to_mapping() for c in cards]})
        video_story = {**fast.to_story(raw), "mode": "fast", "deepened": raw.get("deepened")}
        for game in evidence.games:
            game.interpretation = next((g for g in video_story.get("games") or [] if g.get("game_id") == game.game_id), {})
        self._write("video_story.json", {"format": "recut_video_story", "prompt_version": fast.PROMPT_VERSION,
                                         "provider": s.provider, "mode": "fast", "story": video_story})
        self._write("game_contexts.json", {"games": [g.to_mapping() for g in evidence.games]})
        self._write("content_analysis.json", {**json.loads((self.out / "content_analysis.json").read_text(encoding="utf-8")),
                                              "frames": [r.to_mapping() for r in frames.records.values()],
                                              "brief": self.brief_stats})
        proposals = {k: raw[k] for k in ("concepts", "titles", "preferred_title_id", "preferred_concept_id")}
        self._write("title_candidates.json", {
            "format": "recut_title_candidates", "prompt_version": fast.PROMPT_VERSION, "provider": s.provider,
            "language": s.language, "preferred_title_id": proposals["preferred_title_id"],
            "titles": proposals["titles"]})
        return video_story, proposals

    def _fast_patch(self, agent: PackagingAgent, evidence: Evidence, raw: dict[str, Any], request: dict[str, Any],
                    folder: Path, context: dict[str, Any]) -> dict[str, Any]:
        """The single optional deepening: ReCut extracts the interval, the agent returns only the missing fact
        and the updated concept. A failed patch keeps the original, already valid concept."""
        from analysis.boundaries.agents import AgentCallError

        from . import fast

        concept = next(c for c in raw["concepts"] if c["concept_id"] == request["concept_id"])
        images = self._timed("deepen_extraction", lambda: self._deepen(
            evidence, [{**request, "reason": request.get("question", "")}], folder))
        context = {**context, "frame_ids": {r.id for r in self._frames().records.values() if r.kind == "gameplay"},
                   "webcam_ids": {r.id for r in self._frames().records.values() if r.kind == "webcam"}}
        valid = {"highlights": sorted(context["highlight_ids"]), "gameplay frames": sorted(context["frame_ids"]),
                 "webcam frames": sorted(context["webcam_ids"]),
                 "card obs ids": [o["obs_id"] for o in raw.get("card_observations") or []]}

        def merged(patch: Any) -> dict[str, Any]:
            if not isinstance(patch, dict) or not isinstance(patch.get("concept"), dict):
                raise ValueError("patch must contain a concept object")
            if patch["concept"].get("concept_id") != concept["concept_id"]:
                raise ValueError("patch must keep the same concept_id")
            out = json.loads(json.dumps(raw))
            out["concepts"] = [patch["concept"] if c["concept_id"] == concept["concept_id"] else c
                               for c in out["concepts"]]
            out["card_observations"] = list(out.get("card_observations") or []) + list(patch.get("card_observations") or [])
            out["deepen_requests"] = []
            return fast.validate(out, max_deepen=0, **context)

        prompt = fast.patch_prompt(language=self.settings.language, request=request, concept=concept, images=images,
                                   valid=valid)
        try:
            result = self._timed("agent_patch", lambda: agent.run(
                "packaging_fast_patch", prompt=prompt, schema=fast.PATCH_SCHEMA, system_prompt=fast.SYSTEM_PROMPT,
                prompt_version=fast.PATCH_VERSION, images=[Path(i["path"]) for i in images],
                validate=lambda answer: (merged(answer), answer)[1]))
        except AgentCallError as error:
            self.warnings.append(f"deepen patch failed ({error.code}); the original concept is kept")
            return {**raw, "deepen_requests": [], "deepened": {"request": request, "status": f"failed: {error.code}"}}
        out = merged(result.answer)
        out["deepened"] = {"request": request, "answer": result.answer.get("answer"), "status": "applied"}
        return out

    def _deepen(self, evidence: Evidence, requests: list[dict[str, Any]], folder: Path) -> list[dict[str, Any]]:
        """ReCut extracts the requested bounded intervals; the agent never touches files."""
        frames = self._frames()
        images = []
        for n, request in enumerate(requests, start=1):
            a, b = float(request["edited_start"]), float(request["edited_end"])
            records: list[FrameRecord] = []
            for k in range(6):
                edited = a + (b - a) * (k + 0.5) / 6
                source_index, t = evidence.edit_map.to_source(edited)
                found = frames.best_frame(source_index, t, radius=0.4)
                if found is None:
                    continue
                if request.get("want") == "webcam" and evidence.roi.rect is not None:
                    record = frames.save(evidence.roi.rect.crop(found[1]), source_index=source_index,
                                         source_time=found[0], kind="webcam", role="deepen", prefix="W",
                                         folder="webcam", notes=f"deepen request {n}")
                else:
                    record = frames.save(found[1], source_index=source_index, source_time=found[0], kind="gameplay",
                                         role="deepen", notes=f"deepen request {n}")
                records.append(record)
            if records:
                path = self.out / "contact_sheets" / f"deepen_{n}.jpg"
                contact_sheet(records, frames.images, path, columns=3, title=f"deepen {n}: {request.get('reason', '')}")
                images.append({"path": path, "description": f"deepened interval {n} ({request.get('reason', '')}): "
                                                            f"frames {', '.join(r.id for r in records)}"})
        return images

    def _style_profile(self) -> dict[str, Any] | None:
        if not self.settings.creator or not self.settings.use_style:
            return None
        try:
            return CreatorStyleStore(self.settings.style_dir).load(self.settings.creator)
        except KeyError as exc:
            raise PackagingError("creator_not_found", str(exc)) from exc

    def _creative(self, agent: PackagingAgent, evidence: Evidence, video_story: dict[str, Any],
                  cards: list[dict[str, Any]], *, only: str | None = None,
                  previous: dict[str, Any] | None = None) -> dict[str, Any]:
        s = self.settings
        frames = self._frames()
        gameplay = [r.to_mapping() for r in frames.records.values() if r.kind == "gameplay"]
        webcam = [r.to_mapping() for r in frames.records.values() if r.kind == "webcam"]
        transcript = {line.id: line.text for line in evidence.lines}
        cited = set()
        for h in evidence.highlights:
            cited |= set(h.transcript_ids)
        for item in video_story.get("strongest_highlights", []):
            cited |= set(item.get("transcript_ids", []))
        shown = {tid: transcript[tid] for tid in sorted(cited) if tid in transcript}
        games = {g.game_id: g.to_mapping() for g in evidence.games}
        supported = {str(len([g for g in evidence.games if g.index is not None]))}
        supported |= {str(len([g for g in evidence.games if g.result == "WIN"]))}
        supported |= {str(g.index) for g in evidence.games if g.index is not None}
        card_map = {c["id"]: c for c in cards}
        uncertain = [c["name"] for c in cards if c["status"] == "uncertain" and c.get("name")]
        from analysis.hero_groups.packaging import group_context
        style_lines = prompt_summary(self._style_profile()) + group_context(evidence.payload)

        def validator(raw: Any) -> dict[str, Any]:
            return creative.validate_creative(
                raw, n_titles=s.titles, n_concepts=s.thumbnails, highlight_ids={h.id for h in evidence.highlights},
                frame_ids={f["id"] for f in gameplay}, webcam_ids={w["id"] for w in webcam}, cards=card_map,
                transcript=transcript, games=games, highlight_games={h.id: h.game_id for h in evidence.highlights},
                supported_numbers=supported, uncertain_names=uncertain)

        with tempfile.TemporaryDirectory(prefix="recut-pack-inputs-") as folder:
            images = self._creative_images(Path(folder), evidence, video_story, cards)
            prompt = creative.build_prompt(
                language=s.language, story=video_story, games=list(games.values()),
                highlights=[h.to_mapping() for h in evidence.highlights], frames=gameplay, webcam=webcam, cards=cards,
                transcript=shown, n_titles=s.titles, n_concepts=s.thumbnails, style_lines=style_lines, images=images,
                only=only, previous=previous)
            result = self._timed("agent_creative", lambda: agent.run(
                f"creative_{only or 'all'}", prompt=prompt, schema=creative.RESPONSE_SCHEMA,
                system_prompt=creative.SYSTEM_PROMPT, prompt_version=creative.PROMPT_VERSION,
                images=[Path(i["path"]) for i in images], validate=validator))
        proposals = result.answer
        self._write("title_candidates.json", {
            "format": "recut_title_candidates", "prompt_version": creative.PROMPT_VERSION, "provider": s.provider,
            "language": s.language, "preferred_title_id": proposals["preferred_title_id"],
            "titles": proposals["titles"]})
        return proposals

    def _creative_images(self, folder: Path, evidence: Evidence, video_story: dict[str, Any],
                         cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The candidate backgrounds themselves (1280 px), plus webcam and card sheets."""
        cv2 = require_cv2()
        frames = self._frames()
        images: list[dict[str, Any]] = []
        wanted: list[str] = []
        for item in video_story.get("strongest_highlights", []):
            wanted += [f for f in item.get("frame_ids", []) if frames.records.get(f) and frames.records[f].kind == "gameplay"]
            h = next((x for x in evidence.highlights if x.id == item.get("highlight_id")), None)
            if h is not None:
                wanted += h.frame_ids
        for frame_id in list(dict.fromkeys(wanted))[:9]:
            path = folder / f"{frame_id}.jpg"
            write_image(path, cv2.resize(frames.images[frame_id], (1280, 720), interpolation=cv2.INTER_AREA), quality=90)
            record = frames.records[frame_id]
            images.append({"path": path, "description": f"gameplay frame {frame_id} ({record.highlight_id or ''} "
                                                        f"{record.role}) full frame"})
        sheet = next((sh for sh in evidence.sheets if sh["path"].endswith("webcam.jpg")), None)
        if sheet:
            images.append({"path": self.out / sheet["path"], "description": sheet["description"]})
        if cards:
            records = [FrameRecord(c["id"], "card", 0, c["source_time"], frames.records[c["frame_id"]].edited_time
                                   if c["frame_id"] in frames.records else None, c["path"], 0, 0) for c in cards]
            path = folder / "cards.jpg"
            contact_sheet(records, frames.images, path, columns=4, cell_width=320, title="card crops (id)")
            images.append({"path": path, "description": f"card crops {', '.join(c['id'] for c in cards)}"})
        return images

    # ---- rendering -------------------------------------------------------------------------------
    def _render_all(self, proposals: dict[str, Any], evidence: Evidence | None) -> list[dict[str, Any]]:
        """Render every concept, run the deterministic quality gate, apply automatic fixes (max 3 renders each)."""
        from . import quality

        if self.settings.asset_engine and evidence is not None:
            scenes = self._render_scenes(proposals, evidence)
            if self.settings.thumbnail_engine == "gen":
                scenes = self._generate_finals(proposals, scenes, evidence)
            return scenes

        frames = self._frames()
        profile = self._style_profile()
        brand = (profile or {}).get("brand", {}) if profile and profile.get("enabled", True) else {}
        fonts = list(brand.get("fonts", []) or [])
        style = {"webcam_scale": {"small": 0.75, "medium": 1.0, "large": 1.15}.get(str(brand.get("webcam_size")), 1.0),
                 "typography": brand.get("typography")}
        renderer = ThumbnailRenderer(extra_fonts=[f for f in fonts if Path(f).suffix.lower() in (".ttf", ".otf")])
        mask = [evidence.roi.rect.to_list()] if evidence is not None and evidence.roi.rect is not None else []
        sharp = [float(r.quality.get("sharpness", 0.0)) for r in frames.records.values() if r.kind == "gameplay"]
        reference = float(np.median(sharp)) if sharp else 0.0
        results: list[dict[str, Any]] = []

        def alternatives(concept: dict[str, Any]) -> list[str]:
            current = concept["composition_plan"]["background_frame_id"]
            ids = [f for h in concept.get("highlight_ids", []) for f in self._highlight_frames(h)]
            ids = [f for f in dict.fromkeys(ids) if f != current and f in frames.images]
            # best composed first (selection score from rank_gameplay), sharpness as tie-breaker for older runs
            return sorted(ids, key=lambda f: (-float(frames.records[f].quality.get("selection", 0.0)),
                                              -float(frames.records[f].quality.get("sharpness", 0.0))))

        def render_one(n: int, concept: dict[str, Any], *, restore: bool = True) -> dict[str, Any]:
            target = self.out / "thumbnails" / f"concept_{n:02d}.{self.settings.image_format}"
            fixes: list[str] = []
            issues: list[quality.GateIssue] = []
            result = None
            if restore:
                quality.restore_original(concept["composition_plan"], concept)
            for _ in range(3):
                plan = plan_from_mapping(concept, frames.images, fonts=[Path(f).stem for f in fonts],
                                         mask_boxes=mask, style=style)
                rendered = renderer.render(plan, target, fmt=self.settings.image_format)
                issues = quality.inspect(rendered, plan, reference_sharpness=reference)
                result = rendered
                fixable = [i for i in issues if i.fix]
                if not fixable:
                    break
                issue = fixable[0]
                composition = concept["composition_plan"]
                if issue.fix == "swap_background":
                    options = alternatives(concept)
                    if not options:
                        break
                    quality.remember(composition, concept, "background_frame_id")
                    quality.remember(composition, concept, "focus_box")
                    composition["render_tuning"]["auto"] = True
                    composition["background_frame_id"], composition["focus_box"] = options[0], None
                    fixes.append(f"{issue.code}: background -> {options[0]}")
                elif quality.apply_fix(composition, concept, issue.fix or ""):
                    fixes.append(f"{issue.code}: {issue.fix}")
                else:
                    break
            assert result is not None
            mapping = result.to_mapping()
            mapping["path"] = str(target.relative_to(self.out))
            mapping["quality"] = {"passed": not issues, "issues": [i.to_mapping() for i in issues], "fixes": fixes}
            return mapping

        def render() -> None:
            for n, concept in enumerate(proposals["concepts"], start=1):
                try:
                    results.append({"concept_id": concept["concept_id"], **render_one(n, concept)})
                except (KeyError, ValueError) as exc:
                    results.append({"concept_id": concept["concept_id"], "error": f"{type(exc).__name__}: {exc}"})
                    self.warnings.append(f"{concept['concept_id']} not rendered: {exc}")
            paths = [self.out / r["path"] for r in results if "path" in r]
            for i, j, distance in quality.similar_pairs(paths):  # near-duplicate variants: change the later one
                concept = proposals["concepts"][j]
                options = alternatives(concept)
                note = f"too similar to {proposals['concepts'][i]['concept_id']} (hash distance {distance})"
                if options:
                    composition = concept["composition_plan"]
                    quality.remember(composition, concept, "background_frame_id")
                    quality.remember(composition, concept, "focus_box")
                    composition["render_tuning"]["auto"] = True
                    composition["background_frame_id"], composition["focus_box"] = options[0], None
                    redone = render_one(j + 1, concept, restore=False)
                    redone["quality"]["fixes"].append(f"{note}: background -> {options[0]}")
                    results[j] = {"concept_id": concept["concept_id"], **redone}
                else:
                    results[j].setdefault("quality", {}).setdefault("issues", []).append(
                        {"code": "too_similar", "fix": None, "detail": note})
        self._timed("thumbnail_render", render)
        overview = []
        for concept, rendered in zip(proposals["concepts"], results):
            image = read_image(self.out / rendered["path"]) if "path" in rendered else None
            if image is not None:
                overview.append((f"{concept['concept_id']}: {concept['title']}", image))
        if overview:
            self._overview(overview)
        concepts = [{**concept, "render": rendered} for concept, rendered in zip(proposals["concepts"], results)]
        self._write("thumbnail_concepts.json", {
            "format": "recut_thumbnail_concepts", "prompt_version": creative.PROMPT_VERSION,
            "renderer": RENDERER_VERSION, "provider": self.settings.provider,
            "preferred_concept_id": proposals["preferred_concept_id"], "concepts": concepts})
        return results

    def _highlight_frames(self, highlight_id: str) -> list[str]:
        return [r.id for r in self._frames().records.values() if r.highlight_id == highlight_id and r.kind == "gameplay"]

    def _overview(self, items: list[tuple[str, np.ndarray]]) -> None:
        cv2 = require_cv2()
        cells = []
        for label, image in items:
            cell = cv2.resize(image, (640, 360), interpolation=cv2.INTER_AREA)
            band = np.zeros((40, 640, 3), np.uint8)
            from .frames import _ascii
            text = _ascii(label)[:70]
            cv2.putText(band, text, (8, 27), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1, cv2.LINE_AA)
            cells.append(np.vstack([cell, band]))
        rows = [np.hstack(cells[i:i + 2] + [np.zeros_like(cells[0])] * (2 - len(cells[i:i + 2])))
                for i in range(0, len(cells), 2)]
        write_image(self.out / "thumbnails" / "overview.jpg", np.vstack(rows), quality=90)

    # ---- partial regeneration ----------------------------------------------------------------------
    def _load_previous(self) -> tuple[Evidence, dict[str, Any] | None, dict[str, Any], dict[str, Any]]:
        """Rebuild evidence objects from the output folder (no media decoding, no ASR)."""
        analysis = json.loads((self.out / "content_analysis.json").read_text(encoding="utf-8"))
        input_ = analysis.get("input", {})
        if not self.settings.project and not self.settings.video:
            self.settings.project, self.settings.video = input_.get("project"), input_.get("video")
        timeline = analysis["timeline"]
        from .timeline import KeepPiece

        edit_map = EditMap(timeline["sources"], [KeepPiece(p["source_index"], p["source_start"], p["source_end"],
                                                           p["edited_start"]) for p in timeline["pieces"]],
                           origin=timeline["origin"], project_path=timeline.get("project"))
        lines = [TranscriptLine.from_mapping(item) for item in
                 json.loads((self.out / "transcript.json").read_text(encoding="utf-8"))["lines"]]
        games = [GameContext.from_mapping(g) for g in
                 json.loads((self.out / "game_contexts.json").read_text(encoding="utf-8"))["games"]]
        highlights = [HighlightCandidate.from_mapping(h) for h in
                      json.loads((self.out / "highlights.json").read_text(encoding="utf-8"))["highlights"]]
        webcam = analysis["evidence"]["webcam"]
        roi = parse_roi(webcam["roi"], origin=webcam.get("origin", "")) if webcam.get("roi") else RoiCheck("absent", None)
        self.frames = FrameExtractor(edit_map, self.out, self.cache, sampler=self._sampler, cancellation=self.cancellation)
        for item in analysis["frames"]:
            record = FrameRecord.from_mapping(item)
            image = read_image(self.out / record.path)
            if image is None:
                raise PackagingError("missing_artifact", f"{record.path} is missing from the previous run")
            self.frames.records[record.id] = record
            self.frames.images[record.id] = image
        if (self.out / "visual_assets.json").is_file():  # V3 runs: cut-outs (alpha) and board regions
            from .assets import VisualAssetManifest
            from .frames import read_image_unchanged
            from .opportunities import from_mappings

            self.manifest = VisualAssetManifest.from_mapping(
                json.loads((self.out / "visual_assets.json").read_text(encoding="utf-8")))
            for asset in self.manifest.assets:
                if asset.id not in self.frames.images:
                    loaded = read_image_unchanged(self.out / asset.path) if asset.has_alpha else \
                        read_image(self.out / asset.path)
                    if loaded is not None:
                        self.frames.images[asset.id] = loaded
            if (self.out / "opportunities.json").is_file():
                self.opportunities = from_mappings(json.loads(
                    (self.out / "opportunities.json").read_text(encoding="utf-8"))["opportunities"])
        cards: dict[str, Any] = {"cards": []}
        if (self.out / "cards.json").is_file():
            cards = json.loads((self.out / "cards.json").read_text(encoding="utf-8"))
            for card in cards["cards"]:
                image = read_image(self.out / card["path"])
                if image is not None:
                    self.frames.images[card["id"]] = image
        evidence = Evidence(edit_map, None, lines, analysis["evidence"]["transcript"],
                            AudioProfile(analysis["evidence"]["audio"]["status"]),
                            VisualProfile(analysis["evidence"]["visual"]["status"]), games, highlights, roi,
                            sheets=analysis.get("contact_sheets", []))
        story_file = self.out / "video_story.json"
        video_story = json.loads(story_file.read_text(encoding="utf-8"))["story"] if story_file.is_file() else None
        concepts = json.loads((self.out / "thumbnail_concepts.json").read_text(encoding="utf-8")) \
            if (self.out / "thumbnail_concepts.json").is_file() else {}
        titles = json.loads((self.out / "title_candidates.json").read_text(encoding="utf-8")) \
            if (self.out / "title_candidates.json").is_file() else {}
        return evidence, video_story, concepts, {**titles, "cards": cards["cards"]}

    def _regenerate(self, total: float) -> dict[str, Any]:
        only = self.settings.only
        evidence, video_story, concepts, titles = self._timed("load_previous", self._load_previous)
        cards = titles.pop("cards", [])
        self._archive(AGENT_FILES, ("thumbnails",))
        if only == "render":
            if not concepts.get("concepts"):
                raise PackagingError("no_previous_run", "--only render needs thumbnail_concepts.json")
            proposals = {"concepts": [{k: v for k, v in c.items() if k not in ("render", "scene_rendered")}
                                      for c in concepts["concepts"]],
                         "portfolio_strategy": concepts.get("portfolio_strategy"),
                         "preferred_concept_id": concepts.get("preferred_concept_id"),
                         "titles": titles.get("titles", []), "preferred_title_id": titles.get("preferred_title_id")}
            if self.settings.thumbnail_engine == "gen":
                # Render-only is local: reuse this output's saved generation, never contact a provider.
                finals: dict[str, Path] = {}
                ids = [str(c["concept_id"]) for c in concepts["concepts"]]
                for concept in concepts["concepts"]:
                    raw = (concept.get("render") or {}).get("generated")
                    if not raw:
                        continue
                    saved = (self.out / str(raw)).resolve()
                    if saved.is_relative_to(self.out) and saved.is_file() and read_image(saved) is not None:
                        finals[str(concept["concept_id"])] = saved
                self._prefetched_generation = (finals, ids, {
                    "engine": "gen", "status": "done" if len(finals) == len(ids) else "partial" if finals else "skipped",
                    "calls_made": 0, "cache_hit": bool(finals), "reused_existing": True,
                })
            renders = self._render_all(proposals, evidence)
            return self._report(evidence, status="done", total=total, story_=video_story, proposals=proposals,
                                renders=renders)
        if video_story is None:
            raise PackagingError("no_previous_run", f"--only {only} needs video_story.json from a previous agent run")
        agent = self._make_agent()
        previous = {"concepts": [{k: v for k, v in c.items() if k != "render"} for c in concepts.get("concepts", [])],
                    "titles": titles.get("titles", [])}
        try:
            if self.settings.asset_engine and self.manifest is not None:
                _, proposals = self._fast_v3(agent, evidence, story=video_story, task=f"portfolio_{only}_v3")
                if only == "titles" and previous["concepts"] and all("scene" in c for c in previous["concepts"]):
                    by_order = {new["concept_id"]: old["concept_id"]
                                for new, old in zip(proposals["concepts"], previous["concepts"])}
                    for t in proposals["titles"]:  # keep the existing thumbnails, adopt the new titles
                        t["concept_id"] = by_order.get(t["concept_id"], t["concept_id"])
                    proposals["concepts"] = previous["concepts"]
                    proposals["preferred_concept_id"] = concepts.get("preferred_concept_id")
            else:
                proposals = self._creative(agent, evidence, video_story, cards, only=only, previous=previous)
        except Exception as exc:
            from analysis.boundaries.agents import AgentCallError
            if isinstance(exc, AgentCallError):
                raise PackagingError(f"agent_{exc.code}", f"{self.settings.provider} failed ({exc.code}): {exc.detail}") from exc
            raise
        renders = self._render_all(proposals, evidence)
        return self._report(evidence, status="done", total=total, agent=agent, story_=video_story,
                            proposals=proposals, renders=renders)

    # ---- report ----------------------------------------------------------------------------------
    def _report(self, evidence: Evidence, *, status: str, total: float, agent: PackagingAgent | None = None,
                story_: dict[str, Any] | None = None, proposals: dict[str, Any] | None = None,
                renders: list[dict[str, Any]] | None = None, error: dict[str, Any] | None = None) -> dict[str, Any]:
        s = self.settings
        self.timings["total"] = round(time.perf_counter() - total, 3)
        calls = agent.calls if agent is not None else []
        costs = [float(c["cost_usd"]) for c in calls if isinstance(c.get("cost_usd"), (int, float))]
        preferred_title = None
        if proposals:
            preferred_title = next((t for t in proposals["titles"] if t["title_id"] == proposals["preferred_title_id"]), None)
        report = {
            "format": REPORT_FORMAT, "version": REPORT_VERSION, "status": status,
            "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "output": str(self.out),
            "input": {"project": s.project, "video": s.video, "edited_duration_s": round(evidence.edit_map.duration, 3),
                      "kept_pieces": len(evidence.edit_map.pieces), "edit_digest": evidence.edit_map.digest()},
            "settings": {k: v for k, v in asdict(s).items() if k not in ("cache_dir", "style_dir")},
            "privacy": None if s.analyze_only and not calls else PRIVACY_NOTE.format(provider=s.provider),
            "evidence": {"transcript": evidence.transcript_status.get("status"), "transcript_lines": len(evidence.lines),
                         "audio": evidence.audio.status, "visual": evidence.visual.status,
                         "webcam": evidence.roi.status, "games": len(evidence.games),
                         "highlights": len(evidence.highlights),
                         "frames": len([r for r in self._frames().records.values() if r.kind == "gameplay"]),
                         "webcam_frames": len([r for r in self._frames().records.values() if r.kind == "webcam"])},
            "games": [{"game_id": g.game_id, "index": g.index, "result": g.result, "edited_start": g.edited_start,
                       "edited_end": g.edited_end, "highlights": g.highlight_ids} for g in evidence.games],
            "story": {"main_topic": story_.get("main_topic"), "themes": story_.get("themes")} if story_ else None,
            "preferred_title": preferred_title,
            "preferred_concept_id": proposals.get("preferred_concept_id") if proposals else None,
            "titles": [{"title_id": t["title_id"], "title": t["title"], "concept_id": t["concept_id"],
                        "highlight_ids": t["highlight_ids"]} for t in (proposals or {}).get("titles", [])],
            "thumbnails": renders or [],
            "mode": s.mode, "brief": getattr(self, "brief_stats", None), "thumbnail_engine": s.thumbnail_engine,
            "visual_assets": self.manifest.to_mapping()["counts"] if self.manifest is not None else None,
            "generation": getattr(self, "generation_report", None),
            "opportunities": len(self.opportunities),
            "portfolio": ({"passed": self.portfolio_report["diversity"]["passed"],
                           "regenerate_recommended": self.portfolio_report["regenerate_recommended"],
                           "metrics": self.portfolio_report["diversity"]["metrics"]}
                          if self.portfolio_report else None),
            "agent": {"provider": s.provider, "effort": s.effective_effort, "calls_made": len(
                          [c for c in calls if not c.get("cache_hit")]),
                      "cache_hits": len([c for c in calls if c.get("cache_hit")]), "calls": calls,
                      "cost_usd_total": round(sum(costs), 4) if costs else None,
                      "tokens_used_total": sum(c.get("tokens_used") or 0 for c in calls) or None},
            "extensions": extension_status(),
            "cache": self.cache.summary(), "timings_s": dict(self.timings), "warnings": list(self.warnings),
            "error": error,
            "artifacts": {name: name for name in ANALYSIS_FILES + AGENT_FILES if (self.out / name).exists()},
        }
        self._write("packaging_report.json", report)
        return report


def run_packaging(settings: PackagingSettings, **kwargs: Any) -> dict[str, Any]:
    return PackagingPipeline(settings, **kwargs).run()
