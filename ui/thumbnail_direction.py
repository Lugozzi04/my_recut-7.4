"""Creative Direction for the Covers page: one idea box (+ images) up front, everything else under More options.

The idea and its images work in every mode: write one line ("NON CRAFTARLA su questa carta, faccia schifata") or
attach the card and press Generate. The advanced fields (exact title/text, references, typography, strategy,
style profile) are the old Guided mode and stay folded away.
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QWidget, QComboBox, QLineEdit, QCheckBox, QPlainTextEdit, QHBoxLayout,
                               QPushButton, QFileDialog, QDialog, QVBoxLayout, QMessageBox)
from ui.widgets.inspector import Hint, Section, PropertyRow
from analysis.packaging.direction import MAX_BRIEF_IMAGES, CreativeDirection
from analysis.packaging.style import CreatorStyleStore, competitive_profile

IDEA_HINT = ("e.g. NON CRAFTARLA! on Lama dell'Impero Nero, my disgusted face on the right, the tier list behind "
             "— or just a word, a card, a mood")


class CreativeDirectionWidget(Section):
    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Creative Direction", collapsible=True, expanded=True, parent=parent)
        # ---- the quick way: an idea and up to three images
        self.idea = QPlainTextEdit()
        self.idea.setFixedHeight(64)
        self.idea.setPlaceholderText(IDEA_HINT)
        self.idea.setToolTip("Your idea for the covers, in your own words. A full description becomes cover C1; a "
                             "hint (a card, a word, a mood) guides all of them. Optional.")
        self.add(PropertyRow("Your idea", self.idea))
        self.images_box = QWidget()
        images_layout = QVBoxLayout(self.images_box)
        images_layout.setContentsMargins(0, 0, 0, 0)
        images_layout.setSpacing(2)
        self.add(self.images_box)
        self.add_image = QPushButton("Add image…")
        self.add_image.setObjectName("LinkButton")
        self.add_image.setToolTip("An image to use in the covers, e.g. the card you want big. Up to three.")
        self.add(self.add_image)
        self._images: list[str] = []
        # ---- More options (the former Guided mode and the run options)
        self.more = QPushButton("More options ▸")
        self.more.setObjectName("LinkButton")
        self.more.setCheckable(True)
        self.add(self.more)
        self.mode = QComboBox()
        self.mode.addItem("Automatic", "automatic")
        self.mode.addItem("Guided", "guided")
        self.mode.setToolTip("Guided: the exact title/text, focus and reference below are used as well.")
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
        self.critique = QCheckBox("Critique and improve the best cover")
        self.critique.setChecked(True)
        self.critique.setToolTip("A reviewer ranks the covers (also at phone size) and the most promising one is "
                                 "generated once more with its fixes. One more agent call and one more image.")
        self.official_cards = QCheckBox("Official card images")
        self.official_cards.setChecked(True)
        self.official_cards.setToolTip("Named cards use the clean official card (HearthstoneJSON, in the video's "
                                       "language) instead of a crop of the stream. Card images © Blizzard.")
        self._advanced: list[QWidget] = []
        for label, widget in (("Mode", self.mode), ("YouTube Title", self.title), ("", self.title_lock),
                              ("Thumbnail Text", self.hook), ("", self.hook_lock), ("Creative Focus", self.focus),
                              ("Typography", self.typography), ("Image strategy", self.strategy), ("Style Profile", self.profile),
                              ("Reference role", self.reference_role), ("Reference", self.reference)):
            row = PropertyRow(label, widget)
            self._advanced.append(row)
            self.add(row)
        for widget in (self.critique, self.official_cards, self.pick, self.clear, self.configure, self.identity):
            self._advanced.append(widget)
            self.add(widget)
        self.more.toggled.connect(self._show_more)
        self._show_more(False)
        self.add_image.clicked.connect(self._pick_images)
        self.pick.clicked.connect(self._pick)
        self.clear.clicked.connect(lambda: self.reference.clear())
        self.configure.clicked.connect(self._configure)
        self.identity.clicked.connect(self._identity)
        for widget in (self.title, self.hook, self.reference):
            widget.textChanged.connect(self._changed)
        self.focus.textChanged.connect(self._changed)
        self.idea.textChanged.connect(self._changed)
        for widget in (self.mode, self.typography, self.strategy, self.profile, self.reference_role):
            widget.currentIndexChanged.connect(self._changed)
        for box in (self.title_lock, self.hook_lock, self.critique, self.official_cards):
            box.toggled.connect(self._changed)
        self._changed()

    def _show_more(self, shown: bool) -> None:
        self.more.setText("More options ▾" if shown else "More options ▸")
        for widget in self._advanced:
            widget.setVisible(shown)

    def _changed(self, *_args: Any) -> None:
        guided = self.mode.currentData() == "guided"
        for widget in (self.title, self.title_lock, self.hook, self.hook_lock, self.focus,
                       self.reference, self.reference_role, self.pick, self.clear):
            widget.setEnabled(guided)
        self.add_image.setEnabled(len(self._images) < MAX_BRIEF_IMAGES)
        self.changed.emit()

    # ---- idea images
    def _render_images(self) -> None:
        layout = self.images_box.layout()
        while layout.count():
            item = layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        for n, path in enumerate(self._images, start=1):
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            label = Hint(f"U{n} · {Path(path).name}")
            label.setToolTip(path)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            remove = QPushButton("×")
            remove.setFixedWidth(28)
            remove.setToolTip("Remove this image")
            remove.clicked.connect(lambda _checked=False, p=path: self.remove_image(p))
            row_layout.addWidget(label, 1)
            row_layout.addWidget(remove)
            layout.addWidget(row)
        self.images_box.setVisible(bool(self._images))

    def add_images(self, paths: list[str]) -> None:
        for path in paths:
            if path and path not in self._images and len(self._images) < MAX_BRIEF_IMAGES:
                self._images.append(path)
        self._render_images()
        self._changed()

    def remove_image(self, path: str) -> None:
        self._images = [p for p in self._images if p != path]
        self._render_images()
        self._changed()

    def _pick_images(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "Images for the covers (e.g. the card)", "",
                                                "Images (*.png *.jpg *.jpeg *.webp)")
        self.add_images(files)

    # ---- value
    def value(self) -> dict[str, Any]:
        refs = [{"path": self.reference.text(), "role": str(self.reference_role.currentData())}] if self.reference.text() else []
        return CreativeDirection.from_mapping({
            "mode": self.mode.currentData(), "youtube_title": self.title.text(),
            "title_locked": self.title_lock.isChecked(), "thumbnail_hook": self.hook.text(),
            "hook_locked": self.hook_lock.isChecked(), "creative_focus": self.focus.toPlainText(),
            "reference_images": refs, "style_profile_id": self.profile.currentData() or "lugozzi",
            "typography_mode": self.typography.currentData(),
            "generation_strategy": self.strategy.currentData(),
            "brief": self.idea.toPlainText().strip()[:1500], "brief_images": [{"path": p} for p in self._images],
        }).to_mapping()

    def run_options(self) -> dict[str, bool]:
        """PackagingSettings switches that are not part of the saved direction."""
        return {"critique": self.critique.isChecked(), "official_cards": self.official_cards.isChecked()}

    def restore(self, raw: dict[str, Any], *, root: Path | None = None) -> None:
        d = CreativeDirection.from_mapping(raw)
        self.mode.setCurrentIndex(max(0, self.mode.findData(d.mode)))
        self.title.setText(d.youtube_title)
        self.title_lock.setChecked(d.title_locked)
        self.hook.setText(d.thumbnail_hook)
        self.hook_lock.setChecked(d.hook_locked)
        self.focus.setPlainText(d.creative_focus)
        self.idea.setPlainText(d.brief)
        self.typography.setCurrentIndex(max(0, self.typography.findData(d.typography_mode)))
        self.strategy.setCurrentIndex(max(0, self.strategy.findData(d.generation_strategy)))
        self.profile.setCurrentIndex(max(0, self.profile.findData(d.style_profile_id)))
        ref = d.reference_images[0] if d.reference_images else {}
        location = Path(ref["path"]) if ref else None
        if location is not None and root is not None and not location.is_absolute():
            location = root / location
        self.reference.setText(str(location) if location else "")
        self.reference_role.setCurrentIndex(max(0, self.reference_role.findData(ref.get("role", "style"))))
        images = []
        for item in d.brief_images:
            path = Path(item["path"])
            if root is not None and not path.is_absolute():
                path = root / path
            images.append(str(path))
        self._images = images[:MAX_BRIEF_IMAGES]
        self._render_images()
        self._changed()

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

