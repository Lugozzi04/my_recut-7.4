"""Compact Gameplay panel for local portrait groups and shared group exports."""

from __future__ import annotations

import base64
from copy import deepcopy
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QThread, Qt, QSize, Signal, Slot
from PySide6.QtGui import QIcon, QPixmap, QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QInputDialog,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from analysis.audio_service import AnalysisCancellation
from analysis.hero_groups.projects import analyze_project, assign_group, prepare_outputs, export_outputs, exportable_group_games
from analysis.hero_groups.recognition import HeroGroupingService, refresh_groups
from automation.exporter import ExportCancellation
from analysis.hero_groups.labels import CLASS_COLORS, class_id, class_label, display_group


class GroupWorker(QObject):
    finished = Signal(object)
    failed = Signal(str)
    ended = Signal()
    progress = Signal(int)
    detail = Signal(str)
    output_ready = Signal(object)

    def __init__(
        self, payload: dict[str, Any], *, class_refs: str = "", output_dir: str = "", source: str = "",
        packaging_options: dict[str, Any] | None = None
    ) -> None:
        super().__init__()
        self.payload = payload
        self.class_refs = class_refs
        self.output_dir = output_dir
        self.source = source
        self.packaging_options = packaging_options
        self.packaging_token = AnalysisCancellation()
        self.token: Any = ExportCancellation() if output_dir else AnalysisCancellation()

    def cancel(self) -> None:
        self.token.cancel()
        self.packaging_token.cancel()

    @Slot()
    def run(self) -> None:
        try:
            if self.output_dir:
                available = exportable_group_games(self.payload, source=self.source or None)
                selected = sorted({g for t in self.payload["tracks"]
                                   for g in t.get("gameplay_grouping", {}).get("selected", []) if g in available})
                plans, warnings = prepare_outputs(self.payload, Path(self.output_dir), selected, source=self.source or None)
                outputs = export_outputs(plans, cancellation=self.token, on_progress=self.progress.emit,
                                         on_completed=self.output_ready.emit)
                if self.packaging_options is not None:
                    from analysis.packaging.outputs import package_outputs

                    outputs, cover_warnings = package_outputs(
                        outputs, self.packaging_options, cancellation=self.packaging_token,
                        progress=self.detail.emit, on_result=self.output_ready.emit)
                    warnings.extend(cover_warnings)
                self.finished.emit({"outputs": outputs, "warnings": warnings})
            else:
                service = HeroGroupingService(class_refs=Path(self.class_refs) if self.class_refs else None)
                payload = analyze_project(
                    self.payload, service=service, cancellation=self.token, on_progress=self.progress.emit
                )
                self.finished.emit({"payload": payload})
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self.ended.emit()


def game_spans(games: list[dict[str, Any]], duration: float) -> list[tuple[float, float] | None]:
    """Display interval of each game: observed START/END; a missing side extends to the
    nearest boundary of a neighbouring game (or the file edge). None when not valid."""
    anchors = [(g.get("start"), g.get("end")) for g in games]
    out: list[tuple[float, float] | None] = []
    for index, (start, end) in enumerate(anchors):
        own = [float(t) for t in (start, end) if t is not None]
        if not own:
            out.append(None)
            continue
        others = [float(t) for i, pair in enumerate(anchors) if i != index for t in pair if t is not None]
        a = float(start) if start is not None else max((t for t in others if t < min(own)), default=0.0)
        b = float(end) if end is not None else min((t for t in others if t > max(own)), default=duration)
        out.append((a, b) if b > a else None)
    return out


class HeroGroupsPanel(QWidget):
    def __init__(self, window: Any) -> None:
        super().__init__(window)
        self.owner: Any = window
        self.work_thread: QThread | None = None
        self.worker: GroupWorker | None = None
        self.source = ""
        self.syncing = False
        self.highlighted: Any = None  # key of the game shown in yellow on the timeline (view-only)
        self.track_index: int | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.addWidget(QLabel("Player classes"))
        hint = QLabel("Choose which classes to export in the Export tab.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.games = QTreeWidget()
        self.games.setHeaderLabels(["Game / portrait", "Result", "Class", "Name", "Similarity / samples"])
        self.games.setRootIsDecorated(False)
        self.games.setIconSize(QSize(32, 40))
        self.games.setMaximumHeight(230)
        self.games.setToolTip("Double-click Class to correct it. ≈ means estimated; similarity is not a probability.")
        layout.addWidget(self.games)
        self.outputs = QWidget(window)
        output_layout = QVBoxLayout(self.outputs)
        output_layout.setContentsMargins(0, 0, 0, 0)
        self.groups = QTreeWidget()
        self.groups.setHeaderLabels(["Export / class", "Name (optional)", "WINs kept"])
        self.groups.setRootIsDecorated(False)
        self.groups.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.groups.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.groups.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.groups.setIconSize(QSize(32, 40))
        self.groups.setMaximumHeight(140)
        self.groups.setToolTip("Check groups to export; double-click Name to rename.")
        output_layout.addWidget(self.groups)
        self.covers = QCheckBox("Generate a cover for each video")
        self.covers.setToolTip("Uses the provider, language and thumbnail engine in Thumbnails. "
                               "Each video uses only its own kept games; provider calls are optional.")
        output_layout.addWidget(self.covers)
        self.export = QPushButton("Export selected groups separately…")
        output_layout.addWidget(self.export)
        self.cancel = QPushButton("Cancel class exports")
        self.cancel.setEnabled(False)
        self.cancel.setVisible(False)
        output_layout.addWidget(self.cancel)
        self.status = QLabel("Detect games first. Grouping uses local images only.")
        self.status.setWordWrap(True)
        output_layout.addWidget(self.status)
        self.cancel.clicked.connect(self.cancel_work)
        self.export.clicked.connect(self.start_export)
        self.games.itemDoubleClicked.connect(self.edit_game)
        self.games.itemClicked.connect(self.toggle_highlight)
        self.groups.itemDoubleClicked.connect(self.edit_name)
        self.groups.itemChanged.connect(self.select_groups)
        self.refresh()

    def running(self) -> bool:
        return self.work_thread is not None and self.work_thread.isRunning()

    def cancel_work(self) -> None:
        if self.worker:
            self.worker.cancel()

    def _tracks(self) -> list[Any]:
        return [
            t
            for t in getattr(self.owner, "_tracks", [])
            if getattr(t, "path", None) and str(Path(t.path).resolve()) == self.source
        ]

    def refresh(self) -> None:
        if self.running():
            return
        tracks = getattr(self.owner, "_tracks", [])
        index = getattr(self.owner, "_active_track_index", 0) or 0
        active = tracks[index] if tracks and 0 <= index < len(tracks) else None
        candidates = [active] if active and getattr(active, "gameplay_report", None) else []
        candidates += [t for t in getattr(self.owner, "_tracks", []) if getattr(t, "gameplay_report", None)]
        track = candidates[0] if candidates else None
        self.track_index = next((i for i, t in enumerate(getattr(self.owner, "_tracks", []) or []) if t is track), None)
        self.source = str(Path(track.path).resolve()) if track else ""
        report = getattr(track, "gameplay_report", {}) if track else {}
        grouping = getattr(track, "gameplay_grouping", {}) if track else {}
        rows = grouping.get("games") or [{"game": g} for g in report.get("games", [])]
        available = self._available()
        self.outputs.setVisible(bool(available))
        section = getattr(self.owner, "section_gameplay_exports", None)
        if section is not None:
            section.setVisible(bool(available))
        spans = game_spans([row["game"] for row in rows], float(report.get("duration") or getattr(track, "duration", 0) or 0))
        self.syncing = True
        try:
            self.games.clear()
            for row, span in zip(rows, spans):
                game = row["game"]
                group = row.get("group")
                name = grouping.get("groups", {}).get(group, {}).get("name", "")
                manual = row.get("key") in grouping.get("overrides", {})
                item = QTreeWidgetItem(
                    [
                        str(game["index"]),
                        str(game["result"]),
                        class_label(grouping.get("groups", {}).get(group, {}).get("class_name"))
                        if group
                        else "Da assegnare",
                        name,
                        "manual"
                        if manual
                        else ("≈ " if row.get("class_status") in ("estimated", "conflict") else "")
                        + (
                            f"{row.get('class_similarity', row.get('similarity', 0)):.3f} / {row.get('class_support', row.get('support', 0))}"
                            if group
                            else "—"
                        ),
                    ]
                )
                item.setData(0, Qt.ItemDataRole.UserRole, row.get("key"))
                item.setData(1, Qt.ItemDataRole.UserRole, span)
                item.setToolTip(0, row.get("reason", ""))
                hero_class = class_id(group) or class_id(grouping.get("groups", {}).get(group, {}).get("class_name"))
                if hero_class:
                    item.setForeground(2, QColor(CLASS_COLORS[hero_class]))
                item.setToolTip(
                    4,
                    f"Source: {row.get('class_source', 'portrait')} · status: {row.get('class_status', 'legacy')} · margin: {row.get('class_margin', 0):.3f}",
                )
                if row.get("portrait"):
                    pixmap = QPixmap()
                    pixmap.loadFromData(base64.b64decode(row["portrait"]))
                    item.setIcon(0, QIcon(pixmap))
                self.games.addTopLevelItem(item)
                if self.highlighted is not None and row.get("key") == self.highlighted:
                    item.setSelected(True)
            self.groups.clear()
            for key, group in sorted(grouping.get("groups", {}).items()):
                if key not in available:
                    continue
                item = QTreeWidgetItem(
                    [
                        class_label(group.get("class_name")),
                        group.get("name") or "",
                        str(len(available[key])),
                    ]
                )
                item.setData(0, Qt.ItemDataRole.UserRole, key)
                hero_class = class_id(group.get("class_name"))
                if hero_class:
                    item.setForeground(0, QColor(CLASS_COLORS[hero_class]))
                item.setCheckState(
                    0, Qt.CheckState.Checked if key in grouping.get("selected", []) else Qt.CheckState.Unchecked
                )
                if group.get("portrait"):
                    pixmap = QPixmap()
                    pixmap.loadFromData(base64.b64decode(group["portrait"]))
                    item.setIcon(0, QIcon(pixmap))
                self.groups.addTopLevelItem(item)
        finally:
            self.syncing = False
        if self.highlighted is not None:
            current = [self.games.topLevelItem(i) for i in range(self.games.topLevelItemCount())]
            match = next((it for it in current if it is not None and it.data(0, Qt.ItemDataRole.UserRole) == self.highlighted), None)
            self._show_highlight(match)
        self.export.setEnabled(any(g in available for g in grouping.get("selected", [])))
        self.games.setEnabled(True)
        self.groups.setEnabled(True)
        if self.status.text() == "Detect games first. Grouping uses local images only.":
            if grouping.get("groups"):
                self.status.setText("Classes ready. Review estimated matches (≈), select classes and save the project.")
            elif report.get("games"):
                self.status.setText("Run Detect games to analyze player classes automatically.")

    def toggle_highlight(self, item: QTreeWidgetItem, _column: int = 0) -> None:
        """Click: show this game in yellow on the timeline; click it again to hide (view-only)."""
        key = item.data(0, Qt.ItemDataRole.UserRole)
        if self.highlighted is not None and key == self.highlighted:
            self.games.clearSelection()
            self._show_highlight(None)
        else:
            self._show_highlight(item)

    def _show_highlight(self, item: QTreeWidgetItem | None) -> None:
        span = item.data(1, Qt.ItemDataRole.UserRole) if item is not None else None
        timeline = getattr(self.owner, "timeline", None)
        index = self.track_index
        if item is None or not span or index is None:
            self.highlighted = None
            if timeline is not None and hasattr(timeline, "setGameHighlight"):
                timeline.setGameHighlight(None)
            return
        self.highlighted = item.data(0, Qt.ItemDataRole.UserRole)
        if timeline is not None and hasattr(timeline, "setGameHighlight"):
            timeline.setGameHighlight(index, span[0], span[1])

    def _available(self) -> dict[str, set[str]]:
        # Lightweight snapshot: no RMS arrays, probe, detection, ASR or boundary work.
        tracks = []
        for track in self._tracks():
            item = {key: getattr(track, key, None) for key in (
                "path", "duration", "segment_source_in", "segment_source_out", "cuts_enabled",
                "gameplay_mode", "gameplay_islands", "gameplay_report", "gameplay_grouping",
            )}
            item["keeps"] = [{"start": k.start, "end": k.end} for k in track.keeps]
            item["gameplay_keeps"] = [{"start": k.start, "end": k.end} for k in track.gameplay_keeps]
            tracks.append(item)
        try:
            return exportable_group_games({"tracks": tracks})
        except ValueError:
            return {}  # Unassociated edits must be reviewed rather than guessed.

    def _changed(self) -> None:
        self.owner._mark_project_dirty()
        if hasattr(self.owner, "_refresh_timeline_tracks"):
            self.owner._refresh_timeline_tracks()
        self.refresh()

    def edit_game(self, item: QTreeWidgetItem, column: int) -> None:
        if column != 2 or self.running() or not item.data(0, Qt.ItemDataRole.UserRole):
            return
        choices = [class_label(c) for c in CLASS_COLORS]
        value, ok = QInputDialog.getItem(
            self,
            "Assign class",
            "Player class:",
            choices,
            choices.index(item.text(2)) if item.text(2) in choices else 0,
            False,
        )
        if not ok:
            return
        group = class_id(value)
        try:
            for track in self._tracks():
                assign_group(
                    track.gameplay_grouping,
                    item.data(0, Qt.ItemDataRole.UserRole),
                    group,
                )
            self._changed()
        except ValueError as exc:
            QMessageBox.warning(self, "Hero groups", str(exc))

    def edit_name(self, item: QTreeWidgetItem, column: int) -> None:
        if column != 1 or self.running():
            return
        value, ok = QInputDialog.getText(self, "Group name", "Optional class or group name:", text=item.text(1))
        if ok:
            for track in self._tracks():
                track.gameplay_grouping.setdefault("names", {})[item.data(0, Qt.ItemDataRole.UserRole)] = value.strip()[
                    :100
                ]
                refresh_groups(track.gameplay_grouping)
            self._changed()

    def select_groups(self, item: QTreeWidgetItem, column: int) -> None:
        if self.syncing or self.running():
            return
        items = [self.groups.topLevelItem(i) for i in range(self.groups.topLevelItemCount())]
        selected = [
            row.data(0, Qt.ItemDataRole.UserRole)
            for row in items
            if row is not None and row.checkState(0) == Qt.CheckState.Checked
        ]
        visible = {row.data(0, Qt.ItemDataRole.UserRole) for row in items if row is not None}
        for track in self._tracks():
            hidden = [g for g in track.gameplay_grouping.get("selected", [])
                      if g not in visible and g in track.gameplay_grouping.get("groups", {})]
            track.gameplay_grouping["selected"] = hidden + selected
        self.export.setEnabled(bool(selected))
        self.owner._mark_project_dirty()

    def _snapshot(self) -> dict[str, Any]:
        payload = deepcopy(self.owner._build_project_file_payload())
        payload["tracks"] = [t for t in payload["tracks"] if str(Path(t["path"]).resolve()) == self.source]
        return payload

    def start_export(self) -> None:
        if self.running() or not self.source:
            return
        folder = QFileDialog.getExistingDirectory(self, "Destination for separate group videos")
        if folder:
            options = None
            if self.covers.isChecked():
                options = self.owner.thumbnails.confirm_batch()
                if options is None:
                    return
            self._start(GroupWorker(self._snapshot(), output_dir=folder, source=self.source,
                                    packaging_options=options))

    def _start(self, worker: GroupWorker) -> None:
        if self.owner._export_in_progress() or self.owner._gameplay_running():
            QMessageBox.information(self, "Hero groups", "Wait for the current gameplay/export operation.")
            return
        thumbnails = getattr(self.owner, "thumbnails", None)
        if thumbnails is not None and thumbnails.busy():
            QMessageBox.information(self, "Hero groups", "Wait for the current thumbnail operation.")
            return
        if thumbnails is not None:
            thumbnails.set_batch_busy(True)
            worker.output_ready.connect(thumbnails.register_output, Qt.ConnectionType.QueuedConnection)
        self.worker = worker
        self.work_thread = QThread(self.owner)
        worker.moveToThread(self.work_thread)
        self.work_thread.started.connect(worker.run)
        worker.progress.connect(self._progress, Qt.ConnectionType.QueuedConnection)
        worker.detail.connect(self.status.setText, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(self._done, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self.status.setText, Qt.ConnectionType.QueuedConnection)
        worker.ended.connect(self.work_thread.quit)
        worker.ended.connect(worker.deleteLater)
        self.work_thread.finished.connect(self._finished)
        self.export.setEnabled(False)
        self.games.setEnabled(False)
        self.groups.setEnabled(False)
        self.covers.setEnabled(False)
        self.cancel.setEnabled(True)
        self.cancel.setVisible(True)
        self.work_thread.start()

    @Slot(int)
    def _progress(self, percent: int) -> None:
        self.status.setText(f"Working: {percent}%")

    @Slot(object)
    def _done(self, result: dict[str, Any]) -> None:
        if self.worker is not None and self.worker.token.is_cancelled:
            self.status.setText("Cancelled.")
            return
        if "payload" in result:
            for saved in result["payload"]["tracks"]:
                for track in self._tracks():
                    if str(Path(track.path).resolve()) == str(Path(saved["path"]).resolve()):
                        track.gameplay_grouping = deepcopy(saved["gameplay_grouping"])
            self.owner._mark_project_dirty()
            if hasattr(self.owner, "_refresh_timeline_tracks"):
                self.owner._refresh_timeline_tracks()
            self.status.setText("Classes ready. Review estimated matches (≈), select classes and save the project.")
        else:
            lines = [f"Group {o['group']}: {o['output']}\nProject: {o['project']}"
                     + (f"\nCover: {o.get('packaging_status')} · {o['packaging']}" if o.get("packaging") else "")
                     for o in result.get("outputs", [])]
            lines += result.get("warnings", [])
            self.status.setText("Export complete. Choose each exported video in Thumbnails to generate or review its cover.")
            QMessageBox.information(self, "Group videos", "\n\n".join(lines) or "No groups selected.")

    def _finished(self) -> None:
        thread = self.work_thread
        self.worker = None
        self.work_thread = None
        if thread:
            thread.deleteLater()
        self.cancel.setEnabled(False)
        self.cancel.setVisible(False)
        self.covers.setEnabled(True)
        thumbnails = getattr(self.owner, "thumbnails", None)
        if thumbnails is not None:
            thumbnails.set_batch_busy(False)
        self.refresh()
