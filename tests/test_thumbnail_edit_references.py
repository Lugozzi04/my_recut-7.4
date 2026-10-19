"""Thumbnail re-opening, optional edit attachments and skipped frame work; no providers or VODs."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication, QFileDialog, QWidget

from analysis.packaging.edit import EditError, ThumbnailEditor
from analysis.packaging.frames import FrameExtractor, read_image_unchanged, write_image


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def run_folder(tmp_path):
    out = tmp_path / "run è"
    write_image(out / "thumbnails" / "concept_01.png", np.full((90, 160, 3), 80, np.uint8))
    write_image(out / "webcam" / "original.png", np.full((40, 40, 3), 100, np.uint8))
    (out / "thumbnail_concepts.json").write_text(json.dumps({"concepts": [{
        "concept_id": "C1", "title": "Test", "thumbnail_text": "TEST"}]}), encoding="utf-8")
    (out / "generation_request.json").write_text(json.dumps({"packages": [{"concept_id": "C1", "images": [
        {"kind": "face_main", "path": "webcam/original.png"}]}]}), encoding="utf-8")
    return out


class Generator:
    def __init__(self):
        self.calls = []

    def available(self):
        return True

    def generate(self, prompt, images, ids, output):
        self.calls.append({"prompt": prompt, "images": list(images), "ids": ids})
        generated = output / "fake.png"
        write_image(generated, np.full((90, 160, 3), 150, np.uint8))
        return {ids[0]: generated}


def test_attachment_is_sent_saved_losslessly_and_reopens_with_history(run_folder, tmp_path):
    reference = tmp_path / "reference è.png"
    pixels = np.full((30, 20, 4), 190, np.uint8)
    pixels[:, :, 3] = 70
    write_image(reference, pixels)
    generator = Generator()
    editor = ThumbnailEditor(run_folder, generator=generator)
    entry = editor.edit(1, "Use this illustration on the left", reference_image=reference)
    assert len(generator.calls) == 1
    call = generator.calls[0]
    assert call["images"][0] == run_folder / "thumbnails" / "concept_01.png"
    attachment = run_folder / entry["reference_image"]
    assert call["images"][1] == attachment
    assert call["images"][2] == run_folder / "webcam" / "original.png"
    assert "image 2: creator-attached reference" in call["prompt"]
    assert "Use this illustration on the left" in call["prompt"]
    assert np.array_equal(read_image_unchanged(attachment), pixels)
    assert not Path(entry["reference_image"]).is_absolute()
    reference.unlink()
    reopened = ThumbnailEditor(run_folder)
    item = reopened.resolve(1)
    assert item["current_version"] == 1 and len(item["versions"]) == 2
    assert (run_folder / item["edits"][0]["reference_image"]).is_file()
    reopened.revert(1, 0)
    assert reopened.resolve(1)["current_version"] == 0
    approved = reopened.approve(1)
    assert Path(approved["final"]).is_file()
    assert ThumbnailEditor(run_folder).resolve(1)["approved"]


def test_text_only_edit_remains_compatible(run_folder):
    generator = Generator()
    entry = ThumbnailEditor(run_folder, generator=generator).edit("C1", "Change text")
    assert "reference_image" not in entry
    assert len(generator.calls) == 1 and len(generator.calls[0]["images"]) == 2


@pytest.mark.parametrize("kind", ["missing", "empty", "invalid"])
def test_invalid_attachment_never_calls_model_or_changes_history(run_folder, tmp_path, kind):
    reference = tmp_path / "invalid.png"
    if kind != "missing":
        reference.write_bytes(b"" if kind == "empty" else b"not an image")
    generator = Generator()
    with pytest.raises(EditError, match="reference image"):
        ThumbnailEditor(run_folder, generator=generator).edit(1, "Change", reference_image=reference)
    assert not generator.calls
    assert not (run_folder / "thumbnails" / "edits").exists()
    assert not (run_folder / "thumbnail_edits.json").exists()


def test_dialog_attachment_preview_apply_and_remove(app, run_folder, tmp_path, monkeypatch):
    import ui.thumbnails_page as module

    reference = tmp_path / "example.png"
    write_image(reference, np.full((30, 20, 3), 140, np.uint8))
    parent = QWidget()
    dialog = module.ThumbnailDialog(parent, str(run_folder), 1)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(reference), ""))
    dialog._attach_reference()
    assert dialog.reference_image == str(reference)
    assert not dialog.reference_preview.pixmap().isNull()
    assert dialog.btn_remove_reference.isEnabled()
    monkeypatch.setattr(module, "_start", lambda *a, **k: None)
    dialog.request.setPlainText("Use the attached artwork")
    dialog._apply()
    assert dialog._worker.reference_image == str(reference)
    assert not dialog.btn_reference.isEnabled() and not dialog.btn_remove_reference.isEnabled()
    dialog._edit_failed("fake failure")
    assert dialog.reference_image == str(reference)  # keep it for a retry
    dialog._clear_reference()
    assert dialog.reference_image is None and not dialog.btn_remove_reference.isEnabled()
    dialog.close()


def test_edit_worker_forwards_attachment_without_changing_other_calls(app, run_folder, monkeypatch):
    import analysis.packaging.edit as module
    from ui.thumbnails_page import EditWorker

    calls, results, failures = [], [], []
    class Editor:
        def __init__(self, output):
            assert output == str(run_folder)

        def edit(self, number, request, **kwargs):
            calls.append((number, request, kwargs))
            return {"version": 1}
    monkeypatch.setattr(module, "ThumbnailEditor", Editor)
    worker = EditWorker(str(run_folder), 1, "Change", "example.png")
    worker.finished.connect(results.append)
    worker.failed.connect(failures.append)
    worker.run()
    assert calls == [(1, "Change", {"reference_image": "example.png"})]
    assert results == [{"version": 1}] and failures == []


@pytest.mark.parametrize("subfolder", [False, True])
def test_open_existing_accepts_run_or_thumbnails_folder_without_analysis(app, run_folder, monkeypatch, subfolder):
    from test_packaging_outputs import thumbnail_owner

    owner = thumbnail_owner()
    chosen = run_folder / "thumbnails" if subfolder else run_folder
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: str(chosen))
    owner.thumbnails.open_existing()
    assert owner.thumbnails.output == str(run_folder)
    assert owner.thumb_titles.toPlainText() == ""
    assert not owner.thumb_tiles[0].pixmap().isNull()
    assert "TEST" in owner.thumb_tiles[0].toolTip()
    assert owner.thumbnails._thread is None
    owner.close()


def test_removed_or_intro_bursts_never_decode_or_load_bundles(tmp_path):
    from analysis.packaging.cache import PackagingCache
    from analysis.packaging.timeline import EditMap
    from youtube_packaging_fakes import FakeSampler

    source = tmp_path / "fake.mp4"
    source.write_bytes(b"fake media")
    mapping = EditMap.from_video(str(source), duration_probe=lambda p: 20)
    sampler = FakeSampler()
    frames = FrameExtractor(mapping, tmp_path / "out", PackagingCache(tmp_path / "cache"), sampler=sampler)
    frames.allowed_intervals = [(0, 10, 15)]
    assert frames.burst(0, 0, 4, fps=4) == []  # Intro / excluded part
    assert frames.best_frame(0, 5) is None
    assert frames.burst(0, 20, 21, fps=4) == []
    assert sampler.calls == 0 and frames.decode_calls == 0
    assert frames.burst(0, 9, 11, fps=2)
    assert sampler.calls == 1


def test_disjoint_keep_and_class_filters_do_not_look_like_joint_overlap(tmp_path):
    from analysis.packaging.cache import PackagingCache
    from youtube_packaging_fakes import FakeSampler

    mapping = SimpleNamespace(pieces=[SimpleNamespace(source_index=0, source_start=0, source_end=5)])
    frames = FrameExtractor(mapping, tmp_path, PackagingCache(tmp_path / "cache"), sampler=FakeSampler())
    frames.allowed_intervals = [(0, 6, 10)]
    assert frames.burst(0, 0, 10, fps=2) == []
    assert frames.decode_calls == 0


def test_skipped_bursts_still_respect_cancellation(tmp_path):
    from analysis.audio_service import AnalysisCancellation, AnalysisCancelled
    from analysis.packaging.cache import PackagingCache

    token = AnalysisCancellation()
    token.cancel()
    frames = FrameExtractor(SimpleNamespace(pieces=[]), tmp_path, PackagingCache(tmp_path / "cache"),
                            sampler=object(), cancellation=token)
    with pytest.raises(AnalysisCancelled):
        frames.burst(0, 0, 1, fps=1)
