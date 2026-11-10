"""Thumbnail playbook: ideation before concepts, variants of chosen ideas, creator idea + images, official card
images, closing screen, critique + one revision, the one-box Creative Direction."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication  # noqa: E402

APP = QApplication.instance() or QApplication([])  # before any pipeline creates a QGuiApplication

from analysis.packaging import fast  # noqa: E402
from analysis.packaging.card_art import HearthstoneJsonProvider  # noqa: E402
from analysis.packaging.direction import CreativeDirection  # noqa: E402
from analysis.packaging.frames import read_image, write_image  # noqa: E402
from analysis.packaging.playbook import check_critique, check_ideation, critique_prompt, ideation_lines  # noqa: E402
from analysis.packaging.portfolio import evaluate_portfolio  # noqa: E402
from test_youtube_packaging_gen import make  # noqa: E402
from youtube_packaging_fakes import FakeAgentGen, FakeGenerator  # noqa: E402


def ideation(chosen=("I1", "I2"), n=8, video_type="gameplay_highlight") -> dict[str, Any]:
    return {"video_type": video_type, "promise": "p", "audience": "a", "emotion": "e",
            "candidates": [{"idea_id": f"I{k}", "family": "warning", "hook": "h", "visual": "v", "why": "w"}
                           for k in range(1, n + 1)],
            "chosen": [{"idea_id": c, "why_it_wins": "w"} for c in chosen], "rejected_note": "r"}


def test_ideation_lines_carry_the_playbook_and_the_creator_brief():
    lines = "\n".join(ideation_lines(n_concepts=4, brief="NON CRAFTARLA su Lama dell'Impero Nero",
                                     brief_images=["lama.png"]))
    assert "IDEATION" in lines and "8-12 RAW hook candidates" in lines and "2 per idea" in lines
    assert "NON CRAFTARLA su Lama" in lines and "creator image U1: lama.png" in lines and "concept C1" in lines
    assert "arrows pointing at the key card" in lines and "never an abstract phrase" in lines


def test_check_ideation_requires_candidates_two_chosen_ideas_and_variants():
    concepts = [{"idea_id": "I1"}, {"idea_id": "I1"}, {"idea_id": "I2"}, {"idea_id": "I2"}]
    assert check_ideation({"ideation": ideation(), "concepts": concepts}, n_concepts=4) == []
    assert any("missing" in p for p in check_ideation({"concepts": concepts}, n_concepts=4))
    assert any("8-12" in p for p in check_ideation({"ideation": ideation(n=3), "concepts": concepts}, n_concepts=4))
    assert any("exactly 2" in p for p in check_ideation({"ideation": ideation(chosen=("I1",)), "concepts": concepts},
                                                        n_concepts=4))
    stray = concepts[:3] + [{"idea_id": "I7"}]
    assert any("not a chosen idea" in p for p in check_ideation({"ideation": ideation(), "concepts": stray},
                                                                n_concepts=4))
    unused = [{"idea_id": "I1"}] * 4
    assert any("have no concept" in p for p in check_ideation({"ideation": ideation(), "concepts": unused},
                                                              n_concepts=4))


def test_variants_of_one_idea_may_share_their_message_but_two_ideas_may_not():
    def concept(cid: str, text: str, story: str = "warning", primary: str = "K01") -> dict[str, Any]:
        return {"concept_id": cid, "thumbnail_text": text, "story_type": story, "highlight_ids": ["H1"],
                "treatment": "card_hero", "primary_subject": primary, "scene": {}}
    concepts = [concept("C1", "NON CRAFTARLA"), concept("C2", "NON CRAFTARLA!"),
                concept("C3", "TRAPPOLA", "verdict", "W001"), concept("C4", "TRAPPOLA vera", "verdict", "W001")]
    kwargs = dict(available_story_types=4, available_treatments=4, available_opportunities=4)
    ideas = {"C1": "I1", "C2": "I1", "C3": "I2", "C4": "I2"}
    report = evaluate_portfolio(concepts, {}, ideas=ideas, **kwargs)
    assert not report.blocking and report.metrics["distinct_ideas"] == 2
    clash = evaluate_portfolio(concepts, {}, ideas={"C1": "I1", "C2": "I2", "C3": "I1", "C4": "I2"}, **kwargs)
    # two DIFFERENT ideas with the same text: reported for the critic, never a reason to throw the run away
    assert not clash.blocking and any(f.code == "same_message" for f in clash.findings)
    assert evaluate_portfolio(concepts, {}, **kwargs).blocking  # without ideas: the old diversity rules


def test_brief_and_its_images_work_in_automatic_mode_and_are_snapshotted(tmp_path):
    image = tmp_path / "lama.png"
    write_image(image, np.full((60, 40, 3), 120, np.uint8))
    direction = CreativeDirection(brief="NON CRAFTARLA", brief_images=[{"path": str(image)}],
                                  creative_focus="ignored in automatic", hook_locked=True, thumbnail_hook="x")
    active = direction.active()
    assert active.brief == "NON CRAFTARLA" and active.brief_images and not active.hook_locked
    assert direction.brief_text == "NON CRAFTARLA"
    guided = CreativeDirection(mode="guided", brief="idea", creative_focus="focus")
    assert guided.brief_text == "idea / focus"
    snap = direction.snapshot(tmp_path / "out")
    saved = snap.brief_images[0]
    assert (tmp_path / "out" / saved["path"]).is_file() and saved["name"] == "lama.png"
    with pytest.raises(ValueError):
        CreativeDirection.from_mapping({"brief_images": [{"path": "a"}] * 4})
    with pytest.raises(ValueError):
        CreativeDirection.from_mapping({"brief": "x" * 1501})


# ---- official card images ---------------------------------------------------------------------------------
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def fake_fetch(calls: list[str]):
    cards = {"enUS": [{"id": "BE_227", "name": "Blade of The Black Empire"}, {"id": "BE_725", "name": "The Unknowable One"},
                      {"id": "X1", "name": "Fireball"}, {"id": "X2", "name": "Fireblast"}],
             "itIT": [{"id": "BE_227", "name": "Lama dell'Impero Nero"}, {"id": "BE_725", "name": "L'Inconoscibile"},
                      {"id": "X1", "name": "Palla di Fuoco"}, {"id": "X2", "name": "Esplosione di Fuoco"}]}

    def fetch(url: str) -> bytes:
        calls.append(url)
        for locale, rows in cards.items():
            if f"/{locale}/cards" in url:
                return json.dumps(rows).encode("utf-8")
        return PNG
    return fetch


def test_provider_matches_any_locale_returns_the_audience_name_and_caches(tmp_path):
    calls: list[str] = []
    provider = HearthstoneJsonProvider(tmp_path, language="it", fetch=fake_fetch(calls))
    path, provenance = provider.lookup("The Unknowable One")
    assert provenance["name"] == "L'Inconoscibile" and provenance["locale"] == "itIT"
    assert Path(path).read_bytes() == PNG and "Blizzard" in provenance["licence"]
    assert provider.lookup("lama del impero nero")[1]["card_id"] == "BE_227"  # small spelling differences
    assert provider.lookup("Fireblas") is None or provider.lookup("Fireblas")[1]["card_id"] == "X2"
    assert provider.lookup("Una carta che non esiste") is None
    before = len(calls)
    again = HearthstoneJsonProvider(tmp_path, language="it", fetch=fake_fetch(calls))
    assert again.lookup("The Unknowable One") is not None and len(calls) == before  # all from disk


def test_provider_offline_means_no_official_image(tmp_path):
    def offline(url: str) -> bytes:
        raise OSError("offline")
    provider = HearthstoneJsonProvider(tmp_path, fetch=offline)
    assert provider.lookup("Fireball") is None and "unavailable" in provider.errors[0]


class FakeProvider:
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.errors: list[str] = []
        self.asked: list[str] = []

    def lookup(self, name: str):
        self.asked.append(name)
        if name.lower() not in ("minion", "lama dell'impero nero"):
            return None
        path = self.folder / f"{name[:4]}.png"
        rgba = np.zeros((120, 80, 4), np.uint8)
        rgba[10:110, 10:70] = (40, 120, 200, 255)
        write_image(path, rgba)
        return str(path), {"source": "fake", "card_id": "BE_227" if "lama" in name.lower() else "M1",
                           "locale": "itIT", "name": "Lama dell'Impero Nero" if "lama" in name.lower() else "Servitore",
                           "matched": name, "match_ratio": 1.0, "licence": "test"}


def run_pipeline(tmp_path: Path, *, agent: Any = None, generator: Any = None, provider: Any = None,
                 **settings: Any) -> tuple[dict[str, Any], Path]:
    pipeline = make(tmp_path, agent=agent or FakeAgentGen(), generator=generator or FakeGenerator(), **settings)
    if provider is not None:
        pipeline._card_provider = provider
    return pipeline.run(), tmp_path / "out"


def test_official_card_replaces_the_crop_and_brings_its_audience_name(tmp_path):
    provider = FakeProvider(tmp_path)
    report, out = run_pipeline(tmp_path, provider=provider, critique=False)
    manifest = json.loads((out / "visual_assets.json").read_text(encoding="utf-8"))["assets"]
    official = [a for a in manifest if a["type"] == "card_artwork"]
    assert official and official[0]["provenance"]["method"] == "official_card_image" and \
        official[0]["provenance"]["name"] == "Servitore"
    request = json.loads((out / "generation_request.json").read_text(encoding="utf-8"))
    card_led = next(p for p in request["packages"] if p["role"] == "card-led")
    assert any(i["kind"] == "card_official" and i["asset_id"] == official[0]["id"] for i in card_led["images"])
    assert card_led["card_name"] == "Servitore" and "official card image" in request["prompt"]
    assert (out / "official_cards.json").is_file()


def test_card_name_from_the_brief_is_honoured_and_an_unjustified_one_is_not(tmp_path):
    agent = FakeAgentGen()
    agent.card_name = "Lama dell'Impero Nero"
    direction = {"brief": "NON CRAFTARLA su Lama dell'Impero Nero"}
    report, out = run_pipeline(tmp_path, agent=agent, provider=FakeProvider(tmp_path), critique=False,
                               creative_direction=direction)
    request = json.loads((out / "generation_request.json").read_text(encoding="utf-8"))
    c1 = next(p for p in request["packages"] if p["concept_id"] == "C1")
    assert c1["card_name"] == "Lama dell'Impero Nero" and any(i["kind"] == "card_official" for i in c1["images"])
    assert "NON CRAFTARLA su Lama" in agent.calls[0]["prompt"]
    agent2 = FakeAgentGen()
    agent2.card_name = "Lama dell'Impero Nero"  # not read, not said, not asked
    report2, _ = run_pipeline(tmp_path / "b", agent=agent2, provider=FakeProvider(tmp_path), critique=False)
    assert any("is not read, said or asked for" in w for w in report2["warnings"])


def test_creator_images_reach_the_planner_first_and_the_cover_that_asks_for_them(tmp_path):
    image = tmp_path / "carta.png"
    write_image(image, np.full((300, 200, 3), 90, np.uint8))
    agent = FakeAgentGen()
    agent.creator_images = ["U1"]
    report, out = run_pipeline(tmp_path, agent=agent, critique=False,
                               creative_direction={"brief": "questa carta", "brief_images": [{"path": str(image)}]})
    prompt = agent.calls[0]["prompt"]
    assert "- image 1: creator image U1 (carta.png)" in prompt
    request = json.loads((out / "generation_request.json").read_text(encoding="utf-8"))
    c1 = next(p for p in request["packages"] if p["concept_id"] == "C1")
    assert any(i["kind"] == "creator" and i["asset_id"] == "U1" for i in c1["images"])
    assert "creator image (the creator's own input for this cover)" in request["prompt"]
    bad = FakeAgentGen()
    bad.creator_images = ["U3"]
    with pytest.raises(Exception):
        run_pipeline(tmp_path / "bad", agent=bad, critique=False,
                     creative_direction={"brief": "x", "brief_images": [{"path": str(image)}]})


def test_closing_screen_is_an_asset_and_a_planning_image(tmp_path):
    agent = FakeAgentGen()
    report, out = run_pipeline(tmp_path, agent=agent, critique=False)
    manifest = json.loads((out / "visual_assets.json").read_text(encoding="utf-8"))["assets"]
    closing = [a for a in manifest if a["provenance"].get("role") == "closing"]
    assert len(closing) == 1 and closing[0]["edited_time"] > 0.9 * 300
    assert "CLOSING SCREEN" in agent.calls[0]["prompt"] and "closing screen F" in agent.calls[0]["prompt"]


def test_ideation_is_saved_for_the_creator(tmp_path):
    report, out = run_pipeline(tmp_path, critique=False)
    saved = json.loads((out / "thumbnail_ideation.json").read_text(encoding="utf-8"))
    assert len(saved["candidates"]) == 8 and [c["idea_id"] for c in saved["chosen"]] == ["I1", "I2"]
    concepts = json.loads((out / "thumbnail_concepts.json").read_text(encoding="utf-8"))
    assert concepts["ideation"]["promise"] and {c["idea_id"] for c in concepts["concepts"]} == {"I1", "I2"}


# ---- critique + revision -----------------------------------------------------------------------------------
def test_critique_ranks_the_covers_and_the_revision_becomes_the_preferred_cover(tmp_path):
    agent, generator = FakeAgentGen(), FakeGenerator()
    report, out = run_pipeline(tmp_path, agent=agent, generator=generator)
    assert [c["task"] for c in agent.calls] == ["packaging_fast_playbook", "thumbnail_critique_v1"]
    critique_call = agent.calls[1]
    assert critique_call["images"] == 5 and "phone" in critique_call["prompt"].lower()  # 4 covers + phone strip
    assert len(generator.calls) == 2 and generator.calls[1]["concept_ids"] == ["C1R"]
    revision = generator.calls[1]
    assert "REVISION: image 1" in revision["prompt"] and "testo più grande" in revision["prompt"]
    assert Path(revision["images"][0]).name == "C1.png"  # the draft first
    concepts = json.loads((out / "thumbnail_concepts.json").read_text(encoding="utf-8"))
    assert concepts["preferred_concept_id"] == "C1R"
    revised = next(c for c in concepts["concepts"] if c["concept_id"] == "C1R")
    assert revised["revision_of"] == "C1" and revised["render"]["engine"] == "generated"
    assert read_image(out / revised["render"]["path"]).shape[:2] == (1080, 1920)
    critique = json.loads((out / "thumbnail_critique.json").read_text(encoding="utf-8"))
    assert critique["ranking"][0] == "C1" and report["generation"]["critique"]["revision"]["status"] == "done"


def test_a_failed_critique_keeps_the_generated_covers(tmp_path):
    class Picky(FakeAgentGen):
        @staticmethod
        def critique(prompt: str) -> dict[str, Any]:
            return {"reviews": [], "ranking": [], "revise": {"concept_id": "C9", "instructions": [], "hook": ""},
                    "summary": ""}
    report, out = run_pipeline(tmp_path, agent=Picky())
    ids = [t["concept_id"] for t in report["thumbnails"]]
    assert ids == ["C1", "C2", "C3", "C4"] and any("critique failed" in w for w in report["warnings"])


def test_check_critique_and_prompt():
    ids = ["C1", "C2"]
    good = {"reviews": [{"concept_id": i, "scores": {k: 5 for k in ("idea", "text", "hierarchy", "object", "face",
                                                                     "context", "finish", "title_fit")},
                         "issues": [], "keep": []} for i in ids], "ranking": ["C2", "C1"], "summary": "s",
            "revise": {"concept_id": "C2", "instructions": ["bigger card"], "hook": "TRAPPOLA"}}
    assert check_critique(good, ids) is good
    bad = json.loads(json.dumps(good))
    bad["reviews"][0]["scores"]["idea"] = 12
    with pytest.raises(ValueError):
        check_critique(bad, ids)
    prompt = critique_prompt(concepts=[{"concept_id": "C1", "thumbnail_text": "NON CRAFTARLA"}], language="it",
                             ideation={"promise": "safe craft", "emotion": "warning"}, brief="la mia idea")
    assert "168x94" in prompt and "safe craft" in prompt and "la mia idea" in prompt


def test_validate_playbook_rejects_unknown_creator_images():
    raw = {"ideation": ideation(), "concepts": [{"concept_id": "C1", "idea_id": "I1", "creator_images": ["U2"]}]}
    for key in fast.RESPONSE_SCHEMA_PLAYBOOK["required"]:
        raw.setdefault(key, [] if key != "ideation" else raw["ideation"])
    with pytest.raises(ValueError, match="creator_images"):
        fast.validate_playbook(raw, n_brief_images=1, n_concepts=4, assets={})


# ---- GUI + CLI ---------------------------------------------------------------------------------------------
def test_creative_direction_widget_idea_box_images_and_more_options(tmp_path):
    from ui.thumbnail_direction import CreativeDirectionWidget
    widget = CreativeDirectionWidget()
    assert widget.idea.isVisibleTo(widget) and not widget.title.isVisibleTo(widget)  # advanced folded away
    widget.idea.setPlainText("NON CRAFTARLA su questa carta")
    images = []
    for n in range(4):
        path = tmp_path / f"c{n}.png"
        write_image(path, np.full((20, 20, 3), 50 * n, np.uint8))
        images.append(str(path))
    widget.add_images(images)
    value = widget.value()
    assert value["brief"] == "NON CRAFTARLA su questa carta" and len(value["brief_images"]) == 3
    assert value["mode"] == "automatic" and not widget.add_image.isEnabled()
    widget.remove_image(images[0])
    assert len(widget.value()["brief_images"]) == 2 and widget.add_image.isEnabled()
    assert widget.run_options() == {"critique": True, "official_cards": True}
    widget.more.setChecked(True)
    assert widget.title.isVisibleTo(widget) and widget.critique.isVisibleTo(widget)
    other = CreativeDirectionWidget()
    other.restore(widget.value())
    assert other.idea.toPlainText() == "NON CRAFTARLA su questa carta" and len(other.value()["brief_images"]) == 2


def test_cli_idea_options(tmp_path):
    from automation.cli import build_parser
    from automation.packaging_cli import idea_direction
    image = tmp_path / "card.png"
    write_image(image, np.zeros((10, 10, 3), np.uint8))
    args = build_parser().parse_args(["packaging", "--video", "v.mp4", "--idea", "NON CRAFTARLA", "--idea-image",
                                      str(image), "--no-critique"])
    assert args.idea == "NON CRAFTARLA" and args.idea_image == [str(image)] and args.no_critique
    assert not args.no_official_cards
    direction = idea_direction(args.idea, args.idea_image)
    assert direction["brief"] == "NON CRAFTARLA" and direction["brief_images"][0]["path"] == str(image.resolve())
    assert idea_direction(None, []) == {}
    from analysis.packaging.models import PackagingError
    with pytest.raises(PackagingError):
        idea_direction("x", [str(tmp_path / "missing.png")])


def test_tier_list_ideas_need_one_specific_card():
    concepts = [{"idea_id": "I1"}, {"idea_id": "I1"}, {"idea_id": "I2"}, {"idea_id": "I2"}]
    tier = ideation(video_type="tier_list_or_guide")
    problems = check_ideation({"ideation": tier, "concepts": concepts}, n_concepts=4)
    assert any("ONE specific card" in p for p in problems)
    assert check_ideation({"ideation": tier, "concepts": concepts}, n_concepts=4, creator_brief=True) == []
    with_image = [dict(c) for c in concepts]
    with_image[0]["creator_images"] = ["U1"]  # the creator's card image counts as the specific card
    assert check_ideation({"ideation": tier, "concepts": with_image}, n_concepts=4) == []
    concepts[0]["card_name"] = "Lama dell'Impero Nero"
    assert check_ideation({"ideation": tier, "concepts": concepts}, n_concepts=4) == []
    lines = "\n".join(ideation_lines(n_concepts=4))
    assert "Be BOLD" in lines and "no 'ma', 'tutto'" in lines
