"""Edit page -> "Text": add titles, labels and captions on the video (preview, timeline, export).

One list, one editor. A new text starts at the playhead and lasts 3 s; style presets match the covers.
Texts live in project.text_overlays (timeline seconds) and are drawn by ui.text_render for both the preview
and the export.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import replace
from typing import Any, Sequence

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QWidget,
)

from core.text_overlays import (
    FONT_LABELS,
    FONTS,
    POSITION_LABELS,
    POSITIONS,
    PRESETS,
    TextOverlay,
    active_at,
    apply_preset,
    new_overlay,
    output_intervals,
)
from ui.text_render import OverlayCache, render_overlay
from ui.widgets.inspector import Hint, Section
from utils.timefmt import fmt_hms

CUSTOM = "custom"


def probe_frame_size(ffmpeg_path: str, video: str) -> tuple[int, int]:
    """Width and height of the first video stream (0, 0 when unknown)."""
    import json
    from pathlib import Path

    from utils.subprocess_utils import run_no_window

    exe = Path(ffmpeg_path)
    probe = exe.with_name(exe.name.replace("ffmpeg", "ffprobe")) if exe.name.lower().startswith("ffmpeg") else exe
    try:
        res = run_no_window([str(probe), "-v", "error", "-select_streams", "v:0", "-show_entries",
                             "stream=width,height", "-of", "json", str(video)],
                            capture_output=True, text=True, timeout=20)
        stream = (json.loads(res.stdout or "{}").get("streams") or [{}])[0]
        return int(stream.get("width") or 0), int(stream.get("height") or 0)
    except Exception:
        return 0, 0


def _swatch(color: str) -> QIcon:
    pm = QPixmap(14, 14)
    pm.fill(QColor(color))
    return QIcon(pm)


class TextOverlayController:
    def __init__(self, window: Any) -> None:
        self.w = window
        self.selected_id: str | None = None
        self.cache = OverlayCache()
        self._loading = False
        self.section = self._build()
        self.refresh()

    # ---- data -------------------------------------------------------------------------------
    def overlays(self) -> list[TextOverlay]:
        items = getattr(self.w.project, "text_overlays", None)
        if items is None:
            items = []
            self.w.project.text_overlays = items
        return items

    def _set(self, items: list[TextOverlay]) -> None:
        self.w.project.text_overlays = sorted(items, key=lambda o: (o.start, o.end))
        try:
            self.w._mark_project_dirty(True)
        except Exception:
            pass

    def selected(self) -> TextOverlay | None:
        return next((o for o in self.overlays() if o.id == self.selected_id), None)

    def _replace(self, ov: TextOverlay) -> None:
        self._set([ov if o.id == ov.id else o for o in self.overlays()])
        self._refresh_list()
        self._repaint()

    # ---- UI ---------------------------------------------------------------------------------
    def _build(self) -> Section:
        self.btn_add = QPushButton("+ Add text")
        self.btn_add.setObjectName("SecondarySmall")
        self.btn_add.setCursor(Qt.PointingHandCursor)
        self.btn_add.setToolTip("Add a text at the playhead")
        sec = Section("Text", action=self.btn_add)
        self.empty_hint = Hint("Titles, labels and captions burned into the video. A new text starts at the "
                               "playhead and lasts 3 s; drag it in the preview to move it.")
        sec.add(self.empty_hint)

        self.list = QListWidget()
        self.list.setObjectName("TextOverlayList")
        self.list.setUniformItemSizes(True)
        sec.add(self.list)

        self.editor = QWidget()
        from PySide6.QtWidgets import QVBoxLayout

        ed = QVBoxLayout(self.editor)
        ed.setContentsMargins(0, 0, 0, 0)
        ed.setSpacing(8)
        self.text_edit = QPlainTextEdit()
        self.text_edit.setObjectName("TextOverlayEdit")
        self.text_edit.setPlaceholderText("Your text (Enter = new line)")
        self.text_edit.setFixedHeight(54)
        ed.addWidget(self.text_edit)

        grid_w = QWidget()
        grid = QGridLayout(grid_w)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(1, 1)

        def row(label: str, widget: QWidget, tooltip: str = "") -> None:
            lab = QLabel(label)
            lab.setObjectName("RowLabel")
            if tooltip:
                lab.setToolTip(tooltip)
                widget.setToolTip(tooltip)
            r = grid.rowCount()
            grid.addWidget(lab, r, 0)
            grid.addWidget(widget, r, 1)

        def pair(a: QWidget, b: QWidget) -> QWidget:
            box = QWidget()
            lay = QHBoxLayout(box)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(6)
            lay.addWidget(a, 1)
            lay.addWidget(b, 0)
            return box

        self.style_combo = QComboBox()
        for key, (label, _style, _pos) in PRESETS.items():
            self.style_combo.addItem(label, key)
        self.font_combo = QComboBox()
        for key in FONTS:
            self.font_combo.addItem(FONT_LABELS[key], key)
        self.pos_combo = QComboBox()
        for key, label in POSITION_LABELS.items():
            self.pos_combo.addItem(label, key)
        self.pos_combo.addItem("Custom (dragged)", CUSTOM)
        for combo in (self.style_combo, self.font_combo, self.pos_combo):
            # shrink with the inspector instead of widening the page
            combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(4)
        self.size_spin = QSpinBox()
        self.size_spin.setRange(2, 40)
        self.size_spin.setSuffix(" %")
        self.color_btn = QPushButton("Color")
        self.color_btn.setObjectName("SecondarySmall")
        self.color_btn.setIconSize(QSize(14, 14))
        self.color_btn.setCursor(Qt.PointingHandCursor)
        self.start_spin = QDoubleSpinBox()
        self.start_spin.setDecimals(1)
        self.start_spin.setRange(0.0, 99999.0)
        self.start_spin.setSuffix(" s")
        self.btn_here = QPushButton("Here")
        self.btn_here.setObjectName("SecondarySmall")
        self.btn_here.setCursor(Qt.PointingHandCursor)
        self.btn_here.setToolTip("Start the text at the playhead")
        self.dur_spin = QDoubleSpinBox()
        self.dur_spin.setDecimals(1)
        self.dur_spin.setRange(0.2, 3600.0)
        self.dur_spin.setSingleStep(0.5)
        self.dur_spin.setSuffix(" s")
        row("Style", self.style_combo, "Ready-made looks: Title and Label use the cover fonts")
        row("Font", self.font_combo)
        row("Size", pair(self.size_spin, self.color_btn), "Text height as a share of the video height")
        row("Position", self.pos_combo, "Or drag the text in the preview")
        row("Start", pair(self.start_spin, self.btn_here), "Start on the timeline")
        row("Duration", self.dur_spin)
        ed.addWidget(grid_w)
        self.fade_cb = QCheckBox("Fade in and out")
        ed.addWidget(self.fade_cb)

        actions = QWidget()
        al = QHBoxLayout(actions)
        al.setContentsMargins(0, 2, 0, 0)
        al.setSpacing(6)
        self.btn_goto = QPushButton("Go to")
        self.btn_dup = QPushButton("Duplicate")
        self.btn_del = QPushButton("Delete")
        for b in (self.btn_goto, self.btn_dup, self.btn_del):
            b.setObjectName("SecondarySmall")
            b.setCursor(Qt.PointingHandCursor)
            al.addWidget(b)
        al.addStretch(1)
        ed.addWidget(actions)
        ed.addWidget(Hint("With texts the export re-encodes the whole video (no Smart Render). Texts inside "
                          "removed parts are not shown."))
        sec.add(self.editor)

        self._text_timer = QTimer(self.editor)
        self._text_timer.setSingleShot(True)
        self._text_timer.setInterval(250)
        self._text_timer.timeout.connect(self._commit_text)

        self.btn_add.clicked.connect(lambda: self.add())
        self.list.currentItemChanged.connect(self._on_list_selection)
        self.text_edit.textChanged.connect(self._text_timer.start)
        self.style_combo.activated.connect(self._on_style)
        self.font_combo.activated.connect(lambda _i: self._edit(font=self.font_combo.currentData()))
        self.size_spin.valueChanged.connect(lambda v: self._edit(size=v / 100.0))
        self.color_btn.clicked.connect(self._pick_color)
        self.pos_combo.activated.connect(self._on_position)
        self.start_spin.valueChanged.connect(self._on_start)
        self.dur_spin.valueChanged.connect(self._on_duration)
        self.btn_here.clicked.connect(lambda: self.start_spin.setValue(self._playhead()))
        self.fade_cb.toggled.connect(lambda v: self._edit(fade=bool(v)))
        self.btn_goto.clicked.connect(self._goto)
        self.btn_dup.clicked.connect(self._duplicate)
        self.btn_del.clicked.connect(self.delete_selected)
        return sec

    def _playhead(self) -> float:
        try:
            return max(0.0, float(getattr(self.w, "_last_pos", 0.0) or 0.0))
        except Exception:
            return 0.0

    # ---- actions ----------------------------------------------------------------------------
    def add(self, preset: str | None = None) -> TextOverlay | None:
        if not getattr(self.w, "input_path", None):
            try:
                self.w.statusBar().showMessage("Import a video first, then add texts on it.", 3000)
            except Exception:
                pass
            return None
        last = self.selected()
        ov = new_overlay(self._playhead(), preset or (last.preset if last else "title"))
        self._set(self.overlays() + [ov])
        self.selected_id = ov.id
        self.refresh()
        self.text_edit.setFocus()
        self.text_edit.selectAll()
        return ov

    def delete_selected(self) -> None:
        ov = self.selected()
        if ov is None:
            return
        self._set([o for o in self.overlays() if o.id != ov.id])
        self.selected_id = None
        self.refresh()

    def _duplicate(self) -> None:
        ov = self.selected()
        if ov is None:
            return
        import uuid

        copy_ = replace(ov, id=uuid.uuid4().hex[:12], start=ov.end, end=ov.end + ov.duration)
        self._set(self.overlays() + [copy_])
        self.selected_id = copy_.id
        self.refresh()

    def _goto(self) -> None:
        ov = self.selected()
        if ov is not None:
            try:
                self.w._seek_to(ov.start + min(0.3, ov.duration / 2.0))
            except Exception:
                pass

    def select(self, overlay_id: str | None) -> None:
        self.selected_id = overlay_id
        self.refresh()
        tab = getattr(self.w, "seg_main", None)  # show the editor: the Text section is on the Edit tab
        if overlay_id and tab is not None and not tab.isChecked():
            tab.click()

    def move(self, overlay_id: str, x: float, y: float) -> None:
        ov = next((o for o in self.overlays() if o.id == overlay_id), None)
        if ov is not None:
            self.selected_id = ov.id
            self._replace(replace(ov, x=max(0.0, min(1.0, x)), y=max(0.0, min(1.0, y))))
            self._load_editor()

    # ---- editor bindings --------------------------------------------------------------------
    def _edit(self, **changes: Any) -> None:
        if self._loading:
            return
        ov = self.selected()
        if ov is not None:
            self._replace(replace(ov, **changes))

    def _commit_text(self) -> None:
        text = self.text_edit.toPlainText()
        if text.strip():
            self._edit(text=text[:500])

    def _on_style(self, _i: int) -> None:
        ov = self.selected()
        if ov is None:
            return
        self._replace(apply_preset(ov, str(self.style_combo.currentData()), move=True))
        self._load_editor()

    def _on_position(self, _i: int) -> None:
        key = self.pos_combo.currentData()
        if key in POSITIONS:
            x, y = POSITIONS[key]
            self._edit(x=x, y=y)

    def _on_start(self, value: float) -> None:
        ov = self.selected()
        if ov is not None and not self._loading:
            self._replace(replace(ov, start=float(value), end=float(value) + ov.duration))

    def _on_duration(self, value: float) -> None:
        ov = self.selected()
        if ov is not None and not self._loading:
            self._replace(replace(ov, end=ov.start + float(value)))

    def _pick_color(self) -> None:
        ov = self.selected()
        if ov is None:
            return
        color = QColorDialog.getColor(QColor(ov.color), self.w, "Text color")
        if color.isValid():
            self._edit(color=color.name().upper())
            self.color_btn.setIcon(_swatch(color.name()))

    def _on_list_selection(self, current: QListWidgetItem | None, _prev: Any = None) -> None:
        if self._loading:
            return
        self.selected_id = current.data(Qt.UserRole) if current is not None else None
        self._load_editor()
        self._repaint()

    # ---- refresh ----------------------------------------------------------------------------
    def refresh(self) -> None:
        if self.selected_id and self.selected() is None:
            self.selected_id = None
        self._refresh_list()
        self._load_editor()
        self._repaint()

    def _refresh_list(self) -> None:
        self._loading = True
        try:
            items = self.overlays()
            self.list.clear()
            for ov in items:
                label = ov.display_text().replace("\n", " ")
                item = QListWidgetItem(f"{fmt_hms(ov.start)}  ·  {label[:40]}")
                item.setData(Qt.UserRole, ov.id)
                item.setToolTip(f"{fmt_hms(ov.start)} – {fmt_hms(ov.end)}  ({ov.duration:.1f} s)")
                self.list.addItem(item)
                if ov.id == self.selected_id:
                    self.list.setCurrentItem(item)
            self.list.setFixedHeight(min(110, 8 + 26 * max(1, len(items))))
            self.list.setVisible(bool(items))
            self.empty_hint.setVisible(not items)
            self.section.set_subtitle(f"{len(items)}" if items else "")
        finally:
            self._loading = False

    def _load_editor(self) -> None:
        ov = self.selected()
        self.editor.setVisible(ov is not None)
        if ov is None:
            return
        self._loading = True
        try:
            if self.text_edit.toPlainText() != ov.text:
                self.text_edit.blockSignals(True)
                self.text_edit.setPlainText(ov.text)
                self.text_edit.blockSignals(False)
            self.style_combo.setCurrentIndex(max(0, self.style_combo.findData(ov.preset)))
            self.font_combo.setCurrentIndex(max(0, self.font_combo.findData(ov.font)))
            self.size_spin.setValue(int(round(ov.size * 100)))
            self.color_btn.setIcon(_swatch(ov.color))
            pos = next((k for k, (x, y) in POSITIONS.items() if abs(x - ov.x) < 1e-3 and abs(y - ov.y) < 1e-3),
                       CUSTOM)
            self.pos_combo.setCurrentIndex(self.pos_combo.findData(pos))
            self.start_spin.setValue(ov.start)
            self.dur_spin.setValue(ov.duration)
            self.fade_cb.setChecked(ov.fade)
        finally:
            self._loading = False

    def _repaint(self) -> None:
        tl = getattr(self.w, "timeline", None)
        if tl is not None and hasattr(tl, "set_text_overlays"):
            tl.set_text_overlays([(o.id, o.start, o.end, o.display_text().replace("\n", " "))
                                  for o in self.overlays()], self.selected_id)
        vw = getattr(self.w, "video_widget", None)
        if vw is not None:
            vw.update()

    # ---- preview ----------------------------------------------------------------------------
    def preview_items(self, frame_w: int, frame_h: int) -> list[tuple[Any, int, int, float, str, bool]]:
        """(image, x, y, opacity, id, selected) for the texts visible at the playhead."""
        out = []
        for ov, alpha in active_at(self.overlays(), self._playhead()):
            img, x, y = self.cache.get(ov, frame_w, frame_h)
            if not img.isNull():
                out.append((img, x, y, alpha, ov.id, ov.id == self.selected_id))
        return out

    # ---- export -----------------------------------------------------------------------------
    def export_specs(self, kept: Sequence[tuple[float, float]], frame_w: int, frame_h: int,
                     out_dir: str | None = None) -> list[dict]:
        """Render each text once for the export and map it onto the output timeline."""
        items = [o for o in self.overlays() if o.text.strip()]
        if not items or frame_w <= 0 or frame_h <= 0:
            return []
        out_dir = out_dir or tempfile.mkdtemp(prefix="recut_texts_")
        os.makedirs(out_dir, exist_ok=True)
        specs = []
        for ov in items:
            intervals = output_intervals(kept, ov.start, ov.end)
            if not intervals:
                continue
            img, x, y = render_overlay(ov, frame_w, frame_h)
            if img.isNull():
                continue
            sig = hashlib.sha1(repr((ov.visual_key(), frame_w, frame_h)).encode("utf-8")).hexdigest()[:16]
            png = os.path.join(out_dir, f"text_{sig}.png")
            if not os.path.isfile(png) and not img.save(png):
                raise RuntimeError(f"Cannot write the text image {png}")
            specs.append({"png": png, "x": x, "y": y, "intervals": intervals, "fade": ov.fade, "sig": sig,
                          "text": ov.display_text()})
        return specs
