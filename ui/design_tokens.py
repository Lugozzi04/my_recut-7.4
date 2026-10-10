"""Design tokens shared by the stylesheet (ui/theme.py) and custom-painted widgets.

One place for spacing, radii, control heights, type sizes and colours so every surface of the
editor uses the same scale. Colours are plain hex strings; widgets turn them into QColor.
"""
from __future__ import annotations

from dataclasses import dataclass

# Spacing scale (px)
SP_1 = 4
SP_2 = 8
SP_3 = 12
SP_4 = 16
SP_5 = 24

# Radii (px)
R_SM = 4
R_MD = 6
R_LG = 8
R_PILL = 999

# Control heights (px)
H_CONTROL = 30
H_BUTTON = 30
H_PRIMARY = 36
H_TOPBAR = 48
H_TRANSPORT = 46
H_TOOLBAR = 36
H_STATUS = 26

# Type scale (px)
FS_CAPTION = 11
FS_BODY = 12
FS_STRONG = 13
FS_TITLE = 15

FONT_UI = "'Segoe UI Variable Text', 'Segoe UI', 'Inter', 'Arial'"
FONT_DISPLAY = "'Segoe UI Variable Display', 'Segoe UI', 'Inter', 'Arial'"

# Icon sizes (px)
ICON_SM = 14
ICON_MD = 16
ICON_LG = 20

# Inspector width (px)
INSPECTOR_MIN = 340
INSPECTOR_DEFAULT = 384
INSPECTOR_MAX = 520


@dataclass(frozen=True)
class Palette:
    # surfaces, darkest → lightest
    app: str          # window chrome / gutters
    panel: str        # inspector, timeline, bars
    raised: str       # inputs, buttons
    hover: str        # hover fill
    pressed: str
    viewer: str       # video background
    border: str       # hairlines between regions
    border_strong: str
    text: str
    text_2: str       # secondary
    text_3: str       # tertiary / disabled
    accent: str
    accent_hover: str
    accent_pressed: str
    accent_soft: str  # selected backgrounds
    focus: str
    success: str
    warning: str
    danger: str
    # timeline semantics
    cut_auto: str
    cut_manual: str
    cut_gameplay: str
    restored: str
    keep_island: str
    wave: str
    wave_removed: str
    playhead: str
    clip_video: str


DARK = Palette(
    app="#0D0F12",
    panel="#14171C",
    raised="#1C2027",
    hover="#252A33",
    pressed="#2D333D",
    viewer="#08090B",
    border="#22262E",
    border_strong="#323843",
    text="#E9EDF3",
    text_2="#A3ACB9",
    text_3="#6A7383",
    accent="#4D8DFF",
    accent_hover="#6AA0FF",
    accent_pressed="#3B78E7",
    accent_soft="#1D2B44",
    focus="#7FB0FF",
    success="#3DD68C",
    warning="#F5A524",
    danger="#F2555A",
    cut_auto="#F2555A",
    cut_manual="#F5A524",
    cut_gameplay="#A579F7",
    restored="#3DD68C",
    keep_island="#2FBF7F",
    wave="#4F95F0",
    wave_removed="#3A414D",
    playhead="#FF4F55",
    clip_video="#33445A",
)

LIGHT = Palette(
    app="#E7EAEF",
    panel="#F5F6F8",
    raised="#FFFFFF",
    hover="#E9EDF3",
    pressed="#DCE2EA",
    viewer="#101215",
    border="#D5DAE2",
    border_strong="#C1C8D2",
    text="#141922",
    text_2="#4A5464",
    text_3="#8A93A1",
    accent="#2F6FEB",
    accent_hover="#447FF0",
    accent_pressed="#2560D2",
    accent_soft="#DCE7FC",
    focus="#2F6FEB",
    success="#1F9D63",
    warning="#C77D00",
    danger="#D93A40",
    cut_auto="#E0464C",
    cut_manual="#D98A00",
    cut_gameplay="#8A5CE6",
    restored="#1F9D63",
    keep_island="#1F9D63",
    wave="#2F6FEB",
    wave_removed="#B9C1CC",
    playhead="#E5383E",
    clip_video="#4D7FB0",
)


def palette(light: bool = False) -> Palette:
    return LIGHT if light else DARK
