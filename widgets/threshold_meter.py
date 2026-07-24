from __future__ import annotations

from typing import Optional
import numpy as np

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget


class ThresholdMeter(QWidget):
    """
    Mini-meter verticale per visualizzare la soglia (threshold) rispetto
    al range tipico dell'RMS (0..ref_max). Disegna una linea rossa.
    """
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self._threshold_pct: float = 45.0
        self._ref_max: float = 100.0  
        self._has_ref: bool = False
        self.setFixedWidth(24)
        self.setMinimumHeight(70)

    def set_threshold_pct(self, pct: float):
        self._threshold_pct = float(max(0.0, min(100.0, pct)))
        self.update()

    def set_reference_from_rms(self, rms: Optional[np.ndarray]):
        """
        Imposta un riferimento "alto" in modo robusto usando p95 dell'RMS.
        Questo evita che un picco anomalo schiacci il meter.
        """
        if rms is None or getattr(rms, "size", 0) == 0:
            self._has_ref = False
        else:
            self._has_ref = True
        self.update()

    def paintEvent(self, _e):
        w = self.width()
        h = self.height()

        pad = 6
        bar_x = w // 2
        top = pad
        bottom = h - pad
        bar_h = max(1, bottom - top)

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)

        # background
        p.fillRect(0, 0, w, h, QColor(20, 20, 20))

        # bar outline
        pen = QPen(QColor(90, 90, 90))
        pen.setWidth(1)
        p.setPen(pen)
        p.drawRect(4, top, w - 8, bar_h)

        # fill (solo estetica)
        p.fillRect(5, top + 1, w - 10, bar_h - 1, QColor(45, 45, 45))

        # reference label indicator (optional): small tick near top
        if self._has_ref:
            p.setPen(QPen(QColor(160, 160, 160)))
            p.drawLine(4, top, 8, top)

        # threshold line mapping (0..ref_max)
        t = max(0.0, min(self._threshold_pct / 100.0, 1.0))
        y = bottom - int(t * bar_h)

        # red line
        pen = QPen(QColor(230, 80, 80))
        pen.setWidth(2)
        p.setPen(pen)
        p.drawLine(4, y, w - 4, y)

        # small center line for aesthetics
        p.setPen(QPen(QColor(70, 70, 70)))
        p.drawLine(bar_x, top, bar_x, bottom)
