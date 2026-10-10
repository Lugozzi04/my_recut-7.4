"""Inspector building blocks: segmented control, sections, property rows.

The inspector is a column of flat sections separated by hairlines (no nested cards). Each row is
"label left, control right" so values line up and the panel reads like a property sheet.
"""
from __future__ import annotations

from typing import Iterable

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QAbstractButton,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ui.widgets.glyphs import glyph_icon

LABEL_WIDTH = 116


class SegmentedControl(QFrame):
    """Pill-shaped exclusive button group. Accepts existing checkable buttons."""

    currentChanged = Signal(int)

    def __init__(self, buttons: Iterable[QAbstractButton] | None = None, *, labels: Iterable[str] | None = None,
                 object_name: str = "Segmented", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName(object_name)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._lay = QHBoxLayout(self)
        self._lay.setContentsMargins(3, 3, 3, 3)
        self._lay.setSpacing(2)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons: list[QAbstractButton] = []
        for btn in list(buttons or []):
            self.add_button(btn)
        for text in list(labels or []):
            b = QToolButton()
            b.setText(text)
            self.add_button(b)
        self._group.idClicked.connect(self.currentChanged.emit)

    def add_button(self, btn: QAbstractButton) -> QAbstractButton:
        btn.setCheckable(True)
        btn.setObjectName("SegmentedButton")
        btn.setCursor(Qt.PointingHandCursor)
        btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        if isinstance(btn, QToolButton):
            btn.setToolButtonStyle(Qt.ToolButtonTextOnly if btn.icon().isNull() else Qt.ToolButtonTextBesideIcon)
        self._group.addButton(btn, len(self._buttons))
        self._buttons.append(btn)
        self._lay.addWidget(btn, 1)
        if len(self._buttons) == 1:
            btn.setChecked(True)
        return btn

    def buttons(self) -> list[QAbstractButton]:
        return list(self._buttons)

    def setCurrentIndex(self, idx: int) -> None:
        if 0 <= idx < len(self._buttons):
            self._buttons[idx].setChecked(True)

    def currentIndex(self) -> int:
        return int(self._group.checkedId())


class Section(QFrame):
    """Flat inspector section: header (title, optional action widget, optional chevron) + body."""

    toggled = Signal(bool)

    def __init__(self, title: str, *, collapsible: bool = False, expanded: bool = True,
                 action: QWidget | None = None, subtitle: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("InspectorSection")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._collapsible = bool(collapsible)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(10)

        head = QWidget()
        head.setObjectName("SectionHead")
        head_l = QHBoxLayout(head)
        head_l.setContentsMargins(0, 0, 0, 0)
        head_l.setSpacing(6)
        self.chevron: QToolButton | None = None
        if self._collapsible:
            self.chevron = QToolButton()
            self.chevron.setObjectName("SectionChevron")
            self.chevron.setAutoRaise(True)
            self.chevron.setFixedSize(18, 18)
            self.chevron.setIconSize(QSize(14, 14))
            self.chevron.setFocusPolicy(Qt.NoFocus)
            self.chevron.clicked.connect(self.toggle)
            head_l.addWidget(self.chevron)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("SectionTitle")
        head_l.addWidget(self.title_label)
        self.subtitle_label = QLabel(subtitle)
        self.subtitle_label.setObjectName("SectionMeta")
        self.subtitle_label.setVisible(bool(subtitle))
        head_l.addWidget(self.subtitle_label)
        head_l.addStretch(1)
        if action is not None:
            head_l.addWidget(action)
        self._head = head
        if self._collapsible:
            head.setCursor(Qt.PointingHandCursor)
            head.mousePressEvent = lambda _e: self.toggle()  # type: ignore[method-assign]
        outer.addWidget(head)

        self.body = QWidget()
        self.body.setObjectName("SectionBody")
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(8)
        outer.addWidget(self.body)
        self._expanded = True
        self.set_expanded(expanded if self._collapsible else True, animate=False)

    def add(self, widget: QWidget, stretch: int = 0) -> QWidget:
        self.body_layout.addWidget(widget, stretch)
        return widget

    def add_row(self, label: str, widget: QWidget, *, hint: str = "", tooltip: str = "") -> "PropertyRow":
        row = PropertyRow(label, widget, hint=hint, tooltip=tooltip)
        self.body_layout.addWidget(row)
        return row

    def set_subtitle(self, text: str) -> None:
        self.subtitle_label.setText(text)
        self.subtitle_label.setVisible(bool(text))

    def is_expanded(self) -> bool:
        return bool(self._expanded)

    def toggle(self) -> None:
        if self._collapsible:
            self.set_expanded(not self._expanded)

    def set_expanded(self, expanded: bool, animate: bool = True) -> None:
        expanded = bool(expanded)
        changed = expanded != self._expanded
        self._expanded = expanded
        self.body.setVisible(expanded)
        if self.chevron is not None:
            color = self.palette().color(self.foregroundRole())
            self.chevron.setIcon(glyph_icon("chevron_down" if expanded else "chevron_right", color, 14))
        self.setProperty("expanded", expanded)
        self.style().unpolish(self)
        self.style().polish(self)
        if changed:
            self.toggled.emit(expanded)

    def refresh_icons(self, color: QColor) -> None:
        if self.chevron is not None:
            self.chevron.setIcon(glyph_icon("chevron_down" if self._expanded else "chevron_right", color, 14))


class PropertyRow(QWidget):
    """`Label ........ [control]` with an optional hint line underneath."""

    def __init__(self, label: str, widget: QWidget | None, *, hint: str = "", tooltip: str = "",
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("PropertyRow")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)
        line = QHBoxLayout()
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(10)
        self.label = QLabel(label)
        self.label.setObjectName("RowLabel")
        self.label.setFixedWidth(LABEL_WIDTH)
        self.label.setWordWrap(True)
        if tooltip:
            self.label.setToolTip(tooltip)
            if widget is not None and not widget.toolTip():
                widget.setToolTip(tooltip)
        line.addWidget(self.label, 0, Qt.AlignVCenter)
        if widget is not None:
            if widget.maximumWidth() < 10000:
                # compact control: keep it next to the label instead of letting the layout
                # spread the leftover space around it
                line.addWidget(widget, 0)
                line.addStretch(1)
            else:
                widget.setSizePolicy(QSizePolicy.Expanding, widget.sizePolicy().verticalPolicy())
                line.addWidget(widget, 1)
        self.widget = widget
        lay.addLayout(line)
        self.hint_label = QLabel(hint)
        self.hint_label.setObjectName("RowHint")
        self.hint_label.setWordWrap(True)
        self.hint_label.setContentsMargins(LABEL_WIDTH + 10, 0, 0, 0)
        self.hint_label.setVisible(bool(hint))
        lay.addWidget(self.hint_label)


class ValueHeader(QWidget):
    """Title on the left, live value on the right (used above sliders)."""

    def __init__(self, title: str, value_label: QLabel, *, tooltip: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self.title = QLabel(title)
        self.title.setObjectName("RowLabelStrong")
        if tooltip:
            self.title.setToolTip(tooltip)
        lay.addWidget(self.title)
        lay.addStretch(1)
        value_label.setObjectName("ValueLabel")
        lay.addWidget(value_label)


class Hint(QLabel):
    def __init__(self, text: str = "", parent: QWidget | None = None):
        super().__init__(text, parent)
        self.setObjectName("RowHint")
        self.setWordWrap(True)


class LegendDot(QWidget):
    """Small colour swatch used by legends and cut summaries."""

    def __init__(self, color: str, *, hatched: bool = False, parent: QWidget | None = None):
        super().__init__(parent)
        self._color = QColor(color)
        self._hatched = hatched
        self.setFixedSize(10, 10)

    def set_color(self, color: str) -> None:
        self._color = QColor(color)
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = self.rect().adjusted(1, 1, -1, -1)
        if self._hatched:
            c = QColor(self._color)
            c.setAlpha(60)
            p.setBrush(c)
            p.setPen(self._color)
        else:
            p.setPen(Qt.NoPen)
            p.setBrush(self._color)
        p.drawRoundedRect(r, 2.5, 2.5)
        p.end()


class LegendItem(QWidget):
    def __init__(self, color: str, text: str, *, hatched: bool = False, count_label: QLabel | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.dot = LegendDot(color, hatched=hatched)
        lay.addWidget(self.dot, 0, Qt.AlignVCenter)
        self.text = QLabel(text)
        self.text.setObjectName("LegendText")
        lay.addWidget(self.text)
        if count_label is not None:
            lay.addStretch(1)
            count_label.setObjectName("LegendCount")
            lay.addWidget(count_label)


class ToggleRow(QWidget):
    """Title + one-line explanation on the left, an on/off switch on the right."""

    toggled = Signal(bool)

    def __init__(self, title: str, hint: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        from ui.main_window_components import ToggleSwitch

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(2)
        self.title = QLabel(title)
        self.title.setObjectName("RowLabelStrong")
        col.addWidget(self.title)
        self.hint = QLabel(hint)
        self.hint.setObjectName("RowHint")
        self.hint.setWordWrap(True)
        self.hint.setVisible(bool(hint))
        col.addWidget(self.hint)
        lay.addLayout(col, 1)
        self.switch = ToggleSwitch()
        self.switch.setFixedSize(44, 24)
        lay.addWidget(self.switch, 0, Qt.AlignVCenter)
        self.switch.toggled.connect(self.toggled.emit)

    def isChecked(self) -> bool:
        return bool(self.switch.isChecked())

    def setChecked(self, checked: bool) -> None:
        self.switch.setChecked(bool(checked))

    def setEnabled(self, enabled: bool) -> None:  # type: ignore[override]
        super().setEnabled(enabled)
        self.switch.setEnabled(enabled)
