"""CLI front-end for AI YouTube Packaging (`packaging`, `packaging-style`, `packaging-feedback`).

Imported lazily by automation.cli only when one of these commands is used.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any, TextIO

COMMANDS = ("packaging", "packaging-style", "packaging-feedback")


def add_parsers(sub: Any, json_option: Any) -> None:
    pack = sub.add_parser("packaging", help="Generate YouTube titles and rendered thumbnails for an edited video.",
                          description="Analyse the KEPT content of a ReCut project (or a final MP4) locally, ask the "
                                      "claude/codex CLI for a story and creative concepts, render real PNG thumbnails. "
                                      "Results are saved locally; nothing is uploaded to YouTube.")
    source = pack.add_mutually_exclusive_group()
    source.add_argument("--project", help="Edited ReCut project (.autocutter); only its final keeps are analysed.")
    source.add_argument("--video", help="Already edited video (e.g. the exported MP4).")
    pack.add_argument("--output", help="Output folder (default: <exports>/packaging/<name>-<time>). An existing run is "
                                       "never overwritten without --overwrite/--only.")
    pack.add_argument("--provider", choices=("codex", "claude"), default="codex",
                      help="Reasoning agent CLI. It receives transcript excerpts, evidence and selected frames.")
    pack.add_argument("--language", default="it", help="Language of titles and thumbnail text (default it).")
    pack.add_argument("--asr-language", help="Spoken language for faster-whisper (default: --language; auto = detect).")
    pack.add_argument("--asr-model", default="small", help="faster-whisper model (default small).")
    pack.add_argument("--mode", choices=("fast", "deep"), default="fast",
                      help="fast (default): ONE agent call from a compact brief; deep: story, deepening and creative "
                           "calls (2-3 calls).")
    pack.add_argument("--titles", type=int, default=6, help="Number of titles, 5-10 (default 6).")
    pack.add_argument("--thumbnails", "--variants", dest="thumbnails", type=int, default=4,
                      help="Number of thumbnail concepts rendered, 3-6 (default 4: one planned portfolio).")
    pack.add_argument("--thumbnail-engine", choices=("gen", "v3", "v2"), default="gen",
                      help="gen (default): the agent picks a role and an asset package per variant, then ONE codex "
                           "image_gen call composes the final thumbnails (local render kept as draft/fallback); "
                           "v3: local layered scene graphs only (no image model); v2: archetype layouts.")
    pack.add_argument("--generator-timeout", type=float, default=1500.0,
                      help="Seconds for the single image-generation call (default 1500).")
    pack.add_argument("--webcam-roi", default="auto",
                      help="Webcam region x,y,w,h normalized 0..1, 'auto' (creator profile or face detection) or 'none'.")
    pack.add_argument("--creator", "--style-profile", dest="creator",
                      help="Creator style profile name (see packaging-style).")
    pack.add_argument("--no-style", action="store_true", help="Ignore the creator style memory for this run.")
    pack.add_argument("--games", choices=("auto", "project", "detect", "none"), default="auto",
                      help="Game contexts: project gameplay data (auto), the cached Hearthstone detector (detect), or none.")
    pack.add_argument("--only", choices=("titles", "thumbnails", "render"),
                      help="Regenerate only titles, only thumbnails, or re-render edited thumbnail_concepts.json, "
                           "reusing the analysis in --output.")
    pack.add_argument("--analyze-only", action="store_true", help="Local analysis only: nothing is sent to any provider.")
    pack.add_argument("--no-cache", action="store_true", help="Ignore and do not write packaging/agent caches.")
    pack.add_argument("--overwrite", action="store_true", help="Reuse a folder with a previous run (it is archived to history/).")
    pack.add_argument("--max-highlights", type=int, default=14, help="Highlight candidates given to the agent (default 14).")
    pack.add_argument("--max-deepen", type=int, help="Intervals the agent may ask to inspect more closely "
                                                       "(0-4; default 1 in fast mode, 2 in deep mode).")
    pack.add_argument("--format", dest="image_format", choices=("png", "jpg"), default="png", help="Thumbnail file format.")
    pack.add_argument("--agent-model", help="Model passed to the claude/codex CLI.")
    pack.add_argument("--agent-effort", choices=("low", "medium", "high"),
                      help="claude --effort / codex model_reasoning_effort (codex default: medium).")
    pack.add_argument("--agent-max-input", type=int,
                      help="Prompt size budget in characters (default 60000 fast / 120000 deep); larger prompts are "
                           "refused, not sent.")
    pack.add_argument("--agent-budget-usd", type=float,
                      help="Claude only: --max-budget-usd per call. Codex has no cost cap; its tokens are logged.")
    pack.add_argument("--agent-timeout", type=float, default=900.0, help="Seconds per agent call (default 900).")
    pack.add_argument("--no-interactive", action="store_true",
                      help="Do not open the thumbnails folder nor ask for edits at the end (default: ask when run "
                           "from a terminal).")
    json_option(pack)

    style = sub.add_parser("packaging-style", help="Manage creator style profiles used by packaging.")
    json_option(style)
    actions = style.add_subparsers(dest="style_action", required=True)
    json_option(actions.add_parser("list"))
    show = actions.add_parser("show")
    json_option(show)
    show.add_argument("name")
    create = actions.add_parser("create")
    json_option(create)
    create.add_argument("name")
    create.add_argument("--channel", default="")
    create.add_argument("--language", default="")
    update = actions.add_parser("set")
    json_option(update)
    update.add_argument("name")
    update.add_argument("--channel")
    update.add_argument("--language")
    update.add_argument("--colors", help="Comma separated #RRGGBB brand colours.")
    update.add_argument("--fonts", help="Comma separated font family names or .ttf/.otf paths.")
    update.add_argument("--webcam-roi", help="Default webcam region x,y,w,h (normalized).")
    update.add_argument("--webcam-size", choices=("small", "medium", "large"), help="Preferred webcam size.")
    update.add_argument("--cards", choices=("prefer", "auto", "avoid"), help="Use of real card crops.")
    update.add_argument("--text", choices=("minimal", "short"), help="Amount of thumbnail text.")
    update.add_argument("--typography", choices=("impact", "heavy", "condensed"), help="Headline type style.")
    update.add_argument("--layouts", help="Preferred archetypes, comma separated (reaction_focus,board_focus,story_focus).")
    update.add_argument("--title-note", help="Free note about title preferences (stored as creator evidence).")
    update.add_argument("--thumbnail-note", help="Free note about thumbnail preferences.")
    toggle = update.add_mutually_exclusive_group()
    toggle.add_argument("--enable", action="store_true")
    toggle.add_argument("--disable", action="store_true")
    importer = actions.add_parser("import", help="Import local example titles (text file, one per line) and thumbnails.")
    json_option(importer)
    importer.add_argument("name")
    importer.add_argument("--titles", help="UTF-8 text file with one title per line.")
    importer.add_argument("--thumbnails", nargs="*", default=[], help="Image files or folders.")
    importer.add_argument("--categories", default="",
                          help="Comma separated story types / treatments the thumbnails illustrate "
                               "(e.g. unexpected_reaction,card_hero): used to pick visual references.")
    importer.add_argument("--negative", action="store_true", help="Mark the imported thumbnails as examples to avoid.")

    feedback = sub.add_parser("packaging-feedback", help="Select/approve/reject packaging results and update style memory.")
    feedback.add_argument("--output", required=True, help="Folder of a packaging run.")
    feedback.add_argument("--creator", help="Profile to update (default: the run's --creator).")
    feedback.add_argument("--select-title", help="Title id chosen for publishing (saved to selection.json).")
    feedback.add_argument("--select-thumbnail", help="Concept id chosen for publishing.")
    feedback.add_argument("--approve-title", nargs="*", default=[])
    feedback.add_argument("--reject-title", nargs="*", default=[])
    feedback.add_argument("--approve-thumbnail", nargs="*", default=[])
    feedback.add_argument("--reject-thumbnail", nargs="*", default=[])
    feedback.add_argument("--note", help="Free feedback text stored in the style memory.")
    json_option(feedback)
    add_edit_parser(sub, json_option)


def _default_output(source: str) -> Path:
    from utils.runtime_paths import exports_root

    return exports_root() / "packaging" / f"{Path(source).stem}-{time.strftime('%Y%m%d-%H%M%S')}"


def execute(args: argparse.Namespace, progress: TextIO) -> dict[str, Any]:
    if args.command == "packaging":
        return _packaging(args, progress)
    if args.command == "packaging-style":
        return _style(args)
    if args.command == "packaging-edit":
        return _edit(args, progress)
    return _feedback(args)


def _packaging(args: argparse.Namespace, progress: TextIO) -> dict[str, Any]:
    from analysis.audio_service import AnalysisCancellation
    from analysis.packaging.pipeline import PackagingPipeline, PackagingSettings

    if args.only is None and not (args.project or args.video):
        raise ValueError("Use --project or --video.")
    if args.only is not None and not args.output:
        raise ValueError("--only needs --output pointing at a previous packaging run.")
    if args.max_deepen is not None and not 0 <= args.max_deepen <= 4:
        raise ValueError("--max-deepen must be between 0 and 4.")
    output = Path(args.output) if args.output else _default_output(args.project or args.video)
    settings = PackagingSettings(
        output=str(output), project=args.project, video=args.video, provider=args.provider, language=args.language,
        asr_language=args.asr_language, asr_model=args.asr_model, titles=args.titles, thumbnails=args.thumbnails,
        webcam_roi=args.webcam_roi, creator=args.creator, use_style=not args.no_style, games=args.games,
        only=args.only, analyze_only=args.analyze_only, use_cache=not args.no_cache, overwrite=args.overwrite,
        max_highlights=args.max_highlights, max_deepen=args.max_deepen, image_format=args.image_format,
        agent_model=args.agent_model, agent_effort=args.agent_effort, agent_timeout=args.agent_timeout,
        mode=args.mode, agent_max_input=args.agent_max_input, agent_budget_usd=args.agent_budget_usd,
        thumbnail_engine=args.thumbnail_engine, generator_timeout=args.generator_timeout)

    def report(stage: str, message: str) -> None:
        print(f"[Packaging:{stage}] {message}", file=progress, flush=True)

    cancellation = AnalysisCancellation()
    from automation.cli import _ctrl_c
    with _ctrl_c(_CtrlC(cancellation)):
        result = PackagingPipeline(settings, progress=report, cancellation=cancellation).run()
    out: dict[str, Any] = {"packaging": result}
    if wants_interactive(args) and result.get("status") == "done" and result.get("thumbnails"):
        human("packaging", out, progress)
        out["thumbnail_session"] = interactive_session(result["output"], progress)
    return out


def wants_interactive(args: argparse.Namespace) -> bool:
    return not getattr(args, "no_interactive", False) and not getattr(args, "json", False) and _interactive_terminal()


def run_after_process(*, project: str | None, video: str | None, output: str | None, provider: str, language: str,
                      thumbnail_engine: str, progress: TextIO) -> dict[str, Any]:
    """Packaging right after `process`/`vod`: the job's project (final keeps + gameplay data) is the input;
    without a project, the exported video is analysed instead. Default output: next to the export."""
    from analysis.audio_service import AnalysisCancellation
    from analysis.packaging.pipeline import PackagingPipeline, PackagingSettings

    if not project and not video:
        raise ValueError("--packaging needs a finished export (no project or video to analyse).")
    if output:
        target = Path(output)
    elif video:
        target = Path(video).with_name(f"{Path(video).stem}_packaging")
    else:
        target = _default_output(str(project))
    settings = PackagingSettings(output=str(target), project=project if project else None,
                                 video=None if project else video, provider=provider, language=language,
                                 thumbnail_engine=thumbnail_engine, overwrite=True)

    def report(stage: str, message: str) -> None:
        print(f"[Packaging:{stage}] {message}", file=progress, flush=True)

    cancellation = AnalysisCancellation()
    from automation.cli import _ctrl_c
    with _ctrl_c(_CtrlC(cancellation)):
        return PackagingPipeline(settings, progress=report, cancellation=cancellation).run()


class ParallelPackaging:
    """process --packaging without idle waiting: local analysis first (CPU, before the export), then the
    provider calls (planning + image generation, pure waiting) in a background thread WHILE the video export
    renders, then the light local finish (Qt renders, report) on the main thread. CPU-heavy stages never overlap."""

    def __init__(self, *, project: str | None, video: str | None, output: str | None, provider: str, language: str,
                 thumbnail_engine: str, progress: TextIO) -> None:
        from analysis.audio_service import AnalysisCancellation
        from analysis.packaging.pipeline import PackagingPipeline, PackagingSettings

        if not project and not video:
            raise ValueError("--packaging needs the job project or a video to analyse.")
        if output:
            target = Path(output)
        elif video:
            target = Path(video).with_name(f"{Path(video).stem}_packaging")
        else:
            target = _default_output(str(project))
        settings = PackagingSettings(output=str(target), project=project if project else None,
                                     video=None if project else video, provider=provider, language=language,
                                     thumbnail_engine=thumbnail_engine, overwrite=True)
        self.progress = progress
        self.cancellation = AnalysisCancellation()
        self.pipeline = PackagingPipeline(settings, progress=self._report, cancellation=self.cancellation)
        self._thread: Any = None
        self._early: dict[str, Any] | None = None

    def _report(self, stage: str, message: str) -> None:
        print(f"[Packaging:{stage}] {message}", file=self.progress, flush=True)

    def prepare(self) -> None:
        self._early = self.pipeline.prepare()

    def start(self) -> None:
        """Provider calls in the background (no Qt, no heavy CPU)."""
        import threading

        if self._early is not None:
            return
        self._report("parallel", "agent planning and image generation run while the video is exported")
        self._thread = threading.Thread(target=self.pipeline.plan, name="packaging-plan", daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self.cancellation.cancel()

    def finish(self, check_cancelled: Any = None) -> dict[str, Any]:
        """Wait for the background provider calls (check_cancelled raises the pipeline's own cancellation)."""
        if self._early is not None:
            return self._early
        if self._thread is not None:
            if self._thread.is_alive():
                self._report("parallel", "export done; waiting for the provider")
            while self._thread.is_alive():
                self._thread.join(timeout=0.5)
                if check_cancelled is not None:
                    try:
                        check_cancelled()
                    except BaseException:
                        self.cancel()
                        raise
        return self.pipeline.finish()


class _CtrlC:
    """Adapts AnalysisCancellation to the cancel()/cancelled contract of automation.cli._ctrl_c."""

    def __init__(self, token: Any) -> None:
        self.token = token

    def cancel(self) -> None:
        self.token.cancel()

    @property
    def cancelled(self) -> bool:
        return bool(self.token.is_cancelled)


def _style(args: argparse.Namespace) -> dict[str, Any]:
    from analysis.packaging.style import CreatorStyleStore
    from analysis.packaging.webcam import parse_roi

    store = CreatorStyleStore()
    action = args.style_action
    if action == "list":
        return {"profiles": store.list()}
    if action == "show":
        return {"profile": store.load(args.name)}
    if action == "create":
        return {"profile": store.create(args.name, channel=args.channel, language=args.language)}
    if action == "set":
        roi = None
        if args.webcam_roi:
            check = parse_roi(args.webcam_roi, origin="profile")
            if check.rect is None:
                raise ValueError(check.message)
            roi = check.rect.to_list()
        colors = [c.strip() for c in args.colors.split(",")] if args.colors else None
        fonts = [f.strip() for f in args.fonts.split(",")] if args.fonts else None
        enabled = True if args.enable else (False if args.disable else None)
        layouts = [x.strip() for x in args.layouts.split(",")] if args.layouts else None
        from analysis.packaging.render import LAYOUTS
        if layouts and any(x not in LAYOUTS for x in layouts):
            raise ValueError(f"--layouts must be among {', '.join(LAYOUTS)}")
        return {"profile": store.update(args.name, channel_name=args.channel, language=args.language, colors=colors,
                                        fonts=fonts, webcam_roi=roi, title_note=args.title_note,
                                        thumbnail_note=args.thumbnail_note, enabled=enabled,
                                        webcam_size=args.webcam_size, cards=args.cards, text=args.text,
                                        typography=args.typography, layouts=layouts)}
    titles: list[str] = []
    if args.titles:
        titles = [line for line in Path(args.titles).read_text(encoding="utf-8").splitlines() if line.strip()]
    images: list[Path] = []
    for item in args.thumbnails:
        path = Path(item)
        if path.is_dir():
            images += sorted(p for p in path.iterdir() if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"))
        elif path.is_file():
            images.append(path)
    categories = [c.strip() for c in args.categories.split(",") if c.strip()]
    return {"profile": store.import_examples(args.name, titles=titles, thumbnails=images, categories=categories,
                                             negative=args.negative)}


def _feedback(args: argparse.Namespace) -> dict[str, Any]:
    from analysis.gameplay.cache import atomic_write_json
    from analysis.packaging.style import CreatorStyleStore

    out = Path(args.output).expanduser().resolve()
    try:
        titles = json.loads((out / "title_candidates.json").read_text(encoding="utf-8"))["titles"]
        concepts = json.loads((out / "thumbnail_concepts.json").read_text(encoding="utf-8"))["concepts"]
        report = json.loads((out / "packaging_report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError) as exc:
        raise ValueError(f"{out} is not a completed packaging run: {exc}") from exc
    by_title = {t["title_id"]: t for t in titles}
    by_concept = {c["concept_id"]: c for c in concepts}
    for ident in [args.select_title] + args.approve_title + args.reject_title:
        if ident and ident not in by_title:
            raise ValueError(f"unknown title id {ident}")
    for ident in [args.select_thumbnail] + args.approve_thumbnail + args.reject_thumbnail:
        if ident and ident not in by_concept:
            raise ValueError(f"unknown concept id {ident}")
    run = report.get("created")
    entries: list[dict[str, Any]] = []
    for action, kind, ids in (("approve", "title", args.approve_title), ("reject", "title", args.reject_title),
                              ("approve", "thumbnail", args.approve_thumbnail),
                              ("reject", "thumbnail", args.reject_thumbnail)):
        for ident in ids:
            item = by_title[ident] if kind == "title" else by_concept[ident]
            value = item["title"] if kind == "title" else f"{item['thumbnail_text']} ({item['composition_plan']['layout']})"
            entries.append({"action": action, "kind": kind, "id": ident, "value": value, "run": run})
    if args.select_title:
        entries.append({"action": "approve", "kind": "title", "id": args.select_title,
                        "value": by_title[args.select_title]["title"], "run": run, "note": "selected for publishing"})
    if args.note:
        entries.append({"action": "note", "kind": "general", "note": args.note, "run": run})
    selection = None
    if args.select_title or args.select_thumbnail:
        target = out / "selection.json"
        if target.exists():  # keep the previous choice
            history = out / "history" / time.strftime("%Y%m%d-%H%M%S")
            history.mkdir(parents=True, exist_ok=True)
            (history / "selection.json").write_bytes(target.read_bytes())
        selection = {"format": "recut_packaging_selection", "selected_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                     "title": by_title.get(args.select_title) if args.select_title else None,
                     "thumbnail": by_concept.get(args.select_thumbnail) if args.select_thumbnail else None}
        atomic_write_json(target, selection)
    creator = args.creator or (report.get("settings") or {}).get("creator")
    profile_revision = None
    if creator and entries:
        profile_revision = CreatorStyleStore().record_feedback(creator, entries)["revision"]
    elif entries and not creator:
        entries_note = "no creator profile: feedback not stored in style memory (use --creator)"
        return {"selection": selection, "feedback": entries, "warning": entries_note}
    return {"selection": selection, "feedback": entries, "creator": creator, "profile_revision": profile_revision}


def human(command: str, result: dict[str, Any], stream: TextIO) -> None:
    session = result.get("thumbnail_session")
    if command == "packaging" and session:  # the summary was printed before the interactive session
        final = next((a["approved"]["final"] for a in session.get("actions", [])[::-1] if "approved" in a), None)
        print(f"Thumbnail confermata:\n{final}" if final else "Sessione thumbnail chiusa.", file=stream)
        return
    if command == "packaging-edit":
        for key, label in (("edited", "Nuova versione"), ("reverted", "Ripristinata"), ("approved", "Confermata")):
            if key in result:
                item = result[key]
                print(f"{label}: {item['concept_id']} v{item.get('version', 0)} -> "
                      f"{item.get('current') or item.get('final')}", file=stream)
        for item in result.get("thumbnails", []):
            flags = (f" v{item['current_version']}" if item["current_version"] else "") + \
                    (" [CONFERMATA]" if item["approved"] else "")
            print(f"  {item['number']}. {item['hook'] or '(senza testo)'} - {item['path']}{flags}", file=stream)
        return
    if command == "packaging":
        report = result["packaging"]
        print(f"PACKAGING {report['status'].upper()}\nOutput:\n{report['output']}", file=stream)
        if report.get("story"):
            print(f"Topic: {report['story'].get('main_topic')}", file=stream)
        for title in report.get("titles", []):
            mark = "*" if report.get("preferred_title") and title["title_id"] == report["preferred_title"]["title_id"] else " "
            print(f" {mark} {title['title_id']} [{title['concept_id']}] {title['title']}", file=stream)
        for thumb in report.get("thumbnails", []):
            print(f"   {thumb['concept_id']}: {thumb.get('path') or thumb.get('error')}", file=stream)
        for warning in report.get("warnings", []):
            print(f"Warning: {warning}", file=stream)
        timings = report.get("timings_s", {})
        print("Timings (s): " + ", ".join(f"{k}={v}" for k, v in timings.items()), file=stream)
        agent = report.get("agent", {})
        if agent.get("cost_usd_total") is not None:
            print(f"Agent cost reported by the CLI: ${agent['cost_usd_total']}", file=stream)
        return
    print(json.dumps(result, ensure_ascii=False, indent=2), file=stream)


# ---- thumbnail editing (packaging-edit and the interactive loop after a run) ---------------------------------
def add_edit_parser(sub: Any, json_option: Any) -> None:
    edit = sub.add_parser("packaging-edit", help="Edit, revert or approve the thumbnails of a packaging run.",
                          description="Without an action, opens an interactive session on the run's thumbnails.")
    edit.add_argument("--output", required=True, help="Folder of a packaging run.")
    edit.add_argument("--concept", help="Thumbnail to edit/revert: number (1-4) or concept id (C2).")
    edit.add_argument("--request", help="What to change, sent to the image model (e.g. \"testo: BOARD, font squadrato\").")
    edit.add_argument("--revert", type=int, metavar="VERSION", help="Make an earlier version of --concept current.")
    edit.add_argument("--approve", metavar="CONCEPT", help="Confirm a thumbnail: thumbnails/thumbnail_final.png.")
    edit.add_argument("--list", action="store_true", help="List thumbnails, versions and edits.")
    edit.add_argument("--creator", help="Style profile that stores the approval (default: the run's --creator).")
    edit.add_argument("--no-open", action="store_true", help="Interactive session without opening the folder.")
    json_option(edit)


def _edit(args: argparse.Namespace, progress: TextIO) -> dict[str, Any]:
    from analysis.packaging.edit import ThumbnailEditor

    editor = ThumbnailEditor(args.output, progress=lambda m: print(f"[Thumbnails] {m}", file=progress, flush=True))
    if args.approve:
        return {"approved": editor.approve(args.approve, creator=args.creator), "thumbnails": editor.concepts()}
    if args.revert is not None:
        if not args.concept:
            raise ValueError("--revert needs --concept")
        return {"reverted": editor.revert(args.concept, args.revert), "thumbnails": editor.concepts()}
    if args.request:
        if not args.concept:
            raise ValueError("--request needs --concept")
        return {"edited": editor.edit(args.concept, args.request), "thumbnails": editor.concepts()}
    if args.list or getattr(args, "json", False) or not _interactive_terminal():
        return {"thumbnails": editor.concepts()}
    return interactive_session(args.output, progress, open_folder=not args.no_open, creator=args.creator)


def _interactive_terminal() -> bool:
    import sys

    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except (AttributeError, ValueError):
        return False


def interactive_session(output: str | Path, progress: TextIO, *, open_folder: bool = True, creator: str | None = None,
                        read_line: Any = None) -> dict[str, Any]:
    """After a run: open the thumbnails folder and loop until Enter.

      Enter                      exit
      N                          then type the request for thumbnail N (empty = cancel, ok = approve)
      N <request>                edit thumbnail N in one line
      ok N                       approve thumbnail N (thumbnail_final.png)
      back N V                   make version V of thumbnail N current again
    """
    import sys

    from analysis.packaging.edit import EditError, ThumbnailEditor, open_in_file_manager

    def say(text: str = "") -> None:
        print(text, file=progress, flush=True)

    def ask(prompt: str) -> str:
        print(prompt, end="", file=progress, flush=True)
        line = (read_line or sys.stdin.readline)()
        if line == "":  # EOF: behave like Enter
            return ""
        return str(line).strip()

    editor = ThumbnailEditor(output, progress=lambda m: say(f"[Thumbnails] {m}"))
    if open_folder:
        open_in_file_manager(editor.thumbs)
    actions: list[dict[str, Any]] = []

    def show() -> None:
        say("")
        say("Thumbnails (" + str(editor.thumbs) + "):")
        for item in editor.concepts():
            flags = (f" v{item['current_version']}" if item["current_version"] else "") + \
                    ("  [CONFERMATA]" if item["approved"] else "")
            say(f"  {item['number']}. {item['hook'] or '(senza testo)'}  -  {item['title']}{flags}")
        say("Invio = esci | <numero> = modifica | <numero> <richiesta> | ok <numero> = conferma | "
            "back <numero> <versione> = ripristina")

    show()
    while True:
        line = ask("> ")
        if not line:
            break
        parts = line.split(maxsplit=1)
        head = parts[0].lower()
        try:
            if head in ("ok", "conferma", "approve") and len(parts) == 2:
                result = editor.approve(parts[1].strip(), creator=creator)
                actions.append({"approved": result})
                say(f"Confermata {result['concept_id']} -> {result['final']}")
                continue
            if head in ("back", "ripristina") and len(parts) == 2:
                ref, _, version = parts[1].partition(" ")
                result = editor.revert(ref, int(version))
                actions.append({"reverted": result})
                say(f"{result['concept_id']} riportata alla versione {result['version']}")
                show()
                continue
            item = editor.resolve(head)
            request = parts[1].strip() if len(parts) == 2 else ask(
                f"Copertina {item['number']} ({item['hook'] or 'senza testo'}): cosa vuoi cambiare? "
                "(vuoto = annulla, ok = conferma) ")
            if not request:
                continue
            if request.lower() in ("ok", "conferma"):
                result = editor.approve(item["number"], creator=creator)
                actions.append({"approved": result})
                say(f"Confermata {result['concept_id']} -> {result['final']}")
                continue
            say("Invio la richiesta al modello (circa 1-3 minuti)...")
            result = editor.edit(item["number"], request)
            actions.append({"edited": result})
            say(f"Nuova versione v{result['version']} di {result['concept_id']} ({result['runtime_s']} s): {result['current']}")
            if open_folder:
                open_in_file_manager(result["current"])
            show()
        except (EditError, ValueError) as exc:
            say(f"Errore: {exc}")
    return {"thumbnails": editor.concepts(), "actions": actions}
