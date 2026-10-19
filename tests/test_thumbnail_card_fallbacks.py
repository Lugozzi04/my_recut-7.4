"""Regression for tiny card boxes and uncertain protagonists before V4 validation."""
from __future__ import annotations

from copy import deepcopy

import pytest

from analysis.packaging.cards import prepare_card_fallbacks
from analysis.packaging import creative
from test_thumbnail_v4 import V4Agent
from test_youtube_packaging_gen import make_v4 as make
from youtube_packaging_fakes import FakeGenerator

ASSETS = {"F1": {"type": "gameplay_frame"}, "F2": {"type": "gameplay_frame"}}
BOX = [0.91, 0.471, 0.085, 0.022]


def answer(box=BOX, *, legibility="clear", frame="F1"):
    return {"card_observations": [{"obs_id": "O1", "frame_id": frame, "box": list(box),
                                  "name": "Unverified Card", "legibility": legibility}],
            "concepts": [{"concept_id": "C1", "role": "card-led",
                          "package": {"face": [], "board": ["F2"], "detail": ["O1"]},
                          "primary_subject": "O1", "supporting": [],
                          "evidence": [{"asset_ids": ["O1"]}],
                          "design": {"main_subject": "O1", "secondary_subject": "",
                                     "must_use_assets": ["O1", "F2"], "optional_assets": [],
                                     "must_not_use_assets": [], "visual_hierarchy": ["O1"],
                                     "evidence_ids": ["O1"]}}],
            "portfolio_strategy": {"plan": [{"concept_id": "C1", "primary_subject": "O1"}]}}


def test_exact_reported_tiny_box_falls_back_without_widening_or_recognition():
    raw = answer()
    original = deepcopy(raw)
    recovered = prepare_card_fallbacks(raw, ASSETS)
    assert raw == original
    assert recovered["card_observations"] == []
    assert recovered["_card_fallbacks"][0]["box"] == BOX
    assert not recovered["_card_fallbacks"][0]["verified"]
    c = recovered["concepts"][0]
    assert c["package"]["detail"] == ["F1"]
    assert c["role"] == "board-led"
    assert c["primary_subject"] == c["design"]["main_subject"] == "F1"
    assert c["design"]["must_use_assets"] == ["F1", "F2"]
    assert c["design"]["story_confidence"] == "uncertain"
    assert c["evidence"][0]["asset_ids"] == ["F1"]


@pytest.mark.parametrize("legibility", ["partial", "unreadable"])
def test_uncertain_reading_is_retained_as_uncertain_but_not_a_protagonist(legibility):
    recovered = prepare_card_fallbacks(answer([.1, .2, .2, .4], legibility=legibility), ASSETS)
    assert recovered["card_observations"][0]["legibility"] == legibility
    assert recovered["concepts"][0]["package"]["detail"] == ["F1"]
    assert recovered["_card_fallbacks"][0]["reason"] == "reading_uncertain"


def test_clear_full_card_reading_is_unchanged():
    raw = answer([.1, .2, .2, .4])
    assert prepare_card_fallbacks(raw, ASSETS) == raw


@pytest.mark.parametrize("box", [[.9, .2, .2, .4], [.1, .2, 0., .1], [.1, .2, -.1, .1],
                                [.1, .2, .1, float("nan")], [.1, .2, .1, float("inf")]])
def test_malformed_or_out_of_frame_geometry_is_not_hidden(box):
    recovered = prepare_card_fallbacks(answer(box), ASSETS)
    assert len(recovered["card_observations"]) == 1
    assert recovered["concepts"][0]["package"]["detail"] == ["O1"]
    assert not creative._box_ok(box)


def test_unknown_frame_does_not_get_a_fabricated_replacement():
    recovered = prepare_card_fallbacks(answer(frame="INVENTED"), ASSETS)
    assert recovered["card_observations"][0]["frame_id"] == "INVENTED"
    assert recovered["concepts"][0]["package"]["detail"] == ["O1"]


def test_existing_uncertain_catalog_card_uses_its_real_parent():
    raw = answer([.1, .2, .2, .4])
    raw["card_observations"] = []
    c = raw["concepts"][0]
    c["package"]["detail"] = ["K1"]
    c["primary_subject"] = "K1"
    assets = {**ASSETS, "K1": {"type": "card_crop", "parent_id": "F1",
                              "provenance": {"status": "uncertain"}}}
    recovered = prepare_card_fallbacks(raw, assets)
    assert recovered["concepts"][0]["package"]["detail"] == ["F1"]


class ReportedFailureAgent(V4Agent):
    def packages(self, prompt):
        raw = super().packages(prompt)
        obs = raw["card_observations"][0]
        obs.update(legibility="partial", name="Unverified Card")
        raw["card_observations"].append({**obs, "obs_id": "O_tiny", "box": BOX, "legibility": "clear"})
        webcams = raw["concepts"][0]["package"]["face"]
        for i, c in enumerate(raw["concepts"]):
            if i in (0, 3):
                c["role"] = "card-led"
                c["package"]["face"] = []
                c["package"]["detail"] = [obs["obs_id"]]
            elif i == 1:
                c["role"] = "reaction-led"
                c["package"]["face"] = webcams
                c["package"]["detail"] = []
            else:
                c["role"] = "narrative-led"
                c["package"]["detail"] = []
            selected = [a for ids in c["package"].values() for a in ids]
            c["design"].update(main_subject=selected[0], must_use_assets=selected, visual_hierarchy=selected)
        return raw


def test_same_failure_shape_recovers_four_concepts_in_one_fake_planning_call(tmp_path):
    agent, generator = ReportedFailureAgent(), FakeGenerator()
    pipeline = make(tmp_path, agent=agent, generator=generator)
    report = pipeline.run()
    assert report["status"] == "done" and len(report["thumbnails"]) == 4
    assert len(agent.calls) == 1 and len(generator.calls) == 1
    assert (pipeline.out / "card_fallbacks.json").is_file()
    assert any("Uncertain or tiny card crops" in w for w in report["warnings"])
    for c in pipeline._planned[2]["concepts"]:
        assert "O1" not in c["package"]["detail"]
        assert all(r["type"] != "card_crop" or r["provenance"]["status"] != "uncertain"
                   for r in c["design_pack"]["references"])


def test_unverified_card_name_still_cannot_be_used_as_a_factual_title(tmp_path):
    class NamedCardAgent(ReportedFailureAgent):
        def packages(self, prompt):
            raw = super().packages(prompt)
            raw["concepts"][0]["title"] = "La vittoria grazie a Unverified Card"
            return raw
    with pytest.raises(Exception, match="uncertain card reading"):
        make(tmp_path, agent=NamedCardAgent()).run()
