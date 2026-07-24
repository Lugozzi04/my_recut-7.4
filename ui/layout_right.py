from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QToolButton, QFrame, QStackedWidget

from .layout_main_page import build_main_page
from .layout_export_page import build_export_page


def build_right_panel(window) -> QWidget:
    """
    RIGHT side: Qt inspector (functional).
    WebUI stays for topbar/transport/stats only.
    """

    root = QFrame()
    root.setObjectName("RightPanel")
    root.setMinimumWidth(360)
    outer = QVBoxLayout(root)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.setSpacing(0)

    # segmented tabs row
    seg_wrap = QFrame()
    seg_wrap.setObjectName("SegmentWrap")
    seg_l = QHBoxLayout(seg_wrap)
    seg_l.setContentsMargins(0, 0, 0, 0)
    seg_l.setSpacing(0)

    window.seg_main.setObjectName("SegmentEdit")
    window.seg_export.setObjectName("SegmentExport")

    window.seg_main.setChecked(True)
    window.seg_export.setChecked(False)

    window.seg_main.setFixedHeight(48)
    window.seg_export.setFixedHeight(48)

    seg_l.addWidget(window.seg_main)
    seg_l.addWidget(window.seg_export)
    seg_l.addStretch(1)
    seg_l.addWidget(window.btn_layout)

    outer.addWidget(seg_wrap)

    # pages
    # (window.pages / window.page_main / window.page_export already exist in MainWindow)
    build_main_page(window)
    build_export_page(window)

    # pages (no scroll area)
    outer.addWidget(window.pages, stretch=2)
    
    # Stats are shown only in the bottom bar (left panel) to avoid duplication.
    window.stats_panel_container = None

    return root
