"""`short`: a Twitch clip (link or downloaded file) -> a YouTube Short ready to publish (no provider call)."""
from __future__ import annotations

from typing import Any, TextIO


def add_parser(sub: Any, json_option: Any) -> None:
    parser = sub.add_parser("short", help="Turn a Twitch clip (link or file) into a 9:16 YouTube Short: climax in "
                                          "the first second, webcam on top, board zoomed on the play, big captions, "
                                          "20-40 s ending on the reaction.")
    parser.add_argument("source", help="Twitch clip link (clips.twitch.tv/… or twitch.tv/<channel>/clip/…) or a "
                                       "video file.")
    parser.add_argument("--output", help="Folder of the Short (default: <clip name>_clips next to the clip).")
    parser.add_argument("--max", dest="max_s", type=float, default=40.0, help="Maximum length, s (default 40).")
    parser.add_argument("--no-cold-open", action="store_true",
                        help="Do not show the peak first (the clip only starts later, from its setup).")
    parser.add_argument("--no-zoom", action="store_true", help="Board panel without zoom on the play.")
    parser.add_argument("--no-subtitles", action="store_true", help="No burned captions.")
    parser.add_argument("--codec", default="auto", help="Video encoder (default auto: hardware H.264, else libx264).")
    parser.add_argument("--no-loudnorm", action="store_true", help="Keep the original loudness.")
    json_option(parser)


def execute(args: Any, progress: TextIO) -> dict[str, Any]:
    from analysis.audio_service import AnalysisCancellation
    from analysis.clips.short import make_short
    from automation.cli import _ctrl_c
    from automation.runtime import PipelineCancellation

    cancellation = PipelineCancellation()
    token = AnalysisCancellation()
    cancellation.attach(token)
    with _ctrl_c(cancellation):
        return make_short(args.source, output=args.output, max_s=args.max_s, cold_open=not args.no_cold_open,
                          zoom=not args.no_zoom, subtitles=not args.no_subtitles, codec=args.codec,
                          loudnorm=not args.no_loudnorm, cancellation=token,
                          progress=lambda stage, message: print(f"[Short:{stage}] {message}", file=progress,
                                                                flush=True))


def human(result: dict[str, Any], stream: TextIO) -> None:
    plan = result["plan"]
    print(f"Short: {result['output']} ({result['duration_s']} s)", file=stream)
    teaser = plan.get("teaser")
    if teaser:
        print(f"Cold open: {teaser[0]:.1f}-{teaser[1]:.1f} s of the clip, then {plan['main'][0]:.1f}-"
              f"{plan['main'][1]:.1f} s", file=stream)
    else:
        print(f"Clip part: {plan['main'][0]:.1f}-{plan['main'][1]:.1f} s", file=stream)
    print(f"Peak at {plan['peak']:.1f} s: {plan['reason']}", file=stream)
    for note in plan.get("notes") or []:
        print("· " + note, file=stream)
    for warning in result.get("warnings") or []:
        print("Warning: " + warning, file=stream)
    if result.get("preview"):
        print(f"Preview: {result['preview']}", file=stream)
    if result["youtube"].get("title"):
        print(f"YouTube title: {result['youtube']['title']}", file=stream)
