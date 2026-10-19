"""V4.1 protected layers over an environment, using the existing SceneRenderer/cache.

No video access or provider calls here. All factual pixels come from the run's asset
catalogue. Transformations preserve aspect and never reconstruct content.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import math
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from .frames import read_image_unchanged, write_image
from .typography import apply_preset, TEXT_PRESETS

VERSION = "protected_composite_gameplay_v2"
FAMILIES = {"card-led": "card_dominance", "reaction-led": "reaction_impact",
            "board-led": "gameplay_clutch", "narrative-led": "big_statement"}
ENVIRONMENTS = ("dark directional spotlight and warm edge light",
                "deep cool gradient with a single diagonal light sweep",
                "restrained warm/cool split lighting, strong negative space",
                "clean dark poster backdrop, one broad soft halo")


def _asset_path(root: Path, value: str) -> Path:
    target = (root / value).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError("Protected assets must belong to this run")
    return target


def _usable(asset: Mapping[str, Any], *, card: bool = False) -> bool:
    q = asset.get("quality") or {}
    if int(asset.get("width", 0)) < (120 if card else 96) or int(asset.get("height", 0)) < (180 if card else 96):
        return False
    if card:
        p = asset.get("provenance") or {}
        return (p.get("status") in ("verified", "legible") and p.get("thumbnail_use") == "primary"
                and float(q.get("sharpness", 100)) >= 80)
    return float(q.get("sharpness", 100)) >= 10


def prepare_pack(pack: dict[str, Any], assets: Mapping[str, Mapping[str, Any]], root: Path,
                 *, role: str, index: int = 0) -> None:
    """Resolve only selected assets, limit clutter, record explicit fallback before generation."""
    if pack.get("generation_strategy") != "reference_preserving":
        return
    warnings: list[str] = []
    refs = {r["asset_id"]: r for r in pack["references"]}
    order = list(dict.fromkeys([pack["main_subject"], pack.get("secondary_subject", ""),
                              *pack.get("visual_hierarchy", []), *refs]))
    available = [a for a in order if a in refs and a in assets and _usable(assets[a])]
    cards = [a for a in available if assets[a]["type"] in ("card_crop", "card_artwork") and _usable(assets[a], card=True)]
    faces = []
    for aid in available:
        a = assets[aid]
        isolated: Mapping[str, Any] | None = None
        if a["type"] == "webcam_subject" and a.get("quality", {}).get("isolated"):
            isolated = a
        else:
            isolated = next((s for s in assets.values() if s["type"] == "webcam_subject" and s.get("parent_id") == aid
                             and s.get("quality", {}).get("isolated")), None)
        if isolated and _usable(isolated) and isolated.get("quality", {}).get("face_inside", 1):
            faces.append(aid)
        elif a["type"].startswith("webcam_"):
            warnings.append(f"{aid}: mask unreliable/unavailable; raw webcam box omitted, no synthetic face.")
    boards = [a for a in available if assets[a]["type"] in ("gameplay_frame", "board_region", "background")]
    if not boards:
        raise ValueError(f"{pack['concept_id']}: no usable authentic subject; generation cancelled")
    # Selected package only: never borrow an unselected game/class to complete a layout.
    background_id = next((a for a in boards if assets[a]["type"] == "board_region"), boards[0])
    b = assets[background_id]
    parent = assets.get(str(b.get("parent_id")))
    if parent and parent["type"] in ("gameplay_frame", "background"):
        b = parent  # the entire real frame, not a small region on an empty environment
    bp = _asset_path(root, str(b["path"]))
    if read_image_unchanged(bp) is None:
        raise ValueError("Unreadable real gameplay backdrop")
    background = {"asset_id": b["id"], "path": b["path"],
                  "sha256": hashlib.sha256(bp.read_bytes()).hexdigest(),
                  "source_id": refs[background_id].get("source_id"), "source_index": b.get("source_index"),
                  "source_time": b.get("source_time"), "game_id": b.get("game_id"),
                  "protection": "guided", "mask_boxes": []}
    # Use the already detected overlay ROI; no additional segmentation/video access.
    for a in assets.values():
        if a["type"] == "webcam_original" and a.get("bbox") and a.get("game_id") == b.get("game_id"):
            background["mask_boxes"].append(a["bbox"])
    archetype = pack.get("thumbnail_archetype") or {"card-led": "card_led", "reaction-led": "face_board"}.get(role, "board_led")
    if archetype in ("card_led", "face_card") and not cards:
        warnings.append("No usable authentic card: real gameplay replaces card dominance; no synthetic card.")
        archetype = "face_board" if faces and archetype == "face_card" else "board_led"
    if archetype in ("face_board", "face_card") and not faces:
        warnings.append("No usable isolated creator crop: board replaces reaction; no raw webcam box.")
        archetype = "card_led" if cards and archetype == "face_card" else "board_led"
    chosen = ([cards[0]] if archetype == "card_led" else
              [faces[0], cards[0]] if archetype == "face_card" else
              [faces[0]] if archetype == "face_board" else [])
    if len(available) > len(chosen):
        warnings.append("Clutter limit: only the dominant subject and one optional support are composited.")
    omitted = [a for a in pack.get("must_use_assets", []) if a not in [*chosen, *boards]]
    if omitted:
        warnings.append("Required package references omitted from final layout by quality/clutter checks: " + ", ".join(omitted))
    if pack.get("background_strategy") == "abstract_environment":
        warnings.append("Abstract environment rejected for reference-preserving output; retaining authentic gameplay.")
        pack["background_strategy"] = "real_gameplay_derived"
    layers = []
    # Keep usable selected alternatives for deterministic portfolio correction, not extra visual subjects.
    candidates = list(dict.fromkeys([*chosen, *cards[:1], *faces[:1]]))
    for n, aid in enumerate(candidates):
        a = assets[aid]
        policy = a.get("protection") or ("identity_protected" if a["type"].startswith("webcam_") else
                                         "locked" if a["type"].startswith("card_") else "guided")
        selected = a
        if a["type"].startswith("card_"):
            policy = "locked"
        elif a["type"].startswith("webcam_"):
            policy = "identity_protected"
        if policy == "identity_protected" and a["type"] == "webcam_original":
            cutout: Mapping[str, Any] | None = next((s for s in assets.values() if s["type"] == "webcam_subject"
                             and s.get("parent_id") == aid and s.get("quality", {}).get("isolated")), None)
            if cutout:
                selected = cutout
        p = _asset_path(root, str(selected["path"]))
        image = read_image_unchanged(p)
        if image is None:
            raise ValueError(f"Unreadable authentic asset {aid}")
        source_digest = hashlib.sha256(p.read_bytes()).hexdigest()
        framing = None
        if policy == "identity_protected" and image.ndim == 3 and image.shape[2] == 4:
            ys, xs = np.where(image[..., 3] > 16)
            if len(xs):
                x0, y0, x1, y1 = int(xs.min()), int(ys.min()), int(xs.max())+1, int(ys.max())+1
                framing = [x0, y0, x1-x0, y1-y0]
                # Remove transparent padding only; preserve every visible real subject pixel.
                nonzero_y, nonzero_x = np.where(image[..., 3] > 0)
                x0, y0 = int(nonzero_x.min()), int(nonzero_y.min())
                x1, y1 = int(nonzero_x.max())+1, int(nonzero_y.max())+1
                framing = [x0, y0, x1-x0, y1-y0]
                image = image[y0:y1, x0:x1].copy()
                framed = root / "subjects" / f"{selected['id']}_protected_framed.png"
                write_image(framed, image)
                selected = {**selected, "path": str(framed.relative_to(root))}
                p = framed
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        alpha = image.ndim == 3 and image.shape[2] == 4
        mask = None
        if alpha:
            mask_file = root / "subjects" / f"{selected['id']}_protected_mask.png"
            write_image(mask_file, image[..., 3])
            mask = {"path": str(mask_file.relative_to(root)),
                    "sha256": hashlib.sha256(mask_file.read_bytes()).hexdigest()}
        if policy == "identity_protected" and (not alpha or not np.any(image[..., 3] == 0)):
            warnings.append(f"{aid}: no usable transparent cutout; face omitted.")
            continue
        layers.append({"id": f"protected_{n}", "asset_id": selected["id"], "original_asset_id": aid,
                       "path": selected["path"], "sha256": digest, "protection": policy,
                       "type": "image", "mask": mask,
                       "source_index": a.get("source_index"), "source_time": a.get("source_time"),
                       "source_id": refs[aid].get("source_id"), "game_id": a.get("game_id"),
                       "source_size": [image.shape[1], image.shape[0]], "z": 10 + n,
                       "original_source_sha256": source_digest, "alpha_framing_px": framing,
                       "fallback": "original_frame" if policy == "identity_protected" and
                                   not selected.get("quality", {}).get("isolated") else None})
        if policy == "locked":
            warnings.append(f"{aid}: original source window retained; surrounding gameplay may remain in the crop.")
    if archetype.startswith("face_") and not any(l["protection"] == "identity_protected" for l in layers):
        archetype = "card_led" if any(l["protection"] == "locked" for l in layers) else "board_led"
    pack["composition"] = {"version": VERSION, "strategy": "reference_preserving", "family": "gameplay_clutch",
                           "background": background, "environment_protection": "creative_accents_only",
                           "environment_recipe": "restrained directional light around the real gameplay; no new objects",
                           "candidate_layers": deepcopy(layers), "layers": layers,
                           "warnings": warnings, "reserved_boxes": []}
    set_archetype(pack, archetype)
    if pack["typography"]["mode"] == "ai":
        pack["typography"]["mode"] = "precision"
        warnings.append("Gameplay-anchored composition uses local lettering; AI Typography remains available in Full generative.")
    layout_layers(pack)


def set_archetype(pack: dict[str, Any], archetype: str) -> None:
    """Only change graphic treatment, never introduce a new asset or claim a result."""
    comp = pack["composition"]
    candidates = comp["candidate_layers"]
    cards = [l for l in candidates if l["protection"] == "locked"]
    faces = [l for l in candidates if l["protection"] == "identity_protected"]
    layers = cards[:1] if archetype == "card_led" else faces[:1] if archetype == "face_board" else \
             faces[:1] + cards[:1] if archetype == "face_card" else []
    comp["layers"] = deepcopy(layers)
    comp["family"] = {"card_led": "card_dominance", "face_board": "reaction_impact",
                      "face_card": "reaction_impact", "result_payoff": "big_statement"}.get(archetype, "gameplay_clutch")
    pack.update(thumbnail_archetype=archetype,
                dominant_subject="card" if archetype == "card_led" else "face" if archetype.startswith("face_") else
                                 "result_screen" if archetype == "result_payoff" else "gameplay_board",
                secondary_subject_kind="card" if archetype == "face_card" else "gameplay_board" if layers else "text",
                optional_subject="", face_usage="primary_reaction" if faces and layers and archetype.startswith("face_") else "none",
                card_usage="card_plus_face" if archetype == "face_card" else "hero_card_big" if archetype == "card_led" else "none",
                board_visibility="recognizable_backdrop" if layers else "full_focus",
                composition_template={"card_led": "large_card_right_real_board", "face_board": "large_face_right_board_left",
                                      "face_card": "large_card_left_face_right", "result_payoff": "full_result_compact_hook"}.get(archetype, "full_board_compact_hook"))
    pack["main_subject"] = layers[0]["asset_id"] if layers else comp["background"]["asset_id"]
    pack["secondary_subject"] = layers[1]["asset_id"] if len(layers) > 1 else comp["background"]["asset_id"] if layers else ""
    strategy = pack.get("background_strategy", "real_gameplay_derived")
    comp["background"].update(blur_px=2. if strategy == "gameplay_blurred_depth" else 1.2 if layers else 0.,
                              dim=.82 if layers or strategy == "gameplay_blurred_depth" else 1.)


def gameplay_layout(pack: dict[str, Any]) -> None:
    comp, t = pack["composition"], pack["typography"]
    layers = comp["layers"]
    side = t["position"]
    # Compact lettering overlays the gameplay instead of shrinking the board to make room.
    t["box"] = list({"left": [.05, .06, .43, .24], "right": [.52, .06, .43, .24],
                     "top": [.05, .05, .43 if layers else .62, .23],
                     "bottom": [.05, .72, .43 if layers else .62, .23]}[side])
    if len(layers) == 2:
        t["box"] = [.05 if side != "right" else .52, .76 if side == "bottom" else .05,
                    .43 if side in ("left", "right") else .88, .18]
    comp["reserved_boxes"] = []
    for n, layer in enumerate(layers):
        if len(layers) == 2:
            box = [.05 if layer["protection"] == "locked" else .53, .05 if side == "bottom" else .27, .42, .67]
        else:
            box = [.05 if side == "right" else .51, .05, .44, .88]
        x, y, bw, bh = box
        sw, sh = layer["source_size"]
        ratio = min(bw * 1920 / sw, bh * 1080 / sh, 2.5)
        w, h = sw * ratio / 1920, sh * ratio / 1080
        layer.update(x=x + bw/2, y=y + bh/2, height=h, rotation=0., opacity=1., shadow=.45,
                     rim_light=.15 if layer.get("mask") else 0., brightness=0., contrast=1., saturation=1.,
                     clip="none", upscale=round(ratio, 3), reserved_box=box, fitted_size=[w, h], z=10+n)
        if ratio > 1.5:
            note = f"{layer['asset_id']}: authentic source enlarged {ratio:.2f}x; no new detail is created."
            if note not in comp["warnings"]:
                comp["warnings"].append(note)
        comp["reserved_boxes"].append(box)
    pack["subject_balance"] = {"dominant": pack["dominant_subject"], "foreground_sizes": [l["fitted_size"] for l in layers],
                               "gameplay_backdrop": [0., 0., 1., 1.]}
    comp["preflight"] = preflight(pack)
    if comp["preflight"]["rejected"]:
        # Fail closed on unsuitable large crops; a full real board remains a valid factual fallback.
        comp["warnings"].append("Large subject failed preflight: " + ", ".join(comp["preflight"]["violations"]) + "; using full board.")
        if not layers:
            raise ValueError("Invalid full-board composition: " + ", ".join(comp["preflight"]["violations"]))
        set_archetype(pack, "board_led")
        gameplay_layout(pack)


def preflight(pack: Mapping[str, Any]) -> dict[str, Any]:
    """Geometric/provenance checks, not an aesthetic or semantic certification."""
    c = pack["composition"]
    layers = c["layers"]
    penalties: dict[str, int] = {}
    if not c.get("background") or pack.get("background_strategy") == "abstract_environment":
        penalties["generic_abstract_background"] = 55
    if not pack.get("dominant_subject"):
        penalties["weak_subject_hierarchy"] = 45
    if len(layers) > min(2, int(pack.get("clutter_budget", 2))):
        penalties["too_many_small_elements"] = 40
    cards = [l for l in layers if l["protection"] == "locked"]
    faces = [l for l in layers if l["protection"] == "identity_protected"]
    if pack.get("thumbnail_archetype") in ("card_led", "face_card") and (
            not cards or any(l.get("fitted_size", [0, 0])[1] < .65 for l in cards)):
        penalties["card_not_dominant_or_legible"] = 55
    if pack.get("face_usage") == "primary_reaction" and (not faces or any(
            not l.get("mask") or l.get("fitted_size", [0, 0])[0] < .25 for l in faces)):
        penalties["face_too_small_or_raw_box"] = 55
    if any(l["protection"] == "guided" for l in layers):
        penalties["tiny_center_screenshot"] = 45
    words = len(pack["typography"]["text"].split())
    if words > 6 and not pack["typography"].get("locked"):
        penalties["long_hook"] = 12
    visible = 1. - sum(float(l.get("fitted_size", [0, 0])[0]) * float(l.get("fitted_size", [0, 0])[1]) for l in layers)
    if visible < .35:
        penalties["low_hearthstone_identity"] = 45
    for pattern in pack.get("observed_banned_patterns", []):
        if pattern in pack.get("banned_patterns", []):
            penalties[pattern] = 45
    bonus = {"authentic_full_canvas_gameplay": 15 if c.get("background") else 0,
             "short_hook": 5 if words <= 5 else 0, "single_clear_focus": 5 if len(layers) <= 1 else 0}
    scores = {"clutter_score": round(min(1., len(layers)/3), 2), "gameplay_visibility_score": round(max(0., visible), 2),
              "card_legibility_score": round(min([l.get("fitted_size", [0, 0])[1] for l in cards], default=0.), 2),
              "face_readability_score": round(min([l.get("fitted_size", [0, 0])[0] for l in faces], default=0.), 2)}
    if isinstance(pack, dict):
        pack.update(scores)
    return {**scores, "penalties": penalties, "bonuses": bonus, "score": max(0, min(100, 75 + sum(bonus.values()) - sum(penalties.values()))),
            "rejected": any(v >= 40 for v in penalties.values()), "violations": list(penalties),
            "dominant_subject": pack.get("main_subject"), "visual_elements": len(layers), "hook_words": words,
            "authentic_subjects": True, "protected_pixels_generated": False, "warnings": list(c["warnings"]),
            "aesthetic_quality_certified": False}


def layout_layers(pack: dict[str, Any]) -> None:
    """Contain every authentic asset in a reserved box; never cover-crop a card or face."""
    comp = pack["composition"]
    if comp.get("background"):
        gameplay_layout(pack)
        return
    position = pack["typography"]["position"]
    boxes = {
        "left": [[.53, .08, .42, .84], [.53, .64, .23, .28]],
        "right": [[.05, .08, .42, .84], [.29, .64, .23, .28]],
        "top": [[.08, .39, .84, .55], [.68, .61, .25, .31]],
        "bottom": [[.07, .06, .86, .49], [.70, .09, .23, .30]],
    }[position]
    layers = comp["layers"]
    if len(layers) > 1:
        # Disjoint boxes: the support must not obscure card text or the creator's face.
        if position in ("left", "right"):
            boxes = [[boxes[0][0], .08, .42, .62], [boxes[0][0] + .08, .73, .26, .20]]
        else:
            boxes = [[.06, boxes[0][1], .60, boxes[0][3]], [.70, boxes[0][1], .24, boxes[0][3]]]
    comp["reserved_boxes"] = boxes[:len(layers)]
    for layer, box in zip(layers, boxes):
        x, y, bw, bh = box
        sw, sh = layer["source_size"]
        # Small rotations require room on all sides. Preserve the full input pixel rectangle.
        ratio = min(bw * 1920 / sw, bh * 1080 / sh, 2.5) * .94
        w, h = sw * ratio / 1920, sh * ratio / 1080
        layer.update(x=x + bw / 2, y=y + bh / 2, height=h, rotation=0.,
                     opacity=1., shadow=.45, rim_light=.15 if layer.get("mask") else 0.,
                     brightness=0., contrast=1., saturation=1., clip="none", upscale=round(ratio, 3),
                     reserved_box=box, fitted_size=[w, h])
        if ratio > 1.5:
            note = f"{layer['asset_id']}: authentic source enlarged {ratio:.2f}x; no new detail is created."
            if note not in comp["warnings"]:
                comp["warnings"].append(note)
    comp["preflight"] = {"dominant_subject": layers[0]["asset_id"], "visual_elements": len(layers),
                         "authentic_subjects": True, "protected_pixels_generated": False,
                         "hook_words": len(pack["typography"]["text"].split()),
                         "warnings": list(comp["warnings"])}
    if len(pack["typography"]["text"].split()) > 4:
        comp["preflight"]["warnings"].append("Long hook: adaptive typography must fit; user locks are retained.")


def diversify(packs: list[dict[str, Any]], *, layout_changes: bool = True) -> dict[str, Any]:
    """Deterministic layout/type correction, never rewrite hooks or introduce facts/assets."""
    changes = []
    used_positions: set[str] = set()
    used_styles: set[str] = set()
    used_archetypes: set[str] = set()
    face_count = 0
    portfolio_warnings = []
    for n, pack in enumerate(packs):
        c = pack.get("composition") or {}
        if layout_changes and c.get("background"):
            candidates = c["candidate_layers"]
            card = any(l["protection"] == "locked" for l in candidates)
            face = any(l["protection"] == "identity_protected" for l in candidates)
            allowed = (["card_led"] if card else []) + ["board_led"] + \
                      (["face_board"] if face and face_count < 2 else []) + \
                      (["face_card"] if face and card and face_count < 2 else [])
            # Never invent a payoff/result when the original plan did not supply it.
            if pack["thumbnail_archetype"] == "result_payoff":
                allowed.insert(0, "result_payoff")
            current = pack["thumbnail_archetype"]
            if current in used_archetypes or current not in allowed or (n == len(packs)-1 and "board_led" not in used_archetypes
                                                                      and "result_payoff" not in used_archetypes):
                choice = "board_led" if n == len(packs)-1 and not used_archetypes.intersection({"board_led", "result_payoff"}) else next(
                    (a for a in allowed if a not in used_archetypes), "board_led")
                set_archetype(pack, choice)
                changes.append(f"{pack['concept_id']}: supported archetype diversified to {choice}")
        t = pack["typography"]
        preset = t.get("preset", "white_impact")
        if preset in used_styles:
            preset = next((s for s in TEXT_PRESETS if s not in used_styles), preset)
            pack["typography"] = t = apply_preset(t, preset)
            changes.append(f"{pack['concept_id']}: typography diversified to {preset}")
        used_styles.add(preset)
        t["font_variant"] = n
        position = t["position"]
        if layout_changes and position in used_positions:
            position = next((p for p in ("left", "right", "top", "bottom") if p not in used_positions), position)
            from .design import TEXT_BOXES
            t.update(position=position, box=list(TEXT_BOXES[position]))
            changes.append(f"{pack['concept_id']}: reserved layout diversified to text {position}")
        used_positions.add(position)
        if pack.get("composition"):
            if not pack["composition"].get("background"):
                pack["composition"]["environment_recipe"] = ENVIRONMENTS[n % len(ENVIRONMENTS)]
            layout_layers(pack)
            if pack["composition"].get("background"):
                used_archetypes.add(pack["thumbnail_archetype"])
                face_count += int(pack["face_usage"] != "none")
    signatures = []
    for pack in packs:
        c, t = pack.get("composition", {}), pack["typography"]
        layers = c.get("layers", [])
        hook = t["text"]
        signatures.append({"concept_id": pack["concept_id"], "family": c.get("family", pack["layout"]),
                           "dominant": pack["main_subject"], "archetype": pack.get("thumbnail_archetype"),
                           "face_presence": any(l["protection"] == "identity_protected" for l in layers),
                           "face_size": [l["fitted_size"] for l in layers if l["protection"] == "identity_protected"],
                           "elements": len(layers), "hook": hook,
                           "hook_type": "none" if not hook else "question" if "?" in hook else
                                        "exclamation" if "!" in hook else "statement",
                           "text_position": t["position"], "typography": t.get("preset"), "accent": t.get("color")})
    repeated_hooks = len({s["hook"] for s in signatures}) < len(signatures)
    if used_archetypes and len(used_archetypes) < len(packs):
        portfolio_warnings.append("Not enough usable selected cards/cutouts for four different archetypes; real board variants retained.")
    if used_archetypes and "card_led" not in used_archetypes:
        portfolio_warnings.append("No card-led variant: no suitable selected authentic card; do not fabricate one.")
    return {"version": VERSION, "concepts": signatures, "local_changes": changes,
            "distinct_positions": len(used_positions), "distinct_type_presets": len(used_styles),
            "distinct_archetypes": len(used_archetypes), "face_variants": face_count,
            "warnings": portfolio_warnings + (["Repeated hook; may be an explicit user lock. Story diversity still requires review."]
                        if repeated_hooks else []), "aesthetic_quality_certified": False}


def fallback_environment(target: Path, pack: Mapping[str, Any], root: Path | None = None) -> None:
    """Real gameplay for new runs; retain the legacy environment fallback for old saved packs."""
    from .frames import require_cv2
    cv2 = require_cv2()
    background = pack.get("composition", {}).get("background")
    if background and root is not None:
        from .render import prepare
        p = _asset_path(root, background["path"])
        if hashlib.sha256(p.read_bytes()).hexdigest() != background["sha256"]:
            raise ValueError("Real gameplay backdrop changed")
        image = read_image_unchanged(p)
        if image is None:
            raise ValueError("Missing real gameplay backdrop")
        write_image(target, prepare(image, None, (1920, 1080), masks=background.get("mask_boxes", [])))
        return
    color = str(pack["typography"].get("color", "#FFFFFF")).lstrip("#")
    accent = np.array([int(color[i:i + 2], 16) for i in (4, 2, 0)], np.float32)
    x = np.linspace(0, 1, 1920, dtype=np.float32)[None, :, None]
    y = np.linspace(0, 1, 1080, dtype=np.float32)[:, None, None]
    glow = np.exp(-((x - .65)**2 + (y - .5)**2) * 5) * .18
    image = np.clip(np.array([28, 18, 15]) + glow * accent, 0, 255).astype(np.uint8)
    write_image(target, cv2.resize(image, (1920, 1080)))


def composite(environment: Path, target: Path, pack: Mapping[str, Any], root: Path, *, cache: Any = None) -> dict[str, Any]:
    from .cache import stable_key
    from .render import SceneRenderer
    comp = pack["composition"]
    bg = read_image_unchanged(environment)
    if bg is None:
        raise ValueError("Missing environment layer")
    background = comp.get("background")
    if background:
        from .frames import require_cv2
        from .render import prepare
        cv2 = require_cv2()
        source = _asset_path(root, background["path"])
        if hashlib.sha256(source.read_bytes()).hexdigest() != background["sha256"]:
            raise ValueError("Real gameplay backdrop changed")
        real = read_image_unchanged(source)
        if real is None:
            raise ValueError("Missing real gameplay backdrop")
        # Full canvas factual pixels. The model contributes only a smooth light field,
        # never an invented board/card/face or a tiny screenshot in a poster.
        generated = bg
        bg = prepare(real, None, (1920, 1080), masks=background.get("mask_boxes", []),
                     blur=float(background.get("blur_px", 0.)), dim=float(background.get("dim", 1.)))
        light = cv2.resize(cv2.cvtColor(generated[..., :3], cv2.COLOR_BGR2GRAY), (192, 108))
        light = cv2.GaussianBlur(light, (0, 0), 12)
        light = cv2.resize(light.astype(np.float32)/255 - .5, (1920, 1080))
        bg = np.clip(bg.astype(np.float32) * (1 + .12 * light[..., None]), 0, 255).astype(np.uint8)
    assert bg is not None
    images: dict[str, np.ndarray] = {"environment": bg}
    zs = [int(l["z"]) for l in comp["layers"]]
    if zs != sorted(set(zs)) or not (0 if background else 1) <= len(zs) <= 2:
        raise ValueError("Invalid protected layer order or clutter")
    if background and preflight(pack)["rejected"]:
        raise ValueError("Composition rejected by Hearthstone preflight")
    for layer in comp["layers"]:
        p = _asset_path(root, layer["path"])
        if hashlib.sha256(p.read_bytes()).hexdigest() != layer["sha256"]:
            raise ValueError(f"Protected asset changed: {layer['asset_id']}")
        if layer.get("mask"):
            m = _asset_path(root, layer["mask"]["path"])
            if hashlib.sha256(m.read_bytes()).hexdigest() != layer["mask"]["sha256"]:
                raise ValueError("Protected mask changed")
        authentic = read_image_unchanged(p)
        if authentic is None:
            raise ValueError("Missing protected image")
        images[layer["asset_id"]] = authentic
        if not all(math.isfinite(float(layer[k])) for k in ("x", "y", "height", "rotation")):
            raise ValueError("Invalid protected geometry")
        if not (0 < layer["x"] < 1 and 0 < layer["y"] < 1 and 0 < layer["height"] <= .94):
            raise ValueError("Protected geometry lies outside safe bounds")
        if layer.get("width") or layer.get("crop") or abs(float(layer["rotation"])) > 6:
            raise ValueError("Protected content cannot be cover-cropped or arbitrarily transformed")
        if layer["protection"] == "locked" and any(float(layer.get(k, default)) != default
                for k, default in (("brightness", 0.), ("contrast", 1.), ("saturation", 1.))):
            raise ValueError("Locked card pixels cannot be colour-reinterpreted")
    key = stable_key(VERSION, hashlib.sha256(environment.read_bytes()).hexdigest(), comp)
    if cache is not None:
        found = cache.load_json("render", key)
        if found and cache.load_file("render", key, ".png", target):
            return {**found, "cache_hit": True}
    scene = {"background": {"asset_id": "environment", "blur": 0., "dim": 0., "saturation": 1.},
             "style": {"accent": pack["typography"].get("color", "#FFFFFF")},
             "layers": deepcopy(comp["layers"])}
    result = SceneRenderer(size=(1920, 1080), supersample=1).render_scene(scene, images, target)
    drawn = {l["asset_id"]: l for l in result.layers if l.get("asset_id") != "environment"}
    metadata = {"version": VERSION, "strategy": "reference_preserving", "cache_hit": False,
                "background": deepcopy(background), "preflight": preflight(pack) if background else comp.get("preflight"),
                "layer_order": ["environment", *[l["asset_id"] for l in comp["layers"]], "typography"],
                "layers": [{**l, "drawn": drawn.get(l["asset_id"])} for l in comp["layers"]],
                "provenance_verified": all(l["asset_id"] in drawn for l in comp["layers"]),
                "warnings": list(comp["warnings"]) + result.warnings,
                "semantic_claims_certified": False}
    if not metadata["provenance_verified"]:
        raise ValueError("A protected layer was not drawn")
    if any(drawn[l["asset_id"]].get("cut_edges") for l in comp["layers"]):
        raise ValueError("Protected subject extends outside the final canvas")
    if cache is not None:
        cache.store_json("render", key, metadata)
        cache.store_file("render", key, ".png", target)
    return metadata
