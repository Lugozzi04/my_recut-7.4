from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QVBoxLayout, QWidget


def build_viewer_pane(window) -> QFrame:
    """Viewer column: video (fills), thin scrub bar, transport.

    Expects: video_widget, seek, transport (TransportBar).
    """
    pane = QFrame()
    pane.setObjectName("ViewerPane")
    pane.setAttribute(Qt.WA_StyledBackground, True)
    lay = QVBoxLayout(pane)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)

    lay.addWidget(window.video_widget, stretch=1)

    scrub = QWidget()
    scrub.setObjectName("ScrubRow")
    scrub_l = QVBoxLayout(scrub)
    scrub_l.setContentsMargins(12, 2, 12, 2)
    scrub_l.setSpacing(0)
    window.seek.setObjectName("ScrubSlider")
    window.seek.setFixedHeight(16)
    window.seek.setFocusPolicy(Qt.NoFocus)
    scrub_l.addWidget(window.seek)
    lay.addWidget(scrub)

    lay.addWidget(window.transport)
    return pane


def build_timeline_pane(window) -> QFrame:
    """Full-width timeline: toolbar + timeline canvas.

    Expects: timeline_toolbar, timeline_scroll (or timeline).
    """
    pane = QFrame()
    pane.setObjectName("TimelinePane")
    pane.setAttribute(Qt.WA_StyledBackground, True)
    lay = QVBoxLayout(pane)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)
    lay.addWidget(window.timeline_toolbar)
    timeline_widget = window.timeline_scroll if hasattr(window, "timeline_scroll") else window.timeline
    lay.addWidget(timeline_widget, stretch=1)
    return pane

