"""Hearthstone composition constraints. Synthetic assets; no provider or video access."""
from copy import deepcopy
import json

import numpy as np
import pytest

from analysis.packaging.compositing import composite, diversify, layout_layers, preflight, prepare_pack
from analysis.packaging.design import BANNED_PATTERNS, design_pack, pack_prompt, planning_rules
from analysis.packaging.direction import CreativeDirection
from analysis.packaging.frames import read_image, write_image
from analysis.packaging.generate import AssetPackage, PackageImage, generation_prompt
from analysis.packaging.style import COMPETITIVE_DESIGN
from test_thumbnail_v41 import evidence, pack_for  # shared synthetic fixtures


def test_real_gameplay_default_is_full_canvas_even_with_abstract_generated_image(evidence):
    root, assets = evidence
    p = pack_for(assets, role="board-led")
    prepare_pack(p, assets, root, role="board-led")
    assert p["background_strategy"] == "real_gameplay_derived"
    assert p["composition"]["layers"] == []
    assert p["subject_balance"]["gameplay_backdrop"] == [0, 0, 1, 1]
    env = root / "fake_model.png"
    write_image(env, np.full((1080, 1920, 3), (250, 0, 250), np.uint8))
    result = composite(env, root / "out.png", p, root)
    out = read_image(root / "out.png")
    # Actual board fills both corners; a magenta model backdrop cannot replace factual pixels.
    assert np.max(np.abs(out[100, 100].astype(float) - (120, 80, 40))) < 12
    assert np.max(np.abs(out[-100, -100].astype(float) - (120, 80, 40))) < 12
    assert result["background"]["asset_id"] == "F001" and result["provenance_verified"]
    assert p["gameplay_visibility_score"] == 1


def test_card_is_large_authentic_and_invalid_card_falls_back_to_board(evidence):
    root, assets = evidence
    p = pack_for(assets)
    prepare_pack(p, assets, root, role="card-led")
    assert p["thumbnail_archetype"] == "card_led"
    card = p["composition"]["layers"][0]
    assert card["protection"] == "locked" and card["height"] >= .80
    assert p["card_legibility_score"] >= .80
    assert p["composition"]["background"]["dim"] < 1
    assert p["composition"]["background"]["blur_px"] <= 2
    broken = deepcopy(p)
    broken["composition"]["layers"][0]["fitted_size"] = [.05, .15]
    assert preflight(broken)["rejected"]
    assets["K01"]["provenance"]["thumbnail_use"] = "avoid"
    fallback = pack_for(assets)
    prepare_pack(fallback, assets, root, role="card-led")
    assert fallback["thumbnail_archetype"] == "board_led"
    assert not fallback["composition"]["layers"]


def test_primary_face_requires_reliable_mask_and_minimum_size(evidence):
    root, assets = evidence
    p = pack_for(assets, role="reaction-led")
    prepare_pack(p, assets, root, role="reaction-led")
    assert p["face_usage"] == "primary_reaction"
    assert p["composition"]["layers"][0]["fitted_size"][0] >= .25
    tiny = deepcopy(p)
    tiny["composition"]["layers"][0]["fitted_size"] = [.08, .10]
    assert "face_too_small_or_raw_box" in preflight(tiny)["violations"]
    assets["S001"]["quality"]["isolated"] = 0
    fallback = pack_for(assets, role="reaction-led")
    prepare_pack(fallback, assets, root, role="reaction-led")
    assert fallback["face_usage"] == "none" and fallback["thumbnail_archetype"] == "board_led"
    assert not fallback["composition"]["layers"]


@pytest.mark.parametrize("pattern", BANNED_PATTERNS)
def test_banned_patterns_rejected(pattern, evidence):
    root, assets = evidence
    p = pack_for(assets, role="board-led")
    prepare_pack(p, assets, root, role="board-led")
    p["observed_banned_patterns"] = [pattern]
    assert preflight(p)["rejected"] and preflight(p)["penalties"][pattern] >= 40


def test_tiny_gameplay_layer_and_abstract_background_are_penalized(evidence):
    root, assets = evidence
    p = pack_for(assets)
    prepare_pack(p, assets, root, role="card-led")
    p["background_strategy"] = "abstract_environment"
    p["composition"]["layers"][0]["protection"] = "guided"
    penalties = preflight(p)["penalties"]
    assert penalties["generic_abstract_background"] >= 40
    assert penalties["tiny_center_screenshot"] >= 40


def test_four_supported_archetypes_with_two_faces_max_and_locked_hook(evidence):
    root, assets = evidence
    packs = []
    for i in range(4):
        concept = {"concept_id": f"C{i+1}", "role": "card-led", "primary_subject": "K01",
                   "thumbnail_text": "ignored", "package": {"face": ["W001"], "detail": ["K01"], "board": ["F001"]}}
        p = design_pack(concept, CreativeDirection(mode="guided", thumbnail_hook="È ROTTO?!", hook_locked=True,
                                                 generation_strategy="reference_preserving"),
                        {"design_system": COMPETITIVE_DESIGN}, assets, ["intro", "hunter"])
        prepare_pack(p, assets, root, role="card-led")
        packs.append(p)
    report = diversify(packs)
    assert {p["thumbnail_archetype"] for p in packs} == {"card_led", "board_led", "face_board", "face_card"}
    assert report["distinct_archetypes"] == 4 and report["face_variants"] == 2
    assert report["distinct_type_presets"] == report["distinct_positions"] == 4
    assert all(p["typography"]["text"] == "È ROTTO?!" for p in packs)
    assert all(not preflight(p)["rejected"] for p in packs)


def test_missing_assets_report_diversity_limit_without_inventing_result_or_face(evidence):
    root, assets = evidence
    packs = [pack_for(assets, role="board-led") for _ in range(4)]
    for i, p in enumerate(packs):
        p["concept_id"] = f"C{i+1}"
        prepare_pack(p, assets, root, role="board-led")
    report = diversify(packs)
    assert all(p["thumbnail_archetype"] == "board_led" for p in packs)
    assert report["warnings"] and report["face_variants"] == 0


def test_design_fields_prompt_and_gameplay_reference_are_serialized(evidence):
    root, assets = evidence
    p = pack_for(assets)
    prepare_pack(p, assets, root, role="card-led")
    saved = json.loads(json.dumps(p))
    required = {"thumbnail_archetype", "dominant_subject", "secondary_subject", "optional_subject",
                "background_strategy", "gameplay_frame_role", "card_usage", "face_usage", "board_visibility",
                "text_need", "hook_style", "composition_template", "clutter_budget", "banned_patterns",
                "subject_balance", "clutter_score", "gameplay_visibility_score", "card_legibility_score", "face_readability_score"}
    assert required <= saved.keys()
    assert saved["composition"]["background"]["source_id"] == "hunter-source"
    package = AssetPackage("C1", "card-led", "hook", "moment", "direction", "message", "title", [
        PackageImage("F001", "gameplay_backdrop", root / "F001.png", "real gameplay"),
        PackageImage("K01", "detail", root / "K01.png", "protected card")])
    prompt, images = generation_prompt([package], language="it", design_packs={"C1": p})
    assert images == [root / "F001.png"]
    assert "REAL Hearthstone" in prompt and "Only smooth lighting accents" in prompt
    assert "Do not draw people" in pack_prompt(p)
    assert "Creative" in planning_rules() or "CREATIVE DIRECTOR" in planning_rules()


def test_old_saved_composition_still_renders(evidence):
    root, assets = evidence
    p = pack_for(assets)
    prepare_pack(p, assets, root, role="card-led")
    del p["composition"]["background"]
    layout_layers(p)  # legacy contain geometry/environment path
    env = root / "legacy.png"
    write_image(env, np.full((1080, 1920, 3), 40, np.uint8))
    assert composite(env, root / "old.png", p, root)["provenance_verified"]


def test_background_provenance_tampering_fails_closed(evidence):
    root, assets = evidence
    p = pack_for(assets, role="board-led")
    prepare_pack(p, assets, root, role="board-led")
    env = root / "environment.png"
    write_image(env, np.full((1080, 1920, 3), 40, np.uint8))
    write_image(root / "F001.png", np.full((540, 960, 3), 0, np.uint8))
    with pytest.raises(ValueError, match="backdrop changed"):
        composite(env, root / "out.png", p, root)


def test_real_local_fallback_masks_known_webcam_overlay(evidence):
    from analysis.packaging.compositing import fallback_environment
    root, assets = evidence
    p = pack_for(assets, role="board-led")
    prepare_pack(p, assets, root, role="board-led")
    p["composition"]["background"]["mask_boxes"] = [[.02, .02, .15, .15]]
    fallback_environment(root / "fallback.png", p, root)
    image = read_image(root / "fallback.png")
    assert image.shape[:2] == (1080, 1920)
    assert np.max(np.abs(image[-100, -100].astype(float) - (120, 80, 40))) < 5
