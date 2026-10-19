"""Thumbnail V4 offline regressions: no real CLI, ASR, video or image-model call."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import json

import numpy as np
import pytest

from analysis.packaging.direction import CreativeDirection, relevance
from analysis.packaging.design import design_pack, pack_prompt, validate_design, planner_schema
from analysis.packaging.frames import read_image, write_image
from analysis.packaging.style import CreatorStyleStore, competitive_profile
from analysis.packaging.typography import compose, normalize_canvas
from analysis.packaging.cache import PackagingCache
from analysis.packaging.edit import ThumbnailEditor
from analysis.packaging.pipeline import PackagingPipeline
from analysis.packaging.timeline import EditMap, KeepPiece
from analysis.packaging.models import GameContext
from test_youtube_packaging_gen import make_v4 as make
from youtube_packaging_fakes import FakeAgentGen, FakeGenerator


class V4Agent(FakeAgentGen):
    def packages(self, prompt):
        value = super().packages(prompt)
        for c in value["concepts"]:
            selected = [a for ids in c["package"].values() for a in ids]
            c["design"] = {
                "story_angle": c["narrative_angle"], "evidence_ids": c["highlight_ids"],
                "story_confidence": "interpretation", "main_subject": selected[0], "secondary_subject": "",
                "must_use_assets": selected, "optional_assets": [], "must_not_use_assets": [],
                "layout_archetype": "reaction_impact" if c["role"] == "reaction-led" else "card_power",
                "visual_hierarchy": selected, "text_position": "top", "typography_mode": "auto",
                "typography_preset": "Anton", "color_preset": "yellow_dark",
                "lighting_style": "rim light", "emotion_or_tone": "competitive",
                "effect_intensity": 3, "composition_notes": "Keep subjects below text safe area",
                "why_clickable": "One supported decisive moment",
            }
        return value


@pytest.fixture
def v4_run(tmp_path):
    agent, generator = V4Agent(), FakeGenerator()
    pipeline = make(tmp_path, agent=agent, generator=generator,
                    creative_direction={"mode": "guided", "creative_focus": "Hunter combo",
                                        "youtube_title": "La mia partita", "title_locked": True,
                                        "thumbnail_hook": "È ROTTO?!", "hook_locked": True})
    report = pipeline.run()
    return pipeline, report, agent, generator


def test_direction_validation_automatic_and_serialization():
    automatic = CreativeDirection.from_mapping(None)
    assert automatic.active().creative_focus == ""
    guided = CreativeDirection.from_mapping({"mode": "guided", "creative_focus": "combo Hunter"})
    assert CreativeDirection.from_mapping(json.loads(json.dumps(guided.to_mapping()))) == guided
    assert relevance("combo Hunter", "Ecco la combo Hunter") == 1
    assert relevance("carta impossibile", "board tranquilla") == 0
    for raw in ({"mode": "bad"}, {"typography_mode": "bad"}, {"hook_locked": "yes"},
                {"reference_images": [{"path": "x", "role": "gameplay_verified"}]}):
        with pytest.raises(ValueError):
            CreativeDirection.from_mapping(raw)


def test_profile_is_persistent_configurable_and_never_overwritten(tmp_path):
    store = CreatorStyleStore(tmp_path)
    profile = competitive_profile(store)
    assert profile["design_system"]["fonts"][0] == "Anton"
    assert profile["design_system"]["output_size"] == [1920, 1080]
    profile["design_system"]["mood"] = "custom"
    changed = store.save(profile)
    assert competitive_profile(store)["revision"] == changed["revision"]
    assert competitive_profile(store)["design_system"]["mood"] == "custom"
    assert list((tmp_path / "lugozzi" / "history").glob("*.json"))


def test_v4_automatic_four_concepts_and_single_planning_call(tmp_path):
    agent, generator = V4Agent(), FakeGenerator()
    report = make(tmp_path, agent=agent, generator=generator).run()
    assert len(agent.calls) == len(generator.calls) == 1
    assert len(report["thumbnails"]) == 4
    assert report["generation"]["cli_invocations"] == 1
    assert report["generation"]["image_generation_calls_observed"] is None
    assert report["generation"]["image_generation_variants_requested"] == 4
    assert len(list((tmp_path / "out" / "design_packs").glob("*.json"))) == 4
    assert all(t["technical_checks"]["resolution_ok"] for t in report["thumbnails"])


def test_locked_title_hook_precision_and_design_artifacts(v4_run):
    pipeline, report, agent, generator = v4_run
    assert report["preferred_title"]["title"] == "La mia partita"
    concepts = json.loads((pipeline.out / "thumbnail_concepts.json").read_text(encoding="utf-8"))["concepts"]
    assert all(c["thumbnail_text"] == "È ROTTO?!" for c in concepts)
    assert "Hunter combo" in agent.calls[0]["prompt"]
    assert "NO TEXT, LETTERS OR PLACEHOLDERS" in generator.calls[0]["prompt"]
    for c in concepts:
        pack = json.loads((pipeline.out / "design_packs" / f"{c['concept_id']}.json").read_text(encoding="utf-8"))
        assert pack["typography"]["mode"] == "precision"
        assert pack["typography"]["text"] == "È ROTTO?!"
        assert all("source_id" in r and "source_time" in r for r in pack["references"])
        assert pack["generation_prompt"]
        actual = c["render"]["typography"]
        assert actual["text"] == "È ROTTO?!" and actual["exact_text"]
        assert (pipeline.out / "thumbnails" / "previews" / f"{c['concept_id']}_160.png").is_file()


def test_local_text_edit_revert_and_polish_preserve_versions(v4_run):
    pipeline, _, _, _ = v4_run
    generator = FakeGenerator()
    editor = ThumbnailEditor(pipeline.out, generator=generator)
    original = Path(editor.resolve(1)["path"]).read_bytes()
    result = editor.typography(1, text="Più qualità!", font="Definitely Missing", color="#FFFFFF")
    assert result["cli_invocations"] == 0 and not generator.calls
    assert any("unavailable" in w for w in result["typography"]["warnings"])
    assert editor.design_pack("C1")["typography"]["text"] == "Più qualità!"
    editor.revert(1, 0)
    assert Path(editor.resolve(1)["path"]).read_bytes() == original
    assert editor.design_pack("C1")["typography"]["text"] == "È ROTTO?!"
    polished = editor.polish(1, "Clean the background; preserve card and face")
    assert len(generator.calls) == 1 and polished["operation"] == "polish"
    assert "ORIGINAL DESIGN PACK" in generator.calls[0]["prompt"]
    assert len(editor.resolve(1)["versions"]) == 3
    assert (pipeline.out / "thumbnails" / "edits" / "designs" / "C1_v2.json").is_file()


@pytest.mark.parametrize(("mode", "locked", "text", "expected"), [
    ("auto", False, "È forte", "precision"), ("ai", False, "È forte", "ai"),
    ("ai", True, "È forte", "precision"), ("precision", False, "", "none"),
])
def test_hybrid_modes(mode, locked, text, expected):
    c = {"concept_id": "C1", "role": "board-led", "package": {"board": ["F1"], "face": [], "detail": []},
         "thumbnail_text": text}
    direction = CreativeDirection.from_mapping({"mode": "guided", "typography_mode": mode,
                                                "thumbnail_hook": text, "hook_locked": locked})
    pack = design_pack(c, direction, {}, {"F1": {"source_index": 0}}, ["source.mp4"])
    assert pack["typography"]["mode"] == expected
    prompt = pack_prompt(pack)
    assert ("NO TEXT" in prompt) == (expected == "precision")


def test_precision_cache_exact_unicode_and_ai_does_not_apply_local_text(tmp_path):
    base = tmp_path / "base.png"
    write_image(base, np.full((1080, 1920, 3), 80, np.uint8))
    t = {"mode": "precision", "text": "È ROTTO?!", "fonts": ["Anton", "Impact"], "box": [.06, .06, .88, .28]}
    cache = PackagingCache(tmp_path / "cache")
    first = compose(base, tmp_path / "first.png", t, cache=cache)
    second = compose(base, tmp_path / "second.png", t, cache=cache)
    assert first["text"] == "È ROTTO?!" and second["cache_hit"]
    assert (tmp_path / "first.png").read_bytes() == (tmp_path / "second.png").read_bytes()
    ai = compose(base, tmp_path / "ai.png", {**t, "mode": "ai"})
    assert not ai["text_applied"]
    assert np.array_equal(read_image(base), read_image(tmp_path / "ai.png"))


def test_output_contain_preserves_edge_objects_instead_of_cropping():
    image = np.zeros((300, 300, 3), np.uint8)
    image[:20] = (0, 0, 255)
    image[-20:] = (0, 255, 0)
    output = normalize_canvas(image)
    assert output.shape[:2] == (1080, 1920)
    assert np.any(np.all(output[:80] == (0, 0, 255), axis=2))
    assert np.any(np.all(output[-80:] == (0, 255, 0), axis=2))


def test_analysis_scope_excludes_intro_and_other_source_preserves_output_clock():
    edit = EditMap(["intro.mp4", "hunter.mp4", "mage.mp4"],
                   [KeepPiece(0, 0, 20, 0), KeepPiece(1, 50, 80, 20), KeepPiece(2, 0, 10, 50)], origin="test")
    game = GameContext("g1", 1, "WIN", 1, 50, 80, 20, 50, 30, "export_selection")
    filtered = PackagingPipeline._analysis_map(edit, [game], {"export_selection": {"intervals": []}})
    assert filtered.sources == edit.sources
    assert filtered.pieces == [KeepPiece(1, 50, 80, 20)]
    assert filtered.to_edited(60, 1) == 30
    assert filtered.to_edited(5, 0) is None and filtered.to_edited(5, 2) is None


def test_guided_focus_adds_signal_without_inventing_combo(tmp_path):
    from analysis.packaging.highlights import collect_signals
    from analysis.packaging.models import TranscriptLine
    from analysis.packaging.audio import AudioProfile
    from analysis.packaging.visual import VisualProfile
    edit = EditMap(["v"], [KeepPiece(0, 0, 100, 0)], origin="test")
    lines = [TranscriptLine("T1", 20, 25, 20, 25, "Questa combo Hunter è interessante", .9, (), 0)]
    signals = collect_signals(edit, lines, AudioProfile("unavailable"), VisualProfile("unavailable"), [],
                              creative_focus="combo Hunter")
    guided = [s for s in signals if s.kind == "guided_focus"]
    assert len(guided) == 1 and "not combo verification" in guided[0].detail
    missing = collect_signals(edit, lines, AudioProfile("unavailable"), VisualProfile("unavailable"), [],
                              creative_focus="carta inesistente Zorbulator")
    assert not any(s.kind == "guided_focus" for s in missing)


def test_asset_validation_and_schema(v4_run):
    pipeline, _, _, _ = v4_run
    data = json.loads((pipeline.out / "thumbnail_concepts.json").read_text(encoding="utf-8"))
    c = deepcopy(data["concepts"][0])
    # Saved design has card IDs already resolved; all must still exist.
    lookup = pipeline.manifest.lookup()
    validate_design(c, lookup, set(lookup) | {h.id for h in pipeline._evidence.highlights})
    c["design"]["main_subject"] = "INVENTED"
    with pytest.raises(ValueError, match="Unknown"):
        validate_design(c, lookup, set(lookup))
    from analysis.packaging.fast import RESPONSE_SCHEMA_GEN
    assert "design" in planner_schema(RESPONSE_SCHEMA_GEN)["properties"]["concepts"]["items"]["required"]


def test_reference_is_owned_and_labelled_as_creative_not_gameplay(tmp_path):
    reference = tmp_path / "style.png"
    write_image(reference, np.full((200, 320, 3), 160, np.uint8))
    agent, generator = V4Agent(), FakeGenerator()
    pipeline = make(tmp_path / "run", agent=agent, generator=generator,
                    creative_direction={"mode": "guided", "reference_images": [{"path": str(reference), "role": "layout"}]})
    pipeline.run()
    assert "USER CREATIVE REFERENCE (layout); NOT gameplay evidence" in (pipeline.out / "planning_request.json").read_text(encoding="utf-8")
    assert "role=layout" in generator.calls[0]["prompt"]
    saved = json.loads((pipeline.out / "creative_direction.json").read_text(encoding="utf-8"))
    assert (pipeline.out / saved["reference_images"][0]["path"]).is_file()
    assert saved["reference_images"][0]["path"] != str(reference)


def test_attachment_budget_contains_webcam_and_gameplay(v4_run):
    pipeline, _, agent, _ = v4_run
    request = json.loads((pipeline.out / "planning_request.json").read_text(encoding="utf-8"))
    assert agent.calls[0]["images"] <= 6
    assert any("Creator webcam primary" in x for x in request["images"])
    assert any("Main gameplay reference" in x for x in request["images"])


def test_missing_focus_warns_without_claiming_verification(tmp_path):
    report = make(tmp_path, agent=V4Agent(), creative_direction={
        "mode": "guided", "creative_focus": "Zorbulator inesistente"}).run()
    assert any("no sufficient transcript match" in w for w in report["warnings"])


def test_direction_widget_roundtrip_and_project_field():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from ui.thumbnail_direction import CreativeDirectionWidget
    from core.project import Project
    widget = CreativeDirectionWidget()
    assert app is not None
    raw = {"mode": "guided", "thumbnail_hook": "È ROTTO?!", "hook_locked": True,
           "creative_focus": "Hunter combo", "typography_mode": "precision"}
    widget.restore(raw)
    project = Project.create_default()
    project.creative_direction = widget.value()
    assert project.creative_direction["thumbnail_hook"] == raw["thumbnail_hook"]
    assert widget.mode.currentData() == "guided"


def test_focus_changes_reuse_base_evidence_and_compatible_frame_bundles(tmp_path):
    first = make(tmp_path, analyze_only=True)
    first.run()
    assert first._engine.calls > 0 and first._sampler.calls > 0
    second = make(tmp_path, analyze_only=True, overwrite=True,
                  creative_direction={"mode": "guided", "creative_focus": "Zorbulator inesistente"})
    second.run()
    assert second._engine.calls == 0
    # Existing RMS cache rounds dB to 2 decimals: tied peaks can move slightly,
    # creating a few different burst windows; identical windows remain reusable.
    assert second._sampler.calls < first._sampler.calls
    assert second.cache.stats["frames"]["hit"] > 0
    assert second.cache.stats["transcript"]["hit"] > 0
    assert second.cache.stats["evidence"]["hit"] > 0


def test_guided_ranking_retains_automatic_alternative():
    from analysis.packaging.highlights import Signal, build_candidates
    from analysis.packaging.audio import AudioProfile
    from analysis.packaging.visual import VisualProfile
    edit = EditMap(["v"], [KeepPiece(0, 0, 200, 0)], origin="test")
    game = GameContext("g1", 1, "WIN", 0, 0, 200, 0, 200, 200, "test")
    base = [Signal(40, "reaction", .5, "reaction"), Signal(150, "audio_peak", 1., "energy")]
    auto = build_candidates(edit, base, [], AudioProfile("unavailable"), VisualProfile("unavailable"), [game])
    guided = build_candidates(edit, sorted(base + [Signal(40, "guided_focus", 1., "lexical")], key=lambda s: s.edited), [],
                              AudioProfile("unavailable"), VisualProfile("unavailable"), [game])
    assert len(guided) >= 2 and any(h.peak_source == 150 for h in guided)
    assert next(h.local_score for h in guided if h.peak_source == 40) > next(h.local_score for h in auto if h.peak_source == 40)


def test_webcam_selection_keeps_distinct_available_alternatives(tmp_path):
    from analysis.packaging.quality import dhash, hamming
    pipeline = make(tmp_path, analyze_only=True)
    pipeline.run()
    for h in pipeline._evidence.highlights:
        assert len(h.webcam_frame_ids) <= 3
        crops = [pipeline.frames.images[i] for i in h.webcam_frame_ids]
        assert all(hamming(dhash(a), dhash(b)) >= 10 for n, a in enumerate(crops) for b in crops[n+1:])


def test_local_render_v4_respects_locked_hook_without_image_generation(tmp_path):
    from youtube_packaging_fakes import FakeAgentV3
    generator = FakeGenerator()
    pipeline = make(tmp_path, agent=FakeAgentV3(), generator=generator, thumbnail_engine="v3",
                    creative_direction={"mode": "guided", "thumbnail_hook": "È ROTTO?!", "hook_locked": True})
    report = pipeline.run()
    assert not generator.calls
    assert all(t["typography"]["text"] == "È ROTTO?!" for t in report["thumbnails"])
    assert all(t["width"] == 1920 for t in report["thumbnails"])
