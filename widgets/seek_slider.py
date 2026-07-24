from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QSlider, QStyle, QStyleOptionSlider


class SeekJumpSlider(QSlider):
    """
    Seek slider with direct click-to-seek on groove.
    Dragging the handle still works as usual.
    """

    def _style_option(self) -> QStyleOptionSlider:
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        return opt

    def _groove_rect(self):
        opt = self._style_option()
        return self.style().subControlRect(QStyle.CC_Slider, opt, QStyle.SC_SliderGroove, self)

    def _handle_rect(self):
        opt = self._style_option()
        return self.style().subControlRect(QStyle.CC_Slider, opt, QStyle.SC_SliderHandle, self)

    def _pixel_pos_to_value(self, pos) -> int:
        opt = self._style_option()
        groove = self._groove_rect()
        handle = self._handle_rect()

        if self.orientation() == Qt.Horizontal:
            span = max(1, groove.width() - handle.width())
            rel = float(pos.x() - groove.x()) - (float(handle.width()) / 2.0)
        else:
            span = max(1, groove.height() - handle.height())
            rel = float(pos.y() - groove.y()) - (float(handle.height()) / 2.0)

        rel = max(0.0, min(float(span), rel))
        return int(
            QStyle.sliderValueFromPosition(
                int(self.minimum()),
                int(self.maximum()),
                int(round(rel)),
                int(span),
                bool(opt.upsideDown),
            )
        )

    def mousePressEvent(self, event):
        if (
            event.button() == Qt.LeftButton
            and self.isEnabled()
            and self.orientation() == Qt.Horizontal
        ):
            pos = event.position().toPoint()
            groove = self._groove_rect()
            handle = self._handle_rect()

            # Preserve native drag behavior when pressing on the handle.
            if handle.contains(pos):
                return super().mousePressEvent(event)

            # Jump only when clicking exactly on the horizontal groove.
            if groove.contains(pos):
                self.setFocus(Qt.MouseFocusReason)
                self.sliderPressed.emit()
                self.setSliderDown(True)
                self.setValue(self._pixel_pos_to_value(pos))
                self.setSliderDown(False)
                self.sliderReleased.emit()
                event.accept()
                return

        super().mousePressEvent(event)

