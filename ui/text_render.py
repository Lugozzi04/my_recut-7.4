"""Draw a text overlay exactly the same way for the preview and for the export (one renderer, no surprises).

Qt only (QPainterPath): outline, hard 3D shadow and background box like the cover templates. Safe to call from
a worker thread (QImage painting), but fonts are registered once per process.
"""
from __future__ import annotations

import threading
from collections import OrderedDict

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QFontMetricsF,
    QImage,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
)

from core.text_overlays import FONTS, TextOverlay
from utils.runtime_paths import resource_path

_lock = threading.Lock()
_loaded: dict[str, bool] = {}


def ensure_fonts() -> dict[str, bool]:
    """Register the bundled fonts with Qt (needs a QGuiApplication). Returns key -> available."""
    with _lock:
        if _loaded:
            return dict(_loaded)
        for key, (_family, _style, filename) in FONTS.items():
            path = resource_path("fonts", filename)
            ok = path.is_file() and QFontDatabase.addApplicationFont(str(path)) >= 0
            _loaded[key] = bool(ok)
        return dict(_loaded)


def qfont(key: str, pixel_size: int) -> QFont:
    ensure_fonts()
    family, style, _file = FONTS.get(key, FONTS["luckiest_guy"])
    font = QFontDatabase.font(family, style, 12)
    if font.family() != family:  # font missing: keep a bold, readable fallback
        font = QFont("Arial")
        font.setBold(True)
    font.setPixelSize(max(4, int(pixel_size)))
    font.setHintingPreference(QFont.PreferNoHinting)
    return font


def _text_path(ov: TextOverlay, font: QFont) -> QPainterPath:
    fm = QFontMetricsF(font)
    lines = ov.display_text().split("\n") or [""]
    widths = [fm.horizontalAdvance(line) for line in lines]
    max_w = max(widths) if widths else 0.0
    line_h = fm.ascent() + fm.descent() * 0.6
    path = QPainterPath()
    for i, (line, w) in enumerate(zip(lines, widths)):
        path.addText(QPointF((max_w - w) / 2.0, fm.ascent() + i * line_h), font, line)
    return path


def render_overlay(ov: TextOverlay, frame_w: int, frame_h: int, opacity: float = 1.0) -> tuple[QImage, int, int]:
    """Render one overlay for a frame of frame_w x frame_h. Returns (image cropped to the text, x, y)."""
    frame_w, frame_h = max(16, int(frame_w)), max(16, int(frame_h))
    px = max(6.0, ov.size * frame_h)
    font = qfont(ov.font, int(round(px)))
    path = _text_path(ov, font)
    if path.isEmpty():
        return QImage(), 0, 0
    tb = path.boundingRect()
    outline_w = px * ov.outline_width if ov.outline else 0.0
    shadow_d = px * 0.07 if ov.shadow else 0.0
    pad_x = px * 0.42 if ov.box else 0.0
    pad_y = px * 0.22 if ov.box else 0.0
    box_rect = tb.adjusted(-pad_x, -pad_y, pad_x, pad_y)
    content = box_rect.adjusted(-outline_w, -outline_w, outline_w + shadow_d, outline_w + shadow_d)
    margin = 2.0
    img_w = int(content.width() + 2 * margin) + 1
    img_h = int(content.height() + 2 * margin) + 1
    # Anchor the block centre on (x, y), then keep it inside the frame.
    left = ov.x * frame_w - content.width() / 2.0
    top = ov.y * frame_h - content.height() / 2.0
    left = max(0.0, min(frame_w - content.width(), left))
    top = max(0.0, min(frame_h - content.height(), top))
    img = QImage(max(1, img_w), max(1, img_h), QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    try:
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        p.setOpacity(max(0.0, min(1.0, opacity)))
        p.translate(margin - content.left(), margin - content.top())
        if ov.box:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(ov.box))
            radius = px * 0.16
            p.drawRoundedRect(box_rect, radius, radius)
        outline_path = QPainterPath()
        if outline_w > 0:
            stroker = QPainterPathStroker()
            stroker.setWidth(outline_w * 2.0)
            stroker.setJoinStyle(Qt.RoundJoin)
            stroker.setCapStyle(Qt.RoundCap)
            outline_path = stroker.createStroke(path).united(path)
        if shadow_d > 0:
            shadow = QColor(0, 0, 0, 215)
            base = outline_path if not outline_path.isEmpty() else path
            p.setPen(Qt.NoPen)
            p.setBrush(shadow)
            steps = max(1, int(shadow_d))
            for i in range(1, steps + 1):  # solid extrusion, like the covers' 3D shadow
                p.drawPath(base.translated(i * shadow_d / steps, i * shadow_d / steps))
        if not outline_path.isEmpty():
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(ov.outline))
            p.drawPath(outline_path)
        p.setPen(QPen(Qt.NoPen))
        p.setBrush(QColor(ov.color))
        p.drawPath(path)
    finally:
        p.end()
    x = int(round(left - margin + 0.0))
    y = int(round(top - margin + 0.0))
    return img, x, y


class OverlayCache:
    """Preview cache: re-render only when the text, its style or the frame size changes."""

    def __init__(self, limit: int = 32) -> None:
        self._items: OrderedDict[tuple, tuple[QImage, int, int]] = OrderedDict()
        self._limit = limit

    def get(self, ov: TextOverlay, frame_w: int, frame_h: int) -> tuple[QImage, int, int]:
        key = (ov.visual_key(), int(frame_w), int(frame_h))
        hit = self._items.get(key)
        if hit is None:
            hit = render_overlay(ov, frame_w, frame_h)
            self._items[key] = hit
            while len(self._items) > self._limit:
                self._items.popitem(last=False)
        else:
            self._items.move_to_end(key)
        return hit


def overlay_rect(ov: TextOverlay, frame_w: int, frame_h: int, cache: OverlayCache | None = None) -> QRectF:
    img, x, y = (cache or OverlayCache(1)).get(ov, frame_w, frame_h)
    return QRectF(x, y, img.width(), img.height())
