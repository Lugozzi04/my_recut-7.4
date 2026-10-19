"""Protected compositing and typography: synthetic pixels/fake providers, no media analysis/AI."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from analysis.packaging.assets import VisualAsset
from analysis.packaging.cache import PackagingCache
from analysis.packaging.compositing import prepare_pack, composite, diversify, fallback_environment
from analysis.packaging.design import design_pack, pack_prompt
from analysis.packaging.direction import CreativeDirection
from analysis.packaging.edit import ThumbnailEditor
from analysis.packaging.frames import read_image, write_image
from analysis.packaging.generate import AssetPackage, PackageImage, generation_prompt
from analysis.packaging.style import COMPETITIVE_DESIGN, CreatorStyleStore
from analysis.packaging.typography import apply_preset, compose, resolve_preset, TEXT_PRESETS
from youtube_packaging_fakes import FakeGenerator


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    import cv2
    monkeypatch.setenv("AUTO_CUTTER_CACHE_DIR", str(tmp_path / "shared_cache"))
    monkeypatch.setenv("AUTO_CUTTER_CONFIG_DIR", str(tmp_path / "config"))
    assets = {}
    for aid, kind, w, h, color in (("K01", "card_crop", 400, 600, (30, 190, 60)),
                                   ("W001", "webcam_original", 360, 420, (80, 70, 180)),
                                   ("F001", "gameplay_frame", 960, 540, (120, 80, 40))):
        image = np.full((h, w, 3), color, np.uint8)
        cv2.rectangle(image, (30, 30), (w-30, h-30), (255, 255, 255), 8)
        cv2.putText(image, aid, (40, h//2), cv2.FONT_HERSHEY_SIMPLEX, 2, (0, 0, 0), 5)
        write_image(tmp_path / f"{aid}.png", image)
        assets[aid] = {"id": aid, "type": kind, "path": f"{aid}.png", "width": w, "height": h,
                       "quality": {"sharpness": 150}, "source_index": 1, "source_time": 60., "game_id": "hunter-1",
                       "protection": "locked" if kind == "card_crop" else "identity_protected" if kind.startswith("webcam") else "guided",
                       "provenance": {"status": "verified", "thumbnail_use": "primary"}}
    original = read_image(tmp_path / "W001.png")
    alpha = np.zeros(original.shape[:2], np.uint8)
    cv2.ellipse(alpha, (180, 210), (130, 185), 0, 0, 360, 255, -1)
    write_image(tmp_path / "S001.png", np.dstack([original, alpha]))
    assets["S001"] = {**assets["W001"], "id": "S001", "type": "webcam_subject", "path": "S001.png",
                       "parent_id": "W001", "quality": {"isolated": 1, "face_inside": 1}}
    return tmp_path, assets


def pack_for(assets, *, role="card-led", strategy="reference_preserving", hook="È ROTTO?!"):
    main = "K01" if role == "card-led" else "W001" if role == "reaction-led" else "F001"
    package = {"detail": ["K01"] if main == "K01" else [], "face": ["W001"] if main == "W001" else [],
               "board": ["F001"]}
    concept = {"concept_id": "C1", "role": role, "package": package, "primary_subject": main,
               "thumbnail_text": hook, "narrative_angle": "supported example",
               "design": {"main_subject": main, "text_position": "left", "typography_preset": "Archivo Black, due righe",
                          "color_preset": "Bianco su blu", "story_confidence": "supported"}}
    return design_pack(concept, CreativeDirection(generation_strategy=strategy),
                       {"design_system": COMPETITIVE_DESIGN}, assets, ["intro.mp4", "hunter.mp4"], {1: "hunter-source"})


def test_authentic_card_composited_source_pixels_hash_and_no_cover_crop(evidence):
    root, assets = evidence
    pack = pack_for(assets)
    prepare_pack(pack, assets, root, role="card-led")
    environment = root / "environment.png"
    write_image(environment, np.full((1080, 1920, 3), (100, 0, 100), np.uint8))
    original = (root / "K01.png").read_bytes()
    result = composite(environment, root / "final.png", pack, root)
    layer = result["layers"][0]
    assert layer["protection"] == "locked" and layer["asset_id"] == "K01"
    assert layer["sha256"] == hashlib.sha256(original).hexdigest()
    assert (root / "K01.png").read_bytes() == original
    assert result["provenance_verified"] and not layer["drawn"]["cut_edges"]
    assert layer["drawn"]["source_px"] == [400, 600]
    assert not layer.get("width") and not layer.get("crop")
    output = read_image(root / "final.png")
    assert output.shape[:2] == (1080, 1920)
    assert np.any(np.all(output == (30, 190, 60), axis=2))
    assert "Do not draw people" in pack_prompt(pack)


def test_webcam_mask_reused_and_unreliable_mask_falls_back_to_full_board(evidence):
    root, assets = evidence
    pack = pack_for(assets, role="reaction-led")
    prepare_pack(pack, assets, root, role="reaction-led")
    face = pack["composition"]["layers"][0]
    assert face["asset_id"] == "S001" and face["mask"] and face["fallback"] is None
    assets["S001"]["quality"]["isolated"] = 0
    fallback = pack_for(assets, role="reaction-led")
    prepare_pack(fallback, assets, root, role="reaction-led")
    assert fallback["composition"]["layers"] == []
    assert fallback["composition"]["background"]["asset_id"] == "F001"
    assert any("mask unreliable" in w for w in fallback["composition"]["warnings"])


def test_insufficient_card_uses_only_real_board_and_empty_assets_fail(evidence):
    root, assets = evidence
    assets["K01"]["provenance"]["thumbnail_use"] = "avoid"
    pack = pack_for(assets)
    prepare_pack(pack, assets, root, role="card-led")
    assert pack["composition"]["family"] == "gameplay_clutch"
    assert pack["composition"]["layers"] == []
    assert pack["composition"]["background"]["asset_id"] == "F001"
    for a in assets.values():
        a["width"] = 1
    with pytest.raises(ValueError, match="no usable authentic subject"):
        prepare_pack(pack_for(assets), assets, root, role="card-led")


def test_portfolio_diversifies_geometry_treatment_and_keeps_locked_hook(evidence):
    root, assets = evidence
    packs = []
    for i in range(4):
        p = pack_for(assets)
        p["concept_id"] = f"C{i+1}"
        p["typography"]["locked"] = True
        prepare_pack(p, assets, root, role="card-led", index=i)
        packs.append(p)
    report = diversify(packs)
    assert report["distinct_type_presets"] == report["distinct_positions"] == 4
    assert {p["thumbnail_archetype"] for p in packs} == {"card_led", "board_led"}
    assert any("Not enough usable" in w for w in report["warnings"])
    assert all(p["typography"]["text"] == "È ROTTO?!" for p in packs)
    assert not report["aesthetic_quality_certified"]


@pytest.mark.parametrize("preset", list(TEXT_PRESETS))
def test_gradient_typography_unicode_margins_real_font_and_cache(evidence, preset):
    root, _ = evidence
    base = root / "base.png"
    write_image(base, np.full((1080, 1920, 3), 25, np.uint8))
    t = apply_preset({"mode": "precision", "text": "È ROTTO?!", "fonts": ["Missing Font", "Impact"],
                      "box": [.06, .06, .88, .28], "max_lines": 2}, preset)
    cache = PackagingCache(root / "cache")
    meta = compose(base, root / "text.png", t, cache=cache)
    assert meta["text"] == "È ROTTO?!" and meta["exact_text"] and meta["safe_margins"]
    assert meta["gradient"] and meta["font"] != "Missing Font"
    x, y, w, h = meta["bounds"]
    assert x >= 1920*.06 and y >= 1080*.06 and x+w <= 1920*.94 and y+h <= 1080*.34
    assert compose(base, root / "cached.png", t, cache=cache)["cache_hit"]
    assert (root / "text.png").read_bytes() == (root / "cached.png").read_bytes()


def test_freeform_font_and_color_no_longer_silently_become_yellow_anton(evidence):
    _, assets = evidence
    pack = pack_for(assets)
    assert pack["typography"]["fonts"][0] == "Archivo Black"
    assert pack["typography"]["preset"] == "white_impact"
    assert resolve_preset("Barlow Condensed Black", "rosso/arancio") == "danger_red"


def test_generator_does_not_receive_protected_photos_and_legacy_remains_available(evidence):
    root, assets = evidence
    pack = pack_for(assets)
    prepare_pack(pack, assets, root, role="card-led")
    # AssetPackage uses no additional extraction; protected inputs are intentionally withheld.
    package = AssetPackage("C1", "card-led", "hook", "moment", "direction", "message", "title", [
        PackageImage("K01", "detail", root / "K01.png", "authentic card")])
    prompt, images = generation_prompt([package], language="it", design_packs={"C1": pack})
    assert images == [] and "REAL Hearthstone" in prompt and "intentionally NOT attached" in prompt
    full = pack_for(assets, strategy="full_generative")
    prompt, images = generation_prompt([package], language="it", design_packs={"C1": full})
    assert images == [root / "K01.png"] and "Identity: SAME real person" in prompt
    assert "generate ONLY" not in prompt


def editor_run(root, assets):
    p = pack_for(assets)
    prepare_pack(p, assets, root, role="card-led")
    (root / "design_packs").mkdir(exist_ok=True)
    (root / "design_packs/C1.json").write_text(json.dumps(p), encoding="utf-8")
    env = root / "thumbnails/environments/C1.png"
    fallback_environment(env, p)
    base = root / "thumbnails/bases/C1.png"
    composite(env, base, p, root)
    compose(base, root / "thumbnails/concept_01.png", p["typography"])
    (root / "thumbnail_concepts.json").write_text(json.dumps({"concepts": [{"concept_id": "C1",
        "thumbnail_text": p["typography"]["text"], "design_pack": p,
        "render": {"path": "thumbnails/concept_01.png"}}]}), encoding="utf-8")
    return p


def test_local_style_and_position_edit_zero_ai_and_polish_keeps_original_asset(evidence):
    root, assets = evidence
    pack = editor_run(root, assets)
    generator = FakeGenerator()
    editor = ThumbnailEditor(root, generator=generator)
    result = editor.typography(1, text="Più qualità!", preset="energy_cyan", position="right")
    assert result["cli_invocations"] == 0 and generator.calls == []
    assert editor.design_pack("C1")["composition"]["layers"][0]["sha256"] == pack["composition"]["layers"][0]["sha256"]
    before = (root / "K01.png").read_bytes()
    original_env = (root / "thumbnails/environments/C1.png").read_bytes()
    edited = editor.polish(1, "Make the background cleaner")
    assert len(generator.calls) == 1 and edited["operation"] == "polish"
    assert generator.calls[0]["images"] == [root / "thumbnails/environments/C1.png"]
    assert "ENVIRONMENT ONLY" in generator.calls[0]["prompt"]
    assert (root / "K01.png").read_bytes() == before
    assert (root / "thumbnails/edits/environments/C1_v2.png").is_file()
    editor.revert(1, 0)
    assert (root / "thumbnails/environments/C1.png").read_bytes() == original_env
    assert editor.design_pack("C1")["typography"]["text"] == "È ROTTO?!"


def test_protected_asset_and_mask_tampering_rejected_before_composition(evidence):
    root, assets = evidence
    p = pack_for(assets, role="reaction-led")
    prepare_pack(p, assets, root, role="reaction-led")
    env = root / "environment.png"
    fallback_environment(env, p)
    mask = root / p["composition"]["layers"][0]["mask"]["path"]
    mask.write_bytes(b"changed")
    with pytest.raises(ValueError, match="mask changed"):
        composite(env, root / "final.png", p, root)


def test_locked_pixels_reject_colour_reinterpretation(evidence):
    root, assets = evidence
    p = pack_for(assets)
    prepare_pack(p, assets, root, role="card-led")
    env = root / "environment.png"
    fallback_environment(env, p)
    p["composition"]["layers"][0]["saturation"] = 1.8
    with pytest.raises(ValueError, match="colour-reinterpreted"):
        composite(env, root / "final.png", p, root)


def test_composite_cache_independent_of_local_type_and_never_redecodes(evidence):
    root, assets = evidence
    p = pack_for(assets)
    prepare_pack(p, assets, root, role="card-led")
    env = root / "env.png"
    fallback_environment(env, p)
    cache = PackagingCache(root / "cache")
    first = composite(env, root / "first.png", p, root, cache=cache)
    changed = deepcopy(p)
    changed["typography"]["text"] = "NUOVO TESTO"
    second = composite(env, root / "second.png", changed, root, cache=cache)
    assert not first["cache_hit"] and second["cache_hit"]
    assert (root / "first.png").read_bytes() == (root / "second.png").read_bytes()


def test_legacy_manifest_and_optional_identity_photos_separate_from_creative(evidence):
    root, assets = evidence
    a = VisualAsset.from_mapping(assets["K01"] | {"edited_time": 10., "highlight_id": "H1"})
    assert a.protection == "locked"
    old = dict(assets["K01"])
    old.pop("protection")
    a = VisualAsset.from_mapping(old | {"edited_time": 10., "highlight_id": "H1"})
    assert a.protection == "locked"
    store = CreatorStyleStore(root / "profiles")
    store.create("creator")
    p = store.set_identity_references("creator", [root / "W001.png"])
    assert p["identity_references"][0]["role"] == "identity"
    assert p["references"] == []
    assert Path(p["identity_references"][0]["path"]).is_file()


def test_source_class_provenance_survives_design_and_composition(evidence):
    root, assets = evidence
    p = pack_for(assets)
    prepare_pack(p, assets, root, role="card-led")
    l = p["composition"]["layers"][0]
    assert l["source_id"] == "hunter-source" and l["source_index"] == 1
    assert l["source_time"] == 60 and l["game_id"] == "hunter-1"
    assert all(r["source_id"] == "hunter-source" for r in p["references"])
    assert CreativeDirection.from_mapping({"generation_strategy": "full_generative"}).active().generation_strategy == "full_generative"


def test_same_evidence_two_strategies_cache_and_full_generative_rollback(tmp_path, monkeypatch):
    from test_youtube_packaging_gen import make_v4 as make
    monkeypatch.setenv("AUTO_CUTTER_CACHE_DIR", str(tmp_path / "shared"))
    first = make(tmp_path)
    preserving = first.run()
    assert all(t["compositing"]["provenance_verified"] for t in preserving["thumbnails"])
    assert preserving["generation"]["generation_strategy"] == "reference_preserving"
    # Same source/edit/settings; only the generation strategy changes. Providers are fakes.
    second = make(tmp_path, overwrite=True, creative_direction={"generation_strategy": "full_generative"})
    generative = second.run()
    assert second._engine.calls == 0
    assert second.cache.stats["transcript"]["hit"] > 0
    assert second.cache.stats["frames"]["hit"] > 0
    assert all("compositing" not in t for t in generative["thumbnails"])
    assert generative["generation"]["generation_strategy"] == "full_generative"
    assert len(preserving["thumbnails"]) == len(generative["thumbnails"]) == 4
