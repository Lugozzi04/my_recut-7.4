"""ClipSession: one edit (project or video) -> candidates in clips.json -> rendered clips.

The analysis is the packaging one (transcript, highlights, games, edit map). An existing packaging run of the
SAME edit (same timeline digest) is reused read-only; otherwise a local-only packaging analysis is created in
<clips>/analysis (no provider call, shared caches make it cheap after a packaging run).
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Sequence

from .candidates import MAX_S, MIN_S, Analysis
from .layout import VerticalLayout, compute_layout
from .models import FORMAT, FORMATS, VERSION, ClipCandidate, ClipError

Progress = Callable[[str, str], None]
ENGINE = "clip_finder_v3"  # dense sentence windows + local signals (result anchor, board, tension) + AI rerank
PROVIDERS = ("codex", "claude")
AI_POOL = 40  # candidates sent to the AI rerank: with 30 (capped at 3 per game) 63 % of the hand-marked strong
# moments of 3 real edits reached the agent, with 40 (cap scaled with the pool) 86 %; prompt <= ~55k chars


def default_folder(project: str | None, video: str | None) -> Path:
    if project:
        path = Path(project).expanduser().resolve()
        return path.parent / (path.stem + "_clips")
    path = Path(str(video)).expanduser().resolve()
    return path.with_name(path.stem + "_clips")


def packaging_folders(project: str | None, video: str | None) -> list[Path]:
    """Where the editor/CLI usually leave a packaging run of this edit."""
    if project:
        path = Path(project).expanduser().resolve()
        return [path.parent / "packaging", path.parent / (path.stem + "_packaging")]
    path = Path(str(video)).expanduser().resolve()
    return [path.with_name(path.stem + "_packaging")]


def _digest(folder: Path) -> str | None:
    try:
        return json.loads((folder / "content_analysis.json").read_text(encoding="utf-8"))["timeline"]["digest"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _safe_name(text: str) -> str:
    return re.sub(r"[^\w\-]+", "_", text, flags=re.UNICODE).strip("_")[:60] or "clip"


class ClipSession:
    def __init__(self, *, project: str | None = None, video: str | None = None, output: str | None = None,
                 analysis: str | None = None, progress: Progress | None = None, cancellation: Any = None,
                 edit_map_factory: Callable[[], Any] | None = None, analyzer: Callable[..., None] | None = None,
                 series_factory: Callable[[Analysis], Any] | None = None,
                 agent_factory: Callable[[str], Any] | None = None) -> None:
        if bool(project) == bool(video):
            raise ClipError("invalid_arguments", "Use exactly one of --project or --video.")
        self.project, self.video = project, video
        self.folder = Path(output).expanduser().resolve() if output else default_folder(project, video)
        self.explicit_analysis = Path(analysis).expanduser().resolve() if analysis else None
        self.progress = progress or (lambda stage, message: None)
        self.cancellation = cancellation
        self._edit_map_factory = edit_map_factory
        self._analyzer = analyzer
        self.analysis: Analysis | None = None
        self._sizes: dict[str, tuple[int, int]] = {}
        self._series_factory = series_factory
        self._agent_factory = agent_factory
        self.ranking: dict[str, Any] = {}
        self.warnings: list[str] = []

    @property
    def store(self) -> Path:
        return self.folder / "clips.json"

    # ---- analysis ----------------------------------------------------------------------------------
    def _edit_digest(self) -> str:
        from analysis.packaging.timeline import EditMap
        if self._edit_map_factory is not None:
            return self._edit_map_factory().digest()
        if self.project:
            return EditMap.from_project(self.project).digest()
        return EditMap.from_video(str(self.video)).digest()

    def _analysis_folder(self) -> Path:
        if self.explicit_analysis is not None:
            if _digest(self.explicit_analysis) is None:
                raise ClipError("analysis_missing", f"No packaging analysis in {self.explicit_analysis}")
            return self.explicit_analysis
        digest = self._edit_digest()
        own = self.folder / "analysis"
        for folder in [own, *packaging_folders(self.project, self.video)]:
            if _digest(folder) == digest:
                return folder
        self.progress("analysis", "local analysis of the edit (transcript, highlights); nothing is sent anywhere")
        (self._analyzer or self._analyze)(own)
        if _digest(own) != digest:
            raise ClipError("analysis_failed", "The local analysis did not produce a usable timeline.")
        return own

    def _analyze(self, folder: Path) -> None:
        from analysis.packaging.pipeline import PackagingPipeline, PackagingSettings
        settings = PackagingSettings(output=str(folder), project=self.project, video=self.video, analyze_only=True,
                                     overwrite=True, games="auto" if self.project else "none", max_highlights=24)
        PackagingPipeline(settings, progress=self.progress, cancellation=self.cancellation).run()

    def load_analysis(self) -> Analysis:
        if self.analysis is None:
            self.analysis = Analysis.load(self._analysis_folder())
        return self.analysis

    # ---- candidates ---------------------------------------------------------------------------------
    def saved(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self.store.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if data.get("format") == FORMAT else None

    def _series(self, analysis: Analysis) -> Any:
        """Loudness/visual series of the edit from the packaging evidence caches (computed when missing)."""
        from .scoring import build_series
        if self._series_factory is not None:
            return self._series_factory(analysis)
        from analysis.packaging.audio import analyze_kept_audio
        from analysis.packaging.cache import PackagingCache
        from analysis.packaging.timeline import EditMap, KeepPiece, _identities
        from analysis.packaging.visual import analyze_kept_board, analyze_kept_visual
        try:
            edit_map = EditMap(analysis.sources, [KeepPiece(i, a, b, e) for i, a, b, e in analysis.pieces],
                               origin="clips", identities=_identities(analysis.sources))
            cache = PackagingCache()
            self.progress("signals", "loudness and board changes of the edit (cached after a packaging run)")
            audio = analyze_kept_audio(edit_map, cache=cache, cancellation=self.cancellation)
            visual = analyze_kept_visual(edit_map, cache=cache, cancellation=self.cancellation)
            self.progress("signals", "board changes without webcam and overlays (cached after the first run)")
            board = analyze_kept_board(edit_map, cache=cache, exclude=analysis.webcam_roi,
                                       cancellation=self.cancellation)
            if board.status == "failed":
                self.warnings.append(f"board changes unavailable ({board.detail}); whole-frame changes used")
            return build_series(analysis, audio, visual, board)
        except Exception as exc:  # noqa: BLE001 - optional evidence: transcript-only ranking still works
            if self.cancellation is not None and self.cancellation.is_cancelled:
                raise ClipError("cancelled", "Cancelled") from exc
            self.warnings.append(f"audio/visual signals unavailable ({exc}); ranking from the transcript only")
            return build_series(analysis)

    def _agent(self, provider: str) -> Any:
        if self._agent_factory is not None:
            return self._agent_factory(provider)
        from analysis.packaging.agent import AgentSettings, PackagingAgent
        from analysis.packaging.cache import PackagingCache
        agent = PackagingAgent(AgentSettings(provider=provider, effort="medium" if provider == "codex" else None,
                                             timeout_s=600.0, max_prompt_chars=90000,
                                             log_path=str(self.folder / "agent_calls.jsonl")), PackagingCache())
        agent.require()
        return agent

    def candidates(self, *, count: int = 10, min_s: float = MIN_S, max_s: float = MAX_S,
                   refresh: bool = False, ai: str | None = None) -> list[ClipCandidate]:
        """ai: None (local ranking only) or a provider (codex/claude) for the rerank of the best windows."""
        from .ai_rank import rerank
        from .scoring import find_clips
        if ai is not None and ai not in PROVIDERS:
            raise ClipError("invalid_arguments", f"--ai must be one of {', '.join(PROVIDERS)} or none")
        if not 5 <= min_s < max_s:
            raise ClipError("invalid_arguments", "Clip length must satisfy 5 <= min < max seconds.")
        analysis = self.load_analysis()
        saved = self.saved() or {}
        settings = {"count": count, "min_s": min_s, "max_s": max_s, "ai": ai}
        same_edit = saved.get("digest") == analysis.digest
        if same_edit and not refresh and saved.get("engine") == ENGINE and saved.get("settings") == settings:
            self.ranking = saved.get("ranking") or {}
            return [ClipCandidate.from_mapping(c) for c in saved.get("clips") or []]
        series = self._series(analysis)
        self.progress("ranking", f"scoring every sentence window of {len(analysis.lines)} transcript lines")
        clips = find_clips(analysis, series, count=count, min_s=min_s, max_s=max_s,
                           pool=max(count * 4, AI_POOL) if ai else count)
        self.ranking = {"engine": ENGINE, "ai": None, "candidates_scored": len(clips)}
        if ai and clips:
            self.progress("ranking", f"AI rerank of the best {len(clips)} windows with {ai} "
                                     "(transcript excerpts are sent to the provider of that CLI)")
            try:
                clips = rerank(analysis, clips, run_agent=self._agent(ai).run, count=count, min_s=min_s, max_s=max_s)
                self.ranking["ai"] = ai
            except Exception as exc:  # noqa: BLE001 - the local ranking is always available
                if self.cancellation is not None and self.cancellation.is_cancelled:
                    raise ClipError("cancelled", "Cancelled") from exc
                self.warnings.append(f"AI ranking failed ({exc}); local ranking kept")
                self.ranking["ai_error"] = str(exc)[:300]
                clips = clips[:count]
                for n, clip in enumerate(clips, start=1):
                    clip.id = f"C{n:02d}"
        if same_edit:  # keep the user's ticks for the same moments (any overlap of half a clip)
            ticked = [ClipCandidate.from_mapping(c) for c in saved.get("clips") or [] if c.get("selected")]
            for clip in clips:
                clip.selected = any(min(clip.edited_end, t.edited_end) - max(clip.edited_start, t.edited_start)
                                    >= 0.5 * min(clip.duration, t.duration) for t in ticked)
        self._settings = settings
        self.save(clips)
        return clips

    def evaluate(self, marks: Sequence[Any], *, count: int = 10, min_s: float = MIN_S, max_s: float = MAX_S,
                 profiles: Sequence[str] = ("v2", "v3")) -> dict[str, dict[str, Any]]:
        """Every local scoring profile (and the proposals saved in clips.json, e.g. AI-ranked) against the marked
        moments (evaluate.Mark). Read-only: clips.json is not changed, no provider is called."""
        from .evaluate import score
        from .scoring import find_clips
        analysis = self.load_analysis()
        late = [m for m in marks if m.start > analysis.duration + 1.0]
        if late:
            self.warnings.append(f"{len(late)} mark(s) after the end of the edit ({analysis.duration:.0f} s): "
                                 "marks are times of the edited video")
        series = self._series(analysis)
        results = {}
        for profile in profiles:
            self.progress("evaluate", f"profile {profile}: {count} proposals")
            results[profile] = score(find_clips(analysis, series, count=count, min_s=min_s, max_s=max_s,
                                                profile=profile), marks)
        saved = self.saved() or {}
        if saved.get("digest") == analysis.digest and saved.get("clips"):
            ranking = saved.get("ranking") or {}
            name = f"saved/{ranking['ai']}" if ranking.get("ai") else "saved"
            results[name] = score([ClipCandidate.from_mapping(c) for c in saved["clips"]][:count], marks)
            results[name]["engine"] = ranking.get("engine")
        return results

    def save(self, clips: Sequence[ClipCandidate], *, exports: list[dict[str, Any]] | None = None) -> None:
        analysis = self.load_analysis()
        previous = (self.saved() or {}).get("exports") or []
        _atomic_json(self.store, {
            "format": FORMAT, "version": VERSION, "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "source": {"project": self.project, "video": self.video}, "analysis": str(analysis.folder),
            "digest": analysis.digest, "sources": analysis.sources, "engine": ENGINE,
            "settings": getattr(self, "_settings", None) or (self.saved() or {}).get("settings"),
            "ranking": self.ranking or (self.saved() or {}).get("ranking") or {}, "warnings": self.warnings,
            "clips": [c.to_mapping() for c in clips], "exports": previous + list(exports or [])})

    # ---- rendering ----------------------------------------------------------------------------------
    def vertical_layout(self, clip: ClipCandidate) -> VerticalLayout:
        """Shorts layout for the source of the clip's first part (webcam ROI of the analysis)."""
        from utils.ffmpeg import ensure_ffmpeg
        analysis = self.load_analysis()
        source = analysis.sources[clip.parts[0][0]] if clip.parts else analysis.sources[0]
        if source not in self._sizes:
            self._sizes[source] = _video_size(ensure_ffmpeg()[1], source)
        width, height = self._sizes[source]
        return compute_layout(width, height, analysis.webcam_roi)

    def _captions(self, clip: ClipCandidate, layout: VerticalLayout, name: str, *, still: bool = False) -> str:
        from .captions import ass_document, clip_words, install_font
        font = install_font(self.folder)
        words = clip_words(self.load_analysis(), clip)
        if still and words:  # a preview frame shows the first caption, with its first word highlighted
            first = words[: min(3, len(words))]
            words = [(0.0, 10.0, first[0][2])] + [(10.0 + n, 11.0 + n, w[2]) for n, w in enumerate(first[1:])]
        document = ass_document(words, duration=10.0 if still else clip.duration, size=layout.size,
                                caption_y=layout.caption_y, font=font)
        (self.folder / name).write_text(document, encoding="utf-8")
        return name

    def preview(self, clip: ClipCandidate, *, subtitles: bool = True) -> Path:
        """PNG of the Shorts layout on the clip's preview frame (same FFmpeg graph as the render)."""
        from utils.ffmpeg import ensure_ffmpeg
        from utils.subprocess_utils import run_no_window
        from .render import preview_command
        analysis = self.load_analysis()
        image = analysis.folder / clip.frame if clip.frame else None
        if image is None or not image.is_file():
            raise ClipError("no_preview_frame", f"{clip.id} has no preview frame")
        layout = self.vertical_layout(clip)
        self.folder.mkdir(parents=True, exist_ok=True)
        captions = self._captions(clip, layout, f".preview_{clip.id}.ass", still=True) if subtitles else None
        target = self.folder / f".preview_{clip.id}_9x16.png"
        cmd = preview_command(ensure_ffmpeg()[0], image, layout, target, captions=captions,
                              fonts=".fonts" if captions else None)
        done = run_no_window(cmd, capture_output=True, text=True, cwd=str(self.folder), timeout=60)
        if done.returncode != 0 or not target.is_file():
            raise ClipError("preview_failed", (done.stderr or "FFmpeg failed").strip()[-400:])
        return target

    def render(self, clips: Sequence[ClipCandidate], *, codec: str = "auto", loudnorm: bool = True,
               formats: Sequence[str] = ("horizontal",), subtitles: bool = True,
               on_clip: Callable[[ClipCandidate, float], None] | None = None) -> list[dict[str, Any]]:
        """formats: horizontal (16:9 1080p) and/or vertical (Shorts 9:16, captions unless subtitles=False)."""
        from utils.codec_detection import resolve_video_codec
        from utils.ffmpeg import ensure_ffmpeg

        unknown = [f for f in formats if f not in FORMATS]
        if unknown or not formats:
            raise ClipError("invalid_arguments", f"Unknown clip format: {', '.join(unknown) or 'none'}")
        analysis = self.load_analysis()
        ffmpeg, ffprobe = ensure_ffmpeg()
        resolved = resolve_video_codec(ffmpeg, codec).resolved
        from .render import render_clip
        results = []
        jobs = [(clip, fmt) for clip in clips for fmt in formats]
        for n, (clip, fmt) in enumerate(jobs):
            if self.cancellation is not None and self.cancellation.is_cancelled:
                raise ClipError("cancelled", "Clip rendering cancelled")
            vertical = fmt == "vertical"
            name = f"{clip.id}_{_safe_name(' '.join(clip.text.split()[:6]))}{'_9x16' if vertical else ''}.mp4"
            self.progress("render", f"{clip.id} {'9:16' if vertical else '16:9'}: {clip.duration:.1f} s, "
                                    f"{len(clip.parts)} part(s) -> {name}")
            fps = _common_fps(ffprobe, analysis.sources, clip) if len({p[0] for p in clip.parts}) > 1 else None
            layout = self.vertical_layout(clip) if vertical else None
            captions = (self._captions(clip, layout, f"{clip.id}_9x16.ass")
                        if layout is not None and subtitles else None)

            def report(value: float, clip: ClipCandidate = clip, n: int = n) -> None:
                if on_clip is not None:
                    on_clip(clip, (n + value) / len(jobs))
            result = render_clip(clip, analysis.sources, self.folder / name, ffmpeg=ffmpeg, codec=resolved,
                                 cancellation=self.cancellation, fps=fps, loudnorm=loudnorm, on_progress=report,
                                 layout=layout, captions=captions, fonts=".fonts" if captions else None)
            results.append({"clip_id": clip.id, "format": fmt,
                            "layout": layout.mode if layout is not None else "16:9",
                            "captions": bool(captions),
                            "edited_start": round(clip.edited_start, 3), "edited_end": round(clip.edited_end, 3),
                            **result})
        all_clips = [ClipCandidate.from_mapping(c) for c in (self.saved() or {}).get("clips") or []] or list(clips)
        self.save(all_clips, exports=results)
        return results


def _video_size(ffprobe: str, source: str) -> tuple[int, int]:
    from utils.subprocess_utils import run_no_window
    out = run_no_window([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                         "-of", "csv=p=0:s=x", str(source)], capture_output=True, text=True, timeout=30).stdout.strip()
    try:
        width, height = (int(v) for v in out.split("x")[:2])
        return width, height
    except ValueError as exc:
        raise ClipError("source_unreadable", f"Cannot read the video size of {source}") from exc


def _common_fps(ffprobe: str, sources: Sequence[str], clip: ClipCandidate) -> float | None:
    """Concat needs one frame rate when a clip spans two sources: use the first part's."""
    from utils.subprocess_utils import run_no_window
    try:
        out = run_no_window([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate",
                             "-of", "csv=p=0", str(sources[clip.parts[0][0]])], capture_output=True, text=True,
                            timeout=30).stdout.strip()
        num, _, den = out.partition("/")
        return float(num) / float(den or 1)
    except (OSError, ValueError, ZeroDivisionError):
        return None
