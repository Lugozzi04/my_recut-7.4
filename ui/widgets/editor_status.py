"""Compact edit statistics shown on the right of the status bar."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QWidget

from ui.design_tokens import Palette, palette


class EditStats(QLabel):
    """`1:02:30 → 17:50 · 50 cuts · 71% removed` (details in the tooltip)."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("EditStats")
        self.setTextFormat(Qt.RichText)
        self._pal: Palette = palette(False)
        self._last: dict | None = None
        self.set_empty()

    def apply_palette(self, pal: Palette) -> None:
        self._pal = pal
        if self._last is None:
            self.set_empty()
        else:
            self.set_stats(**self._last)

    def set_empty(self) -> None:
        self._last = None
        self.setText(f"<span style='color:{self._pal.text_3}'>No edit yet</span>")
        self.setToolTip("")

    def set_stats(self, *, duration: str, output: str, cuts: int, removed_pct: float, saved: str = "",
                  avg_cut: str = "", avg_keep: str = "", cuts_per_min: float = 0.0, keeps: int = 0,
                  analyzed: bool = True) -> None:
        self._last = dict(duration=duration, output=output, cuts=cuts, removed_pct=removed_pct, saved=saved,
                          avg_cut=avg_cut, avg_keep=avg_keep, cuts_per_min=cuts_per_min, keeps=keeps,
                          analyzed=analyzed)
        pal = self._pal
        sep = f"<span style='color:{pal.text_3}'>&nbsp;&nbsp;·&nbsp;&nbsp;</span>"
        if not analyzed:
            self.setText(f"<span style='color:{pal.text_2}'>{duration}</span>{sep}"
                         f"<span style='color:{pal.text_3}'>not analyzed</span>")
        else:
            arrow = f"<span style='color:{pal.text_3}'>&nbsp;→&nbsp;</span>"
            self.setText(
                f"<span style='color:{pal.text_2}'>{duration}</span>{arrow}"
                f"<span style='color:{pal.text}; font-weight:600'>{output}</span>{sep}"
                f"<span style='color:{pal.text_2}'>{int(cuts)} cuts</span>{sep}"
                f"<span style='color:{pal.text_2}'>{removed_pct:.0f}% removed</span>"
            )
        tip = [f"Source duration: {duration}", f"Output duration: {output}"]
        if saved:
            tip.append(f"Removed: {saved} ({removed_pct:.0f}%)")
        tip.append(f"Cuts: {int(cuts)}  ·  Kept segments: {int(keeps)}")
        if avg_cut:
            tip.append(f"Average cut: {avg_cut}  ·  Average keep: {avg_keep}")
        if cuts_per_min:
            tip.append(f"Cuts per minute: {cuts_per_min:.1f}")
        self.setToolTip("\n".join(tip))
