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
    QComboBox,
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

from utils.timefmt import fmt_hms
from analysis.gameplay import selection
from .gameplay_timeline import GameplayTimelineController, group_label
from analysis.audio_service import AnalysisCancellation
from analysis.hero_groups.projects import (analyze_project, assign_group, prepare_outputs, export_outputs,
    exportable_group_games, selected_project, selected_outputs, prepare_selected_outputs)
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
        packaging_options: dict[str, Any] | None = None, selected: bool = False,
        separate: bool = True, output_path: str = "", wins_only: bool = True
    ) -> None:
        super().__init__()
        self.payload = payload
        self.class_refs = class_refs
        self.output_dir = output_dir
        self.source = source
        self.packaging_options = packaging_options
        self.selected = selected
        self.separate = separate
        self.output_path = output_path
        self.wins_only = wins_only
        self.packaging_token = AnalysisCancellation()
        self.token: Any = ExportCancellation() if output_dir else AnalysisCancellation()

    def cancel(self) -> None:
        self.token.cancel()
        self.packaging_token.cancel()

    @Slot()
    def run(self) -> None:
        try:
            if self.output_dir:
                if self.selected:
                    plans, warnings = prepare_selected_outputs(self.payload, Path(self.output_dir),
                        separate=self.separate, output_path=Path(self.output_path) if self.output_path else None,
                        wins_only=self.wins_only)
                else:
                    available = exportable_group_games(self.payload, source=self.source or None)
                    selected = sorted({g for t in self.payload["tracks"]
                        for g in t.get("gameplay_grouping", {}).get("selected", []) if g in available})
                    plans, warnings = prepare_outputs(self.payload, Path(self.output_dir), selected, source=self.source or None)
                outputs = export_outputs(plans, cancellation=self.token, on_progress=self.progress.emit,
                                         on_completed=self.output_ready.emit, on_detail=self.detail.emit)
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
        self.group_source = ""
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
        self.games.setHeaderLabels(["Game / portrait", "Result", "Class / group", "Name", "Similarity / samples", "Include"])
        self.games.setRootIsDecorated(False)
        self.games.setIconSize(QSize(32, 40))
        self.games.setMaximumHeight(230)
        self.games.setToolTip("Double-click Class to correct it. ≈ means estimated; similarity is not a probability.")
        layout.addWidget(self.games)
        self.outputs = QWidget(window)
        output_layout = QVBoxLayout(self.outputs)
        output_layout.setContentsMargins(0, 0, 0, 0)
        self.mode = QComboBox()
        self.mode.addItem("Single video — selected classes", "selected_single")
        self.mode.addItem("Videos by class — selected classes", "by_class")
        self.mode.addItem("Single video — current edit (unfiltered)", "normal")
        output_layout.addWidget(self.mode)
        self.wins_only = QCheckBox("Complete WINs only")
        self.wins_only.setChecked(True)
        output_layout.addWidget(self.wins_only)
        self.plan_summary = QLabel("")
        self.plan_summary.setWordWrap(True)
        output_layout.addWidget(self.plan_summary)
        self.plan_button = QPushButton("Show export plan…")
        self.plan_button.clicked.connect(self.show_plan)
        output_layout.addWidget(self.plan_button)
        self.mode.currentIndexChanged.connect(self.select_mode)
        self.wins_only.toggled.connect(self.select_mode)
        self.groups = QTreeWidget()
        self.groups.setHeaderLabels(["Export / group", "Name (optional)", "Games kept", "WIN", "Duration"])
        self.groups.setRootIsDecorated(False)
        self.groups.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.groups.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.groups.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.groups.header().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.groups.header().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.groups.setIconSize(QSize(32, 40))
        self.groups.setMaximumHeight(140)
        self.groups.setToolTip("Check groups to export; double-click Name to rename.")
        output_layout.addWidget(self.groups)
        self.selection_scope = QLabel("Class checkboxes apply to the active source; the plan includes all Gameplay sources.")
        self.selection_scope.setWordWrap(True)
        output_layout.addWidget(self.selection_scope)
        self.covers = QCheckBox("Generate a cover for each video")
        self.covers.setToolTip("Uses the provider, language and thumbnail engine in Thumbnails. "
                               "Each video uses only its own kept games; provider calls are optional.")
        output_layout.addWidget(self.covers)
        self.export = QPushButton("Export with this selection…")
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
        self.games.itemChanged.connect(self.select_game)
        self.groups.itemDoubleClicked.connect(self.edit_name)
        self.groups.itemChanged.connect(self.select_groups)
        self.refresh()

    def running(self) -> bool:
        return self.work_thread is not None and self.work_thread.isRunning()

    def cancel_work(self) -> None:
        if self.worker:
            self.worker.cancel()
            if self.worker.output_dir:
                self.owner.export_status.setText("Cancelling export; waiting for FFmpeg to stop…")

    def _tracks(self) -> list[Any]:
        return [
            t
            for t in getattr(self.owner, "_tracks", [])
            if getattr(t, "path", None) and str(Path(t.path).resolve()) == self.source
        ]

    def _group_tracks(self) -> list[Any]:
        """Export class choices keep the last Gameplay source when viewing an Intro."""
        return [t for t in getattr(self.owner, "_tracks", [])
                if getattr(t, "path", None) and getattr(t, "source_role", "gameplay") != "intro"
                and str(Path(t.path).resolve()) == self.group_source]

    def refresh(self) -> None:
        if self.running():
            return
        tracks = getattr(self.owner, "_tracks", [])
        index = getattr(self.owner, "_active_track_index", 0) or 0
        active = tracks[index] if tracks and 0 <= index < len(tracks) else None
        candidates = [active] if active and getattr(active, "path", None) else []
        if not candidates:
            candidates = [t for t in tracks if getattr(t, "gameplay_report", None)]
        track = candidates[0] if candidates else None
        self.track_index = next((i for i, t in enumerate(getattr(self.owner, "_tracks", []) or []) if t is track), None)
        self.source = str(Path(track.path).resolve()) if track else ""
        gameplay_sources = [t for t in tracks if getattr(t, "path", None)
                            and getattr(t, "source_role", "gameplay") != "intro"]
        group_track = track if any(track is t for t in gameplay_sources) else next(
            (t for t in gameplay_sources if str(Path(t.path).resolve()) == self.group_source),
            next((t for t in gameplay_sources if getattr(t, "gameplay_grouping", {}).get("groups")), None))
        self.group_source = str(Path(group_track.path).resolve()) if group_track else ""
        export_grouping = getattr(group_track, "gameplay_grouping", {}) if group_track else {}
        export_rows = export_grouping.get("games", [])
        self.selection_scope.setText(f"Class checkboxes: {Path(group_track.path).name if group_track else 'no Gameplay source'}. "
            "Viewing an Intro does not change these choices. The plan includes selections from all Gameplay sources; "
            "change Gameplay source in Analysis → Sources.")
        report = getattr(track, "gameplay_report", {}) if track else {}
        grouping = getattr(track, "gameplay_grouping", {}) if track else {}
        if track is not None and getattr(track, "source_role", "gameplay") == "intro":
            report, grouping = {}, {}
        self.syncing = True
        options = getattr(getattr(self.owner, "project", None), "gameplay_export", {})
        self.mode.setCurrentIndex(max(0, self.mode.findData(options.get("mode", "selected_single"))))
        self.wins_only.setChecked(bool(options.get("wins_only", True)))
        self.syncing = False
        rows = grouping.get("games") or [{"game": g} for g in report.get("games", [])]
        effective_grouping = selection.initialize(self._tracks()) if self._tracks() else grouping
        effective = {r["key"]: r for r in selection.catalog(report, effective_grouping)}
        available = self._available()
        group_durations = self._group_durations()
        has_games = any(getattr(t, "gameplay_report", {}) or getattr(t, "source_role", "") == "intro" for t in tracks)
        show_selection = bool(available) or (has_games and hasattr(self.owner, "_build_project_file_payload"))
        self.outputs.setVisible(show_selection)
        section = getattr(self.owner, "section_gameplay_exports", None)
        if section is not None:
            section.setVisible(show_selection)
        display_games = [{**row["game"], **{side: effective.get(row.get("key") or selection.game_key(row["game"]), {}).get(side, row["game"].get(side)) for side in ("start", "end")}} for row in rows]
        spans = game_spans(display_games, float(report.get("duration") or getattr(track, "duration", 0) or 0))
        self.syncing = True
        try:
            self.games.clear()
            for row, span in zip(rows, spans):
                game = row["game"]
                key = row.get("key") or selection.game_key(game)
                effective_row = effective.get(key, {})
                group = row.get("group")
                name = grouping.get("groups", {}).get(group, {}).get("name", "")
                manual = row.get("key") in grouping.get("overrides", {})
                item = QTreeWidgetItem(
                    [
                        str(game["index"]),
                        str(game["result"]),
                        group_label(group, grouping.get("groups", {}).get(group, {}))
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
                item.setData(0, Qt.ItemDataRole.UserRole, key)
                item.setCheckState(5, Qt.CheckState.Checked if effective_row.get("included") else Qt.CheckState.Unchecked)
                item.setToolTip(5, "Individual inclusion; export class selection and its Complete WINs only filter still apply. Incomplete games need START and END first.")
                if span and track is not None:
                    offset, limit = selection.segment_bounds(track)
                    span = (max(0.0, span[0]-offset), min(limit-offset, span[1]-offset))
                    if span[1] <= span[0]:
                        span = None
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
            for key, group in sorted(export_grouping.get("groups", {}).items()):
                if key not in available:
                    continue
                item = QTreeWidgetItem(
                    [
                        group_label(key, group),
                        group.get("name") or "",
                        str(len(available[key])),
                        str(sum(1 for r in export_rows if r.get("key") in available[key] and str(r["game"].get("result", "")).upper() == "WIN")),
                        fmt_hms(group_durations.get(key, 0.0)),
                    ]
                )
                item.setData(0, Qt.ItemDataRole.UserRole, key)
                item.setToolTip(4, "Gameplay kept in this source after cuts and game exclusions; Intro is shown separately in the output summary.")
                hero_class = class_id(group.get("class_name"))
                if hero_class:
                    item.setForeground(0, QColor(CLASS_COLORS[hero_class]))
                item.setCheckState(
                    0, Qt.CheckState.Checked if key in export_grouping.get("selected", []) else Qt.CheckState.Unchecked
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
        self.export.setEnabled(any(g in available for g in export_grouping.get("selected", [])))
        self.refresh_plan()
        self.games.setEnabled(True)
        self.groups.setEnabled(True)
        if self.status.text() == "Detect games first. Grouping uses local images only.":
            if grouping.get("groups"):
                self.status.setText("Classes ready. Review estimated matches (≈), select classes and save the project.")
            elif report.get("games"):
                self.status.setText("Run Detect games to analyze player classes automatically.")

    def toggle_highlight(self, item: QTreeWidgetItem, _column: int = 0) -> None:
        """Click: show this game in yellow on the timeline; click it again to hide (view-only)."""
        if _column == 5:
            return
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

    def select_game(self, item: QTreeWidgetItem, column: int) -> None:
        if self.syncing or self.running() or column != 5 or self.track_index is None:
            return
        controller = getattr(self.owner, "gameplay_actions", None)
        if controller is None:
            controller = GameplayTimelineController(self.owner)
        controller.set_included(self.track_index, str(item.data(0, Qt.ItemDataRole.UserRole)),
                                item.checkState(5) == Qt.CheckState.Checked)
        self.refresh()

    def _group_payload(self) -> dict[str, Any]:
        # Lightweight snapshot: no RMS arrays, probe, detection, ASR or boundary work.
        tracks = []
        for track in self._group_tracks():
            item = {key: getattr(track, key, None) for key in (
                "path", "duration", "segment_source_in", "segment_source_out", "cuts_enabled",
                "gameplay_mode", "gameplay_islands", "gameplay_report", "gameplay_grouping",
            )}
            item["keeps"] = [{"start": k.start, "end": k.end} for k in track.keeps]
            item["gameplay_keeps"] = [{"start": k.start, "end": k.end} for k in track.gameplay_keeps]
            tracks.append(item)
        return {"tracks": tracks}

    def _available(self) -> dict[str, set[str]]:
        tracks = self._group_payload()["tracks"]
        try:
            available = exportable_group_games({"tracks": tracks})
            if self.wins_only.isChecked():
                wins = {r["key"] for t in tracks for r in (t.get("gameplay_grouping") or {}).get("games", [])
                        if str(r["game"].get("result", "")).upper() == "WIN"}
                available = {g: keys & wins for g, keys in available.items() if keys & wins}
            return available
        except ValueError:
            return {}  # Unassociated edits must be reviewed rather than guessed.

    def _group_durations(self) -> dict[str, float]:
        # Show available duration even for unchecked groups, using the renderer's
        # exact projection rather than raw START/END or a second cut calculation.
        payload = self._group_payload()
        for track in payload["tracks"]:
            grouping = track.get("gameplay_grouping") or {}
            track["gameplay_grouping"] = {**grouping, "selected": list(grouping.get("groups", {}))}
        try:
            projected = selected_project(payload, wins_only=self.wins_only.isChecked())
        except ValueError:
            return {}
        durations: dict[str, float] = {}
        for interval in projected["export_selection"]["intervals"]:
            group = interval["group"]
            durations[group] = durations.get(group, 0.0) + interval["end"] - interval["start"]
        return durations

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
            for track in self._group_tracks():
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
        for track in self._group_tracks():
            hidden = [g for g in track.gameplay_grouping.get("selected", [])
                      if g not in visible and g in track.gameplay_grouping.get("groups", {})]
            track.gameplay_grouping["selected"] = hidden + selected
        self.export.setEnabled(bool(selected))
        self.owner._mark_project_dirty()
        if hasattr(self.owner, "_refresh_timeline_tracks"):
            self.owner._refresh_timeline_tracks(reset_view=False)
        self.refresh_plan()

    def _snapshot(self, *, all_sources: bool = False) -> dict[str, Any]:
        payload = deepcopy(self.owner._build_project_file_payload())
        if not all_sources:
            payload["tracks"] = [t for t in payload["tracks"] if str(Path(t["path"]).resolve()) == self.source]
        return payload

    def timeline_boxes(self, track: Any) -> list[dict[str, Any]]:
        boxes = selection.timeline_boxes(track)
        if not self.filter_enabled():
            return boxes
        rows = {r["key"]: r for r in selection.catalog(track.gameplay_report, track.gameplay_grouping)}
        selected = set(track.gameplay_grouping.get("selected", []))
        for box in boxes:
            group = rows[box["game_id"]]["group"]
            if group not in selected:
                box["export_filtered"] = "class deselected"
            elif self.wins_only.isChecked() and box.get("result") != "WIN":
                box["export_filtered"] = "WIN-only"
        return boxes

    def filter_enabled(self) -> bool:
        options = getattr(getattr(self.owner, "project", None), "gameplay_export", {})
        intent = any(getattr(t, "gameplay_report", {}) or getattr(t, "source_role", "") == "intro"
                     for t in getattr(self.owner, "_tracks", []))
        return self.mode.currentData() != "normal" and (intent or options.get("mode") in {"selected_single", "by_class"})

    def select_mode(self, *_args: Any) -> None:
        if self.syncing:
            return
        project = getattr(self.owner, "project", None)
        if project is not None:
            project.gameplay_export = {"mode": self.mode.currentData(), "wins_only": self.wins_only.isChecked()}
        self.owner._mark_project_dirty()
        self.refresh()
        if hasattr(self.owner, "_refresh_timeline_tracks"):
            self.owner._refresh_timeline_tracks(reset_view=False)

    def preview_projects(self) -> list[tuple[str, dict[str, Any]]]:
        payload = self._snapshot(all_sources=True)
        wins = self.wins_only.isChecked()
        if self.mode.currentData() == "by_class":
            return [(key, selected_project(payload, output=key, wins_only=wins))
                    for key in sorted(selected_outputs(payload, wins_only=wins))]
        return [("selected", selected_project(payload, wins_only=wins))]

    def refresh_plan(self) -> None:
        self.preview_summary = None
        if not hasattr(self.owner, "_build_project_file_payload"):
            return
        if not self.filter_enabled():
            self.plan_summary.setText("Current edit: class checkboxes do not filter this explicitly unfiltered mode.")
            return
        try:
            previews = self.preview_projects()
            summaries = [p["export_selection"] for _, p in previews]
            seconds = sum(r["end"]-r["start"] for p in summaries for r in p["intervals"])
            games, wins = sum(p["games"] for p in summaries), sum(p["wins"] for p in summaries)
            self.preview_summary = {"seconds": seconds, "games": games, "wins": wins, "outputs": len(previews)}
            output_durations = []
            for _, projected in previews:
                intervals = projected["export_selection"]["intervals"]
                intro_s = sum(r["end"]-r["start"] for r in intervals if r["role"] == "intro")
                gameplay_s = sum(r["end"]-r["start"] for r in intervals if r["role"] == "gameplay")
                output_durations.append((gameplay_s, intro_s))
            labels = [class_label(key) if class_id(key) else ("Selected classes combined" if key == "selected" else "Portrait group")
                      for key, _ in previews]
            self.plan_summary.setText(f'{len(previews)} video(s) · {games} games · {wins} WIN · {fmt_hms(seconds)} total\n'
                + "\n".join(f"{label}: gameplay {fmt_hms(gameplay_s)} + Intro {fmt_hms(intro_s)} = {fmt_hms(gameplay_s+intro_s)}"
                            for label, (gameplay_s, intro_s) in zip(labels, output_durations))
                + "\nSource order, then chronological games. Multiple sources require normalized rendering at the selected quality.")
            self.export.setEnabled(True)
        except ValueError as exc:
            self.plan_summary.setText(str(exc))
            self.export.setEnabled(True)
        if hasattr(self.owner, "_refresh_export_summary"):
            self.owner._refresh_export_summary()
        if hasattr(self.owner, "_sync_export_settings_ui"):
            self.owner._sync_export_settings_ui()
        thumbnails = getattr(self.owner, "thumbnails", None)
        if thumbnails is not None:  # the Thumbnails page lists the same output videos
            thumbnails._update_hint()

    def show_plan(self) -> None:
        try:
            lines = []
            for key, projected in self.preview_projects():
                lines.append("OUTPUT: " + (class_label(key) if class_id(key) else key))
                names = {t["source_id"]: Path(t["path"]).name for t in projected["tracks"]}
                cursor = 0.0
                for record in projected["export_selection"]["intervals"]:
                    duration = record["end"]-record["start"]
                    lines.append(f'{names[record["source_id"]]} · {record["role"]} · {record["group"] or "intro"} · '
                                 f'{record["result"]} · source {record["start"]:.3f} → {record["end"]:.3f} · '
                                 f'output {cursor:.3f} → {cursor+duration:.3f}')
                    cursor += duration
            QMessageBox.information(self, "Actual export intervals", "\n".join(lines))
        except ValueError as exc:
            QMessageBox.warning(self, "Export selection", str(exc))

    def start_export(self) -> None:
        if self.running():
            return
        if self.mode.currentData() == "normal":
            self.owner.export_mp4()
            return
        try:
            payload = self._snapshot(all_sources=True)
            from automation.exporter import ProjectExportPlanner
            if ProjectExportPlanner._settings_from_payload(payload).output_mode != "single":
                raise ValueError("Filtered gameplay exports use Single video delivery. Choose Single video in Output, or the explicitly unfiltered edit for other delivery modes.")
            selected_project(payload, wins_only=self.wins_only.isChecked())
        except ValueError as exc:
            QMessageBox.warning(self, "Export selection", str(exc))
            return
        separate = self.mode.currentData() == "by_class"
        target = ""
        if separate:
            folder = QFileDialog.getExistingDirectory(self, "Destination for class videos")
        else:
            from automation.exporter import ProjectExportPlanner
            extension = ProjectExportPlanner._settings_from_payload(payload).output_extension()
            projected = selected_project(payload, wins_only=self.wins_only.isChecked())
            default_name = projected.get("hero_group_output", {}).get("name") or "Selected_games"
            target, _ = QFileDialog.getSaveFileName(self, "Export selected games", default_name.replace(" ", "_")+extension)
            if target and not target.lower().endswith(extension):
                target += extension
            folder = str(Path(target).parent) if target else ""
        if not folder:
            return
        if target and Path(target).exists():
            QMessageBox.warning(self, "Export selection", "Output already exists. Choose a new filename.")
            return
        options = None
        if self.covers.isChecked():
            options = self.owner.thumbnails.confirm_batch()
            if options is None:
                return
        self._start(GroupWorker(payload, output_dir=folder, selected=True, separate=separate,
            output_path=target, wins_only=self.wins_only.isChecked(), packaging_options=options))

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
        worker.detail.connect(self._detail, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(self._done, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._failed, Qt.ConnectionType.QueuedConnection)
        worker.ended.connect(self.work_thread.quit)
        worker.ended.connect(worker.deleteLater)
        self.work_thread.finished.connect(self._finished)
        self.export.setEnabled(False)
        self.games.setEnabled(False)
        self.groups.setEnabled(False)
        self.covers.setEnabled(False)
        self.cancel.setEnabled(True)
        self.cancel.setVisible(True)
        if worker.output_dir:
            self.mode.setEnabled(False)
            self.wins_only.setEnabled(False)
            for control in getattr(self.owner, "_export_setting_controls", lambda: ())():
                control.setEnabled(False)
            self.owner.btn_export.setText("Cancel export")
            self.owner.export_progress.setValue(0)
            self.owner.export_progress.setVisible(True)
            self.owner.export_details.clear()
            self.owner.export_status.setText("Preparing selected output(s)…")
        self.work_thread.start()

    @Slot(str)
    def _detail(self, message: str) -> None:
        self.status.setText(message)
        if self.worker is not None and self.worker.output_dir:
            self.owner.export_status.setText(message[:220])
            self.owner.export_details.appendPlainText(message)

    @Slot(str)
    def _failed(self, message: str) -> None:
        cancelled = self.worker is not None and self.worker.token.is_cancelled
        self._detail("Export cancelled." if cancelled else "Export failed: " + message)
        if not cancelled:
            QMessageBox.warning(self, "Export failed", message)

    @Slot(int)
    def _progress(self, percent: int) -> None:
        self.status.setText(f"Working: {percent}%")
        if self.worker is not None and self.worker.output_dir:
            self.owner.export_progress.setValue(percent)

    @Slot(object)
    def _done(self, result: dict[str, Any]) -> None:
        if self.worker is not None and self.worker.token.is_cancelled:
            self.status.setText("Cancelled.")
            return
        if "payload" in result:
            for saved in result["payload"]["tracks"]:
                for track in self._tracks():
                    if str(Path(track.path).resolve()) == str(Path(saved["path"]).resolve()):
                        track.gameplay_grouping = selection.preserve_editor(track.gameplay_grouping, saved["gameplay_grouping"])
            self.owner._mark_project_dirty()
            if hasattr(self.owner, "_refresh_timeline_tracks"):
                self.owner._refresh_timeline_tracks()
            self.status.setText("Classes ready. Review estimated matches (≈), select classes and save the project.")
        else:
            lines = [f"Group {o['group']}: {o['output']}\nProject: {o['project']}"
                     + (f"\nBackup: {o['project_backup']}" if o.get("project_backup") else "")
                     + (f"\nTimings: {o['timestamps']}" if o.get("timestamps") else "")
                     + (f"\nCover: {o.get('packaging_status')} · {o['packaging']}" if o.get("packaging") else "")
                     for o in result.get("outputs", [])]
            lines += result.get("warnings", [])
            self._detail("Export complete. Choose each exported video in Thumbnails to generate or review its cover.")
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
        self.mode.setEnabled(True)
        self.wins_only.setEnabled(True)
        for control in getattr(self.owner, "_export_setting_controls", lambda: ())():
            control.setEnabled(True)
        if hasattr(self.owner, "export_progress"):
            self.owner.export_progress.setVisible(False)
        if hasattr(self.owner, "_sync_export_settings_ui"):
            self.owner._sync_export_settings_ui()
        thumbnails = getattr(self.owner, "thumbnails", None)
        if thumbnails is not None:
            thumbnails.set_batch_busy(False)
        self.refresh()
