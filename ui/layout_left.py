from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QVBoxLayout, QHBoxLayout, QFrame, QWidget, QSplitter


def build_left_panel(window) -> QVBoxLayout:
    """
    LEFT side:
      - step_bar + file label (Qt)
      - video preview (Qt)
      - seek row (Qt)
      - transport bar (HTML via window.web_transport)
      - timeline (Qt)
      - stats strip (HTML via window.web_stats)

    Expects:
      step_bar, lbl_file, video_widget, seek, lbl_time, timeline
      web_transport, web_stats
    """
    left = QVBoxLayout()
    left.setSpacing(8)
    left.setContentsMargins(12, 8, 12, 8)

    left.addWidget(window.step_bar)
    left.addWidget(window.lbl_file)

    # Unified surface for preview + timeline (visual continuity)
    surface = QFrame()
    surface.setObjectName("LeftSurface")
    surface_l = QVBoxLayout(surface)
    surface_l.setContentsMargins(0, 0, 0, 0)
    surface_l.setSpacing(8)

    # User-resizable split between preview and timeline.
    top_panel = QWidget()
    top_l = QVBoxLayout(top_panel)
    top_l.setContentsMargins(0, 0, 0, 0)
    top_l.setSpacing(8)
    top_l.addWidget(window.video_widget, stretch=1)
    top_l.addWidget(window.seek)

    transport_row = QWidget()
    transport_l = QHBoxLayout(transport_row)
    transport_l.setContentsMargins(0, 0, 0, 0)
    transport_l.setSpacing(0)
    window.web_transport.setContentsMargins(0, 0, 0, 0)
    transport_l.addWidget(window.web_transport, stretch=1)
    top_l.addWidget(transport_row)

    bottom_panel = QWidget()
    bottom_l = QVBoxLayout(bottom_panel)
    bottom_l.setContentsMargins(0, 0, 0, 0)
    bottom_l.setSpacing(8)
    timeline_widget = window.timeline_scroll if hasattr(window, "timeline_scroll") else window.timeline
    bottom_l.addWidget(timeline_widget, stretch=1)
    window.web_stats.setContentsMargins(0, 0, 0, 0)
    bottom_l.addWidget(window.web_stats)

    splitter = QSplitter(Qt.Vertical)
    splitter.setObjectName("PreviewTimelineSplitter")
    splitter.setChildrenCollapsible(False)
    splitter.setHandleWidth(10)
    splitter.setOpaqueResize(False)
    splitter.addWidget(top_panel)
    splitter.addWidget(bottom_panel)
    splitter.setStretchFactor(0, 7)
    splitter.setStretchFactor(1, 3)
    if hasattr(window, "_on_preview_timeline_splitter_moved"):
        try:
            splitter.splitterMoved.connect(window._on_preview_timeline_splitter_moved)
        except Exception:
            pass
    window.preview_timeline_splitter = splitter
    surface_l.addWidget(splitter, stretch=1)

    left.addWidget(surface, stretch=1)

    return left
