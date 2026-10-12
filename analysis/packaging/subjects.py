"""Webcam subject extraction: the creator's head and shoulders as an RGBA cut-out.

Local and deterministic, no new dependency, run only on the webcam candidates already
selected (never on the whole VOD):

1. background plate: per-pixel median of many crops of the same webcam ROI from the same
   game (the room is static, the creator moves). Built from frames already decoded for
   the highlight bursts, so it costs no extra decoding;
2. trimap: head ellipse and torso core are "probably foreground" (the creator sits in the
   same place, so the plate partly contains him); pixels that differ from the plate inside
   a head-and-shoulders envelope are "probably foreground" (hands, mic); everything outside
   the envelope is background; inner face and chest are certain foreground;
3. OpenCV GrabCut refines the boundary (hair, glasses, microphone stay attached);
4. the largest component is kept, closed, and feathered into a soft alpha edge;
5. a mask-quality check (coverage, face fully inside, spill outside the envelope, raggedness);
   a failed mask falls back to the original rectangular crop with a feathered edge.

The face is never generated or altered: only alpha is computed; colour adjustments are mild
and applied by the renderer. An optional ONNX portrait-matting model can replace step 2-3
(AUTO_CUTTER_SEGMENTATION_MODEL=<path to .onnx>); nothing is ever downloaded.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from .frames import require_cv2

SEGMENTER_VERSION = "webcam_subject_v1"
MIN_PLATE_SAMPLES = 6


@dataclass
class SubjectCutout:
    rgba: np.ndarray  # BGRA uint8
    method: str  # plate_grabcut | grabcut | onnx | fallback_rect
    quality: dict[str, float] = field(default_factory=dict)
    face: list[float] | None = None  # normalized face box inside the cut-out
    ok: bool = True
    limitations: list[str] = field(default_factory=list)


def background_plate(crops: Sequence[np.ndarray], *, max_items: int = 48) -> np.ndarray | None:
    """The empty room behind the creator, from crops of the same webcam ROI (resized to the first one).

    Each sample masks out its own person envelope (face detection), so every pixel is learned from the
    frames where it was really visible; pixels never uncovered fall back to the plain median.
    """
    cv2 = require_cv2()
    if len(crops) < MIN_PLATE_SAMPLES:
        return None
    h, w = crops[0].shape[:2]
    step = max(1, len(crops) // max_items)
    samples = [cv2.resize(c, (w, h), interpolation=cv2.INTER_AREA) for c in crops[::step]]
    stack = np.stack(samples).astype(np.float32)
    median = np.median(stack, axis=0)
    masked = stack.copy()
    for i, sample in enumerate(samples):
        face = _face(sample)
        if face is not None:
            env, _, _ = _regions(h, w, face)
            masked[i][env > 0] = np.nan
    with np.errstate(all="ignore"):
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            clean = np.nanmedian(masked, axis=0)
    seen = (~np.isnan(masked[..., 0])).sum(axis=0)
    plate = np.where((seen >= 3)[..., None] & ~np.isnan(clean), clean, median)
    return np.clip(plate, 0, 255).astype(np.uint8)


def _face(image: np.ndarray) -> tuple[float, float, float, float] | None:
    from .webcam import detect_faces

    faces = detect_faces(image, min_rel=0.15)
    return max(faces, key=lambda b: b[2] * b[3]) if faces else None


def _regions(h: int, w: int, face: tuple[float, float, float, float]) -> tuple[np.ndarray, np.ndarray, tuple[int, ...]]:
    """Envelope (where the person can be) and core (where the person almost surely is)."""
    cv2 = require_cv2()
    fx, fy, fw, fh = face
    x0, y0, x1, y1 = int(fx * w), int(fy * h), int((fx + fw) * w), int((fy + fh) * h)
    fwp, fhp = max(1, x1 - x0), max(1, y1 - y0)
    cx = (x0 + x1) // 2
    env = np.zeros((h, w), np.uint8)
    cv2.ellipse(env, (cx, (y0 + y1) // 2 - int(fhp * 0.15)), (int(fwp * 0.95), int(fhp * 1.05)), 0, 0, 360, 255, -1)
    cv2.fillPoly(env, [np.array([[cx - int(fwp * 0.75), y1 - fhp // 4], [cx + int(fwp * 0.75), y1 - fhp // 4],
                                 [cx + int(fwp * 1.7), h], [cx - int(fwp * 1.7), h]])], 255)
    k = int(fwp * 0.5) | 1
    env = cv2.dilate(env, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    core = np.zeros((h, w), np.uint8)
    cv2.ellipse(core, (cx, (y0 + y1) // 2 - int(fhp * 0.14)), (int(fwp * 0.66), int(fhp * 0.82)), 0, 0, 360, 255, -1)
    cv2.fillPoly(core, [np.array([[cx - int(fwp * 0.35), y1], [cx + int(fwp * 0.35), y1],
                                  [cx + int(fwp * 1.25), h], [cx - int(fwp * 1.25), h]])], 255)
    return env, core, (x0, y0, x1, y1, fwp, fhp, cx)


def mask_quality(mask: np.ndarray, env: np.ndarray | None, face_px: tuple[int, int, int, int] | None) -> dict[str, float]:
    cv2 = require_cv2()
    fg = mask > 127
    h, w = mask.shape
    coverage = float(fg.mean())
    face_inside = 1.0
    if face_px is not None:
        x0, y0, x1, y1 = face_px
        dx, dy = (x1 - x0) // 6, (y1 - y0) // 8  # inner face: the Haar box is a square with background corners
        region = fg[max(0, y0 + dy):max(0, y1 - dy), max(0, x0 + dx):max(0, x1 - dx)]
        face_inside = float(region.mean()) if region.size else 0.0
    spill = float((fg & (env == 0)).sum() / max(1, fg.sum())) if env is not None else 0.0
    contours, _ = cv2.findContours(fg.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    rough = 0.0
    if contours:
        c = max(contours, key=cv2.contourArea)
        area = max(1.0, cv2.contourArea(c))
        hull = max(1.0, cv2.contourArea(cv2.convexHull(c)))
        rough = 1.0 - area / hull  # 0 = convex blob; high = ragged / with holes and spikes
    side_touch = float(max(fg[:, 0].mean(), fg[:, -1].mean()))  # bottom edge contact is expected (shoulders)
    top_touch = float(fg[0, :].mean())
    return {"coverage": round(coverage, 3), "face_inside": round(face_inside, 3), "spill": round(spill, 3),
            "raggedness": round(rough, 3), "side_touch": round(side_touch, 3), "top_touch": round(top_touch, 3),
            "pixels_h": float(h)}


def fill_holes(mask: np.ndarray) -> np.ndarray:
    """Fill regions fully enclosed by the person (glasses reflections, dark shirt patches)."""
    cv2 = require_cv2()
    h, w = mask.shape
    flood = mask.copy()
    canvas = np.zeros((h + 2, w + 2), np.uint8)
    cv2.floodFill(flood, canvas, (0, 0), 255)
    return mask | cv2.bitwise_not(flood)


def head_notches(mask: np.ndarray, *, chin_y: int) -> float:
    """Share of the head's convex hull missing from the mask (bites taken out of the hair)."""
    cv2 = require_cv2()
    head = (mask[: max(1, chin_y)] > 127).astype(np.uint8)
    contours, _ = cv2.findContours(head, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return 1.0
    c = max(contours, key=cv2.contourArea)
    hull = cv2.contourArea(cv2.convexHull(c))
    return round(1.0 - cv2.contourArea(c) / max(1.0, hull), 3)


def hair_room(mask: np.ndarray, face_px: tuple[int, int, int, int]) -> float:
    """How far the mask rises above the face box (in face heights) over the face's central columns.

    A real head shows hair above the eyebrows; a cut-out that stops at the forehead looks bald. Crops where
    the head touches the top edge are not penalised (the hair is simply out of frame)."""
    x0, y0, x1, y1 = face_px
    fh = max(1, y1 - y0)
    columns = mask[:, max(0, x0 + (x1 - x0) // 4):max(1, x1 - (x1 - x0) // 4)] > 127
    rows = np.flatnonzero(columns.any(axis=1))
    if rows.size == 0:
        return 0.0
    top = int(np.median([np.flatnonzero(col)[0] if col.any() else y0 for col in columns.T]))
    if top <= max(1, int(0.04 * mask.shape[0])):  # hair leaves the frame: nothing missing
        return 1.0
    return round(max(0.0, (y0 - top) / fh), 3)


def mask_ok(q: dict[str, float]) -> tuple[bool, list[str]]:
    problems = []
    if not 0.10 <= q["coverage"] <= 0.70:
        problems.append(f"coverage {q['coverage']:.2f} outside 0.10-0.70")
    if q["face_inside"] < 0.92:
        problems.append(f"face only {q['face_inside']:.0%} inside the mask")
    if q["spill"] > 0.06:
        problems.append(f"{q['spill']:.0%} of the mask outside the person envelope")
    if q["raggedness"] > 0.38:
        problems.append(f"ragged contour ({q['raggedness']:.2f})")
    if q["side_touch"] > 0.35:
        problems.append("mask runs along a side edge (background leak)")
    if q.get("hair_room", 1.0) < 0.12:
        problems.append("top of the head missing (cut-out would look bald)")
    if q.get("head_notches", 0.0) > 0.10:
        problems.append(f"head outline has bites ({q['head_notches']:.0%} of its hull missing)")
    if q.get("static_leak", 0.0) > 0.04:
        problems.append(f"{q['static_leak']:.0%} of the mask matches the empty room")
    return not problems, problems


def _feather_rect(crop: np.ndarray) -> np.ndarray:
    cv2 = require_cv2()
    h, w = crop.shape[:2]
    alpha = np.full((h, w), 255, np.uint8)
    edge = max(2, int(min(h, w) * 0.06))
    alpha = cv2.GaussianBlur(cv2.copyMakeBorder(alpha[edge:-edge, edge:-edge], edge, edge, edge, edge,
                                                cv2.BORDER_CONSTANT, value=0), (0, 0), edge / 2)
    return np.dstack([crop, alpha])


def segment_person(crop: np.ndarray, *, plate: np.ndarray | None = None) -> SubjectCutout:
    """RGBA cut-out of the person in a webcam crop, or a feathered rectangle when the mask is not reliable."""
    cv2 = require_cv2()
    h, w = crop.shape[:2]
    face = _face(crop)
    if face is None:
        return SubjectCutout(_feather_rect(crop), "fallback_rect", {}, None, False, ["no face detected"])
    onnx = OnnxPortraitSegmenter.from_env()
    env, core, (x0, y0, x1, y1, fwp, fhp, cx) = _regions(h, w, face)
    if onnx is not None:
        matte = onnx.alpha(crop)
        if matte is not None:
            q = mask_quality(matte, env, (x0, y0, x1, y1))
            ok, problems = mask_ok(q)
            if ok:
                return SubjectCutout(np.dstack([crop, matte]), "onnx", q, list(face), True, [])
    mask = np.full((h, w), cv2.GC_BGD, np.uint8)
    mask[env > 0] = cv2.GC_PR_BGD
    diff = None
    if plate is not None and plate.shape[:2] == (h, w):
        diff = np.abs(crop.astype(np.int16) - plate.astype(np.int16)).max(axis=2).astype(np.uint8)
        diff = cv2.GaussianBlur(diff, (0, 0), 2)
        mask[(env > 0) & (diff > 28)] = cv2.GC_PR_FGD
    mask[core > 0] = cv2.GC_PR_FGD
    mask[min(h - 1, y1 + fhp // 3):, max(0, cx - fwp // 3): cx + fwp // 3] = cv2.GC_FGD
    mask[y0 + fhp // 6: y1 - fhp // 8, x0 + fwp // 5: x1 - fwp // 5] = cv2.GC_FGD
    bgd, fgd = np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64)
    try:
        cv2.grabCut(crop, mask, None, bgd, fgd, 5, cv2.GC_INIT_WITH_MASK)
    except cv2.error as exc:  # degenerate trimap
        return SubjectCutout(_feather_rect(crop), "fallback_rect", {}, list(face), False, [f"grabcut failed: {exc}"])
    raw = np.where((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD), 255, 0).astype(np.uint8)
    protected = np.zeros((h, w), np.uint8)  # face and a narrow torso: never removed by the plate test
    cv2.rectangle(protected, (x0 - fwp // 10, y0), (x1 + fwp // 10, y1), 255, -1)
    cv2.fillPoly(protected, [np.array([[cx - int(fwp * 0.35), y1], [cx + int(fwp * 0.35), y1],
                                       [cx + int(fwp * 0.9), h], [cx - int(fwp * 0.9), h]])], 255)
    # above the forehead a person is only hair: nothing far beside the head (lamps, door frames, shelves)
    raw[: max(0, y0), : max(0, x0 - int(fwp * 0.3))] = 0
    raw[: max(0, y0), min(w, x1 + int(fwp * 0.3)):] = 0
    variants = [("plate_grabcut", True), ("grabcut", False)] if diff is not None else [("grabcut", False)]
    best: tuple[float, str, np.ndarray, dict[str, float], list[str]] | None = None
    for method, clean in variants:
        fg = raw.copy()
        if clean and diff is not None:  # static room (shelf, lamp, wall) attached to the head or shoulders
            fg[(protected == 0) & (diff < 16)] = 0
        fg = _tidy(fg, keep=protected)
        q = mask_quality(fg, env, (x0, y0, x1, y1))
        q["head_notches"] = head_notches(fg, chin_y=y1)
        q["hair_room"] = hair_room(fg, (x0, y0, x1, y1))
        if diff is not None:  # remaining foreground that still looks exactly like the empty room
            q["static_leak"] = round(float(((fg > 127) & (protected == 0) & (diff < 22)).sum()
                                           / max(1, (fg > 127).sum())), 3)
        ok, problems = mask_ok(q)
        score = q["head_notches"] + 2 * q.get("static_leak", 0.0) + 0.5 * q["raggedness"] + (0 if ok else 10)
        if best is None or score < best[0]:
            best = (score, method, fg, q, problems)
    assert best is not None
    _, method, fg, q, problems = best
    if problems:
        return SubjectCutout(_feather_rect(crop), "fallback_rect", q, list(face), False, problems)
    feathered = cv2.GaussianBlur(fg, (0, 0), max(0.8, h / 300.0))
    return SubjectCutout(np.dstack([crop, feathered]), method, q, [face[0], face[1], face[2], face[3]], True, [])


def _tidy(fg: np.ndarray, keep: np.ndarray | None = None) -> np.ndarray:
    """Keep the person: every component touching the face/torso core (head and torso may be split by a dark
    collar), else the largest one; close small gaps and fill enclosed holes."""
    cv2 = require_cv2()
    n, labels, stats, _ = cv2.connectedComponentsWithStats(fg)
    if n > 1:
        wanted = set(np.unique(labels[(keep > 0) & (labels > 0)]).tolist()) if keep is not None else set()
        if not wanted:
            wanted = {1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))}
        fg = np.where(np.isin(labels, list(wanted)), 255, 0).astype(np.uint8)
    fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7)))
    fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
    return fill_holes(fg)


class OnnxPortraitSegmenter:
    """Optional portrait-matting model through the already shipped onnxruntime.

    Configure AUTO_CUTTER_SEGMENTATION_MODEL with a local .onnx file whose first input is NCHW
    float RGB (any square size) and whose first output is a 1-channel alpha map in 0..1 (the
    MODNet export follows this contract). Inputs are normalised to [-1, 1]. Nothing is downloaded;
    any loading or inference error disables the model and the OpenCV path is used.
    """

    _cache: dict[str, Any] = {}

    def __init__(self, session: Any) -> None:
        self.session = session

    @classmethod
    def from_env(cls) -> OnnxPortraitSegmenter | None:
        path = os.environ.get("AUTO_CUTTER_SEGMENTATION_MODEL")
        if not path or not Path(path).is_file():
            return None
        if path not in cls._cache:
            try:
                import onnxruntime

                cls._cache[path] = cls(onnxruntime.InferenceSession(path, providers=["CPUExecutionProvider"]))
            except Exception:  # an unusable model disables itself
                cls._cache[path] = None
        return cls._cache[path]

    def alpha(self, crop: np.ndarray) -> np.ndarray | None:
        cv2 = require_cv2()
        try:
            meta = self.session.get_inputs()[0]
            side = meta.shape[-1] if isinstance(meta.shape[-1], int) else 512
            rgb = cv2.cvtColor(cv2.resize(crop, (side, side), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGB)
            x = (rgb.astype(np.float32) / 255.0 - 0.5) / 0.5
            out = self.session.run(None, {meta.name: x.transpose(2, 0, 1)[None]})[0]
            matte = np.clip(np.squeeze(out), 0.0, 1.0)
            return cv2.resize((matte * 255).astype(np.uint8), (crop.shape[1], crop.shape[0]),
                              interpolation=cv2.INTER_LINEAR)
        except Exception:
            return None
