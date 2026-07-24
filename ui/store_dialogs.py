from __future__ import annotations

from typing import Literal

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget


SwitchLifetimeChoice = Literal["store", "continue", "cancel"]


def ask_switch_to_lifetime(parent: QWidget | None = None) -> SwitchLifetimeChoice:
    """
    Themed cross-platform dialog for the monthly->lifetime switch confirmation.
    Returns:
      - "store": open Microsoft Store billing page
      - "continue": continue with lifetime purchase
      - "cancel": abort action
    """
    dlg = QDialog(parent)
    dlg.setWindowTitle("Switch to Lifetime")
    dlg.setModal(True)
    dlg.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
    dlg.setMinimumWidth(560)

    bg = "#171B22"
    surface = "#1C212A"
    border = "#313846"
    text = "#EDF2F8"
    muted = "#96A4B6"
    accent = "#4E97FF"
    accent_hover = "#63A6FF"

    try:
        if parent is not None:
            bg = getattr(parent, "_c_surface2", None).name() if getattr(parent, "_c_surface2", None) else bg
            surface = getattr(parent, "_c_surface", None).name() if getattr(parent, "_c_surface", None) else surface
            border = getattr(parent, "_c_border", None).name() if getattr(parent, "_c_border", None) else border
            text = getattr(parent, "_c_text", None).name() if getattr(parent, "_c_text", None) else text
            muted = getattr(parent, "_c_subtle", None).name() if getattr(parent, "_c_subtle", None) else muted
            accent = getattr(parent, "_c_accent", None).name() if getattr(parent, "_c_accent", None) else accent
    except Exception:
        pass

    dlg.setStyleSheet(
        f"""
        QDialog {{
            background: {bg};
            color: {text};
            font-family: "Segoe UI Variable Text", "Segoe UI", "Arial";
            font-size: 13px;
        }}
        QFrame#Card {{
            background: {surface};
            border: 1px solid {border};
            border-radius: 10px;
        }}
        QLabel#Title {{
            color: {text};
            font-size: 16px;
            font-weight: 700;
        }}
        QLabel#Body {{
            color: {muted};
            font-size: 13px;
        }}
        QPushButton {{
            background: {surface};
            border: 1px solid {border};
            border-radius: 8px;
            color: {text};
            padding: 8px 12px;
            min-height: 34px;
            font-weight: 600;
        }}
        QPushButton:hover {{
            border-color: {accent};
        }}
        QPushButton#Primary {{
            background: {accent};
            border-color: {accent};
            color: white;
        }}
        QPushButton#Primary:hover {{
            background: {accent_hover};
            border-color: {accent_hover};
        }}
        """
    )

    root = QVBoxLayout(dlg)
    root.setContentsMargins(14, 14, 14, 14)
    root.setSpacing(10)

    card = QFrame()
    card.setObjectName("Card")
    card_l = QVBoxLayout(card)
    card_l.setContentsMargins(14, 14, 14, 14)
    card_l.setSpacing(10)

    title = QLabel("Monthly subscription is active")
    title.setObjectName("Title")
    body = QLabel(
        "To avoid overlapping charges, cancel monthly auto-renew in Microsoft Store first,\n"
        "then continue with Lifetime purchase."
    )
    body.setObjectName("Body")
    body.setWordWrap(True)
    card_l.addWidget(title)
    card_l.addWidget(body)

    actions = QHBoxLayout()
    actions.setContentsMargins(0, 4, 0, 0)
    actions.setSpacing(8)

    btn_store = QPushButton("Open Microsoft Store Billing")
    btn_continue = QPushButton("Continue to Lifetime")
    btn_continue.setObjectName("Primary")
    btn_cancel = QPushButton("Cancel")

    actions.addWidget(btn_store, stretch=1)
    actions.addWidget(btn_continue, stretch=1)
    actions.addWidget(btn_cancel, stretch=0)

    card_l.addLayout(actions)
    root.addWidget(card)

    result: SwitchLifetimeChoice = "cancel"

    def _choose(choice: SwitchLifetimeChoice) -> None:
        nonlocal result
        result = choice
        dlg.accept()

    btn_store.clicked.connect(lambda: _choose("store"))
    btn_continue.clicked.connect(lambda: _choose("continue"))
    btn_cancel.clicked.connect(lambda: _choose("cancel"))

    dlg.exec()
    return result

