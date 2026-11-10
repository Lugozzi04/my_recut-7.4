"""Thumbnail playbook: the working rules every cover is reasoned and judged with (planning, image model, critic).

Written from what made the creator's best cover work (19 Oct 2026, a ChatGPT-guided session: "NON CRAFTARLA!" on
the worst card of a tier list, the creator's sceptical face, the tier list readable behind) and from common
YouTube packaging practice. Three uses:

  IDEATION   before any concept: the video's promise, 8-12 raw hooks by family, the 2 strongest chosen with the
             reason a viewer would click, the others rejected in writing. Concepts are VARIANTS of the chosen ideas
             (2 ideas x 2 variants for 4 concepts), not 4 forced angles.
  PRINCIPLES one message, 2-4 words, face -> text -> object, the object of the video big and in the audience's
             language, context recognisable but subordinate, expression = message, graphic cues with a meaning.
  CRITIQUE   the generated covers are ranked against a checklist at full size and at phone size, and the best
             one gets one revision pass.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

PLAYBOOK_VERSION = "thumbnail_playbook_v2"  # v2: bold verdicts, one specific card for lists/guides

IDEA_FAMILIES = {
    "warning": "a mistake to avoid / a trap (NON CRAFTARLA!, TRAPPOLA, NON FARLO): usually the strongest click for "
               "guides and tier lists",
    "verdict": "a strong, clear judgement (ROTTA, INGIOCABILE, LA MIGLIORE)",
    "curiosity": "an open loop the video closes (COME?!, PERCHÉ NESSUNO LA GIOCA?)",
    "contrast": "good vs bad, before vs after, expectation vs reality",
    "extreme_result": "an extreme number or outcome that really happened (12-0, 60 DANNI)",
    "reaction": "the creator's genuine emotion at a decisive moment (shock, disgust, joy)",
    "challenge": "a goal or a dare the video attempts (SOLO CARTE COMUNI, LEGGENDA IN 1 GIORNO)",
}

VIDEO_TYPES = {
    "tier_list_or_guide": "negative framing beats utility. At least one chosen idea is about ONE SPECIFIC card "
                          "(card_name required): the worst or most surprising card of the lowest tier (or the bait "
                          "everyone gets wrong) BIG with a red X / arrows and the creator's verdict on THAT card as "
                          "an imperative (NON CRAFTARLA!, TRAPPOLA!, INGIOCABILE); the other idea may be the best card "
                          "(CRAFTALA!) or the whole ranking. The tier list (or the guide's key screen) stays readable "
                          "behind; the title carries the useful promise (safe crafts, best decks).",
    "gameplay_highlight": "the decisive moment (lethal, top-deck, misplay, comeback) + the creator's reaction; "
                          "the result or the key card big; never an ordinary board.",
    "deck_guide": "the deck's win condition or signature card big + a verdict or a number that is true "
                  "(win rate, rank reached); the creator confident or excited.",
    "reaction_or_story": "the creator's strongest expression + the object of the emotion; a curiosity hook.",
}

PRINCIPLES = (
    "ONE idea per cover, readable in one second at phone size (about 168x94 px).",
    "Hook text: 2-4 words, an imperative, a judgement or a short exclamation (NON CRAFTARLA!, TRAPPOLA, ROTTA?!); "
    "never an abstract phrase or a summary (not 'La distinzione conta', not 'Oltre un mazzo').",
    "Be BOLD: when the creator gives a verdict (even a provisional one, e.g. a card in the lowest tier), state it "
    "plainly. No hedging in the hook: no 'ma', 'tutto', 'forse', 'non sempre', 'situazionale' - nuance belongs in the "
    "video and the title, not on the cover.",
    "One object beats many: a list or ranking video is sold by ONE specific card (or result), not by the list.",
    "Cover = emotion and judgement; title = clarity and the useful promise. They complement each other and never "
    "repeat the same words.",
    "Hierarchy: face -> text -> object (or object -> text when there is no face). At most 3 strong elements.",
    "The object of the video is BIG: the key card about 35-45% of the height, the official card image in the "
    "audience's language when available; never a tiny or blurry crop as the protagonist.",
    "Context is recognisable but subordinate: the tier list / board / key screen behind, readable labels, softly "
    "blurred (30-40%) and dimmed, never black.",
    "The creator's expression and gesture match the message (sceptical + raised hand for a warning, shock for a "
    "top-deck); it may be intensified, never changed into another person.",
    "Graphic cues are welcome when they carry meaning: arrows pointing at the key card, a big red X on a bad card, a "
    "green check on a good one, a glow or aura behind the card. No decoration without meaning, no collage.",
    "Strong colour contrast: red/orange against white/yellow, or the accent colour on the key word; dark or blurred "
    "context so the protagonists pop.",
    "Truth: no invented cards, card texts, results, numbers or quotes; the warning or verdict must be what the "
    "creator really says in the video.",
)

# Lettering descriptions for the image model (the user found the previous styles unattractive). Free Google
# Fonts (SIL OFL) named as a look, so the image model draws that style; nothing is rendered locally.
TEXT_STYLES = (
    "heavy condensed all-caps sans in the style of Anton, white with a thick black outline and a hard drop shadow",
    "ultra-bold geometric sans in the style of Montserrat Black, white-to-yellow gradient fill, thick red outline "
    "and an outer glow",
    "tall condensed all-caps in the style of Bebas Neue, the key word in the accent colour, the rest white, thick "
    "dark outline",
    "chunky rounded comic display in the style of Luckiest Guy, yellow fill, thick black outline, slight 3D "
    "extrusion",
    "bold italic impact-style sans, white on a tilted solid red banner with a dark shadow",
    "heavy all-caps in the style of Anton with a two-tone split (top word white, bottom word yellow), black outline",
)

CRITIQUE_CHECKLIST = (
    ("idea", "one clear, clickable idea that matches the video (a warning, verdict, contrast...), not abstract"),
    ("text", "2-4 words, correct spelling, huge, readable at 168x94, never covering the face or the key card"),
    ("hierarchy", "face -> text -> object is obvious; at most 3 strong elements; no clutter"),
    ("object", "the key card/moment is big, sharp, the SAME real card (name, art, text, numbers intact)"),
    ("face", "the creator is big (>= 1/3 of the height), sharp, the same person, expression matching the message"),
    ("context", "the background is recognisable (tier list / board) but subordinate and not too dark"),
    ("finish", "professional thumbnail finish: contrast, depth, clean edges, no smeared patches or fake UI"),
    ("title_fit", "cover and title complement each other (no repeated words, together they tell the promise)"),
)


def ideation_lines(*, n_concepts: int, brief: str = "", brief_images: Sequence[str] = ()) -> list[str]:
    """Planning instructions that replace the forced-diversity portfolio of the gen engine."""
    ideas = 2 if n_concepts >= 3 else 1
    lines = [
        f"THUMBNAIL PLAYBOOK {PLAYBOOK_VERSION} - think like the creator's packaging strategist BEFORE any concept.",
        "A. EDITORIAL UNDERSTANDING - what really makes this video worth watching; confirm, merge, reframe or reject "
        "the opportunities below.",
        "B. IDEATION (field 'ideation', fill it first): video_type (one of " + ", ".join(VIDEO_TYPES) + "); promise "
        "= what the viewer gets, in one sentence; audience; emotion to use; then 8-12 RAW hook candidates, each with "
        "family (" + ", ".join(IDEA_FAMILIES) + "), the 2-4 word hook, the visual (what is big on screen) and why a "
        f"viewer of THIS niche would click. Choose the {ideas} strongest (chosen: idea_id + why it wins) and write why "
        "the others lose (rejected_note). Prefer the families that fit the video type:",
    ]
    lines += [f"  - {name}: {rule}" for name, rule in VIDEO_TYPES.items()]
    lines += [f"  - family {name}: {rule}" for name, rule in IDEA_FAMILIES.items()]
    lines += [
        f"C. PORTFOLIO - exactly {n_concepts} concepts as VARIANTS of the chosen ideas"
        + (f" ({n_concepts // ideas} per idea, the best idea first; an odd one goes to the best idea)" if ideas > 1
           else "") + ". Each concept names its idea_id. Variants of the same idea differ in composition, "
        "protagonist size/side, expression or wording of the hook (A/B test material); different ideas differ in "
        "message. portfolio_strategy explains the plan.",
        "D. ART DIRECTION per concept - follow the principles:",
    ]
    lines += [f"  - {rule}" for rule in PRINCIPLES]
    lines += [
        "  - expression: the creator's expression and gesture you want (e.g. 'sceptical, one eyebrow up, hand "
        "raised in a stop gesture'); pick the webcam crop closest to it in package.face.",
        "  - card_name: the exact name of the ONE card to show big, when the idea needs a specific card: a name you "
        "read in a listed frame (also add that reading to card_observations, with its box), that the creator says, or "
        "that the creator brief asks for. ReCut fetches its official image in the audience's language (so an English "
        "name read on a website becomes the Italian card); leave empty when no single card is the point.",
    ]
    if brief or brief_images:
        lines += ["", "CREATOR BRIEF (the creator's own idea - highest priority, but never a reason to invent facts):"]
        if brief:
            lines.append(f"  \"{brief}\"")
        for n, description in enumerate(brief_images, start=1):
            lines.append(f"  - creator image U{n}: {description}")
        lines += [
            "  If the brief describes a specific cover, concept C1 implements it faithfully (it is one of the chosen "
            "ideas); the other concepts are your strongest alternatives or variants. If it is only a hint (a "
            "card, a tone, a word), use it in the chosen ideas. Creator images are referenced in concept "
            "creator_images (U1..) when a cover must include them (e.g. 'this card'); they are creative inputs, not "
            "evidence of what happens in the video.",
        ]
    return lines


def ideation_schema() -> dict[str, Any]:
    string = {"type": "string"}
    candidate = {"type": "object", "additionalProperties": False,
                 "required": ["idea_id", "family", "hook", "visual", "why"],
                 "properties": {"idea_id": string, "family": {"type": "string", "enum": list(IDEA_FAMILIES)},
                                "hook": string, "visual": string, "why": string}}
    chosen = {"type": "object", "additionalProperties": False, "required": ["idea_id", "why_it_wins"],
              "properties": {"idea_id": string, "why_it_wins": string}}
    return {"type": "object", "additionalProperties": False,
            "required": ["video_type", "promise", "audience", "emotion", "candidates", "chosen", "rejected_note"],
            "properties": {"video_type": {"type": "string", "enum": list(VIDEO_TYPES)}, "promise": string,
                           "audience": string, "emotion": string,
                           "candidates": {"type": "array", "items": candidate},
                           "chosen": {"type": "array", "items": chosen}, "rejected_note": string}}


def check_ideation(raw: Mapping[str, Any], *, n_concepts: int, creator_brief: bool = False) -> list[str]:
    """Ideation present and used: candidates, chosen ideas among them, every concept a variant of a chosen idea.

    creator_brief: the creator gave an idea; it decides the cover, so the 'one specific card' rule is not imposed."""
    problems: list[str] = []
    ideation = raw.get("ideation")
    if not isinstance(ideation, Mapping):
        return ["ideation: missing (fill video_type, promise, candidates, chosen, rejected_note)"]
    candidates = [c for c in ideation.get("candidates") or [] if isinstance(c, Mapping)]
    ids = [str(c.get("idea_id")) for c in candidates]
    if len(candidates) < 6:
        problems.append(f"ideation: {len(candidates)} hook candidates; write 8-12 before choosing")
    if len(set(ids)) != len(ids):
        problems.append("ideation: idea_id values must be unique")
    chosen = [str(c.get("idea_id")) for c in ideation.get("chosen") or [] if isinstance(c, Mapping)]
    want = 2 if n_concepts >= 3 else 1
    if len(set(chosen)) != want:
        problems.append(f"ideation: choose exactly {want} ideas")
    unknown = [c for c in chosen if c not in ids]
    if unknown:
        problems.append(f"ideation: chosen {unknown} are not candidates")
    if ideation.get("video_type") == "tier_list_or_guide" and not creator_brief and not any(
            str(c.get("card_name") or "").strip() or c.get("creator_images") for c in raw.get("concepts") or []):
        problems.append("tier list / guide: at least one chosen idea must be about ONE specific card (card_name), "
                        "the worst or most surprising one, with the creator's verdict on it")
    used = [str(c.get("idea_id")) for c in raw.get("concepts") or []]
    stray = sorted({u for u in used if u not in chosen})
    if stray:
        problems.append(f"concepts use idea_id {stray} that is not a chosen idea")
    if chosen and set(chosen) - set(used):
        problems.append(f"chosen ideas {sorted(set(chosen) - set(used))} have no concept")
    return problems


def critique_prompt(*, concepts: Sequence[Mapping[str, Any]], language: str, ideation: Mapping[str, Any] | None,
                    brief: str = "") -> str:
    lines = [f"PROMPT_VERSION: thumbnail_critique_v1 ({PLAYBOOK_VERSION})",
             "You are a senior YouTube packaging reviewer for a Hearthstone creator. The attached images are the "
             "generated thumbnail candidates (one per concept, in the order listed) and, last, all of them side by "
             "side at PHONE size. Judge them against this checklist (score 0-10 each):"]
    lines += [f"- {key}: {rule}" for key, rule in CRITIQUE_CHECKLIST]
    if ideation:
        lines.append(f"Video promise: {ideation.get('promise', '')} | emotion: {ideation.get('emotion', '')}")
    if brief:
        lines.append(f"Creator brief (must be respected): \"{brief}\"")
    lines.append("CANDIDATES:")
    for n, concept in enumerate(concepts, start=1):
        lines.append(f"- image {n} = {concept.get('concept_id')}: hook \"{concept.get('thumbnail_text', '')}\" | title "
                     f"\"{concept.get('title', '')}\" | idea {concept.get('idea_id', '-')} | card "
                     f"{concept.get('card_name') or '-'}")
    lines += ["Then rank them (best first) and pick ONE to revise: the most promising, not necessarily the most "
              "finished. For it give 3-6 concrete, visual edit instructions that fix its real weaknesses (what to "
              "enlarge, move, remove, make readable, which expression) while keeping its idea; optionally a better "
              f"hook (2-4 words, {language}, true to the video) or an empty string to keep it. Be specific and honest; "
              "never ask for invented cards, texts or results.",
              "Answer with the JSON object only."]
    return "\n".join(lines)


CRITIQUE_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": ["reviews", "ranking", "revise", "summary"],
    "properties": {
        "reviews": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["concept_id", "scores", "issues", "keep"],
            "properties": {"concept_id": {"type": "string"},
                           "scores": {"type": "object", "additionalProperties": False,
                                      "required": [k for k, _ in CRITIQUE_CHECKLIST],
                                      "properties": {k: {"type": "integer"} for k, _ in CRITIQUE_CHECKLIST}},
                           "issues": {"type": "array", "items": {"type": "string"}},
                           "keep": {"type": "array", "items": {"type": "string"}}}}},
        "ranking": {"type": "array", "items": {"type": "string"}},
        "revise": {"type": "object", "additionalProperties": False, "required": ["concept_id", "instructions", "hook"],
                   "properties": {"concept_id": {"type": "string"},
                                  "instructions": {"type": "array", "items": {"type": "string"}},
                                  "hook": {"type": "string"}}},
        "summary": {"type": "string"}}}


def check_critique(raw: Any, concept_ids: Sequence[str]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("critique must be a JSON object")
    ids = list(concept_ids)
    problems = []
    if sorted(r.get("concept_id") for r in raw.get("reviews") or []) != sorted(ids):
        problems.append(f"reviews: one per concept {ids}")
    if sorted(raw.get("ranking") or []) != sorted(ids):
        problems.append(f"ranking: a permutation of {ids}")
    revise = raw.get("revise") or {}
    if revise.get("concept_id") not in ids:
        problems.append("revise.concept_id must be one of the concepts")
    if not 1 <= len(revise.get("instructions") or []) <= 8:
        problems.append("revise.instructions: 1-8 concrete edits")
    hook = " ".join(str(revise.get("hook") or "").split())
    if hook and len(hook.split()) > 5:
        problems.append("revise.hook: 2-4 words or empty")
    for review in raw.get("reviews") or []:
        for key, value in (review.get("scores") or {}).items():
            if not isinstance(value, int) or not 0 <= value <= 10:
                problems.append(f"{review.get('concept_id')}: score {key} must be 0-10")
    if problems:
        raise ValueError("; ".join(problems[:10]))
    return raw
