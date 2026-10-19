"""Export regressions: bounded rendering, user presets and per-output packaging. No AI calls."""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import numpy as np
from PySide6.QtWidgets import QApplication

from export.settings import ExportSettings

APP = QApplication.instance() or QApplication([])


def test_export_button_routes_cancel_to_active_selected_worker():
    from ui.main_window import MainWindow
    calls = []
    panel = SimpleNamespace(running=lambda: True, worker=SimpleNamespace(output_dir="folder"),
                            cancel_work=lambda: calls.append("cancel"))
    MainWindow.export_mp4(SimpleNamespace(hero_groups_panel=panel))
    assert calls == ["cancel"]


def test_selected_chunks_open_only_needed_input_and_keep_quality(tmp_path):
    from export.exporter import ExportWorker
    worker = ExportWorker(ffmpeg_path="ffmpeg", input_path="game.mp4",
        input_paths=["game.mp4", "intro.mp4"], output_path=str(tmp_path / "out.mp4"),
        keeps=[], codec="libx264", use_hwaccel=False,
        export_settings=replace(ExportSettings.defaults(), codec="libx264", quality="custom", custom_quality=15))
    worker._probe_smart_media_signature = lambda p: {"width": 160, "height": 90}
    segment = {"source_id": "intro", "v_idx": 1, "a_idx": None, "v_in": 10., "v_out": 10.4,
               "a_in": 10., "a_out": 10.4, "duration": .4}
    temporary = []
    command, total = worker._build_cmd_filter_segments([segment], temporary, .4,
        input_preseek={1: 8.}, force_fps=30., lossless_audio=True, tmp_dir=str(tmp_path))
    assert [command[i+1] for i, arg in enumerate(command) if arg == "-i"] == ["intro.mp4"]
    assert command[command.index("-ss")+1] == "8.000"
    assert command[command.index("-crf")+1] == "15"
    assert command[command.index("-c:a")+1] == "alac"
    graph = Path(temporary[-1]).read_text()
    assert "[0:v]" in graph and "[1:v]" not in graph
    assert "trim=duration=0.400000000" in graph
    assert total == .4


def test_packaging_games_from_selected_intervals_not_parent_detector(tmp_path):
    from analysis.hero_groups.projects import prepare_selected_outputs
    from analysis.packaging.games import games_from_project
    from analysis.packaging.timeline import EditMap
    from core.project_file import normalize_project_payload
    from test_multi_source_export import multi_payload
    from youtube_packaging_fakes import planner

    plans, _ = prepare_selected_outputs(multi_payload(tmp_path), tmp_path / "outputs")
    ids = set()
    for output in plans:
        payload = normalize_project_payload(json.loads(Path(output["project"]).read_text()))
        edit_map = EditMap.from_project(output["project"], planner=planner())
        contexts = games_from_project(payload, edit_map)
        assert len(contexts) == (4 if output["group"] == "mage" else 2)
        assert len({g.game_id for g in contexts}) == len(contexts)  # repeated IDs in different VODs
        assert not ids.intersection(g.game_id for g in contexts)
        ids.update(g.game_id for g in contexts)
        assert all(g.result == "WIN" and g.origin == "export_selection" for g in contexts)
        assert all(g.edited_start >= 2 for g in contexts)  # intro occupies its actual output time
        assert all(":" not in g.game_id for g in contexts)  # Windows-safe diagnostic paths


def test_selected_packaging_frames_are_only_this_class_never_intro(tmp_path):
    from analysis.hero_groups.projects import prepare_selected_outputs
    from analysis.packaging.outputs import package_outputs
    from analysis.packaging.pipeline import PackagingPipeline
    from test_multi_source_export import multi_payload
    from youtube_packaging_fakes import FakeEngine, FakeSampler, fake_rms, fake_scanner, planner

    plans, _ = prepare_selected_outputs(multi_payload(tmp_path), tmp_path / "outputs")
    runs = []
    def factory(settings, **kwargs):
        settings = replace(settings, analyze_only=True, thumbnail_engine="v2", webcam_roi="none",
                           cache_dir=str(tmp_path / "cache"))
        run = PackagingPipeline(settings, transcription_engine=FakeEngine(), frame_sampler=FakeSampler(),
            rms_extractor=fake_rms, keyframe_scanner=fake_scanner, planner=planner(), **kwargs)
        runs.append(run)
        return run
    results, warnings = package_outputs(plans, {}, pipeline_factory=factory)
    assert not warnings and len(results) == len(runs) == 2
    for output, run in zip(plans, runs):
        records = run.frames.records.values()
        assert records
        assert all(run.frames._contains(r.source_index, r.source_time) for r in records)
        assert all(Path(run.frames.edit_map.sources[r.source_index]).name != "intro.mp4" for r in records)
        times = [r.source_time for r in records]
        assert all((195 <= t < 295) if output["group"] == "death_knight" else (95 <= t < 195 or 295 <= t < 395)
                   for t in times)


def test_many_keeps_with_intro_render_all_gameplay_in_bounded_graphs(tmp_path, monkeypatch):
    from analysis.hero_groups.projects import prepare_selected_outputs, export_outputs
    from automation.manager import PipelineManager
    from automation.store import PipelineStore
    from export.exporter import ExportWorker
    from utils.ffmpeg import ensure_ffmpeg, ffprobe_duration_seconds
    from utils.subprocess_utils import run_no_window

    ffmpeg, ffprobe = ensure_ffmpeg()
    gameplay, intro = tmp_path / "game.mp4", tmp_path / "intro.mp4"
    for destination, color, duration in ((gameplay, "blue", 72), (intro, "red", 1)):
        result = run_no_window([ffmpeg, "-v", "error", "-f", "lavfi", "-i",
            f"color={color}:s=64x48:r=30:d={duration}", "-f", "lavfi", "-i",
            f"sine=frequency=440:sample_rate=48000:duration={duration}", "-c:v", "libx264",
            "-preset", "ultrafast", "-c:a", "aac", "-y", str(destination)],
            capture_output=True, timeout=20)
        assert result.returncode == 0, result.stderr
    keeps = [{"start": 1+i*.2, "end": 1+i*.2+.1} for i in range(350)]
    game = {"id": "game-1", "index": 1, "start": 1, "end": 71, "result": "WIN"}
    track = {"path": str(gameplay), "source_id": "game", "source_role": "gameplay", "duration": 72,
        "cuts_enabled": True, "keeps": keeps, "gameplay_mode": "keep",
        "gameplay_keeps": [{"start": 1, "end": 71}],
        "gameplay_report": {"games": [game], "keep_islands": [{"start": 1, "end": 71, "game": game}]},
        "gameplay_grouping": {"games": [{"key": "game-1", "game": game, "group": "mage"}],
                             "groups": {"mage": {"class_name": "mage"}}, "selected": ["mage"]}}
    payload = {"format": "autocutter_project", "version": 2, "global": {"export_settings": {
        "codec": "libx264", "method": "filter_concat", "parallel_workers": 1,
        "hwaccel_decode": False, "quality": "high", "cut_quality": "higher"}},
        "tracks": [track, {"path": str(intro), "source_id": "intro", "source_role": "intro",
        "intro_targets": ["mage"], "duration": .4, "cuts_enabled": False}]}
    seen = []
    original = ExportWorker._build_cmd_filter_segments
    def inspect(self, segments, *args, **kwargs):
        seen.append((len(segments), {s["v_idx"] for s in segments}))
        return original(self, segments, *args, **kwargs)
    monkeypatch.setattr(ExportWorker, "_build_cmd_filter_segments", inspect)
    plans, _ = prepare_selected_outputs(payload, tmp_path / "outputs")
    details = []
    result = export_outputs(plans, manager=PipelineManager(PipelineStore(tmp_path / "jobs.json")),
                            on_detail=details.append)[0]
    assert max(n for n, _ in seen) <= 40 and len(seen) >= 10
    assert all(len(sources) == 1 for _, sources in seen)
    assert abs(ffprobe_duration_seconds(result["output"])-35.4) < .1
    assert any("bounded_mp4_chunks" in line for line in details)
    for t, channel in ((.2, 0), (10., 2), (35., 2)):
        raw = run_no_window([ffmpeg, "-v", "error", "-ss", str(t), "-i", result["output"],
            "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"],
            capture_output=True, timeout=10)
        image = np.frombuffer(raw.stdout, np.uint8).reshape(48, 64, 3)
        assert image.mean(axis=(0, 1)).argmax() == channel
    raw = run_no_window([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
        "frame=best_effort_timestamp_time", "-of", "json", result["output"]],
        capture_output=True, text=True, timeout=10)
    times = [float(f["best_effort_timestamp_time"]) for f in json.loads(raw.stdout)["frames"]]
    assert np.all(np.diff(times) > 0) and max(np.diff(times)) < .04
    audio = run_no_window([ffmpeg, "-v", "error", "-i", result["output"], "-vn", "-ac", "1",
        "-ar", "48000", "-f", "f32le", "pipe:1"], capture_output=True, timeout=10)
    assert audio.returncode == 0
    samples = np.frombuffer(audio.stdout, np.float32)
    for boundary in (4.4, 8.4, 12.4, 32.4, 35.):
        window = samples[int((boundary-.02)*48000):int((boundary+.02)*48000)]
        assert np.sqrt(np.mean(window**2)) > .03  # no silent gap at bounded joins


def test_gui_single_export_action():
    from ui.main_window import MainWindow

    window = MainWindow()
    window._startup_auto_restore_pending = False
    settings = replace(ExportSettings.defaults(), codec="libx264", quality="high", cut_quality="higher",
                       parallel_workers=3, audio_bitrate_kbps=256)
    window._apply_export_settings_to_ui(settings)
    window.project.gameplay_export = {"mode": "by_class", "wins_only": True}
    window.hero_groups_panel.refresh()
    assert window._export_settings_from_ui() == settings
    assert window.hero_groups_panel.mode.currentData() == "by_class"
    assert window.btn_export.text() == "Export videos by class…"
    assert window.hero_groups_panel.export.isHidden()  # no duplicate render CTA
    window.hero_groups_panel.mode.setCurrentIndex(window.hero_groups_panel.mode.findData("selected_single"))
    assert window.btn_export.text() == "Export selected classes…"
    window._project_dirty = False
    window.close()


def test_thumbnail_covers_only_for_ticked_class_videos(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import ui.thumbnails_page as module
    from PySide6.QtWidgets import QCheckBox, QComboBox, QMessageBox
    from test_packaging_outputs import thumbnail_owner
    from test_multi_source_export import multi_payload

    monkeypatch.setenv("AUTO_CUTTER_OUTPUT_DIR", str(tmp_path / "exports"))
    payload = multi_payload(tmp_path)
    owner = thumbnail_owner()
    owner._build_project_file_payload = lambda: payload
    mode = QComboBox()
    mode.addItem("By class", "by_class")
    wins = QCheckBox()
    wins.setChecked(True)
    owner.hero_groups_panel = SimpleNamespace(filter_enabled=lambda: True, mode=mode, wins_only=wins)
    controller = owner.thumbnails
    controller._update_hint()
    boxes = {b.property("output_group"): b for b in owner.thumb_class_box.findChildren(QCheckBox)}
    assert set(boxes) == {"mage", "death_knight"} and not owner.thumb_class_row.isHidden()
    boxes["death_knight"].setChecked(False)
    assert "covers for 1 of 2 videos (Mage)" in owner.lbl_thumb_hint.text()
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    started = []
    monkeypatch.setattr(module, "_start", lambda owner, worker, *args: (
        started.append(worker) or SimpleNamespace(finished=SimpleNamespace(connect=lambda *a: None))))
    controller.generate()
    assert [o["group"] for o in started[0].outputs] == ["mage"]
    controller._thread = controller._worker = None
    boxes["mage"].setChecked(False)
    messages = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a: messages.append(a[2]))
    controller.generate()
    assert len(started) == 1 and "Tick at least one video" in messages[0]
    owner.close()


def test_thumbnail_generate_follows_by_class_plan_without_rendering(tmp_path, monkeypatch):
    import ui.thumbnails_page as module
    import analysis.packaging.outputs as packaging
    from PySide6.QtWidgets import QComboBox, QCheckBox, QMessageBox
    from test_packaging_outputs import thumbnail_owner
    from test_multi_source_export import multi_payload

    monkeypatch.setenv("AUTO_CUTTER_OUTPUT_DIR", str(tmp_path / "exports"))
    payload = multi_payload(tmp_path)
    owner = thumbnail_owner()
    owner._build_project_file_payload = lambda: payload
    mode = QComboBox()
    mode.addItem("By class", "by_class")
    wins = QCheckBox()
    wins.setChecked(True)
    owner.hero_groups_panel = SimpleNamespace(filter_enabled=lambda: True, mode=mode, wins_only=wins)
    controller = owner.thumbnails
    assert owner.thumb_source_combo.currentData() == "project"
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    started = []
    class Signal:
        def connect(self, *args):
            pass
    monkeypatch.setattr(module, "_start", lambda owner, worker, *args: (
        started.append(worker) or SimpleNamespace(finished=Signal())))
    controller.generate()
    assert len(started) == 1
    worker = started[0]
    assert isinstance(worker, module.PackagingBatchWorker)
    assert {o["group"] for o in worker.outputs} == {"mage", "death_knight"}
    assert all(not Path(o["output"]).exists() and o["render_status"] == "not_rendered" for o in worker.outputs)
    seen = []
    def package(outputs, options, **kwargs):
        for output in outputs:
            kwargs["on_result"](output)
        seen.extend(outputs)
        return outputs, []
    monkeypatch.setattr(packaging, "package_outputs", package)
    worker.run()
    APP.processEvents()
    assert len(seen) == 2 and len(owner.project.packaging_outputs) == 2
    assert all(json.loads(Path(o["project"]).read_text())["export_selection"]["output"] == o["group"] for o in seen)
    controller._thread = controller._worker = None
    owner.close()


def test_intro_chunk_cache_depends_on_gameplay_normalization_reference(tmp_path):
    from export.exporter import ExportWorker
    worker = ExportWorker(ffmpeg_path="ffmpeg", input_path="game.mp4",
        input_paths=["game.mp4", "intro.mp4"], output_path=str(tmp_path / "out.mp4"),
        keeps=[], codec="libx264", use_hwaccel=False,
        export_settings=replace(ExportSettings.defaults(), codec="libx264", pixel_depth="8"))
    worker._source_signature = lambda filename: {"source": filename}
    worker._ffmpeg_version_tag = lambda: "fixture"
    segment = {"source_id": "intro", "v_idx": 1, "a_idx": None, "v_in": 0., "v_out": 1.,
               "a_in": 0., "a_out": 1., "duration": 1.}
    first = worker._chunk_signature_segments([segment], 30., "mp4", False)
    worker.input_paths[0] = "different-resolution-game.mp4"
    second = worker._chunk_signature_segments([segment], 30., "mp4", False)
    assert first != second


def test_thumbnail_scope_change_clears_previous_output_preview():
    from test_packaging_outputs import thumbnail_owner
    owner = thumbnail_owner()
    controller = owner.thumbnails
    controller.output = "previous-class-packaging"
    owner.thumb_results.setVisible(True)
    owner.thumb_source_combo.setCurrentIndex(owner.thumb_source_combo.findData("video"))
    assert controller.output is None and owner.thumb_results.isHidden()
    owner.close()
