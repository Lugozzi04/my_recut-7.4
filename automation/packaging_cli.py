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
    pack.add_argument("--thumbnails", "--variants", dest="thumbnails", type=int, default=3,
                      help="Number of thumbnail concepts rendered, 3-6 (default 3).")
    pack.add_argument("--webcam-roi", default="auto",
                      help="Webcam region x,y,w,h normalized 0..1, 'auto' (creator profile or face detection) or 'none'.")
    pack.add_argument("--creator", help="Creator style profile name (see packaging-style).")
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


def _default_output(source: str) -> Path:
    from utils.runtime_paths import exports_root

    return exports_root() / "packaging" / f"{Path(source).stem}-{time.strftime('%Y%m%d-%H%M%S')}"


def execute(args: argparse.Namespace, progress: TextIO) -> dict[str, Any]:
    if args.command == "packaging":
        return _packaging(args, progress)
    if args.command == "packaging-style":
        return _style(args)
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
        mode=args.mode, agent_max_input=args.agent_max_input, agent_budget_usd=args.agent_budget_usd)

    def report(stage: str, message: str) -> None:
        print(f"[Packaging:{stage}] {message}", file=progress, flush=True)

    cancellation = AnalysisCancellation()
    from automation.cli import _ctrl_c
    with _ctrl_c(_CtrlC(cancellation)):
        result = PackagingPipeline(settings, progress=report, cancellation=cancellation).run()
    return {"packaging": result}


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
    return {"profile": store.import_examples(args.name, titles=titles, thumbnails=images)}


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
