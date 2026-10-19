"""Small GUI adapter for source-time gameplay selection overrides."""
from __future__ import annotations

from typing import Any, Callable
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QMessageBox,
)

from analysis.gameplay import selection
from analysis.hero_groups.projects import assign_group
from analysis.hero_groups.labels import class_id, class_label


def group_label(key: str, info: dict[str, Any]) -> str:
    """Keep portrait IDs distinguishable even if multiple groups share a class."""
    name = info.get("name") or class_label(info.get("class_name"))
    return str(name) if class_id(key) else f"Group {key}" + (f" · {name}" if name and name != "Da assegnare" else "")


class GameplayTimelineController:
    def __init__(self, owner: Any) -> None:
        self.owner = owner
        timeline = getattr(owner, "timeline", None)
        if timeline is not None and hasattr(timeline, "gameToggleRequested"):
            timeline.gameToggleRequested.connect(self.toggle)
            timeline.gameResizeRequested.connect(self.resize)

    def _tracks(self, index: int) -> list[Any]:
        return selection.source_tracks(self.owner._tracks, index)

    def _busy(self) -> bool:
        panel = getattr(self.owner, "hero_groups_panel", None)
        return bool((hasattr(self.owner, "_export_in_progress") and self.owner._export_in_progress())
                    or (hasattr(self.owner, "_gameplay_running") and self.owner._gameplay_running())
                    or (panel is not None and panel.running()))

    def _change(self, index: int, mutate: Callable[[dict[str, Any], dict[str, Any]], None]) -> None:
        if self._busy():
            return
        tracks = self._tracks(index)
        timeline = getattr(self.owner, "timeline", None)
        selected = timeline.selectedIsland() if timeline is not None else None
        selected_key = None
        if selected is not None and 0 <= selected[0] < len(self.owner._tracks):
            boxes = selection.timeline_boxes(self.owner._tracks[selected[0]])
            if 0 <= selected[1] < len(boxes):
                selected_key = boxes[selected[1]]["game_id"]
        grouping = selection.initialize(tracks)
        report = tracks[0].gameplay_report
        try:
            mutate(grouping, report)
        except ValueError as exc:
            QMessageBox.warning(self.owner, "Gameplay editing", str(exc))
            return
        if hasattr(self.owner, "_push_undo_state"):
            self.owner._push_undo_state(kind="timeline")
        selection.apply(tracks, grouping)
        affected = [i for i, t in enumerate(self.owner._tracks) if any(t is x for x in tracks)]
        if hasattr(self.owner, "_recompute_gameplay_tracks"):
            self.owner._recompute_gameplay_tracks(affected)
        else:
            self.owner._mark_project_dirty()
            if hasattr(self.owner, "_refresh_timeline_tracks"):
                self.owner._refresh_timeline_tracks()
        if timeline is not None and selected is not None and selected_key is not None:
            boxes = selection.timeline_boxes(self.owner._tracks[selected[0]])
            new_index = next((i for i, box in enumerate(boxes) if box["game_id"] == selected_key), None)
            timeline.setSelectedIsland((selected[0], new_index) if new_index is not None else None, emit=True)
        if hasattr(self.owner, "_refresh_gameplay_panel"):
            self.owner._refresh_gameplay_panel()
        elif getattr(self.owner, "hero_groups_panel", None) is not None:
            self.owner.hero_groups_panel.refresh()
        if hasattr(self.owner, "_refresh_cut_panels"):
            self.owner._refresh_cut_panels()

    def set_included(self, index: int, key: str, included: bool) -> None:
        def update(grouping: dict[str, Any], report: dict[str, Any]) -> None:
            row = next(r for r in selection.catalog(report, grouping) if r["key"] == key)
            if included and not row["complete"]:
                raise ValueError("Set both START and END before including an incomplete game.")
            selection.editor(grouping)["included"][key] = included
        self._change(index, update)

    def toggle(self, index: int, key: str) -> None:
        track = self.owner._tracks[index]
        row = next((r for r in selection.catalog(track.gameplay_report, track.gameplay_grouping) if r["key"] == key), None)
        if row:
            self.set_included(index, key, not row["included"])

    def assign(self, index: int, key: str, group: str) -> None:
        def update(grouping: dict[str, Any], report: dict[str, Any]) -> None:
            if group not in grouping.get("groups", {}):
                raise ValueError("Choose an existing group.")
            assign_group(grouping, key, group)
            for game in selection.editor(grouping)["manual_games"]:
                if game["id"] == key:
                    game["group"] = group
        self._change(index, update)

    def lock(self, index: int, key: str, locked: bool) -> None:
        def update(grouping: dict[str, Any], report: dict[str, Any]) -> None:
            row = next(r for r in grouping["games"] if r["key"] == key)
            if locked:
                grouping.setdefault("overrides", {})[key] = row.get("group")
            else:
                grouping.get("overrides", {}).pop(key, None)
        self._change(index, update)

    def resize(self, index: int, key: str, start: float, end: float) -> None:
        track = self.owner._tracks[index]
        a, b = selection.segment_bounds(track)
        row = next(r for r in selection.catalog(track.gameplay_report, track.gameplay_grouping) if r["key"] == key)
        # A drag on a split fragment changes only the dragged boundary.
        left = row["start"] if abs(start) < .001 and row["start"] is not None and row["start"] < a else a+start
        right = row["end"] if abs(end-(b-a)) < .001 and row["end"] is not None and row["end"] > b else a+end
        self.bounds(index, key, left, right)

    def bounds(self, index: int, key: str, start: float, end: float) -> None:
        duration = self._duration(index)
        def update(grouping: dict[str, Any], report: dict[str, Any]) -> None:
            selection.validate_bounds(report, grouping, key, start, end, duration)
            selection.editor(grouping)["boundaries"][key] = [start, end]
        self._change(index, update)

    def reset(self, index: int, key: str | None = None) -> None:
        def update(grouping: dict[str, Any], report: dict[str, Any]) -> None:
            boundaries = selection.editor(grouping)["boundaries"]
            if key is None:
                boundaries.clear()
            else:
                boundaries.pop(key, None)
        self._change(index, update)

    def _duration(self, index: int) -> float:
        tracks = self._tracks(index)
        return max(float(t.gameplay_report.get("duration") or t.duration) for t in tracks)

    def interval_dialog(self, index: int, key: str | None, local_time: float = 0) -> None:
        track = self.owner._tracks[index]
        grouping = track.gameplay_grouping
        groups = grouping.get("groups", {})
        if not groups:
            QMessageBox.information(self.owner, "Gameplay editing", "Detect games and assign a group first.")
            return
        row = next((r for r in selection.catalog(track.gameplay_report, grouping) if r["key"] == key), None)
        duration = self._duration(index)
        a, _ = selection.segment_bounds(track)
        start = float(row["start"] or 0) if row else min(a+local_time, max(0, duration-.001))
        end = float(row["end"] or duration) if row else min(duration, start+30)
        dialog = QDialog(self.owner)
        dialog.setWindowTitle("Edit game interval" if key else "Create manual interval · UNKNOWN")
        layout = QFormLayout(dialog)
        controls = []
        for title, value in (("START (source seconds)", start), ("END (source seconds)", end)):
            spin = QDoubleSpinBox(dialog)
            spin.setDecimals(3)
            spin.setRange(0, duration)
            spin.setValue(value)
            layout.addRow(title, spin)
            controls.append(spin)
        choices = QComboBox(dialog)
        for group, info in sorted(groups.items()):
            choices.addItem(group_label(group, info), group)
        if row:
            choices.setCurrentIndex(max(0, choices.findData(row["group"])))
        layout.addRow("Existing group", choices)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        layout.addRow(buttons)
        def accept() -> None:
            try:
                selection.validate_bounds(track.gameplay_report, grouping, key or "", controls[0].value(),
                                          controls[1].value(), duration)
            except ValueError as exc:
                QMessageBox.warning(dialog, "Gameplay editing", str(exc))
                return
            dialog.accept()
        buttons.accepted.connect(accept)
        buttons.rejected.connect(dialog.reject)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        left, right, group = controls[0].value(), controls[1].value(), choices.currentData()
        def update(current: dict[str, Any], report: dict[str, Any]) -> None:
            if key:
                selection.validate_bounds(report, current, key, left, right, duration)
                selection.editor(current)["boundaries"][key] = [left, right]
                assign_group(current, key, group)
                for game in selection.editor(current)["manual_games"]:
                    if game["id"] == key:
                        game["group"] = group
            else:
                selection.create_interval(report, current, left, right, duration, group)
        self._change(index, update)

    def delete(self, index: int, key: str) -> None:
        if QMessageBox.question(self.owner, "Delete manual interval", "Delete this manual interval?",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self._change(index, lambda grouping, report: selection.delete_interval(grouping, key))

    def add_menu(self, menu: Any, actions: dict[Any, Any], index: int,
                 key: str | None, local_time: float) -> None:
        def add(target: Any, text: str, fn: Any) -> Any:
            action = target.addAction(text)
            action.setEnabled(not self._busy())
            actions[action] = fn
            return action
        track = self.owner._tracks[index]
        row = next((r for r in selection.catalog(track.gameplay_report, track.gameplay_grouping)
                    if r["key"] == key), None)
        if row:
            add(menu, "Exclude from export" if row["included"] else "Keep / include in export",
                lambda: self.set_included(index, str(key), not row["included"]))
            submenu = menu.addMenu("Assign to class / portrait group")
            for group, info in sorted(track.gameplay_grouping.get("groups", {}).items()):
                action = add(submenu, group_label(group, info), lambda g=group: self.assign(index, str(key), g))
                action.setCheckable(True)
                action.setChecked(group == row["group"])
            action = add(menu, "Lock manual assignment", lambda: self.lock(index, str(key), not row["locked"]))
            action.setCheckable(True)
            action.setChecked(row["locked"])
            add(menu, "Edit START / END…", lambda: self.interval_dialog(index, key))
            if row["manual"]:
                add(menu, "Delete manual interval…", lambda: self.delete(index, str(key)))
        else:
            add(menu, "Create manual game / interval…", lambda: self.interval_dialog(index, None, local_time))
        menu.addSeparator()
