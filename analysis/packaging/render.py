"""ThumbnailRenderer: validated composition plan + real video assets -> final 16:9 PNG/JPEG.

Rendering is local and deterministic (PySide6 QPainter on an offscreen QImage;
no new dependency, no model-generated code). The canvas is drawn at 2x the
output size and smoothly downscaled. Three creative archetypes, each built for
one clear subject and a strong visual hierarchy (subject > headline > context):

  reaction_focus  the creator's face dominates one side (large, feathered into the
                  scene, no frame); the moment sits behind, pushed back; one optional
                  complementary Hearthstone element; 2-4 word headline.
  board_focus     the card / combo / board situation is the subject: zoomed region or
                  1-2 large real card crops; webcam small or absent.
  story_focus     a recognisable narrative moment: one decisive frame with a strong
                  text side, or two moments split diagonally (before/after, us/them).

Old layout names (v1) are aliases: split_reaction -> reaction_focus,
card_spotlight -> board_focus, cinematic_text -> story_focus.

Text is uppercase, auto-fitted to the largest size whose lines fit, with line
breaks that never leave an article or preposition dangling, a tight outline and
a soft shadow, inside 5% safe margins and away from the bottom-right duration
badge. Every render returns the geometry of its text and subjects so the
quality gate (quality.py) can check overlaps, legibility and composition.
"""
from __future__ import annotations

import math
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

RENDERER_VERSION = "thumbnail_renderer_v2"
LAYOUTS: dict[str, str] = {
    "reaction_focus": "the creator's webcam face is the subject (large, one side), the moment behind it, one optional "
                      "complementary element (card crop K.. or a frame region via inset_id/inset_box), very short "
                      "headline. Needs webcam_frame_id with a visible face.",
    "board_focus": "the card, combo or board situation is the subject: focus_box zooms the board region, or 1-2 real "
                   "card crops (card_asset_ids) are shown large. Webcam small (bubble) or absent.",
    "story_focus": "one decisive, recognisable moment with a strong text side, or two moments split diagonally "
                   "(background + inset_id frame: before/after, conflict). Optional badge only if evidenced.",
}
LAYOUT_ALIASES = {"split_reaction": "reaction_focus", "card_spotlight": "board_focus", "cinematic_text": "story_focus"}
OUTPUT_SIZE = (1280, 720)
SUPERSAMPLE = 2
SAFE = 0.05  # safe margin, fraction of the width/height
TIMESTAMP_ZONE = (0.80, 0.82)  # bottom-right corner (x >= 80%, y >= 82%) kept free of text
YOUTUBE_MAX_BYTES = 2 * 1024 * 1024
ACCENTED_CAPS = set("ÀÁÂÈÉÊÌÍÒÓÔÙÚÛÄÖÜÑÇ")  # glyphs taller than the cap height need more line spacing
# Words that must not end a line (it reads as a broken phrase: "LISTA DA / RIVEDERE").
DANGLING = {"A", "AD", "AL", "ALLA", "ALLE", "AI", "DA", "DAL", "DALLA", "DI", "DEL", "DELLA", "DEI", "E", "ED", "O",
            "IL", "LO", "LA", "I", "GLI", "LE", "UN", "UNO", "UNA", "IN", "NEL", "NELLA", "CON", "PER", "SU", "SUL",
            "TRA", "FRA", "CHE", "MA", "SE", "NON", "MI", "TI", "CI", "SI", "THE", "AN", "OF", "TO", "ON", "AND",
            "OR", "FOR", "WITH", "AT", "BY", "MY", "IS"}
FONT_FILES: dict[str, Sequence[str]] = {
    "Impact": ("impact.ttf",), "Bahnschrift": ("bahnschrift.ttf",), "Arial Black": ("ariblk.ttf",),
    "Segoe UI Black": ("seguibl.ttf",), "Arial": ("arialbd.ttf", "arial.ttf"),
    "Anton": ("Anton-Regular.ttf",), "Bebas Neue": ("BebasNeue-Regular.ttf",), "Oswald": ("Oswald-Bold.ttf",),
    "DejaVu Sans": ("DejaVuSans-Bold.ttf",), "Liberation Sans": ("LiberationSans-Bold.ttf",),
}
HEADLINE_FONTS = ("Impact", "Anton", "Bebas Neue", "Oswald", "Arial Black", "Bahnschrift", "Segoe UI Black", "Arial",
                  "Liberation Sans", "DejaVu Sans")
TYPE_STYLES = {"impact": ("Impact", "Anton", "Bebas Neue"), "heavy": ("Arial Black", "Segoe UI Black", "Impact"),
               "condensed": ("Bahnschrift", "Oswald", "Impact")}

_APP: Any = None
_FONTS: dict[str, tuple[str, int]] = {}  # requested family -> (registered family, weight)


class RenderError(RuntimeError):
    pass


def canonical_layout(name: str) -> str:
    return LAYOUT_ALIASES.get(name, name)


def _font_dirs() -> list[Path]:
    dirs = []
    if sys.platform == "win32":
        dirs.append(Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts")
        local = os.environ.get("LOCALAPPDATA")
        if local:
            dirs.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
    elif sys.platform == "darwin":
        dirs += [Path("/System/Library/Fonts"), Path("/Library/Fonts"), Path.home() / "Library" / "Fonts"]
    else:
        dirs += [Path("/usr/share/fonts"), Path("/usr/local/share/fonts"), Path.home() / ".fonts"]
    return [d for d in dirs if d.is_dir()]


def ensure_qt(extra_fonts: Sequence[str | Path] = ()) -> None:
    """A QGuiApplication (offscreen when none exists) and the system fonts the layouts use (not bundled)."""
    global _APP
    from PySide6.QtGui import QFont, QFontDatabase, QGuiApplication

    if QGuiApplication.instance() is None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        _APP = QGuiApplication([sys.argv[0] if sys.argv else "recut"])
    wanted = list(FONT_FILES.items()) + [(Path(f).stem, (str(f),)) for f in extra_fonts]
    for family, files in wanted:
        if family in _FONTS:
            continue
        for name in files:
            candidates = [Path(name)] if Path(name).is_absolute() else [d / name for d in _font_dirs()]
            path = next((c for c in candidates if c.is_file()), None)
            if path is None:
                continue
            font_id = QFontDatabase.addApplicationFont(str(path))
            families = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
            if families:
                weight = int(QFont.Weight.Black) if family in ("Arial Black", "Segoe UI Black") else (
                    int(QFont.Weight.Bold) if family in ("Bahnschrift", "Arial", "Oswald", "DejaVu Sans",
                                                         "Liberation Sans") else int(QFont.Weight.Normal))
                _FONTS[family] = (families[0], weight)
                break


def available_fonts() -> list[str]:
    ensure_qt()
    return [f for f in HEADLINE_FONTS if f in _FONTS] + [f for f in _FONTS if f not in HEADLINE_FONTS]


def _font(preferred: Sequence[str], px: float) -> Any:
    from PySide6.QtGui import QFont

    for name in list(preferred) + list(HEADLINE_FONTS):
        if name in _FONTS:
            family, weight = _FONTS[name]
            font = QFont(family)
            font.setPixelSize(max(1, int(px)))
            font.setWeight(QFont.Weight(weight))
            font.setKerning(True)
            return font
    font = QFont()
    font.setPixelSize(max(1, int(px)))
    font.setBold(True)
    return font


# ---------------------------------------------------------------------------------------------------
# image helpers (numpy BGR <-> QImage)

def to_qimage(image: np.ndarray) -> Any:
    from PySide6.QtGui import QImage

    data = np.ascontiguousarray(image)
    h, w = data.shape[:2]
    if data.ndim == 3 and data.shape[2] == 4:
        return QImage(data.data, w, h, 4 * w, QImage.Format.Format_ARGB32).copy()
    return QImage(data.data, w, h, 3 * w, QImage.Format.Format_BGR888).copy()


def fit_box(box: Sequence[float] | None, frame_w: int, frame_h: int, aspect: float) -> tuple[int, int, int, int]:
    """Pixel crop (x, y, w, h) of `box` (normalized) grown to `aspect` and kept inside the frame."""
    if box is None:
        x, y, w, h = 0.0, 0.0, float(frame_w), float(frame_h)
    else:
        x, y, w, h = box[0] * frame_w, box[1] * frame_h, box[2] * frame_w, box[3] * frame_h
    cx, cy = x + w / 2, y + h / 2
    if w / max(h, 1e-6) < aspect:
        w = h * aspect
    else:
        h = w / aspect
    if w > frame_w:
        w, h = frame_w, frame_w / aspect
    if h > frame_h:
        h, w = frame_h, frame_h * aspect
    x = min(max(0.0, cx - w / 2), frame_w - w)
    y = min(max(0.0, cy - h / 2), frame_h - h)
    return int(round(x)), int(round(y)), max(1, int(round(w))), max(1, int(round(h)))


def mask_regions(image: np.ndarray, boxes: Sequence[Sequence[float]]) -> np.ndarray:
    """Remove normalized regions (the stream's own webcam overlay) by inpainting from the surroundings."""
    import cv2

    if not boxes:
        return image
    out = image.copy()
    h, w = out.shape[:2]
    small = cv2.resize(out, (max(8, w // 4), max(8, h // 4)), interpolation=cv2.INTER_AREA)
    mask = np.zeros(small.shape[:2], np.uint8)
    regions = []
    for x, y, bw, bh in boxes:
        left, top = max(0, int(x * w)), max(0, int(y * h))
        right, bottom = min(w, int((x + bw) * w)), min(h, int((y + bh) * h))
        if right - left > 2 and bottom - top > 2:
            regions.append((left, top, right, bottom))
            mask[top // 4:bottom // 4 + 2, left // 4:right // 4 + 2] = 255
    if not regions:
        return image
    filled = cv2.resize(cv2.inpaint(small, mask, 6, cv2.INPAINT_TELEA), (w, h), interpolation=cv2.INTER_CUBIC)
    filled = cv2.GaussianBlur(filled, (0, 0), 3.0)
    for left, top, right, bottom in regions:
        out[top:bottom, left:right] = filled[top:bottom, left:right]
    return out


def upscale(image: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """Resize with Lanczos and an edge-only unsharp mask when enlarging (small webcam/card crops)."""
    import cv2

    tw, th = size
    h, w = image.shape[:2]
    if tw <= w and th <= h:
        return cv2.resize(image, (tw, th), interpolation=cv2.INTER_AREA)
    out = cv2.resize(image, (tw, th), interpolation=cv2.INTER_LANCZOS4)
    factor = max(tw / max(1, w), th / max(1, h))
    soft = cv2.GaussianBlur(out, (0, 0), 0.9 * factor)
    edges = cv2.Canny(cv2.cvtColor(out, cv2.COLOR_BGR2GRAY), 40, 110)
    weight = cv2.GaussianBlur(edges.astype(np.float32) / 255.0, (0, 0), 1.5 * factor)[..., None]
    sharpened = cv2.addWeighted(out, 1.6, soft, -0.6, 0).astype(np.float32)
    mixed = out.astype(np.float32) * (1 - np.clip(weight * 1.5, 0, 1)) + sharpened * np.clip(weight * 1.5, 0, 1)
    return np.clip(mixed, 0, 255).astype(np.uint8)


def grade(image: np.ndarray, *, enhance: float = 1.0, blur: float = 0.0, dim: float = 1.0) -> np.ndarray:
    import cv2

    out = image
    if enhance != 1.0:
        hsv = cv2.cvtColor(out, cv2.COLOR_BGR2HSV).astype(np.float32)
        hsv[..., 1] = np.clip(hsv[..., 1] * (1.0 + 0.18 * (enhance - 1.0) / 0.1), 0, 255)
        out = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR)
        out = cv2.convertScaleAbs(out, alpha=1.0 + 0.6 * (enhance - 1.0), beta=-6 * (enhance - 1.0) / 0.1)
    if blur > 0:
        out = cv2.GaussianBlur(out, (0, 0), blur)
    if dim != 1.0:
        out = cv2.convertScaleAbs(out, alpha=dim, beta=0)
    return out


def prepare(image: np.ndarray, box: Sequence[float] | None, size: tuple[int, int], *, enhance: float = 1.0,
            blur: float = 0.0, dim: float = 1.0, masks: Sequence[Sequence[float]] = ()) -> np.ndarray:
    """Crop to the target aspect around box, resize with good filters, light grading for thumbnails."""
    image = mask_regions(image, masks)
    tw, th = size
    x, y, w, h = fit_box(box, image.shape[1], image.shape[0], tw / th)
    return grade(upscale(image[y:y + h, x:x + w], (tw, th)), enhance=enhance, blur=blur, dim=dim)


def sharpness(image: np.ndarray) -> float:
    import cv2

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    h, w = gray.shape[:2]
    if w > 640:
        gray = cv2.resize(gray, (640, max(1, round(h * 640 / w))), interpolation=cv2.INTER_AREA)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def region_sharpness(image: np.ndarray, box: Sequence[float] | None) -> float:
    """Sharpness of the 16:9 region a background shows, at native resolution (comparable to frame quality)."""
    x, y, w, h = fit_box(box, image.shape[1], image.shape[0], 16 / 9)
    return sharpness(image[y:y + h, x:x + w])


def face_box(image: np.ndarray) -> tuple[float, float, float, float] | None:
    from .webcam import detect_faces

    faces = detect_faces(image, min_rel=0.15)
    return max(faces, key=lambda b: b[2] * b[3]) if faces else None


def face_center(image: np.ndarray) -> tuple[float, float] | None:
    box = face_box(image)
    return None if box is None else (box[0] + box[2] / 2, box[1] + box[3] / 2)


def portrait_crop(image: np.ndarray, aspect: float, *, face_share: float = 0.42) -> tuple[np.ndarray, list[float] | None]:
    """Crop of a webcam image at `aspect` (w/h) where the face is large and sits on the upper third.

    Returns the crop and the face box normalized to the crop (None when no face is visible).
    """
    h, w = image.shape[:2]
    face = face_box(image)
    if face is None:
        bx, by, bw, bh = fit_box(None, w, h, aspect)
        return image[by:by + bh, bx:bx + bw], None
    fx, fy, fw, fh = face[0] * w, face[1] * h, face[2] * w, face[3] * h
    ch = min(float(h), fh / face_share)
    cw = ch * aspect
    if cw > w:
        cw, ch = float(w), w / aspect
    cx, cy = fx + fw / 2, fy + fh / 2
    x = min(max(0.0, cx - cw / 2), w - cw)
    y = min(max(0.0, cy - ch * 0.42), h - ch)
    xi, yi, cwi, chi = int(x), int(y), max(1, int(cw)), max(1, int(ch))
    crop = image[yi:yi + chi, xi:xi + cwi]
    rel = [(fx - xi) / cwi, (fy - yi) / chi, fw / cwi, fh / chi]
    return crop, rel


# ---------------------------------------------------------------------------------------------------
# text fitting

@dataclass
class FittedText:
    font_px: float
    lines: list[str]
    width: float
    height: float
    fits: bool


def _break_penalty(parts: Sequence[str]) -> float:
    """Typographic cost of a line split: dangling function words and orphans read as broken phrases."""
    penalty = 0.0
    for i, part in enumerate(parts):
        words = part.split()
        if i < len(parts) - 1 and words[-1].strip(".,!?:;\"'«»").upper() in DANGLING:
            penalty += 1.0
        if len(parts) > 1 and len(words) == 1 and len(words[0]) <= 2:
            penalty += 1.0
    return penalty


def wrap(words: Sequence[str], font: Any, max_width: float, max_lines: int, *,
         clean_only: bool = False) -> list[str] | None:
    """Best split of words into <= max_lines lines that fit max_width (None if impossible).

    The fewest lines without a dangling article/preposition or orphan win; among equal line counts the
    most balanced widths win. A split with a dangling word is used only when no clean split fits.
    """
    from PySide6.QtGui import QFontMetricsF

    metrics = QFontMetricsF(font)
    n = len(words)
    fallback: list[str] | None = None
    for lines in range(1, min(max_lines, n) + 1):
        best: tuple[float, list[str]] | None = None
        for cuts in _splits(n, lines):
            parts = [" ".join(words[a:b]) for a, b in zip((0,) + cuts, cuts + (n,))]
            widths = [metrics.horizontalAdvance(p) for p in parts]
            if max(widths) > max_width:
                continue
            score = _break_penalty(parts) * 1e6 + max(widths) + 0.15 * (max(widths) - min(widths))
            if best is None or score < best[0]:
                best = (score, parts)
        if best is None:
            continue
        if best[0] < 1e6:
            return best[1]
        if fallback is None and not clean_only:
            fallback = best[1]
    return fallback


def _splits(n: int, lines: int) -> list[tuple[int, ...]]:
    if lines == 1:
        return [()]
    out: list[tuple[int, ...]] = []

    def rec(start: int, left: int, acc: tuple[int, ...]) -> None:
        if left == 0:
            out.append(acc)
            return
        for cut in range(start + 1, n - left + 1):
            rec(cut, left - 1, acc + (cut,))
    rec(0, lines - 1, ())
    return out


def cap_height(font: Any) -> float:
    from PySide6.QtGui import QFontMetricsF

    metrics = QFontMetricsF(font)
    cap = metrics.capHeight()
    return cap if cap > 0 else metrics.ascent() * 0.80


def fit_text(text: str, fonts: Sequence[str], box_w: float, box_h: float, *, max_lines: int = 3,
             min_px: float = 24.0, max_px: float = 400.0, gap_ratio: float = 0.17,
             line_gap: float | None = None) -> FittedText:
    """Largest font size whose wrapped lines fit box_w x box_h (cap heights + gaps)."""
    from PySide6.QtGui import QFontMetricsF

    if line_gap is not None:  # v1 signature: line_gap = 0.78 + gap ratio
        gap_ratio = line_gap - 0.78
    words = text.split()
    if not words:
        return FittedText(0.0, [], 0.0, 0.0, True)
    clean = _fit(words, fonts, box_w, box_h, max_lines, min_px, max_px, gap_ratio, clean_only=True)
    if clean is not None and clean.font_px >= 0.0:
        loose = _fit(words, fonts, box_w, box_h, max_lines, min_px, max_px, gap_ratio, clean_only=False)
        # a clean break is worth up to 35% of size: "TENGO / O CAMBIO?" beats "TENGO O / CAMBIO?"
        if loose is None or clean.font_px >= 0.65 * loose.font_px:
            return clean
        return loose
    best = _fit(words, fonts, box_w, box_h, max_lines, min_px, max_px, gap_ratio, clean_only=False)
    if best is None:
        font = _font(fonts, min_px)
        lines = wrap(words, font, box_w, max_lines) or [" ".join(words)]
        metrics = QFontMetricsF(font)
        return FittedText(min_px, lines, max(metrics.horizontalAdvance(line) for line in lines),
                          cap_height(font) * len(lines), False)
    return best


def _fit(words: Sequence[str], fonts: Sequence[str], box_w: float, box_h: float, max_lines: int, min_px: float,
         max_px: float, gap_ratio: float, *, clean_only: bool) -> FittedText | None:
    from PySide6.QtGui import QFontMetricsF

    lo, hi, best = min_px, max_px, None
    for _ in range(18):
        mid = (lo + hi) / 2
        font = _font(fonts, mid)
        lines = wrap(words, font, box_w, max_lines, clean_only=clean_only)
        cap = cap_height(font)
        height = cap * len(lines) + cap * gap_ratio * (len(lines) - 1) if lines else 1e9
        if lines and height <= box_h:
            metrics = QFontMetricsF(font)
            best = FittedText(mid, lines, max(metrics.horizontalAdvance(line) for line in lines), height, True)
            lo = mid
        else:
            hi = mid
    return best


# ---------------------------------------------------------------------------------------------------
# plan + result

@dataclass
class RenderPlan:
    layout: str
    headline: str
    background: np.ndarray
    focus_box: list[float] | None = None
    webcam: np.ndarray | None = None
    inset: np.ndarray | None = None
    inset_box: list[float] | None = None
    cards: list[np.ndarray] = field(default_factory=list)
    emphasis: str | None = None
    badge: str | None = None
    accent: str = "#FFD400"
    text_color: str = "#FFFFFF"
    text_side: str = "left"  # left | right | top | bottom
    webcam_side: str = "right"
    fonts: Sequence[str] = ()
    mask_boxes: list[list[float]] = field(default_factory=list)  # the stream's webcam overlay in the background
    webcam_scale: float = 1.0  # creator preference: 0.7 (smaller) .. 1.15 (larger)
    backdrop: float = 1.0  # strength of the darkening behind the text (raised by the quality gate)
    text_scale: float = 1.0  # max headline size factor (lowered by the quality gate)


@dataclass
class RenderResult:
    path: str
    width: int
    height: int
    bytes: int
    format: str
    layout: str
    text: dict[str, Any]
    warnings: list[str]
    subjects: list[dict[str, Any]] = field(default_factory=list)

    def to_mapping(self) -> dict[str, Any]:
        return dict(self.__dict__)


class ThumbnailRenderer:
    def __init__(self, *, size: tuple[int, int] = OUTPUT_SIZE, supersample: int = SUPERSAMPLE,
                 extra_fonts: Sequence[str | Path] = ()) -> None:
        if size[0] * 9 != size[1] * 16:
            raise ValueError("thumbnails are 16:9")
        self.size = size
        self.scale = max(1, int(supersample))
        ensure_qt(extra_fonts)

    # ---- public ------------------------------------------------------------------------------------
    def render(self, plan: RenderPlan, target: Path, *, fmt: str = "png") -> RenderResult:
        layout = canonical_layout(plan.layout)
        if layout not in LAYOUTS:
            raise RenderError(f"unknown layout {plan.layout}")
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QImage, QPainter

        W, H = self.size[0] * self.scale, self.size[1] * self.scale
        canvas = QImage(W, H, QImage.Format.Format_ARGB32_Premultiplied)
        canvas.fill(0xFF000000)
        painter = QPainter(canvas)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform |
                               QPainter.RenderHint.TextAntialiasing)
        warnings: list[str] = []
        self._subjects: list[dict[str, Any]] = []
        try:
            text_info, used = getattr(self, f"_layout_{layout}")(painter, plan, W, H, warnings)
        finally:
            painter.end()
        final = canvas.scaled(self.size[0], self.size[1], Qt.AspectRatioMode.IgnoreAspectRatio,
                              Qt.TransformationMode.SmoothTransformation).convertToFormat(QImage.Format.Format_RGB32)
        target.parent.mkdir(parents=True, exist_ok=True)
        fmt = fmt.lower()
        if fmt not in ("png", "jpg", "jpeg"):
            raise RenderError("format must be png or jpg")
        if fmt == "png":
            final.save(str(target), "PNG")  # type: ignore[call-overload]  # PySide6 wants str at runtime
            if target.stat().st_size > YOUTUBE_MAX_BYTES:  # YouTube rejects thumbnails over 2 MB
                jpeg = target.with_suffix(".jpg")
                self._save_jpeg(final, jpeg)
                warnings.append(f"PNG exceeded 2 MB; YouTube-ready JPEG written to {jpeg.name}")
        else:
            self._save_jpeg(final, target)
        text_info["px_at_320"] = round(text_info.get("cap_px", 0.0) * 320.0 / self.size[0], 1)  # cap height at 320 px
        if text_info.get("font_px") and text_info["px_at_320"] < 9.0:
            warnings.append("headline may be hard to read on small screens")
        subjects = [{**s, "rect": [round(v / self.scale, 1) for v in s["rect"]]} for s in self._subjects]
        return RenderResult(str(target), self.size[0], self.size[1], target.stat().st_size, fmt, used, text_info,
                            warnings, subjects)

    @staticmethod
    def _save_jpeg(image: Any, target: Path) -> None:
        for quality in (95, 90, 85, 80, 70):
            image.save(str(target), "JPEG", quality)
            if target.stat().st_size <= YOUTUBE_MAX_BYTES:
                return

    def _subject(self, kind: str, rect: Sequence[float], **extra: Any) -> None:
        self._subjects.append({"kind": kind, "rect": [float(v) for v in rect], **extra})

    # ---- drawing primitives ------------------------------------------------------------------------
    @staticmethod
    def _color(value: str, alpha: int = 255) -> Any:
        from PySide6.QtGui import QColor

        color = QColor(value)
        if not color.isValid():
            color = QColor("#FFD400")
        color.setAlpha(alpha)
        return color

    def _background(self, painter: Any, plan: RenderPlan, W: int, H: int, **kw: Any) -> np.ndarray:
        image = prepare(plan.background, plan.focus_box, (W, H), masks=plan.mask_boxes, **kw)
        painter.drawImage(0, 0, to_qimage(image))
        self._subject("background", (0, 0, W, H), sharpness=round(region_sharpness(plan.background, plan.focus_box), 1))
        return image

    def _vignette(self, painter: Any, W: int, H: int, strength: int = 110) -> None:
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QRadialGradient

        gradient = QRadialGradient(QPointF(W / 2, H / 2), math.hypot(W, H) / 1.6)
        gradient.setColorAt(0.55, self._color("#000000", 0))
        gradient.setColorAt(1.0, self._color("#000000", strength))
        painter.fillRect(0, 0, W, H, gradient)

    def _side_gradient(self, painter: Any, W: int, H: int, side: str, reach: float, strength: float) -> None:
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QLinearGradient

        strength = max(0, min(255, int(strength)))
        points = {"left": (QPointF(0, 0), QPointF(W * reach, 0)), "right": (QPointF(W, 0), QPointF(W * (1 - reach), 0)),
                  "top": (QPointF(0, 0), QPointF(0, H * reach)), "bottom": (QPointF(0, H), QPointF(0, H * (1 - reach)))}
        a, b = points[side]
        gradient = QLinearGradient(a, b)
        gradient.setColorAt(0.0, self._color("#000000", strength))
        gradient.setColorAt(0.5, self._color("#000000", int(strength * 0.6)))
        gradient.setColorAt(1.0, self._color("#000000", 0))
        painter.fillRect(0, 0, W, H, gradient)

    def _shadow(self, painter: Any, draw: Any, W: int, H: int, offset: tuple[float, float], radius: float,
                alpha: int = 170) -> None:
        """Soft shadow: draw the shape black on a layer, blur it by down/up-scaling, composite with an offset."""
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QImage, QPainter

        factor = max(1, int(radius / 2))
        layer = QImage(W // factor, H // factor, QImage.Format.Format_ARGB32_Premultiplied)
        layer.fill(0)
        p = QPainter(layer)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.scale(1 / factor, 1 / factor)
        draw(p, self._color("#000000", alpha))
        p.end()
        for _ in range(2):
            small = layer.scaled(max(1, layer.width() // 2), max(1, layer.height() // 2),
                                 Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
            layer = small.scaled(layer.width(), layer.height(), Qt.AspectRatioMode.IgnoreAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
        painter.drawImage(int(offset[0]), int(offset[1]), layer.scaled(W, H, Qt.AspectRatioMode.IgnoreAspectRatio,
                                                                       Qt.TransformationMode.SmoothTransformation))

    def _card(self, painter: Any, image: np.ndarray, center: tuple[float, float], height: float, *, rotate: float,
              W: int, H: int, kind: str = "card", max_width: float | None = None) -> None:
        """A real crop presented as an object: soft drop shadow, hairline light edge, rounded corners, no frame."""
        from PySide6.QtCore import QPointF, QRectF
        from PySide6.QtGui import QPainterPath, QPen

        aspect = image.shape[1] / max(1, image.shape[0])
        h = height
        w = h * aspect
        if max_width is not None and w > max_width:
            w, h = max_width, max_width / aspect
        prepared = to_qimage(grade(upscale(image, (int(w), int(h))), enhance=1.06))
        radius = min(w, h) * 0.04
        cx, cy = center

        def shape(p: Any, color: Any) -> None:
            p.save()
            p.translate(QPointF(cx, cy))
            p.rotate(rotate)
            path = QPainterPath()
            path.addRoundedRect(QRectF(-w / 2, -h / 2, w, h), radius, radius)
            p.fillPath(path, color)
            p.restore()

        self._shadow(painter, shape, W, H, (H * 0.012, H * 0.022), H * 0.03, alpha=200)
        painter.save()
        painter.translate(QPointF(cx, cy))
        painter.rotate(rotate)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(-w / 2, -h / 2, w, h), radius, radius)
        painter.setClipPath(clip)
        painter.drawImage(QRectF(-w / 2, -h / 2, w, h), prepared)
        painter.setClipping(False)
        painter.setPen(QPen(self._color("#FFFFFF", 200), max(1.5, H * 0.0025)))
        painter.drawPath(clip)
        painter.restore()
        self._subject(kind, (cx - w / 2, cy - h / 2, w, h))

    def _bubble(self, painter: Any, image: np.ndarray, cx: float, cy: float, d: float, accent: str, W: int, H: int) -> None:
        from PySide6.QtCore import QPointF, QRectF
        from PySide6.QtGui import QPainterPath, QPen

        crop, face = portrait_crop(image, 1.0, face_share=0.5)
        prepared = to_qimage(grade(upscale(crop, (int(d), int(d))), enhance=1.05))

        def shape(p: Any, color: Any) -> None:
            path = QPainterPath()
            path.addEllipse(QPointF(cx, cy), d / 2, d / 2)
            p.fillPath(path, color)

        self._shadow(painter, shape, W, H, (d * 0.02, d * 0.04), d * 0.06, alpha=190)
        clip = QPainterPath()
        clip.addEllipse(QPointF(cx, cy), d / 2, d / 2)
        painter.save()
        painter.setClipPath(clip)
        painter.drawImage(QRectF(cx - d / 2, cy - d / 2, d, d), prepared)
        painter.restore()
        painter.setPen(QPen(self._color(accent), max(2.0, d * 0.025)))
        painter.drawEllipse(QPointF(cx, cy), d / 2, d / 2)
        self._subject("webcam", (cx - d / 2, cy - d / 2, d, d), face=face is not None)

    def _webcam_side_panel(self, painter: Any, image: np.ndarray, *, right: bool, W: int, H: int,
                           scale: float) -> tuple[float, float]:
        """The face as the dominant subject: full-height portrait on one side, inner edge feathered into the scene."""
        import cv2
        from PySide6.QtCore import QRectF

        pw = min(W * 0.58, W * 0.46 * scale)
        ph = float(H)
        crop, face = portrait_crop(image, pw / ph, face_share=0.40)
        panel = grade(upscale(crop, (int(pw), int(ph))), enhance=1.05)
        alpha = np.full((int(ph), int(pw)), 255, np.float32)
        feather = int(pw * 0.22)
        ramp = np.linspace(0.0, 1.0, feather, dtype=np.float32) ** 1.4 * 255
        if right:
            alpha[:, :feather] = ramp
        else:
            alpha[:, -feather:] = ramp[::-1]
        bottom = int(ph * 0.12)  # soft fade at the bottom so the shoulders do not end on a hard line
        alpha[-bottom:, :] *= np.linspace(1.0, 0.65, bottom, dtype=np.float32)[:, None]
        bgra = np.dstack([panel, alpha.astype(np.uint8)])
        rgba = cv2.cvtColor(bgra, cv2.COLOR_BGRA2RGBA)
        from PySide6.QtGui import QImage

        qimage = QImage(rgba.data, rgba.shape[1], rgba.shape[0], 4 * rgba.shape[1],
                        QImage.Format.Format_RGBA8888).copy()
        x = W - pw if right else 0.0
        painter.drawImage(QRectF(x, 0, pw, ph), qimage)
        face_rect = None
        if face is not None:
            face_rect = [x + face[0] * pw, face[1] * ph, face[2] * pw, face[3] * ph]
            self._subject("face", face_rect)
        self._subject("webcam", (x, 0, pw, ph), face=face is not None)
        return x, pw

    def _headline(self, painter: Any, plan: RenderPlan, box: tuple[float, float, float, float], *, align: str,
                  max_lines: int, W: int, H: int, warnings: list[str], max_px: float | None = None,
                  valign: str = "center") -> dict[str, Any]:
        """Auto-fitted, outlined, shadowed uppercase headline; the emphasis word takes the accent colour."""
        from PySide6.QtCore import QPointF, Qt
        from PySide6.QtGui import QFontMetricsF, QPainterPath, QPainterPathStroker, QPen

        x, y, w, h = box
        pad = H * 0.012  # the outline and shadow extend beyond the glyphs
        x, y, w, h = x + pad, y + pad, max(1.0, w - 2 * pad), max(1.0, h - 2 * pad)
        text = " ".join(plan.headline.upper().split())
        fonts = list(plan.fonts)
        gap_ratio = 0.30 if any(c in ACCENTED_CAPS for c in text) else 0.18
        top_px = (max_px or H * 0.30) * plan.text_scale
        reserve = 0.24 if any(c in ACCENTED_CAPS for c in text) else 0.0  # accent room above the first line
        fitted = fit_text(text, fonts, w, h / (1 + reserve * 0.5), max_lines=max_lines, min_px=H * 0.06,
                          max_px=top_px, gap_ratio=gap_ratio)
        if not fitted.fits:
            warnings.append("headline did not fit at the minimum size: shorten thumbnail_text")
        font = _font(fonts, fitted.font_px)
        metrics = QFontMetricsF(font)
        cap = cap_height(font)
        gap = cap * gap_ratio
        total = cap * len(fitted.lines) + gap * (len(fitted.lines) - 1)
        accent_room = cap * 0.24 if fitted.lines and any(c in ACCENTED_CAPS for c in fitted.lines[0]) else 0.0
        top = y + (h - total) / 2 if valign == "center" else (y if valign == "top" else y + h - total)
        top = max(top, y + accent_room)
        emphasis = {e.strip().upper() for e in (plan.emphasis or "").split()} if plan.emphasis else set()
        normal, accented = QPainterPath(), QPainterPath()
        space = metrics.horizontalAdvance(" ")
        for i, line in enumerate(fitted.lines):
            width = metrics.horizontalAdvance(line)
            left = x if align == "left" else (x + w - width if align == "right" else x + (w - width) / 2)
            baseline = top + cap * (i + 1) + gap * i
            cursor = left
            for word in line.split(" "):
                target = accented if word.strip(".,!?:;\"'«»").upper() in emphasis else normal
                target.addText(QPointF(cursor, baseline), font, word)
                cursor += metrics.horizontalAdvance(word) + space
        both = QPainterPath(normal)
        both.addPath(accented)
        outline_w = max(2.0, fitted.font_px * 0.085)

        def shadow_shape(p: Any, color: Any) -> None:
            stroker = QPainterPathStroker()
            stroker.setWidth(outline_w * 2.2)
            p.fillPath(stroker.createStroke(both).united(both), color)

        self._shadow(painter, shadow_shape, W, H, (fitted.font_px * 0.025, fitted.font_px * 0.05),
                     fitted.font_px * 0.12, alpha=210)
        pen = QPen(self._color("#000000"), outline_w)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.strokePath(both, pen)
        painter.fillPath(normal, self._color(plan.text_color))
        painter.fillPath(accented, self._color(plan.accent))
        bounds = both.boundingRect().adjusted(-outline_w, -outline_w, outline_w, outline_w)
        inside = bounds.left() >= W * SAFE * 0.5 and bounds.top() >= H * SAFE * 0.5 and \
            bounds.right() <= W * (1 - SAFE * 0.5) and bounds.bottom() <= H * (1 - SAFE * 0.5)
        in_timestamp = bounds.right() > W * TIMESTAMP_ZONE[0] and bounds.bottom() > H * TIMESTAMP_ZONE[1]
        if not inside:
            warnings.append("headline touches the safe margin")
        if in_timestamp:
            warnings.append("headline overlaps the YouTube duration badge area")
        rect = (bounds.left(), bounds.top(), bounds.width(), bounds.height())
        return {"font_px": round(fitted.font_px / self.scale, 1), "cap_px": round(cap / self.scale, 1),
                "lines": fitted.lines, "fits": fitted.fits and inside,
                "bounds": [round(v / self.scale, 1) for v in rect], "timestamp_zone_clear": not in_timestamp,
                "align": align}

    def _badge(self, painter: Any, text: str, x: float, y: float, H: int, accent: str) -> tuple[float, float]:
        from PySide6.QtCore import QPointF, QRectF
        from PySide6.QtGui import QFontMetricsF, QPainterPath

        font = _font(("Arial Black", "Segoe UI Black", "Impact"), H * 0.055)
        metrics = QFontMetricsF(font)
        label = text.upper()[:14]
        pad = H * 0.018
        w, h = metrics.horizontalAdvance(label) + pad * 2.4, cap_height(font) + pad * 2
        path = QPainterPath()
        path.addRoundedRect(QRectF(x, y, w, h), h * 0.18, h * 0.18)
        painter.fillPath(path, self._color(accent))
        text_path = QPainterPath()
        text_path.addText(QPointF(x + pad * 1.2, y + pad + cap_height(font)), font, label)
        painter.fillPath(text_path, self._color(_contrast_text(accent)))
        self._subject("badge", (x, y, w, h))
        return w, h

    # ---- archetypes --------------------------------------------------------------------------------
    def _layout_reaction_focus(self, painter: Any, plan: RenderPlan, W: int, H: int,
                               warnings: list[str]) -> tuple[dict[str, Any], str]:
        if plan.webcam is None:
            warnings.append("reaction_focus without webcam: rendered as story_focus")
            return self._layout_story_focus(painter, plan, W, H, warnings)
        right = plan.webcam_side != "left"
        self._background(painter, plan, W, H, enhance=1.08, blur=W * 0.0015, dim=0.82)
        self._side_gradient(painter, W, H, "left" if right else "right", 0.70, 200 * plan.backdrop)
        self._vignette(painter, W, H, 120)
        x, pw = self._webcam_side_panel(painter, plan.webcam, right=right, W=W, H=H, scale=plan.webcam_scale)
        free_x = W * SAFE if right else x + pw * 0.95
        free_w = (x + pw * 0.08 - W * SAFE) if right else (W * (1 - SAFE) - free_x)
        element = plan.cards[0] if plan.cards else (
            _crop_box(plan.inset, plan.inset_box) if plan.inset is not None and plan.inset_box is not None
            else plan.inset)
        top = H * SAFE
        if plan.badge:
            _, bh = self._badge(painter, plan.badge, free_x, top, H, plan.accent)
            top += bh + H * 0.025
        text_bottom = H * (0.56 if element is not None else 0.80)
        info = self._headline(painter, plan, (free_x, top, free_w, text_bottom - top),
                              align="left" if right else "right", max_lines=3, W=W, H=H, warnings=warnings,
                              max_px=H * 0.26, valign="top")
        if element is not None:
            text_end = (info["bounds"][1] + info["bounds"][3]) * self.scale
            space_top = text_end + H * 0.04
            eh = min(H * 0.34, H * 0.93 - space_top)
            if eh >= H * 0.16:
                aspect = element.shape[1] / max(1, element.shape[0])
                ew = min(eh * aspect, free_w * 0.62)
                eh = ew / aspect
                # on the left side hug the text column; on the right side stay left of the duration badge
                cx = free_x + ew / 2 + H * 0.01 if right else min(free_x + free_w - ew / 2, W * 0.78 - ew / 2)
                self._card(painter, element, (cx, space_top + eh / 2), eh, rotate=-3.0 if right else 3.0,
                           W=W, H=H, kind="element", max_width=ew)
        return info, "reaction_focus"

    def _layout_board_focus(self, painter: Any, plan: RenderPlan, W: int, H: int,
                            warnings: list[str]) -> tuple[dict[str, Any], str]:
        cards = plan.cards[:2]
        band = "top" if plan.text_side == "top" else "bottom"
        if cards:
            # cards are the subject: the scene is pushed back so the crops read at a glance
            self._background(painter, plan, W, H, enhance=1.05, blur=W * 0.004, dim=0.55)
            self._vignette(painter, W, H, 150)
            text_left = plan.text_side != "right"
            area_x = W * 0.46 if text_left else W * SAFE
            area_w = W * (1 - SAFE) - area_x if text_left else W * 0.50
            ch = H * (0.74 if len(cards) == 1 else 0.66)
            for n, card in enumerate(cards):
                aspect = card.shape[1] / max(1, card.shape[0])
                w = min(ch * aspect, area_w / len(cards) * 0.92)
                cx = area_x + area_w * ((n + 0.5) / len(cards))
                self._card(painter, card, (cx, H * 0.47), w / aspect, rotate=(-4.0 + 8.0 * n) if len(cards) > 1 else -2.5,
                           W=W, H=H)
            text_w = W * 0.40
            text_x = W * SAFE if text_left else W * (1 - SAFE) - text_w
            box = (text_x, H * SAFE, text_w, H * 0.58)
            info = self._headline(painter, plan, box, align="left" if text_left else "right", max_lines=3, W=W, H=H,
                                  warnings=warnings, max_px=H * 0.30, valign="top")
            if plan.webcam is not None:
                d = H * 0.26 * plan.webcam_scale
                self._bubble(painter, plan.webcam, text_x + d / 2, H * 0.80 - d / 2, d, plan.accent, W, H)
            return info, "board_focus"
        self._background(painter, plan, W, H, enhance=1.12)
        self._side_gradient(painter, W, H, band, 0.48, 230 * plan.backdrop)
        self._vignette(painter, W, H, 100)
        if plan.inset is not None:
            source = plan.inset if plan.inset_box is None else _crop_box(plan.inset, plan.inset_box)
            ih = H * 0.36
            cx = W * (1 - SAFE) - ih * source.shape[1] / max(1, source.shape[0]) / 2
            cy = H * SAFE + ih / 2 if band == "bottom" else H * 0.80 - ih / 2
            self._card(painter, source, (cx, cy), ih, rotate=2.5, W=W, H=H, kind="element", max_width=W * 0.36)
        if plan.webcam is not None:
            d = H * 0.24 * plan.webcam_scale
            cy = H * SAFE + d / 2 if band == "bottom" else H * 0.80 - d / 2
            self._bubble(painter, plan.webcam, W * SAFE + d / 2, cy, d, plan.accent, W, H)
        top = H * 0.58 if band == "bottom" else H * SAFE
        box_h = H * 0.34
        if plan.badge:
            bw, bh = self._badge(painter, plan.badge, W * SAFE, top - H * 0.085 if band == "bottom" else top, H,
                                 plan.accent)
            if band == "top":
                top += bh + H * 0.02
                box_h -= bh + H * 0.02
        info = self._headline(painter, plan, (W * SAFE, top, W * 0.70, box_h), align="left", max_lines=2, W=W, H=H,
                              warnings=warnings, valign="bottom" if band == "bottom" else "top", max_px=H * 0.22)
        return info, "board_focus"

    def _layout_story_focus(self, painter: Any, plan: RenderPlan, W: int, H: int,
                            warnings: list[str]) -> tuple[dict[str, Any], str]:
        side = "right" if plan.text_side == "right" else "left"
        if plan.inset is not None:
            return self._story_split(painter, plan, W, H, warnings), "story_focus"
        self._background(painter, plan, W, H, enhance=1.12)
        self._side_gradient(painter, W, H, side, 0.78, 235 * plan.backdrop)
        self._vignette(painter, W, H, 120)
        if plan.webcam is not None:
            d = H * 0.32 * plan.webcam_scale
            cx = W * (1 - SAFE) - d / 2 if side == "left" else W * SAFE + d / 2
            self._bubble(painter, plan.webcam, cx, H * SAFE + d / 2 + H * 0.02, d, plan.accent, W, H)
        text_x = W * SAFE if side == "left" else W * 0.45
        top = H * SAFE
        if plan.badge:
            _, bh = self._badge(painter, plan.badge, text_x, top, H, plan.accent)
            top += bh + H * 0.03
        info = self._headline(painter, plan, (text_x, top, W * 0.55 - W * SAFE, H * 0.80 - top), align=side,
                              max_lines=3, W=W, H=H, warnings=warnings, max_px=H * 0.30)
        return info, "story_focus"

    def _story_split(self, painter: Any, plan: RenderPlan, W: int, H: int, warnings: list[str]) -> dict[str, Any]:
        """Two moments separated by a slanted accent seam; the headline spans the top."""
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QPainterPath, QPen, QPolygonF

        assert plan.inset is not None
        left = prepare(plan.background, plan.focus_box, (W, H), masks=plan.mask_boxes, enhance=1.1)
        right = prepare(plan.inset, plan.inset_box, (W, H), masks=plan.mask_boxes, enhance=1.1)
        painter.drawImage(0, 0, to_qimage(left))
        seam_top, seam_bottom = W * 0.58, W * 0.46
        clip = QPainterPath()
        clip.addPolygon(QPolygonF([QPointF(seam_top, 0), QPointF(W, 0), QPointF(W, H), QPointF(seam_bottom, H)]))
        painter.save()
        painter.setClipPath(clip)
        painter.drawImage(0, 0, to_qimage(right))
        painter.restore()
        painter.setPen(QPen(self._color(plan.accent), W * 0.006))
        painter.drawLine(QPointF(seam_top, 0), QPointF(seam_bottom, H))
        self._subject("background", (0, 0, seam_top, H),
                      sharpness=round(region_sharpness(plan.background, plan.focus_box), 1))
        self._subject("inset", (seam_bottom, 0, W - seam_bottom, H),
                      sharpness=round(region_sharpness(plan.inset, plan.inset_box), 1))
        self._side_gradient(painter, W, H, "top", 0.45, 220 * plan.backdrop)
        self._vignette(painter, W, H, 110)
        top = H * SAFE
        if plan.badge:
            _, bh = self._badge(painter, plan.badge, W * SAFE, top, H, plan.accent)
            top += bh + H * 0.02
        return self._headline(painter, plan, (W * SAFE, top, W * (1 - 2 * SAFE), H * 0.36), align="center",
                              max_lines=2, W=W, H=H, warnings=warnings, max_px=H * 0.24, valign="top")


def _crop_box(image: np.ndarray, box: Sequence[float]) -> np.ndarray:
    h, w = image.shape[:2]
    x, y, bw, bh = box
    crop = image[int(y * h):int((y + bh) * h), int(x * w):int((x + bw) * w)]
    return crop if crop.size else image


def _contrast_text(hex_color: str) -> str:
    value = hex_color.lstrip("#")
    try:
        r, g, b = (int(value[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return "#000000"
    return "#000000" if 0.299 * r + 0.587 * g + 0.114 * b > 150 else "#FFFFFF"


def plan_from_mapping(plan: Mapping[str, Any], images: Mapping[str, np.ndarray], *, fonts: Sequence[str] = (),
                      mask_boxes: Sequence[Sequence[float]] = (), style: Mapping[str, Any] | None = None
                      ) -> RenderPlan:
    """Resolve a validated composition plan (ids) into images; KeyError when an id has no image."""
    composition = plan["composition_plan"]
    style = style or {}
    tuning = composition.get("render_tuning") or {}
    type_fonts = TYPE_STYLES.get(str(style.get("typography") or ""), ())
    return RenderPlan(
        layout=canonical_layout(composition["layout"]), headline=plan["thumbnail_text"],
        background=images[composition["background_frame_id"]], focus_box=composition.get("focus_box"),
        webcam=images[plan["webcam_frame_id"]] if plan.get("webcam_frame_id") else None,
        inset=images[composition["inset_id"]] if composition.get("inset_id") else None,
        inset_box=composition.get("inset_box"),
        cards=[images[c] for c in plan.get("card_asset_ids", []) or []],
        emphasis=composition.get("emphasis_word"), badge=composition.get("badge"),
        accent=composition.get("accent_color") or "#FFD400", text_color=composition.get("text_color") or "#FFFFFF",
        text_side=composition.get("text_side") or "left", webcam_side=composition.get("webcam_side") or "right",
        fonts=list(fonts) + list(type_fonts), mask_boxes=[list(b) for b in mask_boxes],
        webcam_scale=float(tuning.get("webcam_scale", style.get("webcam_scale", 1.0))),
        backdrop=float(tuning.get("backdrop", 1.0)), text_scale=float(tuning.get("text_scale", 1.0)))
