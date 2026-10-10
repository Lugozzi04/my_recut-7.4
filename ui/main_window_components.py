from __future__ import annotations

from typing import Any

from PySide6.QtCore import QEvent, QLocale, QObject, QPointF, QRect, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QValidator, QWheelEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QSizePolicy,
    QSpinBox,
    QWidget,
)

from analysis.cut_engine import Segment


try:
    from shiboken6 import isValid as qt_is_valid
except Exception:
    def qt_is_valid(obj: Any) -> bool:
        return obj is not None


class MiniTimelineWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._duration = 0.0
        self._segments: list[Segment] = []
        self._speaker_ids: list[int] | None = None
        self._colors = [
            QColor(90, 170, 255),
            QColor(255, 170, 90),
            QColor(140, 220, 140),
            QColor(200, 150, 255),
            QColor(255, 120, 120),
            QColor(180, 200, 90),
        ]
        self.setMinimumHeight(36)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_data(
        self,
        duration: float,
        segments: list[Segment] | None,
        speaker_ids: list[int] | None = None,
    ) -> None:
        self._duration = float(duration or 0.0)
        self._segments = list(segments or [])
        self._speaker_ids = list(speaker_ids) if speaker_ids is not None else None
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)
        rect = self.rect().adjusted(2, 2, -2, -2)
        painter.fillRect(rect, QColor(28, 30, 36))

        if self._duration <= 0 or not self._segments:
            painter.setPen(QColor(130, 130, 130))
            painter.drawText(rect, Qt.AlignCenter, "No AI data")
            painter.end()
            return

        width = max(1.0, float(rect.width()))
        left = float(rect.left())
        top = float(rect.top())
        height = float(rect.height())

        for index, segment in enumerate(self._segments):
            try:
                start = max(0.0, float(segment.start))
                end = max(start, float(segment.end))
            except Exception:
                continue
            if end <= start:
                continue
            x_start = left + (start / self._duration) * width
            x_end = left + (end / self._duration) * width
            color = self._colors[0]
            if self._speaker_ids and index < len(self._speaker_ids):
                color_index = int(self._speaker_ids[index]) % max(1, len(self._colors))
                color = self._colors[color_index]
            painter.fillRect(
                QRect(
                    int(x_start),
                    int(top),
                    max(1, int(x_end - x_start)),
                    int(height),
                ),
                color,
            )

        painter.setPen(QColor(60, 60, 60))
        painter.drawRect(rect)
        painter.end()


class NoWheelUnlessFocusedMixin:
    def wheelEvent(self, event: QWheelEvent):
        if not self.hasFocus():
            event.ignore()
            return
        super().wheelEvent(event)


class DragValueMixin:
    """Allow horizontal click-drag value changes on spin boxes."""

    _drag_active: bool = False
    _drag_origin: QPointF | None = None
    _drag_start_value: float | None = None

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_active = False
            self._drag_origin = event.globalPosition()
            try:
                self._drag_start_value = float(self.value())
            except Exception:
                self._drag_start_value = None
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (
            event.buttons() & Qt.LeftButton
            and self._drag_origin is not None
            and self._drag_start_value is not None
        ):
            dx = int(event.globalPosition().x() - self._drag_origin.x())
            dy = int(event.globalPosition().y() - self._drag_origin.y())
            if not self._drag_active and abs(dx) > 4 and abs(dx) >= abs(dy):
                self._drag_active = True
                self.setCursor(Qt.SizeHorCursor)

            if self._drag_active:
                steps = int(dx / 5)
                self.setValue(self._drag_start_value + (steps * float(self.singleStep())))
                return

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drag_active:
            self.unsetCursor()
        self._drag_active = False
        self._drag_origin = None
        self._drag_start_value = None
        super().mouseReleaseEvent(event)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            self._drag_active = False
            self._drag_origin = event.globalPosition()
            try:
                self._drag_start_value = float(self.value())
            except Exception:
                self._drag_start_value = None
        elif event.type() == QEvent.MouseMove and event.buttons() & Qt.LeftButton:
            if self._drag_origin is not None and self._drag_start_value is not None:
                dx = int(event.globalPosition().x() - self._drag_origin.x())
                dy = int(event.globalPosition().y() - self._drag_origin.y())
                if not self._drag_active and abs(dx) > 4 and abs(dx) >= abs(dy):
                    self._drag_active = True
                    self.setCursor(Qt.SizeHorCursor)
                if self._drag_active:
                    steps = int(dx / 5)
                    self.setValue(
                        self._drag_start_value + (steps * float(self.singleStep()))
                    )
                    return True
        elif event.type() == QEvent.MouseButtonRelease:
            if self._drag_active:
                self.unsetCursor()
            self._drag_active = False
            self._drag_origin = None
            self._drag_start_value = None
        return super().eventFilter(obj, event)


class NoWheelSpinBox(NoWheelUnlessFocusedMixin, DragValueMixin, QSpinBox):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setKeyboardTracking(False)
        self.editingFinished.connect(self._commit_text_value)
        try:
            self.lineEdit().installEventFilter(self)
        except Exception:
            pass

    def validate(self, text: str, pos: int):
        state, _, _ = super().validate(text, pos)
        if state in (QValidator.Acceptable, QValidator.Intermediate):
            return state, text, pos
        if text.strip() in ("", "+", "-"):
            return QValidator.Intermediate, text, pos
        try:
            int(text)
            return QValidator.Intermediate, text, pos
        except Exception:
            return QValidator.Invalid, text, pos

    def _commit_text_value(self):
        text = self.lineEdit().text().strip()
        if text in ("", "+", "-"):
            return
        try:
            value = int(text)
        except Exception:
            return
        self.setValue(max(self.minimum(), min(self.maximum(), value)))


class NoWheelDoubleSpinBox(
    NoWheelUnlessFocusedMixin,
    DragValueMixin,
    QDoubleSpinBox,
):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        try:
            self.setLocale(QLocale.c())
        except Exception:
            pass
        self.setKeyboardTracking(False)
        self.editingFinished.connect(self._commit_text_value)
        try:
            self.lineEdit().installEventFilter(self)
        except Exception:
            pass

    def validate(self, text: str, pos: int):
        state, _, _ = super().validate(text, pos)
        if state in (QValidator.Acceptable, QValidator.Intermediate):
            return state, text, pos
        if text.strip() in ("", "+", "-", ".", ","):
            return QValidator.Intermediate, text, pos
        try:
            float(text.replace(",", "."))
            return QValidator.Intermediate, text, pos
        except Exception:
            return QValidator.Invalid, text, pos

    def _commit_text_value(self):
        text = self.lineEdit().text().strip()
        if text in ("", "+", "-", ".", ","):
            return
        try:
            value = float(text.replace(",", "."))
        except Exception:
            return
        self.setValue(max(self.minimum(), min(self.maximum(), value)))


class WheelOnlyIfFocusedFilter(QObject):
    def eventFilter(self, obj, event):
        if event.type() == QEvent.Wheel:
            if isinstance(obj, (QSpinBox, QDoubleSpinBox)) and not obj.hasFocus():
                return True
            parent = obj.parent()
            if isinstance(parent, (QSpinBox, QDoubleSpinBox)) and not parent.hasFocus():
                return True
        return super().eventFilter(obj, event)


class GuiInvoker(QObject):
    """Receiver that lives on the GUI thread.

    PySide delivers a signal connected to a bare lambda in the *sender's* thread, so worker/thread
    signals wired to lambdas ran UI code on background threads (random access violations).
    Connecting to ``GuiInvoker(fn, gui_parent).call`` makes Qt queue the call to the GUI thread.
    """

    def __init__(self, fn, parent: QObject | None = None):
        super().__init__(parent)
        self._fn = fn

    def call(self, *args) -> None:
        self._fn(*args)


class UiMessageRelay(QObject):
    """Thread-safe bridge: plain Python threads emit, slots run on the GUI thread (queued)."""

    message = Signal(str)


class ToggleSwitch(QCheckBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setFixedSize(58, 30)
        self.setText("")

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect().adjusted(2, 2, -2, -2)
        radius = rect.height() / 2.0
        is_on = self.isChecked()

        painter.setPen(Qt.NoPen)
        painter.setBrush(
            QColor(75, 134, 255, 200)
            if is_on
            else QColor(90, 96, 110, 150)
        )
        painter.drawRoundedRect(rect, radius, radius)

        knob_size = rect.height() - 4
        x = rect.right() - knob_size - 2 if is_on else rect.left() + 2
        y = rect.top() + 2
        painter.setBrush(QColor(240, 243, 248))
        painter.drawEllipse(int(x), int(y), int(knob_size), int(knob_size))
        painter.end()
