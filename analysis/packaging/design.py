"""V4 design specifications connecting existing planning, generation and editing."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping
import json

from .direction import CreativeDirection

VERSION = "thumbnail_design_v4"
LAYOUTS = ("reaction_impact", "card_power", "clutch_moment", "big_statement")
TEXT_BOXES = {"left": [.05, .12, .42, .62], "right": [.53, .12, .42, .62],
              "top": [.06, .06, .88, .28], "bottom": [.06, .59, .70, .31]}
ARCHETYPES = ("card_led", "board_led", "face_board", "face_card", "result_payoff")
SUBJECTS = ("card", "face", "gameplay_board", "result_screen", "choice_screen", "combo_moment")
BACKGROUND_STRATEGIES = ("real_gameplay_derived", "board_full_focus", "gameplay_crop_extended",
                        "gameplay_blurred_depth", "card_focus_with_gameplay_backdrop",
                        "face_plus_gameplay_backdrop", "result_screen_emphasis", "abstract_environment")
BANNED_PATTERNS = ("tiny_center_screenshot", "generic_abstract_background", "raw_webcam_box",
                   "too_many_small_elements", "weak_subject_hierarchy", "low_hearthstone_identity")
_IDS = {"type": "array", "items": {"type": "string"}}
DESIGN_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "story_angle": {"type": "string"}, "evidence_ids": _IDS,
        "story_confidence": {"type": "string", "enum": ["supported", "interpretation", "uncertain"]},
        "main_subject": {"type": "string"}, "secondary_subject": {"type": "string"},
        "must_use_assets": _IDS, "optional_assets": _IDS, "must_not_use_assets": _IDS,
        "layout_archetype": {"type": "string", "enum": list(LAYOUTS)},
        "visual_hierarchy": {**_IDS, "description": "Ordered asset IDs from this concept's package only. "
                             "Never write labels such as text, hook, face, card or background here; "
                             "typography and subject kinds have separate fields. Observation IDs O.. are allowed "
                             "only when declared in card_observations."},
        "text_position": {"type": "string", "enum": list(TEXT_BOXES)},
        "typography_mode": {"type": "string", "enum": ["auto", "precision", "ai", "none"]},
        "typography_preset": {"type": "string"}, "color_preset": {"type": "string"},
        "lighting_style": {"type": "string"}, "emotion_or_tone": {"type": "string"},
        "effect_intensity": {"type": "integer", "minimum": 0, "maximum": 5},
        "composition_notes": {"type": "string"}, "why_clickable": {"type": "string"},
    },
}
DESIGN_SCHEMA["required"] = list(DESIGN_SCHEMA["properties"])
# Older saved V4 designs remain valid; fresh planning requests require the concrete vocabulary.
LEGACY_REQUIRED = list(DESIGN_SCHEMA["required"])
DESIGN_SCHEMA["properties"].update({
    "thumbnail_archetype": {"type": "string", "enum": list(ARCHETYPES)},
    "dominant_subject": {"type": "string", "enum": list(SUBJECTS)},
    "secondary_subject_kind": {"type": "string", "enum": ["", "text", *SUBJECTS]},
    "optional_subject": {"type": "string", "enum": ["", *SUBJECTS]},
    "background_strategy": {"type": "string", "enum": list(BACKGROUND_STRATEGIES)},
    "gameplay_frame_role": {"type": "string"},
    "card_usage": {"type": "string", "enum": ["none", "hero_card_big", "compare_two_cards", "card_plus_board", "card_plus_face"]},
    "face_usage": {"type": "string", "enum": ["none", "support", "primary_reaction"]},
    "board_visibility": {"type": "string", "enum": ["full_focus", "recognizable_backdrop"]},
    "text_need": {"type": "string", "enum": ["none", "optional", "essential"]},
    "hook_style": {"type": "string", "enum": ["none", "question", "payoff", "warning", "surprise", "analysis"]},
    "composition_template": {"type": "string"},
    "clutter_budget": {"type": "integer", "minimum": 1, "maximum": 3},
    "banned_patterns": _IDS,
})
DESIGN_SCHEMA["required"] = list(DESIGN_SCHEMA["properties"])


def planner_schema(base: dict[str, Any]) -> dict[str, Any]:
    schema = deepcopy(base)
    concept = schema["properties"]["concepts"]["items"]
    concept["properties"]["design"] = deepcopy(DESIGN_SCHEMA)
    concept["required"].append("design")
    return schema


def planning_rules() -> str:
    return (
        "HEARTHSTONE CREATIVE DIRECTOR: return a design object per concept following the schema. "
        "Choose thumbnail_archetype first: card_led, board_led, face_board, face_card or result_payoff. "
        "The default background_strategy is real_gameplay_derived: a selected real Hearthstone board fills "
        "the canvas, never a tiny inset on an empty abstract environment. Other gameplay-derived strategies "
        "may blur/dim the board moderately, but retain recognizable Hearthstone structure. "
        "abstract_environment is an explicit exceptional choice, strongly penalized, never a default. "
        "Pick dominant_subject, secondary_subject_kind and optional_subject before layout. "
        "Use at most two strong subjects, one authentic large card or large isolated creator face. "
        "A card-led card should fill about 80% of canvas height; a primary reaction about 30-45% of width. "
        "Prefer four different supported archetypes, including card_led and board_led/result_payoff; "
        "at most two with a face, at least one clean and faceless. When evidence is insufficient, "
        "substitute real board-led concepts and report the diversity limit; never invent cards or payoff. "
        "Banned: tiny_center_screenshot, generic_abstract_background, raw_webcam_box, too_many_small_elements, "
        "weak_subject_hierarchy, low_hearthstone_identity. Text occupies a compact safe corner/band, "
        "not half the canvas at the expense of gameplay. "
        "Design four editorially distinct approaches: change story angle, protagonist and visual hierarchy, "
        "not just colours or face position. Choose only roles supported by actual assets; "
        "you need not force one of every role. Keep the ASSET PACKAGE authoritative: main_subject and every "
        "must_use_asset must be in that package; optional assets may be available alternatives; forbidden assets "
        "must not be selected. Evidence IDs must come from the supplied highlights, transcript or assets. "
        "visual_hierarchy is an ordered list of actual selected asset IDs, NOT subject labels, hook text, "
        "layer names or descriptive phrases. Do not invent K/S IDs before those assets exist. "
        "Use reaction_impact, card_power, clutch_moment or big_statement as starting layouts, adapt to the story. "
        "Do not enlarge cards marked secondary/avoid; use readable real context instead. "
        "Card observation boxes must cover the whole readable card, with width and height at least 0.05 "
        "of the reference frame, entirely inside it. Do not report only a title/statistics strip. "
        "If unreadable, do not make that observation the protagonist or name it in a factual claim; "
        "select a catalogued board/frame or creator instead. "
        "Use precision for ordinary short lettering, ai only when the lettering is integral to the artwork. "
        "Reserve the selected text_position as negative space; place the person/card elsewhere. "
        "Do not infer a combo from keyword overlap, or identify the player's deck from an opponent. "
        "Record uncertainty. A style/layout reference is never gameplay evidence. "
        "Title and thumbnail text should complement each other unless a literal user lock requires otherwise. "
        "Hooks are normally 2-5 words (six at most unless locked): vary question, surprise, decision, contrast or consequence, or omit text. "
        "Do not summarize the title, reuse a stock phrase, or invent a result/statistic. "
        "Consider Card Dominance, Reaction Impact, Gameplay Clutch and Big Statement only when assets support them. "
        "One dominant subject, at most one supporting subject and an optional hook; keep backgrounds subordinate. "
        "Use typography_preset IDs white_impact, gold_impact, danger_red or energy_cyan and vary motivated choices. "
        "Use color_preset white_dark, yellow_dark, red_dark or cyan_dark. Fonts are a separate preference. "
        "Reference-preserving strategy: real gameplay supplies the full background; cards/faces are inserted locally. "
        "AI supplies only restrained lighting accents around that real frame, never replaces the board. "
        "Never require a tiny/unreadable card or synthetic face to complete a family."
    )


def prepare_design_hierarchies(raw: dict[str, Any], assets: Mapping[str, Any]) -> dict[str, Any]:
    """Recover advisory layout ordering from authoritative selections, never fabricate an asset.

    The strict validator still checks all required/optional/forbidden references,
    main subjects and evidence. Invalid hierarchy entries are recorded, not accepted
    as assets. This can avoid a paid repair call for a non-factual layout mistake.
    """
    from .scene import scene_assets
    out = deepcopy(raw)
    diagnostics = list(out.get("_design_repairs") or [])
    for concept in out.get("concepts") or []:
        d = concept.get("design")
        if not isinstance(d, dict):
            continue
        hierarchy = d.get("visual_hierarchy")
        if not isinstance(hierarchy, list) or any(not isinstance(a, str) for a in hierarchy):
            continue  # malformed structures remain validation errors
        package = concept.get("package")
        if isinstance(package, dict):
            selected = [a for values in package.values() if isinstance(values, list) for a in values
                        if isinstance(a, str) and a in assets]
        else:
            selected = [a for a in scene_assets(concept.get("scene") or {}) if a in assets]
        main = d.get("main_subject")
        if not isinstance(main, str) or main not in selected:
            continue  # an invalid protagonist is not recoverable by changing layout
        removed = [a for a in hierarchy if a not in selected]
        if not removed:
            continue
        corrected = list(dict.fromkeys([main, *[a for a in hierarchy if a in selected], *selected]))
        d["visual_hierarchy"] = corrected
        repair = {"concept_id": concept.get("concept_id"), "field": "visual_hierarchy",
                  "original": hierarchy, "removed": removed, "replacement": corrected,
                  "reason": "layout_order_rebuilt_from_selected_assets", "new_evidence_created": False}
        diagnostics.append(repair)
        concept.setdefault("planning_repairs", []).append(repair)
    if diagnostics:
        out["_design_repairs"] = diagnostics
    return out


def validate_design(concept: Mapping[str, Any], assets: Mapping[str, Any], evidence_ids: set[str]) -> None:
    """Validate supplied V4 fields; absence is allowed for legacy regeneration."""
    d = concept.get("design")
    if d is None:
        return
    if not isinstance(d, dict) or set(LEGACY_REQUIRED) - set(d):
        raise ValueError("Incomplete V4 concept design")
    for key in DESIGN_SCHEMA["properties"]:
        rule = DESIGN_SCHEMA["properties"][key]
        if key in d and "enum" in rule and d[key] not in rule["enum"]:
            raise ValueError(f"Invalid V4 design {key}")
    if "clutter_budget" in d and (not isinstance(d["clutter_budget"], int) or not 1 <= d["clutter_budget"] <= 3):
        raise ValueError("Invalid clutter budget")
    if "banned_patterns" in d and (not isinstance(d["banned_patterns"], list) or
                                   any(p not in BANNED_PATTERNS for p in d["banned_patterns"])):
        raise ValueError("Unknown banned pattern")
    from .scene import scene_assets
    selected = ({a for ids in concept["package"].values() for a in ids}
                if concept.get("package") else set(scene_assets(concept.get("scene") or {})))
    for key in ("must_use_assets", "optional_assets", "must_not_use_assets", "visual_hierarchy"):
        if not isinstance(d[key], list):
            raise ValueError(f"{concept.get('concept_id', '?')}: V4 {key} must be a list of asset IDs")
        unknown = [a for a in d[key] if not isinstance(a, str) or a not in assets]
        if unknown:
            raise ValueError(f"{concept.get('concept_id', '?')}: Unknown V4 asset in {key}: {unknown!r}")
    for key in ("main_subject", "secondary_subject"):
        if d[key] and d[key] not in assets:
            raise ValueError(f"Unknown V4 {key}")
    if d["main_subject"] not in selected or not set(d["must_use_assets"]) <= selected:
        raise ValueError("V4 required assets must be present in the package")
    if set(d["must_not_use_assets"]) & selected:
        raise ValueError("V4 package uses a forbidden asset")
    if any(e not in evidence_ids for e in d["evidence_ids"]):
        raise ValueError("Unknown V4 evidence ID")
    if d["layout_archetype"] not in LAYOUTS or d["text_position"] not in TEXT_BOXES:
        raise ValueError("Unknown V4 layout or text position")
    if d["typography_mode"] not in ("auto", "precision", "ai", "none"):
        raise ValueError("Invalid typography mode")
    if d["story_confidence"] not in ("supported", "interpretation", "uncertain"):
        raise ValueError("Invalid story confidence")
    if not isinstance(d["effect_intensity"], int) or not 0 <= d["effect_intensity"] <= 5:
        raise ValueError("Effect intensity must be 0–5")


def design_pack(concept: Mapping[str, Any], direction: CreativeDirection, profile: Mapping[str, Any],
                assets: Mapping[str, Any], sources: list[str], source_ids: Mapping[int, str] | None = None) -> dict[str, Any]:
    d = dict(concept.get("design") or {})
    package = concept.get("package") or {}
    selected = list(dict.fromkeys(a for ids in package.values() for a in ids))
    unknown = set(selected) - assets.keys()
    if unknown:
        raise ValueError(f"Design Pack has unknown assets: {sorted(unknown)}")
    import re
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(concept["concept_id"])):
        raise ValueError("Unsafe concept ID")
    active = direction.active()
    hook = active.thumbnail_hook if active.hook_locked else str(concept.get("thumbnail_text") or "")
    mode = active.typography_mode if active.typography_mode != "auto" else d.get("typography_mode", "auto")
    if not hook:
        mode = "none"
    elif active.hook_locked or mode == "auto":
        mode = "precision"
    system = deepcopy(profile.get("design_system") or {})
    position = d.get("text_position", "top")
    preferred = d.get("typography_preset")
    preferred_fonts = system.get("fonts", ["Anton", "Impact"])
    matched_font = next((f for f in preferred_fonts if str(f).casefold() in str(preferred or "").casefold()), None)
    if matched_font:
        preferred_fonts = [matched_font] + [f for f in preferred_fonts if f != matched_font]
    typography = {**system.get("text", {}), "text": hook, "locked": active.hook_locked,
                  "mode": mode, "fonts": preferred_fonts,
                  "box": TEXT_BOXES[position], "position": position}
    color_preset = d.get("color_preset", "yellow_dark")
    color = {"yellow_dark": "#FFD436", "white_dark": "#FFFFFF",
             "cyan_dark": "#27C9FF", "red_dark": "#FF514A"}.get(color_preset)
    if color:
        typography["color"] = color
    from .typography import apply_preset, resolve_preset
    typography = apply_preset(typography, resolve_preset(str(preferred or ""), str(color_preset)))
    geometry = {
        "left": {"main": [.55, .10, .40, .80], "secondary": [.56, .55, .35, .35]},
        "right": {"main": [.05, .10, .40, .80], "secondary": [.08, .55, .35, .35]},
        "top": {"main": [.56, .36, .38, .60], "secondary": [.05, .36, .48, .58]},
        "bottom": {"main": [.20, .06, .65, .48], "secondary": [.06, .08, .25, .44]},
    }[position]
    refs = []
    for aid in selected:
        a = assets[aid]
        index = int(a.get("source_index", 0))
        refs.append({"asset_id": aid, "path": a.get("path"), "type": a.get("type"),
                     "source_index": index, "source_id": (source_ids or {}).get(index, f"source-{index}"),
                     "source_name": Path(sources[index]).name if 0 <= index < len(sources) else "",
                     "source_time": a.get("source_time"), "edited_time": a.get("edited_time"),
                     "game_id": a.get("game_id"), "provenance": a.get("provenance", {}),
                     "limitations": a.get("limitations", [])})
    role = concept.get("role", "narrative-led")
    layout = d.get("layout_archetype") or dict(zip(
        ("reaction-led", "card-led", "board-led", "narrative-led"), LAYOUTS)).get(role, "big_statement")
    return {
        "format": VERSION, "version": 4, "concept_id": concept["concept_id"],
        "generation_strategy": active.generation_strategy,
        "thumbnail_archetype": d.get("thumbnail_archetype") or {"card-led": "card_led", "reaction-led": "face_board"}.get(role, "board_led"),
        "dominant_subject": d.get("dominant_subject") or {"card-led": "card", "reaction-led": "face"}.get(role, "gameplay_board"),
        "secondary_subject_kind": d.get("secondary_subject_kind", ""), "optional_subject": d.get("optional_subject", ""),
        "background_strategy": d.get("background_strategy", "real_gameplay_derived"),
        "gameplay_frame_role": d.get("gameplay_frame_role", "full_canvas_authentic_backdrop"),
        "card_usage": d.get("card_usage", "hero_card_big" if role == "card-led" else "none"),
        "face_usage": d.get("face_usage", "primary_reaction" if role == "reaction-led" else "none"),
        "board_visibility": d.get("board_visibility", "full_focus" if role == "board-led" else "recognizable_backdrop"),
        "text_need": d.get("text_need", "optional" if hook else "none"),
        "hook_style": d.get("hook_style", "question" if "?" in hook else "analysis" if hook else "none"),
        "composition_template": d.get("composition_template", "full_board_compact_hook"),
        "clutter_budget": d.get("clutter_budget", 2), "banned_patterns": list(BANNED_PATTERNS),
        "direction": direction.to_mapping(), "style_profile": deepcopy(dict(profile)),
        "planning_repairs": deepcopy(concept.get("planning_repairs", [])),
        "story_angle": d.get("story_angle") or concept.get("narrative_angle") or concept.get("moment"),
        "story_confidence": d.get("story_confidence", "interpretation"), "evidence": concept.get("evidence", []),
        "evidence_ids": d.get("evidence_ids", concept.get("highlight_ids", [])),
        "main_subject": d.get("main_subject") or concept.get("primary_subject") or selected[0],
        "secondary_subject": d.get("secondary_subject", ""),
        "must_use_assets": d.get("must_use_assets", selected),
        "optional_assets": d.get("optional_assets", []), "must_not_use_assets": d.get("must_not_use_assets", []),
        "visual_hierarchy": d.get("visual_hierarchy", selected),
        "layout": layout, "composition_notes": d.get("composition_notes") or concept.get("art_direction", ""),
        "lighting": d.get("lighting_style", "integrated rim light; deep controlled shadows"),
        "effects": {**system.get("effects", {}), "concept_intensity": d.get("effect_intensity", 3)},
        "tone": d.get("emotion_or_tone") or concept.get("mood", ""),
        "why_clickable": d.get("why_clickable") or concept.get("message", ""),
        "palette": {**system.get("palette", {}), "concept_preset": color_preset},
        "geometry": geometry, "typography": typography, "references": refs,
        "creative_references": deepcopy(active.reference_images),
        "output_size": [1920, 1080], "safe_margin": .05,
        "preserve": ["creator identity", "real card details", "player class", "verified result"],
        "avoid": system.get("avoid", []),
        "semantic_check": "not certified; creator must review identity, cards and story",
    }


def pack_prompt(pack: Mapping[str, Any]) -> str:
    if pack.get("composition", {}).get("strategy") == "reference_preserving":
        return environment_prompt(pack)
    t = pack["typography"]
    typography = (
        f"NO TEXT, LETTERS OR PLACEHOLDERS. Reserve box {t['box']} for local exact typography; "
        "keep faces/cards outside it."
        if t["mode"] == "precision" else
        "No headline or decorative letters." if t["mode"] == "none" else
        f"Render this exact text: {json.dumps(t['text'], ensure_ascii=False)} at {t['position']}; "
        f"font inspiration {t['fonts']} (exact font not guaranteed)."
    )
    return "\n".join([
        "Mission: Design a finished 16:9 Hearthstone Competitive Gaming thumbnail. Request native landscape "
        "1920x1080 or the best available native 16:9 size. Keep essential content inside 5% safe margins.",
        f"Story: {pack['story_angle']} | evidence level: {pack['story_confidence']}. No additional factual promises.",
        "Asset Roles: use the indexed real references for identity/gameplay; creative references carry only their "
        "declared style/layout/element role, never evidence of gameplay.",
        f"Composition: {pack['layout']}; dominant={pack['main_subject']}; secondary={pack['secondary_subject']}; "
        f"Hearthstone archetype={pack.get('thumbnail_archetype', 'board_led')}; "
        f"background={pack.get('background_strategy', 'real_gameplay_derived')}: the selected REAL gameplay "
        "fills the canvas, not a small pasted screenshot on a generic poster. "
        f"hierarchy={pack['visual_hierarchy']}; relative boxes [x,y,w,h]={pack.get('geometry', {})}; "
        f"{pack['composition_notes']}. One dominant idea, 2–3 strong elements. "
        "Reaction face width approximately 30–45% only when pertinent; not every variant needs a face.",
        "Identity: SAME real person; preserve glasses, hair, face and natural expression. Integrate lighting, "
        "clean cut-out, shadow and depth; no rectangular webcam.",
        "Authenticity: never invent/alter cards, names, numbers, heroes, class or result. Screenshots are evidence, "
        "not rectangular stickers to paste. Uncertain details cannot be promoted to facts.",
        f"Visual Style: palette={pack['palette']}; lighting={pack['lighting']}; effects={pack['effects']}. "
        "At most two dominant saturated colours; selective effects around the protagonist.",
        f"Typography: {typography}",
        "Visual Quality: clear separation, controlled contrast, clean edges, meaningful negative space, mobile readability.",
        f"Negative Constraints: {pack['avoid']}; no tiny screenshot sticker, random collage, duplicated subjects, "
        "invented game objects, excessive glow, tiny text, useless HUD or clipped important elements.",
    ])


def environment_prompt(pack: Mapping[str, Any]) -> str:
    """Only generate the layer actually assigned to AI; protected subjects never enter this image."""
    comp = pack["composition"]
    t = pack["typography"]
    if comp.get("background"):
        return "\n".join([
            "Mission: enhance the attached REAL Hearthstone gameplay backdrop in native 16:9 1920x1080. "
            "This is not an abstract poster or a mockup. The actual frame remains the full-canvas local base.",
            f"Story: {pack['story_angle']}; evidence={pack['story_confidence']}. Invent no additional event.",
            f"Gameplay reference: {comp['background']['asset_id']}; strategy={pack['background_strategy']}. "
            "Preserve recognizable board structure, do not shrink it into a screenshot sticker.",
            "Protected content: authentic cards and creator cutouts are composited AFTER generation. "
            "Do not draw people, faces, foreground cards, synthetic substitutes or duplicate subjects.",
            "Identity: SAME real person retained through original local pixels. "
            "Authenticity: real cards and board retain source content; creative references are not factual evidence.",
            f"Composition: {pack['thumbnail_archetype']}; dominant={pack['dominant_subject']}; "
            f"template={pack['composition_template']}; reserved foreground boxes={comp['reserved_boxes']}. "
            "Keep those areas subdued; no borders, placeholders or miniature insets.",
            f"Generated layer: {comp['environment_recipe']}. Only restrained lighting, external glow and depth; "
            "local compositing uses only low-strength luminance accents, never generated factual pixels.",
            f"Typography: NO TEXT, LETTERS OR PLACEHOLDERS. Reserve compact text area {t['box']}; applied locally.",
            f"Negative Constraints: {pack['banned_patterns']}; empty generic gradients, scattered small subjects, clutter. "
            "One dominant idea and at most one strong support. Creative references are not gameplay proof.",
        ])
    text = (f"Integrate artistic lettering {json.dumps(t['text'], ensure_ascii=False)} inside {t['box']}."
            if t["mode"] == "ai" else f"NO TEXT, LETTERS OR PLACEHOLDERS. Keep text box {t['box']} clear.")
    return "\n".join([
        "Mission V4.1: generate ONLY a 16:9 1920x1080 environment layer for a YouTube gaming composition.",
        f"Story inspiration: {pack['story_angle']}. Do not depict or claim additional events.",
        "Protected content: authentic Hearthstone cards, gameplay and creator photographs are composited locally "
        "AFTER this generation. Do not draw people, faces, cards, heroes, minions, card-shaped placeholders or HUD.",
        "Identity: SAME real person is retained through original local pixels, not reconstructed. "
        "Authenticity: factual content is supplied by the protected layers, never by invented environment details.",
        f"Composition: {comp['family']}; reserved subject boxes [x,y,w,h]={comp['reserved_boxes']}. "
        "Keep these areas quiet and unobstructed, never fill them with substitute subjects. "
        "Build depth and directional light AROUND them; no visible reservation borders.",
        f"Generated environment: {comp['environment_recipe']}; accent={t.get('color')}; "
        "one coherent subdued backdrop, controlled contrast and light, no decorative clutter.",
        f"Typography: {text}",
        "Style: energetic Competitive Gaming, clean hierarchy; subtle external glow only when motivated. "
        "No automatic explosion/particle/ray stack. Creative reference images are inspiration, not gameplay proof.",
        "Negative Constraints: no duplicate subjects or busy small details behind the reserved areas.",
    ])
