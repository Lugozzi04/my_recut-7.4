"""Painter-drawn icon set.

Every glyph is drawn on a 24×24 grid with the same stroke weight and round joins, then rendered
at 2× for crisp HiDPI output. Using one code-drawn family keeps transport, toolbar and inspector
icons visually consistent (the SVG assets in /icons came from different sources).
"""
from __future__ import annotations

import math
from typing import Callable

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

_Draw = Callable[[QPainter, QPen], None]
_REGISTRY: dict[str, _Draw] = {}
_CACHE: dict[tuple[str, int, str], QIcon] = {}

STROKE = 1.75


def _glyph(name: str):
    def deco(fn: _Draw) -> _Draw:
        _REGISTRY[name] = fn
        return fn
    return deco


def _fill(p: QPainter, pen: QPen, path: QPainterPath) -> None:
    p.setPen(Qt.NoPen)
    p.setBrush(pen.color())
    p.drawPath(path)
    p.setBrush(Qt.NoBrush)
    p.setPen(pen)


def _poly(points: list[tuple[float, float]], close: bool = True) -> QPainterPath:
    path = QPainterPath(QPointF(*points[0]))
    for pt in points[1:]:
        path.lineTo(QPointF(*pt))
    if close:
        path.closeSubpath()
    return path


def _rounded_triangle(points: list[tuple[float, float]], r: float = 1.4) -> QPainterPath:
    """Triangle with softened corners (play-style glyphs)."""
    path = QPainterPath()
    n = len(points)
    for i in range(n):
        p0 = QPointF(*points[i - 1])
        p1 = QPointF(*points[i])
        p2 = QPointF(*points[(i + 1) % n])
        v1 = p0 - p1
        v2 = p2 - p1
        l1 = math.hypot(v1.x(), v1.y()) or 1.0
        l2 = math.hypot(v2.x(), v2.y()) or 1.0
        a = p1 + v1 * (r / l1)
        b = p1 + v2 * (r / l2)
        if i == 0:
            path.moveTo(a)
        else:
            path.lineTo(a)
        path.quadTo(p1, b)
    path.closeSubpath()
    return path


@_glyph("play")
def _play(p, pen):
    _fill(p, pen, _rounded_triangle([(8, 5.5), (19, 12), (8, 18.5)]))


@_glyph("pause")
def _pause(p, pen):
    path = QPainterPath()
    path.addRoundedRect(QRectF(7, 5.5, 3.6, 13), 1.2, 1.2)
    path.addRoundedRect(QRectF(13.4, 5.5, 3.6, 13), 1.2, 1.2)
    _fill(p, pen, path)


@_glyph("prev_cut")
def _prev_cut(p, pen):
    _fill(p, pen, _rounded_triangle([(18, 6), (18, 18), (9, 12)], 1.2))
    p.drawLine(QPointF(6.5, 6), QPointF(6.5, 18))


@_glyph("next_cut")
def _next_cut(p, pen):
    _fill(p, pen, _rounded_triangle([(6, 6), (6, 18), (15, 12)], 1.2))
    p.drawLine(QPointF(17.5, 6), QPointF(17.5, 18))


@_glyph("back")
def _back(p, pen):
    _fill(p, pen, _rounded_triangle([(12, 7), (12, 17), (4.5, 12)], 1.0))
    _fill(p, pen, _rounded_triangle([(19.5, 7), (19.5, 17), (12, 12)], 1.0))


@_glyph("forward")
def _forward(p, pen):
    _fill(p, pen, _rounded_triangle([(4.5, 7), (4.5, 17), (12, 12)], 1.0))
    _fill(p, pen, _rounded_triangle([(12, 7), (12, 17), (19.5, 12)], 1.0))


@_glyph("select")
def _select(p, pen):
    path = _poly([(6, 4.5), (18, 12.5), (12.6, 13.6), (15.4, 19.2), (13.2, 20.2), (10.4, 14.6), (6.5, 18)])
    p.drawPath(path)


@_glyph("scissors")
def _scissors(p, pen):
    p.drawEllipse(QPointF(6.5, 17), 2.6, 2.6)
    p.drawEllipse(QPointF(17.5, 17), 2.6, 2.6)
    p.drawLine(QPointF(8.4, 15.2), QPointF(17.5, 4.5))
    p.drawLine(QPointF(15.6, 15.2), QPointF(6.5, 4.5))


@_glyph("split")
def _split(p, pen):
    p.drawLine(QPointF(12, 3.5), QPointF(12, 20.5))
    p.drawRoundedRect(QRectF(3.5, 8, 6, 8), 1.5, 1.5)
    p.drawRoundedRect(QRectF(14.5, 8, 6, 8), 1.5, 1.5)


def _arc_arrow(p: QPainter, pen: QPen, mirror: bool) -> None:
    p.save()
    if mirror:
        p.translate(24, 0)
        p.scale(-1, 1)
    path = QPainterPath(QPointF(5, 10))
    path.lineTo(QPointF(14, 10))
    path.cubicTo(QPointF(17.6, 10), QPointF(19.5, 12.4), QPointF(19.5, 14.75))
    path.cubicTo(QPointF(19.5, 17.1), QPointF(17.6, 19.5), QPointF(14, 19.5))
    path.lineTo(QPointF(10, 19.5))
    p.drawPath(path)
    p.drawPolyline([QPointF(8.8, 6.2), QPointF(5, 10), QPointF(8.8, 13.8)])
    p.restore()


@_glyph("undo")
def _undo(p, pen):
    _arc_arrow(p, pen, False)


@_glyph("redo")
def _redo(p, pen):
    _arc_arrow(p, pen, True)


def _magnifier(p: QPainter) -> None:
    p.drawEllipse(QPointF(10.5, 10.5), 5.8, 5.8)
    p.drawLine(QPointF(14.8, 14.8), QPointF(19.5, 19.5))


@_glyph("zoom_in")
def _zoom_in(p, pen):
    _magnifier(p)
    p.drawLine(QPointF(8, 10.5), QPointF(13, 10.5))
    p.drawLine(QPointF(10.5, 8), QPointF(10.5, 13))


@_glyph("zoom_out")
def _zoom_out(p, pen):
    _magnifier(p)
    p.drawLine(QPointF(8, 10.5), QPointF(13, 10.5))


@_glyph("fit")
def _fit(p, pen):
    for (x, y, dx, dy) in ((4, 9, 0, -5), (20, 9, 0, -5), (4, 15, 0, 5), (20, 15, 0, 5)):
        sx = 1 if x < 12 else -1
        p.drawPolyline([QPointF(x, y), QPointF(x, y + dy), QPointF(x + sx * 5, y + dy)])
    p.drawLine(QPointF(8.5, 12), QPointF(15.5, 12))


@_glyph("magnet")
def _magnet(p, pen):
    path = QPainterPath(QPointF(6, 5))
    path.lineTo(QPointF(6, 12))
    path.cubicTo(QPointF(6, 16.5), QPointF(8.8, 19.5), QPointF(12, 19.5))
    path.cubicTo(QPointF(15.2, 19.5), QPointF(18, 16.5), QPointF(18, 12))
    path.lineTo(QPointF(18, 5))
    p.drawPath(path)
    p.drawLine(QPointF(4.5, 8.5), QPointF(8.5, 8.5))
    p.drawLine(QPointF(15.5, 8.5), QPointF(19.5, 8.5))


def _speaker(p: QPainter, pen: QPen) -> None:
    path = _poly([(4, 9.5), (7.5, 9.5), (12, 5.5), (12, 18.5), (7.5, 14.5), (4, 14.5)])
    p.drawPath(path)


@_glyph("volume")
def _volume(p, pen):
    _speaker(p, pen)
    p.drawArc(QRectF(11, 8.5, 6, 7), -60 * 16, 120 * 16)
    p.drawArc(QRectF(11, 5.5, 10, 13), -60 * 16, 120 * 16)


@_glyph("mute")
def _mute(p, pen):
    _speaker(p, pen)
    p.drawLine(QPointF(15, 9.5), QPointF(20, 14.5))
    p.drawLine(QPointF(20, 9.5), QPointF(15, 14.5))


@_glyph("more")
def _more(p, pen):
    path = QPainterPath()
    for x in (6, 12, 18):
        path.addEllipse(QPointF(x, 12), 1.6, 1.6)
    _fill(p, pen, path)


@_glyph("import")
def _import(p, pen):
    p.drawLine(QPointF(12, 4), QPointF(12, 14.5))
    p.drawPolyline([QPointF(8, 10.5), QPointF(12, 14.5), QPointF(16, 10.5)])
    p.drawPolyline([QPointF(4.5, 14), QPointF(4.5, 19.5), QPointF(19.5, 19.5), QPointF(19.5, 14)])


@_glyph("export")
def _export(p, pen):
    p.drawLine(QPointF(12, 15), QPointF(12, 4.5))
    p.drawPolyline([QPointF(8, 8.5), QPointF(12, 4.5), QPointF(16, 8.5)])
    p.drawPolyline([QPointF(4.5, 14), QPointF(4.5, 19.5), QPointF(19.5, 19.5), QPointF(19.5, 14)])


@_glyph("save")
def _save(p, pen):
    p.drawPath(_poly([(5, 4.5), (16, 4.5), (19.5, 8), (19.5, 19.5), (5, 19.5)]))
    p.drawPolyline([QPointF(8.5, 4.5), QPointF(8.5, 8.5), QPointF(14.5, 8.5), QPointF(14.5, 4.5)])
    p.drawRoundedRect(QRectF(8, 13, 8.5, 6.5), 1, 1)


@_glyph("folder")
def _folder(p, pen):
    p.drawPath(_poly([(3.5, 6.5), (9.5, 6.5), (11.5, 8.5), (20.5, 8.5), (20.5, 18.5), (3.5, 18.5)]))


@_glyph("chevron_down")
def _chev_down(p, pen):
    p.drawPolyline([QPointF(7, 9.5), QPointF(12, 14.5), QPointF(17, 9.5)])


@_glyph("chevron_right")
def _chev_right(p, pen):
    p.drawPolyline([QPointF(9.5, 7), QPointF(14.5, 12), QPointF(9.5, 17)])


@_glyph("check")
def _check(p, pen):
    p.drawPolyline([QPointF(5.5, 12.5), QPointF(10, 17), QPointF(18.5, 7.5)])


@_glyph("film")
def _film(p, pen):
    p.drawRoundedRect(QRectF(4, 5.5, 16, 13), 2, 2)
    p.drawLine(QPointF(8, 5.5), QPointF(8, 18.5))
    p.drawLine(QPointF(16, 5.5), QPointF(16, 18.5))


@_glyph("wave")
def _wave(p, pen):
    for x, h in ((4.5, 3), (7.5, 8), (10.5, 13), (13.5, 7), (16.5, 11), (19.5, 4)):
        p.drawLine(QPointF(x, 12 - h / 2), QPointF(x, 12 + h / 2))


@_glyph("gamepad")
def _gamepad(p, pen):
    path = QPainterPath()
    path.addRoundedRect(QRectF(3.5, 7.5, 17, 10), 5, 5)
    p.drawPath(path)
    p.drawLine(QPointF(7.5, 12.5), QPointF(10.5, 12.5))
    p.drawLine(QPointF(9, 11), QPointF(9, 14))
    dots = QPainterPath()
    dots.addEllipse(QPointF(15, 11.5), 1.0, 1.0)
    dots.addEllipse(QPointF(17, 13.5), 1.0, 1.0)
    _fill(p, pen, dots)


@_glyph("trash")
def _trash(p, pen):
    p.drawLine(QPointF(4.5, 7), QPointF(19.5, 7))
    p.drawPolyline([QPointF(9.5, 7), QPointF(9.5, 4.5), QPointF(14.5, 4.5), QPointF(14.5, 7)])
    p.drawPath(_poly([(6.5, 7), (7.5, 19.5), (16.5, 19.5), (17.5, 7)], close=False))


@_glyph("restore")
def _restore(p, pen):
    path = QPainterPath()
    path.arcMoveTo(QRectF(5, 5, 14, 14), 150)
    path.arcTo(QRectF(5, 5, 14, 14), 150, -300)
    p.drawPath(path)
    p.drawPolyline([QPointF(4.2, 4.8), QPointF(5.9, 8.5), QPointF(9.7, 7.2)])


@_glyph("target")
def _target(p, pen):
    p.drawLine(QPointF(12, 3.5), QPointF(12, 20.5))
    _fill(p, pen, _rounded_triangle([(7.5, 3.5), (16.5, 3.5), (12, 8.5)], 1.0))


@_glyph("sparkle")
def _sparkle(p, pen):
    path = QPainterPath(QPointF(12, 3.5))
    path.cubicTo(QPointF(12.8, 9), QPointF(15, 11.2), QPointF(20.5, 12))
    path.cubicTo(QPointF(15, 12.8), QPointF(12.8, 15), QPointF(12, 20.5))
    path.cubicTo(QPointF(11.2, 15), QPointF(9, 12.8), QPointF(3.5, 12))
    path.cubicTo(QPointF(9, 11.2), QPointF(11.2, 9), QPointF(12, 3.5))
    _fill(p, pen, path)


@_glyph("plus")
def _plus(p, pen):
    p.drawLine(QPointF(12, 5.5), QPointF(12, 18.5))
    p.drawLine(QPointF(5.5, 12), QPointF(18.5, 12))


@_glyph("keyboard")
def _keyboard(p, pen):
    p.drawRoundedRect(QRectF(3.5, 6.5, 17, 11), 2, 2)
    dots = QPainterPath()
    for x in (7, 10, 13, 16):
        dots.addRect(QRectF(x - 0.8, 9.2, 1.6, 1.6))
    _fill(p, pen, dots)
    p.drawLine(QPointF(8, 14.2), QPointF(16, 14.2))


@_glyph("logo")
def _logo(p, pen):
    """ReCut mark: a play shape sliced by a cut gap."""
    top = _rounded_triangle([(5, 3.5), (21, 12), (5, 20.5)], 2.2)
    gap = QPainterPath()
    gap.addRect(QRectF(10.2, 2, 2.6, 21))
    _fill(p, pen, top.subtracted(gap))


def draw_glyph(painter: QPainter, name: str, rect: QRectF, color: QColor, stroke: float = STROKE) -> None:
    fn = _REGISTRY.get(name)
    if fn is None:
        return
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.translate(rect.x(), rect.y())
    painter.scale(rect.width() / 24.0, rect.height() / 24.0)
    pen = QPen(QColor(color), stroke)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    fn(painter, pen)
    painter.restore()


def glyph_pixmap(name: str, size: int, color: QColor | str) -> QPixmap:
    scale = 2
    pm = QPixmap(size * scale, size * scale)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    draw_glyph(p, name, QRectF(0, 0, size * scale, size * scale), QColor(color))
    p.end()
    pm.setDevicePixelRatio(scale)
    return pm


def glyph_icon(name: str, color: QColor | str, size: int = 16, disabled: QColor | str | None = None) -> QIcon:
    c = QColor(color)
    key = (name, int(size), c.name(QColor.HexArgb) + (QColor(disabled).name() if disabled else ""))
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    icon = QIcon()
    icon.addPixmap(glyph_pixmap(name, size, c), QIcon.Normal)
    if disabled is not None:
        icon.addPixmap(glyph_pixmap(name, size, QColor(disabled)), QIcon.Disabled)
    _CACHE[key] = icon
    return icon


def available() -> list[str]:
    return sorted(_REGISTRY)
