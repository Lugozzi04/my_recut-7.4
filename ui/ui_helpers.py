from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget


def section_title(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("SectionTitle")
    return lbl


def hint(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("SubtleHint")
    lbl.setWordWrap(True)
    return lbl


def field(title: str, helper: str, widget: QWidget) -> QWidget:
    w = QWidget()
    w.setObjectName("FieldBlock")
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(4)
    title_lbl = section_title(title)
    title_lbl.setObjectName("FieldTitle")
    lay.addWidget(title_lbl)
    if helper:
        helper_lbl = hint(helper)
        helper_lbl.setObjectName("FieldHint")
        lay.addWidget(helper_lbl)
    lay.addWidget(widget)
    lay.setAlignment(widget, Qt.AlignTop)
    return w


# (card shadow removed to avoid overflow/scrollbars)
