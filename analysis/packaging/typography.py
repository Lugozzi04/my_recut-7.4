"""Deterministic exact text over a text-free image; reuses the existing Qt font/wrapping primitives."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence
import hashlib

import numpy as np

from .frames import read_image, write_image, require_cv2
from .render import ensure_qt, available_fonts, fit_text, _font, to_qimage

TEXT_PRESETS: dict[str, dict[str, Any]] = {
    "white_impact": {"color": "#FFFFFF", "gradient": ["#FFFFFF", "#B5C1D1"], "outline": "#111420",
                     "outer_outline": "#394052", "depth_px": 5, "shadow_dx": 9, "shadow_dy": 12, "tilt": -2},
    "gold_impact": {"color": "#FFD436", "gradient": ["#FFF5B5", "#FFD436", "#E19414"], "outline": "#17131B",
                    "outer_outline": "#643D16", "depth_px": 7, "shadow_dx": 10, "shadow_dy": 14, "tilt": -3},
    "danger_red": {"color": "#FF514A", "gradient": ["#FFCA70", "#FF514A", "#C72135"], "outline": "#230F1E",
                   "outer_outline": "#651D30", "depth_px": 6, "shadow_dx": 10, "shadow_dy": 12, "tilt": 3},
    "energy_cyan": {"color": "#27C9FF", "gradient": ["#FFFFFF", "#63E8FF", "#1593DC"], "outline": "#102440",
                    "outer_outline": "#2385A8", "depth_px": 4, "shadow_dx": 7, "shadow_dy": 10, "tilt": -1},
}


def resolve_preset(typography: str = "", color: str = "", *, fallback: str = "white_impact") -> str:
    """Accept old free-form planner descriptions as well as new canonical preset IDs."""
    if typography in TEXT_PRESETS:
        return typography
    value = (typography + " " + color).casefold()
    for terms, preset in ((("cyan", "ciano", "energy"), "energy_cyan"),
                          (("rosso", "red", "arancio", "danger"), "danger_red"),
                          (("bianco", "white", "silver", "argento"), "white_impact"),
                          (("giallo", "yellow", "gold", "oro"), "gold_impact")):
        if any(term in value for term in terms):
            return preset
    return fallback


def apply_preset(style: Mapping[str, Any], preset: str) -> dict[str, Any]:
    if preset not in TEXT_PRESETS:
        raise ValueError("Unknown local typography preset")
    return {**style, **TEXT_PRESETS[preset], "preset": preset}


def normalize_canvas(image: np.ndarray, size: tuple[int, int] = (1920, 1080)) -> np.ndarray:
    """Contain rather than centre-crop: never remove a face, card or headline."""
    cv2 = require_cv2()
    h, w = image.shape[:2]
    ratio = min(size[0] / w, size[1] / h)
    fitted = cv2.resize(image, (max(1, round(w * ratio)), max(1, round(h * ratio))),
                        interpolation=cv2.INTER_AREA if ratio < 1 else cv2.INTER_CUBIC)
    out = np.full((size[1], size[0], 3), (32, 20, 17), np.uint8)
    x, y = (size[0] - fitted.shape[1]) // 2, (size[1] - fitted.shape[0]) // 2
    out[y:y + fitted.shape[0], x:x + fitted.shape[1]] = fitted[..., :3]
    return out


def compose(base: Path, target: Path, typography: Mapping[str, Any], *,
            size: tuple[int, int] = (1920, 1080), cache: Any = None) -> dict[str, Any]:
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QFontInfo, QLinearGradient, QBrush, QTransform
    from .cache import stable_key

    image = read_image(base)
    if image is None:
        raise ValueError("Cannot read typography base image")
    ensure_qt([f for f in typography.get("fonts", []) if Path(f).suffix.lower() in (".ttf", ".otf")])
    key = stable_key("typography_v41", hashlib.sha256(base.read_bytes()).hexdigest(), typography, size,
                     available_fonts())
    if cache is not None:
        meta = cache.load_json("render", key)
        if meta and cache.load_file("render", key, ".png", target):
            return {**meta, "cache_hit": True}
    image = normalize_canvas(image, size)
    text = str(typography.get("text") or "")
    if typography.get("mode") != "precision" or not text:
        write_image(target, image)
        return {"mode": typography.get("mode", "none"), "text_applied": False, "warnings": []}
    x, y, w, h = [float(v) for v in typography["box"]]
    if not (.025 <= x < .975 and .025 <= y < .975 and w > 0 and h > 0 and
            x + w <= .975 and y + h <= .975):
        raise ValueError("Text box must be inside the canvas safe margins")
    W, H = size
    outline = float(typography.get("outline_px", 12)) * W / 1920
    shadow = float(typography.get("shadow_px", 8)) * W / 1920
    depth = min(12., max(0., float(typography.get("depth_px", 0)))) * W / 1920
    tilt = min(6., max(-6., float(typography.get("tilt", 0))))
    shadow_dx = float(typography.get("shadow_dx", typography.get("shadow_px", 8))) * W / 1920
    shadow_dy = float(typography.get("shadow_dy", typography.get("shadow_px", 8))) * W / 1920
    pad = outline * 1.5 + max(abs(shadow_dx), abs(shadow_dy)) + depth + 3
    box_w, box_h = w * W - pad * 2, h * H - pad * 2
    fonts: Sequence[str] = typography.get("fonts", ["Anton", "Impact"])
    requested_fonts = list(fonts)
    installed = available_fonts()
    if fonts and fonts[0] not in installed and "font_variant" in typography:
        fallbacks = [f for f in fonts if f in installed]
        if fallbacks:
            selected = fallbacks[int(typography["font_variant"]) % len(fallbacks)]
            fonts = [selected, *[f for f in fonts if f != selected]]
    fitted = fit_text(text, fonts, box_w, box_h * .85, max_lines=int(typography.get("max_lines", 2)),
                      min_px=18, max_px=float(typography.get("max_px", 230)) * W / 1920)
    if not fitted.fits:
        raise ValueError("Exact text cannot fit the reserved area; shorten it or enlarge the text area")
    font = _font(fonts, fitted.font_px)
    shape = QPainterPath()
    # Exact Unicode/case is retained. Wrapping only introduces line breaks.
    for i, line in enumerate(fitted.lines):
        shape.addText(QPointF(0, i * fitted.font_px * 1.20), font, line)
    bounds = shape.boundingRect()
    if tilt:
        shape = QTransform().rotate(tilt).map(shape)
        bounds = shape.boundingRect()
    if bounds.width() > box_w or bounds.height() > box_h:
        factor = min(box_w / max(1, bounds.width()), box_h / max(1, bounds.height()))
        shape = QTransform().scale(factor, factor).map(shape)
        bounds = shape.boundingRect()
    left = x * W + pad
    top = y * H + pad + (box_h - bounds.height()) / 2
    shape.translate(left - bounds.left(), top - bounds.top())
    qimage = to_qimage(image)
    painter = QPainter(qimage)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        stroke = QPen(QColor(str(typography.get("outline", "#111420"))), outline * 2)
        stroke.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.save()
        painter.translate(shadow_dx, shadow_dy)
        painter.strokePath(shape, QPen(QColor("#111420"), outline * 2))
        painter.fillPath(shape, QColor("#111420"))
        painter.restore()
        if typography.get("outer_outline"):
            outer = QPen(QColor(str(typography["outer_outline"])), outline * 2.7)
            outer.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.strokePath(shape, outer)
        for step in range(int(depth), 0, -1):
            painter.save()
            painter.translate(step * .6, step)
            painter.strokePath(shape, stroke)
            painter.fillPath(shape, QColor(str(typography.get("outline", "#111420"))))
            painter.restore()
        painter.strokePath(shape, stroke)
        stops = typography.get("gradient")
        if stops:
            b = shape.boundingRect()
            gradient = QLinearGradient(b.left(), b.top(), b.left(), b.bottom())
            for i, stop in enumerate(stops):
                gradient.setColorAt(i / max(1, len(stops) - 1), QColor(str(stop)))
            painter.fillPath(shape, QBrush(gradient))
        else:
            painter.fillPath(shape, QColor(str(typography.get("color", "#FFD436"))))
    finally:
        painter.end()
    target.parent.mkdir(parents=True, exist_ok=True)
    if not qimage.save(str(target), "PNG"):
        raise OSError("Cannot save precision typography")
    actual = QFontInfo(font).family()
    warnings = ["Gameplay/card fidelity and creator identity require visual review."]
    if requested_fonts and actual.casefold() != str(requested_fonts[0]).casefold():
        warnings.append(f"Font {requested_fonts[0]} unavailable: using {actual}")
    if fitted.font_px * 160 / W < 9:
        warnings.append("Text may be small in the 160×90 preview")
    yy, xx = int(y * H), int(x * W)
    gray = require_cv2().cvtColor(image[yy:int((y+h)*H), xx:int((x+w)*W)], require_cv2().COLOR_BGR2GRAY)
    color = QColor(str(typography.get("color", "#FFD436")))
    luminance = .2126 * color.red() + .7152 * color.green() + .0722 * color.blue()
    contrast = (max(luminance, float(gray.mean())) + 12.75) / (min(luminance, float(gray.mean())) + 12.75)
    if contrast < 3:
        warnings.append("Low approximate fill/background contrast; dark outline provides separation")
    b = shape.boundingRect()
    meta = {"mode": "precision", "text": text, "text_applied": True, "font": actual,
            "requested_font": requested_fonts[0] if requested_fonts else None,
            "font_px": fitted.font_px, "lines": fitted.lines, "bounds": [b.x(), b.y(), b.width(), b.height()],
            "preset": typography.get("preset", "legacy_solid"), "gradient": typography.get("gradient", []),
            "tilt": tilt, "depth_px": depth, "safe_margins": True,
            "contrast_proxy": round(contrast, 2), "exact_text": True, "warnings": warnings, "cache_hit": False}
    if cache is not None:
        cache.store_json("render", key, meta)
        cache.store_file("render", key, ".png", target)
    return meta


def preview_and_check(target: Path, preview: Path) -> dict[str, Any]:
    image = read_image(target)
    if image is None:
        raise ValueError("Unreadable final thumbnail")
    cv2 = require_cv2()
    write_image(preview, cv2.resize(image, (160, 90), interpolation=cv2.INTER_AREA))
    h, w = image.shape[:2]
    return {"integrity": True, "size": [w, h], "aspect_ok": abs(w / h - 16 / 9) < .001,
            "resolution_ok": (w, h) == (1920, 1080),
            "semantic_authenticity": "not_verified"}
