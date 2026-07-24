from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


@dataclass
class _Palette:
    bg: str
    surface: str
    border: str
    text: str
    muted: str
    accent: str
    accent_hover: str
    danger: str
    danger_hover: str
    icon_bg_info: str
    icon_bg_warn: str
    icon_bg_error: str
    icon_bg_question: str


def _color_from_parent(parent, attr: str, fallback: str) -> str:
    if parent is None:
        return fallback
    try:
        value = getattr(parent, attr, None)
        if isinstance(value, QColor):
            return value.name()
    except Exception:
        pass
    return fallback


def _resolve_palette(parent) -> _Palette:
    light = bool(getattr(parent, "_theme_light", False)) if parent is not None else False
    if light:
        bg = _color_from_parent(parent, "_c_surface2", "#edf2f8")
        surface = _color_from_parent(parent, "_c_surface", "#ffffff")
        border = _color_from_parent(parent, "_c_border", "#c9d3e3")
        text = _color_from_parent(parent, "_c_text", "#172030")
        muted = _color_from_parent(parent, "_c_subtle", "#55657d")
        accent = _color_from_parent(parent, "_c_accent", "#2f7df6")
        return _Palette(
            bg=bg,
            surface=surface,
            border=border,
            text=text,
            muted=muted,
            accent=accent,
            accent_hover="#4d94ff",
            danger="#d04747",
            danger_hover="#e15f5f",
            icon_bg_info="rgba(47,125,246,0.16)",
            icon_bg_warn="rgba(255,165,0,0.18)",
            icon_bg_error="rgba(208,71,71,0.18)",
            icon_bg_question="rgba(82,98,140,0.20)",
        )

    bg = _color_from_parent(parent, "_c_surface2", "#151b24")
    surface = _color_from_parent(parent, "_c_surface", "#1d2430")
    border = _color_from_parent(parent, "_c_border", "rgba(255,255,255,0.12)")
    text = _color_from_parent(parent, "_c_text", "#edf2f8")
    muted = _color_from_parent(parent, "_c_subtle", "rgba(237,242,248,0.72)")
    accent = _color_from_parent(parent, "_c_accent", "#5ea4ff")
    return _Palette(
        bg=bg,
        surface=surface,
        border=border,
        text=text,
        muted=muted,
        accent=accent,
        accent_hover="#73b2ff",
        danger="#e06262",
        danger_hover="#ef7878",
        icon_bg_info="rgba(94,164,255,0.20)",
        icon_bg_warn="rgba(255,190,92,0.22)",
        icon_bg_error="rgba(224,98,98,0.22)",
        icon_bg_question="rgba(148,166,205,0.22)",
    )


class _ProMessageDialog(QDialog):
    def __init__(self, parent, title: str, text: str, icon: int, palette: _Palette):
        super().__init__(parent)
        self.setObjectName("ProMsgDialog")
        self.setModal(True)
        self.setWindowTitle(str(title or "Message"))
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setMinimumWidth(500)
        self.setMaximumWidth(760)
        self.setSizeGripEnabled(False)

        self._palette = palette
        self._icon = int(icon)
        self._clicked_button: QPushButton | None = None
        self._drag_delta: QPoint | None = None

        self._build_ui(str(title or "Message"), str(text or ""))
        self._apply_style()
        self._refresh_icon_badge()

    def _build_ui(self, title: str, text: str) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(0)

        self._shell = QFrame()
        self._shell.setObjectName("ProMsgShell")
        shell_l = QVBoxLayout(self._shell)
        shell_l.setContentsMargins(0, 0, 0, 0)
        shell_l.setSpacing(0)

        self._card = QFrame()
        self._card.setObjectName("ProMsgCard")
        card_l = QVBoxLayout(self._card)
        card_l.setContentsMargins(14, 12, 14, 12)
        card_l.setSpacing(12)

        self._header = QFrame()
        self._header.setObjectName("ProMsgHeader")
        header_l = QHBoxLayout(self._header)
        header_l.setContentsMargins(2, 0, 2, 4)
        header_l.setSpacing(8)

        self._title = QLabel(str(title or "Message"))
        self._title.setObjectName("ProMsgTitle")
        self._btn_close = QPushButton("x")
        self._btn_close.setObjectName("ProMsgClose")
        self._btn_close.setAutoDefault(False)
        self._btn_close.setDefault(False)
        self._btn_close.clicked.connect(self.reject)
        header_l.addWidget(self._title, 1)
        header_l.addWidget(self._btn_close, 0, Qt.AlignRight)

        body = QWidget()
        body_l = QHBoxLayout(body)
        body_l.setContentsMargins(0, 0, 0, 0)
        body_l.setSpacing(12)

        self._icon_badge = QLabel("?")
        self._icon_badge.setObjectName("ProMsgIconBadge")
        self._icon_badge.setAlignment(Qt.AlignCenter)
        self._icon_badge.setFixedSize(42, 42)

        self._text = QLabel(text)
        self._text.setObjectName("ProMsgText")
        self._text.setWordWrap(True)
        self._text.setTextFormat(Qt.AutoText)
        self._text.setOpenExternalLinks(True)
        self._text.setMinimumWidth(320)

        body_l.addWidget(self._icon_badge, 0, Qt.AlignTop)
        body_l.addWidget(self._text, 1)

        self._buttons_wrap = QWidget()
        self._buttons_wrap.setObjectName("ProMsgButtonsWrap")
        self._buttons_l = QHBoxLayout(self._buttons_wrap)
        self._buttons_l.setContentsMargins(0, 8, 0, 0)
        self._buttons_l.setSpacing(8)
        self._buttons_l.addStretch(1)

        card_l.addWidget(self._header)
        card_l.addWidget(body)
        card_l.addWidget(self._buttons_wrap)
        shell_l.addWidget(self._card)
        root.addWidget(self._shell)

        try:
            shadow = QGraphicsDropShadowEffect(self._shell)
            shadow.setBlurRadius(26)
            shadow.setOffset(0, 8)
            shadow.setColor(QColor(0, 0, 0, 120))
            self._shell.setGraphicsEffect(shadow)
        except Exception:
            pass

    def _apply_style(self) -> None:
        p = self._palette
        self.setStyleSheet(
            f"""
            QDialog#ProMsgDialog {{
                background: transparent;
                color: {p.text};
                font-family: "Segoe UI Variable Text", "Segoe UI", "Bahnschrift", "Arial";
                font-size: 13px;
            }}
            QFrame#ProMsgShell {{
                background: transparent;
                border: none;
            }}
            QFrame#ProMsgCard {{
                background: {p.surface};
                border: 1px solid {p.border};
                border-radius: 14px;
            }}
            QFrame#ProMsgHeader {{
                background: transparent;
                border: none;
                border-bottom: 1px solid {p.border};
                margin: 0px;
                padding: 0px;
            }}
            QLabel#ProMsgTitle {{
                color: {p.text};
                font-size: 13px;
                font-weight: 800;
                letter-spacing: 0.2px;
                padding: 0px 0px 2px 2px;
            }}
            QPushButton#ProMsgClose {{
                min-width: 28px;
                max-width: 28px;
                min-height: 26px;
                max-height: 26px;
                border-radius: 7px;
                padding: 0px;
                font-size: 12px;
                font-weight: 800;
                background: transparent;
                border: 1px solid {p.border};
                color: {p.muted};
            }}
            QPushButton#ProMsgClose:hover {{
                color: {p.text};
                border-color: {p.accent};
                background: {p.bg};
            }}
            QLabel#ProMsgIconBadge {{
                font-size: 18px;
                font-weight: 800;
                color: {p.text};
                border: 1px solid {p.border};
                border-radius: 21px;
                background: {p.icon_bg_question};
            }}
            QLabel#ProMsgText {{
                color: {p.text};
                font-size: 14px;
                font-weight: 700;
                line-height: 1.35;
                padding-top: 3px;
            }}
            QWidget#ProMsgButtonsWrap {{
                border-top: 1px solid {p.border};
                background: transparent;
            }}
            QPushButton {{
                min-width: 122px;
                min-height: 40px;
                border-radius: 11px;
                padding: 8px 14px;
                font-size: 12px;
                font-weight: 700;
                background: {p.surface};
                color: {p.text};
                border: 1px solid {p.border};
            }}
            QPushButton:hover {{
                border-color: {p.accent};
                background: {p.bg};
            }}
            QPushButton:pressed {{
                border-color: {p.accent_hover};
            }}
            QPushButton:focus {{
                border-color: {p.accent};
            }}
            QPushButton#Primary {{
                border-color: {p.accent};
                background: {p.accent};
                color: white;
            }}
            QPushButton#Primary:hover {{
                background: {p.accent_hover};
                border-color: {p.accent_hover};
            }}
            QPushButton#Danger {{
                border-color: {p.danger};
                background: {p.danger};
                color: white;
            }}
            QPushButton#Danger:hover {{
                border-color: {p.danger_hover};
                background: {p.danger_hover};
            }}
            """
        )

    def _refresh_icon_badge(self) -> None:
        p = self._palette
        icon_char = "?"
        bg = p.icon_bg_question
        if self._icon == QMessageBox.Information:
            icon_char = "i"
            bg = p.icon_bg_info
        elif self._icon == QMessageBox.Warning:
            icon_char = "!"
            bg = p.icon_bg_warn
        elif self._icon == QMessageBox.Critical:
            icon_char = "!"
            bg = p.icon_bg_error
        elif self._icon == QMessageBox.Question:
            icon_char = "?"
            bg = p.icon_bg_question
        else:
            icon_char = "i"
            bg = p.icon_bg_info
        self._icon_badge.setText(icon_char)
        self._icon_badge.setStyleSheet(
            f"background:{bg}; border:1px solid {p.border}; border-radius:20px; color:{p.text};"
        )

    def set_main_text(self, text: str) -> None:
        self._text.setText(str(text or ""))

    def set_icon(self, icon: int) -> None:
        self._icon = int(icon)
        self._refresh_icon_badge()

    def set_title(self, title: str) -> None:
        t = str(title or "Message")
        self.setWindowTitle(t)
        self._title.setText(t)

    def add_action(self, label: str, result_code: int, *, kind: str = "normal", is_default: bool = False) -> QPushButton:
        btn = QPushButton(str(label or "OK"))
        if kind == "primary":
            btn.setObjectName("Primary")
        elif kind == "danger":
            btn.setObjectName("Danger")

        btn.setAutoDefault(bool(is_default))
        btn.setDefault(bool(is_default))
        btn.clicked.connect(lambda _=False, c=int(result_code), b=btn: self._on_button_clicked(c, b))
        self._buttons_l.addWidget(btn)
        return btn

    def _on_button_clicked(self, result_code: int, btn: QPushButton) -> None:
        self._clicked_button = btn
        self.done(int(result_code))

    def clicked_button(self) -> QPushButton | None:
        return self._clicked_button

    def exec(self) -> int:  # type: ignore[override]
        self._center_on_parent()
        self.adjustSize()
        w = max(500, min(740, int(self.sizeHint().width()) + 18))
        h = max(210, int(self.sizeHint().height()) + 12)
        self.resize(w, h)
        return super().exec()

    def _center_on_parent(self) -> None:
        try:
            p = self.parentWidget()
            if p is not None:
                g = p.frameGeometry()
                self.move(g.center() - self.rect().center())
                return
        except Exception:
            pass
        try:
            scr = self.screen()
            if scr is not None:
                g = scr.availableGeometry()
                self.move(g.center() - self.rect().center())
        except Exception:
            pass

    def mousePressEvent(self, e):  # type: ignore[override]
        if e.button() == Qt.LeftButton:
            try:
                if self._header.geometry().contains(e.pos()):
                    self._drag_delta = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
                    e.accept()
                    return
            except Exception:
                pass
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):  # type: ignore[override]
        if self._drag_delta is not None and (e.buttons() & Qt.LeftButton):
            try:
                self.move(e.globalPosition().toPoint() - self._drag_delta)
                e.accept()
                return
            except Exception:
                pass
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):  # type: ignore[override]
        self._drag_delta = None
        super().mouseReleaseEvent(e)


class QMessageBox:
    # Icon enums
    NoIcon = 0
    Information = 1
    Warning = 2
    Critical = 3
    Question = 4

    # Standard buttons bitmask values (Qt-compatible semantics)
    Ok = 0x00000400
    Yes = 0x00004000
    No = 0x00010000
    Cancel = 0x00400000

    # Roles (subset used by this app)
    AcceptRole = 0
    RejectRole = 1
    DestructiveRole = 2

    def __init__(self, parent=None):
        self._parent = parent
        self._palette = _resolve_palette(parent)
        self._title = "Message"
        self._text = ""
        self._informative_text = ""
        self._icon = self.NoIcon
        self._buttons_added = False
        self._button_results: dict[QPushButton, int] = {}
        self._clicked_button: QPushButton | None = None

        self._dlg = _ProMessageDialog(parent, self._title, self._text, self._icon, self._palette)

    def setWindowTitle(self, title: str) -> None:
        self._title = str(title or "Message")
        self._dlg.set_title(self._title)

    def setText(self, text: str) -> None:
        self._text = str(text or "")
        self._sync_text()

    def setIcon(self, icon: int) -> None:
        self._icon = int(icon)
        self._dlg.set_icon(self._icon)

    def setInformativeText(self, text: str) -> None:
        self._informative_text = str(text or "").strip()
        self._sync_text()

    def _sync_text(self) -> None:
        if self._informative_text:
            txt = f"{self._text}\n\n<span style='font-weight:500;color:{self._palette.muted};'>{self._informative_text}</span>"
        else:
            txt = self._text
        self._dlg.set_main_text(txt)

    @staticmethod
    def _text_to_result(text: str, role: int) -> int:
        low = str(text or "").strip().lower()
        if low in {"yes", "si", "sì", "ok", "confirm"}:
            return QMessageBox.Yes if low in {"yes", "si", "sì"} else QMessageBox.Ok
        if low in {"no", "cancel", "annulla"}:
            return QMessageBox.No if low == "no" else QMessageBox.Cancel
        if int(role) == QMessageBox.RejectRole:
            return QMessageBox.Cancel
        return QMessageBox.Ok

    def addButton(self, text: str, role: int) -> QPushButton:
        result_code = self._text_to_result(text, role)
        kind = "normal"
        if int(role) == self.DestructiveRole or any(
            k in str(text or "").strip().lower() for k in ("remove", "delete", "reset", "clear")
        ):
            kind = "danger"
        elif result_code in (self.Yes, self.Ok):
            kind = "primary"

        is_default = not self._buttons_added and result_code in (self.Yes, self.Ok)
        btn = self._dlg.add_action(str(text or "OK"), int(result_code), kind=kind, is_default=is_default)
        self._buttons_added = True
        self._button_results[btn] = int(result_code)
        return btn

    def clickedButton(self) -> QPushButton | None:
        return self._clicked_button

    def exec(self) -> int:
        if not self._buttons_added:
            self.addButton("OK", self.AcceptRole)
        result = int(self._dlg.exec())
        self._clicked_button = self._dlg.clicked_button()
        if result in (self.Ok, self.Yes, self.No, self.Cancel):
            return result
        if self._clicked_button in self._button_results:
            return int(self._button_results[self._clicked_button])
        return self.Cancel

    @classmethod
    def _available_codes(cls, buttons: int) -> list[int]:
        out: list[int] = []
        if buttons & cls.Yes:
            out.append(cls.Yes)
        if buttons & cls.No:
            out.append(cls.No)
        if buttons & cls.Cancel:
            out.append(cls.Cancel)
        if buttons & cls.Ok:
            out.append(cls.Ok)
        if not out:
            out.append(cls.Ok)
        return out

    @classmethod
    def _button_label(cls, code: int) -> str:
        if code == cls.Yes:
            return "Yes"
        if code == cls.No:
            return "No"
        if code == cls.Cancel:
            return "Cancel"
        return "OK"

    @classmethod
    def _run_dialog(
        cls,
        parent,
        title: str,
        text: str,
        icon: int,
        buttons: int,
        defaultButton: int | None,
    ) -> int:
        palette = _resolve_palette(parent)
        dlg = _ProMessageDialog(parent, str(title or "Message"), str(text or ""), int(icon), palette)

        codes = cls._available_codes(int(buttons))
        if cls.Yes in codes and cls.No in codes:
            ordered = [c for c in (cls.Yes, cls.No, cls.Cancel, cls.Ok) if c in codes]
        else:
            ordered = [c for c in (cls.Ok, cls.Yes, cls.No, cls.Cancel) if c in codes]

        default_code: int | None = None
        if defaultButton is not None:
            try:
                dc = int(defaultButton)
                if dc in ordered:
                    default_code = dc
            except Exception:
                default_code = None
        fallback = int(default_code) if default_code is not None else int(ordered[0])

        for code in ordered:
            kind = "normal"
            if code in (cls.Yes, cls.Ok):
                if (default_code is not None and default_code == code) or (default_code is None and code == fallback):
                    kind = "primary"
            is_default = bool(default_code == code) if default_code is not None else bool(code == fallback)
            dlg.add_action(cls._button_label(code), int(code), kind=kind, is_default=is_default)

        result = int(dlg.exec())
        if result in ordered:
            return result
        return int(fallback)

    @classmethod
    def information(cls, parent, title: str, text: str):
        cls._run_dialog(parent, str(title or "Information"), str(text or ""), cls.Information, cls.Ok, cls.Ok)
        return cls.Ok

    @classmethod
    def warning(cls, parent, title: str, text: str):
        cls._run_dialog(parent, str(title or "Warning"), str(text or ""), cls.Warning, cls.Ok, cls.Ok)
        return cls.Ok

    @classmethod
    def critical(cls, parent, title: str, text: str):
        cls._run_dialog(parent, str(title or "Error"), str(text or ""), cls.Critical, cls.Ok, cls.Ok)
        return cls.Ok

    @classmethod
    def question(cls, parent, title: str, text: str, buttons: int = None, defaultButton: int | None = None):
        if buttons is None:
            buttons = cls.Yes | cls.No
        return cls._run_dialog(
            parent,
            str(title or "Confirm"),
            str(text or ""),
            cls.Question,
            int(buttons),
            defaultButton,
        )


class _InputDialogBase(QDialog):
    def __init__(self, parent, title: str, label: str):
        super().__init__(parent)
        self.setObjectName("ProInputDialog")
        self.setModal(True)
        self.setWindowTitle(str(title or "Input"))
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self.setWindowFlag(Qt.WindowMaximizeButtonHint, False)
        self.setWindowFlag(Qt.WindowMinimizeButtonHint, False)
        self.setMinimumWidth(520)
        self.setMaximumWidth(760)

        try:
            if parent is not None:
                self.setWindowIcon(parent.windowIcon())
        except Exception:
            pass

        self._palette = _resolve_palette(parent)
        self._accepted = False
        self._label_text = str(label or "")
        self._build_base_ui()
        self._apply_style()

    def _build_base_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(0)

        self._card = QFrame()
        self._card.setObjectName("ProInputCard")
        card_l = QVBoxLayout(self._card)
        card_l.setContentsMargins(14, 14, 14, 12)
        card_l.setSpacing(10)

        self._label = QLabel(self._label_text)
        self._label.setObjectName("ProInputLabel")
        self._label.setWordWrap(True)

        self._field_host = QWidget()
        self._field_l = QVBoxLayout(self._field_host)
        self._field_l.setContentsMargins(0, 0, 0, 0)
        self._field_l.setSpacing(0)

        actions = QWidget()
        actions.setObjectName("ProInputActions")
        actions_l = QHBoxLayout(actions)
        actions_l.setContentsMargins(0, 10, 0, 0)
        actions_l.setSpacing(8)
        actions_l.addStretch(1)

        self._btn_cancel = QPushButton("Cancel")
        self._btn_ok = QPushButton("OK")
        self._btn_ok.setObjectName("Primary")
        self._btn_cancel.clicked.connect(self.reject)
        self._btn_ok.clicked.connect(self.accept)
        actions_l.addWidget(self._btn_cancel)
        actions_l.addWidget(self._btn_ok)

        card_l.addWidget(self._label)
        card_l.addWidget(self._field_host)
        card_l.addWidget(actions)
        root.addWidget(self._card)

    def _apply_style(self) -> None:
        p = self._palette
        self.setStyleSheet(
            f"""
            QDialog#ProInputDialog {{
                background: {p.bg};
                color: {p.text};
                font-family: "Segoe UI Variable Text", "Segoe UI", "Bahnschrift", "Arial";
                font-size: 13px;
            }}
            QFrame#ProInputCard {{
                background: {p.surface};
                border: 1px solid {p.border};
                border-radius: 12px;
            }}
            QLabel#ProInputLabel {{
                color: {p.text};
                font-size: 13px;
                font-weight: 600;
                padding: 2px 0 4px 0;
            }}
            QWidget#ProInputActions {{
                border-top: 1px solid {p.border};
                background: transparent;
            }}
            QLineEdit, QSpinBox {{
                background: {p.bg};
                border: 1px solid {p.border};
                border-radius: 8px;
                padding: 8px 10px;
                min-height: 34px;
                color: {p.text};
                font-size: 13px;
                selection-background-color: {p.accent};
                selection-color: white;
            }}
            QLineEdit:focus, QSpinBox:focus {{
                border-color: {p.accent};
            }}
            QSpinBox::up-button, QSpinBox::down-button {{
                width: 16px;
                border: none;
                background: transparent;
            }}
            QPushButton {{
                min-width: 124px;
                min-height: 36px;
                border-radius: 9px;
                padding: 8px 14px;
                font-size: 12px;
                font-weight: 700;
                background: {p.surface};
                color: {p.text};
                border: 1px solid {p.border};
            }}
            QPushButton:hover {{
                border-color: {p.accent};
                background: {p.bg};
            }}
            QPushButton#Primary {{
                border-color: {p.accent};
                background: {p.accent};
                color: white;
            }}
            QPushButton#Primary:hover {{
                background: {p.accent_hover};
                border-color: {p.accent_hover};
            }}
            """
        )

    def accept(self) -> None:  # type: ignore[override]
        self._accepted = True
        super().accept()

    def was_accepted(self) -> bool:
        return bool(self._accepted)


def get_text(parent, title: str, label: str, text: str = "") -> tuple[str, bool]:
    dlg = _InputDialogBase(parent, title=title, label=label)
    edit = QLineEdit()
    edit.setText(str(text or ""))
    edit.selectAll()
    dlg._field_l.addWidget(edit)
    dlg._btn_ok.setDefault(True)
    dlg._btn_ok.setAutoDefault(True)
    edit.returnPressed.connect(dlg.accept)
    dlg.exec()
    return edit.text(), dlg.was_accepted()


def get_int(
    parent,
    title: str,
    label: str,
    value: int,
    minimum: int,
    maximum: int,
    step: int = 1,
) -> tuple[int, bool]:
    dlg = _InputDialogBase(parent, title=title, label=label)
    spin = QSpinBox()
    spin.setRange(int(minimum), int(maximum))
    spin.setSingleStep(max(1, int(step)))
    spin.setValue(int(value))
    dlg._field_l.addWidget(spin)
    dlg._btn_ok.setDefault(True)
    dlg._btn_ok.setAutoDefault(True)
    dlg.exec()
    return int(spin.value()), dlg.was_accepted()
