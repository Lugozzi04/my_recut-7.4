"""Thumbnails page of the inspector: generate titles + thumbnails, then edit / revert / confirm one of them.

Same engine as the CLI (`analysis.packaging`: PackagingPipeline, ThumbnailEditor); nothing here decides
content. Long work runs on QThreads; results come back as plain data on the GUI thread.

Source of a run:
  * the CURRENT project (a snapshot is written into the output folder: final keeps + gameplay data, no
    re-analysis of an exported file), or
  * a video file chosen by the user (games detected on it).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QComboBox, QDialog, QFileDialog, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSizePolicy, QVBoxLayout,
                               QWidget)

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


class EditWorker(QObject):
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, output: str, number: int, request: str) -> None:
        super().__init__()
        self.output, self.number, self.request = output, number, request

    def run(self) -> None:
        try:
            from analysis.packaging.edit import ThumbnailEditor

            self.finished.emit(ThumbnailEditor(self.output).edit(self.number, self.request))
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
        self.status = QLabel("")
        layout.addWidget(self.status)
        self.busy = QProgressBar()
        self.busy.setRange(0, 0)
        self.busy.setVisible(False)
        self.busy.setFixedHeight(6)
        layout.addWidget(self.busy)
        buttons = QHBoxLayout()
        self.btn_apply = QPushButton("Apply change")
        self.btn_apply.setObjectName("PrimaryCTA")
        self.btn_confirm = QPushButton("Confirm this thumbnail")
        self.btn_folder = QPushButton("Open folder")
        self.btn_close = QPushButton("Close")
        for b in (self.btn_apply, self.btn_confirm, self.btn_folder):
            buttons.addWidget(b)
        buttons.addStretch(1)
        buttons.addWidget(self.btn_close)
        layout.addLayout(buttons)
        self.btn_apply.clicked.connect(self._apply)
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
        edits = "".join(f"\n  v{e['version']}: {e['request']}" for e in item["edits"])
        self.info.setText(f"{item['hook'] or '(no text)'}  -  {item['title']}"
                          f"{'   [CONFIRMED]' if item['approved'] else ''}{edits}")
        self.versions.clear()
        versions = item["versions"] or [item["path"]]
        for n in range(len(versions)):
            self.versions.addItem("original" if n == 0 else f"v{n}", n)
        self.versions.setCurrentIndex(min(item["current_version"], self.versions.count() - 1))
        self.btn_revert.setEnabled(len(item["versions"]) > 1)

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self.busy.setVisible(busy)
        for b in (self.btn_apply, self.btn_confirm, self.btn_revert, self.request):
            b.setEnabled(not busy)
        self.status.setText(message)

    def _apply(self) -> None:
        request = self.request.toPlainText().strip()
        if not request:
            self.status.setText("Describe the change first.")
            return
        self._set_busy(True, "The image model is working on it (about 1-3 minutes)...")
        self._worker = EditWorker(self.output, self.number, request)
        self._thread = _start(self, self._worker, self._edit_done, self._edit_failed)

    def _edit_done(self, result: object) -> None:
        self._set_busy(False, f"New version v{result['version']} ({result['runtime_s']} s).")  # type: ignore[index]
        self.request.clear()
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

    def packaging_options(self) -> dict[str, str]:
        w = self.window
        return {"provider": str(w.thumb_provider_combo.currentData() or "codex"),
                "thumbnail_engine": str(w.thumb_engine_combo.currentData() or "gen"),
                "language": str(w.thumb_language_edit.text().strip() or "it")}

    def confirm_batch(self) -> dict[str, str] | None:
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
        for widget in (w.thumb_source_combo, w.thumb_output_combo, w.thumb_provider_combo,
                       w.thumb_engine_combo, w.thumb_language_edit, w.thumb_video_row):
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
        for entry in getattr(getattr(w, "project", None), "packaging_outputs", []) or []:
            combo.addItem(str(entry.get("title") or Path(entry["output"]).name),
                          str(entry.get("id") or entry["project"]))
        index = combo.findData(selected)
        combo.setCurrentIndex(index if index >= 0 else 0)
        combo.blockSignals(False)
        if select:
            w.thumb_source_combo.setCurrentIndex(w.thumb_source_combo.findData("export"))
        self._update_hint()

    def _selected_output(self) -> dict[str, Any] | None:
        identity = self.window.thumb_output_combo.currentData()
        return next((entry for entry in getattr(getattr(self.window, "project", None), "packaging_outputs", [])
                     if str(entry.get("id") or entry["project"]) == identity), None)

    def _select_output(self) -> None:
        if self.window.thumb_source_combo.currentData() != "export":
            return
        entry = self._selected_output()
        self.output = (entry.get("packaging") or str(Path(entry["project"]).parent / "packaging")) if entry else None
        self.window.thumb_results.setVisible(False)
        self.window.lbl_thumb_status.setText(
            f"{Path(entry['output']).name} · cover: {entry.get('packaging_status', 'not generated')}" if entry else
            "Export class videos first; each exported edit will appear here.")
        self.refresh()

    # ---- inputs
    def set_last_export(self, path: str) -> None:
        self.last_export = str(path)
        w = self.window
        if not w.thumb_video_edit.text().strip():
            w.thumb_video_edit.setText(self.last_export)
        if w.thumb_source_combo.currentData() == "video":
            self._update_hint()

    def _update_hint(self) -> None:
        w = self.window
        source = w.thumb_source_combo.currentData()
        w.thumb_video_row.setVisible(source == "video")
        w.thumb_output_row.setVisible(source == "export")
        self._select_output()
        engine = w.thumb_engine_combo.currentData()
        w.lbl_thumb_hint.setText(
            ("The image model composes the 4 thumbnails (about 5-10 min: one planning call + one generation call). "
             if engine == "gen" else "Thumbnails rendered locally from the planned scenes (one planning call). ")
            + PRIVACY_NOTE)

    def _browse_video(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self.window, "Video for titles and thumbnails", "",
                                              "Video (*.mp4 *.mkv *.mov *.ts *.webm)")
        if path:
            self.window.thumb_video_edit.setText(path)

    def open_existing(self) -> None:
        folder = QFileDialog.getExistingDirectory(self.window, "Open a packaging folder")
        if not folder:
            return
        if not (Path(folder) / "thumbnail_concepts.json").is_file():
            QMessageBox.information(self.window, "Thumbnails", "This folder is not a finished packaging run.")
            return
        self.output = folder
        self.refresh()

    # ---- generation
    def _settings(self) -> Any:
        from analysis.packaging.pipeline import PackagingSettings
        from utils.runtime_paths import exports_root

        w = self.window
        provider = str(w.thumb_provider_combo.currentData() or "codex")
        engine = str(w.thumb_engine_combo.currentData() or "gen")
        language = str(w.thumb_language_edit.text().strip() or "it")
        if w.thumb_source_combo.currentData() == "export":
            from analysis.packaging.outputs import settings_for_output

            entry = self._selected_output()
            if entry is None:
                raise ValueError("Choose an exported video first.")
            return settings_for_output(entry, self.packaging_options())
        stamp = time.strftime("%Y%m%d-%H%M%S")
        if w.thumb_source_combo.currentData() == "project":
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
                                     thumbnail_engine=engine, overwrite=True)
        video = w.thumb_video_edit.text().strip()
        if not video or not Path(video).is_file():
            raise ValueError("Choose the video to analyse.")
        out = Path(video).with_name(f"{Path(video).stem}_packaging")
        return PackagingSettings(output=str(out), video=video, provider=provider, language=language,
                                 thumbnail_engine=engine, games="detect", overwrite=True)

    def generate(self) -> None:
        w = self.window
        if self._batch_busy:
            return
        if self._thread is not None:
            if self._worker is not None:
                self._worker.cancel()
            w.lbl_thumb_status.setText("Cancelling…")
            return
        try:
            settings = self._settings()
        except Exception as exc:  # noqa: BLE001 - shown to the user
            QMessageBox.information(w, "Thumbnails", str(exc))
            return
        answer = QMessageBox.question(w, "Titles and thumbnails", PRIVACY_NOTE + "\n\nContinue?",
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes)
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.output = settings.output
        self._active_parent_id = str(getattr(getattr(w, "project", None), "id", ""))
        self._active_output = None
        if w.thumb_source_combo.currentData() == "export":
            from analysis.packaging.outputs import record_status

            try:
                self._active_output = record_status(self._selected_output() or {}, settings, "running")
                self.register_output(self._active_output)
            except OSError as exc:
                QMessageBox.information(w, "Thumbnails", str(exc))
                return
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
        if self._active_output is not None and self._worker is not None:
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
            f"Done in {round((data.get('timings_s') or {}).get('total', 0))} s."
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
    window.thumb_source_combo.addItem("Current project (kept games)", "project")
    window.thumb_source_combo.addItem("An exported class video", "export")
    window.thumb_source_combo.addItem("A video file", "video")
    gen.add(PropertyRow("Source", window.thumb_source_combo,
                        tooltip="The current project uses its final keeps and gameplay data (no re-analysis)"))
    window.thumb_output_combo = QComboBox()
    window.thumb_output_row = PropertyRow("Exported video", window.thumb_output_combo,
                                          tooltip="Uses the saved edit for this output, not the complete VOD.")
    gen.add(window.thumb_output_row)
    window.thumb_video_edit = QLineEdit()
    window.thumb_video_edit.setPlaceholderText("Exported video…")
    browse = QPushButton("…")
    browse.setFixedWidth(32)
    row = QWidget()
    row_l = QHBoxLayout(row)
    row_l.setContentsMargins(0, 0, 0, 0)
    row_l.addWidget(window.thumb_video_edit, 1)
    row_l.addWidget(browse)
    window.thumb_video_row = PropertyRow("Video", row)
    gen.add(window.thumb_video_row)
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
    window.thumb_output_combo.currentIndexChanged.connect(lambda _i: controller._select_output())
    browse.clicked.connect(controller._browse_video)
    window.btn_thumb_generate.clicked.connect(controller.generate)
    btn_open.clicked.connect(controller.open_existing)
    btn_folder.clicked.connect(controller.open_folder)
    controller._update_hint()

    from ui.layout_main_page import _scroll_page

    window.thumbnails_scroll = _scroll_page(window.page_thumbnails, [gen, res], name="InspectorScroll")
    return controller
