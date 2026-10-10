"""Application top bar: identity + project status on the left, primary actions on the right."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QToolButton, QWidget

from ui.design_tokens import H_TOPBAR, Palette, palette
from ui.widgets.glyphs import draw_glyph, glyph_icon

STATUS_KINDS = ("idle", "busy", "ok", "warn", "error")


class _LogoMark(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedSize(26, 26)
        self._color = QColor("#4D8DFF")

    def set_color(self, color: QColor) -> None:
        self._color = QColor(color)
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        bg = QColor(self._color)
        bg.setAlpha(38)
        p.setBrush(bg)
        p.drawRoundedRect(QRectF(self.rect()), 7, 7)
        draw_glyph(p, "logo", QRectF(5, 5, 16, 16), self._color)
        p.end()


class StatusPill(QWidget):
    """Dot + text. `busy` pulses the dot; the text is elided to keep the bar stable."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._text = ""
        self._kind = "idle"
        self._pal: Palette = palette(False)
        self._phase = 0
        self._timer = QTimer(self)
        self._timer.setInterval(90)
        self._timer.timeout.connect(self._tick)
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        self.setFixedHeight(24)

    def set_palette_tokens(self, pal: Palette) -> None:
        self._pal = pal
        self.update()

    def set_status(self, text: str, kind: str = "idle") -> None:
        kind = kind if kind in STATUS_KINDS else "idle"
        if text == self._text and kind == self._kind:
            return
        self._text = str(text or "")
        self._kind = kind
        if kind == "busy":
            if not self._timer.isActive():
                self._timer.start()
        else:
            self._timer.stop()
            self._phase = 0
        self.setVisible(bool(self._text))
        self._fit()
        self.update()

    def _fit(self) -> None:
        fm = QFontMetrics(self.font())
        self.setFixedWidth(min(340, fm.horizontalAdvance(self._text) + 36))

    def changeEvent(self, e):
        super().changeEvent(e)
        if e.type() in (QEvent.FontChange, QEvent.StyleChange):
            self._fit()

    def text(self) -> str:
        return self._text

    def kind(self) -> str:
        return self._kind

    def _tick(self) -> None:
        self._phase = (self._phase + 1) % 20
        self.update()

    def _dot_color(self) -> QColor:
        pal = self._pal
        return QColor({
            "idle": pal.text_3,
            "busy": pal.accent,
            "ok": pal.success,
            "warn": pal.warning,
            "error": pal.danger,
        }[self._kind])

    def sizeHint(self) -> QSize:
        fm = QFontMetrics(self.font())
        return QSize(min(320, fm.horizontalAdvance(self._text) + 30), 24)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        bg = QColor(self._pal.raised)
        p.setPen(QColor(self._pal.border))
        p.setBrush(bg)
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        dot = self._dot_color()
        if self._kind == "busy":
            alpha = 110 + int(145 * abs(10 - self._phase) / 10.0)
            dot.setAlpha(alpha)
        p.setPen(Qt.NoPen)
        p.setBrush(dot)
        p.drawEllipse(QRectF(10, r.center().y() - 3.5, 7, 7))
        p.setPen(QColor(self._pal.text_2 if self._kind == "idle" else self._pal.text))
        fm = QFontMetrics(self.font())
        text = fm.elidedText(self._text, Qt.ElideRight, int(r.width() - 30))
        p.drawText(QRectF(23, 0, r.width() - 28, r.height()), Qt.AlignVCenter | Qt.AlignLeft, text)
        p.end()


class TopBar(QFrame):
    importClicked = Signal()
    saveClicked = Signal()
    exportClicked = Signal()
    menuClicked = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("TopBar")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedHeight(H_TOPBAR)
        self._pal = palette(False)
        self._progress: float | None = None

        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 0, 12, 0)
        lay.setSpacing(10)

        self.logo = _LogoMark()
        lay.addWidget(self.logo)
        self.app_name = QLabel("ReCut")
        self.app_name.setObjectName("AppName")
        lay.addWidget(self.app_name)

        self.sep = QLabel("/")
        self.sep.setObjectName("TopBarSep")
        lay.addWidget(self.sep)

        self.project_name = QLabel("Untitled")
        self.project_name.setObjectName("ProjectName")
        self.project_name.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.project_name.setMaximumWidth(420)
        lay.addWidget(self.project_name)

        self.dirty_dot = QLabel("●")
        self.dirty_dot.setObjectName("DirtyDot")
        self.dirty_dot.setToolTip("Unsaved changes")
        self.dirty_dot.setVisible(False)
        lay.addWidget(self.dirty_dot)

        lay.addSpacing(6)
        self.status = StatusPill()
        lay.addWidget(self.status)
        lay.addStretch(1)

        self.btn_import = QPushButton("Import")
        self.btn_import.setObjectName("TopButton")
        self.btn_import.setToolTip("Import video (Ctrl+O)")
        self.btn_save = QPushButton("Save")
        self.btn_save.setObjectName("TopButton")
        self.btn_save.setToolTip("Save project (Ctrl+S)")
        self.btn_export = QPushButton("Export")
        self.btn_export.setObjectName("PrimaryButton")
        self.btn_export.setToolTip("Open export settings (Ctrl+E)")
        self.btn_more = QToolButton()
        self.btn_more.setObjectName("TopIconButton")
        self.btn_more.setToolTip("Menu: project, view, preferences, help")
        self.btn_more.setIconSize(QSize(18, 18))
        for b in (self.btn_import, self.btn_save, self.btn_export):
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedHeight(32)
            b.setIconSize(QSize(16, 16))
        self.btn_more.setCursor(Qt.PointingHandCursor)
        self.btn_more.setFixedSize(32, 32)
        lay.addWidget(self.btn_import)
        lay.addWidget(self.btn_save)
        lay.addSpacing(2)
        lay.addWidget(self.btn_export)
        lay.addWidget(self.btn_more)

        self.btn_import.clicked.connect(self.importClicked.emit)
        self.btn_save.clicked.connect(self.saveClicked.emit)
        self.btn_export.clicked.connect(self.exportClicked.emit)
        self.btn_more.clicked.connect(self.menuClicked.emit)

        self.set_video_loaded(False)
        self.status.set_status("No video", "idle")

    # ---- API -------------------------------------------------------------------------------
    def apply_palette(self, pal: Palette) -> None:
        self._pal = pal
        self.logo.set_color(QColor(pal.accent))
        self.status.set_palette_tokens(pal)
        self.btn_import.setIcon(glyph_icon("import", pal.text_2, 16, disabled=pal.text_3))
        self.btn_save.setIcon(glyph_icon("save", pal.text_2, 16, disabled=pal.text_3))
        self.btn_export.setIcon(glyph_icon("export", "#FFFFFF", 16, disabled=pal.text_3))
        self.btn_more.setIcon(glyph_icon("more", pal.text_2, 18))
        self.update()

    def set_project_name(self, name: str, tooltip: str = "") -> None:
        name = str(name or "").strip() or "Untitled"
        fm = QFontMetrics(self.project_name.font())
        self.project_name.setText(fm.elidedText(name, Qt.ElideMiddle, 400))
        self.project_name.setToolTip(tooltip or name)

    def set_dirty(self, dirty: bool) -> None:
        self.dirty_dot.setVisible(bool(dirty))

    def set_status(self, text: str, kind: str = "idle") -> None:
        self.status.set_status(text, kind)

    def set_progress(self, pct: float | None) -> None:
        if pct is None or pct <= 0 or pct >= 100:
            value = None
        else:
            value = max(0.0, min(100.0, float(pct)))
        if value != self._progress:
            self._progress = value
            self.update()

    def progress(self) -> float | None:
        return self._progress

    def set_video_loaded(self, loaded: bool) -> None:
        self.btn_save.setEnabled(bool(loaded))
        self.btn_export.setEnabled(bool(loaded))

    def paintEvent(self, e):
        super().paintEvent(e)
        if self._progress is None:
            return
        p = QPainter(self)
        w = self.width() * (self._progress / 100.0)
        p.fillRect(QRectF(0, self.height() - 2, w, 2), QColor(self._pal.accent))
        p.end()
