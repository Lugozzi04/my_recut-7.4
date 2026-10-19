"""Clips page of the inspector: find moments in the edit, tick/trim/preview them, export the chosen clips.

Same engine as the CLI (`analysis.clips`): ClipSession finds or builds the local analysis, proposes candidates
and renders them from the original sources. Long work runs on QThreads; results come back as plain data.

Source of a run:
  * Current project: a snapshot of the edit is written into the clips folder (final keeps, no re-analysis of
    an exported file).
  * Video: an export registered in the project (its saved edit -> clips from the original VOD) or any file.

"Twitch clip -> Short": a Twitch clip link (or a downloaded clip) becomes one YouTube Short (analysis.clips.short).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Qt, QThread, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFrame, QHBoxLayout,
                               QLabel, QLineEdit, QMessageBox, QProgressBar, QPushButton, QSpinBox, QVBoxLayout,
                               QWidget)

from ui.widgets.inspector import Hint, PropertyRow, Section

PRIVACY_NOTE = "Clips are analysed and rendered locally: nothing is sent to any provider."
AI_NOTE = ("AI ranking sends transcript excerpts of the best moments (no video) to the provider of the selected "
           "CLI; the video is processed locally.")


def fmt(t: float) -> str:
    t = max(0.0, t)
    return f"{int(t // 3600)}:{int(t // 60 % 60):02d}:{t % 60:04.1f}" if t >= 3600 else f"{int(t // 60)}:{t % 60:04.1f}"


# ---- workers -----------------------------------------------------------------------------------------------
class FindWorker(QObject):
    progress = Signal(str)
    finished = Signal(object)  # (session, clips)
    failed = Signal(str)

    def __init__(self, session_args: dict[str, Any], count: int, min_s: float, max_s: float, refresh: bool,
                 ai: str | None = None) -> None:
        super().__init__()
        from analysis.audio_service import AnalysisCancellation
        self.session_args, self.count, self.min_s, self.max_s, self.refresh = session_args, count, min_s, max_s, refresh
        self.ai = ai
        self.cancellation = AnalysisCancellation()

    def cancel(self) -> None:
        self.cancellation.cancel()

    @Slot()
    def run(self) -> None:
        from analysis.clips.service import ClipSession
        try:
            session = ClipSession(**self.session_args, cancellation=self.cancellation,
                                  progress=lambda stage, message: self.progress.emit(message))
            clips = session.candidates(count=self.count, min_s=self.min_s, max_s=self.max_s, refresh=self.refresh,
                                       ai=self.ai)
            self.finished.emit((session, clips))
        except Exception as exc:  # noqa: BLE001 - shown to the user
            self.failed.emit("Cancelled." if self.cancellation.is_cancelled else str(exc))


class RenderWorker(QObject):
    progress = Signal(str)
    finished = Signal(object)  # list of results
    failed = Signal(str)

    def __init__(self, session: Any, clips: list[Any], loudnorm: bool, formats: tuple[str, ...] = ("horizontal",),
                 subtitles: bool = True) -> None:
        super().__init__()
        from analysis.audio_service import AnalysisCancellation
        self.session, self.clips, self.loudnorm = session, clips, loudnorm
        self.formats, self.subtitles = formats, subtitles
        self.cancellation = AnalysisCancellation()
        session.cancellation = self.cancellation

    def cancel(self) -> None:
        self.cancellation.cancel()

    @Slot()
    def run(self) -> None:
        try:
            results = self.session.render(
                self.clips, loudnorm=self.loudnorm, formats=self.formats, subtitles=self.subtitles,
                on_clip=lambda clip, value: self.progress.emit(f"Rendering {clip.id} · {int(value * 100)}% of all"))
            self.finished.emit(results)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            self.failed.emit("Cancelled." if self.cancellation.is_cancelled else str(exc))


class ShortWorker(QObject):
    progress = Signal(str)
    finished = Signal(object)  # make_short summary
    failed = Signal(str)

    def __init__(self, source: str, cold_open: bool, zoom: bool, subtitles: bool, loudnorm: bool) -> None:
        super().__init__()
        from analysis.audio_service import AnalysisCancellation
        self.source, self.cold_open, self.zoom = source, cold_open, zoom
        self.subtitles, self.loudnorm = subtitles, loudnorm
        self.cancellation = AnalysisCancellation()

    def cancel(self) -> None:
        self.cancellation.cancel()

    @Slot()
    def run(self) -> None:
        from analysis.clips.short import make_short
        try:
            self.finished.emit(make_short(
                self.source, cold_open=self.cold_open, zoom=self.zoom, subtitles=self.subtitles,
                loudnorm=self.loudnorm, cancellation=self.cancellation,
                progress=lambda stage, message: self.progress.emit(
                    f"Short · rendering {message}" if stage == "render" and message.endswith("%")
                    else f"Short · {message}")))
        except Exception as exc:  # noqa: BLE001 - shown to the user
            self.failed.emit("Cancelled." if self.cancellation.is_cancelled else str(exc))


def _start(owner: QObject, worker: QObject, on_done: Any, on_fail: Any, on_progress: Any) -> QThread:
    thread = QThread(owner)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)  # type: ignore[attr-defined]
    worker.finished.connect(on_done, Qt.QueuedConnection)  # type: ignore[attr-defined]
    worker.failed.connect(on_fail, Qt.QueuedConnection)  # type: ignore[attr-defined]
    worker.progress.connect(on_progress, Qt.QueuedConnection)  # type: ignore[attr-defined]
    worker.finished.connect(thread.quit)  # type: ignore[attr-defined]
    worker.failed.connect(thread.quit)  # type: ignore[attr-defined]
    thread.finished.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)
    thread.start()
    return thread


# ---- widgets -----------------------------------------------------------------------------------------------
class ClipRow(QFrame):
    """One proposed moment: tick, preview frame, times, text; Start/End trims and a preview button."""

    changed = Signal(str)  # clip id
    preview = Signal(str)

    def __init__(self, clip: Any, frame: Path | None, total: float) -> None:
        super().__init__()
        self.clip_id = clip.id
        self.setObjectName("CardInner")
        self.setAttribute(Qt.WA_StyledBackground, True)
        body = QVBoxLayout(self)  # stacked: the inspector is narrow (~340 px)
        body.setContentsMargins(8, 8, 8, 8)
        body.setSpacing(4)
        head = QHBoxLayout()
        self.image = QLabel()
        self.image.setFixedSize(96, 54)
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if frame is not None and frame.is_file():
            self.image.setPixmap(QPixmap(str(frame)).scaled(96, 54, Qt.AspectRatioMode.KeepAspectRatio,
                                                            Qt.TransformationMode.SmoothTransformation))
        head.addWidget(self.image, 0, Qt.AlignmentFlag.AlignTop)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        self.tick = QCheckBox(clip.id)
        self.tick.setChecked(bool(clip.selected))
        self.times = Hint("")
        self.times.setWordWrap(True)
        titles.addWidget(self.tick)
        titles.addWidget(self.times)
        titles.addStretch(1)
        head.addLayout(titles, 1)
        body.addLayout(head)
        self.text = Hint("")
        self.text.setWordWrap(True)
        self.notes = Hint("")
        self.notes.setWordWrap(True)
        body.addWidget(self.text)
        body.addWidget(self.notes)
        trims = QHBoxLayout()
        trims.setSpacing(4)
        self.start, self.end = QDoubleSpinBox(), QDoubleSpinBox()
        for spin, tip in ((self.start, "Start in the edited video (s)"), (self.end, "End in the edited video (s)")):
            spin.setRange(0.0, max(0.0, total))
            spin.setDecimals(1)
            spin.setSingleStep(0.5)
            spin.setSuffix(" s")
            spin.setToolTip(tip)
            spin.setKeyboardTracking(False)
            spin.setMaximumWidth(78)
        self.play = QPushButton("Preview")
        self.play.setObjectName("LinkButton")
        trims.addWidget(QLabel("In"))
        trims.addWidget(self.start)
        trims.addWidget(QLabel("Out"))
        trims.addWidget(self.end)
        trims.addStretch(1)
        trims.addWidget(self.play)
        body.addLayout(trims)
        self.update_clip(clip)
        self.tick.toggled.connect(lambda _c: self.changed.emit(self.clip_id))
        self.start.valueChanged.connect(lambda _v: self.changed.emit(self.clip_id))
        self.end.valueChanged.connect(lambda _v: self.changed.emit(self.clip_id))
        self.play.clicked.connect(lambda: self.preview.emit(self.clip_id))

    def update_clip(self, clip: Any) -> None:
        for spin, value in ((self.start, clip.edited_start), (self.end, clip.edited_end)):
            spin.blockSignals(True)
            spin.setValue(round(value, 1))
            spin.blockSignals(False)
        result = f" · {clip.result}" if clip.result else ""
        grade = f" · AI {clip.ai['overall']}/100" if clip.ai.get("overall") is not None else ""
        self.tick.setText(f"{clip.id} · {clip.title}" if clip.title else clip.id)
        self.times.setText(f"{fmt(clip.edited_start)}–{fmt(clip.edited_end)} · {clip.duration:.1f} s{result}{grade}")
        words = clip.text.split()
        quote = "“" + " ".join(words[:28]) + ("…" if len(words) > 28 else "") + "”" if words else "(no speech)"
        why = clip.ai.get("reason") or clip.reason
        self.text.setText(f"{why}\n{quote}" if why else quote)
        self.notes.setText(" · ".join(clip.notes))
        self.notes.setVisible(bool(clip.notes))


class ClipPreview(QDialog):
    """Plays the clip's kept parts one after the other (the cuts of the edit are skipped)."""

    def __init__(self, parent: QWidget, clip: Any, sources: list[str]) -> None:
        super().__init__(parent)
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
        from PySide6.QtMultimediaWidgets import QVideoWidget
        self.setWindowTitle(f"Preview {clip.id} · {clip.duration:.1f} s")
        self.resize(800, 480)
        lay = QVBoxLayout(self)
        video = QVideoWidget(self)
        lay.addWidget(video, 1)
        self.status = Hint("")
        lay.addWidget(self.status)
        self.parts, self.sources, self.index = list(clip.parts), sources, -1
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(video)
        self.player.positionChanged.connect(self._position)
        self.player.mediaStatusChanged.connect(self._status)
        self._loaded: str | None = None
        self._next()

    def _next(self) -> None:
        self.index += 1
        if self.index >= len(self.parts):
            self.player.pause()
            self.status.setText("End of clip.")
            return
        source_index, start, _end = self.parts[self.index]
        path = self.sources[source_index]
        self.status.setText(f"Part {self.index + 1}/{len(self.parts)} · {Path(path).name} at {fmt(start)}")
        if path != self._loaded:
            self._loaded = path
            self._pending_seek = int(start * 1000)
            self.player.setSource(QUrl.fromLocalFile(path))
        else:
            self.player.setPosition(int(start * 1000))
            self.player.play()

    def _status(self, status: Any) -> None:
        from PySide6.QtMultimedia import QMediaPlayer
        if status == QMediaPlayer.MediaStatus.LoadedMedia and getattr(self, "_pending_seek", None) is not None:
            self.player.setPosition(self._pending_seek)
            self._pending_seek = None
            self.player.play()

    def _position(self, ms: int) -> None:
        if 0 <= self.index < len(self.parts) and ms / 1000.0 >= self.parts[self.index][2]:
            self._next()

    def closeEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        self.player.stop()
        super().closeEvent(event)


class LayoutPreview(QDialog):
    def __init__(self, parent: QWidget, image: Path, note: str) -> None:
        super().__init__(parent)
        self.setWindowTitle("Shorts layout preview")
        lay = QVBoxLayout(self)
        picture = QLabel()
        picture.setPixmap(QPixmap(str(image)).scaledToHeight(720, Qt.TransformationMode.SmoothTransformation))
        lay.addWidget(picture)
        hint = Hint(note)
        hint.setWordWrap(True)
        lay.addWidget(hint)


# ---- page --------------------------------------------------------------------------------------------------
class ClipsController(QObject):
    """Owns the page state and the background jobs; created by build_clips_page."""

    def __init__(self, window: Any) -> None:
        super().__init__(window)
        self.window = window
        self.session: Any = None
        self.clips: list[Any] = []
        self.rows: dict[str, ClipRow] = {}
        self._files: list[str] = []
        self._thread: QThread | None = None
        self._worker: Any = None
        self.short: dict[str, Any] | None = None  # last make_short summary

    def busy(self) -> bool:
        return self._thread is not None

    def cancel_for_close(self) -> bool:
        if self._thread is None:
            return False
        if self._worker is not None:
            self._worker.cancel()
        return True

    # ---- sources
    def restore_videos(self, *, select: str | None = None) -> None:
        w = self.window
        combo = w.clips_video_combo
        selected = select or combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        exported = set()
        for entry in getattr(getattr(w, "project", None), "packaging_outputs", []) or []:
            if entry.get("output") and entry.get("project"):
                exported.add(str(Path(entry["output"]).expanduser().resolve()))
                combo.addItem(f"{entry.get('title') or Path(entry['output']).name} — export",
                              "export:" + str(entry.get("id") or entry["project"]))
        for path in self._files:
            if path not in exported:
                combo.addItem(f"{Path(path).name} — file", "file:" + path)
        index = combo.findData(selected)
        combo.setCurrentIndex(index if index >= 0 else 0)
        combo.blockSignals(False)
        self._update_hint()

    def _browse(self) -> None:
        if self.busy():
            return
        name, _ = QFileDialog.getOpenFileName(self.window, "Video to cut clips from", "",
                                              "Video (*.mp4 *.mkv *.mov *.ts *.webm *.avi *.m4v)")
        if name:
            self.add_video(name, select=True)

    def set_last_export(self, path: str) -> None:
        """A finished export of the editor appears in the Video list (selected only if Video is the source)."""
        self.add_video(path, select=self.window.clips_source_combo.currentData() == "video")

    def add_video(self, name: str, *, select: bool) -> None:
        """A video becomes an export entry when its saved edit is found next to it (clips then come from the
        original sources with its cuts), otherwise a plain file."""
        from analysis.packaging.outputs import find_previous_export
        path = str(Path(name).expanduser().resolve())
        project = getattr(self.window, "project", None)
        try:
            entry = find_previous_export(path, getattr(project, "packaging_outputs", []))
        except (OSError, ValueError):
            entry = None
        if entry is not None and project is not None:
            identity = str(entry.get("id") or entry["project"])
            if not any(str(e.get("id") or e["project"]) == identity for e in project.packaging_outputs):
                project.packaging_outputs.append(entry)  # shared registry with Covers (its selection is kept)
                thumbnails = getattr(self.window, "thumbnails", None)
                if thumbnails is not None:
                    thumbnails.restore_outputs()
                if hasattr(self.window, "_mark_project_dirty"):
                    self.window._mark_project_dirty(True)
            selected = "export:" + identity
        else:
            if path in self._files:
                self._files.remove(path)
            self._files.insert(0, path)
            selected = "file:" + path
        if select:
            self.window.clips_source_combo.setCurrentIndex(self.window.clips_source_combo.findData("video"))
            self.restore_videos(select=selected)
        else:
            self.restore_videos()

    def _entry(self, identity: str) -> dict[str, Any] | None:
        return next((e for e in getattr(getattr(self.window, "project", None), "packaging_outputs", []) or []
                     if "export:" + str(e.get("id") or e["project"]) == identity), None)

    def _session_args(self) -> dict[str, Any]:
        w = self.window
        if w.clips_source_combo.currentData() == "video":
            identity = str(w.clips_video_combo.currentData() or "")
            if identity.startswith("file:"):
                return {"video": identity[5:]}
            entry = self._entry(identity)
            if entry is None:
                raise ValueError("Choose an exported video or pick a file with …")
            output = Path(entry["output"]).expanduser().resolve()
            return {"project": str(entry.get("packaging_project") or entry["project"]),
                    "output": str(output.with_name(output.stem + "_clips"))}
        payload = w._build_project_file_payload()
        if not isinstance(payload, dict) or not payload.get("tracks"):
            raise ValueError("Load a video and edit it first (no tracks in the project).")
        folder = self._project_folder()
        from core.project_file import make_payload_portable
        snapshot = folder / "source_project.autocutter"
        folder.mkdir(parents=True, exist_ok=True)
        snapshot.write_text(json.dumps(make_payload_portable(payload, snapshot), ensure_ascii=False, indent=2),
                            encoding="utf-8")
        return {"project": str(snapshot), "output": str(folder)}

    def _project_folder(self) -> Path:
        """Stable per project: next to a saved .autocutter, else under Exports/clips (remembered in the project)."""
        w = self.window
        saved = getattr(w, "_project_file_path", None)
        if saved:
            return Path(saved).with_name(Path(saved).stem + "_clips")
        state = getattr(w.project, "clips", None)
        if not isinstance(state, dict):
            state = {}
            w.project.clips = state
        if not state.get("folder"):
            import uuid
            from utils.runtime_paths import exports_root
            state["folder"] = str(exports_root() / "clips" / ("project-" + uuid.uuid4().hex[:12]))
            if hasattr(w, "_mark_project_dirty"):
                w._mark_project_dirty(True)
        return Path(state["folder"])

    def _update_hint(self) -> None:
        w = self.window
        video = w.clips_source_combo.currentData() == "video"
        w.clips_video_row.setVisible(video)
        identity = str(w.clips_video_combo.currentData() or "")
        scope = ("Video file: its own timeline; clips are cut from this file. " if video and identity.startswith("file:")
                 else "Exported video: clips keep its cuts and are rendered from the original VOD. " if video
                 else "Current project: clips keep the current cuts and are rendered from the original sources. ")
        w.lbl_clips_hint.setText(scope + (AI_NOTE if self._ai() else PRIVACY_NOTE))

    def _ai(self) -> str | None:
        value = self.window.clips_ranking_combo.currentData()
        return None if value in (None, "none") else str(value)

    # ---- find
    def find(self, refresh: bool = False) -> None:
        w = self.window
        if self._thread is not None:
            if self._worker is not None:
                self._worker.cancel()
            w.lbl_clips_status.setText("Cancelling…")
            return
        if w.clips_min_spin.value() >= w.clips_max_spin.value():
            QMessageBox.information(w, "Clips", "The minimum length must be shorter than the maximum.")
            return
        try:
            args = self._session_args()
        except Exception as exc:  # noqa: BLE001 - shown to the user
            QMessageBox.information(w, "Clips", str(exc))
            return
        self._worker = FindWorker(args, w.clips_count_spin.value(), float(w.clips_min_spin.value()),
                                  float(w.clips_max_spin.value()), refresh, self._ai())
        self._run(self._worker, self._found, "Finding moments…", w.btn_clips_find)

    @Slot(object)
    def _found(self, value: Any) -> None:
        self.session, self.clips = value
        self._show_clips()
        ranking = self.session.ranking or {}
        how = f"AI ({ranking['ai']})" if ranking.get("ai") else "local signals"
        self.window.lbl_clips_status.setText(
            (f"{len(self.clips)} moments · ranked by {how} · folder {self.session.folder.name}" if self.clips else
             "No usable moment found (too little speech or edit too short).")
            + "".join(f"\n⚠ {warning}" for warning in self.session.warnings))
        self.window.lbl_clips_status.setToolTip(str(self.session.folder))

    def _show_clips(self) -> None:
        w = self.window
        layout = w.clips_list.layout()
        while layout.count():
            item = layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self.rows = {}
        analysis = self.session.load_analysis() if self.session is not None else None
        for clip in self.clips:
            frame = analysis.folder / clip.frame if analysis is not None and clip.frame else None
            row = ClipRow(clip, frame, analysis.duration if analysis is not None else clip.edited_end)
            row.changed.connect(self._row_changed)
            row.preview.connect(self._preview)
            layout.addWidget(row)
            self.rows[clip.id] = row
        w.clips_results.setVisible(bool(self.clips))
        self._update_export()

    def _row_changed(self, clip_id: str) -> None:
        from analysis.clips.candidates import retime
        clip = next(c for c in self.clips if c.id == clip_id)
        row = self.rows[clip_id]
        clip.selected = row.tick.isChecked()
        start, end = row.start.value(), row.end.value()
        if abs(start - clip.edited_start) > 0.05 or abs(end - clip.edited_end) > 0.05:
            retime(self.session.load_analysis(), clip, start, end)
            row.update_clip(clip)
        self.session.save(self.clips)
        self._update_export()

    def _preview(self, clip_id: str) -> None:
        clip = next(c for c in self.clips if c.id == clip_id)
        ClipPreview(self.window, clip, self.session.load_analysis().sources).show()

    # ---- export
    def _formats(self) -> tuple[str, ...]:
        value = self.window.clips_format_combo.currentData()
        return ("horizontal", "vertical") if value == "both" else (str(value or "horizontal"),)

    def _update_export(self) -> None:
        w = self.window
        chosen = [c for c in self.clips if c.selected]
        shorts = "vertical" in self._formats()
        w.clips_subtitles.setVisible(shorts)
        w.btn_clips_preview.setVisible(shorts and bool(self.clips))
        files = len(chosen) * len(self._formats())
        w.btn_clips_export.setText(f"Export {len(chosen)} clip{'s' if len(chosen) != 1 else ''}"
                                   + (f" ({files} files)" if files != len(chosen) else ""))
        w.btn_clips_export.setEnabled(bool(chosen) and self._thread is None)

    def preview_layout(self) -> None:
        if self.session is None or not self.clips:
            return
        clip = next((c for c in self.clips if c.selected), self.clips[0])
        try:
            image = self.session.preview(clip, subtitles=self.window.clips_subtitles.isChecked())
            layout = self.session.vertical_layout(clip)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            QMessageBox.information(self.window, "Clips", str(exc))
            return
        note = (f"{clip.id}: webcam on top, board below; captions on the seam."
                if layout.mode == "webcam_board" else
                f"{clip.id}: no webcam detected - board only over a blurred background.")
        LayoutPreview(self.window, image, note + " The webcam area is the main one of the video: in scenes where "
                      "the stream overlay moves, the crop does not follow it.").show()

    def export(self) -> None:
        w = self.window
        if self._thread is not None:
            if self._worker is not None:
                self._worker.cancel()
            w.lbl_clips_status.setText("Cancelling…")
            return
        chosen = [c for c in self.clips if c.selected]
        if not chosen or self.session is None:
            return
        self._worker = RenderWorker(self.session, chosen, w.clips_loudnorm.isChecked(), self._formats(),
                                    w.clips_subtitles.isChecked())
        self._run(self._worker, self._exported, f"Rendering {len(chosen)} clip(s)…", w.btn_clips_export)

    @Slot(object)
    def _exported(self, results: list[dict[str, Any]]) -> None:
        self.window.lbl_clips_status.setText(f"{len(results)} file(s) saved in {self.session.folder.name}")
        self.window.btn_clips_open.setVisible(True)

    def open_folder(self) -> None:
        if self.session is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.session.folder)))

    # ---- Twitch clip -> Short
    def _browse_short(self) -> None:
        if self.busy():
            return
        name, _ = QFileDialog.getOpenFileName(self.window, "Twitch clip (downloaded)", "",
                                              "Video (*.mp4 *.mkv *.mov *.ts *.webm *.m4v)")
        if name:
            self.window.clips_short_source.setText(name)

    def make_short(self) -> None:
        from analysis.clips.short import is_clip_url
        w = self.window
        if self._thread is not None:
            if self._worker is not None:
                self._worker.cancel()
            w.lbl_clips_status.setText("Cancelling…")
            return
        source = w.clips_short_source.text().strip().strip('"')
        if not source or not (is_clip_url(source) or Path(source).expanduser().is_file()):
            QMessageBox.information(w, "Clips", "Paste a Twitch clip link (clips.twitch.tv/… or "
                                                "twitch.tv/CHANNEL/clip/…) or choose a downloaded clip with …")
            return
        w.clips_short_result.setVisible(False)
        self._worker = ShortWorker(source, w.clips_short_cold_open.isChecked(), w.clips_short_zoom.isChecked(),
                                   w.clips_short_captions.isChecked(), w.clips_loudnorm.isChecked())
        self._run(self._worker, self._short_done, "Short · starting…", w.btn_clips_short)

    @Slot(object)
    def _short_done(self, summary: dict[str, Any]) -> None:
        w = self.window
        self.short = summary
        notes = " · ".join(summary["plan"].get("notes") or [])
        w.lbl_clips_status.setText(f"Short ready: {Path(summary['output']).name} ({summary['duration_s']:.0f} s)"
                                   + (f"\n{notes}" if notes else "")
                                   + "".join(f"\n⚠ {warning}" for warning in summary.get("warnings") or []))
        w.lbl_clips_status.setToolTip(summary["output"])
        preview = summary.get("preview")
        if preview and Path(preview).is_file():
            w.lbl_clips_short_preview.setPixmap(QPixmap(preview).scaled(
                135, 240, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        title = (summary.get("youtube") or {}).get("title") or ""
        w.lbl_clips_short_title.setText(f"YouTube title: {title}" if title else "")
        w.lbl_clips_short_title.setVisible(bool(title))
        w.clips_short_result.setVisible(True)

    def open_short_folder(self) -> None:
        if self.short:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(self.short["output"]).parent)))

    # ---- jobs
    def _run(self, worker: Any, on_done: Any, message: str, button: QPushButton) -> None:
        w = self.window
        self._button, self._button_text = button, button.text()
        self._thread = _start(self, worker, on_done, self._failed, w.lbl_clips_status.setText)
        self._thread.finished.connect(self._finish_job)
        button.setText("Cancel")
        button.setEnabled(True)
        w.clips_progress.setVisible(True)
        w.lbl_clips_status.setText(message)
        self._enable_inputs()

    @Slot(str)
    def _failed(self, message: str) -> None:
        self.window.lbl_clips_status.setText(message)
        if message != "Cancelled.":
            QMessageBox.information(self.window, "Clips", message)

    def _finish_job(self) -> None:
        self._thread = None
        self._worker = None
        self._button.setText(self._button_text)
        self.window.clips_progress.setVisible(False)
        self._enable_inputs()
        self._update_export()

    def _enable_inputs(self) -> None:
        w = self.window
        idle = self._thread is None
        for widget in (w.clips_source_combo, w.clips_video_combo, w.btn_clips_browse, w.btn_clips_add_video,
                       w.clips_ranking_combo, w.clips_min_spin,
                       w.clips_max_spin, w.clips_count_spin, w.clips_list, w.clips_loudnorm, w.clips_format_combo,
                       w.clips_subtitles, w.btn_clips_preview, w.clips_short_source, w.btn_clips_short_browse,
                       w.clips_short_cold_open, w.clips_short_zoom, w.clips_short_captions):
            widget.setEnabled(idle)
        w.btn_clips_find.setEnabled(idle or self._button is w.btn_clips_find)
        w.btn_clips_export.setEnabled(idle or self._button is w.btn_clips_export)
        w.btn_clips_short.setEnabled(idle or self._button is w.btn_clips_short)


def build_clips_page(window: Any) -> ClipsController:
    """Builds window.page_clips' content; returns its controller (window.clips)."""
    controller = ClipsController(window)
    find = Section("Clips", subtitle="short videos from the edit")
    window.clips_source_combo = QComboBox()
    window.clips_source_combo.addItem("Current project", "project")
    window.clips_source_combo.addItem("Video", "video")
    find.add(PropertyRow("Source", window.clips_source_combo,
                         tooltip="Current project: its current cuts. Video: an export of this project or any file."))
    window.clips_video_combo = QComboBox()
    for combo in (window.clips_source_combo, window.clips_video_combo):  # long file names must not widen the page
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        combo.setMinimumContentsLength(8)
    window.btn_clips_browse = QPushButton("…")
    window.btn_clips_browse.setFixedWidth(32)
    window.btn_clips_browse.setToolTip("Choose a video file.")
    row = QWidget()
    row_l = QHBoxLayout(row)
    row_l.setContentsMargins(0, 0, 0, 0)
    row_l.addWidget(window.clips_video_combo, 1)
    row_l.addWidget(window.btn_clips_browse)
    window.clips_video_row = PropertyRow("Video", row)
    find.add(window.clips_video_row)
    window.btn_clips_add_video = QPushButton("Add an exported video…")
    window.btn_clips_add_video.setObjectName("LinkButton")
    window.btn_clips_add_video.setToolTip("Find clips in a video you already exported (or any video file). When its "
                                          "saved edit is next to it, clips are cut from the original VOD.")
    find.add(window.btn_clips_add_video)
    window.clips_min_spin, window.clips_max_spin, window.clips_count_spin = QSpinBox(), QSpinBox(), QSpinBox()
    window.clips_min_spin.setRange(5, 120)
    window.clips_min_spin.setValue(20)
    window.clips_max_spin.setRange(10, 180)
    window.clips_max_spin.setValue(60)
    window.clips_count_spin.setRange(3, 30)
    window.clips_count_spin.setValue(10)
    window.clips_ranking_combo = QComboBox()
    for label, value in (("AI · Codex", "codex"), ("AI · Claude", "claude"), ("Local only", "none")):
        window.clips_ranking_combo.addItem(label, value)
    window.clips_ranking_combo.setToolTip("AI: local signals pick the best windows, then the agent grades hook/flow/"
                                          "value, tightens start/end and writes titles. Local: no provider call.")
    for spin in (window.clips_min_spin, window.clips_max_spin, window.clips_count_spin):
        spin.setMaximumWidth(72)
    for spin in (window.clips_min_spin, window.clips_max_spin):
        spin.setSuffix(" s")
    lengths = QWidget()
    lengths_l = QHBoxLayout(lengths)
    lengths_l.setContentsMargins(0, 0, 0, 0)
    lengths_l.addWidget(window.clips_min_spin)
    lengths_l.addWidget(QLabel("–"))
    lengths_l.addWidget(window.clips_max_spin)
    lengths_l.addStretch(1)
    find.add(PropertyRow("Length", lengths, tooltip="Clip length in the edited video"))
    find.add(PropertyRow("Moments", window.clips_count_spin, tooltip="How many moments to propose"))
    find.add(PropertyRow("Ranking", window.clips_ranking_combo))
    window.lbl_clips_hint = Hint("")
    find.add(window.lbl_clips_hint)
    window.btn_clips_find = QPushButton("Find moments")
    window.btn_clips_find.setObjectName("PrimaryCTA")
    window.btn_clips_find.setCursor(Qt.CursorShape.PointingHandCursor)
    find.add(window.btn_clips_find)
    window.btn_clips_refresh = QPushButton("Recompute proposals")
    window.btn_clips_refresh.setObjectName("LinkButton")
    window.btn_clips_refresh.setToolTip("Rebuild the list (ticks of the same moments are kept).")
    find.add(window.btn_clips_refresh)
    window.clips_progress = QProgressBar()
    window.clips_progress.setRange(0, 0)
    window.clips_progress.setFixedHeight(6)
    window.clips_progress.setVisible(False)
    find.add(window.clips_progress)
    window.lbl_clips_status = Hint("")
    window.lbl_clips_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    find.add(window.lbl_clips_status)

    res = Section("Moments", subtitle="tick, trim and preview")
    window.clips_results = res
    window.clips_list = QWidget()
    list_l = QVBoxLayout(window.clips_list)
    list_l.setContentsMargins(0, 0, 0, 0)
    list_l.setSpacing(6)
    res.add(window.clips_list)
    window.clips_format_combo = QComboBox()
    window.clips_format_combo.addItem("16:9 · 1080p", "horizontal")
    window.clips_format_combo.addItem("Shorts 9:16", "vertical")
    window.clips_format_combo.addItem("Both", "both")
    window.clips_format_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    window.clips_format_combo.setMinimumContentsLength(8)
    window.clips_format_combo.setToolTip("Shorts 9:16: webcam on top, board below, 1080×1920, burned captions.")
    res.add(PropertyRow("Format", window.clips_format_combo))
    window.clips_subtitles = QCheckBox("Captions on Shorts (word by word)")
    window.clips_subtitles.setChecked(True)
    window.clips_subtitles.setVisible(False)
    res.add(window.clips_subtitles)
    window.btn_clips_preview = QPushButton("Shorts layout preview")
    window.btn_clips_preview.setObjectName("LinkButton")
    window.btn_clips_preview.setToolTip("The 9:16 layout and captions on a frame of the first ticked clip.")
    window.btn_clips_preview.setVisible(False)
    res.add(window.btn_clips_preview)
    window.clips_loudnorm = QCheckBox("Normalize loudness (-14 LUFS)")
    window.clips_loudnorm.setChecked(True)
    res.add(window.clips_loudnorm)
    window.btn_clips_export = QPushButton("Export 0 clips")
    window.btn_clips_export.setObjectName("PrimaryCTA")
    window.btn_clips_export.setEnabled(False)
    res.add(window.btn_clips_export)
    window.btn_clips_open = QPushButton("Open clips folder")
    window.btn_clips_open.setObjectName("LinkButton")
    window.btn_clips_open.setVisible(False)
    res.add(window.btn_clips_open)
    res.setVisible(False)

    short = Section("Twitch clip → Short", subtitle="")
    window.clips_short_source = QLineEdit()
    window.clips_short_source.setPlaceholderText("Twitch clip link or a downloaded clip")
    window.clips_short_source.setClearButtonEnabled(True)
    window.btn_clips_short_browse = QPushButton("…")
    window.btn_clips_short_browse.setFixedWidth(32)
    window.btn_clips_short_browse.setToolTip("Choose a clip already downloaded.")
    source_row = QWidget()
    source_l = QHBoxLayout(source_row)
    source_l.setContentsMargins(0, 0, 0, 0)
    source_l.addWidget(window.clips_short_source, 1)
    source_l.addWidget(window.btn_clips_short_browse)
    short.add(PropertyRow("Clip", source_row, tooltip="clips.twitch.tv/… or twitch.tv/CHANNEL/clip/…: the clip is "
                                                      "downloaded; or an MP4 on disk."))
    window.clips_short_cold_open = QCheckBox("Climax first")
    window.clips_short_cold_open.setChecked(True)
    window.clips_short_cold_open.setToolTip("Cold open: the peak (loud reaction) is shown first for ~2 s, then the "
                                            "clip runs from its setup and ends on the reaction (the loop leads back "
                                            "to the peak).")
    window.clips_short_zoom = QCheckBox("Zoom on the play")
    window.clips_short_zoom.setChecked(True)
    window.clips_short_zoom.setToolTip("Below the webcam the board is cropped around where it changes before the "
                                       "peak (up to x1.5); no zoom when the action covers the whole board.")
    window.clips_short_captions = QCheckBox("Big captions")
    window.clips_short_captions.setChecked(True)
    window.clips_short_captions.setToolTip("Turn off for streamers who already burn live captions into the stream.")
    for box in (window.clips_short_cold_open, window.clips_short_zoom, window.clips_short_captions):
        short.add(box)
    short.add(Hint("1080×1920: webcam on top, board below, 20-40 s ending right after the reaction. Analysed and "
                   "rendered locally."))
    window.btn_clips_short = QPushButton("Make Short")
    window.btn_clips_short.setObjectName("PrimaryCTA")
    window.btn_clips_short.setCursor(Qt.CursorShape.PointingHandCursor)
    short.add(window.btn_clips_short)
    window.clips_short_result = QWidget()
    result_l = QHBoxLayout(window.clips_short_result)
    result_l.setContentsMargins(0, 0, 0, 0)
    window.lbl_clips_short_preview = QLabel()
    window.lbl_clips_short_preview.setFixedSize(135, 240)
    window.lbl_clips_short_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
    result_l.addWidget(window.lbl_clips_short_preview, 0, Qt.AlignmentFlag.AlignTop)
    side = QVBoxLayout()
    window.lbl_clips_short_title = Hint("")
    window.lbl_clips_short_title.setWordWrap(True)
    window.lbl_clips_short_title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    side.addWidget(window.lbl_clips_short_title)
    window.btn_clips_short_open = QPushButton("Open folder")
    window.btn_clips_short_open.setObjectName("LinkButton")
    side.addWidget(window.btn_clips_short_open)
    side.addStretch(1)
    result_l.addLayout(side, 1)
    window.clips_short_result.setVisible(False)
    short.add(window.clips_short_result)

    window.clips_source_combo.currentIndexChanged.connect(lambda _i: controller._update_hint())
    window.clips_video_combo.currentIndexChanged.connect(lambda _i: controller._update_hint())
    window.clips_ranking_combo.currentIndexChanged.connect(lambda _i: controller._update_hint())
    window.btn_clips_browse.clicked.connect(controller._browse)
    window.btn_clips_add_video.clicked.connect(controller._browse)
    window.btn_clips_find.clicked.connect(lambda: controller.find())
    window.btn_clips_refresh.clicked.connect(lambda: controller.find(refresh=True))
    window.btn_clips_export.clicked.connect(controller.export)
    window.btn_clips_open.clicked.connect(controller.open_folder)
    window.clips_format_combo.currentIndexChanged.connect(lambda _i: controller._update_export())
    window.btn_clips_preview.clicked.connect(controller.preview_layout)
    window.btn_clips_short_browse.clicked.connect(controller._browse_short)
    window.btn_clips_short.clicked.connect(controller.make_short)
    window.clips_short_source.returnPressed.connect(controller.make_short)
    window.btn_clips_short_open.clicked.connect(controller.open_short_folder)
    controller._button = window.btn_clips_find
    controller.restore_videos()

    from ui.layout_main_page import _scroll_page

    window.clips_scroll = _scroll_page(window.page_clips, [find, res, short], name="InspectorScroll")
    return controller
