"""AI rerank of the best local clip windows (one claude/codex call, cached like the packaging agent).

Like OpusClip's virality score, the model grades HOOK (first seconds grab attention), FLOW (setup -> payoff ->
reaction, ends on a satisfying beat), VALUE/EMOTION (funny, hype, rage, surprise, a lesson) and judges whether the
clip stands alone. It may move the start/end to other transcript lines offered around each window, rejects weak
windows and writes a short title. Only transcript excerpts and local signal summaries are sent - no media.
"""
from __future__ import annotations

from typing import Any, Callable, Sequence

from .candidates import LEAD_S, TAIL_S, Analysis, retime
from .models import ClipCandidate

PROMPT_VERSION = "clips_rank_v1"
CONTEXT_S = 10.0  # transcript offered before/after each window, so the model can extend it
SYSTEM_PROMPT = (
    "You are a senior short-form editor (YouTube Shorts / TikTok / Reels) for an Italian Hearthstone streamer. "
    "You pick the clips that will make viewers stop scrolling, watch to the end and share. Answer with JSON only.")

SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": ["clips"],
    "properties": {"clips": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["id", "keep", "hook", "flow", "value", "emotion", "overall", "start_line", "end_line",
                     "title", "reason"],
        "properties": {
            "id": {"type": "string"}, "keep": {"type": "boolean"},
            "hook": {"type": "integer"}, "flow": {"type": "integer"}, "value": {"type": "integer"},
            "emotion": {"type": "integer"}, "overall": {"type": "integer"},
            "start_line": {"type": "string"}, "end_line": {"type": "string"},
            "title": {"type": "string"}, "reason": {"type": "string"}}}}}}

RULES = """CRITERIA (score each 0-10, overall 0-100):
- HOOK: the first 2-3 seconds must grab attention (an outburst, a question, a surprising line, the action already
  happening). A slow setup or filler ("allora", "comunque", "quindi") as the opening is a weak hook: start later.
- FLOW: setup -> peak/payoff -> reaction, ending right after the reaction or punchline. Never cut the payoff.
- VALUE / EMOTION: genuinely funny, hype, rage, disbelief, a clutch/lethal/top-deck, an absurd RNG moment, a misplay,
  a sharp insight. Loudness alone is not value.
- STANDALONE: understandable without the rest of the video; no references to things not shown.
- REJECT (keep=false): reading chat, thanking subs/donations, greetings, calls to action, menu/queue small talk,
  plain narration of ordinary turns, anything whose point is missing from the window.
- LENGTH: {min_s:.0f}-{max_s:.0f} s; 20-45 s is ideal. Tighten long windows.
For every clip return start_line/end_line chosen from ITS OWN offered lines (ids like L0123): the first line must
start the hook, the last line must contain the end of the reaction. title: 3-8 words in Italian, a hook-style
title (no clickbait lies, no emoji). reason: one short sentence in Italian. Judge every listed clip."""


def _line_id(n: int) -> str:
    return f"L{n:04d}"


def offered_lines(analysis: Analysis, clip: ClipCandidate) -> list[int]:
    lo, hi = clip.edited_start - CONTEXT_S, clip.edited_end + CONTEXT_S
    return [n for n, line in enumerate(analysis.lines) if line.end > lo and line.start < hi]


def build_prompt(analysis: Analysis, clips: Sequence[ClipCandidate], *, min_s: float, max_s: float,
                 language: str = "it") -> str:
    out = [f"PROMPT_VERSION: {PROMPT_VERSION}", f"LANGUAGE OF TITLES/REASONS: {language}",
           RULES.replace("{min_s:.0f}", f"{min_s:.0f}").replace("{max_s:.0f}", f"{max_s:.0f}"), "", "CANDIDATES:"]
    for clip in clips:
        signals = clip.signals or {}
        game = f"game result {clip.result}" if clip.result else "no game data"
        out.append(f"\n=== {clip.id} | proposed {clip.duration:.0f} s | {game} | local signals: {clip.reason}; "
                   f"excitement {signals.get('arousal', 0):.2f}, reactions {signals.get('reaction', 0):.2f}")
        for n in offered_lines(analysis, clip):
            line = analysis.lines[n]
            mark = ">" if clip.edited_start - 0.5 <= line.start < clip.edited_end else " "
            out.append(f"{mark} {_line_id(n)} [{line.start - clip.edited_start:+6.1f}s] {line.text}")
    out.append("\nLines marked '>' are the proposed window; the others are context you may extend into.")
    return "\n".join(out)


def validate(raw: Any, analysis: Analysis, clips: Sequence[ClipCandidate], *, min_s: float,
             max_s: float) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict) or not isinstance(raw.get("clips"), list):
        raise ValueError("answer must be an object with a clips array")
    wanted = {c.id: c for c in clips}
    found: dict[str, dict[str, Any]] = {}
    problems = []
    for item in raw["clips"]:
        cid = str(item.get("id"))
        if cid not in wanted or cid in found:
            problems.append(f"{cid}: unknown or repeated id")
            continue
        allowed = {_line_id(n): n for n in offered_lines(analysis, wanted[cid])}
        a, b = allowed.get(str(item.get("start_line"))), allowed.get(str(item.get("end_line")))
        if a is None or b is None or b < a:
            problems.append(f"{cid}: start_line/end_line must be its own offered lines, start before end")
            continue
        start, end = analysis.lines[a].start - LEAD_S, analysis.lines[b].end + TAIL_S
        if item.get("keep") and not (0.6 * min_s <= end - start <= 1.15 * max_s):
            problems.append(f"{cid}: kept clip lasts {end - start:.0f} s, outside {min_s:.0f}-{max_s:.0f} s")
            continue
        found[cid] = {**item, "start": max(0.0, start), "end": min(analysis.duration, end)}
    missing = [cid for cid in wanted if cid not in found]
    if missing:
        problems.append("missing clips: " + ", ".join(missing))
    if problems:
        raise ValueError("; ".join(problems[:12]))
    return found


def rerank(analysis: Analysis, clips: Sequence[ClipCandidate], *, run_agent: Callable[..., Any], count: int,
           min_s: float, max_s: float, language: str = "it") -> list[ClipCandidate]:
    """run_agent(task, prompt=..., schema=..., system_prompt=..., prompt_version=..., validate=...) -> result with
    .answer (PackagingAgent.run). Returns the best `count` clips, AI-ordered, renumbered C01..."""
    if not clips:
        return []
    prompt = build_prompt(analysis, clips, min_s=min_s, max_s=max_s, language=language)
    result = run_agent("clips_rank", prompt=prompt, schema=SCHEMA, system_prompt=SYSTEM_PROMPT,
                       prompt_version=PROMPT_VERSION,
                       validate=lambda raw: (validate(raw, analysis, clips, min_s=min_s, max_s=max_s), raw)[1])
    judged = validate(result.answer, analysis, clips, min_s=min_s, max_s=max_s)
    best_local = max((c.score for c in clips), default=1.0) or 1.0
    ranked: list[tuple[float, ClipCandidate]] = []
    for clip in clips:
        item = judged[clip.id]
        grades = {k: max(0, min(10, int(item.get(k, 0)))) for k in ("hook", "flow", "value", "emotion")}
        overall = max(0, min(100, int(item.get("overall", 0))))
        retime(analysis, clip, item["start"], item["end"])
        clip.title = " ".join(str(item.get("title") or "").split())[:80]
        clip.ai = {**grades, "overall": overall, "keep": bool(item.get("keep")),
                   "reason": " ".join(str(item.get("reason") or "").split())[:240]}
        final = 0.7 * overall / 100.0 + 0.3 * max(0.0, clip.score) / best_local
        if not item.get("keep"):
            clip.notes.append("AI: weak clip")
        ranked.append((final if item.get("keep") else final - 1.0, clip))
    out: list[ClipCandidate] = []
    for final, clip in sorted(ranked, key=lambda pair: -pair[0]):
        if any(min(clip.edited_end, o.edited_end) - max(clip.edited_start, o.edited_start)
               > 0.3 * min(clip.duration, o.duration) for o in out):
            continue  # two windows tightened onto the same moment
        clip.score = round(final, 4)
        out.append(clip)
        if len(out) >= count:
            break
    for n, clip in enumerate(out, start=1):
        clip.id = f"C{n:02d}"
    return out

