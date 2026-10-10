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
