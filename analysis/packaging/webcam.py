"""Optional webcam region: normalized ROI (CLI/profile/auto), crops and frame-quality scores.

The webcam is optional: no ROI, an invalid ROI or no detected face only disable
webcam crops; the rest of the pipeline continues. Scores are image-quality and
activity proxies (sharpness, exposure, face found, motion). They never assert an
emotion: a visible face is evidence that a face is visible, nothing more.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np

from .frames import require_cv2


@dataclass(frozen=True)
class NormalizedRect:
    x: float
    y: float
    w: float
    h: float

    def pixels(self, width: int, height: int) -> tuple[int, int, int, int]:
        left, top = int(round(self.x * width)), int(round(self.y * height))
        right, bottom = int(round((self.x + self.w) * width)), int(round((self.y + self.h) * height))
        return left, top, max(left + 1, right), max(top + 1, bottom)

    def crop(self, image: np.ndarray) -> np.ndarray:
        left, top, right, bottom = self.pixels(image.shape[1], image.shape[0])
        return image[top:bottom, left:right]

    def to_list(self) -> list[float]:
        return [round(self.x, 4), round(self.y, 4), round(self.w, 4), round(self.h, 4)]


@dataclass(frozen=True)
class RoiCheck:
    status: str  # ok | clamped | invalid | absent
    rect: NormalizedRect | None
    message: str = ""
    origin: str = ""  # cli | profile | auto


def parse_roi(value: Any, *, origin: str = "cli") -> RoiCheck:
    """Accept "x,y,w,h" or a 4-sequence of normalized numbers; clamp partial overflow, reject the rest."""
    if value is None or value == "" or value == "none":
        return RoiCheck("absent", None, "no webcam ROI configured", origin)
    try:
        parts = [float(v) for v in (value.split(",") if isinstance(value, str) else value)]
    except (TypeError, ValueError):
        return RoiCheck("invalid", None, f"webcam ROI must be 4 numbers x,y,w,h in 0..1, got {value!r}", origin)
    if len(parts) != 4 or not all(np.isfinite(parts)):
        return RoiCheck("invalid", None, f"webcam ROI must be 4 finite numbers x,y,w,h, got {value!r}", origin)
    x, y, w, h = parts
    if w <= 0 or h <= 0:
        return RoiCheck("invalid", None, "webcam ROI width and height must be positive", origin)
    left, top, right, bottom = max(0.0, x), max(0.0, y), min(1.0, x + w), min(1.0, y + h)
    if right - left < 0.02 or bottom - top < 0.02:
        return RoiCheck("invalid", None, "webcam ROI lies outside the frame (normalized coordinates are 0..1)", origin)
    rect = NormalizedRect(left, top, right - left, bottom - top)
    if (left, top, right, bottom) != (x, y, x + w, y + h):
        return RoiCheck("clamped", rect, f"webcam ROI clamped to the frame: {rect.to_list()}", origin)
    return RoiCheck("ok", rect, "", origin)


_CASCADE: Any = None


def detect_faces(image: np.ndarray, *, min_rel: float = 0.04) -> list[tuple[float, float, float, float]]:
    """Frontal faces as normalized (x, y, w, h) boxes (OpenCV Haar cascade, local and offline)."""
    global _CASCADE
    cv2 = require_cv2()
    if _CASCADE is None:
        _CASCADE = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    h, w = image.shape[:2]
    scale = 960.0 / w if w > 960 else 1.0
    small = cv2.resize(image, (int(w * scale), int(h * scale))) if scale != 1.0 else image
    gray = cv2.equalizeHist(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY))
    min_side = max(16, int(min_rel * gray.shape[0]))
    found = _CASCADE.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6, minSize=(min_side, min_side))
    sh, sw = gray.shape[:2]
    return [(float(x) / sw, float(y) / sh, float(fw) / sw, float(fh) / sh) for x, y, fw, fh in found]


def detect_webcam_roi(images: Sequence[np.ndarray], *, min_share: float = 0.4) -> tuple[RoiCheck, dict[str, Any]]:
    """A face found at a stable place in many frames => webcam ROI around it (marked 'auto')."""
    boxes = [(i, box) for i, image in enumerate(images) for box in detect_faces(image)]
    evidence: dict[str, Any] = {"frames": len(images), "faces": len(boxes)}
    if not images or not boxes:
        return RoiCheck("absent", None, "no face found in the sampled frames", "auto"), evidence
    clusters: list[list[tuple[int, tuple[float, float, float, float]]]] = []
    for item in boxes:
        cx, cy = item[1][0] + item[1][2] / 2, item[1][1] + item[1][3] / 2
        for cluster in clusters:
            ref = cluster[0][1]
            if abs(cx - (ref[0] + ref[2] / 2)) < 0.06 and abs(cy - (ref[1] + ref[3] / 2)) < 0.08:
                cluster.append(item)
                break
        else:
            clusters.append([item])
    best = max(clusters, key=lambda c: len({i for i, _ in c}))
    share = len({i for i, _ in best}) / len(images)
    evidence["stable_share"] = round(share, 3)
    if share < min_share:
        return RoiCheck("absent", None, f"no stable face position (best share {share:.0%})", "auto"), evidence
    xs = np.array([b for _, b in best])
    fx, fy, fw, fh = (float(np.median(xs[:, k])) for k in range(4))
    evidence["face_box"] = [round(v, 4) for v in (fx, fy, fw, fh)]
    box, sides = overlay_borders([images[i] for i in sorted({i for i, _ in best})], (fx, fy, fw, fh))
    evidence["borders"] = sides
    rect = parse_roi(box, origin="auto")
    message = "auto-detected: stable face, overlay borders " + ", ".join(f"{k}={v}" for k, v in sides.items())
    return RoiCheck(rect.status if rect.status != "clamped" else "ok", rect.rect, message, "auto"), evidence


def overlay_borders(images: Sequence[np.ndarray], face: tuple[float, float, float, float]
                    ) -> tuple[list[float], dict[str, str]]:
    """Webcam overlay rectangle around a stable face: straight edges accumulated over many frames.

    Each side independently takes the strongest edge line in its search band when it clearly stands out
    (>= 2.4x the band median); otherwise a head-and-shoulders margin around the face is used for that side.
    """
    cv2 = require_cv2()
    fx, fy, fw, fh = face
    fallback = {"left": fx - 1.1 * fw, "right": fx + 2.1 * fw, "top": fy - 0.9 * fh, "bottom": fy + 2.0 * fh}
    bands = {"left": (fx - 3.5 * fw, fx - 0.25 * fw), "right": (fx + 1.25 * fw, fx + 4.5 * fw),
             "top": (fy - 2.5 * fh, fy - 0.25 * fh), "bottom": (fy + 1.15 * fh, fy + 4.0 * fh)}
    cols = rows = None
    size = (960, 540)
    for image in images:
        gray = cv2.cvtColor(cv2.resize(image, size, interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY).astype(np.float32)
        dx = np.abs(cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3))
        dy = np.abs(cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3))
        y0, y1 = int(max(0.0, fy - 0.3 * fh) * size[1]), int(min(1.0, fy + 1.6 * fh) * size[1])
        x0, x1 = int(max(0.0, fx - 0.3 * fw) * size[0]), int(min(1.0, fx + 1.3 * fw) * size[0])
        c = dx[y0:y1].mean(axis=0) if y1 > y0 else np.zeros(size[0], np.float32)
        r = dy[:, x0:x1].mean(axis=1) if x1 > x0 else np.zeros(size[1], np.float32)
        cols = c if cols is None else cols + c
        rows = r if rows is None else rows + r
    found: dict[str, float] = {}
    sides: dict[str, str] = {}
    for side, (lo, hi) in bands.items():
        profile, length = (cols, size[0]) if side in ("left", "right") else (rows, size[1])
        a, b = int(max(0.0, lo) * length), int(min(1.0, hi) * length)
        if profile is None or b - a < 4:
            found[side], sides[side] = fallback[side], "face_margin"
            continue
        segment = profile[a:b]
        floor = 2.4 * max(1e-3, float(np.median(segment)))
        strong = np.flatnonzero((segment >= floor) & (segment >= 0.35 * float(segment.max())))
        if strong.size:  # the strong straight edge closest to the face is the overlay border
            peak = int(strong.max() if side in ("left", "top") else strong.min())
            while 0 < peak < len(segment) - 1 and max(segment[peak - 1], segment[peak + 1]) > segment[peak]:
                peak += 1 if segment[peak + 1] > segment[peak - 1] else -1  # climb to the local maximum
            found[side], sides[side] = (a + peak) / length, "edge"
        else:
            found[side], sides[side] = fallback[side], "face_margin"
    left, right = max(0.0, found["left"]), min(1.0, found["right"])
    top, bottom = max(0.0, found["top"]), min(1.0, found["bottom"])
    return [left, top, right - left, bottom - top], sides


def webcam_quality(crop: np.ndarray, previous: np.ndarray | None = None) -> dict[str, float]:
    """Image-quality proxies for a webcam crop; never an emotion.

    face_size (face height / crop height) and face_sharpness (Laplacian on the face only) decide whether the
    face can carry a reaction thumbnail; resolution is the real pixel height; activity is the inter-frame
    difference (talking, gesturing), used only as a weak hint.
    """
    cv2 = require_cv2()
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    sharp = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    bright = float(gray.mean()) / 255.0
    faces = detect_faces(crop, min_rel=0.18)
    face = max(faces, key=lambda b: b[2] * b[3]) if faces else None
    activity = 0.0
    if previous is not None and previous.shape == crop.shape:
        activity = float(np.mean(cv2.absdiff(gray, cv2.cvtColor(previous, cv2.COLOR_BGR2GRAY)))) / 32.0
    centered = face_sharp = 0.0
    if face is not None:
        cx, cy = face[0] + face[2] / 2, face[1] + face[3] / 2
        centered = 1.0 - min(1.0, (abs(cx - 0.5) + abs(cy - 0.45)) * 2.0)
        h, w = gray.shape
        region = gray[int(face[1] * h):int((face[1] + face[3]) * h), int(face[0] * w):int((face[0] + face[2]) * w)]
        face_sharp = float(cv2.Laplacian(region, cv2.CV_64F).var()) if region.size else 0.0
    resolution = min(1.0, crop.shape[0] / 300.0)
    face_size = 0.0 if face is None else face[3]
    score = (0.20 * min(1.0, np.log1p(sharp) / np.log1p(600.0)) + 0.10 * (1.0 - min(1.0, abs(bright - 0.45) / 0.45))
             + 0.25 * (1.0 if face is not None else 0.0) + 0.15 * min(1.0, face_size / 0.35)
             + 0.15 * min(1.0, np.log1p(face_sharp) / np.log1p(300.0)) + 0.05 * centered + 0.05 * resolution
             + 0.05 * min(1.0, activity))
    return {"sharpness": sharp, "brightness": bright, "face": 1.0 if face is not None else 0.0,
            "face_size": face_size, "face_sharpness": face_sharp, "resolution_px": float(crop.shape[0]),
            "centered": centered, "activity_proxy": min(1.0, activity), "score": float(score)}
