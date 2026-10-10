"""AI YouTube Packaging: units (mapping, transcript, highlights, frames, webcam, cards, style, agents, validation)."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from analysis.boundaries.agents import AgentCallError
from analysis.boundaries.transcript import TranscriptSettings
from analysis.packaging import creative, story
from analysis.packaging.agent import AgentSettings, PackagingAgent, codex_tokens
from analysis.packaging.audio import analyze_kept_audio
from analysis.packaging.cache import PackagingCache
from analysis.packaging.cards import extract_cards, transcript_mention
from analysis.packaging.frames import FrameExtractor, contact_sheet, read_image
from analysis.packaging.games import add_uncovered_segments, games_from_project
from analysis.packaging.highlights import build_candidates, collect_signals, reaction_strength
from analysis.packaging.models import GameContext, PackagingError, TranscriptLine
from analysis.packaging.render import RenderPlan, ThumbnailRenderer, fit_box, fit_text, ensure_qt
from analysis.packaging.style import CreatorStyleStore, analyze_titles, prompt_summary
from analysis.packaging.timeline import EditMap, KeepPiece
from analysis.packaging.transcript import build_transcript, plan_chunks
from analysis.packaging.visual import VisualProfile
from analysis.packaging.webcam import detect_webcam_roi, parse_roi, webcam_quality

from youtube_packaging_fakes import FakeEngine, FakeSampler, fake_rms, planner, synthetic_frame, write_project


def edit_map(pieces=((10.0, 20.0), (30.0, 50.0), (200.0, 260.0))) -> EditMap:
    out, cursor = [], 0.0
    for a, b in pieces:
        out.append(KeepPiece(0, a, b, cursor))
        cursor += b - a
    return EditMap(["video.mp4"], out, origin="project", identities=[{"path": "video.mp4", "size": 1}])


# ---------- timestamp mapping ----------

def test_source_edited_mapping_round_trip_and_cut_instants():
    m = edit_map()
    assert m.duration == pytest.approx(90.0)
    assert m.to_edited(15.0) == pytest.approx(5.0)
    assert m.to_edited(35.0) == pytest.approx(15.0)
    assert m.to_edited(25.0) is None  # removed content has no edited time
    assert m.to_edited_nearest(25.0) in (pytest.approx(10.0),)
    for t in (0.0, 4.2, 10.0, 29.9, 30.0, 89.9):
        index, source = m.to_source(t)
        assert m.to_edited(source, index) == pytest.approx(t)
    assert m.edited_range(0, 15.0, 40.0) == (pytest.approx(5.0), pytest.approx(20.0))
    assert m.kept_seconds(0, 0.0, 100.0) == pytest.approx(30.0)
    assert m.spans(0, merge_gap=20.0) == [(10.0, 50.0), (200.0, 260.0)]


def test_project_mapping_uses_the_exporter_plan(tmp_path):
    media = tmp_path / "video.mp4"
    media.write_bytes(b"x" * 1000)
    project = write_project(tmp_path, media)
    m = EditMap.from_project(project, planner=planner())
    assert [(p.source_start, p.source_end) for p in m.pieces][:2] == [(20.0, 60.0), (62.0, 150.0)]
    assert m.duration == pytest.approx(40 + 88 + 49 + 70 + 88)
    assert m.to_edited(400.0) == pytest.approx(177.0)


def test_range_digest_only_changes_for_the_modified_range():
    a = edit_map(((10.0, 20.0), (200.0, 260.0)))
    b = edit_map(((10.0, 20.0), (200.0, 250.0)))
    assert a.range_digest(0, 0.0, 100.0) == b.range_digest(0, 0.0, 100.0)
    assert a.range_digest(0, 150.0, 300.0) != b.range_digest(0, 150.0, 300.0)


# ---------- transcript ----------

def test_transcript_contains_only_kept_words_and_splits_at_cuts(tmp_path):
    m = edit_map(((0.0, 51.15), (52.3, 120.0)))  # cuts through the sentence spoken at 50-54 s
    engine = FakeEngine()
    status, lines = build_transcript(m, TranscriptSettings(language="it"), cache=PackagingCache(tmp_path),
                                     engine=engine)
    assert status["status"] == "ok" and lines
    for line in lines:
        for w in line.words:
            middle = (w["source_start"] + w["source_end"]) / 2
            assert m.to_edited(middle) is not None  # nothing from removed content
            assert w["edited_start"] == pytest.approx(m.to_edited(w["source_start"]), abs=1e-3)
    around_cut = [line for line in lines if 50.0 <= line.source_start < 53.0]
    assert [line.text for line in around_cut] == ["allora qui", "e poi attacco"]  # split at the cut
    assert all(a.edited_start <= b.edited_start for a, b in zip(lines, lines[1:]))


def test_transcript_cache_hit_and_invalidation(tmp_path):
    cache = PackagingCache(tmp_path)
    engine = FakeEngine()
    m = edit_map(((0.0, 100.0),))
    build_transcript(m, TranscriptSettings(), cache=cache, engine=engine)
    build_transcript(m, TranscriptSettings(), cache=cache, engine=engine)
    assert engine.calls == 1 and cache.stats["transcript"]["hit"] == 1
    build_transcript(edit_map(((0.0, 90.0),)), TranscriptSettings(), cache=cache, engine=engine)
    assert engine.calls == 2  # new keeps: dependent transcript recomputed


def test_asr_chunks_are_grid_aligned_so_unchanged_regions_keep_their_windows():
    long_a = plan_chunks(edit_map(((0.0, 500.0), (1000.0, 1300.0))))
    long_b = plan_chunks(edit_map(((0.0, 500.0), (1000.0, 1250.0))))
    windows_a = {(c.window.start, c.window.end) for c in long_a}
    windows_b = {(c.window.start, c.window.end) for c in long_b}
    assert {w for w in windows_a if w[1] <= 500.0} == {w for w in windows_b if w[1] <= 500.0}


def test_missing_asr_is_reported_never_invented(tmp_path):
    status, lines = build_transcript(edit_map(), TranscriptSettings(), cache=PackagingCache(tmp_path),
                                     engine=FakeEngine(status="unavailable"))
    assert status["status"] == "unavailable" and lines == []


# ---------- highlights ----------

def _lines(m: EditMap, tmp_path: Path):
    return build_transcript(m, TranscriptSettings(), cache=PackagingCache(tmp_path / "t"), engine=FakeEngine())[1]


def test_highlights_from_multiple_signals_and_game_coverage(tmp_path):
    m = edit_map(((0.0, 120.0), (150.0, 330.0)))
    lines = _lines(m, tmp_path)
    audio = analyze_kept_audio(m, cache=PackagingCache(tmp_path / "a"), extractor=fake_rms)
    games = [GameContext("g-1", 1, "WIN", 0, 0.0, 120.0, 0.0, 120.0, 120.0, "project_gameplay",
                         markers={"result_screen": {"source": 118.0, "edited": 118.0}}),
             GameContext("g-2", 2, "WIN", 0, 150.0, 330.0, 120.0, 300.0, 180.0, "project_gameplay")]
    signals = collect_signals(m, lines, audio, VisualProfile("unavailable"), games)
    kinds = {s.kind for s in signals}
    assert {"audio_peak", "reaction", "near_result"} <= kinds
    highlights = build_candidates(m, signals, lines, audio, VisualProfile("unavailable"), games, max_candidates=6)
    assert all(h.game_id in ("g-1", "g-2") for h in highlights)
    assert {h.game_id for h in highlights} == {"g-1", "g-2"}  # every kept game is covered
    reaction = next(h for h in highlights if h.edited_start <= 42.0 <= h.edited_end)
    assert "audio_peak" in reaction.evidence_sources and "reaction" in reaction.evidence_sources
    assert reaction.transcript_ids and "incredibile" in reaction.transcript_context


def test_a_game_without_audio_peaks_still_gets_a_coverage_candidate(tmp_path):
    m = edit_map(((0.0, 120.0), (500.0, 560.0)))
    games = [GameContext("quiet", 1, "WIN", 0, 500.0, 560.0, 120.0, 180.0, 60.0, "project_gameplay")]
    highlights = build_candidates(m, [], [], analyze_kept_audio(m, cache=PackagingCache(tmp_path),
                                                                extractor=lambda r, **k: np.full(10000, 0.01)),
                                  VisualProfile("unavailable"), games)
    assert len(highlights) == 1 and highlights[0].coverage and highlights[0].game_id == "quiet"


def test_reaction_strength_prefers_short_outbursts():
    strong, words = reaction_strength("No no no, incredibile!")
    weak, _ = reaction_strength("allora adesso gioco questa carta e poi vediamo cosa succede nel prossimo turno")
    assert strong > 0.5 > weak and "incredibile" in words


# ---------- games ----------

def test_games_from_project_keep_only_kept_games(tmp_path):
    media = tmp_path / "video.mp4"
    media.write_bytes(b"x" * 1000)
    project = write_project(tmp_path, media)
    m = EditMap.from_project(project, planner=planner())
    payload = json.loads(project.read_text(encoding="utf-8"))
    games = add_uncovered_segments(games_from_project(payload, m), m)
    assert [(g.game_id, g.result) for g in games] == [("g-1", "WIN"), ("g-3", "WIN")]  # LOSS was removed
    assert games[1].edited_start == pytest.approx(177.0)
    assert games[0].markers["vs_screen"]["edited"] == pytest.approx(0.0)


def test_whole_video_context_when_no_games():
    m = edit_map(((0.0, 300.0),))
    games = add_uncovered_segments([], m)
    assert [(g.game_id, g.origin) for g in games] == [("video", "whole_video")]


# ---------- frames ----------

def test_frame_bursts_are_cached_and_contact_sheet_written(tmp_path):
    m = edit_map(((0.0, 100.0),))
    sampler = FakeSampler()
    frames = FrameExtractor(m, tmp_path / "out", PackagingCache(tmp_path / "c"), sampler=sampler)
    first = frames.burst(0, 10.0, 12.0, fps=4.0)
    second = frames.burst(0, 10.0, 12.0, fps=4.0)
    assert sampler.calls == 1 and len(first) == len(second) == 8
    record = frames.grab(0, 11.0, highlight_id="H01", role="peak")
    assert record is not None and (tmp_path / "out" / record.path).is_file()
    assert record.edited_time == pytest.approx(record.source_time)
    sheet = contact_sheet([record], frames.images, tmp_path / "out" / "sheet.jpg")
    assert read_image(sheet) is not None


def test_frames_in_removed_content_are_not_chosen(tmp_path):
    m = edit_map(((0.0, 10.0), (20.0, 30.0)))
    frames = FrameExtractor(m, tmp_path, PackagingCache(tmp_path / "c"), sampler=FakeSampler())
    found = frames.best_frame(0, 10.5, radius=1.0)
    assert found is not None and m.to_edited(found[0]) is not None


# ---------- webcam ----------

@pytest.mark.parametrize("value,status", [
    ("0.0,0.55,0.25,0.45", "ok"), (None, "absent"), ("none", "absent"), ("0.9,0.9,0.3,0.3", "clamped"),
    ("1.5,1.5,0.2,0.2", "invalid"), ("a,b,c,d", "invalid"), ("0.1,0.1,0,0.2", "invalid"), ("0.1,0.2", "invalid"),
])
def test_webcam_roi_validation(value, status):
    check = parse_roi(value)
    assert check.status == status
    assert (check.rect is not None) == (status in ("ok", "clamped"))


def test_webcam_crop_quality_and_absent_detection():
    image = synthetic_frame(3.0)
    crop = _crop("0.0,0.55,0.25,0.45", image)
    quality = webcam_quality(crop, crop)
    assert set(quality) >= {"sharpness", "face", "activity_proxy", "score"} and quality["face"] == 0.0
    check, evidence = detect_webcam_roi([synthetic_frame(t) for t in range(4)])
    assert check.status == "absent" and evidence["faces"] == 0


# ---------- rendering ----------

def _crop(value: str, image: np.ndarray) -> np.ndarray:
    rect = parse_roi(value).rect
    assert rect is not None
    return rect.crop(image)


def _plan(**kw):
    base: dict[str, Any] = dict(layout="split_reaction", headline="Lettura perfetta", background=synthetic_frame(1.0),
                webcam=_crop("0.0,0.55,0.25,0.45", synthetic_frame(1.0)))
    base.update(kw)
    return RenderPlan(**base)


@pytest.mark.parametrize("layout", ["split_reaction", "board_focus", "card_spotlight", "cinematic_text"])
def test_every_layout_renders_a_16_9_png_with_fitting_text(tmp_path, layout):
    renderer = ThumbnailRenderer()
    plan = _plan(layout=layout, cards=[synthetic_frame(2.0)[50:250, 100:240]], badge="WIN", emphasis="perfetta")
    result = renderer.render(plan, tmp_path / f"{layout}.png")
    image = read_image(Path(result.path))
    assert image is not None and image.shape[:2] == (720, 1280) and result.width * 9 == result.height * 16
    assert result.text["fits"] and result.text["timestamp_zone_clear"], result.warnings
    x, y, w, h = result.text["bounds"]
    assert x >= 0 and y >= 0 and x + w <= 1280 and y + h <= 720


def test_long_headlines_shrink_but_never_overflow(tmp_path):
    result = ThumbnailRenderer().render(_plan(headline="Questa partita assurda incredibile davvero"),
                                        tmp_path / "long.png")
    x, y, w, h = result.text["bounds"]
    assert x + w <= 1280 and y + h <= 720 and len(result.text["lines"]) <= 3
    ensure_qt()
    fitted = fit_text("UNA PAROLA LUNGHISSIMAAAAAAAAAAAAAAAAAAAA", (), 200, 100, min_px=20)
    assert not fitted.fits  # impossible text is reported, not silently clipped


def test_webcam_and_board_are_composited_and_the_stream_overlay_masked(tmp_path):
    background = synthetic_frame(1.0)
    webcam = np.zeros((160, 160, 3), np.uint8)
    webcam[:] = (0, 0, 255)  # pure red webcam crop
    plan = _plan(background=background, webcam=webcam, mask_boxes=[[0.0, 0.55, 0.25, 0.45]], focus_box=None)
    result = ThumbnailRenderer().render(plan, tmp_path / "split.png")
    image = read_image(Path(result.path))
    assert image is not None
    panel = image[300:450, 900:1100].reshape(-1, 3).mean(axis=0)
    assert panel[2] > 180 and panel[0] < 80  # the webcam panel is visible on the right
    fallback = ThumbnailRenderer().render(_plan(webcam=None), tmp_path / "nowebcam.png")
    assert "rendered as story_focus" in " ".join(fallback.warnings) and fallback.layout == "story_focus"


def test_renderer_rejects_non_16_9_and_writes_jpeg(tmp_path):
    with pytest.raises(ValueError):
        ThumbnailRenderer(size=(1000, 700))
    result = ThumbnailRenderer().render(_plan(), tmp_path / "x.jpg", fmt="jpg")
    assert result.format == "jpg" and result.bytes < 2 * 1024 * 1024


def test_fit_box_expands_to_aspect_inside_the_frame():
    x, y, w, h = fit_box([0.9, 0.9, 0.1, 0.1], 1920, 1080, 16 / 9)
    assert x + w <= 1920 and y + h <= 1080 and abs(w / h - 16 / 9) < 0.01


# ---------- cards ----------

def test_card_status_verified_legible_and_uncertain(tmp_path):
    m = edit_map(((0.0, 100.0),))
    frames = FrameExtractor(m, tmp_path, PackagingCache(tmp_path / "c"), sampler=FakeSampler())
    record = frames.grab(0, 40.0)
    assert record is not None
    lines = [TranscriptLine("T0001", 39.0, 41.0, 39.0, 41.0, "adesso gioco il dottor boom", 0.9)]
    observations: list[dict[str, Any]] = [
        {"frame_id": record.id, "box": [0.1, 0.2, 0.2, 0.3], "name": "Dottor Boom", "legibility": "clear",
         "confidence": 0.9, "transcript_ids": ["T0001"]},
        {"frame_id": record.id, "box": [0.4, 0.2, 0.2, 0.3], "name": "Leeroy", "legibility": "clear",
         "confidence": 0.8, "transcript_ids": []},
        {"frame_id": record.id, "box": [0.6, 0.2, 0.2, 0.3], "name": "Forse Drago", "legibility": "partial",
         "confidence": 0.3, "transcript_ids": []},
        {"frame_id": record.id, "box": [0.6, 0.5, 0.2, 0.3], "name": None, "legibility": "unreadable",
         "confidence": 0.1, "transcript_ids": []},
    ]
    cards = extract_cards(observations, frames, lines)
    assert [c.status for c in cards] == ["verified", "legible", "uncertain", "uncertain"]
    assert cards[3].name is None and cards[2].alternatives_tried > 0  # neighbour frames were tried
    assert all((tmp_path / c.path).is_file() and c.provenance["type"] == "video_crop" for c in cards)


def test_transcript_mention_is_fuzzy_but_not_loose():
    assert transcript_mention("Dottor Boom", ["e poi gioco dottor bum adesso"])
    assert transcript_mention("Dottor Boom", ["e poi attacco con il drago"]) is None


# ---------- creator style memory ----------

def test_style_profile_versioning_import_feedback_and_disable(tmp_path):
    store = CreatorStyleStore(tmp_path)
    store.create("Lugozzi", channel="Lugozzi HS")
    with pytest.raises(FileExistsError):
        store.create("Lugozzi")
    thumb = tmp_path / "thumb.png"
    from analysis.packaging.frames import write_image
    write_image(thumb, synthetic_frame(1.0))
    profile = store.import_examples("Lugozzi", titles=["Ho vinto con il mazzo più strano?", "TOP 1 LEGEND!"],
                                    thumbnails=[thumb])
    assert profile["revision"] == 2 and profile["title_style"]["observed"]["examples"] == 2
    assert profile["thumbnail_style"]["observed"]["examples"] == 1
    assert (tmp_path / "lugozzi" / "history" / "profile.r0001.json").is_file()
    assert thumb.is_file()  # the original example is only copied
    profile = store.record_feedback("Lugozzi", [{"action": "approve", "kind": "title", "value": "Bel titolo"},
                                                {"action": "reject", "kind": "thumbnail", "value": "testo rosso"}])
    assert profile["revision"] == 3 and len(profile["approved"]) == 1 and len(profile["rejected"]) == 1
    lines = prompt_summary(store.load("Lugozzi"))
    assert any("APPROVED" in line for line in lines) and any("REJECTED" in line for line in lines)
    store.update("Lugozzi", enabled=False)
    assert prompt_summary(store.load("Lugozzi")) == []


def test_empty_profile_asserts_no_preferences(tmp_path):
    store = CreatorStyleStore(tmp_path)
    lines = prompt_summary(store.create("Nuovo"))
    assert "no evidence" in lines[-1]
    assert analyze_titles([]) == {}


# ---------- agent adapters (mocked CLIs) ----------

class FakePopen:
    calls: list[dict] = []
    answers: list[dict] = []

    def __init__(self, cmd, **kwargs):
        self.cmd, self.kwargs, self.returncode, self.pid = cmd, kwargs, 0, 1
        FakePopen.calls.append({"cmd": cmd, "cwd": kwargs.get("cwd")})

    def communicate(self, input=None, timeout=None):
        FakePopen.calls[-1]["input"] = input
        answer = FakePopen.answers.pop(0)
        if "-o" in self.cmd:
            Path(self.cmd[self.cmd.index("-o") + 1]).write_text(json.dumps(answer), encoding="utf-8")
            return "", "tokens used\n12,345\n"
        events = [{"type": "system"}, {"type": "result", "subtype": "success", "is_error": False,
                                       "structured_output": answer, "total_cost_usd": 0.02, "duration_ms": 900,
                                       "usage": {"input_tokens": 10, "output_tokens": 5}}]
        return "\n".join(json.dumps(e) for e in events), ""

    def kill(self):
        pass


@pytest.fixture
def fake_cli(tmp_path):
    FakePopen.calls, FakePopen.answers = [], []
    cli = tmp_path / "agent.exe"
    cli.write_text("")
    with patch("analysis.boundaries.agents.subprocess.Popen", FakePopen):
        yield cli


def _image(tmp_path):
    from analysis.packaging.frames import write_image
    path = tmp_path / "img.png"
    write_image(path, synthetic_frame(0.0))
    return path


def test_claude_adapter_sends_images_as_stream_json_with_tools_disabled(tmp_path, fake_cli):
    FakePopen.answers = [{"ok": 1}]
    agent = PackagingAgent(AgentSettings(provider="claude", executable=str(fake_cli)), PackagingCache(tmp_path / "c"))
    result = agent.run("t", prompt="hello", schema={"type": "object"}, system_prompt="sys", prompt_version="v1",
                       images=[_image(tmp_path)])
    cmd = FakePopen.calls[0]["cmd"]
    assert cmd[cmd.index("--input-format") + 1] == "stream-json" and cmd[cmd.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in cmd and "--no-session-persistence" in cmd
    message = json.loads(FakePopen.calls[0]["input"])
    blocks = message["message"]["content"]
    assert blocks[0]["type"] == "image" and base64.b64decode(blocks[0]["source"]["data"])[:4] == b"\x89PNG"
    assert blocks[-1] == {"type": "text", "text": "hello"}
    assert Path(FakePopen.calls[0]["cwd"]).name == "empty"
    assert result.answer == {"ok": 1} and result.meta["cost_usd"] == 0.02


def test_codex_adapter_attaches_images_and_reads_the_output_file(tmp_path, fake_cli):
    FakePopen.answers = [{"ok": 2}]
    agent = PackagingAgent(AgentSettings(provider="codex", executable=str(fake_cli), effort="low"),
                           PackagingCache(tmp_path / "c"))
    result = agent.run("t", prompt="hello", schema={"type": "object"}, system_prompt="sys", prompt_version="v1",
                       images=[_image(tmp_path)])
    cmd = FakePopen.calls[0]["cmd"]
    assert cmd[1] == "exec" and cmd[cmd.index("--sandbox") + 1] == "read-only" and cmd[-2:] == ["--", "-"]
    image_flags = [c for c in cmd if c.startswith("--image=")]
    assert len(image_flags) == 1 and Path(image_flags[0].split("=", 1)[1]).parent.name == "inputs"
    assert FakePopen.calls[0]["input"].startswith("sys\n\nhello")
    assert result.answer == {"ok": 2} and result.meta["tokens_used"] == 12345


def test_agent_cache_hit_and_repair_attempt(tmp_path, fake_cli):
    FakePopen.answers = [{"n": -1}, {"n": 3}]

    def validate(raw):
        if raw["n"] < 0:
            raise ValueError("n must be positive")
        return raw

    agent = PackagingAgent(AgentSettings(provider="codex", executable=str(fake_cli)), PackagingCache(tmp_path / "c"))
    first = agent.run("t", prompt="p", schema={}, system_prompt="s", prompt_version="v", validate=validate)
    assert first.answer == {"n": 3} and first.meta["attempts"] == 2
    assert "REJECTED BY VALIDATION: n must be positive" in FakePopen.calls[1]["input"]
    again = agent.run("t", prompt="p", schema={}, system_prompt="s", prompt_version="v", validate=validate)
    assert again.meta["cache_hit"] and len(FakePopen.calls) == 2
    agent.run  # a different prompt version is a cache miss
    FakePopen.answers = [{"n": 4}]
    other = agent.run("t", prompt="p", schema={}, system_prompt="s", prompt_version="v2", validate=validate)
    assert other.answer == {"n": 4} and len(FakePopen.calls) == 3


def test_missing_cli_is_an_explanatory_error(tmp_path):
    agent = PackagingAgent(AgentSettings(provider="claude", executable=str(tmp_path / "nope.exe")),
                           PackagingCache(tmp_path))
    with patch("analysis.boundaries.agents.shutil.which", return_value=None):
        with pytest.raises(AgentCallError) as error:
            agent.run("t", prompt="p", schema={}, system_prompt="s", prompt_version="v")
    assert error.value.code == "cli_missing" and "AUTO_CUTTER_CLAUDE_CLI" in str(error.value)


def test_codex_token_parsing():
    assert codex_tokens("x\ntokens used\n1,234\n") == 1234
    assert codex_tokens("tokens used: 99") == 99
    assert codex_tokens("") is None


# ---------- structured JSON validation ----------

def _valid_story():
    return {"language": "it", "main_topic": "x", "summary": "s", "themes": [], "why_interesting": "w",
            "games": [{"game_id": "g", "summary": "s", "specific_plays": [], "general_talk": [], "conclusion": "c"}],
            "strongest_highlights": [{"highlight_id": "H01", "title": "t", "why": "w", "transcript_ids": ["T0001"],
                                      "frame_ids": ["F001"], "strength": 0.7}],
            "narrative_angles": [{"angle_id": "A", "angle": "a", "highlight_ids": ["H01"]},
                                 {"angle_id": "B", "angle": "b", "highlight_ids": ["H01"]}],
            "card_observations": [], "risky_claims": [], "deepen_requests": []}


def _validate_story(raw, **kw):
    args: dict[str, Any] = dict(highlight_ids={"H01"}, frame_ids={"F001"}, transcript_ids={"T0001"}, game_ids={"g"}, max_deepen=1,
                edited_duration=100.0)
    args.update(kw)
    return story.validate_story(raw, **args)


def test_story_validation_accepts_valid_and_rejects_invented_ids():
    assert _validate_story(_valid_story())["main_topic"] == "x"
    bad = _valid_story()
    bad["strongest_highlights"][0]["highlight_id"] = "H99"
    with pytest.raises(ValueError, match="unknown highlight"):
        _validate_story(bad)
    bad = _valid_story()
    bad["deepen_requests"] = [{"edited_start": 10, "edited_end": 50, "want": "frames", "reason": "r"}]
    with pytest.raises(ValueError, match="at most 20 s"):
        _validate_story(bad)
    bad = _valid_story()
    bad["card_observations"] = [{"frame_id": "F001", "box": [0.9, 0.9, 0.5, 0.5], "name": None,
                                 "legibility": "unreadable", "confidence": 0.1, "transcript_ids": []}]
    with pytest.raises(ValueError, match="invalid box"):
        _validate_story(bad)


def _creative_answer():
    concept = {"concept_id": "C1", "narrative_angle": "a", "direction": "competitive", "title": "Una vittoria pulita",
               "thumbnail_text": "Che lettura", "highlight_ids": ["H01"], "source_frame_ids": ["F001"],
               "webcam_frame_id": "W001", "card_asset_ids": [],
               "composition_plan": {"layout": "split_reaction", "background_frame_id": "F001", "focus_box": None,
                                    "inset_id": None, "inset_box": None, "text_side": "left", "webcam_side": "right",
                                    "emphasis_word": "lettura", "badge": "WIN", "accent_color": "#FFD400",
                                    "text_color": "#FFFFFF", "rationale": ""},
               "evidence": [{"claim": "c", "highlight_id": "H01", "transcript_ids": ["T0001"], "frame_ids": []}],
               "risk_notes": ""}
    concepts = []
    for i, layout in enumerate(("split_reaction", "board_focus", "cinematic_text")):
        c = json.loads(json.dumps(concept))
        c["concept_id"], c["narrative_angle"], c["thumbnail_text"] = f"C{i + 1}", f"a{i}", ["Uno", "Due", "Tre"][i]
        c["composition_plan"]["layout"], c["composition_plan"]["emphasis_word"] = layout, None
        c["composition_plan"]["background_frame_id"] = ["F001", "F002", "F001"][i]
        concepts.append(c)
    titles = [{"title_id": f"T{i}", "title": f"Titolo valido numero {chr(65 + i)}", "concept_id": "C1",
               "style": "narrative", "highlight_ids": ["H01"], "transcript_ids": [], "rationale": ""} for i in range(5)]
    return {"concepts": concepts, "titles": titles, "preferred_title_id": "T0", "preferred_concept_id": "C1"}


def _validate_creative(raw, **kw):
    args: dict[str, Any] = dict(n_titles=5, n_concepts=3, highlight_ids={"H01"}, frame_ids={"F001", "F002"}, webcam_ids={"W001"},
                cards={"K01": {"status": "uncertain", "name": "Drago Rosso"}},
                transcript={"T0001": "ho fatto 30 danni"}, games={"g": {"result": "WIN"}},
                highlight_games={"H01": "g"}, supported_numbers={"2"}, uncertain_names=["Drago Rosso"])
    args.update(kw)
    return creative.validate_creative(raw, **args)


def test_creative_validation_accepts_grounded_concepts():
    assert _validate_creative(_creative_answer())["preferred_concept_id"] == "C1"


@pytest.mark.parametrize("mutate,message", [
    (lambda a: a["titles"][0].update(title="Ho fatto 7 vittorie di fila oggi"), "numbers"),
    (lambda a: a["titles"][0].update(title="Il Drago Rosso ha deciso la partita"), "uncertain card"),
    (lambda a: a["titles"][0].update(title="Titolo con CTR garantito al cento"), "performance"),
    (lambda a: a["concepts"][1].update(thumbnail_text="troppe parole in questo testo qui"), "1-5 words"),
    (lambda a: [c["composition_plan"].update(layout="board_focus") for c in a["concepts"]], "two different layouts"),
    (lambda a: a["concepts"][0].update(webcam_frame_id=None), "requires webcam"),
    (lambda a: a["concepts"][0]["composition_plan"].update(focus_box=[0.5, 0.5, 0.9, 0.9]), "focus_box"),
    (lambda a: a["titles"].pop(), "exactly 5 titles"),
    (lambda a: a["titles"][1].update(title="TITOLO TUTTO MAIUSCOLO QUI"), "ALL-CAPS"),
])
def test_creative_validation_rejects_ungrounded_or_weak_proposals(mutate, message):
    answer = _creative_answer()
    mutate(answer)
    with pytest.raises(ValueError, match=message):
        _validate_creative(answer)


def test_numbers_cited_by_evidence_are_allowed_and_win_badge_needs_a_win():
    answer = _creative_answer()
    answer["concepts"][0]["title"] = "Ho fatto 30 danni in un turno"
    assert _validate_creative(answer)
    with pytest.raises(ValueError, match="WIN badge"):
        _validate_creative(_creative_answer(), games={"g": {"result": "LOSS"}})


def test_schemas_are_strict_for_structured_outputs():
    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(story.RESPONSE_SCHEMA)
    walk(creative.RESPONSE_SCHEMA)


def test_packaging_errors_carry_cli_exit_codes():
    assert PackagingError("invalid_arguments", "x").exit_code == 2
    assert PackagingError("agent_timeout", "x").exit_code == 20


# ---------- V2: brief, typography, quality gate ----------

def test_brief_keeps_highlight_context_and_samples_quiet_passages():
    from analysis.packaging.brief import select_transcript
    from analysis.packaging.models import HighlightCandidate

    lines = [TranscriptLine(f"T{i:04d}", float(i * 5), float(i * 5 + 4), float(i * 5), float(i * 5 + 4),
                            f"frase numero {i} " + "parole " * 12, 0.8) for i in range(1, 120)]
    game = GameContext("g", 1, "WIN", 0, 0.0, 600.0, 0.0, 600.0, 600.0, "project_gameplay")
    h = HighlightCandidate("H01", "g", 0, 100, 110, 100, 110, 105, ["audio_peak"], {}, ["T0020", "T0021"], "", "", 1.0)
    chosen = select_transcript(game, lines, [h], budget=3000)
    ids = [line.id for line in chosen]
    assert "T0020" in ids and "T0021" in ids  # highlight context first
    assert min(line.edited_start for line in chosen) < 60 and max(line.edited_start for line in chosen) > 500
    assert sum(len(line.text) + 16 for line in chosen) <= 3000
    assert ids == sorted(ids)


def test_line_breaks_never_leave_articles_dangling():
    ensure_qt()
    from analysis.packaging.render import _break_penalty

    fitted = fit_text("LISTA DA RIVEDERE", (), 900, 400, max_lines=2, min_px=20, max_px=300)
    assert _break_penalty(fitted.lines) == 0
    assert _break_penalty(["TENGO O", "CAMBIO?"]) > 0 and _break_penalty(["TENGO", "O CAMBIO?"]) == 0


def test_quality_gate_detects_overlap_blur_and_duplicates(tmp_path):
    import cv2

    from analysis.packaging import quality
    from analysis.packaging.frames import write_image
    from analysis.packaging.render import RenderResult

    path = tmp_path / "x.png"
    write_image(path, cv2.resize(synthetic_frame(1.0), (1280, 720)))
    result = RenderResult(str(path), 1280, 720, 1, "png", "reaction_focus",
                          {"lines": ["A"], "fits": True, "timestamp_zone_clear": False, "px_at_320": 12,
                           "bounds": [100, 100, 400, 200]},
                          [], [{"kind": "face", "rect": [150, 120, 200, 200]},
                               {"kind": "background", "rect": [0, 0, 1280, 720], "sharpness": 10.0}])
    plan = SimpleNamespace(webcam=None, cards=[])
    codes = {i.code for i in quality.inspect(result, plan, reference_sharpness=1000.0)}
    assert {"text_in_duration_badge", "text_covers_subject"} <= codes
    assert "background_blurry" not in codes  # reaction_focus pushes the scene back on purpose
    result.layout = "story_focus"
    assert "background_blurry" in {i.code for i in quality.inspect(result, plan, reference_sharpness=1000.0)}
    assert quality.similar_pairs([path, path]) == [(0, 1, 0)]
    composition: dict[str, Any] = {"text_side": "left"}
    assert quality.apply_fix(composition, {}, "flip_text") and composition["text_side"] == "right"
    assert quality.apply_fix(composition, {}, "backdrop") and composition["render_tuning"]["backdrop"] == 1.25


def test_old_layout_names_render_with_the_new_archetypes(tmp_path):
    result = ThumbnailRenderer().render(_plan(layout="card_spotlight", cards=[synthetic_frame(2.0)[40:300, 80:260]]),
                                        tmp_path / "old.png")
    assert result.layout == "board_focus" and any(s["kind"] == "card" for s in result.subjects)


def test_frame_ranking_prefers_settled_sharp_frames():
    import cv2

    from analysis.packaging.frames import pick_diverse, rank_gameplay

    sharp = synthetic_frame(1.0)
    blurry = cv2.GaussianBlur(sharp, (0, 0), 6)
    ranked = rank_gameplay([(0.0, blurry), (0.5, sharp), (1.0, blurry)], target=0.0)
    assert ranked[0][1] == 0.5
    taken: list[int] = []
    picks = pick_diverse(ranked, k=2, taken=taken)
    assert picks[0][1] == 0.5 and len(taken) == len(picks)


def test_automatic_fixes_are_reversible_and_never_compound():
    from analysis.packaging import quality

    concept: dict[str, Any] = {"webcam_frame_id": "W001",
                               "composition_plan": {"layout": "reaction_focus", "text_side": "left"}}
    composition = concept["composition_plan"]
    assert quality.apply_fix(composition, concept, "flip_text")
    assert quality.apply_fix(composition, concept, "drop_webcam")
    assert quality.apply_fix(composition, concept, "smaller_text")
    assert not quality.apply_fix(composition, concept, "smaller_text")  # one text reduction at most
    assert (composition["text_side"], composition["layout"], concept["webcam_frame_id"]) == ("right", "story_focus", None)
    quality.restore_original(composition, concept)
    assert (composition["text_side"], composition["layout"], concept["webcam_frame_id"]) == ("left", "reaction_focus", "W001")
    assert "render_tuning" not in composition
    manual: dict[str, Any] = {"composition_plan": {"render_tuning": {"text_scale": 0.9}}}
    quality.restore_original(manual["composition_plan"], manual)  # tuning written by a person is kept
    assert manual["composition_plan"]["render_tuning"] == {"text_scale": 0.9}


def test_accented_headline_stays_inside_the_safe_area(tmp_path):
    result = ThumbnailRenderer().render(_plan(layout="story_focus", headline="Dov'è quella magia?", webcam=None,
                                              inset=synthetic_frame(4.0)), tmp_path / "accent.png")
    assert result.text["fits"] and result.text["bounds"][1] >= 18.0
