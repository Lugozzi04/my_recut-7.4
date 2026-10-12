"""Phase B - Creative Packaging: VideoStory + evidence -> paired title / thumbnail concepts.

The agent proposes truly different directions; each concept pairs a title with a
structured composition plan for ThumbnailRenderer (layout from a fixed catalog,
frame/webcam/card ids, normalized boxes, colours, short text). It never writes
code or pixels. Validation rejects unknown ids, impossible boxes, overlong text,
numbers or card names that the evidence does not support, CTR/performance
promises and concepts that are near-duplicates of each other.
"""
from __future__ import annotations

import json
import re
from typing import Any, Mapping, Sequence

from .render import LAYOUTS, canonical_layout

PROMPT_VERSION = "packaging_creative_v1"
TITLE_MAX_CHARS = 100  # YouTube limit
TITLE_MIN_CHARS = 15
THUMB_TEXT_MAX_WORDS = 5
THUMB_TEXT_MAX_CHARS = 32
DIRECTIONS = ("competitive", "informative", "surprising", "narrative", "educational", "emotional", "question")
PERFORMANCE_CLAIMS = re.compile(r"\b(ctr|click[- ]?through|views? garantit|garantit[oaie]|viral[ei]? sicur|"
                                r"guaranteed|go viral)\b", re.IGNORECASE)

SYSTEM_PROMPT = (
    "You are a YouTube packaging strategist for gaming creators. From a validated analysis of an edited video "
    "you design title + thumbnail concepts that are attractive AND truthful. Each concept is a different creative "
    "direction tied to real moments (highlight ids, frame ids, transcript ids). You describe thumbnails ONLY as the "
    "requested structured composition plan: a local renderer composes the real frames. Never invent cards, numbers, "
    "results, combos, quotes or events; never promise performance (CTR, views). Do not use tools, files or "
    "commands. Reply only with the requested JSON object."
)

_IDS = {"type": "array", "items": {"type": "string"}}
_BOX_OR_NULL = {"type": ["array", "null"], "items": {"type": "number"},
                "description": "normalized [x, y, w, h] in 0..1 of the referenced image, or null"}


def _obj(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


RESPONSE_SCHEMA: dict[str, Any] = _obj({
    "concepts": {"type": "array", "items": _obj({
        "concept_id": {"type": "string"},
        "narrative_angle": {"type": "string"},
        "direction": {"type": "string", "enum": list(DIRECTIONS)},
        "title": {"type": "string"},
        "thumbnail_text": {"type": "string"},
        "highlight_ids": _IDS,
        "source_frame_ids": _IDS,
        "webcam_frame_id": {"type": ["string", "null"]},
        "card_asset_ids": _IDS,
        "composition_plan": _obj({
            "layout": {"type": "string", "enum": list(LAYOUTS)},
            "background_frame_id": {"type": "string"},
            "focus_box": _BOX_OR_NULL,
            "inset_id": {"type": ["string", "null"]},
            "inset_box": _BOX_OR_NULL,
            "text_side": {"type": "string", "enum": ["left", "right", "top", "bottom"]},
            "webcam_side": {"type": "string", "enum": ["left", "right"]},
            "emphasis_word": {"type": ["string", "null"]},
            "badge": {"type": ["string", "null"]},
            "accent_color": {"type": "string", "description": "#RRGGBB"},
            "text_color": {"type": "string", "description": "#RRGGBB"},
            "rationale": {"type": "string"},
        }),
        "evidence": {"type": "array", "items": _obj({
            "claim": {"type": "string"},
            "highlight_id": {"type": "string"},
            "transcript_ids": _IDS,
            "frame_ids": _IDS,
        })},
        "risk_notes": {"type": "string"},
    })},
    "titles": {"type": "array", "items": _obj({
        "title_id": {"type": "string"},
        "title": {"type": "string"},
        "concept_id": {"type": "string"},
        "style": {"type": "string", "enum": list(DIRECTIONS)},
        "highlight_ids": _IDS,
        "transcript_ids": _IDS,
        "rationale": {"type": "string"},
    })},
    "preferred_title_id": {"type": "string"},
    "preferred_concept_id": {"type": "string"},
})


def _clock(t: Any) -> str:
    if t is None:
        return "--:--"
    return f"{int(t) // 60:02d}:{int(t) % 60:02d}"


def build_prompt(*, language: str, story: Mapping[str, Any], games: Sequence[Mapping[str, Any]],
                 highlights: Sequence[Mapping[str, Any]], frames: Sequence[Mapping[str, Any]],
                 webcam: Sequence[Mapping[str, Any]], cards: Sequence[Mapping[str, Any]],
                 transcript: Mapping[str, str], n_titles: int, n_concepts: int, style_lines: Sequence[str],
                 images: Sequence[Mapping[str, Any]], only: str | None = None,
                 previous: Mapping[str, Any] | None = None) -> str:
    out = [f"PROMPT_VERSION: {PROMPT_VERSION}",
           f"LANGUAGE for titles and thumbnail text: {language} (natural, idiomatic; no literal translations)",
           "", "DELIVERABLES:",
           f"- exactly {n_concepts} concepts with clearly DIFFERENT narrative angles and directions; use at least "
           f"{min(n_concepts, 3)} different layouts when the assets allow it; every concept has a paired title.",
           f"- exactly {n_titles} titles in total (the concept titles included), varied in style "
           "(competitive, informative, surprising, narrative...), each linked to a concept and to highlights.",
           "- preferred_title_id and preferred_concept_id: your recommendation (editorial judgement, no metrics).",
           "", "TITLE RULES: readable, <= 70 characters preferred and never over 100; concrete references to what "
           "actually happens; curiosity without misleading clickbait; no ALL-CAPS titles; at most one emoji and only "
           "if the creator style uses them; numbers only when they appear in the cited evidence.",
           f"THUMBNAIL TEXT RULES: 2-4 words (max {THUMB_TEXT_MAX_WORDS}), <= {THUMB_TEXT_MAX_CHARS} characters, it must "
           "complement the title (do not repeat it), readable on a phone; emphasis_word must be one of its words.",
           "COMPOSITION RULES:",
           "- background_frame_id: a gameplay frame (F...) that shows the moment; focus_box crops the region that "
           "matters (it is expanded to 16:9 automatically; null = whole frame). Prefer boxes that keep the board/"
           "minions large and drop UI clutter.",
           "- webcam_frame_id: a webcam crop (W...) whose expression fits the angle; facial expressions are not proof "
           "of emotion, never claim one in text. reaction_focus requires it; do not force it elsewhere.",
           "- inset_id: another frame (F...) or card crop (K...) for board_focus; inset_box crops it.",
           "- card_asset_ids: only cards with status verified or legible; never put an uncertain card's name in any "
           "text. In board_focus the cards are the subject; do not force a card into every concept.",
           "- badge: optional 1-2 words; 'WIN' only for a game whose result is WIN.",
           "- colours: high contrast; accent_color for emphasis; text_color usually #FFFFFF.",
           "- evidence: for every factual claim in the title/text cite highlight, transcript and frame ids.",
           "", "LAYOUT CATALOG:"]
    out += [f"- {name}: {text}" for name, text in LAYOUTS.items()]
    if only == "titles" and previous is not None:
        out += ["", "REGENERATE TITLES ONLY: keep these concepts and composition plans unchanged (same concept ids, "
                "same composition_plan), write new, better titles:", json.dumps(previous.get("concepts", []),
                                                                                 ensure_ascii=False)[:20000]]
    if only == "thumbnails" and previous is not None:
        out += ["", "REGENERATE THUMBNAILS ONLY: keep these titles exactly (same title ids and text) and propose new "
                "thumbnail concepts and composition plans:", json.dumps(previous.get("titles", []),
                                                                        ensure_ascii=False)[:20000]]
    if style_lines:
        out += [""] + list(style_lines)
    else:
        out += ["", "CREATOR STYLE MEMORY: none (do not assume any preference)."]
    out += ["", "VIDEO STORY (validated phase A analysis):", json.dumps(story, ensure_ascii=False)[:40000]]
    out += ["", "GAMES:"]
    for game in games:
        out.append(f"- {game['game_id']}: index {game.get('index')}, result {game.get('result') or 'n/a'}, edited "
                   f"{_clock(game.get('edited_start'))}-{_clock(game.get('edited_end'))}")
    out += ["", "HIGHLIGHTS:"]
    for h in highlights:
        out.append(f"- {h['id']} game={h.get('game_id')} {_clock(h['edited_start'])}-{_clock(h['edited_end'])} "
                   f"signals={'+'.join(h['evidence_sources'])} frames={','.join(h.get('frame_ids', []))} "
                   f"webcam={','.join(h.get('webcam_frame_ids', []))} | {h['reason']}")
    out += ["", "GAMEPLAY FRAMES (id, edited time, highlight, role, sharpness score):"]
    for f in frames:
        out.append(f"- {f['id']} {_clock(f.get('edited_time'))} {f.get('highlight_id') or ''} {f.get('role', '')} "
                   f"q={f.get('quality', {}).get('score', 0):.2f}")
    out += ["", "WEBCAM CROPS (id, edited time, highlight, face found, quality):"]
    out += [f"- {w['id']} {_clock(w.get('edited_time'))} {w.get('highlight_id') or ''} "
            f"face={'yes' if w.get('quality', {}).get('face') else 'no'} q={w.get('quality', {}).get('score', 0):.2f}"
            for w in webcam] or ["- none (no webcam available: do not use reaction_focus)"]
    out += ["", "CARD CROPS (id, status, name, frame):"]
    out += [f"- {c['id']} {c['status']} name={c['name'] if c['status'] != 'uncertain' else 'UNKNOWN'} frame={c['frame_id']}"
            for c in cards] or ["- none (board_focus uses board regions only)"]
    out += ["", "TRANSCRIPT LINES CITED BY THE STORY AND HIGHLIGHTS:"]
    out += [f"- {tid}: {text}" for tid, text in transcript.items()]
    out += ["", "ATTACHED IMAGES (in order):"]
    out += [f"- image {n}: {image['description']}" for n, image in enumerate(images, start=1)]
    out += ["", "Answer with the JSON object only."]
    return "\n".join(out)


def _box_ok(box: Any) -> bool:
    if box is None:
        return True
    if not isinstance(box, list) or len(box) != 4:
        return False
    try:
        x, y, w, h = (float(v) for v in box)
    except (TypeError, ValueError):
        return False
    return 0.0 <= x < 1.0 and 0.0 <= y < 1.0 and w >= 0.05 and h >= 0.05 and x + w <= 1.001 and y + h <= 1.001


def _words(text: str) -> list[str]:
    return re.findall(r"[\w']+", text.lower())


def _numbers(text: str) -> set[str]:
    return set(re.findall(r"\d+", text))


def validate_creative(raw: Any, *, n_titles: int, n_concepts: int, highlight_ids: set[str], frame_ids: set[str],
                      webcam_ids: set[str], cards: Mapping[str, Mapping[str, Any]], transcript: Mapping[str, str],
                      games: Mapping[str, Mapping[str, Any]], highlight_games: Mapping[str, str | None],
                      supported_numbers: set[str], uncertain_names: Sequence[str]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("answer must be a JSON object")
    problems: list[str] = []
    concepts = raw.get("concepts") or []
    titles = raw.get("titles") or []
    if len(concepts) != n_concepts:
        problems.append(f"expected exactly {n_concepts} concepts, got {len(concepts)}")
    if len(titles) != n_titles:
        problems.append(f"expected exactly {n_titles} titles, got {len(titles)}")
    concept_ids = [c.get("concept_id") for c in concepts]
    if len(set(concept_ids)) != len(concept_ids):
        problems.append("concept_id values must be unique")
    numbers_ok = set(supported_numbers)

    def cite_numbers(ids: Sequence[str]) -> set[str]:
        return {n for i in ids for n in _numbers(transcript.get(i, ""))}

    def check_text(label: str, text: str, cited: set[str]) -> None:
        if PERFORMANCE_CLAIMS.search(text):
            problems.append(f"{label}: performance/CTR promises are not allowed")
        unsupported = _numbers(text) - numbers_ok - cited
        if unsupported:
            problems.append(f"{label}: numbers {sorted(unsupported)} are not supported by cited evidence")
        lowered = text.lower()
        for name in uncertain_names:
            if name and len(name) >= 4 and name.lower() in lowered:
                problems.append(f"{label}: '{name}' is an uncertain card reading and cannot be named")

    for concept in concepts:
        cid = concept.get("concept_id")
        plan = concept.get("composition_plan") or {}
        layout = canonical_layout(str(plan.get("layout")))
        hl = concept.get("highlight_ids") or []
        if not hl or any(h not in highlight_ids for h in hl):
            problems.append(f"{cid}: highlight_ids must reference listed highlights")
        if plan.get("background_frame_id") not in frame_ids:
            problems.append(f"{cid}: background_frame_id must be a listed gameplay frame")
        for f in concept.get("source_frame_ids") or []:
            if f not in frame_ids:
                problems.append(f"{cid}: unknown source frame {f}")
        webcam = concept.get("webcam_frame_id")
        if webcam is not None and webcam not in webcam_ids:
            problems.append(f"{cid}: unknown webcam frame {webcam}")
        if layout == "reaction_focus" and webcam is None:
            problems.append(f"{cid}: reaction_focus requires webcam_frame_id")
        card_ids = concept.get("card_asset_ids") or []
        for k in card_ids:
            if k not in cards:
                problems.append(f"{cid}: unknown card asset {k}")
            elif cards[k].get("status") == "uncertain" and layout == "board_focus":
                problems.append(f"{cid}: uncertain card {k} cannot be the subject of board_focus")
        inset = plan.get("inset_id")
        if inset is not None and inset not in frame_ids and inset not in cards:
            problems.append(f"{cid}: inset_id must be a frame or card id")
        for key in ("focus_box", "inset_box"):
            if not _box_ok(plan.get(key)):
                problems.append(f"{cid}: {key} must be null or a normalized [x,y,w,h] of at least 5%")
        for key in ("accent_color", "text_color"):
            if not re.fullmatch(r"#[0-9A-Fa-f]{6}", str(plan.get(key, ""))):
                problems.append(f"{cid}: {key} must be #RRGGBB")
        text = " ".join(str(concept.get("thumbnail_text", "")).split())
        if not text or len(text.split()) > THUMB_TEXT_MAX_WORDS or len(text) > THUMB_TEXT_MAX_CHARS:
            problems.append(f"{cid}: thumbnail_text must have 1-{THUMB_TEXT_MAX_WORDS} words and <= "
                            f"{THUMB_TEXT_MAX_CHARS} characters")
        words = _words(text)
        if len(words) >= 2 and " ".join(words) in " ".join(_words(str(concept.get("title", "")))):
            problems.append(f"{cid}: thumbnail_text repeats the title; it must add something")
        emphasis = plan.get("emphasis_word")
        if emphasis and emphasis.upper() not in {w.strip(".,!?:;\"'").upper() for w in text.split()}:
            problems.append(f"{cid}: emphasis_word must be a word of thumbnail_text")
        badge = plan.get("badge")
        if badge and len(str(badge)) > 14:
            problems.append(f"{cid}: badge must be at most 14 characters")
        if badge and str(badge).strip().upper() in ("WIN", "VITTORIA", "VINTA"):
            results = {games.get(str(highlight_games.get(h)), {}).get("result") for h in hl}
            if "WIN" not in results:
                problems.append(f"{cid}: a WIN badge needs a highlight from a game whose result is WIN")
        cited: set[str] = set()
        for item in concept.get("evidence") or []:
            if item.get("highlight_id") not in highlight_ids:
                problems.append(f"{cid}: evidence cites unknown highlight {item.get('highlight_id')}")
            bad = [t for t in item.get("transcript_ids") or [] if t not in transcript]
            if bad:
                problems.append(f"{cid}: evidence cites unknown transcript ids {bad[:4]}")
            cited |= cite_numbers(item.get("transcript_ids") or [])
        if not concept.get("evidence"):
            problems.append(f"{cid}: evidence must not be empty")
        check_text(f"{cid} title", str(concept.get("title", "")), cited)
        check_text(f"{cid} thumbnail_text", text, cited)
    if len(concepts) >= 3:
        layouts = {canonical_layout(str((c.get("composition_plan") or {}).get("layout"))) for c in concepts}
        backgrounds = [(c.get("composition_plan") or {}).get("background_frame_id") for c in concepts]
        angles = {str(c.get("narrative_angle", "")).strip().lower() for c in concepts}
        if len(layouts) < 2:
            problems.append("concepts must use at least two different layouts")
        if len(set(backgrounds)) < min(len(concepts), 2):
            problems.append("concepts must not all reuse the same background frame")
        if len(angles) < len(concepts):
            problems.append("each concept needs its own narrative_angle")
        texts = [str(c.get("thumbnail_text", "")).strip().lower() for c in concepts]
        if len(set(texts)) < len(texts):
            problems.append("thumbnail_text values must differ between concepts")
    title_ids = [t.get("title_id") for t in titles]
    if len(set(title_ids)) != len(title_ids):
        problems.append("title_id values must be unique")
    seen: set[str] = set()
    for t in titles:
        tid, text = t.get("title_id"), " ".join(str(t.get("title", "")).split())
        if not TITLE_MIN_CHARS <= len(text) <= TITLE_MAX_CHARS:
            problems.append(f"{tid}: title length must be {TITLE_MIN_CHARS}-{TITLE_MAX_CHARS} characters")
        letters = [c for c in text if c.isalpha()]
        if letters and sum(c.isupper() for c in letters) / len(letters) > 0.6:
            problems.append(f"{tid}: avoid ALL-CAPS titles")
        if text.lower() in seen:
            problems.append(f"{tid}: duplicate title")
        seen.add(text.lower())
        if t.get("concept_id") not in concept_ids:
            problems.append(f"{tid}: concept_id must reference a concept")
        hl = t.get("highlight_ids") or []
        if not hl or any(h not in highlight_ids for h in hl):
            problems.append(f"{tid}: highlight_ids must reference listed highlights")
        bad = [x for x in t.get("transcript_ids") or [] if x not in transcript]
        if bad:
            problems.append(f"{tid}: unknown transcript ids {bad[:4]}")
        check_text(tid or "title", text, cite_numbers(t.get("transcript_ids") or []))
    if raw.get("preferred_title_id") not in title_ids:
        problems.append("preferred_title_id must be one of the titles")
    if raw.get("preferred_concept_id") not in concept_ids:
        problems.append("preferred_concept_id must be one of the concepts")
    if problems:
        raise ValueError("; ".join(list(dict.fromkeys(problems))[:15]))
    return raw


# ---------------------------------------------------------------------------------------------------
# V3: portfolio art direction (scene graphs instead of fixed layouts)

PROMPT_VERSION_V3 = "packaging_portfolio_v3"


def _concept_v3_schema() -> dict[str, Any]:
    from .opportunities import STORY_TYPES, TREATMENTS
    from .scene import SCENE_SCHEMA

    return _obj({
        "concept_id": {"type": "string"},
        "opportunity_id": {"type": "string"},
        "story_type": {"type": "string", "enum": list(STORY_TYPES)},
        "narrative_angle": {"type": "string"},
        "message": {"type": "string", "description": "the idea a viewer gets in one second"},
        "treatment": {"type": "string", "enum": list(TREATMENTS)},
        "primary_subject": {"type": "string", "description": "asset id of the protagonist"},
        "supporting": _IDS,
        "omitted": {"type": "string", "description": "what you deliberately leave out and why"},
        "mood": {"type": "string"},
        "title": {"type": "string"},
        "thumbnail_text": {"type": "string", "description": "2-4 words, or empty string for no text"},
        "highlight_ids": _IDS,
        "evidence": {"type": "array", "items": _obj({
            "claim": {"type": "string"}, "highlight_id": {"type": "string"}, "transcript_ids": _IDS,
            "asset_ids": _IDS})},
        "scene": SCENE_SCHEMA,
        "risk_notes": {"type": "string"},
    })


def portfolio_sections(*, n_concepts: int, n_titles: int, language: str) -> list[str]:
    """The four tasks of the single art-direction call (shared by FAST v3 and DEEP v3)."""
    from .opportunities import TREATMENTS
    from .scene import LAYER_TYPES, MAX_LAYERS

    return [
        "A. EDITORIAL UNDERSTANDING - what really makes this video worth watching, and which evidence ids support "
        "each hook. Opportunities below are local hypotheses: confirm, merge, reframe or reject them.",
        f"B. PORTFOLIO PLANNING - design ONE coherent portfolio of exactly {n_concepts} strategies, not "
        f"{n_concepts} independent thumbnails. First decide narrative diversity (different moments or different "
        "readings of a moment, different messages), then visual diversity. Concepts must differ in several of: "
        "moment/story, message, protagonist, treatment, composition, use of text, assets. Colour, font or one word "
        "is NOT a difference. With enough material: >= 3 different story angles, >= 3 treatments, no two concepts "
        "with the same story + protagonist + composition, no shared main frame without an editorial reason. With "
        "little material, give the same event genuinely different readings; never invent events. Explain choices "
        "in portfolio_strategy (plan + tradeoffs).",
        "C. ART DIRECTION per concept - one immediately readable message; one protagonist (face, card, board "
        "situation or moment) and at most one or two supports; mood; foreground/background relation; text or no "
        "text; what to omit. Do NOT force a webcam, a card or text into every concept; do not make all concepts "
        "look like one template; no decorative frames, arrows, glows or badges.",
        f"D. PRODUCTION - a scene graph per concept using ONLY asset ids from the manifest. Layer types: "
        f"{', '.join(LAYER_TYPES)}; at most {MAX_LAYERS} layers. Coordinates: x, y = centre in 0..1 of the 1280x720 "
        "canvas; height = share of canvas height; width = share of canvas width (needed for text, gradient reach, "
        "shapes, board crops shown as panels). Keep text inside x 0.05-0.95 / y 0.05-0.95 and out of the bottom-right "
        "corner (YouTube duration badge). visual_hierarchy lists layer ids, protagonist first. Use subject_cutout only "
        "with webcam_subject assets marked isolated (non-isolated ones: small or omit). card layers use obs_id values "
        "of your card_observations (O1...) or listed card assets. Treatments: " + ", ".join(TREATMENTS) + ".",
        f"TITLES: exactly {n_titles} YouTube titles in {language}: descriptive, natural, specific, <= 70 characters "
        "preferred (never > 100), varied in style; each linked to a concept; titles and thumbnail text complement each "
        "other (never the same words). Thumbnail text: 2-4 words, or empty; spoken and natural, no generic slogans.",
        "EVIDENCE RULES: no invented cards, results, numbers, combos or quotes; facial expressions are not proof of "
        "emotions; no CTR or view promises; clarity at small size first.",
    ]


def preset_examples() -> str:
    """Compact scene examples (presets) so the model sees valid structures; they are starting points only."""
    import json as _json

    from .scene import preset_board, preset_reaction

    reaction = preset_reaction(background="F010", subject="S004", text="Quale tengo?", element="O1", side="right")
    board = preset_board(background="F021", text=None, region="B003")
    slim = []
    for scene in (reaction, board):
        compact = {**scene, "layers": [{k: v for k, v in layer.items() if v not in (None, "none", 0, 0.0) or
                                        k in ("id", "type", "x", "y", "height", "z")} for layer in scene["layers"]]}
        slim.append(_json.dumps(compact, ensure_ascii=False))
    return "\n".join(slim)


def validate_portfolio_answer(raw: Any, *, n_titles: int, n_concepts: int, highlight_ids: set[str],
                              assets: Mapping[str, Mapping[str, Any]], transcript: Mapping[str, str],
                              games: Mapping[str, Mapping[str, Any]], highlight_games: Mapping[str, str | None],
                              supported_numbers: set[str], opportunities: Mapping[str, Mapping[str, Any]],
                              available_story_types: int, available_treatments: int) -> dict[str, Any]:
    """Validate concepts (scene graphs + evidence), titles and portfolio diversity; ValueError lists problems."""
    from .portfolio import evaluate_portfolio
    from .scene import validate_scene

    if not isinstance(raw, dict):
        raise ValueError("answer must be a JSON object")
    problems: list[str] = []
    observations = {str(o.get("obs_id")): o for o in raw.get("card_observations") or []}
    lookup: dict[str, Mapping[str, Any]] = dict(assets)
    for obs_id, obs in observations.items():  # card readings become (virtual) card assets for validation
        legible = obs.get("legibility") == "clear" and bool(obs.get("name"))
        lookup[obs_id] = {"type": "card_crop", "parent_id": obs.get("frame_id"),
                          "provenance": {"status": "legible" if legible else "uncertain", "name": obs.get("name")}}
    uncertain = [str(o.get("name")) for o in observations.values()
                 if o.get("name") and not (o.get("legibility") == "clear")]
    concepts = raw.get("concepts") or []
    titles = raw.get("titles") or []
    if len(concepts) != n_concepts:
        problems.append(f"expected exactly {n_concepts} concepts, got {len(concepts)}")
    if len(titles) != n_titles:
        problems.append(f"expected exactly {n_titles} titles, got {len(titles)}")
    concept_ids = [c.get("concept_id") for c in concepts]
    if len(set(concept_ids)) != len(concept_ids):
        problems.append("concept_id values must be unique")

    def check_text(label: str, text: str, cited: set[str]) -> None:
        if PERFORMANCE_CLAIMS.search(text):
            problems.append(f"{label}: performance/CTR promises are not allowed")
        unsupported = _numbers(text) - supported_numbers - cited
        if unsupported:
            problems.append(f"{label}: numbers {sorted(unsupported)} are not supported by cited evidence")
        for name in uncertain:
            if len(name) >= 4 and name.lower() in text.lower():
                problems.append(f"{label}: '{name}' is an uncertain card reading and cannot be named")

    for concept in concepts:
        cid = concept.get("concept_id")
        if concept.get("opportunity_id") not in opportunities:
            problems.append(f"{cid}: opportunity_id must be a listed opportunity")
        hl = concept.get("highlight_ids") or []
        if not hl or any(h not in highlight_ids for h in hl):
            problems.append(f"{cid}: highlight_ids must reference listed highlights")
        scene = concept.get("scene")
        problems += validate_scene(scene, lookup, where=f"{cid}.scene")
        if concept.get("primary_subject") not in lookup:
            problems.append(f"{cid}: primary_subject must be an asset id")
        elif isinstance(scene, Mapping):
            used = {(scene.get("background") or {}).get("asset_id")} | {layer.get("asset_id") for layer in
                                                                        scene.get("layers") or [] if isinstance(layer, Mapping)}
            if concept.get("primary_subject") not in used:
                problems.append(f"{cid}: primary_subject {concept.get('primary_subject')} is not drawn in its scene")
            for layer in scene.get("layers") or []:
                if isinstance(layer, Mapping) and layer.get("type") == "card":
                    info = lookup.get(str(layer.get("asset_id"))) or {}
                    if (info.get("provenance") or {}).get("status") == "uncertain" and \
                            layer.get("id") in (scene.get("visual_hierarchy") or [])[:1]:
                        problems.append(f"{cid}: an uncertain card reading cannot be the protagonist")
        text = " ".join(str(concept.get("thumbnail_text") or "").split())
        if text and (len(text.split()) > THUMB_TEXT_MAX_WORDS or len(text) > THUMB_TEXT_MAX_CHARS):
            problems.append(f"{cid}: thumbnail_text must have at most {THUMB_TEXT_MAX_WORDS} words and "
                            f"{THUMB_TEXT_MAX_CHARS} characters (or be empty)")
        if isinstance(scene, Mapping):
            layer_texts = [" ".join(str(layer.get("text") or "").split()) for layer in scene.get("layers") or []
                           if isinstance(layer, Mapping) and layer.get("type") == "text"]
            if text and text not in layer_texts:
                problems.append(f"{cid}: thumbnail_text must appear as a text layer of its scene")
            if not text and layer_texts:
                problems.append(f"{cid}: text layers present but thumbnail_text is empty")
        words = _words(text)
        if len(words) >= 2 and " ".join(words) in " ".join(_words(str(concept.get("title", "")))):
            problems.append(f"{cid}: thumbnail_text repeats the title; it must add something")
        cited: set[str] = set()
        for item in concept.get("evidence") or []:
            if item.get("highlight_id") not in highlight_ids:
                problems.append(f"{cid}: evidence cites unknown highlight {item.get('highlight_id')}")
            bad = [t for t in item.get("transcript_ids") or [] if t not in transcript]
            bad += [a for a in item.get("asset_ids") or [] if a not in lookup]
            if bad:
                problems.append(f"{cid}: evidence cites unknown ids {bad[:4]}")
            cited |= {n for t in item.get("transcript_ids") or [] for n in _numbers(transcript.get(t, ""))}
        if not concept.get("evidence"):
            problems.append(f"{cid}: evidence must not be empty")
        check_text(f"{cid} title", str(concept.get("title", "")), cited)
        check_text(f"{cid} thumbnail_text", text, cited)
        badge_words = {"WIN", "VITTORIA", "VINTA"}
        if any(w.upper() in badge_words for w in text.split()):
            results = {games.get(str(highlight_games.get(h)), {}).get("result") for h in hl}
            if "WIN" not in results:
                problems.append(f"{cid}: claims a win for a highlight whose game is not a WIN")
    title_ids = [t.get("title_id") for t in titles]
    seen: set[str] = set()
    for t in titles:
        tid, text = t.get("title_id"), " ".join(str(t.get("title", "")).split())
        if not TITLE_MIN_CHARS <= len(text) <= TITLE_MAX_CHARS:
            problems.append(f"{tid}: title length must be {TITLE_MIN_CHARS}-{TITLE_MAX_CHARS} characters")
        letters = [c for c in text if c.isalpha()]
        if letters and sum(c.isupper() for c in letters) / len(letters) > 0.6:
            problems.append(f"{tid}: avoid ALL-CAPS titles")
        if text.lower() in seen:
            problems.append(f"{tid}: duplicate title")
        seen.add(text.lower())
        if t.get("concept_id") not in concept_ids:
            problems.append(f"{tid}: concept_id must reference a concept")
        if any(h not in highlight_ids for h in t.get("highlight_ids") or []) or not t.get("highlight_ids"):
            problems.append(f"{tid}: highlight_ids must reference listed highlights")
        check_text(str(tid), text, {n for x in t.get("transcript_ids") or [] for n in _numbers(transcript.get(x, ""))})
    if raw.get("preferred_title_id") not in title_ids:
        problems.append("preferred_title_id must be one of the titles")
    if raw.get("preferred_concept_id") not in concept_ids:
        problems.append("preferred_concept_id must be one of the concepts")
    if not problems:
        diversity = evaluate_portfolio(concepts, lookup, available_story_types=available_story_types,
                                       available_treatments=available_treatments,
                                       available_opportunities=len(opportunities))
        for finding in diversity.blocking:
            problems.append(f"portfolio {finding.code} ({', '.join(finding.concepts)}): {finding.detail}")
    if problems:
        raise ValueError("; ".join(list(dict.fromkeys(problems))[:15]))
    return raw
