from __future__ import annotations

import math

from PySide6.QtCore import Qt, QRect, QRectF, QTimer
from PySide6.QtGui import (
    QColor,
    QDragEnterEvent,
    QDropEvent,
    QFont,
    QFontMetrics,
    QImage,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import QWidget
from PySide6.QtMultimedia import QVideoSink


class FrameVideoWidget(QWidget):
    """
    Qt-only video renderer (no native surface), so drag&drop works on the black area.
    Uses QVideoSink to receive frames from QMediaPlayer and paints them.
    """

    def __init__(self, on_file_dropped, on_browse_requested=None):
        super().__init__()
        self._on_file_dropped = on_file_dropped
        self._on_browse_requested = on_browse_requested
        self.setAcceptDrops(True)
        self.setMinimumHeight(220)
        self.setMouseTracking(True)
        self._image: QImage | None = None
        self._overlay_text: str | None = None
        self._overlay_color = QColor(77, 141, 255)
        self._pal = None
        self._overlay_alpha = 1.0
        self._overlay_phase = 0.0
        self._empty_card_rect = QRect()
        self._empty_browse_rect = QRect()
        self._drag_hover = False
        self._overlay_timer = QTimer(self)
        self._overlay_timer.setInterval(50)
        self._overlay_timer.timeout.connect(self._tick_overlay)

        self.video_sink = QVideoSink(self)
        self.video_sink.videoFrameChanged.connect(self._on_frame)

        self.setAutoFillBackground(True)
        pal = self.palette()
        pal.setColor(self.backgroundRole(), Qt.black)
        self.setPalette(pal)

    def _on_frame(self, frame):
        if frame is None or not frame.isValid():
            # Keep last valid frame to avoid black flashes during seeks/segment switches
            return
        img = frame.toImage()
        if img.isNull():
            return
        self._image = img
        self.update()

    def clear_frame(self) -> None:
        # Explicitly clear the last frame (e.g. on reset) to show black preview.
        self._image = None
        self.update()

    def set_palette_tokens(self, pal) -> None:
        """Adopt the editor palette (ui.design_tokens.Palette) for empty state, matte and overlays."""
        self._pal = pal
        self.update()

    def _tok(self, name: str, default: str) -> QColor:
        return QColor(getattr(self._pal, name, default) if self._pal is not None else default)

    def _paint_empty_state(self, p: QPainter) -> None:
        r = self.rect().adjusted(24, 24, -24, -24)
        if r.width() < 160 or r.height() < 140:
            self._empty_card_rect = QRect()
            self._empty_browse_rect = QRect()
            return
        accent = self._tok("accent", "#4D8DFF")
        text = self._tok("text", "#E9EDF3")
        text_2 = self._tok("text_2", "#A3ACB9")
        text_3 = self._tok("text_3", "#6A7383")
        border = self._tok("border_strong", "#323843")

        card_w = min(460, r.width())
        card_h = min(250, r.height())
        card = QRect(r.center().x() - card_w // 2, r.center().y() - card_h // 2, card_w, card_h)
        self._empty_card_rect = card
        p.setRenderHint(QPainter.Antialiasing, True)

        # dashed drop zone (accent while dragging)
        zone_pen = QPen(accent if self._drag_hover else border, 1.5 if self._drag_hover else 1.2)
        zone_pen.setStyle(Qt.DashLine)
        zone_pen.setDashPattern([5, 4])
        p.setPen(zone_pen)
        fill = QColor(accent)
        fill.setAlpha(22 if self._drag_hover else 8)
        p.setBrush(fill)
        p.drawRoundedRect(card, 14, 14)

        cx = card.center().x()
        top = card.top() + 30
        # icon badge
        badge = QRect(cx - 24, top, 48, 48)
        bg = QColor(accent)
        bg.setAlpha(36)
        p.setPen(Qt.NoPen)
        p.setBrush(bg)
        p.drawEllipse(badge)
        try:
            from ui.widgets.glyphs import draw_glyph

            draw_glyph(p, "import", QRectF(cx - 12, top + 12, 24, 24), accent, 1.9)
        except Exception:
            pass

        f = QFont(self.font())
        f.setPixelSize(16)
        f.setBold(True)
        p.setFont(f)
        p.setPen(text)
        p.drawText(QRect(card.left(), top + 60, card.width(), 24), Qt.AlignCenter,
                   "Release to import" if self._drag_hover else "Drop a video here")
        f.setPixelSize(12)
        f.setBold(False)
        p.setFont(f)
        p.setPen(text_3)
        p.drawText(QRect(card.left(), top + 88, card.width(), 18), Qt.AlignCenter, "or")

        browse = QRect(cx - 62, top + 112, 124, 32)
        self._empty_browse_rect = browse
        hover = browse.contains(self.mapFromGlobal(self.cursor().pos()))
        p.setPen(Qt.NoPen)
        p.setBrush(accent.lighter(112) if hover else accent)
        p.drawRoundedRect(browse, 7, 7)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor("#FFFFFF"))
        p.drawText(browse, Qt.AlignCenter, "Choose file")
        f.setBold(False)
        f.setPixelSize(11)
        p.setFont(f)
        p.setPen(text_2)
        p.drawText(QRect(card.left(), top + 156, card.width(), 18), Qt.AlignCenter, "MP4 · MOV · MKV · M4V")
        p.setFont(QFont(self.font()))

    def _paint_background_matte(self, p: QPainter) -> None:
        p.fillRect(self.rect(), self._tok("viewer", "#08090B"))

    def paintEvent(self, event):
        p = QPainter(self)
        self._paint_background_matte(p)

        if self._image:
            p.setRenderHint(QPainter.SmoothPixmapTransform, True)
            target = self.rect()
            img = self._image
            iw = img.width()
            ih = img.height()
            if iw > 0 and ih > 0:
                scale = min(target.width() / iw, target.height() / ih)
                w = max(1, int(iw * scale))
                h = max(1, int(ih * scale))
                x = target.x() + (target.width() - w) // 2
                y = target.y() + (target.height() - h) // 2
                fitted = QRect(x, y, w, h)
                p.drawImage(fitted, img)
        else:
            self._paint_empty_state(p)

        if self._overlay_text:
            p.setRenderHint(QPainter.Antialiasing, True)
            f = QFont(self.font())
            f.setPixelSize(13)
            f.setBold(True)
            p.setFont(f)
            fm = QFontMetrics(f)
            lines = str(self._overlay_text).splitlines() or [""]
            tw = max(fm.horizontalAdvance(line) for line in lines)
            th = fm.height() * len(lines)
            box = QRect(0, 0, tw + 56, th + 24)
            box.moveCenter(self.rect().center())
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(10, 12, 16, 215))
            p.drawRoundedRect(box, 10, 10)
            dot = QColor(self._overlay_color)
            dot.setAlpha(max(60, min(255, int(255 * self._overlay_alpha))))
            p.setBrush(dot)
            p.drawEllipse(QRectF(box.left() + 18, box.center().y() - 4, 8, 8))
            p.setPen(QColor(236, 240, 246))
            p.drawText(box.adjusted(34, 0, -14, 0), Qt.AlignVCenter | Qt.AlignLeft, "\n".join(lines))

        p.end()

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            self._drag_hover = True
            self.update()
            event.acceptProposedAction()

    def dragLeaveEvent(self, _event):
        self._drag_hover = False
        self.update()

    def dropEvent(self, event: QDropEvent):
        self._drag_hover = False
        self.update()
        urls = event.mimeData().urls()
        if not urls:
            return
        for url in urls:
            path = url.toLocalFile()
            if path:
                self._on_file_dropped(path)
                break

    def mouseMoveEvent(self, event):
        if self._image:
            self.unsetCursor()
            return super().mouseMoveEvent(event)
        pos = event.position().toPoint()
        if self._empty_browse_rect.isValid() and self._empty_browse_rect.contains(pos):
            self.setCursor(Qt.PointingHandCursor)
        elif self._empty_card_rect.isValid() and self._empty_card_rect.contains(pos):
            self.setCursor(Qt.PointingHandCursor)
        else:
            self.unsetCursor()
        self.update()  # hover state of the "Choose file" button
        return super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self.unsetCursor()
        return super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        if (
            not self._image
            and event.button() == Qt.LeftButton
            and callable(self._on_browse_requested)
        ):
            pos = event.position().toPoint()
            if (
                (self._empty_browse_rect.isValid() and self._empty_browse_rect.contains(pos))
                or (self._empty_card_rect.isValid() and self._empty_card_rect.contains(pos))
            ):
                self._on_browse_requested()
                event.accept()
                return
        return super().mouseReleaseEvent(event)

    def set_overlay(self, text: str | None, color: QColor | None = None) -> None:
        self._overlay_text = text
        if color is not None:
            self._overlay_color = QColor(color)
        if text:
            self._overlay_alpha = 1.0
            if not self._overlay_timer.isActive():
                self._overlay_timer.start()
        else:
            if self._overlay_timer.isActive():
                self._overlay_timer.stop()
        self.update()

    def _tick_overlay(self) -> None:
        # Soft pulse: fade out then in (slowly)
        cycle_s = 2.8
        step = (2.0 * math.pi) * (self._overlay_timer.interval() / 1000.0) / cycle_s
        self._overlay_phase = (self._overlay_phase + step) % (2.0 * math.pi)
        s = 0.5 * (1.0 + math.sin(self._overlay_phase))
        # alpha range: 0.2 -> 1.0
        self._overlay_alpha = 0.2 + (0.8 * s)
        if self._overlay_text:
            self.update()
