"""Import a final video for first-time thumbnails; no real providers, probes or VOD analysis."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from analysis.packaging.outputs import find_previous_export, settings_for_previous_video
from export.artifacts import save_snapshot
from test_packaging_outputs import thumbnail_owner


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_CUTTER_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("AUTO_CUTTER_CONFIG_DIR", str(tmp_path / "config"))


def snapshot(tmp_path, *, suffix="one"):
    video = tmp_path / "Mage [finale] è.mp4"
    video.write_bytes(b"finished render")
    source = tmp_path / "original.mp4"
    source.write_bytes(b"source")
    folder = tmp_path / (video.stem + "_recut_" + suffix)
    project = folder / "export_edit.autocutter"
    save_snapshot({"project_id": suffix, "project_name": "Mage export", "tracks": [
        {"path": str(source), "duration": 20, "cuts_enabled": True, "keeps": [{"start": 3, "end": 8}]}]}, project)
    return video, source, project


def test_standalone_export_does_not_require_project_run_or_detection(tmp_path):
    video = tmp_path / "video precedente è.mp4"
    video.write_bytes(b"render")
    assert find_previous_export(video) is None
    settings = settings_for_previous_video(video, {"provider": "claude", "language": "it",
        "creative_direction": {"generation_strategy": "reference_preserving"}})
    assert settings.video == str(video) and settings.project is None and settings.games == "none"
    assert Path(settings.output) == tmp_path / "video precedente è_packaging"
    assert settings.provider == "claude" and settings.creative_direction["generation_strategy"] == "reference_preserving"
    with pytest.raises(ValueError, match="existing"):
        settings_for_previous_video(tmp_path / "missing.mp4", {})


def test_recovers_exact_snapshot_without_existing_cover_run_and_preserves_keeps(tmp_path):
    video, _, project = snapshot(tmp_path)
    entry = find_previous_export(video)
    assert entry["packaging_project"] == str(project)
    assert not (project.parent / "packaging").exists()
    from analysis.packaging.outputs import settings_for_output
    settings = settings_for_output(entry, {})
    assert settings.project == str(project) and settings.games == "project"
    assert settings.video is None


def test_missing_originals_corrupt_or_ambiguous_metadata_falls_back_to_video(tmp_path):
    video, source, project = snapshot(tmp_path)
    source.unlink()
    assert find_previous_export(video) is None
    source.write_bytes(b"source")
    snapshot(tmp_path, suffix="two")
    assert find_previous_export(video) is None  # do not guess which historical edit produced it
    project.write_text("invalid json", encoding="utf-8")
    entry = find_previous_export(video)
    assert entry["id"] == "two"


def test_saved_request_matches_video_and_never_imports_sibling_class(tmp_path):
    video, _, project = snapshot(tmp_path)
    request = {"export": {"id": "output-id", "project": str(project), "packaging_project": str(project),
                          "output": str(video), "group": "MAGE", "creative_direction": {"mode": "guided"}}}
    (project.parent / "packaging_request.json").write_text(json.dumps(request), encoding="utf-8")
    assert find_previous_export(video)["id"] == "output-id"
    other = tmp_path / "Hunter.mp4"
    other.write_bytes(b"other render")
    assert find_previous_export(other) is None
    entry = find_previous_export(video, [request["export"]])
    entry["creative_direction"]["mode"] = "automatic"
    assert request["export"]["creative_direction"]["mode"] == "guided"


def test_parent_backup_is_not_mistaken_for_exact_export_selection(tmp_path):
    video, _, project = snapshot(tmp_path)
    project.rename(project.parent / "project_before_export.autocutter")
    assert find_previous_export(video) is None


def test_source_has_two_choices_and_project_hint_follows_export_selection(tmp_path):
    from types import SimpleNamespace
    owner = thumbnail_owner()
    combo = owner.thumb_source_combo
    assert [combo.itemData(i) for i in range(combo.count())] == ["project", "video"]
    assert owner.thumb_output_row.isHidden()
    owner.hero_groups_panel = SimpleNamespace(filter_enabled=lambda: False)
    owner.thumbnails._update_hint()
    assert "one combined cover" in owner.lbl_thumb_hint.text()
    owner.hero_groups_panel = SimpleNamespace(filter_enabled=lambda: True)
    owner.thumbnails._update_hint()
    assert "Single video with the selected classes: one cover set" in owner.lbl_thumb_hint.text()
    video = tmp_path / "Hunter.mp4"
    video.write_bytes(b"render")
    owner.thumbnails.set_last_export(str(video))
    assert owner.thumb_output_combo.findData("file:" + str(video.resolve())) >= 0
    assert combo.currentData() == "project"  # a finished export never switches the source by itself
    owner.close()


def test_gui_selects_previous_file_with_empty_project_and_clears_stale_results(tmp_path):
    video = tmp_path / "old export.mp4"
    video.write_bytes(b"render")
    owner = thumbnail_owner()
    project = owner.project
    owner.thumbnails.output = "previous-cover-folder"
    owner.thumbnails.select_previous_export(str(video))
    assert owner.thumb_source_combo.currentData() == "video"
    assert owner.thumb_output_combo.currentData() == "file:" + str(video.resolve())
    assert owner.thumbnails.output is None and owner.thumb_results.isHidden()
    assert owner.project is project and not project.media
    settings = owner.thumbnails._settings()
    assert settings.video == str(video.resolve()) and settings.games == "detect"  # unknown file: detect games
    owner.thumbnails.set_batch_busy(True)
    assert not owner.btn_thumb_browse_video.isEnabled()
    owner.thumbnails.set_batch_busy(False)
    assert owner.btn_thumb_browse_video.isEnabled()
    owner.close()


def test_gui_recovers_export_into_existing_registry_without_replacing_project(tmp_path):
    video, _, project = snapshot(tmp_path)
    owner = thumbnail_owner()
    original_project = owner.project
    tracks = deepcopy(original_project.tracks)
    owner.thumbnails.select_previous_export(str(video))
    assert owner.thumb_source_combo.currentData() == "video"
    assert owner.thumbnails._settings().project == str(project)
    assert owner.project is original_project and owner.project.tracks == tracks
    assert len(owner.project.packaging_outputs) == 1
    owner.thumbnails.select_previous_export(str(video))
    assert len(owner.project.packaging_outputs) == 1
    owner.close()


def test_file_only_pipeline_uses_final_render_and_never_calls_game_detector(tmp_path):
    from analysis.packaging.pipeline import PackagingPipeline
    from youtube_packaging_fakes import FakeEngine, FakeSampler, fake_rms, fake_scanner
    video = tmp_path / "Hunter finished.mp4"
    video.write_bytes(b"render")
    settings = settings_for_previous_video(video, {})
    settings.analyze_only = True
    settings.webcam_roi = "none"
    pipeline = PackagingPipeline(settings, transcription_engine=FakeEngine(), frame_sampler=FakeSampler(),
        rms_extractor=fake_rms, keyframe_scanner=fake_scanner, duration_probe=lambda _: 60.,
        game_detector=lambda *a, **k: pytest.fail("previous final video must not trigger Hearthstone detection"))
    report = pipeline.run()
    assert report["status"] == "analyzed"
    assert pipeline._evidence.edit_map.sources == [str(video)]
    assert all(g.result is None and g.origin == "whole_video" for g in pipeline._evidence.games)


def test_gui_generate_from_final_file_starts_worker_without_loaded_vod(tmp_path, monkeypatch):
    import time
    from PySide6.QtWidgets import QMessageBox
    from test_packaging_outputs import APP
    import analysis.packaging.pipeline as module
    video = tmp_path / "previous.mp4"
    video.write_bytes(b"final render")
    owner = thumbnail_owner()
    controller = owner.thumbnails
    controller.select_previous_export(str(video))
    calls = []
    class FakePipeline:
        def __init__(self, settings, **kwargs):
            calls.append(settings)
        def run(self):
            return {"status": "done", "timings_s": {"total": 0}}
    monkeypatch.setattr(module, "PackagingPipeline", FakePipeline)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    controller.generate()
    assert controller.busy() and not owner.btn_thumb_browse_video.isEnabled()
    deadline = time.monotonic() + 5
    while controller.busy() and time.monotonic() < deadline:
        APP.processEvents()
        time.sleep(.005)
    assert not controller.busy() and len(calls) == 1
    assert calls[0].video == str(video.resolve()) and calls[0].games == "detect"
    assert owner.project.media == {} and owner.btn_thumb_browse_video.isEnabled()
    owner.close()
