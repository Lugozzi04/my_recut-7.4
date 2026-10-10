"""Playback transport under the viewer: timecode · cut navigation + play · preview options."""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QSlider,
    QToolButton,
    QWidget,
    QWidgetAction,
)

from ui.design_tokens import H_TRANSPORT, Palette, palette
from ui.widgets.glyphs import glyph_icon


def _tool(tip: str, name: str = "TransportButton", size: int = 32) -> QToolButton:
    b = QToolButton()
    b.setObjectName(name)
    b.setToolTip(tip)
    b.setCursor(Qt.PointingHandCursor)
    b.setFixedSize(size, size)
    b.setIconSize(QSize(18, 18))
    b.setFocusPolicy(Qt.NoFocus)
    return b


class TransportBar(QFrame):
    prevCutClicked = Signal()
    nextCutClicked = Signal()
    backClicked = Signal()
    forwardClicked = Signal()
    skipToggled = Signal(bool)

    def __init__(self, play_button: QToolButton, volume_slider: QSlider, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Transport")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedHeight(H_TRANSPORT)
        self._pal = palette(False)
        self._playing = False
        self._volume = 100

        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 0, 10, 0)
        lay.setSpacing(4)

        # timecode
        self.tc_current = QLabel("00:00")
        self.tc_current.setObjectName("TimecodeCurrent")
        self.tc_total = QLabel("/ 00:00")
        self.tc_total.setObjectName("TimecodeTotal")
        tc = QWidget()
        tc_l = QHBoxLayout(tc)
        tc_l.setContentsMargins(0, 0, 0, 0)
        tc_l.setSpacing(6)
        tc_l.addWidget(self.tc_current)
        tc_l.addWidget(self.tc_total)
        tc_l.addStretch(1)
        tc.setMinimumWidth(170)
        lay.addWidget(tc)
        lay.addStretch(1)

        # centre group
        self.btn_prev_cut = _tool("Previous cut  (↑ / [)")
        self.btn_back = _tool("Back 5 s  (J)")
        self.btn_play = play_button
        self.btn_play.setObjectName("PlayButton")
        self.btn_play.setFixedSize(40, 40)
        self.btn_play.setIconSize(QSize(20, 20))
        self.btn_play.setCursor(Qt.PointingHandCursor)
        self.btn_play.setFocusPolicy(Qt.NoFocus)
        self.btn_play.setToolTip("Play / Pause  (Space, K)")
        self.btn_forward = _tool("Forward 5 s  (L)")
        self.btn_next_cut = _tool("Next cut  (↓ / ])")
        for w in (self.btn_prev_cut, self.btn_back):
            lay.addWidget(w)
        lay.addSpacing(6)
        lay.addWidget(self.btn_play)
        lay.addSpacing(6)
        for w in (self.btn_forward, self.btn_next_cut):
            lay.addWidget(w)
        lay.addStretch(1)

        # right group
        self.btn_skip = QToolButton()
        self.btn_skip.setObjectName("PillToggle")
        self.btn_skip.setText("Skip cuts")
        self.btn_skip.setCheckable(True)
        self.btn_skip.setChecked(True)
        self.btn_skip.setCursor(Qt.PointingHandCursor)
        self.btn_skip.setFocusPolicy(Qt.NoFocus)
        self.btn_skip.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.btn_skip.setIconSize(QSize(14, 14))
        self.btn_skip.setFixedHeight(28)
        self.btn_skip.setToolTip("Preview the edit: jump over cut sections during playback")
        lay.addWidget(self.btn_skip)
        lay.addSpacing(4)

        self.btn_volume = _tool("Preview volume")
        self.btn_volume.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(self.btn_volume)
        menu.setObjectName("VolumeMenu")
        holder = QWidget()
        hl = QHBoxLayout(holder)
        hl.setContentsMargins(12, 8, 12, 8)
        hl.setSpacing(8)
        volume_slider.setOrientation(Qt.Horizontal)
        volume_slider.setFixedWidth(150)
        self.volume_value = QLabel("100%")
        self.volume_value.setObjectName("VolumeValue")
        self.volume_value.setFixedWidth(36)
        hl.addWidget(volume_slider)
        hl.addWidget(self.volume_value)
        act = QWidgetAction(menu)
        act.setDefaultWidget(holder)
        menu.addAction(act)
        self.btn_volume.setMenu(menu)
        self.volume_slider = volume_slider
        lay.addWidget(self.btn_volume)

        self.btn_prev_cut.clicked.connect(self.prevCutClicked.emit)
        self.btn_next_cut.clicked.connect(self.nextCutClicked.emit)
        self.btn_back.clicked.connect(self.backClicked.emit)
        self.btn_forward.clicked.connect(self.forwardClicked.emit)
        self.btn_skip.toggled.connect(self._on_skip)
        self.set_cuts_ready(False)

    def _on_skip(self, checked: bool) -> None:
        self._refresh_skip_icon()
        self.skipToggled.emit(bool(checked))

    # ---- API -------------------------------------------------------------------------------
    def apply_palette(self, pal: Palette) -> None:
        self._pal = pal
        c, d = pal.text, pal.text_3
        self.btn_prev_cut.setIcon(glyph_icon("prev_cut", c, 18, disabled=d))
        self.btn_next_cut.setIcon(glyph_icon("next_cut", c, 18, disabled=d))
        self.btn_back.setIcon(glyph_icon("back", c, 18, disabled=d))
        self.btn_forward.setIcon(glyph_icon("forward", c, 18, disabled=d))
        self._refresh_play_icon()
        self._refresh_skip_icon()
        self._refresh_volume_icon()

    def _refresh_play_icon(self) -> None:
        self.btn_play.setIcon(glyph_icon("pause" if self._playing else "play", "#FFFFFF", 20,
                                         disabled=self._pal.text_3))

    def _refresh_skip_icon(self) -> None:
        color = self._pal.accent if self.btn_skip.isChecked() else self._pal.text_2
        self.btn_skip.setIcon(glyph_icon("scissors", color, 14))

    def _refresh_volume_icon(self) -> None:
        self.btn_volume.setIcon(glyph_icon("mute" if self._volume <= 0 else "volume", self._pal.text, 18))

    def set_timecode(self, current: str, total: str) -> None:
        self.tc_current.setText(str(current))
        self.tc_total.setText(f"/ {total}")

    def set_playing(self, playing: bool) -> None:
        playing = bool(playing)
        if playing != self._playing:
            self._playing = playing
            self._refresh_play_icon()

    def set_cuts_ready(self, ready: bool) -> None:
        for b in (self.btn_prev_cut, self.btn_next_cut):
            b.setEnabled(bool(ready))

    def set_media_ready(self, ready: bool) -> None:
        for b in (self.btn_back, self.btn_forward):
            b.setEnabled(bool(ready))

    def set_skip(self, enabled: bool) -> None:
        if self.btn_skip.isChecked() != bool(enabled):
            self.btn_skip.blockSignals(True)
            self.btn_skip.setChecked(bool(enabled))
            self.btn_skip.blockSignals(False)
        self._refresh_skip_icon()

    def set_volume(self, pct: int) -> None:
        self._volume = int(pct)
        self.volume_value.setText(f"{int(pct)}%")
        self.btn_volume.setToolTip(f"Preview volume: {int(pct)}%")
        self._refresh_volume_icon()
