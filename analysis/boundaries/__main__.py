"""Debug CLI: inspect boundary windows, evidence, candidates and resolver decisions. Never exports or cuts.

python -m analysis.boundaries VIDEO --gameplay-analysis gameplay_analysis.json [--game N | --list]
python -m analysis.boundaries --contexts removable_contexts.json --resolver exact --resolver claude --resolver codex
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
from typing import Any, TextIO

from analysis.gameplay.models import GameplayAnalysisResult


def _hms(value: float | None) -> str:
    if value is None:
        return "-"
    minutes, seconds = divmod(float(value), 60.0)
    hours, minutes = divmod(int(minutes), 60)
    return f"{hours:d}:{minutes:02d}:{seconds:05.2f}"


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="python -m analysis.boundaries",
                                      description="Inspect editorial boundary candidates around gameplay anchors")
    command.add_argument("video", type=Path, nargs="?")
    command.add_argument("--gameplay-analysis", type=Path,
                         help="Saved gameplay_analysis.json (the detector is not re-run)")
    command.add_argument("--contexts", type=Path,
                         help="Reuse saved BoundaryContext JSON (--output of a previous run); no video, ASR or FFmpeg")
    command.add_argument("--select", action="append", metavar="GAME:TYPE",
                         help="With --contexts: only these contexts, e.g. 1:END (repeatable)")
    command.add_argument("--resolver", action="append", choices=("exact", "claude", "codex"),
                         help="Resolver(s) to run and compare (repeatable; default exact). "
                              "claude/codex send the context text to the provider of the local CLI")
    command.add_argument("--agent-timeout", type=float, default=120.0)
    command.add_argument("--agent-model", default=None)
    command.add_argument("--agent-effort", default=None, help="claude --effort / codex model_reasoning_effort")
    command.add_argument("--agent-fallback", choices=("exact", "none"), default="exact")
    command.add_argument("--no-agent-cache", action="store_true")
    command.add_argument("--agent-log", type=Path, help="Local JSONL decision log (no transcript)")
    command.add_argument("--show-prompt", action="store_true", help="Print the agent prompt of each context")
    command.add_argument("--decisions-output", type=Path, help="Write resolver decisions JSON here")
    command.add_argument("--game", type=int, action="append", help="Game index (repeatable); default: all removable")
    command.add_argument("--list", action="store_true", help="Only list games with termination/disposition")
    command.add_argument("--include-kept", action="store_true", help="Also analyse KEEP games (dataset collection)")
    command.add_argument("--no-transcript", action="store_true")
    command.add_argument("--no-visual", action="store_true")
    command.add_argument("--whisper-model", default=None)
    command.add_argument("--whisper-python", default=None, help="Interpreter with faster-whisper")
    command.add_argument("--language", default=None, help="ASR language code; default auto-detect")
    command.add_argument("--speech-engine", choices=("rms", "silero"), default="rms")
    command.add_argument("--start-before", type=float)
    command.add_argument("--start-after", type=float)
    command.add_argument("--end-before", type=float)
    command.add_argument("--end-after", type=float)
    command.add_argument("--output", type=Path, help="Write full contexts JSON here")
    command.add_argument("--agent-payload", action="store_true", help="Also print the compact agent payloads")
    command.add_argument("--json", action="store_true", help="Only JSON on stdout")
    return command


def _settings(args: argparse.Namespace) -> Any:
    from .analyzer import BoundarySettings

    settings = BoundarySettings()
    windows = replace(settings.windows, **{k: v for k, v in {
        "start_before_s": args.start_before, "start_after_s": args.start_after,
        "end_before_s": args.end_before, "end_after_s": args.end_after}.items() if v is not None})
    transcript = replace(settings.transcript, enabled=not args.no_transcript, language=args.language,
                         python=args.whisper_python, **({"model": args.whisper_model} if args.whisper_model else {}))
    return replace(settings, windows=windows, transcript=transcript,
                   visual=replace(settings.visual, enabled=not args.no_visual),
                   silence=replace(settings.silence, speech_engine=args.speech_engine))


def print_context(context: Any, stream: TextIO) -> None:
    from .resolvers import ExactBoundaryResolver

    c = context
    print(f"\n=== {c.boundary_type.value}  window {_hms(c.window.start)} -> {_hms(c.window.end)} "
          f"({c.window.duration:.1f}s){' clipped by ' + ','.join(c.window_clipped_by) if c.window_clipped_by else ''}",
          file=stream)
    print(f"transcript: {c.transcript_status.status} {c.transcript_status.engine} {c.transcript_status.detail}", file=stream)
    for s in c.transcript:
        print(f"  [{_hms(s.start)} - {_hms(s.end)}] {s.text}", file=stream)
    print(f"audio: {c.audio_status.status} {c.audio_status.engine} {c.audio_status.detail}", file=stream)
    print("  silences: " + ", ".join(f"{_hms(r.start)}-{_hms(r.end)} ({r.duration:.2f}s)" for r in c.silences), file=stream)
    peak = max(c.visual, key=lambda v: v.change, default=None)
    print(f"visual: {c.visual_status.status} {len(c.visual)} samples"
          + (f", max change {peak.change:.3f} at {_hms(peak.timestamp)}" if peak else ""), file=stream)
    print("gameplay markers: " + ", ".join(f"{m.kind}@{_hms(m.timestamp)}" for m in c.gameplay_markers), file=stream)
    decision = ExactBoundaryResolver().resolve(c)
    print(f"candidates ({len(c.candidates)}); exact baseline -> #{decision.candidate_index} {_hms(decision.timestamp)}",
          file=stream)
    keys = ("offset_from_anchor_s", "silence_before_s", "silence_after_s", "speech_density_before",
            "speech_density_after", "transcript_gap_before_s", "transcript_gap_after_s", "is_sentence_boundary",
            "visual_change_score")
    short = {"offset_from_anchor_s": "off", "silence_before_s": "silB", "silence_after_s": "silA",
             "speech_density_before": "spB", "speech_density_after": "spA", "transcript_gap_before_s": "txB",
             "transcript_gap_after_s": "txA", "is_sentence_boundary": "sent", "visual_change_score": "vis"}
    for i, cand in enumerate(c.candidates):
        values = " ".join(f"{short[k]}={'-' if cand.features.get(k) is None else format(cand.features[k], '.2f')}"
                          for k in keys)
        print(f"  #{i:02d} {_hms(cand.timestamp)} {'+'.join(s.value for s in cand.sources):<45} {values}", file=stream)


PRIVACY_NOTICE = ("NOTE: --resolver {modes} sends the boundary context text (window transcript, timings, candidate "
                  "features) to the provider configured in the local CLI. 'exact' sends nothing.")


def load_contexts(path: Path) -> list[Any]:
    """Accept a debug payload, a game analysis, a single context or a list of contexts."""
    from .models import BoundaryContext

    raw = json.loads(path.read_text(encoding="utf-8"))
    items: list[Any] = []
    if isinstance(raw, dict) and raw.get("format") == "recut_boundary_debug":
        items = [ctx for game in raw.get("games", []) for ctx in game.get("contexts", {}).values()]
    elif isinstance(raw, dict) and raw.get("format") == "recut_game_boundary_analysis":
        items = list(raw.get("contexts", {}).values())
    elif isinstance(raw, dict):
        items = [raw]
    elif isinstance(raw, list):
        items = raw
    return [BoundaryContext.from_mapping(item) for item in items]


def resolver_options(args: argparse.Namespace) -> Any:
    from .resolvers import ResolverOptions

    return ResolverOptions(timeout_s=args.agent_timeout, fallback=None if args.agent_fallback == "none" else "exact",
                           model=args.agent_model, effort=args.agent_effort, use_cache=not args.no_agent_cache,
                           log_path=args.agent_log)


def compare_resolvers(contexts: list[Any], modes: list[str], options: Any, human: TextIO,
                      show_prompt: bool = False) -> list[dict[str, Any]]:
    from .agents import build_prompt
    from .resolvers import REMOTE_MODES, create_resolver

    remote = [m for m in modes if m in REMOTE_MODES]
    if remote:
        print(PRIVACY_NOTICE.format(modes="/".join(remote)), file=human)
    resolvers = [create_resolver(mode, options) for mode in modes]
    rows = []
    for context in contexts:
        label = f"game {context.game.index} {context.boundary_type.value}"
        print(f"\n=== {label}  window {_hms(context.window.start)} -> {_hms(context.window.end)}  "
              f"{len(context.candidates)} candidates", file=human)
        if show_prompt:
            print(build_prompt(context), file=human)
        for resolver in resolvers:
            decision = resolver.resolve(context)
            meta = decision.metadata
            candidate = context.candidates[decision.candidate_index] if decision.candidate_index is not None else None
            sources = "+".join(s.value for s in candidate.sources) if candidate else "-"
            note = f" FALLBACK({meta.get('error_code')})" if decision.fallback else ""
            note += " cache" if meta.get("cache_hit") else ""
            print(f"  {resolver.name:<7} #{decision.candidate_index} {_hms(decision.timestamp)} "
                  f"conf={decision.confidence:.2f} {meta.get('runtime_s', 0.0) or 0.0:.1f}s {sources}{note}", file=human)
            if decision.detail and decision.resolver != "exact":
                print(f"          reason: {decision.detail}", file=human)
            rows.append({"game_index": context.game.index, "boundary_type": context.boundary_type.value,
                         "candidate_sources": sources.split("+") if candidate else [], **decision.to_mapping()})
    return rows


def main(argv: list[str] | None = None, stream: TextIO | None = None) -> int:
    from .analyzer import BoundaryAnalyzer
    from .gameplay import GameplayTimeline
    from .resolvers import agent_payload

    out = stream or sys.stdout
    args = parser().parse_args(argv)
    human = sys.stderr if args.json else out
    if args.contexts is not None:
        contexts = load_contexts(args.contexts)
        if args.select:
            wanted = {tuple(item.upper().split(":")) for item in args.select}
            contexts = [c for c in contexts if (str(c.game.index), c.boundary_type.value) in wanted]
        rows = compare_resolvers(contexts, args.resolver or ["exact"], resolver_options(args), human, args.show_prompt)
        payload = {"format": "recut_boundary_decisions", "version": 1, "source": str(args.contexts), "decisions": rows}
        if args.decisions_output:
            args.decisions_output.parent.mkdir(parents=True, exist_ok=True)
            args.decisions_output.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"\ndecisions written to {args.decisions_output}", file=human)
        if args.json:
            print(json.dumps(payload, ensure_ascii=False), file=out)
        return 0
    if args.video is None or args.gameplay_analysis is None:
        parser().error("VIDEO and --gameplay-analysis are required unless --contexts is given")
    analysis = GameplayAnalysisResult.from_mapping(json.loads(args.gameplay_analysis.read_text(encoding="utf-8")))
    settings = _settings(args)
    timeline = GameplayTimeline(analysis, settings.eof)
    infos = [timeline.info(game) for game in timeline.games]
    for info in infos:
        print(f"game {info.index}: {_hms(info.start)} -> {_hms(info.end)} {info.result:<7} "
              f"{info.termination.value:<18} {info.disposition.value:<13} {info.reason}", file=human)
    if args.list:
        return 0
    indices = args.game or [i.index for i in infos if args.include_kept or i.disposition.value != "KEEP"]
    analyzer = BoundaryAnalyzer(settings)
    results = []
    for index in indices:
        result = analyzer.analyze_game(args.video, analysis, index, require_removable=not args.include_kept)
        results.append(result)
        print(f"\n##### game {index}: {result.game.disposition.value} ({result.game.termination.value}) "
              f"profiling {json.dumps({k: round(v, 2) for k, v in result.profiling.items()})}", file=human)
        for context in result.contexts.values():
            print_context(context, human)
            if args.agent_payload:
                print(json.dumps(agent_payload(context), ensure_ascii=False, indent=1), file=human)
    payload = {"format": "recut_boundary_debug", "version": 1, "games": [r.to_mapping() for r in results]}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\ncontexts written to {args.output}", file=human)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False), file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
