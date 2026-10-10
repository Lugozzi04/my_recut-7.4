"""Phase A - Video Understanding: evidence package -> validated VideoStory.

The agent reads the full kept-content transcript (ids + edited timestamps), the
game contexts, the highlight candidates and labelled contact sheets, and returns
the main topic, themes, per-game interpretation, strongest highlights, narrative
angles, card readings and claims it could NOT verify. It may ask ReCut to deepen
a few bounded intervals (validated protocol; ReCut extracts, the agent never
explores files). Every reference is checked against existing ids.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from .models import GameContext, HighlightCandidate, TranscriptLine

PROMPT_VERSION = "packaging_story_v1"

SYSTEM_PROMPT = (
    "You are a senior YouTube editor for gaming creators. You analyse an ALREADY EDITED video (only kept content) "
    "from evidence prepared by a local tool: a timestamped speech transcript, game contexts, highlight candidates "
    "found by local signals (energy, reactions, speech density, game results, visual changes) and labelled frames. "
    "Your job is to understand what the video is really about and which moments are strongest, citing evidence ids. "
    "Never invent events, results, statistics, card names, combos or quotes. When something cannot be verified from "
    "the evidence, list it under risky_claims. Facial expressions are not proof of emotions. Do not use tools, "
    "files or commands. Reply only with the requested JSON object."
)

_NULLABLE_STR = {"type": ["string", "null"]}
_IDS = {"type": "array", "items": {"type": "string"}}
_BOX = {"type": "array", "items": {"type": "number"}, "description": "normalized [x, y, w, h] in 0..1 of the frame"}


def _obj(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


RESPONSE_SCHEMA: dict[str, Any] = _obj({
    "language": {"type": "string"},
    "main_topic": {"type": "string"},
    "summary": {"type": "string"},
    "themes": {"type": "array", "items": {"type": "string"}},
    "why_interesting": {"type": "string"},
    "games": {"type": "array", "items": _obj({
        "game_id": {"type": "string"},
        "summary": {"type": "string"},
        "specific_plays": {"type": "array", "items": _obj({"description": {"type": "string"}, "transcript_ids": _IDS})},
        "general_talk": {"type": "array", "items": _obj({"description": {"type": "string"}, "transcript_ids": _IDS})},
        "conclusion": {"type": "string"},
    })},
    "strongest_highlights": {"type": "array", "items": _obj({
        "highlight_id": {"type": "string"},
        "title": {"type": "string"},
        "why": {"type": "string"},
        "transcript_ids": _IDS,
        "frame_ids": _IDS,
        "strength": {"type": "number", "description": "0..1, your editorial judgement"},
    })},
    "narrative_angles": {"type": "array", "items": _obj({
        "angle_id": {"type": "string"},
        "angle": {"type": "string"},
        "highlight_ids": _IDS,
    })},
    "card_observations": {"type": "array", "items": _obj({
        "frame_id": {"type": "string"},
        "box": _BOX,
        "name": _NULLABLE_STR,
        "legibility": {"type": "string", "enum": ["clear", "partial", "unreadable"]},
        "confidence": {"type": "number"},
        "transcript_ids": _IDS,
    })},
    "risky_claims": {"type": "array", "items": _obj({"claim": {"type": "string"}, "why": {"type": "string"}})},
    "deepen_requests": {"type": "array", "items": _obj({
        "edited_start": {"type": "number"},
        "edited_end": {"type": "number"},
        "want": {"type": "string", "enum": ["frames", "webcam"]},
        "reason": {"type": "string"},
    })},
})


def _clock(t: float | None) -> str:
    if t is None:
        return "--:--"
    return f"{int(t) // 60:02d}:{int(t) % 60:02d}"


def transcript_block(lines: Sequence[TranscriptLine], *, max_chars: int, keep_ids: set[str]) -> list[str]:
    """Full transcript when it fits; otherwise every line near a highlight plus an even sample of the rest."""
    rendered = [f"{line.id} [{_clock(line.edited_start)}-{_clock(line.edited_end)}] {line.text}" for line in lines]
    if sum(len(r) + 1 for r in rendered) <= max_chars:
        return rendered
    budget = max_chars
    chosen = []
    for line, text in zip(lines, rendered):
        if line.id in keep_ids:
            chosen.append(text)
            budget -= len(text) + 1
    others = [text for line, text in zip(lines, rendered) if line.id not in keep_ids]
    if others and budget > 0:
        step = max(1, int(sum(len(o) + 1 for o in others) / budget) + 1)
        chosen += others[::step]
    order = {text: i for i, text in enumerate(rendered)}
    chosen.sort(key=lambda text: order[text])
    return chosen + [f"(transcript shortened: {len(rendered) - len(chosen)} of {len(rendered)} lines omitted)"]


def build_prompt(*, language: str, edited_duration: float, games: Sequence[GameContext],
                 highlights: Sequence[HighlightCandidate], lines: Sequence[TranscriptLine],
                 frames: Sequence[Mapping[str, Any]], images: Sequence[Mapping[str, Any]],
                 evidence_status: Mapping[str, Any], max_deepen: int, max_transcript_chars: int = 120000,
                 previous_story: Mapping[str, Any] | None = None, include_transcript: bool = True) -> str:
    out = [f"PROMPT_VERSION: {PROMPT_VERSION}",
           f"OUTPUT LANGUAGE for every free-text field: {language}",
           f"EDITED VIDEO LENGTH: {_clock(edited_duration)} ({edited_duration:.0f} s). All times are EDITED video times.",
           "EVIDENCE AVAILABILITY: " + ", ".join(f"{k}={v}" for k, v in evidence_status.items()),
           "", "TASK:",
           "1. Understand the main topic, the themes and why a viewer would watch this video.",
           "2. For every game: summarise it, separate SPECIFIC PLAYS of that game (with transcript ids) from GENERAL "
           "talk about the deck/matchup. The creator often plays the SAME deck in every game: do not infer topic "
           "changes from class or keyword changes, read what is actually said.",
           "3. Rank the strongest highlights (only listed highlight ids). Local signal scores are hints, not verdicts.",
           "4. Propose several clearly DIFFERENT narrative angles for packaging, each backed by highlight ids.",
           "5. Card readings: only when a card name is legible in a listed frame. Give its box in that frame "
           "(normalized x,y,w,h; frames carry a 10x10 grid of reference lines when marked 'grid'), legibility and "
           "transcript ids that mention it. Never name a card from deck knowledge or meta expectations; use null when "
           "unreadable.",
           "6. risky_claims: everything tempting but not verifiable from this evidence (results, numbers, card names, "
           "intentions, emotions).",
           f"7. deepen_requests: at most {max_deepen} bounded intervals (<= 20 s each, edited times) where more frames "
           "or webcam crops would change your judgement; empty when not needed.",
           "", "GAMES (kept content only):"]
    if not games:
        out.append("- none identified")
    for game in games:
        markers = ", ".join(f"{k} at {_clock((v or {}).get('edited'))}" for k, v in game.markers.items()
                            if (v or {}).get("edited") is not None)
        label = f"game {game.index}" if game.index is not None else game.game_id
        out.append(f"- {game.game_id}: {label}, result {game.result or 'n/a'} (from local detector), edited "
                   f"{_clock(game.edited_start)}-{_clock(game.edited_end)}, {game.kept_seconds:.0f} s kept"
                   f"{'; ' + markers if markers else ''}; highlights {', '.join(game.highlight_ids) or 'none'}")
    out += ["", "HIGHLIGHT CANDIDATES (local signals; ids are the only valid references):"]
    for h in highlights:
        audio = ", ".join(f"{k}={v:.2f}" for k, v in h.audio.items()) or "audio n/a"
        out.append(f"- {h.id} game={h.game_id} edited {_clock(h.edited_start)}-{_clock(h.edited_end)} "
                   f"signals={'+'.join(h.evidence_sources)}{' (coverage)' if h.coverage else ''} | {h.reason} | {audio} | "
                   f"frames {', '.join(h.frame_ids) or 'none'} | webcam {', '.join(h.webcam_frame_ids) or 'none'}")
    out += ["", "FRAMES (id, kind, edited time, highlight, notes):"]
    for frame in frames:
        out.append(f"- {frame['id']} {frame['kind']} {_clock(frame.get('edited_time'))} {frame.get('highlight_id') or ''} "
                   f"{frame.get('role', '')} {frame.get('notes', '')}".rstrip())
    out += ["", "ATTACHED IMAGES (in order):"]
    for n, image in enumerate(images, start=1):
        out.append(f"- image {n}: {image['description']}")
    keep_ids = {i for h in highlights for i in h.transcript_ids}
    out += ["", "TRANSCRIPT (kept content only; ASR may contain recognition errors):"]
    if include_transcript:
        out += transcript_block(lines, max_chars=max_transcript_chars, keep_ids=keep_ids) or ["- (no transcribed speech)"]
    else:  # refinement call: the transcript was already analysed; it is not sent twice
        out.append("- (already analysed in your previous answer; cite the same transcript ids)")
    if previous_story is not None:
        import json
        out += ["", "YOUR PREVIOUS ANALYSIS (refine it with the additional images for the deepened intervals; "
                "deepen_requests must now be empty):", json.dumps(previous_story, ensure_ascii=False)[:20000]]
    out += ["", "Answer with the JSON object only."]
    return "\n".join(out)


def validate_story(raw: Any, *, highlight_ids: set[str], frame_ids: set[str], transcript_ids: set[str],
                   game_ids: set[str], max_deepen: int, edited_duration: float) -> dict[str, Any]:
    """Raise ValueError (with a precise message for the repair attempt) on any invalid reference."""
    if not isinstance(raw, dict):
        raise ValueError("answer must be a JSON object")
    missing = [k for k in RESPONSE_SCHEMA["required"] if k not in raw]
    if missing:
        raise ValueError(f"missing fields: {missing}")
    story = dict(raw)
    problems: list[str] = []

    def check_ids(values: Any, valid: set[str], where: str) -> list[str]:
        if not isinstance(values, list):
            problems.append(f"{where} must be a list")
            return []
        bad = [v for v in values if v not in valid]
        if bad:
            problems.append(f"{where} references unknown ids {bad[:5]}")
        return [v for v in values if v in valid]

    if not str(story.get("main_topic", "")).strip():
        problems.append("main_topic is empty")
    for game in story.get("games", []):
        if game.get("game_id") not in game_ids:
            problems.append(f"games: unknown game_id {game.get('game_id')!r}")
        for key in ("specific_plays", "general_talk"):
            for item in game.get(key, []):
                check_ids(item.get("transcript_ids"), transcript_ids, f"games.{key}.transcript_ids")
    strongest = story.get("strongest_highlights", [])
    if not strongest:
        problems.append("strongest_highlights is empty")
    for item in strongest:
        if item.get("highlight_id") not in highlight_ids:
            problems.append(f"strongest_highlights: unknown highlight {item.get('highlight_id')!r}")
        check_ids(item.get("transcript_ids"), transcript_ids, "strongest_highlights.transcript_ids")
        check_ids(item.get("frame_ids"), frame_ids, "strongest_highlights.frame_ids")
        if not 0.0 <= float(item.get("strength", 0.0)) <= 1.0:
            problems.append("strongest_highlights.strength must be within 0..1")
    angles = story.get("narrative_angles", [])
    if len(angles) < 2:
        problems.append("provide at least two different narrative_angles")
    for item in angles:
        check_ids(item.get("highlight_ids"), highlight_ids, "narrative_angles.highlight_ids")
    for item in story.get("card_observations", []):
        if item.get("frame_id") not in frame_ids:
            problems.append(f"card_observations: unknown frame {item.get('frame_id')!r}")
        box = item.get("box")
        if not _valid_box(box):
            problems.append(f"card_observations: invalid box {box}")
        check_ids(item.get("transcript_ids"), transcript_ids, "card_observations.transcript_ids")
    requests = story.get("deepen_requests", [])
    if len(requests) > max_deepen:
        problems.append(f"at most {max_deepen} deepen_requests are allowed")
    for item in requests:
        a, b = float(item.get("edited_start", -1)), float(item.get("edited_end", -1))
        if not (0.0 <= a < b <= edited_duration + 0.5 and b - a <= 20.0 + 1e-6):
            problems.append(f"deepen_request [{a}, {b}] must be inside the video and at most 20 s long")
    if problems:
        raise ValueError("; ".join(problems[:12]))
    return story


def _valid_box(box: Any) -> bool:
    if not isinstance(box, list) or len(box) != 4:
        return False
    try:
        x, y, w, h = (float(v) for v in box)
    except (TypeError, ValueError):
        return False
    return 0.0 <= x < 1.0 and 0.0 <= y < 1.0 and w > 0.005 and h > 0.005 and x + w <= 1.001 and y + h <= 1.001
