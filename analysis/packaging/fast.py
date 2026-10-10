"""FAST path: one agent call -> VideoStory + angles + titles + thumbnail concepts + render plans.

Evidence is prepared locally into a PackagingBrief; the agent answers once with a
single structured object that ReCut validates end to end. Optional, only when
the agent says information is really missing: ONE deepen request -> ReCut
extracts that interval -> ONE small "patch" call that returns only the missing
facts and the updated concept (the story is not repeated). Invalid JSON gets one
minimal correction call (previous answer + errors + valid ids; no transcript, no
images). Standard cost: 1 call; worst case: 1 + 1 repair + 1 patch (+1 repair).
"""
from __future__ import annotations

import json
import re
from typing import Any, Mapping, Sequence

from . import creative
from .brief import PackagingBrief
from .render import LAYOUTS

PROMPT_VERSION = "packaging_fast_v1"
PATCH_VERSION = "packaging_fast_patch_v1"

SYSTEM_PROMPT = (
    "You are a senior YouTube editor and packaging strategist for gaming creators. From a compact, verified "
    "evidence brief of an ALREADY EDITED video you (1) understand the story and (2) design truthful title + "
    "thumbnail concepts, in one answer. Cite only the ids you are given. Never invent events, results, numbers, "
    "card names, combos or quotes; facial expressions are not proof of emotions; never promise performance. "
    "Thumbnails are described only as structured composition plans: a local renderer composes real frames. "
    "Do not use tools, files or commands. Reply only with the requested JSON object."
)

_IDS = {"type": "array", "items": {"type": "string"}}
_OBJ = creative._obj
_CONCEPT = json.loads(json.dumps(creative.RESPONSE_SCHEMA["properties"]["concepts"]["items"]))
_CONCEPT["properties"]["composition_plan"]["properties"]["layout"]["enum"] = list(LAYOUTS)
_CONCEPT["properties"]["card_asset_ids"]["description"] = "obs_id values of card_observations (O1, O2, ...)"
_TITLE = creative.RESPONSE_SCHEMA["properties"]["titles"]["items"]
_OBSERVATION = _OBJ({
    "obs_id": {"type": "string", "description": "O1, O2, ..."},
    "frame_id": {"type": "string"},
    "box": {"type": "array", "items": {"type": "number"}, "description": "normalized [x, y, w, h] in that frame"},
    "name": {"type": ["string", "null"]},
    "legibility": {"type": "string", "enum": ["clear", "partial", "unreadable"]},
    "confidence": {"type": "number"},
    "transcript_ids": _IDS,
})
_DEEPEN = _OBJ({
    "concept_id": {"type": "string"},
    "edited_start": {"type": "number"},
    "edited_end": {"type": "number"},
    "want": {"type": "string", "enum": ["frames", "webcam"]},
    "question": {"type": "string", "description": "the single missing fact you need"},
})

RESPONSE_SCHEMA: dict[str, Any] = _OBJ({
    "language": {"type": "string"},
    "main_topic": {"type": "string"},
    "summary": {"type": "string"},
    "themes": {"type": "array", "items": {"type": "string"}},
    "why_interesting": {"type": "string"},
    "games": {"type": "array", "items": _OBJ({
        "game_id": {"type": "string"},
        "summary": {"type": "string"},
        "key_moments": {"type": "array", "items": _OBJ({"highlight_id": {"type": "string"},
                                                          "description": {"type": "string"},
                                                          "transcript_ids": _IDS})},
        "deck_talk": {"type": "array", "items": _OBJ({"description": {"type": "string"}, "transcript_ids": _IDS})},
        "conclusion": {"type": "string"},
    })},
    "strongest_highlights": {"type": "array", "items": _OBJ({"highlight_id": {"type": "string"},
                                                             "why": {"type": "string"},
                                                             "strength": {"type": "number"}})},
    "narrative_angles": {"type": "array", "items": _OBJ({"angle_id": {"type": "string"},
                                                         "angle": {"type": "string"}, "highlight_ids": _IDS})},
    "card_observations": {"type": "array", "items": _OBSERVATION},
    "risky_claims": {"type": "array", "items": _OBJ({"claim": {"type": "string"}, "why": {"type": "string"}})},
    "concepts": {"type": "array", "items": _CONCEPT},
    "titles": {"type": "array", "items": _TITLE},
    "preferred_title_id": {"type": "string"},
    "preferred_concept_id": {"type": "string"},
    "deepen_requests": {"type": "array", "items": _DEEPEN},
})

PATCH_SCHEMA: dict[str, Any] = _OBJ({
    "answer": {"type": "string", "description": "the missing fact, or why it is still not determinable"},
    "card_observations": {"type": "array", "items": _OBSERVATION},
    "concept": _CONCEPT,
})


def build_prompt(*, language: str, brief: PackagingBrief, n_titles: int, n_concepts: int,
                 style_lines: Sequence[str], images: Sequence[Mapping[str, Any]], max_deepen: int,
                 webcam_available: bool) -> str:
    out = [f"PROMPT_VERSION: {PROMPT_VERSION}",
           f"LANGUAGE of every free-text field, title and thumbnail text: {language} (natural and idiomatic)",
           "", "PART 1 - UNDERSTAND: main topic, themes, why someone would watch; per game a short summary, the key "
           "moments (highlight ids) and deck/matchup talk kept apart from specific plays (the creator often plays "
           "the SAME deck: read what is said, do not infer topic changes from class or keywords); strongest "
           "highlights; 2-4 clearly different narrative angles; card readings only when a name is legible in a listed "
           "frame (box on that frame; grid images have lines every 0.1), else name null; risky_claims = what is "
           "tempting but not verifiable here.",
           "", "PART 2 - PACKAGE:",
           f"- exactly {n_concepts} concepts, each a different angle, and exactly {n_titles} titles in total "
           "(each concept's title included), varied: competitive, informative, surprising, narrative.",
           "- YouTube TITLE: descriptive and specific, natural spoken language, <= 70 characters preferred "
           "(never > 100), concrete about what happens, curiosity without misleading; no ALL CAPS.",
           "- THUMBNAIL TEXT is a different thing: 2-4 words (5 only if indispensable), punchy, spoken, adds "
           "something the title does not say, never a copy of the title, no generic slogans.",
           "- ARCHETYPES (composition_plan.layout):"]
    out += [f"  * {name}: {text}" for name, text in LAYOUTS.items()]
    out += ["- Use at least two different archetypes. Pick the archetype that fits the angle; do NOT force the webcam "
            "or a card into every concept; a badge only when evidenced (e.g. WIN only for a WIN game); no "
            "decoration for its own sake.",
            "- background_frame_id: a listed gameplay frame; focus_box crops what matters (expanded to 16:9; "
            "null = whole frame); prefer settled, readable moments over animations." +
            ("" if webcam_available else " NO WEBCAM is available: do not use reaction_focus."),
            "- card_asset_ids hold obs_id values of your card_observations; uncertain readings are never named.",
            "- inset_id: another listed frame for a story_focus split (before/after, conflict) or a board_focus inset.",
            "- emphasis_word: one word of the thumbnail text; colours #RRGGBB with high contrast.",
            "- evidence: cite highlight, transcript and frame ids for every factual claim.",
            f"- deepen_requests: at most {max_deepen}, ONLY if one concrete fact is truly missing (an unreadable card, "
            "an unclear play) for a concept; <= 20 s of edited time. Normally empty."]
    if style_lines:
        out += [""] + list(style_lines)
    else:
        out += ["", "CREATOR STYLE: no profile (assume no preference)."]
    out += ["", "GAMES:"]
    for game in brief.games:
        out.append(f"- {game['game_id']}: index {game['index']}, result {game['result'] or 'n/a'} (local detector), "
                   f"edited {game['edited'][0]}-{game['edited'][1]}, {game['kept_s']} s kept, markers "
                   f"{json.dumps(game['markers'])}, highlights {', '.join(game['highlights']) or 'none'}")
    out += ["", "HIGHLIGHTS (local signals rank candidates only; frames are already selected: best first):"]
    for h in brief.highlights:
        out.append(f"- {h['id']} game={h['game_id']} {h['edited'][0]}-{h['edited'][1]} "
                   f"signals={'+'.join(h['signals'])}{' (coverage)' if h['coverage'] else ''} | {h['reason']} | "
                   f"frames {','.join(h['frames']) or '-'} context {','.join(h['context_frames']) or '-'} | "
                   f"webcam {','.join(h['webcam']) or '-'} | lines {','.join(h['transcript_ids']) or '-'}")
    out += ["", f"TRANSCRIPT (kept content; {len(brief.selected_line_ids)} of {brief.total_lines} lines selected: "
            "all lines near highlights plus an even sample over each game; ASR may mis-hear names):"]
    for game_id, rows in brief.transcript.items():
        out.append(f"[{game_id}]")
        out += rows or ["(no speech)"]
    out += ["", "ATTACHED IMAGES (in order):"]
    out += [f"- image {n}: {image['description']}" for n, image in enumerate(images, start=1)]
    out += ["", "Answer with the JSON object only."]
    return "\n".join(out)


def card_table(observations: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """obs_id -> {status, name}: a reading is usable only when clear and named; everything else is uncertain."""
    table = {}
    for item in observations:
        clear = item.get("legibility") == "clear" and bool(item.get("name"))
        table[str(item.get("obs_id"))] = {"status": "legible" if clear else "uncertain", "name": item.get("name")}
    return table


def validate(raw: Any, *, n_titles: int, n_concepts: int, highlight_ids: set[str], frame_ids: set[str],
             webcam_ids: set[str], transcript: Mapping[str, str], games: Mapping[str, Mapping[str, Any]],
             highlight_games: Mapping[str, str | None], supported_numbers: set[str], max_deepen: int,
             edited_duration: float) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("answer must be a JSON object")
    missing = [k for k in RESPONSE_SCHEMA["required"] if k not in raw]
    if missing:
        raise ValueError(f"missing fields: {missing}")
    problems: list[str] = []

    def ids(values: Any, valid: set[str] | Mapping[str, Any], where: str) -> None:
        bad = [v for v in (values or []) if v not in valid]
        if bad:
            problems.append(f"{where}: unknown ids {bad[:5]}")

    if not str(raw.get("main_topic", "")).strip():
        problems.append("main_topic is empty")
    for game in raw.get("games", []):
        if game.get("game_id") not in games:
            problems.append(f"games: unknown game_id {game.get('game_id')!r}")
        for item in game.get("key_moments", []):
            if item.get("highlight_id") not in highlight_ids:
                problems.append(f"key_moments: unknown highlight {item.get('highlight_id')!r}")
            ids(item.get("transcript_ids"), transcript, "key_moments.transcript_ids")
        for item in game.get("deck_talk", []):
            ids(item.get("transcript_ids"), transcript, "deck_talk.transcript_ids")
    for item in raw.get("strongest_highlights", []):
        if item.get("highlight_id") not in highlight_ids:
            problems.append(f"strongest_highlights: unknown highlight {item.get('highlight_id')!r}")
    for item in raw.get("narrative_angles", []):
        ids(item.get("highlight_ids"), highlight_ids, "narrative_angles.highlight_ids")
    obs_ids = [str(o.get("obs_id")) for o in raw.get("card_observations", [])]
    if len(set(obs_ids)) != len(obs_ids):
        problems.append("card_observations: obs_id values must be unique")
    for item in raw.get("card_observations", []):
        if item.get("frame_id") not in frame_ids:
            problems.append(f"card_observations: unknown frame {item.get('frame_id')!r}")
        box = item.get("box")
        if not (isinstance(box, list) and len(box) == 4 and creative._box_ok(box)):
            problems.append(f"card_observations: invalid box {box}")
        ids(item.get("transcript_ids"), transcript, "card_observations.transcript_ids")
    concept_ids = {c.get("concept_id") for c in raw.get("concepts", [])}
    requests = raw.get("deepen_requests", [])
    if len(requests) > max_deepen:
        problems.append(f"at most {max_deepen} deepen_requests are allowed")
    for item in requests:
        a, b = float(item.get("edited_start", -1)), float(item.get("edited_end", -1))
        if item.get("concept_id") not in concept_ids:
            problems.append("deepen_requests: concept_id must be one of the concepts")
        if not (0.0 <= a < b <= edited_duration + 0.5 and b - a <= 20.0 + 1e-6):
            problems.append(f"deepen_request [{a}, {b}] must be inside the video and at most 20 s long")
    cards = card_table(raw.get("card_observations", []))
    uncertain = [c["name"] for c in cards.values() if c["status"] == "uncertain" and c.get("name")]
    try:
        creative.validate_creative(raw, n_titles=n_titles, n_concepts=n_concepts, highlight_ids=highlight_ids,
                                   frame_ids=frame_ids, webcam_ids=webcam_ids, cards=cards, transcript=transcript,
                                   games=games, highlight_games=highlight_games, supported_numbers=supported_numbers,
                                   uncertain_names=uncertain)
    except ValueError as exc:
        problems.append(str(exc))
    if problems:
        raise ValueError("; ".join(list(dict.fromkeys(problems))[:15]))
    return raw


def repair_prompt(previous: Any, error: str, *, valid: Mapping[str, Sequence[str]], n_titles: int,
                  n_concepts: int) -> str:
    """Minimal correction request: the previous JSON, the validation errors and the valid ids only."""
    lines = [f"PROMPT_VERSION: {PROMPT_VERSION}-repair",
             "Your previous JSON answer failed validation. Fix ONLY what the errors require and return the complete "
             "corrected JSON object (same schema). Do not invent new facts.",
             f"Required: exactly {n_concepts} concepts and exactly {n_titles} titles.",
             "ERRORS:", error[:2000], "", "VALID IDS:"]
    lines += [f"- {name}: {', '.join(values)}" for name, values in valid.items()]
    lines += ["", "PREVIOUS ANSWER:", json.dumps(previous, ensure_ascii=False)[:60000]]
    return "\n".join(lines)


def patch_prompt(*, language: str, request: Mapping[str, Any], concept: Mapping[str, Any],
                 images: Sequence[Mapping[str, Any]], valid: Mapping[str, Sequence[str]]) -> str:
    lines = [f"PROMPT_VERSION: {PATCH_VERSION}", f"LANGUAGE: {language}",
             "You asked for more evidence to complete ONE concept. The new frames are attached. Answer the question "
             "in one or two sentences, add card_observations only for cards you can now read (obs_id P1, P2, ...; "
             "box on the listed frame), and return the updated concept (same concept_id; you may change background, "
             "focus_box, cards, text). If the evidence still does not settle it, keep the concept safe: do not name "
             "anything uncertain.",
             f"QUESTION: {request.get('question')}",
             f"INTERVAL: edited {request.get('edited_start')}-{request.get('edited_end')} s",
             "CONCEPT:", json.dumps(concept, ensure_ascii=False), "", "VALID IDS:"]
    lines += [f"- {name}: {', '.join(values)}" for name, values in valid.items()]
    lines += ["", "ATTACHED IMAGES:"] + [f"- image {n}: {i['description']}" for n, i in enumerate(images, start=1)]
    return "\n".join(lines)


def to_story(raw: Mapping[str, Any]) -> dict[str, Any]:
    """The understanding part of a FAST answer, in the VideoStory shape used by reports and --only runs."""
    keys = ("language", "main_topic", "summary", "themes", "why_interesting", "games", "strongest_highlights",
            "narrative_angles", "card_observations", "risky_claims")
    return {k: raw.get(k) for k in keys}


def thumbnail_words(text: str) -> list[str]:
    return [w for w in re.findall(r"[\wàèéìòóù']+", text.lower())]
