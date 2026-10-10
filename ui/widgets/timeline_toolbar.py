"""Toolbar above the timeline: edit tools, history, snapping, legend and zoom."""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QSlider, QToolButton, QWidget

from ui.design_tokens import H_TOOLBAR, Palette, palette
from ui.widgets.glyphs import glyph_icon
from ui.widgets.inspector import LegendItem


def _icon_button(tip: str, *, checkable: bool = False, name: str = "ToolbarButton") -> QToolButton:
    b = QToolButton()
    b.setObjectName(name)
    b.setToolTip(tip)
    b.setCheckable(checkable)
    b.setCursor(Qt.PointingHandCursor)
    b.setFixedSize(30, 28)
    b.setIconSize(QSize(16, 16))
    b.setFocusPolicy(Qt.NoFocus)
    return b


def _divider() -> QFrame:
    d = QFrame()
    d.setObjectName("ToolbarDivider")
    d.setFixedSize(1, 18)
    return d


class TimelineToolbar(QFrame):
    selectToolClicked = Signal()
    undoClicked = Signal()
    redoClicked = Signal()
    snapToggled = Signal(bool)
    zoomIndexChanged = Signal(int)

    def __init__(self, btn_cut_tool: QToolButton, btn_split: QToolButton, btn_zoom_out: QToolButton,
                 btn_zoom_in: QToolButton, btn_zoom_reset: QToolButton, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("TimelineToolbar")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedHeight(H_TOOLBAR)
        self._pal = palette(False)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 0, 10, 0)
        lay.setSpacing(4)

        tools = QFrame()
        tools.setObjectName("ToolGroup")
        tools_l = QHBoxLayout(tools)
        tools_l.setContentsMargins(2, 2, 2, 2)
        tools_l.setSpacing(2)
        self.btn_select = _icon_button("Select  (V)\nClick a cut to select it · drag on empty space to select several",
                                       checkable=True, name="ToolButton")
        self.btn_cut = btn_cut_tool
        self.btn_split = btn_split
        for b, tip in ((self.btn_cut, "Cut tool  (C)\nClick the start, then the end of the range to remove"),
                       (self.btn_split, "Split tool  (S)\nClick a clip to split it")):
            b.setObjectName("ToolButton")
            b.setToolTip(tip)
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedSize(30, 26)
            b.setIconSize(QSize(16, 16))
            b.setFocusPolicy(Qt.NoFocus)
        self.btn_select.setFixedSize(30, 26)
        for b in (self.btn_select, self.btn_cut, self.btn_split):
            tools_l.addWidget(b)
        lay.addWidget(tools)
        lay.addSpacing(6)
        lay.addWidget(_divider())
        lay.addSpacing(2)

        self.btn_undo = _icon_button("Undo  (Ctrl+Z)")
        self.btn_redo = _icon_button("Redo  (Ctrl+Shift+Z / Ctrl+Y)")
        lay.addWidget(self.btn_undo)
        lay.addWidget(self.btn_redo)
        lay.addSpacing(2)
        lay.addWidget(_divider())
        lay.addSpacing(2)
        self.btn_snap = _icon_button("Snapping  (N)\nClip drags snap to edges and the playhead", checkable=True)
        lay.addWidget(self.btn_snap)

        lay.addStretch(1)

        self.legend = QWidget()
        leg_l = QHBoxLayout(self.legend)
        leg_l.setContentsMargins(0, 0, 0, 0)
        leg_l.setSpacing(14)
        self.legend_items: dict[str, LegendItem] = {}
        for key, text, hatched, tip in (
            ("auto", "Auto cut", False, "Removed by audio analysis (Classic or AI)"),
            ("manual", "Manual", False, "Cuts you added"),
            ("gameplay", "WIN kept", False, "Complete WIN games kept by gameplay editing; the rest is dimmed"),
            ("restored", "Restored", True, "Automatic cuts you restored (kept)"),
        ):
            item = LegendItem("#888", text, hatched=hatched)
            item.setToolTip(tip)
            self.legend_items[key] = item
            leg_l.addWidget(item)
        lay.addWidget(self.legend)
        lay.addSpacing(14)
        lay.addWidget(_divider())
        lay.addSpacing(4)

        self.btn_zoom_out = btn_zoom_out
        self.btn_zoom_in = btn_zoom_in
        self.btn_fit = btn_zoom_reset
        for b, tip in ((self.btn_zoom_out, "Zoom out  (Ctrl+-)"), (self.btn_zoom_in, "Zoom in  (Ctrl+=)"),
                       (self.btn_fit, "Fit timeline to window  (Shift+Z / Ctrl+0)")):
            b.setObjectName("ToolbarButton")
            b.setToolTip(tip)
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedSize(30, 28)
            b.setIconSize(QSize(16, 16))
            b.setFocusPolicy(Qt.NoFocus)
        self.zoom_slider = QSlider(Qt.Horizontal)
        self.zoom_slider.setObjectName("ZoomSlider")
        self.zoom_slider.setFixedWidth(110)
        self.zoom_slider.setRange(0, 11)
        self.zoom_slider.setPageStep(1)
        self.zoom_slider.setFocusPolicy(Qt.NoFocus)
        self.zoom_slider.setToolTip("Timeline zoom")
        lay.addWidget(self.btn_zoom_out)
        lay.addWidget(self.zoom_slider)
        lay.addWidget(self.btn_zoom_in)
        lay.addWidget(self.btn_fit)

        self.btn_select.clicked.connect(self.selectToolClicked.emit)
        self.btn_undo.clicked.connect(self.undoClicked.emit)
        self.btn_redo.clicked.connect(self.redoClicked.emit)
        self.btn_snap.toggled.connect(self.snapToggled.emit)
        self.zoom_slider.valueChanged.connect(self.zoomIndexChanged.emit)

    def apply_palette(self, pal: Palette) -> None:
        self._pal = pal
        c, d = pal.text_2, pal.text_3
        self.btn_select.setIcon(glyph_icon("select", c, 16, disabled=d))
        self.btn_cut.setIcon(glyph_icon("scissors", c, 16, disabled=d))
        self.btn_split.setIcon(glyph_icon("split", c, 16, disabled=d))
        self.btn_undo.setIcon(glyph_icon("undo", c, 16, disabled=d))
        self.btn_redo.setIcon(glyph_icon("redo", c, 16, disabled=d))
        self.btn_snap.setIcon(glyph_icon("magnet", c, 16, disabled=d))
        self.btn_zoom_out.setIcon(glyph_icon("zoom_out", c, 16, disabled=d))
        self.btn_zoom_in.setIcon(glyph_icon("zoom_in", c, 16, disabled=d))
        self.btn_fit.setIcon(glyph_icon("fit", c, 16, disabled=d))
        colors = {"auto": pal.cut_auto, "manual": pal.cut_manual, "gameplay": pal.keep_island,
                  "restored": pal.restored}
        for key, item in self.legend_items.items():
            item.dot.set_color(colors[key])

    def set_zoom(self, index: int, maximum: int) -> None:
        self.zoom_slider.blockSignals(True)
        self.zoom_slider.setRange(0, max(0, int(maximum)))
        self.zoom_slider.setValue(max(0, min(int(maximum), int(index))))
        self.zoom_slider.blockSignals(False)

    def set_tool(self, mode: str) -> None:
        self.btn_select.blockSignals(True)
        self.btn_select.setChecked(mode not in {"cut", "split"})
        self.btn_select.blockSignals(False)

    def set_history(self, can_undo: bool, can_redo: bool) -> None:
        self.btn_undo.setEnabled(bool(can_undo))
        self.btn_redo.setEnabled(bool(can_redo))

    def set_legend_visible(self, key: str, visible: bool) -> None:
        item = self.legend_items.get(key)
        if item is not None:
            item.setVisible(bool(visible))
