from __future__ import annotations

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QColor, QGuiApplication, QPalette
from PySide6.QtWidgets import QComboBox, QFrame
from utils.runtime_paths import resource_path

# Curated dark themes (UI palettes)
THEMES = {
    "Night Owl": dict(
        bg="#0B1220",
        surface="#111827",
        surface2="#172033",
        border="#25314A",
        text="#E6ECF5",
        muted="#B5C0D0",
        subtle="#8FA0B8",
        accent="#4E9EFF",
    ),
    "Dracula": dict(
        bg="#1E1F28",
        surface="#23252F",
        surface2="#2B2E3A",
        border="#383D4A",
        text="#F8F8F2",
        muted="#C5C7D5",
        subtle="#A1A6B4",
        accent="#BD93F9",
    ),
    "Nord": dict(
        bg="#2B313C",
        surface="#2E3440",
        surface2="#3A4252",
        border="#434C5E",
        text="#ECEFF4",
        muted="#C7CFDA",
        subtle="#A5AFBF",
        accent="#88C0D0",
    ),
    "Solarized Dark": dict(
        bg="#002B36",
        surface="#073642",
        surface2="#0B3E4B",
        border="#154653",
        text="#EEE8D5",
        muted="#C6C1AF",
        subtle="#93A1A1",
        accent="#2AA198",
    ),
    "Monokai": dict(
        bg="#1E1F1C",
        surface="#272822",
        surface2="#2F302A",
        border="#3A3A33",
        text="#F8F8F2",
        muted="#C9C9C2",
        subtle="#A7A79E",
        accent="#A6E22E",
    ),
}


def apply_theme_from_system(window) -> None:
    """Detect OS light/dark and apply stylesheet + icon tinting."""
    pal = QGuiApplication.palette()
    win = pal.color(QPalette.Window)
    light = win.lightness() > 128
    window._theme_light = light
    apply_styles(window, light_theme=light)


def apply_theme(window, theme_name: str) -> None:
    """Apply a named theme or fall back to system."""
    key = str(theme_name or "").strip()
    if not key or key == "System":
        apply_theme_from_system(window)
        return
    if key == "Light":
        window._theme_light = True
        apply_styles(window, light_theme=True)
        return
    if key == "Dark":
        window._theme_light = False
        apply_styles(window, light_theme=False)
        return

    theme = THEMES.get(key)
    if not theme:
        apply_theme_from_system(window)
        return

    window._theme_light = False
    window._c_bg = QColor(theme["bg"])
    window._c_surface = QColor(theme["surface"])
    window._c_surface2 = QColor(theme["surface2"])
    window._c_border = QColor(theme["border"])
    window._c_text = QColor(theme["text"])
    window._c_muted = QColor(theme["muted"])
    window._c_subtle = QColor(theme["subtle"])
    window._c_accent = QColor(theme["accent"])
    apply_styles(window, light_theme=False)


def apply_styles(window, *, light_theme: bool) -> None:
    """Apply stylesheet tokens and refresh icons (extracted from MainWindow)."""
    if light_theme:
        # Light theme tuned for readability and contrast on different Windows
        # themes/GPUs: less pure white, stronger borders, stronger accent.
        window._c_bg = QColor("#EDEFF3")
        window._c_surface = QColor("#F8FAFD")
        window._c_surface2 = QColor("#EEF2F7")
        window._c_border = QColor("#BCC6D4")
        window._c_text = QColor("#101722")
        window._c_muted = QColor("#344254")
        window._c_subtle = QColor("#5A6A7F")
        window._c_accent = QColor("#2F6FEB")
    else:
        window._c_bg = QColor("#111318")
        window._c_surface = QColor("#171B22")
        window._c_surface2 = QColor("#1C212A")
        window._c_border = QColor("#313846")
        window._c_text = QColor("#EDF2F8")
        window._c_muted = QColor("#C1CAD7")
        window._c_subtle = QColor("#96A4B6")
        window._c_accent = QColor("#4E97FF")

    bg = window._c_bg.name()
    surface = window._c_surface.name()
    surface2 = window._c_surface2.name()
    border = window._c_border.name()
    text = window._c_text.name()
    muted = window._c_muted.name()
    subtle = window._c_subtle.name()
    accent = window._c_accent.name()
    accent_hover = window._c_accent.lighter(112).name()
    accent_pressed = window._c_accent.darker(112).name()
    if light_theme:
        badge_base_bg = "rgba(16, 23, 34, 0.04)"
        badge_muted_bg = "rgba(16, 23, 34, 0.03)"
        badge_info_bg = "rgba(47, 111, 235, 0.12)"
        badge_success_border = "rgba(30, 133, 78, 0.55)"
        badge_success_bg = "rgba(30, 133, 78, 0.12)"
        badge_warning_border = "rgba(189, 122, 17, 0.60)"
        badge_warning_bg = "rgba(189, 122, 17, 0.12)"
        card_inner_border = "rgba(16,23,34,0.08)"
        card_soft_bg = "rgba(16,23,34,0.025)"
        card_soft_border = "rgba(16,23,34,0.07)"
        section_soft_bg = "rgba(16,23,34,0.018)"
        section_soft_border = "rgba(16,23,34,0.06)"
        slider_groove_bg = "rgba(16,23,34,0.14)"
        adv_header_bg = "rgba(16,23,34,0.025)"
        adv_header_border = "rgba(16,23,34,0.08)"
        adv_toggle_hover_bg = "rgba(16,23,34,0.04)"
        adv_toggle_hover_border = "rgba(16,23,34,0.10)"
    else:
        badge_base_bg = "rgba(255,255,255,0.04)"
        badge_muted_bg = "rgba(255,255,255,0.02)"
        badge_info_bg = "rgba(78, 151, 255, 0.14)"
        badge_success_border = "rgba(70, 208, 140, 0.55)"
        badge_success_bg = "rgba(70, 208, 140, 0.14)"
        badge_warning_border = "rgba(255, 185, 77, 0.6)"
        badge_warning_bg = "rgba(255, 185, 77, 0.14)"
        card_inner_border = "rgba(255,255,255,0.03)"
        card_soft_bg = "rgba(255,255,255,0.02)"
        card_soft_border = "rgba(255,255,255,0.04)"
        section_soft_bg = "rgba(255,255,255,0.015)"
        section_soft_border = "rgba(255,255,255,0.035)"
        slider_groove_bg = "rgba(255,255,255,0.08)"
        adv_header_bg = "rgba(255,255,255,0.02)"
        adv_header_border = "rgba(255,255,255,0.04)"
        adv_toggle_hover_bg = "rgba(255,255,255,0.03)"
        adv_toggle_hover_border = "rgba(255,255,255,0.04)"
    icons_dir = resource_path("icons", "ui")
    up_icon = icons_dir / "up.svg"
    down_icon = icons_dir / "down.svg"
    # Qt stylesheets can mis-handle file:// URLs here on Windows and prepend the
    # app working directory, producing paths like ".../file:/C:/...". Use a
    # plain local path with forward slashes instead.
    up_icon_url = str(up_icon).replace("\\", "/")
    down_icon_url = str(down_icon).replace("\\", "/")

    window.setStyleSheet(
        f"""
            QMainWindow {{ background: {bg}; }}
            QWidget#MainContainer {{
                background: {surface};
                border: none;
                border-radius: 0;
            }}
            QWidget#RightPanel {{
                background: {surface2};
                border-left: 1px solid {border};
                border-top-right-radius: 0;
                border-bottom-right-radius: 0;
                font-family: "Segoe UI Variable Text", "Segoe UI", "Bahnschrift", "Calibri", "Arial";
            }}
            QWidget#LeftPanel {{
                background: transparent;
            }}
            QFrame#LeftSurface {{
                background: {surface};
                border: none;
                border-radius: 0;
            }}
            QFrame#SoftSeparator {{
                background: {border};
            }}

            QSplitter::handle {{
                background: {border};
            }}
            QSplitter::handle:horizontal {{
                width: 1px;
                margin: 0px 6px;   /* crea respiro tra i pannelli */
            }}
            QSplitter#PreviewTimelineSplitter::handle:vertical {{
                height: 10px;
                margin: 0px 10px;
                background: transparent;
                border-top: 1px solid rgba(255,255,255,0.10);
                border-bottom: 1px solid rgba(255,255,255,0.03);
                border-radius: 4px;
            }}
            QSplitter#PreviewTimelineSplitter::handle:vertical:hover {{
                background: rgba(94,164,255,0.14);
                border-top: 1px solid rgba(94,164,255,0.35);
                border-bottom: 1px solid rgba(94,164,255,0.16);
            }}
            QSplitter#PreviewTimelineSplitter::handle:vertical:pressed {{
                background: rgba(94,164,255,0.22);
                border-top: 1px solid rgba(94,164,255,0.50);
                border-bottom: 1px solid rgba(94,164,255,0.24);
            }}

            QPushButton#Action {{
                background: {surface};
                border: 1px solid {border};
                color: {text};
                padding: 8px 14px;
                border-radius: 6px;
                font-weight: 600;
            }}
            QPushButton#Action:hover {{
                background: {surface2};
                border-color: {accent};
            }}
            QPushButton#Action:pressed {{
                background: {surface2};
                border-color: {accent_pressed};
            }}

            QScrollArea#RightScroll {{
                background: transparent;
                border: none;
            }}
            QWidget#RightViewport {{
                background: transparent;
            }}
            QFrame#RightPanel {{
                background: {surface2};
            }}
            QScrollArea#MainScroll, QScrollArea#AdvancedScroll, QScrollArea#ExportScroll {{
                background: transparent;
                border: none;
            }}
            QAbstractScrollArea {{
                background: transparent;
                border: none;
            }}
            QAbstractScrollArea::corner {{
                background: transparent;
                border: none;
            }}

            QStackedWidget {{
                background: transparent;
                border: none;
            }}

            QPushButton#Primary {{
                background: {accent};
                border: 1px solid {accent};
                color: white;
                padding: 10px 14px;
                border-radius: 8px;
                font-weight: 700;
            }}
            QPushButton#Primary:hover {{
                background: {accent_hover};
                border-color: {accent_hover};
            }}
            QPushButton#Primary:pressed {{
                background: {accent_pressed};
                border-color: {accent_pressed};
            }}
            QPushButton#Primary:disabled {{
                background: {surface2};
                border-color: {border};
                color: {muted};
            }}

            QFrame#TopBar {{
                background: {surface};
                border: 1px solid {border};
                border-radius: 0;
            }}

            QPushButton#Action:checked {{
                background: {surface2};
                border-color: {border};
                color: {text};
            }}

            QToolButton#IconButton {{
                background: transparent;
                border: 1px solid transparent;
                border-radius: 6px;
                padding: 8px 10px;
            }}
            QToolButton#IconButton:hover {{
                background: {surface2};
                border-color: {border};
            }}

            QLabel {{
                color: {text};
                font-size: 12px;
            }}
            QLabel#SubtleHint {{
                color: {subtle};
                font-size: 12px;
            }}
            QLabel#SectionTitle {{
                color: {text};
                font-size: 13px;
                font-weight: 700;
            }}
            QLabel#InspectorBlockTitle {{
                color: {text};
                font-size: 14px;
                font-weight: 700;
                letter-spacing: 0.2px;
            }}
            QLabel#FieldTitle {{
                color: {muted};
                font-size: 12px;
                font-weight: 700;
            }}
            QLabel#FieldHint, QLabel#PresetMeta, QLabel#StatusNote {{
                color: {subtle};
                font-size: 12px;
                line-height: 1.35;
            }}
            QLabel#ExportStatus {{
                color: {muted};
                font-size: 12px;
                padding: 2px 2px 0 2px;
            }}
            QLabel#Badge, QLabel#AdvancedSectionBadge {{
                color: {text};
                background: {badge_base_bg};
                border: 1px solid {border};
                border-radius: 999px;
                padding: 3px 10px;
                min-height: 20px;
                font-size: 12px;
                font-weight: 700;
            }}
            QLabel#Badge[kind="info"], QLabel#AdvancedSectionBadge[kind="info"] {{
                border-color: {accent};
                background: {badge_info_bg};
                color: {text};
            }}
            QLabel#Badge[kind="success"], QLabel#AdvancedSectionBadge[kind="success"] {{
                border-color: {badge_success_border};
                background: {badge_success_bg};
                color: {text};
            }}
            QLabel#Badge[kind="warning"], QLabel#AdvancedSectionBadge[kind="warning"] {{
                border-color: {badge_warning_border};
                background: {badge_warning_bg};
                color: {text};
            }}
            QLabel#Badge[kind="muted"], QLabel#AdvancedSectionBadge[kind="muted"] {{
                color: {muted};
                background: {badge_muted_bg};
                border-color: {border};
            }}

            QFrame#StepWrap {{
                background: {surface2};
                border: none;
                border-radius: 8px;
            }}

            QFrame#Card {{
                background: {surface2};
                border: 1px solid {border};
                border-radius: 10px;
            }}
            QFrame#CardInner {{
                background: {surface};
                border: 1px solid {card_inner_border};
                border-radius: 8px;
            }}
            QFrame#InspectorBlock {{
                background: {surface};
                border: 1px solid {card_inner_border};
                border-radius: 10px;
            }}
            QFrame#StatMiniCard {{
                background: {card_soft_bg};
                border: 1px solid {card_soft_border};
                border-radius: 8px;
            }}
            QLabel#StatMiniValue {{
                color: {text};
                font-size: 14px;
                font-weight: 700;
            }}
            QFrame#AdvancedSection {{
                background: {section_soft_bg};
                border: 1px solid {section_soft_border};
                border-radius: 8px;
            }}
            QFrame#AdvancedSectionHeader {{
                background: transparent;
                border: none;
            }}
            QLabel#AdvancedSectionTitle {{
                color: {text};
                font-size: 12px;
                font-weight: 700;
            }}

            QFrame#SegmentWrap {{
                background: {surface};
                border-bottom: 1px solid {border};
                border-radius: 0;
            }}

            QToolButton {{
                background: transparent;
                border: 1px solid transparent;
                border-radius: 6px;
                padding: 8px 10px;
                color: {text};
                font-size: 12px;
            }}

            QToolButton:hover {{
                background: {surface2};
                border-color: {border};
            }}
            QToolButton:focus {{
                border-color: {accent};
            }}

            QToolButton#SegmentEdit, QToolButton#SegmentExport, QToolButton#SegmentLayout {{
                border-radius: 0;
                padding: 6px 16px 8px;
                min-width: 90px;
                border: none;
                border-bottom: 2px solid transparent;
                font-weight: 600;
                font-family: "Segoe UI Variable Display", "Segoe UI", "Bahnschrift", "Calibri", "Arial";
                letter-spacing: 0.2px;
                color: {muted};
                background: transparent;
            }}
            QToolButton#SegmentLayout {{
                min-width: 80px;
            }}
            QToolButton#SegmentEdit:hover {{
                color: {accent};
                border-bottom-color: {accent};
                background: {surface2};
            }}
            QToolButton#SegmentExport:hover {{
                color: #ff6b6b;
                border-bottom-color: #ff6b6b;
                background: {surface2};
            }}
            QToolButton#SegmentLayout:hover {{
                color: {text};
                border-bottom-color: {accent};
                background: {surface2};
            }}
            QToolButton#SegmentEdit:checked {{
                color: {text};
                border-bottom-color: {accent};
                background: {surface2};
            }}
            QToolButton#SegmentExport:checked {{
                color: {text};
                border-bottom-color: #ff4d4d;
                background: {surface2};
            }}

            QFrame#TransportGroup {{
                background: {surface};
                border: 1px solid {border};
                border-radius: 10px;
            }}
            QFrame#VSeparator {{
                background: {border};
            }}

            QPushButton {{
                background: {surface};
                border: 1px solid {border};
                border-radius: 8px;
                padding: 10px 12px;
                color: {text};
                font-weight: 600;
            }}
            QPushButton:hover {{
                background: {surface2};
                border-color: {accent};
            }}
            QPushButton:disabled {{
                color: {subtle};
            }}
            QPushButton:focus {{
                border-color: {accent};
            }}
            QPushButton#SmallSecondary {{
                padding: 6px 10px;
                min-height: 28px;
                border-radius: 7px;
                font-size: 12px;
                font-weight: 600;
                color: {muted};
            }}
            QPushButton#SmallSecondary:hover {{
                color: {text};
            }}

            QPlainTextEdit {{
                background: {surface};
                border: 1px solid {border};
                border-radius: 8px;
                color: {text};
                selection-background-color: {accent};
                selection-color: white;
            }}
            QPlainTextEdit:focus {{
                border-color: {accent};
            }}

            QCheckBox {{
                color: {text};
                spacing: 8px;
            }}

            QSlider::groove:horizontal {{
                height: 6px;
                background: {slider_groove_bg};
                border-radius: 3px;
            }}
            QSlider::handle:horizontal {{
                width: 16px;
                margin: -6px 0;
                border: 2px solid {surface};
                border-radius: 8px;
                background: {accent};
            }}
            QSlider::handle:horizontal:hover {{
                background: {accent_hover};
            }}

            QComboBox, QSpinBox, QDoubleSpinBox {{
                background: {surface2};
                border: 1px solid {border};
                border-radius: 8px;
                padding: 8px 10px;
                color: {text};
            }}
            QComboBox {{
                padding-right: 26px;
            }}
            QComboBox::drop-down {{
                subcontrol-origin: padding;
                subcontrol-position: top right;
                width: 22px;
                border: none;
            }}
            QComboBox::down-arrow {{
                image: url("{down_icon_url}");
                width: 10px;
                height: 10px;
            }}
            QComboBox::down-arrow:on {{
                image: url("{up_icon_url}");
            }}
            QComboBox QAbstractItemView {{
                background: {surface2};
                color: {text};
                border: 1px solid {border};
                border-radius: 8px;
                padding: 4px;
                outline: 0;
                selection-background-color: {accent};
                selection-color: white;
            }}
            QComboBox QAbstractItemView::item {{
                min-height: 24px;
                padding: 5px 8px;
                background: transparent;
            }}
            QComboBox QAbstractItemView::item:selected {{
                background: {accent};
                color: white;
            }}
            QComboBox QAbstractItemView::item:disabled {{
                color: {subtle};
                background: {surface2};
            }}
            QComboBox QAbstractItemView::item:hover {{
                background: {surface2};
            }}
            QFrame#qt_combo_box_popup {{
                background: {surface2};
                border: 1px solid {border};
                border-radius: 8px;
                padding: 2px;
                margin: 0px;
            }}
            QWidget#qt_combo_box_popup {{
                background: {surface2};
                border: 1px solid {border};
                border-radius: 8px;
                padding: 2px;
                margin: 0px;
            }}
            QListView {{
                background: {surface2};
                color: {text};
                border: 1px solid {border};
                outline: 0;
            }}
            QListView::item:selected {{
                background: {accent};
                color: white;
            }}
            QSpinBox, QDoubleSpinBox {{
                min-height: 30px;
                max-width: 160px;
            }}
            QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
                border-color: {accent};
                background: {surface};
            }}
            QSpinBox::up-button, QDoubleSpinBox::up-button,
            QSpinBox::down-button, QDoubleSpinBox::down-button {{
                width: 0px;
                border: none;
            }}

            QProgressBar {{
                border: 1px solid {border};
                border-radius: 8px;
                text-align: center;
                background: {surface2};
                color: {text};
                padding: 2px;
                min-height: 20px;
            }}
            QProgressBar::chunk {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {accent}, stop:1 {accent_hover});
                border-radius: 7px;
            }}

            QMenu {{
                background: {surface};
                color: {text};
                border: 1px solid {border};
                padding: 6px;
            }}
            QMenu::item {{
                padding: 7px 10px;
                border-radius: 6px;
            }}
            QMenu::item:selected {{
                background: {surface2};
            }}
            QMenu::separator {{
                height: 1px;
                margin: 5px 6px;
                background: {border};
            }}

            QDialogButtonBox {{
                background: transparent;
            }}

            QMessageBox {{
                background: {surface2};
                border: 1px solid {border};
                border-radius: 12px;
            }}
            QMessageBox QWidget {{
                background: transparent;
            }}
            QMessageBox QLabel {{
                color: {text};
            }}
            QMessageBox QLabel#qt_msgbox_label {{
                min-width: 420px;
                padding: 4px 2px 0px 2px;
                font-size: 13px;
                font-weight: 600;
            }}
            QMessageBox QLabel#qt_msgboxex_icon_label {{
                min-width: 30px;
                max-width: 34px;
                padding: 2px 8px 0px 10px;
            }}
            QMessageBox QDialogButtonBox {{
                border-top: 1px solid {border};
                margin-top: 10px;
                padding-top: 10px;
            }}
            QMessageBox QPushButton {{
                min-width: 122px;
                min-height: 36px;
                padding: 8px 14px;
                border-radius: 9px;
                font-size: 12px;
                font-weight: 700;
                background: {surface};
                border: 1px solid {border};
                color: {text};
            }}
            QMessageBox QPushButton:hover {{
                background: {surface2};
                border-color: {accent};
            }}
            QMessageBox QPushButton:pressed {{
                background: {surface};
                border-color: {accent_pressed};
            }}
            QMessageBox QPushButton:default {{
                border-color: {accent};
                background: {surface};
            }}
            QMessageBox QPushButton:focus {{
                border-color: {accent};
            }}

            QInputDialog {{
                background: {surface2};
                border: 1px solid {border};
                border-radius: 12px;
            }}
            QInputDialog QLabel {{
                color: {text};
                min-width: 280px;
                font-size: 13px;
                font-weight: 600;
                padding-bottom: 2px;
            }}
            QInputDialog QLineEdit {{
                background: {surface};
                border: 1px solid {border};
                border-radius: 8px;
                padding: 8px 10px;
                color: {text};
                selection-background-color: {accent};
                selection-color: white;
            }}
            QInputDialog QLineEdit:focus {{
                border-color: {accent};
            }}
            QInputDialog QPushButton {{
                min-width: 120px;
                min-height: 34px;
                padding: 8px 14px;
                border-radius: 8px;
                font-size: 12px;
                font-weight: 700;
                background: {surface};
                border: 1px solid {border};
                color: {text};
            }}
            QInputDialog QPushButton:hover {{
                border-color: {accent};
                background: {surface2};
            }}

            QScrollBar:vertical {{
                width: 10px;
                background: {surface2};
                margin: 0px;
            }}
            QScrollBar::handle:vertical {{
                min-height: 28px;
                border-radius: 5px;
                background: {border};
            }}
            QScrollBar::handle:vertical:hover {{
                background: {accent};
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0px;
            }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
                background: transparent;
                border: none;
            }}
            QScrollBar:horizontal {{
                height: 10px;
                background: transparent;
                margin: 0px 2px 0px 2px;
            }}
            QScrollBar::handle:horizontal {{
                min-width: 28px;
                border-radius: 5px;
                background: {border};
            }}
            QScrollBar::handle:horizontal:hover {{
                background: {accent};
            }}
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
                width: 0px;
            }}

            QFrame#AdvHeader {{
                background: {adv_header_bg};
                border: 1px solid {adv_header_border};
                border-radius: 8px;
                padding: 2px;
            }}
            
            QFrame#StatsPanel {{
                background: {surface2};
                border: none;
                border-radius: 8px;
            }}
            
            QLabel#StatsLabel {{
                color: {text};
                font-size: 12px;
                padding: 2px 0;
            }}
            
            QToolButton#AdvToggle {{
                background: {surface};
                border: 1px solid {border};
                border-radius: 6px;
                padding: 0px;
                min-width: 34px;
                min-height: 34px;
                icon-size: 16px;
            }}
            QToolButton#AdvToggle:hover {{
                background: {surface2};
                border-color: {accent};
            }}
            QToolButton#AdvSectionToggle {{
                background: transparent;
                border: 1px solid transparent;
                border-radius: 6px;
                padding: 2px;
                min-width: 24px;
                min-height: 24px;
            }}
            QToolButton#AdvSectionToggle:hover {{
                background: {adv_toggle_hover_bg};
                border-color: {adv_toggle_hover_border};
            }}

            QToolButton#ResetTiny {{
                background: {surface};
                border: 1px solid {border};
                border-radius: 4px;
                padding: 0px;
                min-width: 18px;
                min-height: 18px;
            }}
            QToolButton#ResetTiny:hover {{
                background: {surface2};
                border-color: {accent};
            }}
        """
    )

    # Subtle labels
    window.lbl_file.setStyleSheet(f"color: {muted};")
    window.lbl_footer.setStyleSheet(f"color: {muted};")
    window.lbl_time.setStyleSheet(f"color: {muted};")
    window.lbl_precision.setStyleSheet(f"color: {muted};")

    # Segmented buttons style
    window.seg_main.setObjectName("SegmentEdit")
    window.seg_export.setObjectName("SegmentExport")

    # Apply icons (tinted)
    window._sync_icons()

    # Ensure WebEngine views don't show white backgrounds
    for name in ("web_topbar", "web_transport", "web_stats", "web_stats_full", "web_inspector"):
        view = getattr(window, name, None)
        if view is None:
            continue
        try:
            view.page().setBackgroundColor(window._c_surface)
        except Exception:
            pass

    # Force combo popup palette to avoid bright native dropdowns on some Windows setups.
    try:
        popup_qss = (
            f"QListView {{ background: {surface2}; color: {text}; border: 1px solid {border}; "
            f"selection-background-color: {accent}; selection-color: white; }}"
            f"QListView::item:disabled {{ color: {subtle}; background: {surface2}; }}"
        )
        popup_container_qss = (
            f"QWidget#qt_combo_box_popup, QFrame#qt_combo_box_popup {{ "
            f"background: {surface2}; border: 1px solid {border}; border-radius: 8px; padding: 2px; margin: 0px; }}"
        )
        for combo in window.findChildren(QComboBox):
            try:
                view = combo.view()
                if view is None:
                    continue
                view.setStyleSheet(popup_qss)
                try:
                    view.setFrameShape(QFrame.NoFrame)
                except Exception:
                    pass
                pal = view.palette()
                pal.setColor(QPalette.Base, QColor(surface2))
                pal.setColor(QPalette.Text, QColor(text))
                pal.setColor(QPalette.Highlight, QColor(accent))
                pal.setColor(QPalette.HighlightedText, QColor("white"))
                view.setPalette(pal)
                view.setAttribute(Qt.WA_StyledBackground, True)
                view.viewport().setAutoFillBackground(False)
                view.viewport().setStyleSheet(f"background: {surface2};")
                popup = view.window()
                if popup is not None:
                    popup.setObjectName("qt_combo_box_popup")
                    popup.setStyleSheet(popup_container_qss)
                    popup.setAttribute(Qt.WA_StyledBackground, True)
                    popup.setContentsMargins(0, 0, 0, 0)
                    try:
                        lay = popup.layout()
                        if lay is not None:
                            lay.setContentsMargins(0, 0, 0, 0)
                            lay.setSpacing(0)
                    except Exception:
                        pass
                    ppal = popup.palette()
                    ppal.setColor(QPalette.Window, QColor(surface2))
                    ppal.setColor(QPalette.Base, QColor(surface2))
                    popup.setPalette(ppal)
            except Exception:
                pass
    except Exception:
        pass

    # Keep timeline scroll viewport transparent to avoid white corner artifacts.
    try:
        timeline_scroll = getattr(window, "timeline_scroll", None)
        if timeline_scroll is not None:
            timeline_scroll.setStyleSheet("background: transparent; border: none;")
            timeline_scroll.viewport().setAutoFillBackground(False)
            timeline_scroll.viewport().setStyleSheet("background: transparent;")
    except Exception:
        pass
