"""Deterministic thumbnail quality gate (no AI call).

After each render the gate checks the PNG and the geometry the renderer reports:
size and 16:9, headline inside the safe area and away from the duration badge,
headline not covering the subject (face, cards, inset), subject actually present
(a face for reaction_focus), minimum crop resolution, background sharpness,
contrast of the headline region, legibility at 320 px and near-duplicate
variants. Each failed check carries a fix the pipeline can apply automatically
(flip sides, stronger backdrop, smaller text, sharper background frame, other
archetype); what cannot be fixed is reported. A multimodal critic is left to a
future --mode deep option and is never part of the fast path.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np

BLUR_RATIO = 0.38  # background region vs the median whole frame of this video (calibrated on videoprova)
MIN_CROP_SIDE = 110  # px at 1280x720 for cards / elements
MIN_WEBCAM_SOURCE_H = 120  # px of real webcam pixels behind a face subject
MIN_TEXT_CONTRAST = 38.0  # luminance std inside the headline box at 320 px
MIN_CAP_AT_320 = 9.0
SIMILAR_HAMMING = 8  # of 64 bits


@dataclass
class GateIssue:
    code: str
    fix: str | None  # flip_text | flip_webcam | backdrop | smaller_text | swap_background | drop_webcam | None
    detail: str

    def to_mapping(self) -> dict[str, Any]:
        return asdict(self)


def _area(rect: Sequence[float]) -> float:
    return max(0.0, rect[2]) * max(0.0, rect[3])


def _intersection(a: Sequence[float], b: Sequence[float]) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    return max(0.0, x2 - x1) * max(0.0, y2 - y1)


def dhash(image: np.ndarray) -> int:
    import cv2

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    small = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA)
    bits = (small[:, 1:] > small[:, :-1]).flatten()
    return int(sum(1 << i for i, bit in enumerate(bits) if bit))


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def inspect(result: Any, plan: Any, *, reference_sharpness: float = 0.0) -> list[GateIssue]:
    """Issues of one rendered thumbnail (RenderResult + the RenderPlan that produced it)."""
    import cv2

    from .frames import read_image

    issues: list[GateIssue] = []
    image = read_image(Path(result.path))
    if image is None:
        return [GateIssue("unreadable_output", None, "rendered file cannot be read")]
    h, w = image.shape[:2]
    if (w, h) != (result.width, result.height) or w * 9 != h * 16:
        issues.append(GateIssue("bad_size", None, f"{w}x{h} is not the requested 16:9 size"))
    text = result.text or {}
    if text.get("lines"):
        if not text.get("fits", True):
            issues.append(GateIssue("text_outside_safe_area", "smaller_text", "headline touches the margins"))
        if not text.get("timestamp_zone_clear", True):
            issues.append(GateIssue("text_in_duration_badge", "flip_text", "headline under the YouTube duration badge"))
        if float(text.get("px_at_320", 99)) < MIN_CAP_AT_320:
            issues.append(GateIssue("text_too_small", None, f"cap height {text.get('px_at_320')} px at 320 wide"))
        bounds = text.get("bounds") or [0, 0, 0, 0]
        for subject in result.subjects:
            if subject["kind"] in ("face", "card", "element"):
                covered = _intersection(bounds, subject["rect"]) / max(1.0, _area(subject["rect"]))
                if covered > (0.06 if subject["kind"] == "face" else 0.15):
                    fix = "flip_webcam" if subject["kind"] == "face" else "flip_text"
                    issues.append(GateIssue("text_covers_subject", fix,
                                            f"headline covers {covered:.0%} of the {subject['kind']}"))
        x, y, bw, bh = (int(round(v)) for v in bounds)
        region = image[max(0, y):max(0, y + bh), max(0, x):max(0, x + bw)]
        if region.size:
            small = cv2.resize(cv2.cvtColor(region, cv2.COLOR_BGR2GRAY),
                               (max(1, bw // 4), max(1, bh // 4)), interpolation=cv2.INTER_AREA)
            contrast = float(small.std())
            if contrast < MIN_TEXT_CONTRAST:
                issues.append(GateIssue("low_text_contrast", "backdrop", f"luminance std {contrast:.0f}"))
    kinds = {s["kind"] for s in result.subjects}
    if result.layout == "reaction_focus" and not any(s["kind"] == "face" for s in result.subjects):
        issues.append(GateIssue("no_face", "drop_webcam", "reaction_focus without a detectable face"))
    if plan.webcam is not None and "face" in kinds and plan.webcam.shape[0] < MIN_WEBCAM_SOURCE_H:
        issues.append(GateIssue("webcam_low_resolution", "drop_webcam", f"webcam crop only {plan.webcam.shape[0]} px"))
    for subject in result.subjects:
        if subject["kind"] in ("card", "element") and min(subject["rect"][2], subject["rect"][3]) < MIN_CROP_SIDE * 0.6:
            issues.append(GateIssue("subject_too_small", None, f"{subject['kind']} only {subject['rect'][2]:.0f} px"))
        intentionally_soft = result.layout == "reaction_focus" or bool(plan.cards)  # pushed-back scenes
        value = subject.get("sharpness")
        blurry = value is not None and value < BLUR_RATIO * reference_sharpness
        if subject["kind"] in ("background", "inset") and blurry and not intentionally_soft:
            issues.append(GateIssue("background_blurry", "swap_background",
                                    f"sharpness {value:.0f} vs video median {reference_sharpness:.0f}"))
    return issues


def similar_pairs(paths: Sequence[Path]) -> list[tuple[int, int, int]]:
    """(i, j, distance) of variants whose 64-bit difference hashes are near-identical."""
    from .frames import read_image

    hashes: list[int | None] = []
    for path in paths:
        image = read_image(Path(path))
        hashes.append(dhash(image) if image is not None else None)
    pairs = []
    for i in range(len(hashes)):
        for j in range(i + 1, len(hashes)):
            first, second = hashes[i], hashes[j]
            if first is not None and second is not None:
                distance = hamming(first, second)
                if distance <= SIMILAR_HAMMING:
                    pairs.append((i, j, distance))
    return pairs


def remember(composition: dict[str, Any], concept: dict[str, Any], key: str, *, on_concept: bool = False) -> None:
    """Record the agent's original value once, so automatic fixes are reversible (restore_original)."""
    original = composition.setdefault("render_tuning", {}).setdefault("original", {})
    name = f"concept.{key}" if on_concept else key
    if name not in original:
        original[name] = concept.get(key) if on_concept else composition.get(key)


def restore_original(composition: dict[str, Any], concept: dict[str, Any]) -> None:
    """Undo previous automatic fixes before a new render pass (the gate re-derives them)."""
    tuning = composition.get("render_tuning") or {}
    if not tuning.get("auto"):
        return  # tuning written by a person is kept
    for name, value in (tuning.get("original") or {}).items():
        if name.startswith("concept."):
            concept[name.split(".", 1)[1]] = value
        else:
            composition[name] = value
    composition.pop("render_tuning", None)


def apply_fix(composition: dict[str, Any], concept: dict[str, Any], fix: str) -> bool:
    """Mutate the composition (render_tuning / sides / layout) for one automatic fix; False if not applicable."""
    tuning = composition.setdefault("render_tuning", {})
    tuning["auto"] = True
    if fix == "flip_text":
        remember(composition, concept, "text_side")
        flips = {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}
        composition["text_side"] = flips.get(composition.get("text_side") or "left", "left")
        return True
    if fix == "flip_webcam":
        if tuning.get("text_scale", 1.0) > 0.86:
            tuning["text_scale"] = 0.85
            return True
        remember(composition, concept, "webcam_side")
        composition["webcam_side"] = "left" if composition.get("webcam_side") != "left" else "right"
        return True
    if fix == "backdrop":
        if tuning.get("backdrop", 1.0) >= 1.5:
            return False
        tuning["backdrop"] = round(tuning.get("backdrop", 1.0) + 0.25, 2)
        return True
    if fix == "smaller_text":  # one step only: persistent margin problems are geometry, not size
        if tuning.get("text_scale", 1.0) < 1.0:
            return False
        tuning["text_scale"] = 0.85
        return True
    if fix == "drop_webcam":
        if concept.get("webcam_frame_id") is None:
            return False
        remember(composition, concept, "webcam_frame_id", on_concept=True)
        remember(composition, concept, "layout")
        concept["webcam_frame_id"] = None
        if composition.get("layout") in ("reaction_focus", "split_reaction"):
            composition["layout"] = "story_focus"
        return True
    return False


# ---------------------------------------------------------------------------------------------------
# V3: checks on rendered scene graphs (SceneResult) and their local fixes

MAX_TEXT_AREA = 0.30  # share of the canvas
MAX_CONTENT_LAYERS = 5  # image-like layers + text: more reads as clutter at phone size
MIN_FOCAL_AREA = 0.10
MAX_PRIMARY_UPSCALE = 3.2


def _face_rect(layer: dict[str, Any], asset: dict[str, Any] | None) -> list[float] | None:
    """Face rectangle (output px) inside a drawn subject_cutout layer, from the cut-out's face box."""
    if asset is None:
        return None
    face = (asset.get("provenance") or {}).get("face")
    if not face:
        return None
    x, y, w, h = layer["rect"]
    return [x + face[0] * w, y + face[1] * h, face[2] * w, face[3] * h]


def inspect_scene(result: Any, scene: dict[str, Any], assets: dict[str, dict[str, Any]], *,
                  reference_sharpness: float = 0.0) -> list[GateIssue]:
    import cv2

    from .frames import read_image
    from .scene import primary_layer

    issues: list[GateIssue] = []
    image = read_image(Path(result.path))
    if image is None:
        return [GateIssue("unreadable_output", None, "rendered file cannot be read")]
    h, w = image.shape[:2]
    if (w, h) != (result.width, result.height) or w * 9 != h * 16:
        issues.append(GateIssue("bad_size", None, f"{w}x{h} is not the requested 16:9 size"))
    drawn = {d["id"]: d for d in result.layers}
    texts = [d for d in result.layers if d.get("type") == "text"]
    primary = primary_layer(scene)
    primary_drawn = drawn.get(str(primary["id"])) if primary else None
    for t in texts:
        info = t.get("text") or {}
        if not info.get("fits", True):
            issues.append(GateIssue("text_outside_safe_area", "smaller_text", f"{t['id']} touches the margins"))
        if not info.get("timestamp_zone_clear", True):
            issues.append(GateIssue("text_in_duration_badge", "move_text", f"{t['id']} under the duration badge"))
        if float(info.get("px_at_320", 99)) < MIN_CAP_AT_320:
            issues.append(GateIssue("text_too_small", None, f"cap height {info.get('px_at_320')} px at 320 wide"))
        bx, by, bw, bh = info.get("bounds") or t["rect"]
        if bw * bh / (w * h) > MAX_TEXT_AREA:
            issues.append(GateIssue("too_much_text", "smaller_text", f"text covers {bw * bh / (w * h):.0%} of the canvas"))
        words = len(" ".join(info.get("lines", [])).split())
        if words > 5:
            issues.append(GateIssue("long_thumbnail_text", None, f"{words} words: hard to read at a glance"))
        for layer in result.layers:
            if layer.get("type") == "subject_cutout":
                face = _face_rect(layer, assets.get(str(layer.get("asset_id"))))
                target = face or [layer["rect"][0], layer["rect"][1], layer["rect"][2], layer["rect"][3] * 0.55]
                covered = _intersection([bx, by, bw, bh], target) / max(1.0, _area(target))
                if covered > 0.06:
                    issues.append(GateIssue("text_covers_subject", "move_text", f"text covers {covered:.0%} of the face"))
            elif layer.get("type") in ("card",) and layer.get("rect"):
                covered = _intersection([bx, by, bw, bh], layer["rect"]) / max(1.0, _area(layer["rect"]))
                if covered > 0.15:
                    issues.append(GateIssue("text_covers_subject", "move_text", f"text covers {covered:.0%} of a card"))
        region = image[max(0, int(by)):max(0, int(by + bh)), max(0, int(bx)):max(0, int(bx + bw))]
        if region.size:
            small = cv2.resize(cv2.cvtColor(region, cv2.COLOR_BGR2GRAY), (max(1, int(bw) // 4), max(1, int(bh) // 4)),
                               interpolation=cv2.INTER_AREA)
            if float(small.std()) < MIN_TEXT_CONTRAST:
                issues.append(GateIssue("low_text_contrast", "backdrop", f"luminance std {float(small.std()):.0f}"))
    for layer in result.layers:
        kind = layer.get("type")
        if kind not in ("subject_cutout", "card", "board_crop", "image"):
            continue
        asset = assets.get(str(layer.get("asset_id"))) or {}
        if kind == "subject_cutout":
            q = asset.get("quality") or {}
            if not q.get("isolated", 1.0) and layer["rect"][3] > 0.45 * h:
                issues.append(GateIssue("unisolated_subject_large", "shrink_subject",
                                        "webcam is not isolated (rectangle) but shown large"))
            edges = [e for e in layer.get("cut_edges", []) if e != "bottom"]  # shoulders may leave the frame
            if edges and layer.get("visible_share", 1.0) < 0.85:
                issues.append(GateIssue("subject_cropped", "fit_subject", f"{layer['id']} cut at {', '.join(edges)}"))
        elif layer.get("visible_share", 1.0) < 0.8:
            issues.append(GateIssue("subject_cropped", "fit_subject",
                                    f"{layer['id']} only {layer.get('visible_share'):.0%} visible"))
        if layer is primary_drawn and float(layer.get("upscale", 1.0)) > MAX_PRIMARY_UPSCALE:
            issues.append(GateIssue("soft_primary", None, f"primary enlarged {layer.get('upscale')}x from its source"))
    if primary_drawn is not None and primary_drawn.get("rect") and primary_drawn.get("type") != "text":
        area = primary_drawn["rect"][2] * primary_drawn["rect"][3] * primary_drawn.get("visible_share", 1.0) / (w * h)
        if area < MIN_FOCAL_AREA:
            issues.append(GateIssue("weak_focal_point", None, f"primary element covers only {area:.0%} of the canvas"))
    content = [d for d in result.layers if d.get("type") in ("subject_cutout", "card", "board_crop", "image", "text")]
    if len(content) > MAX_CONTENT_LAYERS:
        issues.append(GateIssue("crowded", None, f"{len(content)} content elements; keep one subject and few supports"))
    background = drawn.get("background") or {}
    if not background.get("blurred") and reference_sharpness and \
            float(background.get("sharpness", 1e9)) < BLUR_RATIO * reference_sharpness and \
            primary_drawn is not None and primary_drawn.get("type") in (None, "text"):
        issues.append(GateIssue("background_blurry", "swap_background",
                                f"sharpness {background.get('sharpness')} vs median {reference_sharpness:.0f}"))
    return issues


def apply_scene_fix(scene: dict[str, Any], fix: str, *, alternatives: Sequence[str] = ()) -> bool:
    """Local, deterministic scene correction (geometry / tone only, never the idea)."""
    layers = scene.get("layers", [])
    texts = [layer for layer in layers if layer.get("type") == "text"]
    subjects = [layer for layer in layers if layer.get("type") == "subject_cutout"]
    if fix == "move_text" and texts:
        for t in texts:
            if subjects and abs(t["x"] - subjects[0]["x"]) < 0.35:
                t["x"] = round(1.0 - t["x"], 3)  # opposite side of the subject
                t["align"] = "left" if t["x"] < 0.5 else "right"
            elif t["y"] > 0.55:
                t["y"] = round(max(0.2, 1.0 - t["y"]), 3)  # out of the bottom band (duration badge)
            else:
                t["height"] = round(max(0.12, t["height"] * 0.8), 3)
        return True
    if fix == "smaller_text" and texts:
        changed = False
        for t in texts:
            margin = 0.055  # keep the whole text box inside the 5% safe area
            width = float(t.get("width") or 0.5)
            x = min(max(t["x"], margin + width / 2), 1 - margin - width / 2)
            y = min(max(t["y"], margin + t["height"] / 2), 1 - margin - t["height"] / 2)
            if (round(x, 3), round(y, 3)) != (round(t["x"], 3), round(t["y"], 3)):
                t["x"], t["y"] = round(x, 3), round(y, 3)
                changed = True
            elif t["height"] > 0.16:
                t["height"] = round(t["height"] * 0.82, 3)
                changed = True
        return changed
    if fix == "backdrop":
        shades = [layer for layer in layers if layer.get("type") == "gradient"]
        if shades:
            if shades[0]["strength"] >= 0.95:
                return False
            shades[0]["strength"] = round(min(0.95, shades[0]["strength"] + 0.2), 2)
            return True
        from .scene import default_layer

        side = "left" if texts and texts[0]["x"] < 0.5 else "right"
        layers.append(default_layer("gradient", "auto_shade", side=side, strength=0.75, width=0.7, z=1))
        return True
    if fix in ("shrink_subject", "fit_subject") and subjects:
        s = subjects[0]
        if fix == "shrink_subject":
            s["height"] = round(min(s["height"], 0.42), 3)
        else:
            s["height"] = round(max(0.3, s["height"] * 0.9), 3)
            half_w = s["height"] * 0.5625 * (4 / 3) / 2  # webcam cut-outs are about 4:3; width in canvas units
            s["x"] = round(min(max(s["x"], half_w + 0.01), 1 - half_w - 0.01), 3)
            s["y"] = round(min(max(s["y"], s["height"] / 2), 1.0), 3)
        return True
    if fix == "fit_subject":
        for layer in layers:
            if layer.get("type") in ("card", "board_crop", "image"):
                layer["height"] = round(max(0.2, layer["height"] * 0.9), 3)
                layer["x"] = round(min(max(layer["x"], 0.18), 0.82), 3)
                layer["y"] = round(min(max(layer["y"], 0.25), 0.75), 3)
        return True
    if fix == "swap_background" and alternatives:
        scene["background"]["asset_id"] = alternatives[0]
        scene["background"]["crop"] = None
        return True
    return False
