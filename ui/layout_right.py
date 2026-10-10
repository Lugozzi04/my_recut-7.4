from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QVBoxLayout, QWidget

from ui.design_tokens import INSPECTOR_MAX, INSPECTOR_MIN
from ui.widgets.inspector import SegmentedControl

from .layout_export_page import build_export_page
from .layout_main_page import build_analysis_page, build_main_page


def build_right_panel(window) -> QWidget:
    """Inspector: segmented tabs (Edit · Analysis · Export) over a stack of property pages.

    Page indices in window.pages: 0 = Edit, 1 = Export, 2 = Analysis (tab order differs on purpose:
    older code switches to the export page with index 1).
    """
    root = QFrame()
    root.setObjectName("Inspector")
    root.setAttribute(Qt.WA_StyledBackground, True)
    root.setMinimumWidth(INSPECTOR_MIN)
    root.setMaximumWidth(INSPECTOR_MAX)
    outer = QVBoxLayout(root)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.setSpacing(0)

    header = QFrame()
    header.setObjectName("InspectorHeader")
    header.setAttribute(Qt.WA_StyledBackground, True)
    head_l = QVBoxLayout(header)
    head_l.setContentsMargins(12, 10, 12, 10)
    head_l.setSpacing(0)
    window.seg_main.setText("Edit")
    window.seg_analysis.setText("Analysis")
    window.seg_export.setText("Export")
    window.seg_main.setToolTip("Cuts, preset and detection settings  (Alt+1)")
    window.seg_analysis.setToolTip("Classic / AI analysis and gameplay  (Alt+2)")
    window.seg_export.setToolTip("Output settings and export  (Alt+3)")
    window.inspector_tabs = SegmentedControl([window.seg_main, window.seg_analysis, window.seg_export],
                                             object_name="Segmented")
    for b in (window.seg_main, window.seg_analysis, window.seg_export):
        b.setFixedHeight(28)
    head_l.addWidget(window.inspector_tabs)
    outer.addWidget(header)

    build_main_page(window)
    build_export_page(window)
    build_analysis_page(window)

    outer.addWidget(window.pages, stretch=1)
    window.stats_panel_container = None
    return root
