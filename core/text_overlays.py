"""Text overlays ("scritte") placed on the edit timeline and burned into the exported video.

Times are timeline seconds (the same coordinates as clips and cuts), so a text stays on its moment when cuts
change; the parts of a text that fall inside removed ranges simply are not shown. Position and size are
relative to the video frame, so the same text works at any resolution.
"""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Iterable, Mapping, Sequence

# Font keys -> (Qt family, Qt style, file name). Bundled in fonts/ (SIL OFL / Apache 2.0), all free for
# monetized videos. Anton and Luckiest Guy are the cover (thumbnail) fonts.
FONTS: dict[str, tuple[str, str, str]] = {
    "luckiest_guy": ("Luckiest Guy", "Regular", "LuckiestGuy-Regular.ttf"),
    "anton": ("Anton", "Regular", "Anton-Regular.ttf"),
    "bebas_neue": ("Bebas Neue", "Regular", "BebasNeue-Regular.ttf"),
    "bangers": ("Bangers", "Regular", "Bangers-Regular.ttf"),
    "montserrat": ("Montserrat", "ExtraBold", "Montserrat-ExtraBold.ttf"),
    "poppins": ("Poppins", "Bold", "Poppins-Bold.ttf"),
}
FONT_LABELS: dict[str, str] = {
    "luckiest_guy": "Luckiest Guy (cover titles)",
    "anton": "Anton (cover labels)",
    "bebas_neue": "Bebas Neue",
    "bangers": "Bangers",
    "montserrat": "Montserrat ExtraBold",
    "poppins": "Poppins Bold",
}

# Anchor positions (centre of the text block, relative to the frame).
POSITIONS: dict[str, tuple[float, float]] = {
    "top": (0.5, 0.14),
    "center": (0.5, 0.5),
    "bottom": (0.5, 0.86),
    "lower_left": (0.27, 0.84),
}
POSITION_LABELS = {"top": "Top", "center": "Center", "bottom": "Bottom", "lower_left": "Lower left"}


@dataclass(frozen=True)
class TextStyle:
    font: str
    size: float           # text height as a fraction of the frame height
    color: str            # fill
    outline: str = ""     # outline colour ("" = none)
    outline_width: float = 0.0   # fraction of the font size
    shadow: bool = False  # hard 3D drop shadow (cover style)
    box: str = ""         # background box colour with alpha, "#AARRGGBB" ("" = none)
    uppercase: bool = False


PRESETS: dict[str, tuple[str, TextStyle, str]] = {
    # key: (label, style, default position)
    "title": ("Title (cover)", TextStyle("luckiest_guy", 0.11, "#FFFFFF", "#000000", 0.16, True, "", True), "top"),
    "label": ("Label (cover)", TextStyle("anton", 0.075, "#FFFFFF", "", 0.0, False, "#F0D7263D", True), "center"),
    "impact": ("Impact", TextStyle("bebas_neue", 0.13, "#FFD43B", "#000000", 0.10, True, "", True), "center"),
    "comic": ("Comic", TextStyle("bangers", 0.10, "#FFE14D", "#000000", 0.14, True, "", True), "center"),
    "caption": ("Caption", TextStyle("montserrat", 0.055, "#FFFFFF", "#000000", 0.14, False, "", False), "bottom"),
    "lower_third": ("Lower third", TextStyle("poppins", 0.045, "#FFFFFF", "", 0.0, False, "#C0101318", False),
                    "lower_left"),
}
DEFAULT_PRESET = "title"
DEFAULT_DURATION = 3.0
FADE_SECONDS = 0.2


@dataclass(frozen=True)
class TextOverlay:
    text: str
    start: float
    end: float
    preset: str = DEFAULT_PRESET
    font: str = "luckiest_guy"
    size: float = 0.11
    color: str = "#FFFFFF"
    outline: str = "#000000"
    outline_width: float = 0.16
    shadow: bool = True
    box: str = ""
    uppercase: bool = True
    x: float = 0.5
    y: float = 0.14
    fade: bool = True
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def display_text(self) -> str:
        text = self.text.strip("\n")
        return text.upper() if self.uppercase else text

    def visual_key(self) -> tuple:
        """Everything that changes the rendered image (not the timing)."""
        return (self.display_text(), self.font, round(self.size, 4), self.color, self.outline,
                round(self.outline_width, 4), self.shadow, self.box, round(self.x, 4), round(self.y, 4))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def new_overlay(start: float, preset: str = DEFAULT_PRESET, text: str = "",
                duration: float = DEFAULT_DURATION) -> TextOverlay:
    label, style, position = PRESETS.get(preset, PRESETS[DEFAULT_PRESET])
    x, y = POSITIONS[position]
    start = max(0.0, float(start))
    return TextOverlay(text=text or "YOUR TEXT", start=start, end=start + max(0.2, float(duration)),
                       preset=preset if preset in PRESETS else DEFAULT_PRESET, x=x, y=y,
                       **{k: getattr(style, k) for k in ("font", "size", "color", "outline", "outline_width",
                                                          "shadow", "box", "uppercase")})


def apply_preset(overlay: TextOverlay, preset: str, *, move: bool = True) -> TextOverlay:
    if preset not in PRESETS:
        return overlay
    _label, style, position = PRESETS[preset]
    x, y = POSITIONS[position] if move else (overlay.x, overlay.y)
    return replace(overlay, preset=preset, x=x, y=y,
                   **{k: getattr(style, k) for k in ("font", "size", "color", "outline", "outline_width",
                                                      "shadow", "box", "uppercase")})


def _clamp(value: Any, low: float, high: float, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    return max(low, min(high, v))


def _color(value: Any, default: str) -> str:
    s = str(value or "").strip()
    if s.startswith("#") and len(s) in (7, 9):
        try:
            int(s[1:], 16)
            return s.upper()
        except ValueError:
            pass
    return default


def overlay_from_dict(raw: Mapping[str, Any]) -> TextOverlay | None:
    try:
        start = float(raw.get("start", 0.0))
        end = float(raw.get("end", start + DEFAULT_DURATION))
    except (TypeError, ValueError):
        return None
    text = str(raw.get("text") or "")
    if not text.strip() or end <= start:
        return None
    base = new_overlay(start, str(raw.get("preset") or DEFAULT_PRESET))
    font = str(raw.get("font") or base.font)
    return TextOverlay(
        text=text[:500], start=max(0.0, start), end=end, preset=base.preset,
        font=font if font in FONTS else base.font,
        size=_clamp(raw.get("size"), 0.02, 0.4, base.size),
        color=_color(raw.get("color"), base.color),
        outline=_color(raw.get("outline"), "") if raw.get("outline") else "",
        outline_width=_clamp(raw.get("outline_width"), 0.0, 0.4, base.outline_width),
        shadow=bool(raw.get("shadow", base.shadow)),
        box=_color(raw.get("box"), "") if raw.get("box") else "",
        uppercase=bool(raw.get("uppercase", base.uppercase)),
        x=_clamp(raw.get("x"), 0.0, 1.0, base.x), y=_clamp(raw.get("y"), 0.0, 1.0, base.y),
        fade=bool(raw.get("fade", True)),
        id=str(raw.get("id") or uuid.uuid4().hex[:12])[:40],
    )


def overlays_from_payload(items: Iterable[Any] | None) -> list[TextOverlay]:
    out = [ov for ov in (overlay_from_dict(i) for i in (items or []) if isinstance(i, Mapping)) if ov]
    return sorted(out, key=lambda o: (o.start, o.end))


def overlays_to_payload(overlays: Iterable[TextOverlay]) -> list[dict[str, Any]]:
    return [o.to_dict() for o in sorted(overlays, key=lambda o: (o.start, o.end)) if o.text.strip()]


def active_at(overlays: Iterable[TextOverlay], t: float) -> list[tuple[TextOverlay, float]]:
    """Overlays visible at timeline time t, each with its opacity (fades in/out)."""
    out = []
    for o in overlays:
        if o.start <= t < o.end:
            alpha = 1.0
            if o.fade:
                fade = min(FADE_SECONDS, o.duration / 3.0)
                if fade > 0:
                    alpha = max(0.0, min(1.0, (t - o.start) / fade, (o.end - t) / fade))
            out.append((o, alpha))
    return out


def output_intervals(segments: Sequence[tuple[float, float]], start: float, end: float) -> list[tuple[float, float]]:
    """Map a timeline range to the exported video's time.

    segments: the kept timeline ranges in export order. Returns the (merged) output-time intervals where the
    text is on screen.
    """
    out: list[tuple[float, float]] = []
    cursor = 0.0
    for s0, s1 in segments:
        s0, s1 = float(s0), float(s1)
        if s1 <= s0:
            continue
        a, b = max(start, s0), min(end, s1)
        if b > a + 1e-6:
            oa, ob = cursor + (a - s0), cursor + (b - s0)
            if out and abs(out[-1][1] - oa) < 1e-4:
                out[-1] = (out[-1][0], ob)
            else:
                out.append((oa, ob))
        cursor += s1 - s0
    return out
