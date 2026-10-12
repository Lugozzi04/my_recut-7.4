from __future__ import annotations

from copy import deepcopy
import itertools
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

cv = pytest.importorskip("cv2")
from analysis.hero_groups.recognition import (
    HeroGroupingService,
    PortraitSettings,
    features,
    group_signatures,
    signature,
    sample_times,
    refresh_groups,
)
from analysis.hero_groups.projects import (
    assign_group,
    group_project,
    prepare_outputs,
    read_project,
    export_outputs,
    analyze_project,
)
from core.project_file import make_payload_portable, normalize_project_payload
from automation.manager import PipelineManager
from automation.store import PipelineStore


def art(seed=1):
    rng = np.random.default_rng(seed)
    return cv.GaussianBlur(rng.integers(15, 200, (80, 64, 3), np.uint8), (3, 3), 0)


def record(index, image, result="WIN"):
    return {
        "game": {"id": str(index), "index": index, "start": index * 100.0, "end": index * 100.0 + 90, "result": result},
        "_signature": features(image),
        "portrait": "",
        "similarity": 1.0,
        "support": 5,
    }


def grouped():
    return group_signatures([record(1, art()), record(2, art(2)), record(3, art())], PortraitSettings())


def project(tmp_path):
    source = tmp_path / "sorgente è 🎮.mp4"
    source.write_bytes(b"source")
    g = grouped()
    games = [r["game"] for r in g["games"]]
    islands = [{"start": v["start"] - 5, "end": v["end"] + 5, "game": v} for v in games]
    keeps = [{"start": i["start"], "end": i["end"]} for i in islands]
    # Existing Classic/manual cut: remove 110..120 inside game 1.
    final = [dict(keeps[0], end=110), dict(keeps[0], start=120), *keeps[1:]]
    return {
        "format": "autocutter_project",
        "version": 2,
        "project_name": "VOD",
        "tracks": [
            {
                "path": str(source),
                "duration": 500.0,
                "segment_source_in": 0.0,
                "segment_source_out": 500.0,
                "cuts_enabled": True,
                "keeps": final,
                "gameplay_mode": "keep",
                "gameplay_keeps": keeps,
                "gameplay_grouping": g,
                "gameplay_report": {"games": games, "keep_islands": islands},
            }
        ],
    }


def test_same_skin_brightness_and_distinct_art():
    a = art()
    bright = np.clip(a.astype(float) * 1.08 + 12, 0, 255).astype(np.uint8)
    s, support, score, _ = signature([a, bright, a, bright, a], PortraitSettings())
    assert s is not None and support == 5 and score > 0.88
    result = group_signatures([record(1, a), record(2, bright), record(3, art(4))], PortraitSettings())
    assert [r["group"] for r in result["games"]] == ["A", "A", "B"]


def test_order_independent_noncontiguous_and_no_bridge(monkeypatch):
    records = [record(1, art()), record(2, art(2)), record(3, art())]
    for permutation in itertools.permutations(records):
        assert [r["group"] for r in group_signatures(deepcopy(list(permutation)), PortraitSettings())["games"]] == [
            "A",
            "B",
            "A",
        ]
    import analysis.hero_groups.recognition as module

    records = [record(i, art(i)) for i in range(3)]
    for i, r in enumerate(records):
        r["_signature"] = i
    monkeypatch.setattr(module, "agrees", lambda a, b, s: abs(a - b) <= 1)
    result = group_signatures(records, PortraitSettings())
    assert all(not r.get("group") for r in result["games"])


def test_covered_flat_and_inconsistent_frames_unknown():
    assert features(np.zeros((80, 64, 3), np.uint8)) is None
    assert signature([art(i) for i in range(8)], PortraitSettings())[0] is None
    assert signature([art(), art(), np.zeros((80, 64, 3), np.uint8)], PortraitSettings())[0] is None
    result = group_signatures([record(1, np.zeros((80, 64, 3), np.uint8))], PortraitSettings())
    assert result["games"][0].get("group") is None


def test_sampling_short_partial_and_missing_start():
    settings = PortraitSettings()
    assert sample_times({"start": None, "end": 100}, 100, settings) == []
    times = sample_times({"start": 10, "end": 30}, 100, settings)
    assert len(times) == 10 and min(times) > 10 and max(times) < 30
    assert sample_times({"start": 10, "end": 12}, 100, settings) == []


def test_manual_edits_names_selection_survive_project_round_trip_and_recalculation(tmp_path):
    p = project(tmp_path)
    g = p["tracks"][0]["gameplay_grouping"]
    assign_group(g, "2", "A")
    g["names"]["A"] = "La mia classe"
    g["selected"] = ["A"]
    refresh_groups(g)
    target = tmp_path / "edit.autocutter"
    target.write_text(json.dumps(make_payload_portable(p, target)), encoding="utf-8")
    restored = read_project(target)["tracks"][0]["gameplay_grouping"]
    result = group_signatures([record(1, art()), record(2, art(2)), record(3, art())], PortraitSettings(), restored)
    assert result["games"][1]["group"] == "A"
    assert result["names"]["A"] == "La mia classe" and result["selected"] == ["A"]
    assert normalize_project_payload({"tracks": [{"path": "old.mp4"}]})["version"] == 2


def test_group_keeps_order_existing_cuts_boundary_and_output_isolation(tmp_path):
    p = project(tmp_path)
    child = group_project(p, "A")
    assert child["tracks"][0]["keeps"] == [
        {"start": 95.0, "end": 110},
        {"start": 120, "end": 195.0},
        {"start": 295.0, "end": 395.0},
    ]
    assert [g["index"] for g in child["gameplay"]["games"]] == [1, 3]
    assert child["hero_group_output"]["name"] is None
    plans, warnings = prepare_outputs(p, tmp_path / "output", ["A", "B"])
    assert len(plans) == 2 and not warnings
    assert plans[0]["id"] != plans[1]["id"]
    assert Path(plans[0]["project"]).parent != Path(plans[1]["project"]).parent
    assert p["tracks"][0]["gameplay_grouping"]["games"][0]["group"] == "A"


@pytest.mark.parametrize("container", ["mkv", "mov"])
def test_group_output_preserves_export_container(tmp_path, container):
    p = project(tmp_path)
    p["global"] = {"export_settings": {"container": container}}
    plans, warnings = prepare_outputs(p, tmp_path / "outputs", ["A"])
    assert not warnings and len(plans) == 1
    assert Path(plans[0]["output"]).suffix == "." + container
    assert read_project(Path(plans[0]["project"]))["global"] == p["global"]


def test_current_edited_islands_and_split_local_domain(tmp_path):
    p = project(tmp_path)
    t = p["tracks"][0]
    t["gameplay_islands"] = [{"game_index": i} for i in (1, 2, 3)]
    t["gameplay_keeps"][0] = {"start": 90.0, "end": 198.0}
    t["keeps"][0]["start"] = 90
    t["keeps"][1]["end"] = 198
    child = group_project(p, "A")
    assert child["tracks"][0]["keeps"][0]["start"] == 90
    assert child["gameplay"]["keep_islands"][0]["start"] == 90


def test_group_without_win_is_warned_no_empty_output(tmp_path):
    p = project(tmp_path)
    g = p["tracks"][0]["gameplay_grouping"]
    g["games"][1]["game"]["result"] = "LOSS"
    plans, warnings = prepare_outputs(p, tmp_path / "out", ["A", "B"])
    assert len(plans) == 1 and len(warnings) == 1 and "no exportable WIN" in warnings[0]


def test_export_uses_shared_service_separate_jobs_no_analysis(tmp_path):
    p = project(tmp_path)
    plans, _ = prepare_outputs(p, tmp_path / "out")
    manager = PipelineManager(PipelineStore(tmp_path / "jobs.json"))

    class FakeExport:
        def execute(self, job_id, **kwargs):
            job = manager.start_export(job_id)
            output = Path(job.metadata["delivery"]["output_path"])
            output.write_bytes(b"video")
            return manager.mark_exported(job.id, output)

    result = export_outputs(plans, manager=manager, service=FakeExport())
    assert len(result) == 2 and len({r["job_id"] for r in result}) == 2
    assert all(j.state.value == "done" for j in manager.list_jobs())


def test_hero_power_optional_missing_refs_and_multigame_evidence(tmp_path):
    service = HeroGroupingService(board_gate=lambda f: 1, cache_dir=tmp_path / "cache", class_refs=tmp_path / "missing")
    assert service._references() == {}
    assert service._classify([art()] * 4, {}) is None
    assert service._classify([art()] * 4, {"hunter": art()}) == "hunter"
    result = grouped()
    result["games"][0]["class_evidence"] = "hunter"
    refresh_groups(result)
    assert result["groups"]["A"]["class_name"] == "hunter"
    result["games"][2]["class_evidence"] = "hunter"
    refresh_groups(result)
    assert result["groups"]["A"]["class_name"] == "hunter"


def test_power_references_load_square_icons_and_full_cards_without_downloads(tmp_path):
    refs = tmp_path / "refs"
    refs.mkdir()
    icon = cv.resize(art(), (64, 64))
    assert cv.imwrite(str(refs / "hunter.png"), icon)
    card = np.full((300, 200, 3), 30, dtype=np.uint8)
    artwork = cv.resize(art(2), (62, 62))
    card[63:125, 69:131] = artwork
    assert cv.imwrite(str(refs / "mage.png"), card)
    (refs / "invalid.png").write_bytes(b"not an image")
    service = HeroGroupingService(board_gate=lambda f: 1, cache_dir=tmp_path / "cache", class_refs=refs)
    loaded = service._references()
    assert set(loaded) == {"hunter", "mage"}
    assert loaded["mage"].shape == artwork.shape
    assert service._classify([icon] * 4, loaded) == "hunter"
    assert service._classify([artwork] * 4, loaded) == "mage"


def test_service_cache_board_rejection_manual_override_and_cancel(tmp_path):
    source = tmp_path / "local.mp4"
    source.write_bytes(b"fake-media")

    class Sampler:
        calls = 0

        def probe(self, *args, **kwargs):
            return SimpleNamespace(duration_s=500.0)

        def sample(self, *args, **kwargs):
            self.calls += 1
            frame = np.zeros((540, 960, 3), np.uint8)
            frame[382:451, 447:513] = cv.resize(art(), (66, 69))
            yield SimpleNamespace(timestamp=kwargs["start_s"], image=frame)

    sampler = Sampler()
    service = HeroGroupingService(sampler=sampler, board_gate=lambda f: 1.0, cache_dir=tmp_path / "cache", class_refs=tmp_path / "absent-refs")
    games = [record(1, art())["game"]]
    result = service.analyze(source, games, duration=500.0)
    assert result["games"][0]["group"] == "A"
    assign_group(result, "1", "C")
    cached = service.analyze(source, games, duration=500.0, previous=result)
    assert cached["cache_hit"] and sampler.calls == 10 and cached["games"][0]["group"] == "C"
    from analysis.audio_service import AnalysisCancellation

    token = AnalysisCancellation()
    token.cancel()
    with pytest.raises(Exception, match="CANCELLED"):
        service.analyze(source, games, duration=500.0, cancellation=token)
    rejected = HeroGroupingService(sampler=sampler, board_gate=lambda f: 0.0, cache_dir=tmp_path / "rejected").analyze(
        source, games, duration=500.0
    )
    assert rejected["games"][0].get("group") is None


def test_project_analysis_does_not_detect_again(tmp_path):
    p = project(tmp_path)

    class Service:
        sampler = SimpleNamespace(probe=lambda *a, **kw: SimpleNamespace(duration_s=500))

        def analyze(self, source, games, **kwargs):
            return grouped()

    assert analyze_project(p, service=Service())["tracks"][0]["gameplay_grouping"]["groups"]
    with pytest.raises(ValueError, match="Detect games first"):
        analyze_project({"tracks": [{"path": "foo"}]}, service=Service())


def test_shared_border_does_not_merge_different_portraits():
    a, b = art(7), art(8)
    for image in (a, b):
        image[:3, :] = [30, 180, 230]
        image[-3:, :] = [30, 180, 230]
        image[:, :3] = [30, 180, 230]
        image[:, -3:] = [30, 180, 230]
    result = group_signatures([record(1, a), record(2, b)], PortraitSettings())
    assert result["games"][0]["group"] != result["games"][1]["group"]


def test_conflicting_multiframe_identity_is_unknown():
    assert signature([art()] * 4 + [art(2)] * 4, PortraitSettings())[0] is None


def test_power_reference_tolerates_brightness_and_normalized_size(tmp_path):
    service = HeroGroupingService(board_gate=lambda f: 1, cache_dir=tmp_path / "cache")
    changed = np.clip(art().astype(float) * 1.04 + 10, 0, 255).astype(np.uint8)
    assert service._classify([changed] * 4, {"hunter": cv.resize(art(), (128, 160))}) == "hunter"


def test_cache_invalidation_and_corrupt_cache_fallback(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"media")

    class Sampler:
        calls = 0

        def sample(self, *args, **kw):
            self.calls += 1
            frame = np.zeros((540, 960, 3), np.uint8)
            frame[382:451, 447:513] = cv.resize(art(), (66, 69))
            yield SimpleNamespace(timestamp=kw["start_s"], image=frame)

    sampler = Sampler()
    service = HeroGroupingService(sampler=sampler, board_gate=lambda f: 1, cache_dir=tmp_path / "cache")
    games = [record(1, art())["game"]]
    result = service.analyze(source, games, duration=500)
    key = result["cache_key"]
    (tmp_path / "cache" / f"{key}.json").write_text("[]", encoding="utf-8")
    assert not service.analyze(source, games, duration=500)["cache_hit"]
    assert sampler.calls == 20
    games[0]["result"] = "LOSS"
    assert service.analyze(source, games, duration=500)["games"][0]["game"]["result"] == "LOSS"
    assert sampler.calls == 30
    source.write_bytes(b"changed media")
    assert service.analyze(source, games, duration=500)["cache_key"] != key
    assert sampler.calls == 40


def test_retired_group_ids_are_not_reused_for_a_different_skin():
    previous = group_signatures([record(1, art())], PortraitSettings())
    assign_group(previous, "1", "D")
    assign_group(previous, "1", None)
    previous["names"]["D"] = "Manual label"
    result = group_signatures([record(2, art(2))], PortraitSettings(), previous)
    assert result["games"][0]["group"] == "B"
    assert "A" in result["allocated_ids"] and "D" in result["allocated_ids"]
    assert result["groups"]["B"]["name"] == ""
