"""ThumbnailScene: a layered, validated scene graph for one thumbnail (V3 engine).

The agent (or a preset, or the V2 adapter) describes WHAT to draw as data; the renderer
decides HOW. A scene only references asset ids from the VisualAssetManifest; values have
hard ranges; nothing in a scene is executable. Canvas and safe areas are owned by ReCut.

Layer types:
  image          any visual asset, plain (rarely needed; prefer the typed layers)
  subject_cutout a webcam_subject asset (RGBA cut-out of the creator)
  card           a card_crop / card_artwork asset, presented as an object
  board_crop     a gameplay_frame or board_region asset, cropped and placed
  text           the thumbnail headline (at most 2 text layers)
  gradient       darkening / colour gradient from a side or radial
  shape          rect / ellipse / bar behind text or subjects (sparingly)
  effect         vignette or spotlight on the focal point

Coordinates are normalized to the 1280x720 canvas: x, y = centre; height = fraction of
canvas height; width (optional) = fraction of canvas width.
"""
from __future__ import annotations

import copy
import re
from typing import Any, Iterable, Mapping, Sequence

SCENE_VERSION = "thumbnail_scene_v1"
CANVAS = {"width": 1280, "height": 720}
SAFE_AREAS = {"margin": 0.05, "duration_badge": [0.80, 0.82, 0.20, 0.18]}  # x, y, w, h (normalized)
LAYER_TYPES = ("image", "subject_cutout", "card", "board_crop", "text", "gradient", "shape", "effect")
CLIPS = ("none", "rounded", "circle")
BLENDS = ("normal", "multiply", "screen", "overlay")
ALIGNS = ("left", "center", "right")
SIDES = ("left", "right", "top", "bottom", "radial", "none")
SHAPES = ("rect", "ellipse", "bar", "none")
EFFECTS = ("vignette", "spotlight")
TYPOGRAPHY = ("impact", "heavy", "condensed")
MAX_LAYERS = 8
MAX_TEXT_LAYERS = 2
# allowed asset types per layer type
ASSET_TYPES_FOR = {
    "image": ("gameplay_frame", "board_region", "background", "webcam_original", "webcam_subject", "card_crop",
              "card_artwork"),
    "subject_cutout": ("webcam_subject",),
    "card": ("card_crop", "card_artwork"),
    "board_crop": ("gameplay_frame", "board_region", "background"),
}
BACKGROUND_TYPES = ("gameplay_frame", "board_region", "background")
# numeric ranges: name -> (min, max, default)
RANGES: dict[str, tuple[float, float, float]] = {
    "x": (0.0, 1.0, 0.5), "y": (0.0, 1.0, 0.5), "height": (0.04, 1.6, 0.5), "width": (0.04, 1.0, 0.0),
    "z": (0, 20, 1), "rotation": (-12.0, 12.0, 0.0), "opacity": (0.2, 1.0, 1.0), "shadow": (0.0, 1.0, 0.0),
    "outline": (0.0, 1.0, 0.0), "rim_light": (0.0, 1.0, 0.0), "blur": (0.0, 1.0, 0.0),
    "brightness": (-0.3, 0.3, 0.0), "contrast": (0.7, 1.5, 1.0), "saturation": (0.5, 1.6, 1.0),
    "strength": (0.0, 1.0, 0.6),
}
BACKGROUND_RANGES = {"blur": (0.0, 1.0, 0.0), "dim": (0.0, 0.8, 0.0), "saturation": (0.5, 1.5, 1.05)}
_HEX = re.compile(r"#[0-9A-Fa-f]{6}")
_ID = re.compile(r"[A-Za-z0-9_-]{1,24}")

_NUM = {"type": "number"}
_NULL_STR = {"type": ["string", "null"]}
_BOX = {"type": ["array", "null"], "items": {"type": "number"},
        "description": "normalized [x, y, w, h] crop inside the referenced asset, or null"}


def _obj(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


LAYER_SCHEMA = _obj({
    "id": {"type": "string"},
    "type": {"type": "string", "enum": list(LAYER_TYPES)},
    "asset_id": _NULL_STR,
    "crop": _BOX,
    "x": _NUM, "y": _NUM, "height": _NUM, "width": {"type": ["number", "null"]},
    "z": {"type": "integer"}, "rotation": _NUM, "opacity": _NUM,
    "shadow": _NUM, "outline": _NUM, "rim_light": _NUM, "blur": _NUM,
    "clip": {"type": "string", "enum": list(CLIPS)},
    "blend": {"type": "string", "enum": list(BLENDS)},
    "brightness": _NUM, "contrast": _NUM, "saturation": _NUM,
    "text": _NULL_STR,
    "align": {"type": "string", "enum": list(ALIGNS)},
    "emphasis": _NULL_STR,
    "color": _NULL_STR,
    "side": {"type": "string", "enum": list(SIDES)},
    "strength": _NUM,
    "shape": {"type": "string", "enum": list(SHAPES)},
    "effect": {"type": ["string", "null"], "enum": list(EFFECTS) + [None]},
})

SCENE_SCHEMA = _obj({
    "background": _obj({"asset_id": {"type": "string"}, "crop": _BOX, "blur": _NUM, "dim": _NUM, "saturation": _NUM}),
    "layers": {"type": "array", "items": LAYER_SCHEMA},
    "focal_point": _obj({"x": _NUM, "y": _NUM}),
    "visual_hierarchy": {"type": "array", "items": {"type": "string"}},
    "style": _obj({"accent": {"type": "string"}, "text_color": {"type": "string"},
                   "typography": {"type": "string", "enum": list(TYPOGRAPHY)}}),
})


def _box_ok(box: Any) -> bool:
    if box is None:
        return True
    if not isinstance(box, list) or len(box) != 4:
        return False
    try:
        x, y, w, h = (float(v) for v in box)
    except (TypeError, ValueError):
        return False
    return 0.0 <= x < 1.0 and 0.0 <= y < 1.0 and w >= 0.03 and h >= 0.03 and x + w <= 1.001 and y + h <= 1.001


def default_layer(layer_type: str, layer_id: str, **values: Any) -> dict[str, Any]:
    """A complete layer with defaults (every schema field present)."""
    layer: dict[str, Any] = {"id": layer_id, "type": layer_type, "asset_id": None, "crop": None, "width": None,
                             "clip": "none", "blend": "normal", "text": None, "align": "left", "emphasis": None,
                             "color": None, "side": "none", "shape": "none", "effect": None}
    for name, (_, _, default) in RANGES.items():
        if name != "width":
            layer[name] = default
    layer["z"] = int(RANGES["z"][2])
    layer.update(values)
    return layer


def validate_scene(scene: Any, assets: Mapping[str, Mapping[str, Any]], *, where: str = "scene") -> list[str]:
    """Problems of an agent/preset scene (empty list = valid). `assets`: asset_id -> manifest entry."""
    problems: list[str] = []
    if not isinstance(scene, Mapping):
        return [f"{where} must be an object"]
    background = scene.get("background") or {}
    bid = str(background.get("asset_id"))
    if bid not in assets:
        problems.append(f"{where}.background: unknown asset {bid!r}")
    elif assets[bid].get("type") not in BACKGROUND_TYPES:
        problems.append(f"{where}.background: {bid} is a {assets[bid].get('type')}, not a gameplay frame/region")
    if not _box_ok(background.get("crop")):
        problems.append(f"{where}.background.crop must be null or a normalized [x,y,w,h]")
    for name, (lo, hi, _) in BACKGROUND_RANGES.items():
        value = background.get(name)
        if not isinstance(value, (int, float)) or not lo <= float(value) <= hi:
            problems.append(f"{where}.background.{name} must be within {lo}..{hi}")
    layers = scene.get("layers")
    if not isinstance(layers, list):
        return problems + [f"{where}.layers must be a list"]
    if len(layers) > MAX_LAYERS:
        problems.append(f"{where}: at most {MAX_LAYERS} layers (clear hierarchy, few elements)")
    ids = [layer.get("id") for layer in layers if isinstance(layer, Mapping)]
    if len(set(ids)) != len(ids):
        problems.append(f"{where}: layer ids must be unique")
    texts = [layer for layer in layers if isinstance(layer, Mapping) and layer.get("type") == "text"]
    if len(texts) > MAX_TEXT_LAYERS:
        problems.append(f"{where}: at most {MAX_TEXT_LAYERS} text layers")
    for layer in layers:
        if not isinstance(layer, Mapping):
            problems.append(f"{where}: every layer must be an object")
            continue
        lid = str(layer.get("id"))
        label = f"{where}.{lid}"
        if not _ID.fullmatch(lid):
            problems.append(f"{label}: id must be 1-24 letters, digits, _ or -")
        kind = layer.get("type")
        if kind not in LAYER_TYPES:
            problems.append(f"{label}: unknown type {kind!r}")
            continue
        for name, (lo, hi, _) in RANGES.items():
            value = layer.get(name)
            if name == "width" and value is None:
                continue
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not lo <= float(value) <= hi:
                problems.append(f"{label}.{name} must be within {lo}..{hi}")
        for name, allowed in (("clip", CLIPS), ("blend", BLENDS), ("align", ALIGNS), ("side", SIDES),
                              ("shape", SHAPES)):
            if layer.get(name) not in allowed:
                problems.append(f"{label}.{name} must be one of {allowed}")
        if layer.get("color") is not None and not _HEX.fullmatch(str(layer.get("color"))):
            problems.append(f"{label}.color must be #RRGGBB or null")
        if not _box_ok(layer.get("crop")):
            problems.append(f"{label}.crop must be null or a normalized [x,y,w,h]")
        if kind in ASSET_TYPES_FOR:
            aid = str(layer.get("asset_id"))
            if aid not in assets:
                problems.append(f"{label}: unknown asset {aid!r}")
            elif assets[aid].get("type") not in ASSET_TYPES_FOR[kind]:
                problems.append(f"{label}: a {kind} layer cannot use a {assets[aid].get('type')} asset ({aid})")
        elif layer.get("asset_id") not in (None, ""):
            problems.append(f"{label}: {kind} layers take no asset_id")
        if kind == "text":
            text = " ".join(str(layer.get("text") or "").split())
            if not text:
                problems.append(f"{label}: text layers need text (omit the layer for no text)")
            elif len(text.split()) > 6 or len(text) > 40:
                problems.append(f"{label}: thumbnail text must stay short (<= 6 words, <= 40 characters)")
            if layer.get("width") is None:
                problems.append(f"{label}: text layers need a width")
            emphasis = layer.get("emphasis")
            if emphasis and emphasis.strip(".,!?:;\"'").upper() not in {w.strip(".,!?:;\"'").upper() for w in text.split()}:
                problems.append(f"{label}: emphasis must be a word of its text")
        if kind == "effect" and layer.get("effect") not in EFFECTS:
            problems.append(f"{label}: effect must be one of {EFFECTS}")
        if kind == "shape" and layer.get("shape") == "none":
            problems.append(f"{label}: shape layers need a shape")
    focal = scene.get("focal_point") or {}
    if not all(isinstance(focal.get(k), (int, float)) and 0.0 <= float(focal[k]) <= 1.0 for k in ("x", "y")):
        problems.append(f"{where}.focal_point must have x, y within 0..1")
    hierarchy = scene.get("visual_hierarchy") or []
    if not hierarchy or any(h not in ids for h in hierarchy):
        problems.append(f"{where}.visual_hierarchy must list existing layer ids, most important first")
    style = scene.get("style") or {}
    for name in ("accent", "text_color"):
        if not _HEX.fullmatch(str(style.get(name, ""))):
            problems.append(f"{where}.style.{name} must be #RRGGBB")
    if style.get("typography") not in TYPOGRAPHY:
        problems.append(f"{where}.style.typography must be one of {TYPOGRAPHY}")
    return problems


def normalize_scene(scene: Mapping[str, Any]) -> dict[str, Any]:
    """Complete a valid scene with canvas, safe areas and version (ReCut-owned fields)."""
    out = copy.deepcopy(dict(scene))
    out["version"] = SCENE_VERSION
    out["canvas"] = dict(CANVAS)
    out["safe_areas"] = copy.deepcopy(SAFE_AREAS)
    out.setdefault("metadata", {})
    out["layers"] = sorted(out.get("layers", []), key=lambda layer: int(layer.get("z", 0)))
    return out


def primary_layer(scene: Mapping[str, Any]) -> Mapping[str, Any] | None:
    layers = {layer["id"]: layer for layer in scene.get("layers", [])}
    for lid in scene.get("visual_hierarchy", []):
        if lid in layers:
            return layers[lid]
    return None


def scene_assets(scene: Mapping[str, Any]) -> list[str]:
    ids = [(scene.get("background") or {}).get("asset_id")]
    ids += [layer.get("asset_id") for layer in scene.get("layers", [])]
    return [i for i in ids if i]


def composition_signature(scene: Mapping[str, Any]) -> tuple[str, str, str]:
    """(primary subject type, focal third, text placement): coarse composition identity for diversity checks."""
    primary = primary_layer(scene)
    kind = str(primary.get("type")) if primary else "background"
    focal = scene.get("focal_point") or {"x": 0.5, "y": 0.5}
    third = ("left", "centre", "right")[min(2, int(float(focal.get("x", 0.5)) * 3))]
    texts = [layer for layer in scene.get("layers", []) if layer.get("type") == "text"]
    if not texts:
        placement = "no_text"
    else:
        t = texts[0]
        placement = ("top" if t["y"] < 0.4 else "bottom" if t["y"] > 0.6 else "middle") + "-" + \
            ("left" if t["x"] < 0.4 else "right" if t["x"] > 0.6 else "centre")
    return kind, third, placement


# ---------------------------------------------------------------------------------------------------
# presets (the V2 archetypes, now as scene graphs) and the V2 -> V3 adapter

def _style(accent: str, text_color: str = "#FFFFFF", typography: str = "impact") -> dict[str, Any]:
    return {"accent": accent, "text_color": text_color, "typography": typography}


def _text(text: str, *, x: float, y: float, width: float, height: float, align: str, emphasis: str | None,
          z: int = 10) -> dict[str, Any]:
    return default_layer("text", "headline", text=text, x=x, y=y, width=width, height=height, align=align,
                         emphasis=emphasis, z=z, shadow=0.8, outline=0.6)


def small_creator(*, subject: str | None, bubble: str | None, x: float, y: float, height: float,
                  accent: str, z: int = 8) -> dict[str, Any] | None:
    if subject:
        return default_layer("subject_cutout", "creator", asset_id=subject, x=x, y=y, height=height, shadow=0.5,
                             rim_light=0.3, z=z)
    if bubble:
        return default_layer("image", "creator", asset_id=bubble, x=x, y=min(y, 1 - height * 0.6), height=height * 0.7,
                             width=height * 0.7 * 0.5625, clip="circle", outline=0.9, color=accent, shadow=0.6, z=z)
    return None


def preset_reaction(*, background: str, subject: str, text: str | None, element: str | None = None,
                    element_type: str = "card", side: str = "right", accent: str = "#FFD400",
                    emphasis: str | None = None, background_crop: list[float] | None = None) -> dict[str, Any]:
    """Creator cut-out large on one side, the moment pushed back, one optional element, short text."""
    right = side != "left"
    layers = [default_layer("gradient", "shade", side="left" if right else "right", strength=0.8, width=0.75, z=1),
              default_layer("subject_cutout", "creator", asset_id=subject, x=0.74 if right else 0.26, y=0.56,
                            height=1.08, shadow=0.6, rim_light=0.35, z=6, brightness=0.04, contrast=1.04,
                            saturation=1.05),
              default_layer("effect", "vignette", effect="vignette", strength=0.5, z=2)]
    hierarchy = ["creator"]
    if text:
        layers.append(_text(text, x=0.3 if right else 0.7, y=0.27, width=0.5, height=0.42,
                            align="left" if right else "right", emphasis=emphasis))
        hierarchy.append("headline")
    if element:
        layers.append(default_layer(element_type, "element", asset_id=element, x=0.22 if right else 0.78, y=0.7,
                                    height=0.36, rotation=-4.0 if right else 4.0, shadow=0.7, outline=0.3,
                                    clip="rounded", z=5))
        hierarchy.append("element")
    return {"background": {"asset_id": background, "crop": background_crop, "blur": 0.45, "dim": 0.4,
                           "saturation": 1.0},
            "layers": layers, "focal_point": {"x": 0.74 if right else 0.26, "y": 0.42},
            "visual_hierarchy": hierarchy, "style": _style(accent)}


def preset_board(*, background: str, text: str | None, cards: Sequence[str] = (), region: str | None = None,
                 region_crop: list[float] | None = None, subject: str | None = None, accent: str = "#FFD400",
                 emphasis: str | None = None, text_side: str = "left",
                 background_crop: list[float] | None = None, bubble: str | None = None) -> dict[str, Any]:
    """Cards / a board region as the subject; the scene pushed back; webcam small or absent."""
    left_text = text_side != "right"
    layers = [default_layer("effect", "vignette", effect="vignette", strength=0.6, z=1)]
    hierarchy: list[str] = []
    focal_x = 0.68 if left_text else 0.32
    if cards:
        for n, card in enumerate(cards[:2]):
            offset = (n - (len(cards[:2]) - 1) / 2) * 0.2
            layers.append(default_layer("card", f"card{n + 1}", asset_id=card, x=focal_x + offset, y=0.5,
                                        height=0.78 if len(cards) == 1 else 0.68, rotation=(-4 + 8 * n) if len(cards) > 1
                                        else -2.5, shadow=0.8, outline=0.35, clip="rounded", z=6 + n))
            hierarchy.append(f"card{n + 1}")
        background_kw = {"blur": 0.45, "dim": 0.45}
    else:
        layers.append(default_layer("board_crop", "board", asset_id=region or background, crop=region_crop,
                                    x=focal_x, y=0.5, height=0.86, width=0.6, shadow=0.7, outline=0.25,
                                    clip="rounded", z=5, contrast=1.08))
        hierarchy.append("board")
        background_kw = {"blur": 0.6, "dim": 0.5}
    if text:
        layers.append(_text(text, x=0.28 if left_text else 0.72, y=0.3, width=0.44, height=0.46,
                            align="left" if left_text else "right", emphasis=emphasis))
        hierarchy.append("headline")
    creator = small_creator(subject=subject, bubble=bubble, x=0.17 if left_text else 0.83, y=0.8, height=0.42,
                            accent=accent)
    if creator:
        layers.append(creator)
        hierarchy.append("creator")
    return {"background": {"asset_id": background, "crop": background_crop, "saturation": 1.0, **background_kw},
            "layers": layers, "focal_point": {"x": focal_x, "y": 0.5}, "visual_hierarchy": hierarchy,
            "style": _style(accent)}


def preset_story(*, background: str, text: str | None, inset: str | None = None, inset_crop: list[float] | None = None,
                 subject: str | None = None, accent: str = "#FFD400", emphasis: str | None = None,
                 text_side: str = "left", background_crop: list[float] | None = None,
                 bubble: str | None = None) -> dict[str, Any]:
    """One decisive moment full-bleed with a strong text side, optionally a second moment as a panel."""
    left = text_side != "right"
    layers = [default_layer("gradient", "shade", side="left" if left else "right", strength=0.85, width=0.8, z=1),
              default_layer("effect", "vignette", effect="vignette", strength=0.45, z=2)]
    hierarchy: list[str] = []
    if inset:
        layers.append(default_layer("board_crop", "moment2", asset_id=inset, crop=inset_crop, x=0.76 if left else 0.24,
                                    y=0.55, height=0.46, width=0.38, shadow=0.7, outline=0.3, clip="rounded", z=5,
                                    rotation=2.0 if left else -2.0))
        hierarchy.append("moment2")
    if text:
        layers.append(_text(text, x=(0.30 if inset else 0.33) if left else (0.70 if inset else 0.67), y=0.36,
                            width=0.5 if inset else 0.56, height=0.58,
                            align="left" if left else "right", emphasis=emphasis))
        hierarchy.insert(0, "headline")
    creator = small_creator(subject=subject, bubble=bubble, x=0.84 if left else 0.16, y=0.74, height=0.5,
                            accent=accent, z=7)
    if creator:
        layers.append(creator)
        hierarchy.append("creator")
    if not hierarchy:
        layers.append(default_layer("effect", "spot", effect="spotlight", strength=0.4, z=3))
        hierarchy.append("spot")
    return {"background": {"asset_id": background, "crop": background_crop, "blur": 0.0, "dim": 0.1,
                           "saturation": 1.1},
            "layers": layers, "focal_point": {"x": 0.6 if left else 0.4, "y": 0.5}, "visual_hierarchy": hierarchy,
            "style": _style(accent)}


def preset_board_band(*, background: str, background_crop: list[float] | None, text: str | None,
                      inset: str | None = None, inset_crop: list[float] | None = None, band: str = "bottom",
                      subject: str | None = None, bubble: str | None = None, accent: str = "#FFD400",
                      emphasis: str | None = None) -> dict[str, Any]:
    """The board region itself is the subject (sharp), a supporting panel, the headline in a dark band."""
    bottom = band != "top"
    layers = [default_layer("gradient", "band", side="bottom" if bottom else "top", strength=0.9, width=0.5, z=1),
              default_layer("effect", "vignette", effect="vignette", strength=0.4, z=2)]
    hierarchy = []
    if text:
        layers.append(_text(text, x=0.38, y=0.8 if bottom else 0.2, width=0.66, height=0.3, align="left",
                            emphasis=emphasis))
        hierarchy.append("headline")
    if inset:
        layers.append(default_layer("board_crop", "detail", asset_id=inset, crop=inset_crop, x=0.79,
                                    y=0.25 if bottom else 0.72, height=0.36, width=0.3, shadow=0.7, outline=0.3,
                                    clip="rounded", rotation=2.0, z=5))
        hierarchy.append("detail")
    creator = small_creator(subject=subject, bubble=bubble, x=0.13, y=0.24 if bottom else 0.76, height=0.36,
                            accent=accent)
    if creator:
        layers.append(creator)
        hierarchy.append("creator")
    return {"background": {"asset_id": background, "crop": background_crop, "blur": 0.0, "dim": 0.05,
                           "saturation": 1.1},
            "layers": layers, "focal_point": {"x": 0.5, "y": 0.45}, "visual_hierarchy": hierarchy or ["band"],
            "style": _style(accent)}


V2_LAYOUTS = {"split_reaction": "reaction_focus", "card_spotlight": "board_focus", "cinematic_text": "story_focus"}


def scene_from_v2(concept: Mapping[str, Any], subject_for: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Convert a V2 concept (composition_plan) into a scene graph so old outputs re-render with V3.

    subject_for maps a webcam_original id (W..) to its cut-out id (S..) when one exists.
    """
    plan = concept["composition_plan"]
    layout = V2_LAYOUTS.get(plan.get("layout"), plan.get("layout"))
    text = " ".join(str(concept.get("thumbnail_text") or "").split()) or None
    accent = plan.get("accent_color") or "#FFD400"
    webcam = concept.get("webcam_frame_id")
    subject = (subject_for or {}).get(webcam, None) if webcam else None  # only isolated cut-outs
    bubble = webcam if webcam and not subject else None
    cards = list(concept.get("card_asset_ids") or [])
    common = {"accent": accent, "emphasis": plan.get("emphasis_word"), "background_crop": plan.get("focus_box")}
    if layout == "reaction_focus" and subject:
        scene = preset_reaction(background=plan["background_frame_id"], subject=subject, text=text,
                                element=cards[0] if cards else None, side=plan.get("webcam_side") or "right", **common)
    elif layout == "board_focus" and cards:
        scene = preset_board(background=plan["background_frame_id"], text=text, cards=cards, subject=subject,
                             bubble=bubble, text_side="right" if plan.get("text_side") == "right" else "left", **common)
    elif layout == "board_focus":
        scene = preset_board_band(background=plan["background_frame_id"], background_crop=plan.get("focus_box"),
                                  text=text, inset=plan.get("inset_id"), inset_crop=plan.get("inset_box"),
                                  band="top" if plan.get("text_side") == "top" else "bottom", subject=subject,
                                  bubble=bubble, accent=accent, emphasis=plan.get("emphasis_word"))
    else:
        scene = preset_story(background=plan["background_frame_id"], text=text, inset=plan.get("inset_id"),
                             inset_crop=plan.get("inset_box"), subject=subject, bubble=bubble,
                             text_side="right" if plan.get("text_side") == "right" else "left", **common)
    scene["style"]["text_color"] = plan.get("text_color") or "#FFFFFF"
    scene["metadata"] = {"converted_from": "v2_composition_plan", "v2_layout": plan.get("layout")}
    return scene


def iter_layers(scene: Mapping[str, Any], kind: str | None = None) -> Iterable[Mapping[str, Any]]:
    for layer in scene.get("layers", []):
        if kind is None or layer.get("type") == kind:
            yield layer
