from __future__ import annotations

import math

from PySide6.QtCore import Qt, QRect, QTimer
from PySide6.QtGui import (
    QColor,
    QDragEnterEvent,
    QDropEvent,
    QFontMetrics,
    QImage,
    QLinearGradient,
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
        self._overlay_color = QColor(220, 60, 60)
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

    def _paint_empty_state(self, p: QPainter) -> None:
        r = self.rect().adjusted(18, 18, -18, -18)
        if r.width() < 120 or r.height() < 120:
            self._empty_card_rect = QRect()
            self._empty_browse_rect = QRect()
            return

        card_w = min(560, max(160, r.width() - 8))
        card_h = min(220, max(130, r.height() - 8))
        card = QRect(
            r.center().x() - (card_w // 2),
            r.center().y() - (card_h // 2),
            card_w,
            card_h,
        )
        card = card.intersected(r)
        self._empty_card_rect = card

        bg = QColor(14, 16, 20)
        if self._drag_hover:
            bg = QColor(17, 26, 38)
        border = QColor(56, 66, 84)
        if self._drag_hover:
            border = QColor(90, 150, 255)

        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(QPen(border, 1))
        p.setBrush(bg)
        p.drawRoundedRect(card, 14, 14)

        # Inner dashed drop zone
        inner = card.adjusted(12, 12, -12, -12)
        dash_pen = QPen(QColor(74, 84, 104), 1)
        dash_pen.setStyle(Qt.DashLine)
        p.setPen(dash_pen)
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(inner, 10, 10)

        cx = inner.center().x()
        top = inner.top() + 18

        # Simple upload icon
        p.setPen(QPen(QColor(123, 171, 255), 2))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRect(cx - 20, top + 18, 40, 24), 6, 6)
        p.drawLine(cx, top - 4, cx, top + 22)
        p.drawLine(cx, top - 4, cx - 8, top + 4)
        p.drawLine(cx, top - 4, cx + 8, top + 4)

        title_font = p.font()
        title_font.setBold(True)
        title_font.setPointSize(max(11, title_font.pointSize() + 2))
        p.setFont(title_font)
        p.setPen(QColor(236, 242, 248))
        title_rect = QRect(inner.left() + 16, top + 56, inner.width() - 32, 22)
        p.drawText(title_rect, Qt.AlignCenter, "Drop video here")

        sub_font = p.font()
        sub_font.setBold(False)
        sub_font.setPointSize(max(9, sub_font.pointSize() - 1))
        p.setFont(sub_font)
        p.setPen(QColor(150, 162, 180))
        sub_rect = QRect(inner.left() + 18, top + 82, inner.width() - 36, 18)
        p.drawText(sub_rect, Qt.AlignCenter, "or click Browse to select files")

        # CTA button
        browse_w = 108
        browse_h = 30
        browse_rect = QRect(cx - (browse_w // 2), top + 108, browse_w, browse_h)
        self._empty_browse_rect = browse_rect
        p.setPen(QPen(QColor(94, 164, 255), 1))
        p.setBrush(QColor(36, 70, 116, 180))
        p.drawRoundedRect(browse_rect, 8, 8)
        p.setPen(QColor(244, 248, 255))
        p.drawText(browse_rect, Qt.AlignCenter, "Browse...")

        p.setPen(QColor(125, 136, 152))
        hint_rect = QRect(inner.left() + 16, top + 146, inner.width() - 32, 34)
        p.drawText(
            hint_rect,
            Qt.AlignCenter | Qt.TextWordWrap,
            "Supports MP4, MOV, MKV, M4V  -  Tip: drag files directly into this preview area",
        )

    def _paint_background_matte(self, p: QPainter) -> None:
        r = self.rect()
        bg = QLinearGradient(0, float(r.top()), 0, float(r.bottom()))
        bg.setColorAt(0.0, QColor(12, 18, 28))
        bg.setColorAt(0.55, QColor(9, 14, 22))
        bg.setColorAt(1.0, QColor(7, 11, 17))
        p.fillRect(r, bg)

    def _paint_letterbox_matte(self, p: QPainter, target: QRect, content: QRect) -> None:
        if not target.isValid() or not content.isValid():
            return
        if content == target:
            return

        dark = QColor(8, 12, 18, 228)
        near = QColor(24, 34, 48, 190)

        left_w = content.left() - target.left()
        if left_w > 0:
            left = QRect(target.left(), target.top(), left_w, target.height())
            g = QLinearGradient(float(left.right()), 0.0, float(left.left()), 0.0)
            g.setColorAt(0.0, near)
            g.setColorAt(1.0, dark)
            p.fillRect(left, g)

        right_w = target.right() - content.right()
        if right_w > 0:
            right = QRect(content.right() + 1, target.top(), right_w, target.height())
            g = QLinearGradient(float(right.left()), 0.0, float(right.right()), 0.0)
            g.setColorAt(0.0, near)
            g.setColorAt(1.0, dark)
            p.fillRect(right, g)

        top_h = content.top() - target.top()
        if top_h > 0:
            top = QRect(content.left(), target.top(), content.width(), top_h)
            g = QLinearGradient(0.0, float(top.bottom()), 0.0, float(top.top()))
            g.setColorAt(0.0, near)
            g.setColorAt(1.0, dark)
            p.fillRect(top, g)

        bot_h = target.bottom() - content.bottom()
        if bot_h > 0:
            bottom = QRect(content.left(), content.bottom() + 1, content.width(), bot_h)
            g = QLinearGradient(0.0, float(bottom.top()), 0.0, float(bottom.bottom()))
            g.setColorAt(0.0, near)
            g.setColorAt(1.0, dark)
            p.fillRect(bottom, g)

    def paintEvent(self, event):
        p = QPainter(self)
        self._paint_background_matte(p)

        if self._image:
            p.setRenderHint(QPainter.SmoothPixmapTransform, False)
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
                self._paint_letterbox_matte(p, target, fitted)
                p.drawImage(fitted, img)
                p.setPen(QPen(QColor(240, 246, 255, 28), 1))
                p.setBrush(Qt.NoBrush)
                p.drawRoundedRect(fitted.adjusted(0, 0, -1, -1), 2, 2)
        else:
            self._paint_empty_state(p)

        if self._overlay_text:
            text = self._overlay_text
            font = p.font()
            font.setPointSize(max(12, font.pointSize() + 6))
            font.setBold(True)
            p.setFont(font)
            fm = QFontMetrics(font)
            max_w = max(40, int(self.width() * 0.8))
            max_h = max(40, int(self.height() * 0.5))
            text_rect = fm.boundingRect(0, 0, max_w, max_h, Qt.AlignCenter, text)
            text_rect.moveCenter(self.rect().center())
            pad = 12
            box = text_rect.adjusted(-pad, -pad, pad, pad)
            p.setPen(Qt.NoPen)
            bg = QColor(0, 0, 0, max(0, min(255, int(170 * self._overlay_alpha))))
            p.setBrush(bg)
            p.drawRoundedRect(box, 8, 8)
            c = QColor(self._overlay_color)
            c.setAlpha(max(0, min(255, int(255 * self._overlay_alpha))))
            p.setPen(c)
            p.drawText(text_rect, Qt.AlignCenter, text)

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
