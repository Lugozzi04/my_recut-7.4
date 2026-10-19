"""Thumbnails page of the inspector: generate titles + thumbnails, then edit / revert / confirm one of them.

Same engine as the CLI (`analysis.packaging`: PackagingPipeline, ThumbnailEditor); nothing here decides
content. Long work runs on QThreads; results come back as plain data on the GUI thread.

Source of a run (two choices; the page decides the rest):
  * Current project: a snapshot is written into the output folder (final keeps + gameplay data, no
    re-analysis). With an Export tab selection there is one cover set per output video, otherwise one
    combined cover.
  * Video: an export registered in the project reuses its saved edit and games; any other file is analysed
    on its own (games detected on it).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSizePolicy, QVBoxLayout,
                               QWidget, QSpinBox)

from ui.widgets.inspector import Hint, PropertyRow, Section

PRIVACY_NOTE = ("Titles and thumbnails send transcript excerpts and frames of the kept games to the provider of the "
                "selected CLI; generated thumbnails also send the webcam and game crops to the codex CLI.")


# ---- workers -----------------------------------------------------------------------------------------------
class PackagingWorker(QObject):
    progress = Signal(str)
    finished = Signal(object)  # packaging report (dict)
    failed = Signal(str)

    def __init__(self, settings: Any) -> None:
        super().__init__()
        self.settings = settings
        from analysis.audio_service import AnalysisCancellation

        self._token = AnalysisCancellation()

    def cancel(self) -> None:
        self._token.cancel()

    def run(self) -> None:
        try:
            from analysis.packaging.pipeline import PackagingPipeline

            report = PackagingPipeline(self.settings, progress=lambda stage, msg: self.progress.emit(f"{stage}: {msg}"),
                                       cancellation=self._token).run()
            self.finished.emit(report)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            if self._token.is_cancelled:
                self.failed.emit("cancelled")
            else:
                self.failed.emit(f"{exc.__class__.__name__}: {exc}")


class PackagingBatchWorker(PackagingWorker):
    output_ready = Signal(object)

    def __init__(self, outputs: list[dict[str, Any]], options: dict[str, Any]) -> None:
        super().__init__(None)
        self.outputs, self.options = outputs, options

    def run(self) -> None:
        try:
            from analysis.packaging.outputs import package_outputs
            outputs, warnings = package_outputs(self.outputs, self.options, cancellation=self._token,
                progress=self.progress.emit, on_result=self.output_ready.emit)
            self._token.raise_if_cancelled()
            self.finished.emit({"outputs": outputs, "warnings": warnings})
        except Exception as exc:
            self.failed.emit("cancelled" if self._token.is_cancelled else str(exc))


class EditWorker(QObject):
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, output: str, number: int, request: str, reference_image: str | None = None, *, polish: bool = False) -> None:
        super().__init__()
        self.output, self.number, self.request = output, number, request
        self.reference_image = reference_image
        self.polish = polish

    def run(self) -> None:
        try:
            from analysis.packaging.edit import ThumbnailEditor

            editor = ThumbnailEditor(self.output)
            options: dict[str, Any] = {"reference_image": self.reference_image}
            if self.polish:
                options["polish"] = True
            self.finished.emit(editor.edit(self.number, self.request, **options))
        except Exception as exc:  # noqa: BLE001 - shown to the user
            self.failed.emit(f"{exc}")


def _start(owner: QObject, worker: QObject, on_done: Any, on_fail: Any, on_progress: Any = None) -> QThread:
    thread = QThread(owner)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)  # type: ignore[attr-defined]
    worker.finished.connect(on_done, Qt.QueuedConnection)  # type: ignore[attr-defined]
    worker.failed.connect(on_fail, Qt.QueuedConnection)  # type: ignore[attr-defined]
    if on_progress is not None:
        worker.progress.connect(on_progress, Qt.QueuedConnection)  # type: ignore[attr-defined]
    worker.finished.connect(thread.quit)  # type: ignore[attr-defined]
    worker.failed.connect(thread.quit)  # type: ignore[attr-defined]
    thread.finished.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)
    thread.start()
    return thread


# ---- widgets -----------------------------------------------------------------------------------------------
class ThumbTile(QLabel):
    clicked = Signal(int)

    def __init__(self, number: int) -> None:
        super().__init__()
        self.number = number
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(160, 90)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setObjectName("ThumbTile")
        self.setToolTip("Click to edit or confirm this thumbnail")
        self._pixmap: QPixmap | None = None

    def set_image(self, path: str | None, label: str) -> None:
        self._pixmap = QPixmap(path) if path and Path(path).is_file() else None
        self.setText("" if self._pixmap else label)
        self._rescale()
        self.setToolTip(f"{label}\nClick to edit or confirm")

    def _rescale(self) -> None:
        if self._pixmap is not None and not self._pixmap.isNull():
            width = max(160, self.width())
            self.setPixmap(self._pixmap.scaled(width, int(width * 9 / 16), Qt.AspectRatioMode.KeepAspectRatio,
                                               Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        self._rescale()

    def mousePressEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.number)
        super().mousePressEvent(event)


class ThumbnailDialog(QDialog):
    """Big preview of one thumbnail: describe a change, apply it (one image-model call), revert, confirm."""

    changed = Signal()

    def __init__(self, parent: QWidget, output: str, number: int) -> None:
        super().__init__(parent)
        from analysis.packaging.edit import ThumbnailEditor

        self.output, self.number = output, number
        self.editor = ThumbnailEditor(output)
        self._thread: QThread | None = None
        self._worker: EditWorker | None = None
        self.reference_image: str | None = None
        self.setWindowTitle(f"Thumbnail {number}")
        self.resize(1000, 760)
        layout = QVBoxLayout(self)
        self.preview = QLabel()
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(640, 360)
        layout.addWidget(self.preview, 1)
        self.info = QLabel()
        self.info.setWordWrap(True)
        self.info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.info)
        self.small_preview = QLabel()
        self.small_preview.setFixedSize(160, 90)
        layout.addWidget(self.small_preview)
        self.local_text = QLineEdit()
        self.local_font = QComboBox()
        from analysis.packaging.render import available_fonts
        for font in available_fonts():
            self.local_font.addItem(font)
        self.local_color = QLineEdit("#FFD436")
        self.local_preset = QComboBox()
        from analysis.packaging.typography import TEXT_PRESETS
        for preset in TEXT_PRESETS:
            self.local_preset.addItem(preset.replace("_", " ").title(), preset)
        self.local_preset.currentIndexChanged.connect(lambda: self.local_color.setText(
            TEXT_PRESETS[str(self.local_preset.currentData())]["color"]))
        self.local_size = QSpinBox()
        self.local_size.setRange(24, 400)
        self.local_size.setValue(230)
        self.local_position = QComboBox()
        for position in ("top", "left", "right", "bottom"):
            self.local_position.addItem(position, position)
        self.btn_local = QPushButton("Apply local text — no AI")
        pack = self.editor.design_pack(self._item()["concept_id"])
        self.local_controls = QWidget()
        local_layout = QVBoxLayout(self.local_controls)
        for label, control in (("Exact text", self.local_text), ("Font", self.local_font), ("Treatment", self.local_preset), ("Colour", self.local_color),
                               ("Max font px", self.local_size), ("Position", self.local_position)):
            local_layout.addWidget(PropertyRow(label, control))
        local_layout.addWidget(self.btn_local)
        self.local_controls.setVisible(bool(pack and pack["typography"]["mode"] == "precision"))
        layout.addWidget(self.local_controls)
        self.btn_local.clicked.connect(self._local_text)
        row = QHBoxLayout()
        row.addWidget(QLabel("Version"))
        self.versions = QComboBox()
        row.addWidget(self.versions, 1)
        self.btn_revert = QPushButton("Use this version")
        row.addWidget(self.btn_revert)
        layout.addLayout(row)
        layout.addWidget(QLabel("What do you want to change? (sent to the image model)"))
        self.request = QPlainTextEdit()
        self.request.setPlaceholderText("e.g. testo: BOARD, font più squadrato e pulito")
        self.request.setFixedHeight(80)
        layout.addWidget(self.request)
        reference_row = QHBoxLayout()
        self.btn_reference = QPushButton("Attach reference image…")
        self.btn_reference.setToolTip("Optional image sent with your request to the image model; saved in this run.")
        self.reference_preview = QLabel()
        self.reference_preview.setFixedSize(72, 54)
        self.reference_label = QLabel("No image attached")
        self.btn_remove_reference = QPushButton("Remove")
        self.btn_remove_reference.setEnabled(False)
        for widget in (self.btn_reference, self.reference_preview, self.reference_label, self.btn_remove_reference):
            reference_row.addWidget(widget)
        reference_row.setStretch(2, 1)
        layout.addLayout(reference_row)
        self.btn_reference.clicked.connect(self._attach_reference)
        self.btn_remove_reference.clicked.connect(self._clear_reference)
        self.status = QLabel("")
        layout.addWidget(self.status)
        self.busy = QProgressBar()
        self.busy.setRange(0, 0)
        self.busy.setVisible(False)
        self.busy.setFixedHeight(6)
        layout.addWidget(self.busy)
        buttons = QHBoxLayout()
        self.btn_apply = QPushButton("Apply change")
        self.btn_polish = QPushButton("Polish Selected")
        self.btn_polish.setToolTip("Uses the current image, Design Pack and references; no new video analysis.")
        self.btn_apply.setObjectName("PrimaryCTA")
        self.btn_confirm = QPushButton("Confirm this thumbnail")
        self.btn_folder = QPushButton("Open folder")
        self.btn_close = QPushButton("Close")
        for b in (self.btn_apply, self.btn_polish, self.btn_confirm, self.btn_folder):
            buttons.addWidget(b)
        buttons.addStretch(1)
        buttons.addWidget(self.btn_close)
        layout.addLayout(buttons)
        self.btn_apply.clicked.connect(self._apply)
        self.btn_polish.clicked.connect(lambda: self._apply(polish=True))
        self.btn_confirm.clicked.connect(self._confirm)
        self.btn_revert.clicked.connect(self._revert)
        self.btn_folder.clicked.connect(self._open_folder)
        self.btn_close.clicked.connect(self.close)
        self._refresh()

    def _item(self) -> dict[str, Any]:
        return self.editor.resolve(self.number)

    def _refresh(self) -> None:
        item = self._item()
        pix = QPixmap(item["path"])
        if not pix.isNull():
            self.preview.setPixmap(pix.scaled(960, 540, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        self.small_preview.setPixmap(pix.scaled(160, 90, Qt.AspectRatioMode.KeepAspectRatio,
                                               Qt.TransformationMode.SmoothTransformation))
        pack = self.editor.design_pack(item["concept_id"])
        if pack:
            t = pack["typography"]
            self.local_preset.setCurrentIndex(max(0, self.local_preset.findData(t.get("preset", "white_impact"))))
            self.local_text.setText(t["text"])
            self.local_color.setText(t.get("color", "#FFD436"))
            self.local_size.setValue(int(t.get("max_px", 230)))
            self.local_position.setCurrentText(t.get("position", "top"))
            self.local_font.setCurrentText(t.get("fonts", ["Anton"])[0])
        edits = "".join(f"\n  v{e['version']}: {e['request']}" for e in item["edits"])
        self.info.setText(f"{item['hook'] or '(no text)'}  -  {item['title']}"
                          f"{'   [CONFIRMED]' if item['approved'] else ''}{edits}"
                          + (f"\nTypography: {item['typography_mode']}" if item.get("typography_mode") else "")
                          + "\n".join([""] + list(item.get("quality_warnings") or [])))
        self.versions.clear()
        versions = item["versions"] or [item["path"]]
        for n in range(len(versions)):
            self.versions.addItem("original" if n == 0 else f"v{n}", n)
        self.versions.setCurrentIndex(min(item["current_version"], self.versions.count() - 1))
        self.btn_revert.setEnabled(len(item["versions"]) > 1)

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self.busy.setVisible(busy)
        for b in (self.btn_apply, self.btn_polish, self.btn_local, self.local_controls, self.btn_confirm, self.btn_revert, self.request, self.btn_reference):
            b.setEnabled(not busy)
        self.btn_remove_reference.setEnabled(not busy and self.reference_image is not None)
        self.status.setText(message)

    def _attach_reference(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, "Attach an image for this change", "",
                                                  "Images (*.png *.jpg *.jpeg *.webp *.bmp)")
        if not filename:
            return
        pix = QPixmap(filename)
        if pix.isNull():
            self.status.setText("Cannot read this image. Choose a PNG, JPEG, WebP or BMP image.")
            return
        self.reference_image = filename
        self.reference_preview.setPixmap(pix.scaled(72, 54, Qt.AspectRatioMode.KeepAspectRatio,
                                                    Qt.TransformationMode.SmoothTransformation))
        self.reference_label.setText(Path(filename).name)
        self.reference_label.setToolTip(filename)
        self.btn_remove_reference.setEnabled(True)
        self.status.setText("The attached image will be sent to the image model with your request.")

    def _clear_reference(self) -> None:
        self.reference_image = None
        self.reference_preview.clear()
        self.reference_label.setText("No image attached")
        self.reference_label.setToolTip("")
        self.btn_remove_reference.setEnabled(False)

    def _local_text(self) -> None:
        try:
            self.editor.typography(self.number, text=self.local_text.text(),
                                   font=self.local_font.currentText(), color=self.local_color.text(),
                                   max_px=self.local_size.value(), position=self.local_position.currentData(),
                                   preset=self.local_preset.currentData())
            self.editor = type(self.editor)(self.output)
            self._refresh()
            self.status.setText("Local text updated. No AI call.")
            self.changed.emit()
        except Exception as exc:
            self.status.setText(str(exc))

    def _apply(self, *, polish: bool = False) -> None:
        request = self.request.toPlainText().strip()
        if polish and not request:
            request = "Improve face lighting/integration, visual hierarchy and background cleanliness. Preserve the story and identity."
        if not request:
            self.status.setText("Describe the change first.")
            return
        self._set_busy(True, "The image model is working on it (about 1-3 minutes)...")
        self._worker = EditWorker(self.output, self.number, request, self.reference_image, polish=polish)
        self._thread = _start(self, self._worker, self._edit_done, self._edit_failed)

    def _edit_done(self, result: object) -> None:
        self._set_busy(False, f"New version v{result['version']} ({result['runtime_s']} s).")  # type: ignore[index]
        self.request.clear()
        self._clear_reference()
        self.editor = type(self.editor)(self.output)
        self._refresh()
        self.changed.emit()

    def _edit_failed(self, message: str) -> None:
        self._set_busy(False, f"Edit failed: {message}")

    def _revert(self) -> None:
        version = int(self.versions.currentData() or 0)
        try:
            self.editor.revert(self.number, version)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            self.status.setText(str(exc))
            return
        self.status.setText(f"Version {version} is the current one.")
        self._refresh()
        self.changed.emit()

    def _confirm(self) -> None:
        try:
            result = self.editor.approve(self.number)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            self.status.setText(str(exc))
            return
        self.status.setText(f"Confirmed: {result['final']}")
        self._refresh()
        self.changed.emit()

    def _open_folder(self) -> None:
        from analysis.packaging.edit import open_in_file_manager

        open_in_file_manager(self.editor.thumbs)

    def closeEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        if self._thread is not None and self._thread.isRunning():
            QMessageBox.information(self, "Thumbnail", "An edit is still running; it will finish in the background.")
        super().closeEvent(event)


# ---- page --------------------------------------------------------------------------------------------------
class ThumbnailsController(QObject):
    """Owns the page state and the background jobs; created by build_thumbnails_page."""

    def __init__(self, window: Any) -> None:
        super().__init__(window)
        self.window = window
        self.output: str | None = None
        self.last_export: str | None = None
        self._files: list[str] = []  # videos picked with "…" that are not registered exports
        self._skipped_outputs: set[str] = set()  # by-class videos unticked under "Covers for"
        self._thread: QThread | None = None
        self._worker: PackagingWorker | None = None
        self._batch_busy = False
        self._active_output: dict[str, Any] | None = None
        self._active_parent_id: str | None = None

    # ---- exported edit registry (also saved in the parent .autocutter)
    def busy(self) -> bool:
        return self._batch_busy or self._thread is not None

    def cancel_for_close(self) -> bool:
        if self._thread is None:
            return False
        if self._worker is not None:
            self._worker.cancel()
        return True

    def packaging_options(self) -> dict[str, Any]:
        w = self.window
        return {"provider": str(w.thumb_provider_combo.currentData() or "codex"),
                "thumbnail_engine": str(w.thumb_engine_combo.currentData() or "gen"),
                "language": str(w.thumb_language_edit.text().strip() or "it"),
                "creative_direction": w.thumb_direction.value()}

    def confirm_batch(self) -> dict[str, Any] | None:
        answer = QMessageBox.question(self.window, "Covers for each video", PRIVACY_NOTE
                                      + "\n\nA separate run will be created for every exported video. Continue?",
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                      QMessageBox.StandardButton.No)
        return self.packaging_options() if answer == QMessageBox.StandardButton.Yes else None

    def set_batch_busy(self, busy: bool) -> None:
        self._batch_busy = busy
        self._enable_inputs()

    def _enable_inputs(self) -> None:
        w = self.window
        for widget in (w.thumb_source_combo, w.thumb_output_combo, w.thumb_provider_combo, w.thumb_class_box,
                       w.thumb_engine_combo, w.thumb_language_edit, w.thumb_direction, w.btn_thumb_browse_video):
            widget.setEnabled(not self.busy())
        w.btn_thumb_generate.setEnabled(not self._batch_busy)

    @Slot(object)
    def register_output(self, output: dict[str, Any]) -> None:
        from copy import deepcopy

        project = getattr(self.window, "project", None)
        if project is None:
            return
        entries = project.packaging_outputs
        identity = str(output.get("id") or output["project"])
        index = next((i for i, entry in enumerate(entries)
                      if str(entry.get("id") or entry["project"]) == identity), None)
        if index is None:
            entries.append(deepcopy(output))
        else:
            entries[index] = deepcopy(output)
        self.restore_outputs(select=identity)
        self.window._mark_project_dirty()

    def restore_outputs(self, *, select: str | None = None) -> None:
        w = self.window
        combo = w.thumb_output_combo
        selected = select or combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        exported = set()
        for entry in getattr(getattr(w, "project", None), "packaging_outputs", []) or []:
            exported.add(str(Path(entry["output"]).expanduser().resolve()))
            combo.addItem(f"{entry.get('title') or Path(entry['output']).name} — export",
                          str(entry.get("id") or entry["project"]))
        for path in self._files:
            if path not in exported:
                combo.addItem(f"{Path(path).name} — file", "file:" + path)
        index = combo.findData(selected)
        combo.setCurrentIndex(index if index >= 0 else 0)
        combo.blockSignals(False)
        if select:
            w.thumb_source_combo.setCurrentIndex(w.thumb_source_combo.findData("video"))
        self._update_hint()

    def _selected_file(self) -> str | None:
        """A picked video that is not a registered export of this project."""
        identity = str(self.window.thumb_output_combo.currentData() or "")
        return identity[5:] if identity.startswith("file:") else None

    def _selected_output(self) -> dict[str, Any] | None:
        identity = self.window.thumb_output_combo.currentData()
        return next((entry for entry in getattr(getattr(self.window, "project", None), "packaging_outputs", [])
                     if str(entry.get("id") or entry["project"]) == identity), None)

    def _select_output(self) -> None:
        if self.window.thumb_source_combo.currentData() != "video":
            return
        picked = self._selected_file()
        if picked:
            self.output = None
            self.window.thumb_results.setVisible(False)
            self.window.lbl_thumb_status.setText(f"{Path(picked).name} · not an export of this project")
            return
        entry = self._selected_output()
        identity = entry.get("id") if entry else None
        if entry and identity != getattr(self, "_direction_output_id", None):
            self._direction_output_id = identity
            if entry.get("creative_direction"):
                run = Path(entry.get("packaging") or Path(entry["project"]).parent / "packaging")
                self.window.thumb_direction.restore(entry["creative_direction"], root=run)
        self.output = (entry.get("packaging") or str(Path(entry["project"]).parent / "packaging")) if entry else None
        self.window.thumb_results.setVisible(False)
        self.window.lbl_thumb_status.setText(
            f"{Path(entry['output']).name} · {'planned (not rendered) · ' if entry.get('render_status') == 'not_rendered' else ''}cover: {entry.get('packaging_status', 'not generated')}" if entry else
            "Choose an exported video, or pick any video file with …")
        self.refresh()

    # ---- inputs
    def set_last_export(self, path: str) -> None:
        """A finished export becomes a pickable Video (registered exports keep their own entry)."""
        self.last_export = str(path)
        self._remember_file(self.last_export)
        self.restore_outputs()

    def _remember_file(self, path: str) -> None:
        path = str(Path(path).expanduser().resolve())
        if path in self._files:
            self._files.remove(path)
        self._files.insert(0, path)

    def _update_hint(self) -> None:
        w = self.window
        source = w.thumb_source_combo.currentData()
        if source != getattr(self, "_display_scope", None):
            self.output = None
            w.thumb_results.setVisible(False)
            self._display_scope = source
        w.thumb_output_row.setVisible(source == "video")
        self._select_output()
        engine = w.thumb_engine_combo.currentData()
        classes = self._refresh_class_list() if source == "project" else []
        w.thumb_class_row.setVisible(bool(classes))
        if source == "project" and classes:
            chosen = [label for key, label in classes if key not in self._skipped_outputs]
            scope = (f"Videos by class: covers for {len(chosen)} of {len(classes)} videos"
                     + (f" ({', '.join(label.split(' · ')[0] for label in chosen)}). " if chosen else ". "))
        elif source == "project" and self._per_output():
            scope = "Single video with the selected classes: one cover set for it. "
        elif source == "project":
            scope = "No Export selection: one combined cover for the whole project. "
        elif self._selected_output() is not None:
            scope = "Exported video: its saved games and final keeps are reused (no re-detection). "
        elif self._selected_file():
            scope = "Video file: games are detected on the video. "
        else:
            scope = ""
        w.lbl_thumb_hint.setText(
            scope +
            ("The image model composes the 4 thumbnails (about 5-10 min: one planning call + one generation call). "
             if engine == "gen" else "Thumbnails rendered locally from the planned scenes (one planning call). ")
            + PRIVACY_NOTE)

    def _class_outputs(self) -> list[tuple[str, str]]:
        """(output group, label) of the videos the Export tab makes in Videos by class mode; [] otherwise."""
        panel = getattr(self.window, "hero_groups_panel", None)
        mode = getattr(panel, "mode", None)
        if (not self._per_output() or mode is None or mode.currentData() != "by_class"
                or not hasattr(self.window, "_build_project_file_payload")):
            return []
        from analysis.hero_groups.labels import class_id, class_label
        from analysis.hero_groups.projects import selected_outputs
        wins = getattr(panel, "wins_only", None)
        try:
            preview = selected_outputs(self.window._build_project_file_payload(),
                                       wins_only=bool(wins.isChecked()) if wins is not None else True)
        except (KeyError, TypeError, ValueError):
            return []
        return [(key, f"{class_label(key) if class_id(key) else 'Portrait group ' + key.split(':')[-1]}"
                      f" · {len(value['wins'])} WIN") for key, value in sorted(preview.items())]

    def _refresh_class_list(self) -> list[tuple[str, str]]:
        layout = self.window.thumb_class_box.layout()
        classes = self._class_outputs()
        current = [(box.property("output_group"), box.text()) for box in self.window.thumb_class_box.findChildren(QCheckBox)]
        if current == classes:
            return classes
        while layout.count():
            item = layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        for key, label in classes:
            box = QCheckBox(label)
            box.setProperty("output_group", key)
            box.setChecked(key not in self._skipped_outputs)
            box.toggled.connect(lambda checked, key=key: self._toggle_output(key, checked))
            layout.addWidget(box)
        return classes

    def _toggle_output(self, key: str, checked: bool) -> None:
        (self._skipped_outputs.discard if checked else self._skipped_outputs.add)(key)
        self._update_hint()

    def _chosen_outputs(self) -> set[str]:
        return {key for key, _ in self._class_outputs() if key not in self._skipped_outputs}

    def _per_output(self) -> bool:
        panel = getattr(self.window, "hero_groups_panel", None)
        return bool(panel is not None and panel.filter_enabled())

    def _browse_video(self) -> None:
        if self.busy():
            return
        name, _ = QFileDialog.getOpenFileName(self.window, "Video for titles and thumbnails", "",
                                              "Video (*.mp4 *.mkv *.mov *.ts *.webm *.avi *.m4v)")
        if name:
            self.select_previous_export(name)

    def select_previous_export(self, video: str) -> None:
        """Select a video without loading/replacing the user's editing project: a recognised export reuses its
        snapshot, any other file is analysed on its own."""
        if self.busy():
            return
        from analysis.packaging.outputs import find_previous_export
        w = self.window
        try:
            entry = find_previous_export(video, getattr(w.project, "packaging_outputs", []))
        except (OSError, ValueError) as exc:
            QMessageBox.information(w, "Thumbnails", str(exc))
            return
        self.output = None
        w.thumb_results.setVisible(False)
        if entry:
            self.register_output(entry)
            w.lbl_thumb_status.setText("Export snapshot found: saved games, final keeps and compatible evidence will be reused.")
        else:
            path = str(Path(video).expanduser().resolve())
            self._remember_file(path)
            self.restore_outputs(select="file:" + path)

    def open_existing(self) -> None:
        if self.busy():
            return
        folder = QFileDialog.getExistingDirectory(
            self.window, "Open run folder (contains thumbnail_concepts.json)")
        if not folder:
            return
        if Path(folder).name == "thumbnails" and (Path(folder).parent / "thumbnail_concepts.json").is_file():
            folder = str(Path(folder).parent)
        if not (Path(folder) / "thumbnail_concepts.json").is_file():
            QMessageBox.information(self.window, "Thumbnails",
                                    "Choose the run folder containing thumbnail_concepts.json, or its thumbnails subfolder.")
            return
        self.output = folder
        direction = Path(folder) / "creative_direction.json"
        if direction.is_file():
            self.window.thumb_direction.restore(json.loads(direction.read_text(encoding="utf-8")), root=Path(folder))
        self.refresh()

    # ---- generation
    def _settings(self) -> Any:
        from analysis.packaging.pipeline import PackagingSettings
        from utils.runtime_paths import exports_root

        w = self.window
        provider = str(w.thumb_provider_combo.currentData() or "codex")
        engine = str(w.thumb_engine_combo.currentData() or "gen")
        language = str(w.thumb_language_edit.text().strip() or "it")
        if w.thumb_source_combo.currentData() == "video":
            from analysis.packaging.outputs import settings_for_output

            entry = self._selected_output()
            if entry is not None:
                return settings_for_output(entry, self.packaging_options())
            video = self._selected_file()
            if not video or not Path(video).is_file():
                raise ValueError("Choose the video to analyse.")
            out = Path(video).with_name(f"{Path(video).stem}_packaging")
            return PackagingSettings(output=str(out), video=video, provider=provider, language=language,
                                     thumbnail_engine=engine, games="detect", overwrite=True,
                                     creative_direction=w.thumb_direction.value())
        stamp = time.strftime("%Y%m%d-%H%M%S")
        # current project
        payload = w._build_project_file_payload()
        if not isinstance(payload, dict) or not payload.get("tracks"):
            raise ValueError("Load a video and edit it first (no tracks in the project).")
        base = Path(self.last_export).with_suffix("") if self.last_export else None
        out = Path(f"{base}_packaging") if base else exports_root() / "packaging" / f"project-{stamp}"
        out.mkdir(parents=True, exist_ok=True)
        from core.project_file import make_payload_portable

        snapshot = out / "source_project.autocutter"
        snapshot.write_text(json.dumps(make_payload_portable(payload, snapshot), ensure_ascii=False, indent=2),
                            encoding="utf-8")
        return PackagingSettings(output=str(out), project=str(snapshot), provider=provider, language=language,
                                 thumbnail_engine=engine, overwrite=True,
                                 creative_direction=w.thumb_direction.value())

    def generate(self) -> None:
        w = self.window
        if self._batch_busy:
            return
        if self._thread is not None:
            if self._worker is not None:
                self._worker.cancel()
            w.lbl_thumb_status.setText("Cancelling…")
            return
        batch_outputs = None
        try:
            panel = getattr(w, "hero_groups_panel", None)
            if w.thumb_source_combo.currentData() == "project" and panel is not None and panel.filter_enabled():
                from analysis.hero_groups.projects import prepare_selected_outputs
                from utils.runtime_paths import exports_root
                import uuid

                separate = panel.mode.currentData() == "by_class"
                only = self._chosen_outputs() if separate else None
                if only is not None and not only:
                    raise ValueError("Tick at least one video under Covers for.")
                folder = exports_root() / "packaging" / ("selection-" + uuid.uuid4().hex)
                batch_outputs, _ = prepare_selected_outputs(w._build_project_file_payload(), folder,
                    separate=separate, output_path=folder / "Selected_games.mp4",
                    wins_only=panel.wins_only.isChecked(), only=only)
                for output in batch_outputs:
                    output["render_status"] = "not_rendered"
                settings = None
            else:
                settings = self._settings()
        except Exception as exc:  # noqa: BLE001 - shown to the user
            QMessageBox.information(w, "Thumbnails", str(exc))
            return
        answer = QMessageBox.question(w, "Titles and thumbnails", PRIVACY_NOTE + "\n\nContinue?",
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes)
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.output = settings.output if settings is not None else None
        self._active_parent_id = str(getattr(getattr(w, "project", None), "id", ""))
        self._active_output = None
        if w.thumb_source_combo.currentData() == "video" and self._selected_output() is not None:
            assert settings is not None
            from analysis.packaging.outputs import record_status

            try:
                self._active_output = record_status(self._selected_output() or {}, settings, "running")
                self.register_output(self._active_output)
            except OSError as exc:
                QMessageBox.information(w, "Thumbnails", str(exc))
                return
        if batch_outputs is not None:
            self._worker = PackagingBatchWorker(batch_outputs, self.packaging_options())
            self._worker.output_ready.connect(self.register_output, Qt.ConnectionType.QueuedConnection)
        else:
            self._worker = PackagingWorker(settings)
        self._thread = _start(self, self._worker, self._done, self._failed, self._progress)
        self._thread.finished.connect(self._finish_job)
        self._enable_inputs()
        w.btn_thumb_generate.setText("Cancel")
        w.thumb_progress.setVisible(True)
        w.lbl_thumb_status.setText("Starting…")

    def _progress(self, message: str) -> None:
        self.window.lbl_thumb_status.setText(message[:160])

    def _finish_job(self) -> None:
        w = self.window
        self._thread, self._worker = None, None
        w.btn_thumb_generate.setText("Generate titles and thumbnails")
        w.thumb_progress.setVisible(False)
        self._enable_inputs()

    def _save_output_status(self, status: str) -> None:
        if self._active_output is not None and self._worker is not None and self._worker.settings is not None:
            from analysis.packaging.outputs import record_status

            try:
                record = record_status(self._active_output, self._worker.settings, status)
                if self._active_parent_id == str(getattr(getattr(self.window, "project", None), "id", "")):
                    self.register_output(record)
            except OSError as exc:
                self.window.lbl_thumb_status.setText(f"Cover status could not be saved: {exc}")

    def _done(self, report: object) -> None:
        self._save_output_status("done")
        data = report if isinstance(report, dict) else {}
        warnings = data.get("warnings") or []
        self.window.lbl_thumb_status.setText(
            (f"Completed cover processing for {len(data['outputs'])} separate output(s)." if "outputs" in data else
             f"Done in {round((data.get('timings_s') or {}).get('total', 0))} s.")
            + (f" {len(warnings)} warning(s): {warnings[0][:120]}" if warnings else ""))
        self.refresh()

    def _failed(self, message: str) -> None:
        self._save_output_status("cancelled" if message == "cancelled" else "failed")
        self.window.lbl_thumb_status.setText("Cancelled." if message == "cancelled" else f"Failed: {message[:300]}")

    # ---- results
    def refresh(self) -> None:
        w = self.window
        if not self.output or not (Path(self.output) / "thumbnail_concepts.json").is_file():
            return
        from analysis.packaging.edit import ThumbnailEditor

        editor = ThumbnailEditor(self.output)
        items = editor.concepts()
        for tile in w.thumb_tiles:
            item = next((i for i in items if i["number"] == tile.number), None)
            tile.setVisible(item is not None)
            if item is not None:
                tile.set_image(item["path"], f"{item['number']}. {item['hook'] or '(no text)'}"
                               + ("  ✓" if item["approved"] else ""))
        try:
            titles = json.loads((Path(self.output) / "title_candidates.json").read_text(encoding="utf-8"))
            lines = [f"{t['title_id']}: {t['title']}" for t in titles.get("titles", [])]
        except (OSError, ValueError, KeyError):
            lines = []
        w.thumb_titles.setPlainText("\n".join(lines))
        approved = next((i for i in items if i["approved"]), None)
        w.lbl_thumb_folder.setText(f"{self.output}" + (f"\nConfirmed: thumbnail {approved['number']}" if approved else ""))
        w.thumb_results.setVisible(True)

    def open_tile(self, number: int) -> None:
        if not self.output:
            return
        dialog = ThumbnailDialog(self.window, self.output, number)
        dialog.changed.connect(self.refresh)
        dialog.show()

    def open_folder(self) -> None:
        if self.output:
            from analysis.packaging.edit import open_in_file_manager

            open_in_file_manager(Path(self.output) / "thumbnails")


def build_thumbnails_page(window: Any) -> ThumbnailsController:
    """Builds window.page_thumbnails' content; returns its controller (window.thumbnails)."""
    controller = ThumbnailsController(window)
    gen = Section("Titles and thumbnails", subtitle="YouTube packaging")
    window.thumb_source_combo = QComboBox()
    window.thumb_source_combo.addItem("Current project", "project")
    window.thumb_source_combo.addItem("Video", "video")
    gen.add(PropertyRow("Source", window.thumb_source_combo,
                        tooltip="Current project: final keeps and gameplay data, one cover set per video of the "
                                "Export selection. Video: an export of this project or any video file."))
    window.thumb_output_combo = QComboBox()
    window.btn_thumb_browse_video = QPushButton("…")
    window.btn_thumb_browse_video.setFixedWidth(32)
    window.btn_thumb_browse_video.setToolTip("Choose a video file; an existing thumbnail run is not required.")
    row = QWidget()
    row_l = QHBoxLayout(row)
    row_l.setContentsMargins(0, 0, 0, 0)
    row_l.addWidget(window.thumb_output_combo, 1)
    row_l.addWidget(window.btn_thumb_browse_video)
    window.thumb_output_row = PropertyRow("Video", row, tooltip="Exports reuse their saved edit and games; "
                                                                "other files are analysed on their own.")
    gen.add(window.thumb_output_row)
    window.thumb_class_box = QWidget()
    QVBoxLayout(window.thumb_class_box).setContentsMargins(0, 0, 0, 0)
    window.thumb_class_row = PropertyRow("Covers for", window.thumb_class_box,
                                         tooltip="Videos by class from the Export tab. Untick a video to skip its "
                                                 "covers; the export itself does not change.")
    window.thumb_class_row.setVisible(False)
    gen.add(window.thumb_class_row)
    window.thumb_provider_combo = QComboBox()
    window.thumb_provider_combo.addItem("Codex", "codex")
    window.thumb_provider_combo.addItem("Claude", "claude")
    gen.add(PropertyRow("Agent", window.thumb_provider_combo, tooltip="CLI that writes titles and concepts"))
    window.thumb_engine_combo = QComboBox()
    window.thumb_engine_combo.addItem("Generated (image model)", "gen")
    window.thumb_engine_combo.addItem("Local render", "v3")
    gen.add(PropertyRow("Thumbnails", window.thumb_engine_combo,
                        tooltip="Generated: codex image_gen composes them; Local: rendered on this PC"))
    window.thumb_language_edit = QLineEdit("it")
    window.thumb_language_edit.setMaximumWidth(60)
    gen.add(PropertyRow("Language", window.thumb_language_edit))
    window.lbl_thumb_hint = Hint("")
    gen.add(window.lbl_thumb_hint)
    from ui.thumbnail_direction import CreativeDirectionWidget
    window.thumb_direction = CreativeDirectionWidget(window)
    window.thumb_direction.restore(getattr(window.project, "creative_direction", {}) or {})
    def save_direction() -> None:
        from copy import deepcopy
        value = window.thumb_direction.value()
        window.project.creative_direction = value
        if window.thumb_source_combo.currentData() == "video":
            selected = controller._selected_output()
            if selected is not None:
                selected["creative_direction"] = deepcopy(value)
        if hasattr(window, "_mark_project_dirty"):
            window._mark_project_dirty(True)
    window.thumb_direction.changed.connect(save_direction)
    gen.add(window.thumb_direction)
    window.btn_thumb_generate = QPushButton("Generate titles and thumbnails")
    window.btn_thumb_generate.setObjectName("PrimaryCTA")
    window.btn_thumb_generate.setCursor(Qt.CursorShape.PointingHandCursor)
    gen.add(window.btn_thumb_generate)
    window.thumb_progress = QProgressBar()
    window.thumb_progress.setRange(0, 0)
    window.thumb_progress.setFixedHeight(6)
    window.thumb_progress.setVisible(False)
    gen.add(window.thumb_progress)
    window.lbl_thumb_status = Hint("")
    window.lbl_thumb_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    gen.add(window.lbl_thumb_status)
    btn_open = QPushButton("Open an existing run…")
    btn_open.setObjectName("LinkButton")
    btn_open.setToolTip("Choose the folder containing thumbnail_concepts.json (not the MP4 or a single PNG).")
    gen.add(btn_open)

    res = Section("Results", subtitle="click a thumbnail to edit or confirm it")
    window.thumb_results = res
    grid_host = QWidget()
    grid = QGridLayout(grid_host)
    grid.setContentsMargins(0, 0, 0, 0)
    grid.setSpacing(6)
    window.thumb_tiles = []
    for n in range(1, 7):
        tile = ThumbTile(n)
        tile.clicked.connect(controller.open_tile)
        tile.setVisible(False)
        grid.addWidget(tile, (n - 1) // 2, (n - 1) % 2)
        window.thumb_tiles.append(tile)
    res.add(grid_host)
    window.thumb_titles = QPlainTextEdit()
    window.thumb_titles.setReadOnly(True)
    window.thumb_titles.setFixedHeight(120)
    res.add(PropertyRow("Titles", window.thumb_titles))
    window.lbl_thumb_folder = Hint("")
    window.lbl_thumb_folder.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    res.add(window.lbl_thumb_folder)
    btn_folder = QPushButton("Open thumbnails folder")
    btn_folder.setObjectName("LinkButton")
    res.add(btn_folder)
    res.setVisible(False)

    window.thumb_source_combo.currentIndexChanged.connect(lambda _i: controller._update_hint())
    window.thumb_engine_combo.currentIndexChanged.connect(lambda _i: controller._update_hint())
    window.thumb_output_combo.currentIndexChanged.connect(lambda _i: controller._update_hint())
    window.btn_thumb_browse_video.clicked.connect(controller._browse_video)
    window.btn_thumb_generate.clicked.connect(controller.generate)
    btn_open.clicked.connect(controller.open_existing)
    btn_folder.clicked.connect(controller.open_folder)
    controller._update_hint()

    from ui.layout_main_page import _scroll_page

    window.thumbnails_scroll = _scroll_page(window.page_thumbnails, [gen, res], name="InspectorScroll")
    return controller
