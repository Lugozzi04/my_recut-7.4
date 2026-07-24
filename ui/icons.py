from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPixmap

# SVG rendering (tinted icons)
try:
    from PySide6.QtSvg import QSvgRenderer  # type: ignore
except Exception:
    QSvgRenderer = None  # fallback


class IconSet:
    """
    Loads SVG icons from ./icons/... and tints them to match the theme.
    Works best with outline SVGs; tinting is done by alpha-mask composition.
    """

    def __init__(self, icons_dir: Path):
        try:
            self.icons_dir = Path(icons_dir)
        except Exception:
            self.icons_dir = Path(".")
        self._cache: dict[tuple[str, int, int, int, int], QIcon] = {}

    def _resolve(self, rel: str) -> Optional[Path]:
        try:
            base = self.icons_dir if isinstance(self.icons_dir, Path) else Path(self.icons_dir)
            p = base / str(rel)
            return p if p.exists() else None
        except Exception:
            return None

    def _render_svg_tinted(self, svg_path: Path, size_px: int, color: QColor) -> QIcon:
        # Fallback: attempt direct QIcon(svg)
        if QSvgRenderer is None:
            return QIcon(str(svg_path))

        key = (str(svg_path), size_px, color.red(), color.green(), color.blue())
        if key in self._cache:
            return self._cache[key]

        renderer = QSvgRenderer(str(svg_path))
        img = QImage(size_px, size_px, QImage.Format_ARGB32_Premultiplied)
        img.fill(Qt.transparent)

        p = QPainter(img)
        renderer.render(p)
        p.end()

        # Tint via alpha mask
        tinted = QImage(size_px, size_px, QImage.Format_ARGB32_Premultiplied)
        tinted.fill(Qt.transparent)

        p = QPainter(tinted)
        p.setCompositionMode(QPainter.CompositionMode_Source)
        p.fillRect(tinted.rect(), color)
        p.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        p.drawImage(0, 0, img)
        p.end()

        icon = QIcon(QPixmap.fromImage(tinted))
        self._cache[key] = icon
        return icon

    def icon(self, rel: str, size_px: int, color: QColor) -> QIcon:
        p = self._resolve(rel)
        if not p:
            return QIcon()
        return self._render_svg_tinted(p, size_px, color)

    def first_icon(self, rel_candidates: list[str], size_px: int, color: QColor) -> QIcon:
        for rel in rel_candidates:
            p = self._resolve(rel)
            if p:
                return self._render_svg_tinted(p, size_px, color)
        return QIcon()
