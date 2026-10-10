"""End-to-end automation wiring for smart gameplay editing (no detector/ASR/agent/export actually run)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from analysis.cut_engine import Segment
from automation.analyzer import PipelineProjectAnalyzer
from automation.cli import _implicit_command
from automation.runtime import PipelineRuntime
from core.presets import match_preset_name
from core.project_file import normalize_project_payload
from export.settings import ExportSettings
import tests.test_cli as cli_tests
from tests.test_pipeline_analyzer import fake_audio_analysis


@pytest.fixture
def cli():
    helper = cli_tests.CliTests("test_presets_json_is_actual_catalog")
    helper.setUp()
    yield helper
    helper.doCleanups()


def plan_job(cli, **gameplay):
    runtime = cli.factory(on_event=lambda event: None)
    options = {"game": "hearthstone", "keep_games": "wins", "boundary_mode": "exact", **gameplay}
    return runtime, runtime.create_job(runtime.plan_local(cli.source, gameplay=options))


# ---------- CLI ----------

def test_bare_video_path_is_a_first_class_local_input(cli):
    assert _implicit_command([str(cli.source), "--json"])[0] == "process"
    assert _implicit_command(["vod.mp4"])[:2] == ["process", "vod.mp4"]
    assert _implicit_command(["https://www.twitch.tv/videos/123"])[0] == "vod"
    assert _implicit_command(["unknown"]) == ["unknown"]  # still an argument error
    assert _implicit_command(["presets"]) == ["presets"]
    code, out, _err = cli.invoke([str(cli.source), "--gameplay", "hearthstone", "--json"])
    assert code == 0 and json.loads(out)["command"] == "process"


def test_gameplay_flags_reach_the_job_and_remote_mode_is_announced(cli):
    code, out, err = cli.invoke(["process", str(cli.source), "--preset", "Balanced", "--gameplay", "hearthstone",
                                 "--keep-games", "wins", "--boundary-mode", "codex", "--agent-timeout", "90",
                                 "--agent-effort", "low", "--json"])
    assert code == 0, err
    job = json.loads(out)["job"]
    assert job["metadata"]["preset"]["name"] == "Balanced (Default)"
    gameplay = job["metadata"]["gameplay"]
    assert (gameplay["game"], gameplay["keep_games"], gameplay["boundary_mode"]) == ("hearthstone", "wins", "codex")
    assert gameplay["resolver"]["timeout_s"] == 90.0 and gameplay["resolver"]["effort"] == "low"
    assert job["metadata"]["delivery"]["gameplay_report_path"].endswith(".gameplay_report.json")
    assert "sends the local transcript" in err
    code, _out, err = cli.invoke(["process", str(cli.source), "--gameplay", "hearthstone", "--json"])
    assert code == 0 and "sends the local transcript" not in err  # exact sends nothing


def test_gameplay_options_require_gameplay_and_are_validated(cli):
    code, out, _err = cli.invoke(["process", str(cli.source), "--boundary-mode", "codex", "--json"])
    assert code == 2 and "require --gameplay" in json.loads(out)["error"]["message"]
    code, _out, _err = cli.invoke(["process", str(cli.source), "--gameplay", "chess", "--json"])
    assert code == 2
    code, out, _err = cli.invoke(["process", str(cli.source), "--gameplay", "hearthstone", "--agent-timeout", "0", "--json"])
    assert code == 2 and json.loads(out)["error"]["code"] == "invalid_gameplay"


def test_dry_run_shows_gameplay_plan_without_creating_a_job(cli):
    code, out, _err = cli.invoke(["process", str(cli.source), "--gameplay", "hearthstone", "--dry-run", "--json"])
    payload = json.loads(out)
    assert code == 0 and payload["dry_run"] and payload["plan"]["gameplay"]["boundary_mode"] == "exact"
    assert cli.manager.list_jobs() == []


def test_analyze_only_stops_before_rendering(cli):
    def factory(*, on_event):
        runtime = cli.factory(on_event=on_event)

        def run(job_id, *, resume=False, cancellation=None, stop_after_analysis=False):
            assert stop_after_analysis is True
            project = cli.root / (job_id + ".autocutter")
            project.write_text("project", encoding="utf-8")
            job = cli.manager.mark_analyzed(job_id, project)
            return cli.manager.update_metadata(job.id, {"analysis_summary": {"kept_games": [2]},
                                                         "gameplay_report_path": "r.json"})

        runtime.run = run
        return runtime

    code, out, _err = cli.invoke(["process", str(cli.source), "--gameplay", "hearthstone", "--analyze-only", "--json"],
                                 factory=factory)
    payload = json.loads(out)
    assert code == 0 and payload["analyze_only"] and payload["job"]["state"] == "ready_export"
    assert payload["summary"] == {"kept_games": [2]} and payload["report"] == "r.json"


def test_preset_name_matching_is_shared_and_unambiguous():
    catalog = {"Balanced (Default)": {}, "Natural Speech": {}, "Aggressive Cleanup": {}}
    assert match_preset_name(catalog, "Balanced") == "Balanced (Default)"
    assert match_preset_name(catalog, "natural speech") == "Natural Speech"
    with pytest.raises(KeyError):
        match_preset_name(catalog, "Turbo")
    with pytest.raises(KeyError, match="Ambiguous"):
        match_preset_name({"Gameplay A": {}, "Gameplay B": {}}, "Gameplay")


# ---------- runtime: analyze-only with the real state machine ----------

def test_runtime_stop_after_analysis_never_calls_export(cli):
    exported = []

    class Analysis:
        def __init__(self, manager):
            self.manager = manager

        def execute(self, job_id, *, cancellation=None, on_progress=None):
            project = cli.root / "p.autocutter"
            project.write_text("project", encoding="utf-8")
            return self.manager.mark_analyzed(job_id, project)

    runtime = PipelineRuntime(cli.manager, cli.presets, None, duration_probe=lambda _p: 10.0,
                              video_probe=lambda _p: True, audio_probe=lambda _p: True, settings_loader=lambda: {},
                              metadata_resolver=SimpleNamespace(metadata=lambda url: {}),
                              analysis_service_factory=Analysis,
                              export_service_factory=lambda manager: exported.append(manager))
    job = runtime.create_job(runtime.plan_local(cli.source, gameplay={"game": "hearthstone"}))
    with patch.object(cli.manager, "reconcile_artifacts", side_effect=lambda job_id, **kw: cli.manager.get(job_id)):
        done = runtime.run(job.id, stop_after_analysis=True)
    assert done.state.value == "ready_export" and exported == []
    assert "analysis" in done.metadata["stage_timings"]


# ---------- analysis stage: composition, project, report ----------

class FakeEdit:
    """Keep-based result: one WIN island [1, 5.5] in a 6 s file (fake_audio_analysis: classic cut 4.34-5.13)."""

    def __init__(self, islands=((1.0, 5.5),)):
        self.gameplay_keeps = [Segment(a, b) for a, b in islands]
        edges = [0.0, *[x for island in islands for x in island], 6.0]
        self.gameplay_cuts = [Segment(edges[i], edges[i + 1]) for i in range(0, len(edges), 2) if edges[i + 1] > edges[i]]

    def report(self):
        return {"format": "recut_gameplay_edit_report", "settings": {"boundary_mode": "exact"},
                "games": [{"index": 1, "result": "LOSS", "termination": "COMPLETE", "disposition": "REMOVE"},
                          {"index": 2, "result": "WIN", "termination": "COMPLETE", "disposition": "KEEP"}],
                "keep_islands": [{"start": k.start, "end": k.end} for k in self.gameplay_keeps],
                "gameplay_keeps": [{"start": k.start, "end": k.end} for k in self.gameplay_keeps],
                "gameplay_cuts": [{"start": c.start, "end": c.end} for c in self.gameplay_cuts],
                "timings": {"gameplay_detection_s": 1.0}, "warnings": []}


def test_analysis_stage_keeps_only_win_islands_and_applies_classic_cuts_inside(cli):
    _runtime, job = plan_job(cli)
    calls = []

    def run(self, video, *, cancellation=None, on_progress=None):
        calls.append(Path(video))
        on_progress(50)
        return FakeEdit()

    with patch("analysis.gameplay_editing.GameplayEditingService.run", run):
        result = PipelineProjectAnalyzer(analyze_fn=fake_audio_analysis).analyze(job)
    assert calls == [cli.source.resolve()]
    payload = normalize_project_payload(json.loads(result.path.read_text(encoding="utf-8")))
    track = payload["tracks"][0]
    assert track["gameplay_mode"] == "keep" and track["gameplay_keeps"] == [{"start": 1.0, "end": 5.5}]
    assert track["gameplay_cuts"] == [{"start": 0.0, "end": 1.0}, {"start": 5.5, "end": 6.0}]  # derived complement
    assert all(k["start"] >= 1.0 and k["end"] <= 5.5 for k in track["keeps"])  # nothing outside the WIN island
    assert track["classic_cuts"] == track["cuts"] and track["manual_cuts"] == []
    assert payload["gameplay"]["mode"] == "keep" and payload["gameplay"]["gameplay_keeps"] == [{"start": 1.0, "end": 5.5}]
    report = json.loads(Path(result.summary["report_path"]).read_text(encoding="utf-8"))
    composition = report["composition"]
    assert composition["effective_cuts"] == track["cuts"] and composition["final_keeps"] == track["keeps"]
    assert composition["classic_cuts_inside_keeps"] == [{"start": 4.34, "end": 5.13}]
    assert composition["final_keeps"] != composition["gameplay_keeps"]  # classic cuts really shrink the island
    assert "classic_analysis_s" in report["timings"]
    assert result.summary["kept_games"] == [2] and result.summary["gameplay_keeps"] == [[1.0, 5.5]]
    with patch("analysis.gameplay_editing.GameplayEditingService.run", run):
        assert PipelineProjectAnalyzer(analyze_fn=fake_audio_analysis).analyze(cli.manager.get(job.id)).reused
    assert len(calls) == 1


def test_no_complete_win_fails_instead_of_exporting_nothing(cli):
    from automation.analyzer import PipelineAnalysisError

    _runtime, job = plan_job(cli)
    with patch("analysis.gameplay_editing.GameplayEditingService.run", lambda self, video, **kw: FakeEdit(islands=())),             pytest.raises(PipelineAnalysisError) as error:
        PipelineProjectAnalyzer(analyze_fn=fake_audio_analysis).analyze(job)
    assert error.value.code == "no_publishable_games"


def test_gameplay_failure_fails_analysis_instead_of_publishing_unfiltered(cli):
    from automation.analyzer import PipelineAnalysisError

    _runtime, job = plan_job(cli)

    def broken(self, video, **kwargs):
        raise FileNotFoundError("profile missing")

    with patch("analysis.gameplay_editing.GameplayEditingService.run", broken), pytest.raises(PipelineAnalysisError) as error:
        PipelineProjectAnalyzer(analyze_fn=fake_audio_analysis).analyze(job)
    assert error.value.code == "gameplay_analysis_failed"


def test_legacy_jobs_keep_their_analysis_signature_and_project_shape(cli):
    runtime = cli.factory(on_event=lambda event: None)
    job = runtime.create_job(runtime.plan_local(cli.source))
    assert "gameplay" not in job.metadata
    preset = PipelineProjectAnalyzer._preset_for_job(job)
    settings = ExportSettings.from_mapping(job.metadata["export_settings"]).to_mapping()
    legacy = hashlib.sha256(json.dumps({"preset": preset, "export_settings": settings,
                                        "source_range": PipelineProjectAnalyzer._source_range(job)},
                                       sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    assert PipelineProjectAnalyzer._request_signature(job, preset) == legacy
    result = PipelineProjectAnalyzer(analyze_fn=fake_audio_analysis).analyze(job)
    track = normalize_project_payload(json.loads(result.path.read_text(encoding="utf-8")))["tracks"][0]
    assert "gameplay_cuts" not in track and result.summary is None
