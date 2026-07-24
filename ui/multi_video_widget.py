from __future__ import annotations

import math
from typing import Callable, Dict, Optional, List

from PySide6.QtCore import Qt
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QImage, QPainter
from PySide6.QtWidgets import QWidget
from PySide6.QtMultimedia import QVideoSink


class MultiVideoWidget(QWidget):
    """
    Composite video renderer for multiple tracks.
    Uses one QVideoSink per track and paints frames in a grid.
    """

    def __init__(self, on_file_dropped: Callable[[str], None]):
        super().__init__()
        self._on_file_dropped = on_file_dropped
        self.setAcceptDrops(True)
        self.setMinimumHeight(220)
        self._frames: Dict[int, QImage] = {}
        self._sinks: Dict[int, QVideoSink] = {}
        self._track_count: int = 1

        self.setAutoFillBackground(True)
        pal = self.palette()
        pal.setColor(self.backgroundRole(), Qt.black)
        self.setPalette(pal)

    def set_track_count(self, n: int) -> None:
        self._track_count = max(1, int(n))
        self.update()

    def sink_for(self, track_idx: int) -> QVideoSink:
        idx = int(track_idx)
        if idx in self._sinks:
            return self._sinks[idx]

        sink = QVideoSink(self)

        def _on_frame(frame, i=idx):
            if frame is None or not frame.isValid():
                self._frames.pop(i, None)
                self.update()
                return
            img = frame.toImage()
            if img is None or img.isNull():
                self._frames.pop(i, None)
                self.update()
                return
            self._frames[i] = img
            self.update()

        sink.videoFrameChanged.connect(_on_frame)
        self._sinks[idx] = sink
        return sink

    def _track_indices(self) -> List[int]:
        if self._sinks:
            keys = sorted(self._sinks.keys())
            max_idx = max(keys)
            return list(range(max_idx + 1))
        return list(range(max(1, self._track_count)))

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), Qt.black)

        indices = self._track_indices()
        n = len(indices)
        if n <= 0:
            return

        cols = int(math.ceil(math.sqrt(n)))
        rows = int(math.ceil(n / cols)) if cols > 0 else 1

        w = self.width()
        h = self.height()
        cell_w = max(1, w // cols)
        cell_h = max(1, h // rows)

        for i, idx in enumerate(indices):
            row = i // cols
            col = i % cols
            x0 = col * cell_w
            y0 = row * cell_h

            cell = self.rect().adjusted(x0, y0, x0 + cell_w - w, y0 + cell_h - h)
            img = self._frames.get(idx)
            if img is None or img.isNull():
                continue

            target = cell
            scaled = img.scaled(target.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            x = target.x() + (target.width() - scaled.width()) // 2
            y = target.y() + (target.height() - scaled.height()) // 2
            p.drawImage(x, y, scaled)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        urls = event.mimeData().urls()
        if not urls:
            return
        for url in urls:
            path = url.toLocalFile()
            if path:
                self._on_file_dropped(path)
