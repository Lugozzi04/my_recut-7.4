"""AI YouTube Packaging V3: assets, cut-outs, opportunities, scene graphs, renderer, portfolio, pipeline."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import cv2
import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from analysis.boundaries.agents import AgentCallError
from analysis.packaging import fast, quality
from analysis.packaging.assets import VisualAsset, VisualAssetManifest, board_regions
from analysis.packaging.frames import read_image, read_image_unchanged, write_image
from analysis.packaging.models import GameContext, HighlightCandidate, PackagingError, TranscriptLine
from analysis.packaging.opportunities import build_opportunities, shortlist
from analysis.packaging.pipeline import PackagingPipeline, PackagingSettings
from analysis.packaging.portfolio import content_words, evaluate_portfolio, family
from analysis.packaging.render import SceneRenderer
from analysis.packaging.scene import (MAX_LAYERS, default_layer, normalize_scene, preset_board, preset_reaction,
                                      preset_story, scene_from_v2, validate_scene)
from analysis.packaging.style import CreatorStyleStore, select_references
from analysis.packaging.subjects import background_plate, mask_ok, segment_person

from youtube_packaging_fakes import (WEBCAM_ROI, FakeAgentV3, FakeEngine, FakeSampler, fake_rms, fake_scanner,
                                     planner, synthetic_frame)

# ---------- synthetic person for segmentation (no real faces in the test suite) ----------

def room(seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    image = np.zeros((240, 320, 3), np.uint8)
    image[:] = (170, 180, 190)
    image[150:240, :90] = (60, 90, 140)  # a shelf, static
    image += rng.integers(0, 6, image.shape, dtype=np.uint8)
    return image


def person(image: np.ndarray, dx: int = 0) -> np.ndarray:
    out = image.copy()
    cv2.ellipse(out, (170 + dx, 95), (38, 48), 0, 0, 360, (120, 150, 210), -1)  # head
    cv2.ellipse(out, (170 + dx, 62), (36, 18), 0, 180, 360, (40, 60, 90), -1)  # hair, within the head outline
    cv2.rectangle(out, (155 + dx, 135), (185 + dx, 155), (110, 140, 200), -1)  # neck
    cv2.fillPoly(out, [np.array([[120 + dx, 150], [220 + dx, 150], [260 + dx, 240], [80 + dx, 240]])], (50, 40, 40))
    return out


def fake_face(image: np.ndarray) -> tuple[float, float, float, float]:
    h, w = image.shape[:2]
    cols = np.where((np.abs(image.astype(int) - (120, 150, 210)).sum(axis=2) < 40).any(axis=0))[0]
    cx = (cols.min() + cols.max()) / 2 if cols.size else 170
    return ((cx - 34) / w, 72 / h, 68 / w, 68 / h)  # eyebrows to chin, like a real detector


# ---------- VisualAssetManifest ----------

def manifest_fixture() -> VisualAssetManifest:
    m = VisualAssetManifest()
    m.add(VisualAsset("F001", "gameplay_frame", "frames/F001.jpg", 0, 100.0, 10.0, "H01", "g-1", 1920, 1080,
                      uses=["background", "primary"], provenance={"method": "ffmpeg_frame"}))
    m.add(VisualAsset("F002", "gameplay_frame", "frames/F002.jpg", 0, 200.0, 50.0, "H02", "g-1", 1920, 1080,
                      uses=["background", "primary"]))
    m.add(VisualAsset("W001", "webcam_original", "webcam/W001.jpg", 0, 100.0, 10.0, "H01", "g-1", 390, 290))
    m.add(VisualAsset("S001", "webcam_subject", "subjects/S001.png", 0, 100.0, 10.0, "H01", "g-1", 390, 290,
                      parent_id="W001", has_alpha=True, quality={"isolated": 1.0}, uses=["primary"],
                      provenance={"face": [0.3, 0.2, 0.3, 0.4]}))
    m.add(VisualAsset("B001", "board_region", "regions/B001.jpg", 0, 100.0, 10.0, "H01", "g-1", 800, 450,
                      bbox=[0.3, 0.3, 0.42, 0.42], parent_id="F001"))
    m.add(VisualAsset("K01", "card_crop", "cards/K01.png", 0, 200.0, 50.0, "H02", "g-1", 200, 280, parent_id="F002",
                      provenance={"status": "verified", "name": "Meccaciclo"}))
    return m


def test_manifest_types_provenance_roundtrip_and_families():
    m = manifest_fixture()
    with pytest.raises(ValueError):
        m.add(VisualAsset("F001", "gameplay_frame", "x", 0, 0.0, 0.0, None, None, 1, 1))
    with pytest.raises(ValueError):
        m.add(VisualAsset("X1", "generated_art", "x", 0, 0.0, 0.0, None, None, 1, 1))
    data = m.to_mapping()
    assert data["counts"] == {"gameplay_frame": 2, "webcam_original": 1, "webcam_subject": 1, "board_region": 1,
                              "card_crop": 1}
    again = VisualAssetManifest.from_mapping(json.loads(json.dumps(data)))
    assert again.by_id()["B001"].bbox == [0.3, 0.3, 0.42, 0.42] and again.by_id()["S001"].has_alpha
    lookup = m.lookup()
    assert family("S001", lookup) == "W001" and family("B001", lookup) == "F001" and family("K01", lookup) == "F002"
    assert lookup["F001"]["edited_time"] == 10.0 and lookup["F001"]["source_time"] == 100.0


def test_board_regions_are_normalized_and_avoid_the_webcam():
    image = synthetic_frame(3.0)
    image = cv2.resize(image, (1280, 720))
    image[400:720, :320] = np.random.default_rng(1).integers(0, 255, (320, 320, 3), dtype=np.uint8)  # busy webcam
    found = board_regions(image, exclude=[[0.0, 0.55, 0.25, 0.45]])
    assert found
    for (x, y, w, h), _ in found:
        assert 0 <= x < 1 and 0 <= y < 1 and x + w <= 1.001 and y + h <= 1.001
        overlap = max(0.0, min(x + w, 0.25) - x) * max(0.0, min(y + h, 1.0) - max(y, 0.55))
        assert overlap / (w * h) < 0.25  # the noisy webcam corner never dominates a region


# ---------- webcam subject extraction ----------

def test_cutout_with_background_plate_and_fallbacks():
    samples = [person(room(i), dx) for i, dx in enumerate((-100, -80, -60, 60, 80, 100, -90, 90))]
    with patch("analysis.packaging.subjects._face", side_effect=fake_face):
        plate = background_plate(samples)
        assert plate is not None
        cut = segment_person(person(room(99)), plate=plate)
    assert cut.ok and cut.method in ("plate_grabcut", "grabcut") and cut.rgba.shape[2] == 4
    alpha = cut.rgba[..., 3]
    assert alpha[95, 170] > 200 and alpha[10, 10] < 30  # face kept, room removed
    assert alpha[200, 30] < 60  # the static shelf is not attached to the shoulders
    with patch("analysis.packaging.subjects._face", return_value=None):
        none = segment_person(room(5))
    assert not none.ok and none.method == "fallback_rect" and "no face detected" in none.limitations
    assert background_plate(samples[:3]) is None  # too few samples: no plate
    ok, problems = mask_ok({"coverage": 0.9, "face_inside": 0.5, "spill": 0.2, "raggedness": 0.5, "side_touch": 0.6})
    assert not ok and len(problems) == 5


# ---------- opportunities ----------

def _highlight(hid: str, sources: list[str], lines: list[str], game: str = "g-1", score: float = 1.0):
    return HighlightCandidate(hid, game, 0, 10, 20, 10, 20, 15, sources, {}, lines, "", "reason", score)


def test_opportunities_come_from_evidence_and_are_not_audio_led():
    lines = [TranscriptLine("T0001", 10, 12, 10, 12, "allora tengo questa o la cambio? conviene", 0.9),
             TranscriptLine("T0002", 30, 32, 30, 32, "aaah", 0.9)]
    highlights = [_highlight("H01", ["speech_burst"], ["T0001"], score=0.4),
                  _highlight("H02", ["audio_peak", "reaction"], ["T0002"], score=2.0)]
    games = [GameContext("g-1", 1, "WIN", 0, 0, 100, 0, 100, 100, "project_gameplay", highlight_ids=["H01", "H02"])]
    m = manifest_fixture()
    opps = build_opportunities(highlights, games, lines, m)
    by_h = {o.highlight_ids[0]: o for o in opps if len(o.highlight_ids) == 1}
    assert by_h["H01"].story_type == "strategic_decision"
    assert by_h["H01"].weight >= by_h["H02"].weight - 0.2  # a decision is not drowned by a loud peak
    assert "T0001" in by_h["H01"].evidence_ids
    assert any(o.story_type == "matchup_story" for o in opps)
    assert all(o.opportunity_id.startswith("O") for o in opps)
    assert "reaction_cutout" in by_h["H01"].possible_treatments  # S001 isolated cut-out is available


def test_shortlist_prefers_diverse_story_types():
    lines: list[TranscriptLine] = []
    highlights = [_highlight(f"H0{i}", ["audio_peak"], []) for i in range(1, 6)]
    games = [GameContext("g-1", 1, "WIN", 0, 0, 100, 0, 100, 100, "project_gameplay")]
    opps = build_opportunities(highlights, games, lines, VisualAssetManifest(), limit=10)
    picked = shortlist(opps, limit=3)
    assert len(picked) == 3 and [o.opportunity_id for o in picked] == ["O01", "O02", "O03"]


# ---------- scene graph ----------

def test_scene_validation_limits_and_asset_types():
    lookup = manifest_fixture().lookup()
    good = preset_reaction(background="F001", subject="S001", text="Quale tengo?", element="K01")
    assert validate_scene(good, lookup) == []
    bad = copy.deepcopy(good)
    bad["layers"][1]["asset_id"] = "W001"  # subject_cutout needs a cut-out
    bad["layers"][0]["strength"] = 3
    bad["background"]["asset_id"] = "S001"
    problems = " ".join(validate_scene(bad, lookup))
    assert "cannot use a webcam_original" in problems and "strength" in problems and "not a gameplay" in problems
    too_many = copy.deepcopy(good)
    too_many["layers"] += [default_layer("shape", f"s{i}", shape="rect", width=0.1) for i in range(MAX_LAYERS)]
    assert any("at most" in p for p in validate_scene(too_many, lookup))
    ghost = copy.deepcopy(good)
    ghost["layers"][1]["asset_id"] = "S999"
    assert any("unknown asset" in p for p in validate_scene(ghost, lookup))
    wordy = preset_story(background="F001", text="una frase davvero troppo lunga per una miniatura")
    assert any("short" in p for p in validate_scene(wordy, lookup))
    assert validate_scene(preset_story(background="F002", text=None), lookup) == []  # no text is allowed


def test_v2_concepts_convert_to_valid_scenes():
    lookup = manifest_fixture().lookup()
    concept: dict[str, Any] = {"thumbnail_text": "Quale tengo?", "webcam_frame_id": "W001", "card_asset_ids": [],
               "composition_plan": {"layout": "split_reaction", "background_frame_id": "F001", "focus_box": None,
                                    "inset_id": None, "inset_box": None, "text_side": "left", "webcam_side": "right",
                                    "emphasis_word": None, "badge": None, "accent_color": "#FFD400",
                                    "text_color": "#FFFFFF"}}
    scene = scene_from_v2(concept, {"W001": "S001"})
    assert validate_scene(scene, lookup) == [] and scene["metadata"]["converted_from"] == "v2_composition_plan"
    assert any(layer["type"] == "subject_cutout" for layer in scene["layers"])
    bubble = scene_from_v2(dict(concept, composition_plan=dict(concept["composition_plan"], layout="cinematic_text")))
    creator = next(layer for layer in bubble["layers"] if layer["id"] == "creator")
    assert creator["type"] == "image" and creator["clip"] == "circle"  # not isolated: a bubble, never a big rectangle
    board = scene_from_v2(dict(concept, webcam_frame_id=None, composition_plan=dict(
        concept["composition_plan"], layout="board_focus", inset_id="F002", inset_box=[0.1, 0.1, 0.4, 0.4])))
    assert validate_scene(board, lookup) == [] and board["background"]["blur"] == 0.0  # the board is the subject


# ---------- renderer ----------

def images_fixture() -> dict[str, np.ndarray]:
    face = person(room(3))
    alpha = np.zeros(face.shape[:2], np.uint8)
    cv2.ellipse(alpha, (170, 95), (40, 50), 0, 0, 360, 255, -1)
    return {"F001": cv2.resize(synthetic_frame(1.0), (1280, 720)), "F002": cv2.resize(synthetic_frame(5.0), (1280, 720)),
            "W001": face, "S001": np.dstack([face, alpha]), "B001": cv2.resize(synthetic_frame(2.0), (640, 360)),
            "K01": cv2.resize(synthetic_frame(4.0)[40:300, 100:280], (200, 280))}


def test_scene_rendering_layers_alpha_and_determinism(tmp_path):
    renderer = SceneRenderer()
    scene = normalize_scene(preset_reaction(background="F001", subject="S001", text="Quale tengo?", element="K01"))
    first = renderer.render_scene(scene, images_fixture(), tmp_path / "a.png")
    second = renderer.render_scene(scene, images_fixture(), tmp_path / "b.png")
    assert (tmp_path / "a.png").read_bytes() == (tmp_path / "b.png").read_bytes()  # deterministic
    image = read_image(tmp_path / "a.png")
    assert image is not None and image.shape[:2] == (720, 1280)
    ids = [layer["id"] for layer in first.layers]
    assert ids[0] == "background" and ids.index("creator") > ids.index("shade")  # z order
    creator = next(layer for layer in first.layers if layer["id"] == "creator")
    assert creator["has_alpha"] and "bottom" in creator["cut_edges"]  # shoulders run off the bottom, intentionally
    assert first.text["lines"] and second.text["fits"]


def test_scenes_without_text_webcam_or_cards(tmp_path):
    renderer = SceneRenderer()
    for name, scene in {"no_text": preset_story(background="F001", text=None),
                        "no_webcam": preset_board(background="F001", text="Tavolo pieno", region="B001"),
                        "no_cards": preset_story(background="F002", text="Finale", inset="F001")}.items():
        result = renderer.render_scene(normalize_scene(scene), images_fixture(), tmp_path / f"{name}.png")
        assert result.width == 1280 and (tmp_path / f"{name}.png").is_file()
        assert (result.text == {}) == (name == "no_text")


def test_missing_asset_image_is_skipped_with_a_warning(tmp_path):
    images = images_fixture()
    del images["K01"]
    scene = normalize_scene(preset_board(background="F001", text="Carta", cards=["K01"]))
    result = SceneRenderer().render_scene(scene, images, tmp_path / "x.png")
    assert any("K01" in w for w in result.warnings)


# ---------- quality gate V3 ----------

def test_scene_gate_detects_text_on_face_unisolated_subject_and_fixes_it(tmp_path):
    lookup = manifest_fixture().lookup()
    scene = preset_reaction(background="F001", subject="S001", text="Quale tengo?")
    text = next(layer for layer in scene["layers"] if layer["type"] == "text")
    creator = next(layer for layer in scene["layers"] if layer["id"] == "creator")
    text["x"], text["y"] = creator["x"], 0.35  # headline on the face
    renderer = SceneRenderer()
    result = renderer.render_scene(normalize_scene(scene), images_fixture(), tmp_path / "g.png")
    codes = {i.code for i in quality.inspect_scene(result, normalize_scene(scene), lookup)}
    assert "text_covers_subject" in codes
    assert quality.apply_scene_fix(scene, "move_text") and abs(text["x"] - creator["x"]) > 0.3
    lookup["S001"]["quality"] = {"isolated": 0.0}
    result = renderer.render_scene(normalize_scene(scene), images_fixture(), tmp_path / "h.png")
    assert "unisolated_subject_large" in {i.code for i in quality.inspect_scene(result, normalize_scene(scene), lookup)}
    assert quality.apply_scene_fix(scene, "shrink_subject") and creator["height"] <= 0.42


def test_smaller_text_fix_moves_boxes_into_the_safe_area():
    scene = preset_story(background="F001", text="Finale")
    text = next(layer for layer in scene["layers"] if layer["type"] == "text")
    text["x"] = 0.2
    assert quality.apply_scene_fix(scene, "smaller_text")
    assert text["x"] - text["width"] / 2 >= 0.05


# ---------- portfolio ----------

def _concept(cid: str, story: str, treatment: str, scene: dict[str, Any], text: str, highlights: list[str],
             primary: str) -> dict[str, Any]:
    return {"concept_id": cid, "story_type": story, "treatment": treatment, "scene": scene, "thumbnail_text": text,
            "highlight_ids": highlights, "primary_subject": primary, "narrative_angle": story}


def test_portfolio_of_four_diverse_concepts_passes():
    lookup = manifest_fixture().lookup()
    concepts = [
        _concept("C1", "unexpected_reaction", "reaction_cutout",
                 preset_reaction(background="F001", subject="S001", text="Non ci credo"), "Non ci credo", ["H01"], "S001"),
        _concept("C2", "decisive_card", "card_hero", preset_board(background="F002", text="Questa carta", cards=["K01"]),
                 "Questa carta", ["H02"], "K01"),
        _concept("C3", "impressive_board", "board_zoom", preset_board(background="F002", text=None, region="B001",
                                                                      text_side="right"), "", ["H01"], "B001"),
        _concept("C4", "strategic_decision", "split_moments",
                 preset_story(background="F002", text="Tengo o cambio?", inset="F001"), "Tengo o cambio?", ["H02"], "F001"),
    ]
    report = evaluate_portfolio(concepts, lookup, available_story_types=5, available_treatments=4,
                                available_opportunities=5)
    assert report.blocking == [] and report.metrics["distinct_treatments"] == 4
    assert any(f.code == "shared_primary_source" and f.severity == "compromise" for f in report.findings)  # B001<-F001


def test_portfolio_rejects_template_clones_but_accepts_justified_compromise():
    lookup = manifest_fixture().lookup()
    scene = preset_board(background="F002", text="Carta pazza", cards=["K01"])
    clones = [_concept(f"C{i}", "decisive_card", "card_hero", copy.deepcopy(scene), t, ["H02"], "K01")
              for i, t in enumerate(["Carta pazza", "Carta folle", "Pazza carta", "Carta pazza!"], start=1)]
    report = evaluate_portfolio(clones, lookup, available_story_types=4, available_treatments=4,
                                available_opportunities=4)
    codes = {f.code for f in report.blocking}
    assert {"duplicate_concept", "too_few_angles", "too_few_treatments", "same_message"} <= codes
    one_moment = evaluate_portfolio(clones[:2], lookup, available_story_types=1, available_treatments=1,
                                    available_opportunities=1)
    assert "too_few_angles" not in {f.code for f in one_moment.blocking}
    assert content_words("Quale tengo?") == content_words("quale TENGO")


# ---------- style references ----------

def test_style_reference_selection(tmp_path):
    store = CreatorStyleStore(tmp_path)
    store.create("Lugozzi")
    image = tmp_path / "r.png"
    write_image(image, synthetic_frame(1.0))
    store.import_examples("Lugozzi", thumbnails=[image], categories=["unexpected_reaction"])
    store.import_examples("Lugozzi", thumbnails=[tmp_path / "r.png"], categories=["card_hero"])  # duplicate path
    bad = tmp_path / "bad.png"
    write_image(bad, synthetic_frame(2.0))
    store.import_examples("Lugozzi", thumbnails=[bad], negative=True)
    profile = store.load("Lugozzi")
    refs = select_references(profile, ["unexpected_reaction"], k=3)
    assert refs[0]["categories"] == ["unexpected_reaction"] and refs[-1]["polarity"] == "negative"
    store.update("Lugozzi", enabled=False)
    assert select_references(store.load("Lugozzi"), ["unexpected_reaction"]) == []


# ---------- pipeline V3 ----------

def make(tmp_path: Path, *, agent: Any = None, **settings: Any) -> PackagingPipeline:
    media = tmp_path / "video.mp4"
    if not media.exists():
        media.write_bytes(b"x" * 2048)
    base: dict[str, Any] = dict(output=str(tmp_path / "out"), video=str(media), webcam_roi=WEBCAM_ROI,
                                cache_dir=str(tmp_path / "cache"), style_dir=str(tmp_path / "styles"), max_deepen=0,
                                thumbnail_engine="v3")
    base.update(settings)
    return PackagingPipeline(PackagingSettings(**base), agent=cast(Any, agent or FakeAgentV3()),
                             transcription_engine=FakeEngine(), rms_extractor=fake_rms, keyframe_scanner=fake_scanner,
                             frame_sampler=FakeSampler(), duration_probe=lambda path: 330.0, planner=planner())


def test_fast_v3_is_one_call_and_writes_the_whole_portfolio(tmp_path):
    agent = FakeAgentV3()
    report = make(tmp_path, agent=agent).run()
    out = tmp_path / "out"
    assert [c["task"] for c in agent.calls] == ["packaging_fast_v3"]
    assert report["thumbnail_engine"] == "v3" and report["portfolio"]["passed"]
    for name in ("visual_assets.json", "opportunities.json", "portfolio_strategy.json",
                 "portfolio_quality_report.json", "thumbnail_concepts.json"):
        assert (out / name).is_file(), name
    pngs = sorted((out / "thumbnails").glob("concept_*.png"))
    assert len(pngs) == 4 and all(read_image(p).shape[:2] == (720, 1280) for p in pngs)  # type: ignore[union-attr]
    concepts = json.loads((out / "thumbnail_concepts.json").read_text(encoding="utf-8"))["concepts"]
    assert all("scene" in c and c["render"]["layers"] for c in concepts)
    assert any(layer["type"] == "card" and layer["asset_id"].startswith("K")  # O1 resolved to a real crop
               for c in concepts for layer in c["scene"]["layers"])
    assert (out / "thumbnails" / "overview_320.jpg").is_file()
    prompt = agent.calls[0]["prompt"]
    assert "VISUAL ASSET MANIFEST" in prompt and "CREATIVE OPPORTUNITIES" in prompt and "PORTFOLIO PLANNING" in prompt


def test_fast_v3_rejects_a_non_diverse_portfolio(tmp_path):
    with pytest.raises(PackagingError):
        make(tmp_path, agent=FakeAgentV3(duplicate=True)).run()
    report = json.loads((tmp_path / "out" / "packaging_report.json").read_text(encoding="utf-8"))
    assert report["status"] == "agent_failed" and "portfolio" in report["error"]["detail"]
    assert (tmp_path / "out" / "visual_assets.json").is_file()  # local work kept for a retry


def test_rerender_needs_no_agent_and_uses_the_render_cache(tmp_path):
    make(tmp_path).run()
    agent = FakeAgentV3()
    pipeline = make(tmp_path, agent=agent, only="render", video=None)
    report = pipeline.run()
    assert agent.calls == [] and report["status"] == "done"
    assert pipeline.cache.stats["render"]["hit"] >= 4  # unchanged scenes are not redrawn


def test_v2_outputs_rerender_with_the_v3_engine(tmp_path):
    from youtube_packaging_fakes import FakeAgent

    make(tmp_path, agent=FakeAgent(), thumbnail_engine="v2", thumbnails=3).run()
    report = make(tmp_path, agent=FakeAgentV3(), only="render", video=None, thumbnail_engine="v3").run()
    concepts = json.loads((tmp_path / "out" / "thumbnail_concepts.json").read_text(encoding="utf-8"))["concepts"]
    assert report["status"] == "done" and all(c["scene"]["metadata"]["converted_from"] == "v2_composition_plan"
                                              for c in concepts)
    assert (tmp_path / "out" / "visual_assets.json").is_file()  # assets upgraded locally


def test_deep_v3_is_story_plus_one_portfolio_call(tmp_path):
    agent = FakeAgentV3()
    make(tmp_path, agent=agent, mode="deep").run()
    tasks = [c["task"] for c in agent.calls]
    assert tasks == ["video_story", "creative_portfolio_v3"]
    assert "TRANSCRIPT (" not in agent.calls[1]["prompt"]  # the story replaces the transcript


def test_webcam_absent_pipeline_still_renders(tmp_path):
    report = make(tmp_path, webcam_roi="none").run()
    assets = json.loads((tmp_path / "out" / "visual_assets.json").read_text(encoding="utf-8"))
    assert report["status"] == "done" and "webcam_subject" not in assets["counts"]


def test_agent_failure_keeps_v3_assets(tmp_path):
    with pytest.raises(PackagingError):
        make(tmp_path, agent=FakeAgentV3(fail=AgentCallError("timeout", "slow"))).run()
    assert (tmp_path / "out" / "opportunities.json").is_file()


def test_v3_schema_and_validation_reject_unknown_assets():
    assert "portfolio_strategy" in fast.RESPONSE_SCHEMA_V3["required"]
    concept_schema = fast.RESPONSE_SCHEMA_V3["properties"]["concepts"]["items"]
    assert "scene" in concept_schema["required"] and concept_schema["additionalProperties"] is False


def test_cutouts_are_cached_between_runs(tmp_path):
    images = {}
    with patch("analysis.packaging.subjects._face", side_effect=fake_face):
        from analysis.packaging.assets import subject_assets
        from analysis.packaging.cache import PackagingCache
        from analysis.packaging.models import FrameRecord

        frames = SimpleNamespace(records={"W001": FrameRecord("W001", "webcam", 0, 1.0, 1.0, "webcam/W001.jpg", 320, 240,
                                                               highlight_id="H01")},
                                 images={"W001": person(room(1))})
        cache = PackagingCache(tmp_path / "c")
        plates = {None: background_plate([person(room(i), dx) for i, dx in enumerate((-100, -80, -60, 60, 80, 100, -90))])}
        first = subject_assets(frames, plates, tmp_path, [], cache=cache)  # type: ignore[arg-type]
        images["first"] = read_image_unchanged(tmp_path / first[0].path)
        second = subject_assets(frames, plates, tmp_path, [], cache=cache)  # type: ignore[arg-type]
    assert cache.stats["frames"]["hit"] >= 1 and first[0].quality == second[0].quality
    assert images["first"] is not None and images["first"].shape[2] == 4


def test_subject_framing_and_enlargement_cap(tmp_path):
    from analysis.packaging import render

    rgba = images_fixture()["S001"]
    with patch("analysis.packaging.render.face_box", return_value=(0.42, 0.25, 0.22, 0.3)):
        framed = render.frame_subject(rgba)
    assert framed.shape[0] < rgba.shape[0] and framed.shape[2] == 4  # headroom trimmed around the face
    scene = normalize_scene(preset_reaction(background="F001", subject="S001", text="Ciao"))
    result = SceneRenderer().render_scene(scene, images_fixture(), tmp_path / "cap.png")
    creator = next(layer for layer in result.layers if layer["id"] == "creator")
    assert creator["upscale_limited"] and creator["rect"][3] <= rgba.shape[0] * render.MAX_SUBJECT_UPSCALE + 1


def test_render_cache_key_follows_renderer_source():
    from analysis.packaging.pipeline_v3 import renderer_code_digest

    assert len(renderer_code_digest()) == 16 and renderer_code_digest() == renderer_code_digest()
