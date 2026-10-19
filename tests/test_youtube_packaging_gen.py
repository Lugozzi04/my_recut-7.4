"""Thumbnail engine "gen": roles + asset packages, ONE image-model call, local render as draft/fallback.

No test calls Claude, Codex or an image model: the planning agent and the generator are fakes.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, cast

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from analysis.packaging import fast, generate
from analysis.packaging.frames import read_image, write_image
from analysis.packaging.generate import (GenerationError, asset_package, attach_drafts, generation_prompt,
                                         map_generated, normalize_generated)
from analysis.packaging.models import PackagingError
from analysis.packaging.pipeline import PackagingPipeline, PackagingSettings
from analysis.packaging.scene import validate_scene

from youtube_packaging_fakes import (WEBCAM_ROI, FakeAgentGen, FakeEngine, FakeGenerator, FakeSampler, fake_rms,
                                     fake_scanner, planner)


def make(tmp_path: Path, *, agent: Any = None, generator: Any = None, **settings: Any) -> PackagingPipeline:
    tmp_path.mkdir(parents=True, exist_ok=True)
    media = tmp_path / "video.mp4"
    if not media.exists():
        media.write_bytes(b"x" * 2048)
    base: dict[str, Any] = dict(output=str(tmp_path / "out"), video=str(media), webcam_roi=WEBCAM_ROI,
                                cache_dir=str(tmp_path / "cache"), style_dir=str(tmp_path / "styles"))
    base.update(settings)
    return PackagingPipeline(PackagingSettings(**base), agent=cast(Any, agent or FakeAgentGen()),
                             generator=generator if generator is not None else FakeGenerator(),
                             transcription_engine=FakeEngine(), rms_extractor=fake_rms, keyframe_scanner=fake_scanner,
                             frame_sampler=FakeSampler(), duration_probe=lambda path: 330.0, planner=planner())


def make_v4(tmp_path: Path, **settings: Any) -> PackagingPipeline:
    """The opt-in V4.1 protected-compositing strategy (AI environment + local layers/typography)."""
    direction = {"generation_strategy": "reference_preserving", "strategy_version": 2,
                 **settings.pop("creative_direction", {})}
    return make(tmp_path, creative_direction=direction, **settings)


def test_gen_is_the_default_engine():
    assert PackagingSettings(output="x", video="v.mp4").thumbnail_engine == "gen"
    assert PackagingSettings(output="x", video="v.mp4").mode == "fast"


def test_fast_gen_is_one_planning_call_and_one_generation_call(tmp_path):
    agent, generator = FakeAgentGen(), FakeGenerator()
    report = make(tmp_path, agent=agent, generator=generator).run()
    out = tmp_path / "out"
    assert [c["task"] for c in agent.calls] == ["packaging_fast_gen"] and len(generator.calls) == 1
    assert report["thumbnail_engine"] == "gen" and report["generation"]["status"] == "done"
    assert report["generation"]["calls_made"] == 1
    prompt = agent.calls[0]["prompt"]
    assert "ASSET PACKAGE" in prompt and "VALID SCENE EXAMPLES" not in prompt and "D. PRODUCTION" not in prompt
    concepts = json.loads((out / "thumbnail_concepts.json").read_text(encoding="utf-8"))["concepts"]
    assert {c["role"] for c in concepts} == set(generate.ROLES)
    assert all(c["render"]["engine"] == "generated" for c in concepts)
    for n in range(1, 5):
        final = read_image(out / "thumbnails" / f"concept_{n:02d}.png")
        assert final is not None and final.shape[:2] == (1080, 1920)
        assert (out / "thumbnails" / "local" / f"concept_{n:02d}.png").is_file()  # local draft kept
    request = json.loads((out / "generation_request.json").read_text(encoding="utf-8"))
    assert len(request["packages"]) == 4 and len(request["images"]) <= generate.MAX_GENERATION_IMAGES
    sent = generator.calls[0]["images"]
    assert all(Path(p).is_file() and Path(p).suffix == ".jpg" for p in sent)
    reaction = next(p for p in request["packages"] if p["role"] == "reaction-led")
    assert reaction["images"][0]["kind"] == "face_main" and reaction["images"][0]["asset_id"].startswith("W")
    gen_prompt = generator.calls[0]["prompt"]
    assert f"PROMPT_VERSION: {generate.GENERATOR_VERSION}\n" in gen_prompt
    for phrase in ("not a live-stream box", "SAME real person", "Never invent or alter card", "2-3 strong elements",
                   "screenshot with stickers", "duration badge", "Animate it, do not change it", "spaghetti"):
        assert phrase in gen_prompt
    assert "Do not draw people" not in gen_prompt and "NO TEXT" not in gen_prompt  # V4 protected prompt
    assert report["generation"]["generation_strategy"] == "full_generative"
    assert report["generation"]["generator_version"] == generate.GENERATOR_VERSION
    assert report["generation"]["generator_base"] == "thumbnail_gen_v2"
    planning = json.loads((out / "planning_request.json").read_text(encoding="utf-8"))
    assert planning["schema_version"] == fast.PROMPT_VERSION_GEN
    assert not (out / "design_packs").exists() and not (out / "thumbnails" / "bases").exists()
    assert all("compositing" not in c["render"] and "typography" not in c["render"] for c in concepts)


def test_saved_v4_default_strategy_migrates_to_full_generative():
    from analysis.packaging.direction import CreativeDirection
    assert CreativeDirection().generation_strategy == "full_generative"
    legacy = CreativeDirection.from_mapping({"mode": "automatic", "generation_strategy": "reference_preserving"})
    assert legacy.generation_strategy == "full_generative"
    chosen = CreativeDirection.from_mapping(CreativeDirection(generation_strategy="reference_preserving").to_mapping())
    assert chosen.generation_strategy == "reference_preserving" and chosen.active().generation_strategy == "reference_preserving"


def test_clean_frame_crops_edge_webcam_instead_of_patching():
    image = np.zeros((1080, 1920, 3), np.uint8)
    image[:, :] = (40, 90, 160)
    image[554:842, :394] = (255, 255, 255)  # stream webcam overlay on the left edge
    cleaned = generate.clean_frame(image, [[0.001, 0.513, 0.2052, 0.2667]])
    assert cleaned.shape[0] == 1080 and 1500 < cleaned.shape[1] < 1540
    assert int(cleaned.max()) < 255  # no overlay pixels and no grey patch left
    centre = generate.clean_frame(image, [[0.4, 0.4, 0.2, 0.2]])
    assert centre.shape == image.shape  # not on an edge: inpainted in place


def test_every_variant_gets_its_own_text_style_and_grade_across_many_videos(tmp_path):
    from analysis.packaging.generate import (AssetPackage, PackageImage, choose_compositions,
                                             remember_compositions)
    history = tmp_path / "history.json"
    face = PackageImage("W001", "face_main", tmp_path / "w.jpg")
    packages = [AssetPackage("C1", "reaction-led", "", "", "", "", "", [face]),
                AssetPackage("C2", "card-led", "", "", "", "", ""),
                AssetPackage("C3", "board-led", "", "", "", "", ""),
                AssetPackage("C4", "narrative-led", "", "", "", "", "", [face])]
    previous: set[str] = set()
    for video in range(8):  # the old exclusion left one style/grade for all four every other video
        choice = choose_compositions(packages, video_key=f"video-{video}", history_path=history)
        assert len({c["text_style"] for c in choice.values()}) == 4
        assert len({c["grade"] for c in choice.values()}) == 4
        assert choice["C4"]["face"] != "none"  # a selected face is laid out big, not dropped to a corner
        assert all(not c["face"].startswith("small") for c in choice.values())
        if previous:  # 6 grades: the next video leads with the 2 the previous one did not use
            assert len({c["grade"] for c in choice.values()} - previous) >= 2
        previous = {c["grade"] for c in choice.values()}
        remember_compositions(history, f"video-{video}", choice)
    assert choose_compositions(packages, video_key="video-7", history_path=history) == choice  # rerun: cached


def test_stale_single_style_choice_is_not_reused(tmp_path):
    from analysis.packaging.generate import AssetPackage, choose_compositions
    history = tmp_path / "history.json"
    stale = {f"C{n}": {"recipe": "B1", "layout": "x", "face": "none", "text_style": "same", "grade": "same"}
             for n in range(1, 5)}
    history.write_text(json.dumps({"runs": [{"video": "v", "choice": stale}]}), encoding="utf-8")
    packages = [AssetPackage(f"C{n}", "board-led", "", "", "", "", "") for n in range(1, 5)]
    choice = choose_compositions(packages, video_key="v", history_path=history)
    assert len({c["grade"] for c in choice.values()}) == 4


def test_hook_repeating_its_title_takes_another_planned_title():
    from analysis.packaging.pipeline_v3 import untangle_hook_titles
    raw = {"titles": [{"title_id": "P1", "concept_id": "C1", "title": "Neanche il power? Che partita"},
                      {"title_id": "P2", "concept_id": "C2", "title": "Lo Stregone che non molla"}],
           "concepts": [{"concept_id": "C1", "thumbnail_text": "NEANCHE IL POWER?",
                         "title": "Neanche il power? Che partita"},
                        {"concept_id": "C2", "thumbnail_text": "Lo vedo forte", "title": "Lo Stregone che non molla"}]}
    notes = untangle_hook_titles(raw)
    assert raw["concepts"][0]["title"] == "Lo Stregone che non molla" and raw["concepts"][0]["thumbnail_text"] == "NEANCHE IL POWER?"
    assert raw["concepts"][1]["title"] == "Lo Stregone che non molla" and len(notes) == 1
    assert untangle_hook_titles({**raw, "_locked_hook": "x"}) == []
    raw["titles"].append({"title_id": "P3", "concept_id": "C2", "title": "Una partita da rivedere"})
    raw["concepts"][0]["title"] = "Neanche il power? Che partita"
    untangle_hook_titles(raw)  # a title already used by another concept is the last resort
    assert raw["concepts"][0]["title"] == "Una partita da rivedere"


def test_style_references_are_attached_as_finish_only(tmp_path):
    from analysis.packaging.generate import AssetPackage, PackageImage
    face, style = tmp_path / "W001.jpg", tmp_path / "style_01.jpg"
    for path in (face, style):
        write_image(path, np.zeros((64, 64, 3), np.uint8))
    package = AssetPackage("C1", "reaction-led", "Lo vedo forte", "", "", "", "",
                           [PackageImage("W001", "face_main", face)])
    prompt, images = generation_prompt([package], language="it", style_refs=[style])
    assert images == [face, style]
    assert "STYLE REFERENCES (images 2)" in prompt and "NOT content" in prompt
    assert "STYLE REFERENCES" not in generation_prompt([package], language="it")[0]


def test_rerender_reuses_generated_images_without_any_call(tmp_path):
    make(tmp_path).run()
    agent, generator = FakeAgentGen(), FakeGenerator()
    report = make(tmp_path, agent=agent, generator=generator, only="render", video=None).run()
    assert agent.calls == [] and generator.calls == []
    assert report["generation"]["cache_hit"] and report["generation"]["calls_made"] == 0
    assert all(t.get("engine") == "generated" for t in report["thumbnails"])


def test_generator_unavailable_or_failing_keeps_local_renders(tmp_path):
    report = make(tmp_path, generator=FakeGenerator(available=False)).run()
    assert report["status"] == "done" and report["generation"]["status"] == "unavailable"
    assert all(t.get("engine") == "local" for t in report["thumbnails"])
    assert len(list((tmp_path / "out" / "thumbnails").glob("concept_*.png"))) == 4
    report = make(tmp_path / "b", generator=FakeGenerator(fail=GenerationError("timeout", "slow"))).run()
    assert report["generation"]["status"] == "failed" and any("generation failed" in w for w in report["warnings"])


def test_partial_generation_falls_back_per_concept(tmp_path):
    report = make(tmp_path, generator=FakeGenerator(skip=["C2"])).run()
    engines = {t["concept_id"]: t.get("engine") for t in report["thumbnails"]}
    assert engines["C2"] == "local" and engines["C1"] == "generated"
    assert report["generation"]["status"] == "partial"


def test_gen_validation_requires_every_role(tmp_path):
    with pytest.raises(PackagingError):
        make(tmp_path, agent=FakeAgentGen(duplicate=True)).run()
    report = json.loads((tmp_path / "out" / "packaging_report.json").read_text(encoding="utf-8"))
    assert "roles" in report["error"]["detail"]


def test_reference_preserving_rejects_nondiverse_portfolio(tmp_path):
    direction = {"generation_strategy": "reference_preserving", "strategy_version": 2}
    with pytest.raises(PackagingError):
        make(tmp_path, agent=FakeAgentGen(duplicate=True), creative_direction=direction).run()
    report = json.loads((tmp_path / "out" / "packaging_report.json").read_text(encoding="utf-8"))
    assert "portfolio" in report["error"]["detail"]


def test_v3_engine_makes_no_generation_call(tmp_path):
    from youtube_packaging_fakes import FakeAgentV3

    generator = FakeGenerator()
    report = make(tmp_path, agent=FakeAgentV3(), generator=generator, thumbnail_engine="v3", max_deepen=0).run()
    assert generator.calls == [] and report["generation"] is None


# ---------- units ----------

ASSETS: dict[str, dict[str, Any]] = {
    "F001": {"type": "gameplay_frame"}, "F002": {"type": "gameplay_frame"},
    "B001": {"type": "board_region", "parent_id": "F001"},
    "W001": {"type": "webcam_original"}, "W002": {"type": "webcam_original"},
    "S001": {"type": "webcam_subject", "parent_id": "W001", "quality": {"isolated": 1.0}},
    "K01": {"type": "card_crop", "parent_id": "F001", "provenance": {"name": "Meccaciclo", "status": "legible"}},
}


def concept(cid: str, role: str, package: dict[str, list[str]], text: str = "Testo") -> dict[str, Any]:
    return {"concept_id": cid, "role": role, "package": package, "thumbnail_text": text, "moment": "m",
            "art_direction": "a", "message": "msg", "title": "titolo"}


def portfolio() -> dict[str, Any]:
    return {"concepts": [
        concept("C1", "reaction-led", {"face": ["S001", "W002"], "board": ["F001"], "detail": []}, "Non ci credo"),
        concept("C2", "card-led", {"face": [], "board": ["F002"], "detail": ["K01"]}, "Questa carta"),
        concept("C3", "board-led", {"face": [], "board": ["B001"], "detail": []}, "Board piena?"),
        concept("C4", "narrative-led", {"face": ["W002"], "board": ["F001", "F002"], "detail": []}, "")]}


def test_attach_drafts_builds_valid_local_scenes():
    raw = portfolio()
    assert attach_drafts(raw, ASSETS) == []
    by_id = {c["concept_id"]: c for c in raw["concepts"]}
    assert by_id["C1"]["primary_subject"] == "S001" and by_id["C1"]["treatment"] == "reaction_cutout"
    assert by_id["C2"]["primary_subject"] == "K01" and by_id["C3"]["primary_subject"] == "B001"
    for c in raw["concepts"]:
        assert validate_scene(c["scene"], ASSETS, where=c["concept_id"]) == []


def test_package_and_hook_rules():
    raw = portfolio()
    raw["concepts"][1]["package"]["detail"] = []  # card-led without a card
    raw["concepts"][0]["package"]["board"] = ["W001"]  # a webcam is not a board
    for c in raw["concepts"]:
        c["thumbnail_text"] = "Davvero?"
    problems = " | ".join(attach_drafts(raw, ASSETS))
    assert "card-led needs a card" in problems and "not a frame/region" in problems and "questions" in problems


def test_gen_schema_has_no_scene_and_requires_package():
    item = fast.RESPONSE_SCHEMA_GEN["properties"]["concepts"]["items"]
    assert "scene" not in item["properties"] and {"role", "package", "moment", "art_direction"} <= set(item["required"])
    assert item["properties"]["role"]["enum"] == list(generate.ROLES)


def test_asset_package_uses_original_webcam_and_masks_board(tmp_path):
    images = {k: np.full((360, 640, 3), 120, np.uint8) for k in ("F001", "F002", "B001", "W001", "W002", "K01")}
    images["F001"][:60, :100] = 255  # the stream's webcam overlay area
    raw = portfolio()
    attach_drafts(raw, ASSETS)
    packages = asset_package(raw["concepts"], ASSETS, images, tmp_path / "pk", mask_boxes=[[0.0, 0.0, 0.16, 0.17]])
    first = packages[0]
    assert [i.kind for i in first.images] == ["face_main", "face_alt", "board"]
    assert first.images[0].asset_id == "W001"  # the cut-out's original crop, not the cut-out
    board = read_image(tmp_path / "pk" / "F001.jpg")
    assert board is not None and board[:40, :60].mean() < 200  # overlay inpainted away
    assert "Meccaciclo" in packages[1].images[-1].note
    prompt, order = generation_prompt(packages, language="it")
    assert len(order) == len(set(order)) and "VARIANT C4" in prompt and "hook text: none" in prompt
    assert "image 1: main webcam crop" in prompt


def test_map_generated_by_reported_path_then_order(tmp_path):
    files = []
    for n in range(3):
        p = tmp_path / f"ig_{n}.png"
        p.write_bytes(b"x")
        files.append(p)
    answer = {"variants": [{"concept_id": "C2", "path": str(files[0])}, {"concept_id": "C1", "path": ""},
                           {"concept_id": "C3", "path": ""}]}
    assert map_generated(answer, files, ["C1", "C2", "C3"]) == {"C2": files[0], "C1": files[1], "C3": files[2]}
    assert map_generated({}, files[:2], ["C1", "C2"]) == {"C1": files[0], "C2": files[1]}


def test_normalize_generated_crops_to_16_9(tmp_path):
    source = tmp_path / "g.png"
    write_image(source, np.zeros((1024, 1536, 3), np.uint8))
    assert normalize_generated(source, tmp_path / "n.png") == (1536, 1024)
    out = read_image(tmp_path / "n.png")
    assert out is not None and out.shape[:2] == (1080, 1920)


def test_codex_generator_command_and_collection(tmp_path, monkeypatch):
    home = tmp_path / "codex_home"
    (home / "generated_images" / "old").mkdir(parents=True)
    (home / "generated_images" / "old" / "before.png").write_bytes(b"old")
    monkeypatch.setenv("CODEX_HOME", str(home))
    generator = generate.CodexImageGenerator(executable="codex")
    monkeypatch.setattr(generator._locator, "locate", lambda: "codex")
    seen: dict[str, Any] = {}

    def fake_process(cmd, stdin, workdir, timeout):
        seen.update(cmd=cmd, stdin=stdin)
        folder = home / "generated_images" / "thread"
        folder.mkdir(parents=True, exist_ok=True)
        for n in range(2):
            write_image(folder / f"ig_{n}.png", np.full((1024, 1536, 3), 40 * n, np.uint8))
        Path(cmd[cmd.index("-o") + 1]).write_text(json.dumps({"variants": [
            {"concept_id": "C1", "path": ""}, {"concept_id": "C2", "path": ""}]}), encoding="utf-8")
        return "", "tokens used\n12,345"

    monkeypatch.setattr("analysis.boundaries.agents.run_agent_process", fake_process)
    image = tmp_path / "a.jpg"
    write_image(image, np.zeros((10, 10, 3), np.uint8))
    result = generator.generate("prompt", [image], ["C1", "C2"], tmp_path / "raw")
    assert sorted(result) == ["C1", "C2"] and all(p.parent == tmp_path / "raw" for p in result.values())
    assert "--enable" in seen["cmd"] and "image_generation" in seen["cmd"] and "read-only" in seen["cmd"]
    assert any(str(c).startswith("--image=") for c in seen["cmd"]) and generator.meta["tokens_used"] == 12345
    assert (home / "generated_images" / "old" / "before.png").read_bytes() == b"old"  # user files untouched
