"""Compact optional Creative Direction controls for the existing Thumbnails page."""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QWidget, QComboBox, QLineEdit, QCheckBox, QPlainTextEdit,
                              QPushButton, QFileDialog, QDialog, QVBoxLayout, QMessageBox)
from ui.widgets.inspector import Section, PropertyRow
from analysis.packaging.direction import CreativeDirection
from analysis.packaging.style import CreatorStyleStore, competitive_profile


class CreativeDirectionWidget(Section):
    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Creative Direction", collapsible=True, expanded=False, parent=parent)
        self.mode = QComboBox()
        self.mode.addItem("Automatic", "automatic")
        self.mode.addItem("Guided", "guided")
        self.title = QLineEdit()
        self.title.setMaxLength(100)
        self.title_lock = QCheckBox("Use exact title")
        self.hook = QLineEdit()
        self.hook.setMaxLength(180)
        self.hook_lock = QCheckBox("Use exact text")
        self.hook_lock.setToolTip("Full generative: the exact text is given to the image model. "
                                  "Reference preserving: it is drawn with local Precision Typography.")
        self.focus = QPlainTextEdit()
        self.focus.setFixedHeight(70)
        self.focus.setPlaceholderText("e.g. Hunter combo, decisive card and my reaction")
        self.typography = QComboBox()
        for label, value in (("Hybrid Auto", "auto"), ("Precision — local exact text", "precision"),
                             ("AI — integrated lettering", "ai")):
            self.typography.addItem(label, value)
        self.strategy = QComboBox()
        self.strategy.addItem("Full generative — AI composes the thumbnail", "full_generative")
        self.strategy.addItem("Reference preserving — local compositing (experimental)", "reference_preserving")
        self.strategy.setToolTip("Full generative: the image model re-stages webcam, board and card into a finished "
                                 "thumbnail with its own lettering. Reference preserving: AI lighting only, real crops "
                                 "and text composited locally.")
        self.profile = QComboBox()
        store = CreatorStyleStore()
        competitive_profile(store)
        for row in store.list():
            data = store.load(row["creator_id"])
            self.profile.addItem(data.get("design_system", {}).get("name") or row.get("display_name") or row["creator_id"], row["creator_id"])
        self.reference_role = QComboBox()
        for label, value in (("Style inspiration", "style"), ("Layout reference", "layout"),
                             ("Visual element to incorporate", "element")):
            self.reference_role.addItem(label, value)
        self.reference = QLineEdit()
        self.reference.setReadOnly(True)
        self.pick = QPushButton("Attach reference image…")
        self.clear = QPushButton("Remove reference")
        self.configure = QPushButton("Edit Design System…")
        self.identity = QPushButton("Set creator identity photos…")
        self.identity.setToolTip("Optional, separate from creative references. Used by Full generative only; authentic mode keeps webcam pixels.")
        for label, widget in (("Mode", self.mode), ("YouTube Title", self.title), ("", self.title_lock),
                              ("Thumbnail Text", self.hook), ("", self.hook_lock), ("Creative Focus", self.focus),
                              ("Typography", self.typography), ("Image strategy", self.strategy), ("Style Profile", self.profile),
                              ("Reference role", self.reference_role), ("Reference", self.reference)):
            self.add(PropertyRow(label, widget))
        for button in (self.pick, self.clear, self.configure, self.identity):
            self.add(button)
        self.pick.clicked.connect(self._pick)
        self.clear.clicked.connect(lambda: self.reference.clear())
        self.configure.clicked.connect(self._configure)
        self.identity.clicked.connect(self._identity)
        for widget in (self.title, self.hook, self.reference):
            widget.textChanged.connect(self._changed)
        self.focus.textChanged.connect(self._changed)
        for widget in (self.mode, self.typography, self.strategy, self.profile, self.reference_role):
            widget.currentIndexChanged.connect(self._changed)
        self.title_lock.toggled.connect(self._changed)
        self.hook_lock.toggled.connect(self._changed)
        self._changed()

    def _changed(self, *_args: Any) -> None:
        guided = self.mode.currentData() == "guided"
        for widget in (self.title, self.title_lock, self.hook, self.hook_lock, self.focus,
                       self.reference, self.reference_role, self.pick, self.clear):
            widget.setEnabled(guided)
        self.changed.emit()

    def value(self) -> dict[str, Any]:
        refs = [{"path": self.reference.text(), "role": str(self.reference_role.currentData())}] if self.reference.text() else []
        return CreativeDirection.from_mapping({
            "mode": self.mode.currentData(), "youtube_title": self.title.text(),
            "title_locked": self.title_lock.isChecked(), "thumbnail_hook": self.hook.text(),
            "hook_locked": self.hook_lock.isChecked(), "creative_focus": self.focus.toPlainText(),
            "reference_images": refs, "style_profile_id": self.profile.currentData() or "lugozzi",
            "typography_mode": self.typography.currentData(),
            "generation_strategy": self.strategy.currentData(),
        }).to_mapping()

    def restore(self, raw: dict[str, Any], *, root: Path | None = None) -> None:
        d = CreativeDirection.from_mapping(raw)
        self.mode.setCurrentIndex(max(0, self.mode.findData(d.mode)))
        self.title.setText(d.youtube_title)
        self.title_lock.setChecked(d.title_locked)
        self.hook.setText(d.thumbnail_hook)
        self.hook_lock.setChecked(d.hook_locked)
        self.focus.setPlainText(d.creative_focus)
        self.typography.setCurrentIndex(max(0, self.typography.findData(d.typography_mode)))
        self.strategy.setCurrentIndex(max(0, self.strategy.findData(d.generation_strategy)))
        self.profile.setCurrentIndex(max(0, self.profile.findData(d.style_profile_id)))
        ref = d.reference_images[0] if d.reference_images else {}
        location = Path(ref["path"]) if ref else None
        if location is not None and root is not None and not location.is_absolute():
            location = root / location
        self.reference.setText(str(location) if location else "")
        self.reference_role.setCurrentIndex(max(0, self.reference_role.findData(ref.get("role", "style"))))

    def _pick(self) -> None:
        name, _ = QFileDialog.getOpenFileName(self, "Creative reference", "", "Images (*.png *.jpg *.jpeg *.webp)")
        if name:
            self.reference.setText(name)

    def _configure(self) -> None:
        store = CreatorStyleStore()
        profile = competitive_profile(store, str(self.profile.currentData() or "lugozzi"))
        dialog = QDialog(self)
        dialog.setWindowTitle("Design System — explicit preferences (JSON)")
        dialog.resize(620, 650)
        layout = QVBoxLayout(dialog)
        editor = QPlainTextEdit(json.dumps(profile["design_system"], ensure_ascii=False, indent=2))
        layout.addWidget(editor)
        save = QPushButton("Save profile")
        layout.addWidget(save)
        def commit() -> None:
            try:
                value = json.loads(editor.toPlainText())
                if not isinstance(value, dict) or not isinstance(value.get("fonts"), list) or not isinstance(value.get("text"), dict):
                    raise ValueError("Profile requires fonts (list) and text (object)")
                store.update(profile["creator_id"], design_system=value)
                self.changed.emit()
                dialog.accept()
            except (ValueError, OSError, RuntimeError) as exc:
                QMessageBox.information(dialog, "Invalid profile", str(exc))
        save.clicked.connect(commit)
        dialog.exec()

    def _identity(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "Creator identity photos (up to 3)", "", "Images (*.png *.jpg *.jpeg *.webp)")
        if not files:
            return
        try:
            CreatorStyleStore().set_identity_references(str(self.profile.currentData()), [Path(f) for f in files])
            QMessageBox.information(self, "Identity references", "Saved separately in the creator profile. No AI call.")
            self.changed.emit()
        except (ValueError, OSError) as exc:
            QMessageBox.information(self, "Identity references", str(exc))
