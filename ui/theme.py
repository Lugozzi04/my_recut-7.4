"""Application stylesheet built from the design tokens in ui/design_tokens.py.

Public API (unchanged): `apply_theme(window, name)` and `apply_theme_from_system(window)`; both set
`window._c_*` colours used by older code paths and `window._pal` (a Palette) used by the native
editor widgets.
"""
from __future__ import annotations

import dataclasses
import tempfile
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication, QPalette
from PySide6.QtWidgets import QComboBox, QFrame

from ui.design_tokens import DARK, FONT_UI, LIGHT, Palette

# Curated alternative dark palettes (kept for users who picked them in older versions)
THEMES = {
    "Night Owl": dict(app="#0B1220", panel="#111827", raised="#172033", border="#25314A",
                      text="#E6ECF5", text_2="#B5C0D0", text_3="#8FA0B8", accent="#4E9EFF"),
    "Dracula": dict(app="#1E1F28", panel="#23252F", raised="#2B2E3A", border="#383D4A",
                    text="#F8F8F2", text_2="#C5C7D5", text_3="#A1A6B4", accent="#BD93F9"),
    "Nord": dict(app="#2B313C", panel="#2E3440", raised="#3A4252", border="#434C5E",
                 text="#ECEFF4", text_2="#C7CFDA", text_3="#A5AFBF", accent="#88C0D0"),
    "Solarized Dark": dict(app="#002B36", panel="#073642", raised="#0B3E4B", border="#154653",
                           text="#EEE8D5", text_2="#C6C1AF", text_3="#93A1A1", accent="#2AA198"),
    "Monokai": dict(app="#1E1F1C", panel="#272822", raised="#2F302A", border="#3A3A33",
                    text="#F8F8F2", text_2="#C9C9C2", text_3="#A7A79E", accent="#A6E22E"),
}


def apply_theme_from_system(window) -> None:
    """Detect OS light/dark and apply stylesheet + icon tinting."""
    pal = QGuiApplication.palette()
    light = pal.color(QPalette.Window).lightness() > 128
    window._theme_light = light
    apply_palette(window, LIGHT if light else DARK)


def apply_theme(window, theme_name: str) -> None:
    """Apply a named theme or fall back to system."""
    key = str(theme_name or "").strip()
    if not key or key == "System":
        apply_theme_from_system(window)
        return
    if key == "Light":
        window._theme_light = True
        apply_palette(window, LIGHT)
        return
    if key == "Dark":
        window._theme_light = False
        apply_palette(window, DARK)
        return
    theme = THEMES.get(key)
    if not theme:
        apply_theme_from_system(window)
        return
    window._theme_light = False
    accent = QColor(theme["accent"])
    pal = dataclasses.replace(
        DARK, **theme,
        hover=QColor(theme["raised"]).lighter(118).name(),
        pressed=QColor(theme["raised"]).lighter(130).name(),
        border_strong=QColor(theme["border"]).lighter(125).name(),
        accent_hover=accent.lighter(112).name(),
        accent_pressed=accent.darker(112).name(),
        focus=accent.lighter(125).name(),
    )
    apply_palette(window, pal)


def apply_styles(window, *, light_theme: bool) -> None:
    """Backward-compatible entry point."""
    window._theme_light = bool(light_theme)
    apply_palette(window, LIGHT if light_theme else DARK)


def _glyph_png(name: str, color: str, size: int = 12) -> str:
    """Render a glyph to a cached PNG so QSS sub-controls (combo arrow, check mark) can use it."""
    from ui.widgets.glyphs import glyph_pixmap

    cache = Path(tempfile.gettempdir()) / "recut_ui_glyphs"
    cache.mkdir(parents=True, exist_ok=True)
    out = cache / f"{name}_{color.strip('#')}_{size}.png"
    if not out.exists():
        pm = glyph_pixmap(name, size, QColor(color))
        pm.save(str(out), "PNG")
    return str(out).replace("\\", "/")


def build_stylesheet(p: Palette) -> str:
    chev = _glyph_png("chevron_down", p.text_2, 12)
    check = _glyph_png("check", "#FFFFFF", 12)
    return f"""
    * {{ font-family: {FONT_UI}; font-size: 12px; }}
    QMainWindow, QWidget#Central {{ background: {p.app}; color: {p.text}; }}
    QWidget {{ color: {p.text}; }}
    QToolTip {{
        background: {p.raised}; color: {p.text}; border: 1px solid {p.border_strong};
        border-radius: 6px; padding: 6px 8px;
    }}

    /* ---------- top bar ---------- */
    QFrame#TopBar {{ background: {p.panel}; border-bottom: 1px solid {p.border}; }}
    QLabel#AppName {{ font-size: 13px; font-weight: 700; letter-spacing: 0.2px; }}
    QLabel#TopBarSep {{ color: {p.text_3}; font-size: 14px; }}
    QLabel#ProjectName {{ color: {p.text}; font-size: 13px; }}
    QLabel#DirtyDot {{ color: {p.warning}; font-size: 9px; }}

    /* ---------- buttons ---------- */
    QPushButton {{
        background: {p.raised}; color: {p.text}; border: 1px solid {p.border_strong};
        border-radius: 6px; padding: 0 12px; min-height: 28px;
    }}
    QPushButton:hover {{ background: {p.hover}; }}
    QPushButton:pressed {{ background: {p.pressed}; }}
    QPushButton:disabled {{ color: {p.text_3}; background: {p.panel}; border-color: {p.border}; }}
    QPushButton:focus {{ border-color: {p.focus}; }}
    QPushButton#TopButton {{ background: transparent; border-color: {p.border_strong}; padding: 0 12px 0 10px; }}
    QPushButton#TopButton:hover {{ background: {p.hover}; }}
    QPushButton#PrimaryButton, QPushButton#PrimaryCTA {{
        background: {p.accent}; color: #FFFFFF; border: 1px solid {p.accent}; font-weight: 600;
        padding: 0 14px 0 12px;
    }}
    QPushButton#PrimaryButton:hover, QPushButton#PrimaryCTA:hover {{
        background: {p.accent_hover}; border-color: {p.accent_hover};
    }}
    QPushButton#PrimaryButton:pressed, QPushButton#PrimaryCTA:pressed {{ background: {p.accent_pressed}; }}
    QPushButton#PrimaryButton:disabled, QPushButton#PrimaryCTA:disabled {{
        background: {p.raised}; color: {p.text_3}; border-color: {p.border};
    }}
    QPushButton#PrimaryCTA {{ min-height: 38px; font-size: 13px; border-radius: 7px; }}
    QPushButton#LinkButton {{
        background: transparent; border: none; color: {p.accent}; padding: 0 2px; min-height: 22px;
        text-align: left;
    }}
    QPushButton#LinkButton:hover {{ color: {p.accent_hover}; text-decoration: underline; }}
    QPushButton#LinkButton:disabled {{ color: {p.text_3}; background: transparent; }}
    QPushButton#SecondarySmall {{ min-height: 26px; padding: 0 10px; font-size: 12px; }}

    QToolButton {{ background: transparent; border: none; border-radius: 6px; color: {p.text}; }}
    QToolButton:hover {{ background: {p.hover}; }}
    QToolButton:pressed {{ background: {p.pressed}; }}
    QToolButton::menu-indicator {{ image: none; width: 0; }}
    QToolButton#TopIconButton {{ border: 1px solid {p.border_strong}; }}
    QToolButton#PlayButton {{ background: {p.accent}; border-radius: 20px; }}
    QToolButton#PlayButton:hover {{ background: {p.accent_hover}; }}
    QToolButton#PlayButton:pressed {{ background: {p.accent_pressed}; }}
    QToolButton#PlayButton:disabled {{ background: {p.raised}; }}
    QToolButton#ToolButton {{ border-radius: 5px; }}
    QToolButton#ToolButton:checked {{ background: {p.pressed}; }}
    QToolButton#ToolbarButton:checked {{ background: {p.accent_soft}; }}
    QToolButton#PillToggle {{
        border: 1px solid {p.border_strong}; border-radius: 14px; padding: 0 10px 0 8px; color: {p.text_2};
    }}
    QToolButton#PillToggle:checked {{ background: {p.accent_soft}; border-color: {p.accent}; color: {p.text}; }}
    QToolButton#SectionChevron {{ border-radius: 4px; }}
    QToolButton#IconButton {{ border: 1px solid {p.border_strong}; border-radius: 6px; }}

    /* ---------- viewer / transport / timeline chrome ---------- */
    QFrame#ViewerPane {{ background: {p.viewer}; border: none; }}
    QFrame#Transport {{ background: {p.panel}; border-top: 1px solid {p.border}; }}
    QLabel#TimecodeCurrent {{ font-size: 15px; font-weight: 600; font-family: 'Cascadia Mono', 'Consolas', monospace; }}
    QLabel#TimecodeTotal {{ font-size: 13px; color: {p.text_3}; font-family: 'Cascadia Mono', 'Consolas', monospace; }}
    QLabel#VolumeValue {{ color: {p.text_2}; }}
    QFrame#TimelinePane {{ background: {p.panel}; border-top: 1px solid {p.border}; }}
    QFrame#TimelineToolbar {{ background: {p.panel}; border-bottom: 1px solid {p.border}; }}
    QFrame#ToolGroup {{ background: {p.app}; border: 1px solid {p.border}; border-radius: 7px; }}
    QFrame#ToolbarDivider {{ background: {p.border_strong}; }}
    QLabel#LegendText {{ color: {p.text_2}; font-size: 11px; }}
    QLabel#LegendCount {{ color: {p.text}; font-weight: 600; }}

    QScrollArea#TimelineScroll, QScrollArea#TimelineScroll > QWidget {{ background: transparent; border: none; }}
    QSplitter::handle {{ background: {p.border}; }}
    QSplitter::handle:hover {{ background: {p.accent}; }}
    QSplitter#MainSplitter::handle:horizontal {{ width: 1px; }}
    QSplitter#WorkSplitter::handle:vertical {{ height: 1px; }}

    /* ---------- inspector ---------- */
    QFrame#Inspector {{ background: {p.panel}; }}
    QFrame#InspectorHeader {{ background: {p.panel}; border-bottom: 1px solid {p.border}; }}
    QFrame#InspectorFooter {{ background: {p.panel}; border-top: 1px solid {p.border}; }}
    QFrame#InspectorSection {{ background: transparent; border: none; border-bottom: 1px solid {p.border}; }}
    QLabel#SectionTitle {{ font-size: 12px; font-weight: 600; color: {p.text}; }}
    QLabel#SectionMeta {{ color: {p.text_3}; font-size: 11px; }}
    QLabel#RowLabel {{ color: {p.text_2}; }}
    QLabel#RowLabelStrong {{ color: {p.text}; font-weight: 600; }}
    QLabel#RowHint {{ color: {p.text_3}; font-size: 11px; }}
    QLabel#ValueLabel {{ color: {p.text}; font-weight: 600; }}
    QLabel#Muted {{ color: {p.text_2}; }}
    QLabel#StatValue {{ font-size: 15px; font-weight: 600; }}
    QLabel#StatCaption {{ color: {p.text_3}; font-size: 11px; }}
    QLabel#StatusLine {{ color: {p.text_2}; }}
    QLabel#ExportStatus {{ color: {p.text_2}; }}
    QLabel#Badge {{
        border-radius: 9px; padding: 1px 8px; font-size: 11px; font-weight: 600;
        background: {p.raised}; color: {p.text_2}; border: 1px solid {p.border_strong};
    }}
    QLabel#Badge[kind="success"] {{ color: {p.success}; border-color: {p.success}; }}
    QLabel#Badge[kind="warning"] {{ color: {p.warning}; border-color: {p.warning}; }}
    QLabel#Badge[kind="info"] {{ color: {p.accent}; border-color: {p.accent}; }}
    QFrame#Segmented {{ background: {p.app}; border: 1px solid {p.border}; border-radius: 8px; }}
    QToolButton#SegmentedButton, QPushButton#SegmentedButton {{
        background: transparent; color: {p.text_2}; border: none; border-radius: 6px;
        min-height: 26px; padding: 0 10px; font-weight: 600;
    }}
    QToolButton#SegmentedButton:hover, QPushButton#SegmentedButton:hover {{ color: {p.text}; background: transparent; }}
    QToolButton#SegmentedButton:checked, QPushButton#SegmentedButton:checked {{
        background: {p.hover}; color: {p.text};
    }}
    QToolButton#SegmentedButton:disabled {{ color: {p.text_3}; }}
    QFrame#SelectionCard {{ background: {p.raised}; border: 1px solid {p.border}; border-radius: 8px; }}
    QFrame#SummaryCard {{ background: {p.raised}; border: 1px solid {p.border}; border-radius: 8px; }}
    QScrollArea#InspectorScroll {{ background: transparent; border: none; }}
    QScrollArea#InspectorScroll > QWidget > QWidget {{ background: transparent; }}

    /* ---------- inputs ---------- */
    QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit {{
        background: {p.raised}; color: {p.text}; border: 1px solid {p.border_strong};
        border-radius: 6px; min-height: 28px; padding: 0 8px;
        selection-background-color: {p.accent};
    }}
    QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover, QLineEdit:hover {{ border-color: {p.text_3}; }}
    QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QLineEdit:focus {{ border-color: {p.focus}; }}
    QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {{ color: {p.text_3}; border-color: {p.border}; }}
    QComboBox {{ padding-right: 26px; }}
    QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right; width: 24px; border: none; }}
    QComboBox::down-arrow {{ image: url("{chev}"); width: 12px; height: 12px; }}
    QComboBox QAbstractItemView {{
        background: {p.raised}; color: {p.text}; border: 1px solid {p.border_strong}; border-radius: 8px;
        padding: 4px; outline: 0; selection-background-color: {p.accent}; selection-color: #FFFFFF;
    }}
    QComboBox QAbstractItemView::item {{ min-height: 26px; padding: 4px 8px; border-radius: 4px; }}
    QComboBox QAbstractItemView::item:hover {{ background: {p.hover}; }}
    QComboBox QAbstractItemView::item:selected {{ background: {p.accent}; color: #FFFFFF; }}
    QSpinBox::up-button, QDoubleSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::down-button {{
        width: 0; border: none;
    }}
    QSpinBox, QDoubleSpinBox {{ padding-right: 8px; }}

    QCheckBox {{ spacing: 8px; color: {p.text}; }}
    QCheckBox:disabled {{ color: {p.text_3}; }}
    QCheckBox::indicator {{
        width: 16px; height: 16px; border-radius: 4px; border: 1px solid {p.border_strong}; background: {p.raised};
    }}
    QCheckBox::indicator:hover {{ border-color: {p.text_3}; }}
    QCheckBox::indicator:checked {{ background: {p.accent}; border-color: {p.accent}; image: url("{check}"); }}

    QSlider {{ min-height: 20px; }}
    QSlider::groove:horizontal {{ height: 4px; border-radius: 2px; background: {p.border_strong}; }}
    QSlider::sub-page:horizontal {{ height: 4px; border-radius: 2px; background: {p.accent}; }}
    QSlider::handle:horizontal {{
        width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; background: #FFFFFF;
        border: 1px solid {p.border_strong};
    }}
    QSlider::handle:horizontal:hover {{ background: {p.text}; border-color: {p.accent}; }}
    QSlider::sub-page:horizontal:disabled {{ background: {p.text_3}; }}
    QSlider#ZoomSlider::groove:horizontal {{ height: 3px; }}
    QSlider#ZoomSlider::sub-page:horizontal {{ background: {p.text_3}; }}
    QSlider#ZoomSlider::handle:horizontal {{ width: 10px; height: 10px; margin: -4px 0; border-radius: 5px; }}
    QSlider#ScrubSlider::groove:horizontal {{ height: 3px; background: {p.border_strong}; border-radius: 1px; }}
    QSlider#ScrubSlider::sub-page:horizontal {{ background: {p.accent}; border-radius: 1px; }}
    QSlider#ScrubSlider::handle:horizontal {{ width: 10px; height: 10px; margin: -4px 0; border-radius: 5px; background: #FFFFFF; border: none; }}

    QProgressBar {{
        background: {p.raised}; border: none; border-radius: 3px; height: 6px; color: transparent; text-align: center;
    }}
    QProgressBar::chunk {{ background: {p.accent}; border-radius: 3px; }}
    QPlainTextEdit {{
        background: {p.app}; color: {p.text_2}; border: 1px solid {p.border}; border-radius: 6px;
        font-family: 'Cascadia Mono', 'Consolas', monospace; font-size: 11px;
    }}

    /* ---------- menus / scrollbars / status bar ---------- */
    QMenu {{
        background: {p.raised}; color: {p.text}; border: 1px solid {p.border_strong}; border-radius: 8px; padding: 5px;
    }}
    QMenu::item {{ padding: 6px 24px 6px 12px; border-radius: 5px; }}
    QMenu::item:selected {{ background: {p.accent}; color: #FFFFFF; }}
    QMenu::item:disabled {{ color: {p.text_3}; background: transparent; }}
    QMenu::separator {{ height: 1px; background: {p.border}; margin: 5px 6px; }}
    QMenu::section {{ color: {p.text_3}; padding: 6px 12px 2px 12px; font-size: 11px; font-weight: 600; }}
    QMenu::indicator {{ width: 14px; height: 14px; left: 6px; }}
    QMenu#VolumeMenu {{ padding: 2px; }}

    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: {p.border_strong}; border-radius: 3px; min-height: 30px; }}
    QScrollBar::handle:vertical:hover {{ background: {p.text_3}; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
    QScrollBar::handle:horizontal {{ background: {p.border_strong}; border-radius: 3px; min-width: 30px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

    QStatusBar {{ background: {p.panel}; border-top: 1px solid {p.border}; color: {p.text_2}; min-height: 24px; }}
    QStatusBar::item {{ border: none; }}
    QStatusBar QLabel {{ color: {p.text_2}; padding: 0 8px; }}

    QDialog, QMessageBox {{ background: {p.panel}; }}
    """


def apply_palette(window, pal: Palette) -> None:
    window._pal = pal
    window._c_bg = QColor(pal.app)
    window._c_surface = QColor(pal.panel)
    window._c_surface2 = QColor(pal.raised)
    window._c_border = QColor(pal.border)
    window._c_text = QColor(pal.text)
    window._c_muted = QColor(pal.text_2)
    window._c_subtle = QColor(pal.text_3)
    window._c_accent = QColor(pal.accent)

    window.setStyleSheet(build_stylesheet(pal))

    # Combo popups are separate top-level windows; give them the same surface.
    for combo in window.findChildren(QComboBox):
        try:
            view = combo.view()
            if view is None:
                continue
            view.setFrameShape(QFrame.NoFrame)
            qpal = view.palette()
            qpal.setColor(QPalette.Base, QColor(pal.raised))
            qpal.setColor(QPalette.Text, QColor(pal.text))
            qpal.setColor(QPalette.Highlight, QColor(pal.accent))
            qpal.setColor(QPalette.HighlightedText, QColor("#FFFFFF"))
            view.setPalette(qpal)
            popup = view.window()
            if popup is not None and popup is not window:
                popup.setAttribute(Qt.WA_TranslucentBackground, False)
                ppal = popup.palette()
                ppal.setColor(QPalette.Window, QColor(pal.raised))
                popup.setPalette(ppal)
        except Exception:
            pass

    hook = getattr(window, "_apply_native_palette", None)
    if callable(hook):
        try:
            hook(pal)
        except Exception:
            pass
    sync = getattr(window, "_sync_icons", None)
    if callable(sync):
        try:
            sync()
        except Exception:
            pass
