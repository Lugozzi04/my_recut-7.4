"""`clips`: propose short clips from an edit and render the chosen ones (no provider call)."""
from __future__ import annotations

from typing import Any, TextIO


def add_parser(sub: Any, json_option: Any) -> None:
    parser = sub.add_parser("clips", help="Propose clips (20-60 s, on sentence boundaries) from an edit and render "
                                          "the chosen ones from the original sources.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--project", help="Edited ReCut project (.autocutter): clips keep its final cuts.")
    source.add_argument("--video", help="Already edited video (e.g. an exported MP4).")
    parser.add_argument("--output", help="Clips folder (default: <name>_clips next to the project/video).")
    parser.add_argument("--analysis", help="Use this packaging run folder as the analysis.")
    parser.add_argument("--count", type=int, default=10, help="Number of proposed clips (default 10).")
    parser.add_argument("--min", dest="min_s", type=float, default=20.0, help="Minimum clip length, s (default 20).")
    parser.add_argument("--max", dest="max_s", type=float, default=60.0, help="Maximum clip length, s (default 60).")
    parser.add_argument("--refresh", action="store_true", help="Recompute the proposals (keeps ticks of same moments).")
    parser.add_argument("--ai", choices=("codex", "claude", "none"), default="codex",
                        help="AI rerank of the best local windows (default codex): grades hook/flow/value, tightens "
                             "start/end, writes titles. Sends transcript excerpts to that CLI's provider; 'none' = "
                             "local ranking only.")
    parser.add_argument("--render", metavar="IDS", help="Render clips: comma separated ids (C01,C03) or 'all'.")
    parser.add_argument("--format", dest="clip_format", choices=("horizontal", "vertical", "both"),
                        default="horizontal", help="horizontal 16:9 1080p (default), vertical Shorts 9:16 "
                                                   "(webcam on top, board below, captions) or both.")
    parser.add_argument("--no-subtitles", action="store_true", help="Shorts without burned captions.")
    parser.add_argument("--preview", metavar="IDS", help="Write the Shorts layout preview PNG of these clips.")
    parser.add_argument("--codec", default="auto", help="Video encoder (default auto: hardware H.264, else libx264).")
    parser.add_argument("--no-loudnorm", action="store_true", help="Keep the original loudness.")
    parser.add_argument("--marks", metavar="FILE",
                        help="Evaluate instead of proposing: compare the local scoring profiles (and the saved "
                             "proposals) with the moments listed in FILE, one per line in edited-video time "
                             "(12:34 or 12:30-13:05, optional note). Nothing is sent and clips.json is unchanged.")
    json_option(parser)


def execute(args: Any, progress: TextIO) -> dict[str, Any]:
    from analysis.audio_service import AnalysisCancellation
    from analysis.clips.models import ClipError
    from analysis.clips.service import ClipSession
    from automation.cli import _ctrl_c
    from automation.runtime import PipelineCancellation

    cancellation = PipelineCancellation()
    token = AnalysisCancellation()
    cancellation.attach(token)
    with _ctrl_c(cancellation):
        session = ClipSession(project=args.project, video=args.video, output=args.output, analysis=args.analysis,
                              cancellation=token,
                              progress=lambda stage, message: print(f"[Clips:{stage}] {message}", file=progress,
                                                                    flush=True))
        if getattr(args, "marks", None):
            from analysis.clips.evaluate import load_marks, report
            marks = load_marks(args.marks)
            evaluation = session.evaluate(marks, count=args.count, min_s=args.min_s, max_s=args.max_s)
            return {"folder": str(session.folder), "analysis": str(session.load_analysis().folder),
                    "warnings": session.warnings, "marks": [m.label() for m in marks], "evaluation": evaluation,
                    "report": report(evaluation, marks)}
        clips = session.candidates(count=args.count, min_s=args.min_s, max_s=args.max_s, refresh=args.refresh,
                                   ai=None if args.ai == "none" else args.ai)
        result: dict[str, Any] = {"folder": str(session.folder), "analysis": str(session.load_analysis().folder),
                                  "ranking": session.ranking, "warnings": session.warnings,
                                  "clips": [c.to_mapping() for c in clips]}
        by_id = {c.id: c for c in clips}
        if args.preview:
            wanted_preview = [part.strip().upper() for part in args.preview.split(",") if part.strip()]
            missing = [i for i in wanted_preview if i not in by_id]
            if missing:
                raise ClipError("invalid_arguments", "Unknown clip ids: " + ", ".join(missing))
            result["previews"] = [str(session.preview(by_id[i], subtitles=not args.no_subtitles))
                                  for i in wanted_preview]
        if args.render:
            wanted = {c.id for c in clips} if args.render.strip().lower() == "all" else \
                {part.strip().upper() for part in args.render.split(",") if part.strip()}
            unknown = wanted - {c.id for c in clips}
            if unknown:
                raise ClipError("invalid_arguments", "Unknown clip ids: " + ", ".join(sorted(unknown)))
            for clip in clips:
                clip.selected = clip.id in wanted
            session.save(clips)
            chosen = [c for c in clips if c.selected]

            def report(clip: Any, value: float) -> None:
                print(f"[Clips:render] {clip.id} {int(value * 100)}%", file=progress, flush=True)
            formats = ("horizontal", "vertical") if args.clip_format == "both" else (args.clip_format,)
            result["rendered"] = session.render(chosen, codec=args.codec, loudnorm=not args.no_loudnorm,
                                                formats=formats, subtitles=not args.no_subtitles, on_clip=report)
        return result


def human(result: dict[str, Any], stream: TextIO) -> None:
    def hms(t: float) -> str:
        t = int(round(t))
        return f"{t // 3600}:{t // 60 % 60:02d}:{t % 60:02d}"

    print(f"Clips folder: {result['folder']}", file=stream)
    if "evaluation" in result:
        for warning in result.get("warnings") or []:
            print("Warning: " + warning, file=stream)
        print(result["report"], file=stream)
        return
    ranking = result.get("ranking") or {}
    print("Ranking: " + (f"AI ({ranking['ai']}) over local signals" if ranking.get("ai") else "local signals"),
          file=stream)
    for warning in result.get("warnings") or []:
        print("Warning: " + warning, file=stream)
    for clip in result["clips"]:
        flags = (" [selected]" if clip.get("selected") else "") + \
                (f" ({'; '.join(clip['notes'])})" if clip.get("notes") else "")
        game = f" · {clip['result']}" if clip.get("result") else ""
        print(f"{clip['id']}  {hms(clip['edited_start'])}-{hms(clip['edited_end'])}  {clip['duration']:5.1f} s"
              f"  score {clip['score']:+.2f}{game}{flags}", file=stream)
        if clip.get("title"):
            ai = clip.get("ai") or {}
            print(f"     {clip['title']}  [AI {ai.get('overall', '?')}/100 · hook {ai.get('hook', '?')} "
                  f"flow {ai.get('flow', '?')} value {ai.get('value', '?')} emotion {ai.get('emotion', '?')}]",
                  file=stream)
            if ai.get("reason"):
                print(f"     {ai['reason']}", file=stream)
        else:
            print(f"     {clip.get('reason', '')}", file=stream)
        print(f"     \"{clip['text'][:140]}\"", file=stream)
    for path in result.get("previews", []):
        print(f"Shorts preview: {path}", file=stream)
    for item in result.get("rendered", []):
        shape = "9:16 " + item.get("layout", "") + (" + captions" if item.get("captions") else "")             if item.get("format") == "vertical" else "16:9"
        print(f"Rendered {item['clip_id']} [{shape}]: {item['path']} ({item['duration_s']} s, {item['codec']})",
              file=stream)
    if not result.get("rendered"):
        print("Render with: --render C01,C03 (or --render all)", file=stream)
