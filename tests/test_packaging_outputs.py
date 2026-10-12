"""Per-export cover isolation; no cloud calls, VOD detection or lengthy renders."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import numpy as np
import pytest
from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout

from analysis.audio_service import AnalysisCancellation
from analysis.packaging.cache import PackagingCache
from analysis.packaging.frames import FrameExtractor
from analysis.packaging.outputs import package_outputs, settings_for_output
from analysis.packaging.timeline import EditMap
from analysis.hero_groups.projects import prepare_outputs, group_project, export_outputs
from test_hero_groups import project
from youtube_packaging_fakes import FakeEngine, FakeSampler, fake_rms, fake_scanner, planner

APP = QApplication.instance() or QApplication([])


def plans_for_test(tmp_path):
    payload = project(tmp_path)
    plans, warnings = prepare_outputs(payload, tmp_path / "video è 🎮", ["A", "B"])
    assert not warnings
    return plans


def test_settings_each_output_uses_own_saved_edit_and_directory(tmp_path):
    outputs = plans_for_test(tmp_path)
    settings = [settings_for_output(o, {"provider": "claude", "language": "en", "thumbnail_engine": "v3"}) for o in outputs]
    assert settings[0].output != settings[1].output
    assert all(s.video is None and s.games == "project" for s in settings)
    assert [s.project for s in settings] == [o["project"] for o in outputs]
    assert all(s.provider == "claude" and s.language == "en" and s.thumbnail_engine == "v3" for s in settings)
    assert group_project({**project(tmp_path), "packaging_outputs": outputs}, "A").get("packaging_outputs") is None


def test_shared_raw_frame_cache_filtered_for_each_output_and_no_unsafe_fallback(tmp_path):
    (tmp_path / "source.mp4").write_bytes(b"fixture")
    edit_a = EditMap.from_video(str(tmp_path / "source.mp4"), duration_probe=lambda p: 10)
    edit_b = deepcopy(edit_a)
    edit_a.pieces[0] = replace(edit_a.pieces[0], source_start=1, source_end=2)
    edit_b.pieces[0] = replace(edit_b.pieces[0], source_start=3, source_end=4)
    sampler = FakeSampler()
    cache = PackagingCache(tmp_path / "cache")
    a = FrameExtractor(edit_a, tmp_path / "a", cache, sampler=sampler)
    b = FrameExtractor(edit_b, tmp_path / "b", cache, sampler=sampler)
    assert [t for t, _ in a.burst(0, 0, 5, fps=2)] == [1, 1.5]
    assert [t for t, _ in b.burst(0, 0, 5, fps=2)] == [3, 3.5]
    assert sampler.calls == 1  # raw source bundle reused, filtering is per edit
    assert a.best_frame(0, 8, radius=0.25) is None
    with pytest.raises(ValueError, match="keeps"):
        a.save(np.zeros((10, 10, 3), np.uint8), source_index=0, source_time=3)
    assert not a.records


def test_batch_runs_shared_pipeline_kept_games_and_frames_only_with_cache_reuse(tmp_path):
    from analysis.packaging.pipeline import PackagingPipeline

    outputs = plans_for_test(tmp_path)
    engine, sampler = FakeEngine(), FakeSampler()
    runs = []
    def factory(settings, **kwargs):
        settings = replace(settings, analyze_only=True, thumbnail_engine="v2", webcam_roi="none",
                           cache_dir=str(tmp_path / "cache"))
        pipeline = PackagingPipeline(settings, transcription_engine=engine, frame_sampler=sampler,
                                     rms_extractor=fake_rms, keyframe_scanner=fake_scanner, planner=planner(),
                                     game_detector=lambda *a, **k: pytest.fail("must reuse games"), **kwargs)
        runs.append(pipeline)
        return pipeline
    done, warnings = package_outputs(outputs, {}, pipeline_factory=factory)
    assert not warnings and len(done) == 2
    for output, run, expected in zip(done, runs, ({1, 3}, {2})):
        folder = Path(output["packaging"])
        games = json.loads((folder / "game_contexts.json").read_text())["games"]
        assert {int(g["game_id"].split("-")[-1]) for g in games} == expected
        assert run.frames.records
        assert all(run.frames.edit_map.to_edited(r.source_time, r.source_index) is not None
                   for r in run.frames.records.values())
        record = json.loads((folder.parent / "packaging_request.json").read_text())
        assert record["export"]["id"] == output["id"] and output["packaging_status"] == "done"
    calls = (engine.calls, sampler.calls)
    package_outputs(outputs, {}, pipeline_factory=factory)
    assert (engine.calls, sampler.calls) == calls  # no repeat ASR / decoding on another run of the edit


def test_failed_cover_does_not_stop_other_outputs_or_delete_videos(tmp_path):
    outputs = plans_for_test(tmp_path)
    for o in outputs:
        Path(o["output"]).write_bytes(b"rendered video")
    class Fake:
        def __init__(self, settings, **kwargs):
            self.settings = settings
        def run(self):
            if self.settings.project == outputs[0]["project"]:
                raise RuntimeError("provider unavailable")
            return {"status": "done"}
    notifications = []
    done, warnings = package_outputs(outputs, {}, pipeline_factory=Fake, on_result=notifications.append)
    assert [o["packaging_status"] for o in done] == ["failed", "done"]
    assert len(warnings) == 1 and notifications == done
    assert all(Path(o["output"]).read_bytes() == b"rendered video" for o in done)


def test_cancel_persists_all_exported_video_associations_without_next_provider_call(tmp_path):
    outputs = plans_for_test(tmp_path)
    token = AnalysisCancellation()
    calls = []
    class Fake:
        def __init__(self, settings, **kwargs):
            calls.append(settings)
        def run(self):
            token.cancel()
            token.raise_if_cancelled()
    done, warnings = package_outputs(outputs, {}, pipeline_factory=Fake, cancellation=token)
    assert len(calls) == 1 and not warnings
    assert [o["packaging_status"] for o in done] == ["cancelled", "cancelled"]
    assert all((Path(o["project"]).parent / "packaging_request.json").is_file() for o in done)


def test_successful_exports_notified_and_packaging_snapshot_immutable(tmp_path):
    from automation.manager import PipelineManager
    from automation.store import PipelineStore

    outputs = plans_for_test(tmp_path)
    manager = PipelineManager(PipelineStore(tmp_path / "jobs.json"))
    class FakeExport:
        def execute(self, job_id, **kwargs):
            job = manager.start_export(job_id)
            destination = Path(job.metadata["delivery"]["output_path"])
            destination.write_bytes(b"video")
            return manager.mark_exported(job.id, destination)
    seen = []
    done = export_outputs(outputs, manager=manager, service=FakeExport(), on_completed=seen.append)
    assert seen == done
    settings = settings_for_output(done[0], {})
    original = Path(settings.project).read_bytes()
    Path(done[0]["project"]).write_text("edited later")
    assert Path(settings.project).read_bytes() == original


def test_export_worker_packaging_is_optional_and_receives_only_successful_outputs(tmp_path, monkeypatch):
    import ui.hero_groups as module
    import analysis.packaging.outputs as packaging

    payload = project(tmp_path)
    outputs = plans_for_test(tmp_path)
    monkeypatch.setattr(module, "prepare_outputs", lambda *a, **k: (outputs, []))
    def export(*args, **kwargs):
        for output in outputs:
            kwargs["on_completed"](output)
        return outputs
    monkeypatch.setattr(module, "export_outputs", export)
    calls = []
    monkeypatch.setattr(packaging, "package_outputs", lambda *a, **k: (calls.append(a) or outputs, []))
    for enabled in (False, True):
        worker = module.GroupWorker(payload, output_dir=str(tmp_path), packaging_options={} if enabled else None)
        done, ready = [], []
        worker.finished.connect(done.append)
        worker.output_ready.connect(ready.append)
        worker.run()
        assert done and ready == outputs
    assert len(calls) == 1 and calls[0][0] == outputs


def thumbnail_owner():
    from core.project import Project
    from ui.thumbnails_page import build_thumbnails_page

    owner = QWidget()
    owner.page_thumbnails = QWidget(owner)
    QVBoxLayout(owner)
    owner.layout().addWidget(owner.page_thumbnails)
    owner.project = Project.create_default()
    owner._mark_project_dirty = lambda *a: None
    owner.thumbnails = build_thumbnails_page(owner)
    return owner


def test_thumbnail_selector_isolates_results_and_registry_survives_project_roundtrip(tmp_path):
    from core.project_file import normalize_project_payload

    outputs = plans_for_test(tmp_path)
    owner = thumbnail_owner()
    controller = owner.thumbnails
    for output in outputs:
        controller.register_output(output)
    assert owner.thumb_output_combo.count() == 2
    assert owner.thumb_source_combo.currentData() == "export"
    for i in range(2):
        owner.thumb_output_combo.setCurrentIndex(i)
        settings = controller._settings()
        assert settings.project == outputs[i]["project"]
        assert controller.output == settings.output
        assert owner.thumb_results.isHidden()  # no stale sibling cover
    saved = normalize_project_payload({"tracks": [{"path": "old.mp4"}],
                                       "packaging_outputs": deepcopy(owner.project.packaging_outputs)})
    owner.project.packaging_outputs = json.loads(json.dumps(saved))["packaging_outputs"]
    controller.restore_outputs()
    assert owner.thumb_output_combo.count() == 2
    controller.register_output({**outputs[0], "packaging_status": "failed"})
    assert owner.thumb_output_combo.count() == 2
    assert owner.project.packaging_outputs[0]["packaging_status"] == "failed"
    controller.set_batch_busy(True)
    assert not owner.btn_thumb_generate.isEnabled() and not owner.thumb_output_combo.isEnabled()
    controller.set_batch_busy(False)
    assert owner.btn_thumb_generate.isEnabled()
    owner.close()


@pytest.mark.parametrize("remove_generated", [False, True])
def test_render_only_never_calls_provider_even_without_cache(tmp_path, remove_generated):
    from test_youtube_packaging_gen import make, FakeAgentGen, FakeGenerator

    make(tmp_path).run()
    if remove_generated:
        for image in (tmp_path / "out" / "thumbnails" / "generated").glob("*.png"):
            image.unlink()
    agent, generator = FakeAgentGen(), FakeGenerator()
    report = make(tmp_path, agent=agent, generator=generator, only="render", video=None, use_cache=False).run()
    assert not agent.calls and not generator.calls
    assert report["generation"]["calls_made"] == 0
    assert all(t["engine"] == ("local" if remove_generated else "generated") for t in report["thumbnails"])


def test_each_output_has_own_thumbnails_and_independent_approval(tmp_path):
    from analysis.packaging.edit import ThumbnailEditor
    from analysis.packaging.pipeline import PackagingPipeline
    from youtube_packaging_fakes import FakeAgent

    outputs = plans_for_test(tmp_path)
    agents = []
    def factory(settings, **kwargs):
        agent = FakeAgent()
        agents.append(agent)
        return PackagingPipeline(replace(settings, thumbnail_engine="v2", thumbnails=3, webcam_roi="none",
                                         cache_dir=str(tmp_path / "cache"), style_dir=str(tmp_path / "styles")),
                                 agent=agent, transcription_engine=FakeEngine(), frame_sampler=FakeSampler(),
                                 rms_extractor=fake_rms, keyframe_scanner=fake_scanner, planner=planner(), **kwargs)
    done, warnings = package_outputs(outputs, {}, pipeline_factory=factory)
    assert [o["packaging_status"] for o in done] == ["done", "done"], warnings
    editors = [ThumbnailEditor(o["packaging"]) for o in done]
    assert all(len(editor.concepts()) == 3 for editor in editors)
    first = editors[0].approve(1)
    assert not any(c["approved"] for c in editors[1].concepts())
    second = editors[1].approve(2)
    assert first["final"] != second["final"]
    assert editors[0].concepts()[0]["approved"] and editors[1].concepts()[1]["approved"]
    assert all(len(agent.calls) == 1 for agent in agents)


@pytest.mark.parametrize("cancel", [False, True])
def test_thumbnail_worker_lifecycle_and_persisted_status(tmp_path, monkeypatch, cancel):
    import time
    from PySide6.QtWidgets import QMessageBox
    import analysis.packaging.pipeline as module

    output = plans_for_test(tmp_path)[0]
    owner = thumbnail_owner()
    controller = owner.thumbnails
    controller.register_output(output)
    calls = []
    class FakePipeline:
        def __init__(self, settings, cancellation, **kwargs):
            self.token = cancellation
            calls.append(settings)
        def run(self):
            if cancel:
                deadline = time.monotonic() + 3
                while not self.token.is_cancelled and time.monotonic() < deadline:
                    time.sleep(.005)
                self.token.raise_if_cancelled()
            return {"status": "done", "timings_s": {"total": 1}}
    monkeypatch.setattr(module, "PackagingPipeline", FakePipeline)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    controller.generate()
    assert controller.busy() and not owner.thumb_output_combo.isEnabled()
    if cancel:
        assert controller.cancel_for_close()
    deadline = time.monotonic() + 5
    while controller.busy() and time.monotonic() < deadline:
        APP.processEvents()
        time.sleep(.005)
    assert not controller.busy() and controller._worker is None
    assert len(calls) == 1
    status = "cancelled" if cancel else "done"
    assert owner.project.packaging_outputs[0]["packaging_status"] == status
    manifest = json.loads((Path(output["project"]).parent / "packaging_request.json").read_text())
    assert manifest["export"]["packaging_status"] == status
    assert owner.thumb_output_combo.isEnabled()
    owner.close()
