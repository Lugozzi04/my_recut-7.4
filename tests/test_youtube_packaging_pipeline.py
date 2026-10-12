"""AI YouTube Packaging: end-to-end pipeline and CLI with fake media, ASR and agent."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from analysis.boundaries.agents import AgentCallError
from analysis.packaging.frames import read_image
from analysis.packaging.models import PackagingError
from analysis.packaging.pipeline import PackagingPipeline, PackagingSettings
from automation.cli import run_cli

from youtube_packaging_fakes import (WEBCAM_ROI, FakeAgent, FakeEngine, FakeSampler, failing_rms, fake_rms,
                                     fake_scanner, planner, write_project)


def make(tmp_path: Path, *, agent=None, engine=None, rms=fake_rms, sampler=None, **settings):
    media = tmp_path / "video.mp4"
    if not media.exists():
        media.write_bytes(b"x" * 2048)
    base: dict[str, Any] = dict(output=str(tmp_path / "out"), video=str(media), webcam_roi=WEBCAM_ROI, titles=6, thumbnails=3,
                thumbnail_engine="v2",  # this file is the V2-engine regression suite (V3: test_youtube_packaging_v3.py)
                cache_dir=str(tmp_path / "cache"), style_dir=str(tmp_path / "styles"), max_deepen=0)
    base.update(settings)
    pipeline = PackagingPipeline(PackagingSettings(**base), agent=cast(Any, agent if agent is not None else FakeAgent()),
                                 transcription_engine=engine or FakeEngine(), rms_extractor=rms,
                                 keyframe_scanner=fake_scanner, frame_sampler=sampler or FakeSampler(),
                                 duration_probe=lambda path: 330.0, planner=planner())
    return pipeline


def test_mp4_without_project_produces_every_artifact_and_three_real_thumbnails(tmp_path):
    agent = FakeAgent()
    report = make(tmp_path, agent=agent).run()
    out = tmp_path / "out"
    assert report["status"] == "done"
    for name in ("content_analysis.json", "transcript.json", "game_contexts.json", "highlights.json",
                 "video_story.json", "title_candidates.json", "thumbnail_concepts.json", "packaging_report.json"):
        assert (out / name).is_file(), name
    for folder in ("frames", "webcam", "cards", "contact_sheets", "thumbnails"):
        assert any((out / folder).iterdir()), folder
    pngs = sorted((out / "thumbnails").glob("concept_*.png"))
    assert len(pngs) == 3
    for png in pngs:
        image = read_image(png)
        assert image is not None and image.shape[:2] == (720, 1280)
    assert [c["task"] for c in agent.calls] == ["packaging_fast"]  # FAST path: one agent call
    assert len(report["titles"]) == 6 and report["preferred_title"]["title_id"] == "T1"
    assert report["privacy"] and "provider" in report["privacy"]
    assert {"transcription", "audio_analysis", "highlight_discovery", "frame_extraction", "brief", "agent_fast",
            "thumbnail_render", "total"} <= set(report["timings_s"])
    assert report["mode"] == "fast" and report["agent"]["effort"] == "medium" and report["brief"]["images"] <= 6
    highlights = json.loads((out / "highlights.json").read_text(encoding="utf-8"))["highlights"]
    assert all(h["game_id"] == "video" for h in highlights) and all(h["frame_ids"] for h in highlights)
    concepts = json.loads((out / "thumbnail_concepts.json").read_text(encoding="utf-8"))["concepts"]
    assert all(c["render"]["text"]["fits"] for c in concepts)
    assert (out / "thumbnails" / "overview.jpg").is_file()


def test_project_with_two_wins_and_silence_cuts_only_uses_kept_content(tmp_path):
    media = tmp_path / "video.mp4"
    media.write_bytes(b"x" * 2048)
    project = write_project(tmp_path, media)
    report = make(tmp_path, video=None, project=str(project)).run()
    out = tmp_path / "out"
    games = json.loads((out / "game_contexts.json").read_text(encoding="utf-8"))["games"]
    assert [(g["game_id"], g["result"]) for g in games] == [("g-1", "WIN"), ("g-3", "WIN")]
    assert all(g["interpretation"] for g in games)  # enriched by the story
    frames = json.loads((out / "content_analysis.json").read_text(encoding="utf-8"))["frames"]
    assert frames and all(f["edited_time"] is not None for f in frames)  # never a frame from removed content
    lines = json.loads((out / "transcript.json").read_text(encoding="utf-8"))["lines"]
    kept = [(20.0, 60.0), (62.0, 150.0), (151.0, 200.0), (400.0, 470.0), (472.0, 560.0)]
    for line in lines:
        for w in line["words"]:
            middle = (w["source_start"] + w["source_end"]) / 2
            assert any(a - 1e-3 <= middle <= b + 1e-3 for a, b in kept)
    highlights = json.loads((out / "highlights.json").read_text(encoding="utf-8"))["highlights"]
    assert {h["game_id"] for h in highlights} == {"g-1", "g-3"}
    assert report["evidence"]["games"] == 2


def test_analyze_only_sends_nothing(tmp_path):
    agent = FakeAgent()
    report = make(tmp_path, agent=agent, analyze_only=True).run()
    assert report["status"] == "analyzed" and agent.calls == [] and report["privacy"] is None
    assert not (tmp_path / "out" / "video_story.json").exists()


def test_existing_runs_are_never_overwritten_silently(tmp_path):
    make(tmp_path).run()
    with pytest.raises(PackagingError) as error:
        make(tmp_path).run()
    assert error.value.code == "output_exists" and error.value.exit_code == 2
    report = make(tmp_path, overwrite=True).run()
    history = list((tmp_path / "out" / "history").iterdir())
    assert len(history) == 1 and (history[0] / "packaging_report.json").is_file()
    assert (history[0] / "thumbnails" / "concept_01.png").is_file()
    assert any("archived" in w for w in report["warnings"])


def test_new_thumbnail_text_rerenders_without_reanalysis_or_agent(tmp_path):
    make(tmp_path).run()
    out = tmp_path / "out"
    concepts = json.loads((out / "thumbnail_concepts.json").read_text(encoding="utf-8"))
    before = (out / "thumbnails" / "concept_01.png").read_bytes()
    concepts["concepts"][0]["thumbnail_text"] = "Testo nuovo"
    (out / "thumbnail_concepts.json").write_text(json.dumps(concepts), encoding="utf-8")
    agent, sampler, engine = FakeAgent(), FakeSampler(), FakeEngine()
    report = make(tmp_path, agent=agent, sampler=sampler, engine=engine, only="render", video=None).run()
    assert agent.calls == [] and sampler.calls == 0 and engine.calls == 0
    assert report["status"] == "done"
    assert (out / "thumbnails" / "concept_01.png").read_bytes() != before
    updated = json.loads((out / "thumbnail_concepts.json").read_text(encoding="utf-8"))["concepts"][0]
    assert updated["render"]["text"]["lines"] == ["TESTO NUOVO"] or updated["render"]["text"]["lines"] == ["TESTO", "NUOVO"]
    assert any((out / "history").iterdir())


def test_regenerate_titles_only_reuses_story_and_evidence(tmp_path):
    make(tmp_path).run()
    agent, sampler, engine = FakeAgent(), FakeSampler(), FakeEngine()
    report = make(tmp_path, agent=agent, sampler=sampler, engine=engine, only="titles", video=None).run()
    assert [c["task"] for c in agent.calls] == ["creative_titles"]
    assert sampler.calls == 0 and engine.calls == 0 and report["status"] == "done"


def test_agent_failure_keeps_local_artifacts_and_invents_nothing(tmp_path):
    agent = FakeAgent(fail=AgentCallError("timeout", "no answer within 900s"))
    with pytest.raises(PackagingError) as error:
        make(tmp_path, agent=agent).run()
    out = tmp_path / "out"
    assert error.value.code == "agent_timeout"
    assert (out / "content_analysis.json").is_file() and (out / "highlights.json").is_file()
    report = json.loads((out / "packaging_report.json").read_text(encoding="utf-8"))
    assert report["status"] == "agent_failed" and report["titles"] == []
    assert not (out / "title_candidates.json").exists()


def test_optional_evidence_failures_degrade_gracefully(tmp_path):
    report = make(tmp_path, rms=failing_rms, engine=FakeEngine(status="unavailable"),
                  webcam_roi="2,2,0.1,0.1").run()
    text = " ".join(report["warnings"])
    assert report["status"] == "done"
    assert "audio evidence failed" in text and "transcript unavailable" in text and "webcam disabled" in text
    assert report["evidence"]["webcam_frames"] == 0 and report["evidence"]["highlights"] >= 1


def test_invalid_settings_are_rejected(tmp_path):
    for bad in (dict(titles=3), dict(thumbnails=9), dict(provider="gpt"), dict(only="everything")):
        with pytest.raises(PackagingError) as error:
            make(tmp_path, **bad)
        assert error.value.exit_code == 2


# ---------- CLI ----------

def run(args, env):
    out, err = io.StringIO(), io.StringIO()
    with patch.dict(os.environ, env):
        code = run_cli(args, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def test_cli_rejects_bad_arguments_without_crashing(tmp_path):
    code, out, _ = run(["packaging", "--video", str(tmp_path / "x.mp4"), "--titles", "3", "--json"],
                       {"AUTO_CUTTER_CACHE_DIR": str(tmp_path)})
    assert code == 2 and json.loads(out)["error"]["code"] == "invalid_arguments"
    code, out, _ = run(["packaging", "--json"], {})
    assert code == 2


def test_cli_style_profile_and_feedback_round_trip(tmp_path):
    env = {"AUTO_CUTTER_CONFIG_DIR": str(tmp_path / "config")}
    code, out, _ = run(["packaging-style", "create", "Lugozzi", "--channel", "Lugozzi", "--json"], env)
    assert code == 0 and json.loads(out)["profile"]["revision"] == 1
    titles = tmp_path / "titles.txt"
    titles.write_text("Primo titolo di esempio\nSecondo titolo?\n", encoding="utf-8")
    code, out, _ = run(["packaging-style", "import", "Lugozzi", "--titles", str(titles), "--json"], env)
    assert code == 0 and json.loads(out)["profile"]["title_style"]["observed"]["examples"] == 2
    code, out, _ = run(["packaging-style", "set", "Lugozzi", "--webcam-roi", "0,0.5,0.2,0.3", "--json"], env)
    assert json.loads(out)["profile"]["brand"]["webcam_roi"] == [0.0, 0.5, 0.2, 0.3]
    code, out, _ = run(["packaging-style", "set", "Lugozzi", "--webcam-roi", "3,3,1,1", "--json"], env)
    assert code == 2
    make(tmp_path, creator="Lugozzi", style_dir=str(tmp_path / "config" / "packaging" / "creators")).run()
    code, out, _ = run(["packaging-feedback", "--output", str(tmp_path / "out"), "--select-title", "T2",
                        "--reject-thumbnail", "C2", "--note", "meno testo", "--json"], env)
    result = json.loads(out)
    assert code == 0 and result["creator"] == "Lugozzi" and result["profile_revision"] == 4
    selection = json.loads((tmp_path / "out" / "selection.json").read_text(encoding="utf-8"))
    assert selection["title"]["title_id"] == "T2"
    code, out, _ = run(["packaging-style", "show", "Lugozzi", "--json"], env)
    profile = json.loads(out)["profile"]
    assert profile["rejected"][0]["id"] == "C2" and profile["feedback"][-1]["note"] == "meno testo"
    code, _, err = run(["packaging-feedback", "--output", str(tmp_path / "out"), "--select-title", "T99"], env)
    assert code == 2 and "unknown title id" in err


# ---------- V2: fast / deep, call budget, quality gate ----------

def test_deep_mode_keeps_the_previous_multi_call_pipeline(tmp_path):
    agent = FakeAgent()
    report = make(tmp_path, agent=agent, mode="deep").run()
    assert [c["task"] for c in agent.calls] == ["video_story", "creative_all"] and report["mode"] == "deep"


def test_fast_deepen_is_one_patch_call_that_updates_only_one_concept(tmp_path):
    agent = FakeAgent(deepen=True)
    make(tmp_path, agent=agent, max_deepen=1).run()
    assert [c["task"] for c in agent.calls] == ["packaging_fast", "packaging_fast_patch"]
    patch_prompt = agent.calls[1]["prompt"]
    assert "TRANSCRIPT" not in patch_prompt and "QUESTION: which card?" in patch_prompt  # no story repeated
    concepts = json.loads((tmp_path / "out" / "thumbnail_concepts.json").read_text(encoding="utf-8"))["concepts"]
    assert concepts[1]["thumbnail_text"] == "Carta letta" and concepts[0]["thumbnail_text"] == "Che lettura"
    story = json.loads((tmp_path / "out" / "video_story.json").read_text(encoding="utf-8"))["story"]
    assert story["deepened"]["status"] == "applied"


def test_fast_repair_is_a_minimal_prompt_without_transcript(tmp_path):
    agent = FakeAgent(invalid_first=True)
    make(tmp_path, agent=agent).run()
    repair = agent.repair_prompts[0]
    assert "H99" in repair and "VALID IDS" in repair and "TRANSCRIPT" not in repair


def test_max_deepen_zero_never_patches(tmp_path):
    agent = FakeAgent(deepen=True)
    with pytest.raises(PackagingError):  # a request above the limit is rejected by validation
        make(tmp_path, agent=agent, max_deepen=0).run()
    assert [c["task"] for c in agent.calls] == ["packaging_fast"]


def test_quality_gate_results_are_recorded_for_every_thumbnail(tmp_path):
    make(tmp_path).run()
    concepts = json.loads((tmp_path / "out" / "thumbnail_concepts.json").read_text(encoding="utf-8"))["concepts"]
    for concept in concepts:
        gate = concept["render"]["quality"]
        assert set(gate) == {"passed", "issues", "fixes"}
        assert all(issue["code"] != "bad_size" for issue in gate["issues"])
    assert all(c["render"]["layout"] in ("reaction_focus", "board_focus", "story_focus") for c in concepts)


def test_codex_defaults_to_medium_effort_and_input_budget(tmp_path):
    settings = PackagingSettings(output=str(tmp_path / "o"), video=str(tmp_path / "v.mp4"),
                                 cache_dir=str(tmp_path / "c"))
    assert settings.effective_effort == "medium" and settings.deepen_limit == 1
    assert PackagingSettings(output="o", video="v.mp4", provider="claude").effective_effort is None
    assert PackagingSettings(output="o", video="v.mp4", mode="deep").deepen_limit == 2
    with pytest.raises(PackagingError):
        make(tmp_path, agent_effort="xhigh")
    cli = tmp_path / "codex.exe"
    cli.write_text("")
    with patch("analysis.packaging.agent.PackagingAgent.locate", return_value=str(cli)):
        agent = PackagingPipeline(settings)._make_agent()
    assert agent.settings.effort == "medium" and agent.settings.max_prompt_chars == 60000
    assert agent.settings.max_images == 6


def test_input_budget_refuses_oversized_prompts(tmp_path):
    from analysis.packaging.agent import AgentSettings, PackagingAgent
    from analysis.packaging.cache import PackagingCache

    agent = PackagingAgent(AgentSettings(max_prompt_chars=100), PackagingCache(tmp_path))
    with pytest.raises(AgentCallError) as error:
        agent.run("t", prompt="x" * 101, schema={}, system_prompt="s", prompt_version="v")
    assert error.value.code == "budget_exceeded"


def test_cli_exposes_mode_and_bounded_effort(tmp_path):
    from automation.cli import build_parser

    args = build_parser().parse_args(["packaging", "--video", "v.mp4", "--mode", "deep", "--agent-effort", "high"])
    assert args.mode == "deep" and args.agent_effort == "high" and args.titles == 6
    code, _, _ = run(["packaging", "--video", "v.mp4", "--agent-effort", "xhigh"], {})
    assert code == 2
