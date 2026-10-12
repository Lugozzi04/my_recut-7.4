from __future__ import annotations
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import numpy as np
import pytest

cv = pytest.importorskip("cv2")
from analysis.hero_groups.recognition import HeroGroupingService, PortraitSettings, features, refresh_groups
from analysis.hero_groups.classes import classify_records, class_groups
from analysis.hero_groups.labels import CLASS_COLORS, class_id, class_label, default_class_refs, timeline_islands
from analysis.hero_groups.projects import assign_group, prepare_outputs, read_project
from test_hero_groups import art, project, record, grouped


def evidence(name="hunter", score=0.74, other="mage", alternative=0.40):
    return {"class": name, "scores": {name: score, other: alternative}, "support": 5, "status": "matched"}


def test_default_references_are_repo_relative_not_cwd(tmp_path, monkeypatch):
    import analysis.hero_groups.labels as labels

    refs = tmp_path / "app" / "class_refs"
    refs.mkdir(parents=True)
    cv.imwrite(str(refs / "hunter.png"), cv.resize(art(), (64, 64)))
    other = tmp_path / "other cwd"
    other.mkdir()
    monkeypatch.chdir(other)
    monkeypatch.setattr(labels, "resource_path", lambda name: tmp_path / "app" / name)
    monkeypatch.setattr(labels, "is_frozen", lambda: False)
    service = HeroGroupingService(board_gate=lambda f: 1, cache_dir=tmp_path / "cache")
    assert service.class_refs == refs and set(service._references()) == {"hunter"}


def test_frozen_references_allow_folder_next_to_executable(tmp_path, monkeypatch):
    import analysis.hero_groups.labels as labels

    folder = tmp_path / "class_refs"
    folder.mkdir()
    monkeypatch.setattr(labels, "is_frozen", lambda: True)
    monkeypatch.setattr(labels.sys, "executable", str(tmp_path / "AutoCutter.exe"))
    assert default_class_refs() == folder


def test_multiple_power_frames_name_a_single_game_and_record_margin(tmp_path, monkeypatch):
    service = HeroGroupingService(board_gate=lambda f: 1, cache_dir=tmp_path)
    hunter, mage = art(), art(2)
    monkeypatch.setattr(service, "_power_score", lambda image, ref, **kw: 0.74 if np.array_equal(ref, hunter) else 0.51)
    power = service._power_evidence([art(3)] * 5, {"hunter": hunter, "mage": mage})
    assert power["class"] == "hunter" and power["status"] == "matched"
    assert power["margin"] == pytest.approx(0.23) and power["support"] == 5
    r = record(1, art())
    r["power_evidence"] = power
    classify_records([r], service.settings)
    g = class_groups([r], service.settings)
    assert g["games"][0]["group"] == "hunter" and g["groups"]["hunter"]["name"] == "Hunter"


def test_weak_best_match_is_named_but_marked_estimated(tmp_path, monkeypatch):
    service = HeroGroupingService(board_gate=lambda f: 1, cache_dir=tmp_path)
    a, b = art(), art(2)
    monkeypatch.setattr(service, "_power_score", lambda image, ref, **kw: 0.53 if np.array_equal(ref, a) else 0.52)
    power = service._power_evidence([art(3)] * 5, {"hunter": a, "mage": b})
    r = record(1, art())
    r["power_evidence"] = power
    classify_records([r], service.settings)
    assert r["class_evidence"] == "hunter" and r["class_status"] == "estimated"
    assert r["class_margin"] == pytest.approx(0.01)


def test_animated_portrait_without_local_consensus_recovers_by_known_skin():
    anchor = record(1, art())
    anchor["power_evidence"] = evidence()
    target = record(2, art())
    target["_signature"] = None
    target["_sample_features"] = [features(art()) for _ in range(3)]
    target["power_evidence"] = {**evidence(score=0.69, alternative=0.50), "status": "estimated"}
    classify_records([anchor, target], PortraitSettings())
    assert target["class_evidence"] == "hunter" and target["class_source"] == "portrait"
    assert target["class_support"] == 3 and target["class_similarity"] > 0.99
    assert class_groups([anchor, target], PortraitSettings())["groups"]["hunter"]["games"] == ["1", "2"]


def test_conflicting_skin_and_power_is_explicit_for_manual_review():
    anchor = record(1, art())
    anchor["power_evidence"] = evidence("mage", other="hunter")
    target = record(2, art())
    target["_sample_features"] = [features(art()) for _ in range(3)]
    target["power_evidence"] = evidence("hunter", other="mage")
    classify_records([anchor, target], PortraitSettings())
    assert target["class_evidence"] == "mage" and target["class_status"] == "conflict"


def test_no_valid_frames_do_not_fabricate_a_class():
    r = record(1, art())
    r["power_evidence"] = {"scores": {}, "status": "no_samples"}
    classify_records([r], PortraitSettings())
    assert r["class_evidence"] is None and r["class_status"] == "no_samples"


def test_legacy_letters_migrate_preserving_manual_edits_and_names():
    old = grouped()
    assign_group(old, "2", "A")
    old["names"]["A"] = "My videos"
    old["selected"] = ["A"]
    records = [record(1, art()), record(2, art(2)), record(3, art())]
    for row, name in zip(records, ("druid", "hunter", "druid")):
        row["class_evidence"] = name
    result = class_groups(records, PortraitSettings(), old)
    assert result["overrides"]["2"] == "druid" and result["games"][1]["group"] == "druid"
    assert result["names"]["druid"] == "My videos" and result["selected"] == ["druid"]


def test_class_cache_and_manual_override_survive_recalculation(tmp_path, monkeypatch):
    source = tmp_path / "video.mp4"
    source.write_bytes(b"media")

    class Sampler:
        calls = 0

        def sample(self, *a, **kw):
            self.calls += 1
            image = np.zeros((540, 960, 3), np.uint8)
            image[382:451, 447:513] = cv.resize(art(), (66, 69))
            image[374:452, 543:621] = cv.resize(art(2), (78, 78))
            yield SimpleNamespace(timestamp=kw["start_s"], image=image)

    sampler = Sampler()
    service = HeroGroupingService(sampler=sampler, board_gate=lambda f: 1, cache_dir=tmp_path / "cache")
    monkeypatch.setattr(service, "_references", lambda: {"hunter": cv.resize(art(2), (78, 78))})
    first = service.analyze(source, [record(1, art())["game"]], duration=500)
    assert first["games"][0]["group"] == "hunter"
    assign_group(first, "1", "Mage")
    first["names"]["mage"] = "Custom"
    again = service.analyze(source, [record(1, art())["game"]], duration=500, previous=first)
    assert again["cache_hit"] and sampler.calls == 10 and again["games"][0]["group"] == "mage"
    assert again["groups"]["mage"]["name"] == "Custom"


def test_named_classes_export_only_current_win_keeps_and_persist(tmp_path):
    p = project(tmp_path)
    g = p["tracks"][0]["gameplay_grouping"]
    for row in g["games"]:
        row["group"] = "hunter" if row["group"] == "A" else "druid"
    g["selected"] = ["hunter", "druid"]
    refresh_groups(g)
    plans, warnings = prepare_outputs(p, tmp_path / "outputs", ["Hunter"])
    assert len(plans) == 1 and not warnings and plans[0]["group"] == "hunter"
    restored = read_project(Path(plans[0]["project"]))
    assert [x["index"] for x in restored["gameplay"]["games"]] == [1, 3]
    assert restored["hero_group_output"]["name"] == "Hunter"


@pytest.mark.parametrize("value,expected", [("dh", "demon_hunter"), ("Death Knight", "death_knight"), ("MAGE", "mage")])
def test_class_aliases(value, expected):
    assert class_id(value) == expected and class_label(value) != "Da assegnare"


def test_timeline_metadata_is_decorated_without_boundary_mutation():
    original = [{"start": 10, "end": 20, "game_id": "1", "game_index": 1}]
    grouping = {
        "games": [{"key": "1", "group": "mage", "game": {"index": 1}}],
        "groups": {"mage": {"class_name": "mage"}},
    }
    decorated = timeline_islands(original, grouping)
    assert original == [{"start": 10, "end": 20, "game_id": "1", "game_index": 1}]
    assert decorated == [{**original[0], "hero_class": "mage"}]


def test_timeline_uses_same_class_color_on_all_win_islands():
    from PySide6.QtWidgets import QApplication
    from test_timeline_widget import make_timeline
    from analysis.cut_engine import Segment

    app = QApplication.instance() or QApplication([])
    tl = make_timeline()
    clip = tl._audio_clips()[0]
    clip["gameplay_keeps"] = [Segment(20, 40), Segment(45, 60)]
    clip["gameplay_islands"] = [
        {"start": 20, "end": 40, "hero_class": "mage"},
        {"start": 45, "end": 60, "hero_class": "mage"},
    ]
    islands = tl._gameplay_islands()
    assert [tl._island_color(i).name() for i in islands] == [CLASS_COLORS["mage"].lower()] * 2
    assert tl._island_text_color(tl._island_color(islands[0])).isValid()
    tl.show()
    app.processEvents()
    assert not tl.grab().isNull()
    tl.close()
