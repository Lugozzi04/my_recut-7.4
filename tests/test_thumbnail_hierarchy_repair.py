"""Advisory layout references recover locally; factual asset validation stays strict."""
from copy import deepcopy
import json

import pytest

from analysis.packaging.design import LEGACY_REQUIRED, prepare_design_hierarchies, validate_design
from test_thumbnail_v4 import V4Agent
from test_youtube_packaging_gen import make_v4 as make
from youtube_packaging_fakes import FakeGenerator


ASSETS = {"F1": {"type": "gameplay_frame"}, "W1": {"type": "webcam_original"}}


def answer(hierarchy):
    d = {key: "" for key in LEGACY_REQUIRED}
    d.update(story_angle="A real board", evidence_ids=["F1"], story_confidence="supported",
             main_subject="F1", secondary_subject="", must_use_assets=["F1"], optional_assets=[],
             must_not_use_assets=[], visual_hierarchy=hierarchy, layout_archetype="clutch_moment",
             text_position="top", typography_mode="precision", effect_intensity=1)
    return {"concepts": [{"concept_id": "C1", "package": {"board": ["F1"], "face": ["W1"], "detail": []},
                          "design": d}]}


@pytest.mark.parametrize("value", ["text", "thumbnail_hook", "gameplay_board", "K_INVENTED", "F999", ""])
def test_invalid_hierarchy_entry_rebuilt_using_selected_real_assets_only(value):
    raw = answer(["F1", value, "W1"])
    original = deepcopy(raw)
    fixed = prepare_design_hierarchies(raw, ASSETS)
    assert raw == original
    c = fixed["concepts"][0]
    validate_design(c, ASSETS, set(ASSETS))
    assert c["design"]["visual_hierarchy"] == ["F1", "W1"]
    assert fixed["_design_repairs"][0]["removed"] == [value]
    assert not fixed["_design_repairs"][0]["new_evidence_created"]
    assert prepare_design_hierarchies(fixed, ASSETS) == fixed


def test_valid_order_is_unchanged_and_unknown_mandatory_asset_is_rejected():
    raw = answer(["W1", "F1"])
    assert prepare_design_hierarchies(raw, ASSETS) == raw
    raw["concepts"][0]["design"]["must_use_assets"].append("K_INVENTED")
    fixed = prepare_design_hierarchies(raw, ASSETS)
    with pytest.raises(ValueError, match="must_use_assets.*K_INVENTED"):
        validate_design(fixed["concepts"][0], ASSETS, set(ASSETS))


def test_invalid_main_subject_is_not_repaired_by_layout_adapter():
    raw = answer(["K_INVENTED", "F1"])
    raw["concepts"][0]["design"]["main_subject"] = "K_INVENTED"
    assert prepare_design_hierarchies(raw, ASSETS) == raw
    with pytest.raises(ValueError, match="Unknown"):
        validate_design(raw["concepts"][0], ASSETS, set(ASSETS))


def test_unknown_reference_error_names_concept_field_and_bad_value():
    raw = answer(["F404"])
    with pytest.raises(ValueError, match="C1: Unknown V4 asset in visual_hierarchy.*F404"):
        validate_design(raw["concepts"][0], ASSETS, set(ASSETS))


@pytest.mark.parametrize("hierarchy", ["F1", ["F1", {"id": "F404"}], ["F1", None]])
def test_malformed_layout_values_fail_clearly(hierarchy):
    raw = answer(hierarchy)
    assert prepare_design_hierarchies(raw, ASSETS) == raw
    with pytest.raises(ValueError, match="visual_hierarchy"):
        validate_design(raw["concepts"][0], ASSETS, set(ASSETS))


def test_unselected_real_asset_cannot_leak_into_output_hierarchy():
    raw = answer(["F1", "OTHER_CLASS_FRAME"])
    assets = {**ASSETS, "OTHER_CLASS_FRAME": {"type": "gameplay_frame"}}
    fixed = prepare_design_hierarchies(raw, assets)
    assert fixed["concepts"][0]["design"]["visual_hierarchy"] == ["F1", "W1"]
    assert "OTHER_CLASS_FRAME" not in fixed["concepts"][0]["package"]["board"]


def test_real_planning_failure_shape_recovers_without_second_fake_call(tmp_path):
    class HierarchyAgent(V4Agent):
        def packages(self, prompt):
            raw = super().packages(prompt)
            for c in raw["concepts"]:
                c["design"]["visual_hierarchy"] = [*c["design"]["visual_hierarchy"], "text", "K_NOT_IN_CATALOG"]
            return raw

    agent, generator = HierarchyAgent(), FakeGenerator()
    pipeline = make(tmp_path, agent=agent, generator=generator)
    report = pipeline.run()
    assert report["status"] == "done" and len(report["thumbnails"]) == 4
    assert len(agent.calls) == len(generator.calls) == 1
    repairs = json.loads((pipeline.out / "design_repairs.json").read_text(encoding="utf-8"))
    assert len(repairs["items"]) == 4
    assert any("Invalid visual hierarchy" in warning for warning in report["warnings"])
    for c in pipeline._planned[2]["concepts"]:
        p = c["design_pack"]
        assert p["planning_repairs"]
        assert "text" not in p["visual_hierarchy"] and "K_NOT_IN_CATALOG" not in p["visual_hierarchy"]
