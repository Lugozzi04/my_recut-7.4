"""Compact source controls over the existing TrackState/ProjectSession multi-video editor."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from analysis.cut_engine import Segment
from analysis.hero_groups.labels import class_id, class_label
from analysis.hero_groups.projects import output_key
from core.project_file import source_identity


def output_options(tracks: list[Any]) -> dict[str, str]:
    result = {}
    for track in tracks:
        if track.source_role == "intro":
            continue
        for group in track.gameplay_grouping.get("groups", {}):
            key = output_key({"path": track.path, "source_id": track.source_id,
                              "gameplay_grouping": track.gameplay_grouping}, group)
            result[key] = class_label(key) if class_id(key) else f"{Path(track.path).name} · group {group}"
    return result


class SourcesPanel(QWidget):
    def __init__(self, owner: Any) -> None:
        super().__init__(owner)
        self.owner = owner
        self.syncing = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.sources = QTreeWidget()
        self.sources.setHeaderLabels(["Source", "Role", "Analysis", "Intro for"])
        self.sources.setRootIsDecorated(False)
        self.sources.setMaximumHeight(140)
        self.sources.itemClicked.connect(self.activate)
        layout.addWidget(self.sources)
        row = QHBoxLayout()
        for text, action in (("Add video…", self.add), ("Role / intro…", self.configure),
                             ("↑", lambda: self.move_source(-1)), ("↓", lambda: self.move_source(1))):
            button = QPushButton(text)
            button.clicked.connect(action)
            row.addWidget(button)
        layout.addLayout(row)
        row = QHBoxLayout()
        for text, action in (("Replace intro…", self.replace_intro), ("Remove source…", self.remove)):
            button = QPushButton(text)
            button.clicked.connect(action)
            row.addWidget(button)
        layout.addLayout(row)
        self.refresh()

    def busy(self) -> bool:
        return bool(self.owner._export_in_progress() or self.owner._gameplay_running()
                    or any(t.isRunning() for t in getattr(self.owner, "_analysis_threads", {}).values())
                    or any(t.isRunning() for t in getattr(self.owner, "_ai_threads", {}).values()))

    def index(self) -> int | None:
        item = self.sources.currentItem()
        return int(item.data(0, Qt.ItemDataRole.UserRole)) if item is not None else None

    def refresh(self) -> None:
        self.syncing = True
        try:
            tracks = getattr(self.owner, "_tracks", [])
            names = output_options(tracks)
            active = getattr(self.owner, "_active_track_index", 0)
            self.sources.clear()
            for index, track in enumerate(tracks):
                if not track.path:
                    continue
                status = "Not needed" if track.source_role == "intro" else (
                    f'{len(track.gameplay_report["games"])} games' if track.gameplay_report.get("games")
                    else ("Audio ready · detect games" if track.rms is not None else "Pending"))
                labels = [names.get(t, "All selected outputs" if t == "*" else t) for t in track.intro_targets]
                item = QTreeWidgetItem([Path(track.path).name, track.source_role.title(), status, ", ".join(labels)])
                item.setData(0, Qt.ItemDataRole.UserRole, index)
                item.setToolTip(0, str(track.path))
                self.sources.addTopLevelItem(item)
                if index == active:
                    self.sources.setCurrentItem(item)
        finally:
            self.syncing = False

    def changed(self) -> None:
        self.owner._mark_project_dirty()
        self.owner._refresh_gameplay_panel()
        self.owner._refresh_timeline_tracks(reset_view=False)

    def activate(self, item: Any, column: int = 0) -> None:
        if self.syncing:
            return
        index = int(item.data(0, Qt.ItemDataRole.UserRole))
        self.owner._activate_track(index)
        start = self.owner._clip_timeline_in(index)
        self.owner._seek_to(start)
        timeline = self.owner.timeline
        timeline.view_start = start
        timeline.view_span = max(.001, self.owner._tracks[index].duration)
        timeline._changed()
        self.owner._refresh_gameplay_panel()

    def add(self) -> None:
        if self.busy():
            QMessageBox.information(self, "Sources", "Wait for the current analysis/export to finish.")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Add video")
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("Role for the files being added:"))
        role = QComboBox()
        role.addItem("Gameplay", "gameplay")
        role.addItem("Intro · no detection or transcription", "intro")
        layout.addWidget(role)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        paths, _ = QFileDialog.getOpenFileNames(self, "Add video", "", "Video (*.mp4 *.mov *.mkv *.m4v);;All files (*.*)")
        for filename in paths:
            # Existing importer still creates media, linked clips, playback and persistent caches.
            self.owner._open_path(filename, restore_state={"source_role": role.currentData(),
                                                          "source_id": source_identity({"path": filename})})
        self.changed()

    def configure(self) -> None:
        index = self.index()
        if index is None or self.busy():
            return
        track = self.owner._tracks[index]
        dialog = QDialog(self)
        dialog.setWindowTitle("Source role and intro assignments")
        layout = QVBoxLayout(dialog)
        role = QComboBox()
        role.addItem("Gameplay", "gameplay")
        role.addItem("Intro", "intro")
        role.setCurrentIndex(role.findData(track.source_role))
        layout.addWidget(role)
        choices = QListWidget()
        for key, label in {"*": "All selected outputs", **output_options(self.owner._tracks)}.items():
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, key)
            item.setCheckState(Qt.CheckState.Checked if key in track.intro_targets else Qt.CheckState.Unchecked)
            choices.addItem(item)
        layout.addWidget(QLabel("Intro applies once to these outputs:"))
        layout.addWidget(choices)
        choices.setEnabled(role.currentData() == "intro")
        role.currentIndexChanged.connect(lambda: choices.setEnabled(role.currentData() == "intro"))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.owner._push_undo_state(kind="timeline")
        old_role = track.source_role
        track.source_role = role.currentData()
        track.intro_targets = [choices.item(i).data(Qt.ItemDataRole.UserRole)
                              for i in range(choices.count()) if choices.item(i).checkState() == Qt.CheckState.Checked]
        if old_role != track.source_role and track.source_role == "intro":
            # A role controls analysis/routing, not whether the existing edit is kept.
            track.gameplay_cuts_enabled = False
        elif old_role != track.source_role and track.source_role == "gameplay":
            if track.gameplay_report.get("games"):
                from analysis.gameplay.selection import apply, initialize
                apply([track], initialize([track]))
                self.owner._recompute_gameplay_tracks([index])
            else:
                self.owner._start_analysis_thread(track.path, index)
        self.changed()

    def move_source(self, direction: int) -> None:
        index = self.index()
        if index is None or self.busy() or not 0 <= index+direction < len(self.owner._tracks):
            return
        self.owner._swap_tracks(index, index+direction)
        self.changed()

    def remove(self) -> None:
        index = self.index()
        if index is not None and not self.busy():
            self.owner._remove_track_at_index(index)
            self.changed()

    def replace_intro(self) -> None:
        index = self.index()
        if index is None or self.busy() or self.owner._tracks[index].source_role != "intro":
            return
        filename, _ = QFileDialog.getOpenFileName(self, "Replacement intro", "", "Video (*.mp4 *.mov *.mkv *.m4v)")
        if not filename:
            return
        old = self.owner._tracks[index]
        options = deepcopy(self.owner.project.gameplay_export)
        targets = list(old.intro_targets)
        self.owner._remove_track_at_index(index)  # existing confirmation and cancellation lifecycle
        if any(t is old for t in self.owner._tracks):
            return
        self.owner._open_path(filename, restore_state={"source_role": "intro", "intro_targets": targets})
        self.owner.project.gameplay_export = options
        self.owner._move_track_to_index(len(self.owner._tracks)-1, min(index, len(self.owner._tracks)-1))
        self.changed()
