"""Clip finder v3: result anchor, board-only change, tension before the burst, and the evaluation against marked
moments (v2 kept as a profile for the comparison)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np
import pytest

from analysis.clips.candidates import Analysis
from analysis.clips.evaluate import Mark, parse_marks, report, score
from analysis.clips.models import ClipCandidate, ClipError
from analysis.clips.scoring import GRID_S, build_series, find_clips, real_games, result_at
from analysis.clips.service import ClipSession
from analysis.packaging.visual import H, W, analyze_kept_board, board_mask
from test_clip_finder import PIECE, flat, talk
from test_clips import FakeMap, quiet_series, write_analysis


def win_game(result_screen: float | None, end: float = 200.0) -> dict:
    markers = {"result_screen": {"source": result_screen, "edited": result_screen, "exact": True}} \
        if result_screen is not None else {}
    return {"game_id": "g1", "origin": "detector", "edited_start": 0.0, "edited_end": end, "result": "WIN",
            "markers": markers}


def test_result_time_is_the_marker_else_the_end_of_the_game():
    assert result_at(win_game(150.0)) == 150.0
    assert result_at(win_game(None, end=180.0)) == 180.0  # islands / export selections end on the result
    unseen = {**win_game(None, end=3012.0), "markers": {"vs_screen": {"source": 635.5, "edited": 635.5}}}
    assert result_at(unseen) is None  # the detector saw the VS screen but not the end: no guess


def test_v3_ends_a_few_seconds_after_the_result_screen_even_in_silence(tmp_path):
    lines = talk(0, 140) + talk(172, 400)  # the creator is silent around the VICTORY screen at 150 s
    folder = write_analysis(tmp_path / "a", pieces=PIECE, lines=lines, highlights=[], games=[win_game(150.0)])
    analysis = Analysis.load(folder)
    best = find_clips(analysis, flat(analysis), count=3, min_s=20, max_s=40)[0]
    assert best.edited_start < 150 <= best.edited_end <= 150 + 8 and best.result == "WIN"
    assert "end of a WIN game" in best.reason and best.signals["game"] == 1.0
    old = find_clips(analysis, flat(analysis), count=3, min_s=20, max_s=40, profile="v2")[0]
    assert old.edited_end <= 140.5  # v2 only knew sentence ends: the result screen itself is never in the clip


def test_v3_lead_up_before_the_result_scores_part_and_lobby_lingering_decays(tmp_path):
    folder = write_analysis(tmp_path / "a", pieces=PIECE, lines=talk(0, 400), highlights=[],
                            games=[win_game(150.0, end=300.0)])
    analysis = Analysis.load(folder)
    clips = find_clips(analysis, flat(analysis), count=10, min_s=20, max_s=40)
    by_end = {round(c.edited_end): c for c in clips}
    assert clips[0].edited_start < 150 <= clips[0].edited_end
    lead = [c for c in clips if 150 - 35 < c.edited_end < 150]
    assert all(0 < c.signals["game"] < 0.6 for c in lead)
    late = [c for c in clips if c.edited_start < 150 and c.edited_end > 150 + 20]
    assert all(c.signals["game"] < 1.0 for c in late) and by_end


def test_board_change_ignores_webcam_and_overlay_corners_and_long_gaps():
    roi = (0.0, 0.5, 0.2, 0.27)
    base = np.zeros((H, W), dtype=np.uint8)
    webcam = base.copy()
    webcam[int(0.55 * H):int(0.75 * H), :int(0.18 * W)] = 255  # the creator moves in the webcam
    corner = webcam.copy()
    corner[:int(0.12 * H), int(0.85 * W):] = 255  # an alert in the overlay corner
    board = corner.copy()
    board[int(0.3 * H):int(0.7 * H), int(0.3 * W):int(0.7 * W)] = 200  # the board changes
    frames = [(10.0, base), (12.0, webcam), (14.0, corner), (16.0, board), (40.0, base)]

    class Map:
        sources = ["vod.mp4"]

        def spans(self, index, merge_gap):
            return [(0.0, 60.0)]

        def range_digest(self, index, a, b):
            return "r"

        def to_edited(self, t, index=0):
            return t

    store = {}
    cache = SimpleNamespace(load_json=lambda kind, key: store.get(key),
                            store_json=lambda kind, key, value: store.__setitem__(key, value))
    profile = analyze_kept_board(Map(), cache=cache, exclude=roi, scanner=lambda *a, **k: frames)
    values = {t: c for _, t, c in profile.samples}
    assert profile.status == "ok" and set(values) == {12.0, 14.0, 16.0}  # 40 s: 24 s after, not compared
    assert values[12.0] == 0.0 and values[14.0] == 0.0 and values[16.0] > 0.3  # raw share of the board
    assert not board_mask(roi)[int(0.6 * H), int(0.1 * W)]
    again = analyze_kept_board(Map(), cache=cache, exclude=roi, scanner=lambda *a, **k: pytest.fail("cached"))
    assert again.samples == profile.samples


def test_v3_visual_reads_the_board_series_not_the_whole_frame(tmp_path):
    folder = write_analysis(tmp_path / "a", pieces=PIECE, lines=talk(0, 400), highlights=[])
    analysis = Analysis.load(folder)
    overlay = SimpleNamespace(samples=[(0, 100.0, 1.0)])  # whole frame: an alert at 100 s
    board = SimpleNamespace(status="ok", samples=[(0, 300.0, 0.9)])  # board: a swing at 300 s
    series = build_series(analysis, None, overlay, board)
    best = find_clips(analysis, series, count=1, min_s=20, max_s=40)[0]
    old = find_clips(analysis, series, count=1, min_s=20, max_s=40, profile="v2")[0]
    assert best.edited_start <= 300 <= best.edited_end and "big board change" in best.reason
    assert old.edited_start <= 100 <= old.edited_end
    assert build_series(analysis, None, overlay, SimpleNamespace(status="failed", samples=[])).board is None


def test_tension_is_measured_but_does_not_weigh_and_starting_on_the_climax_pays_less(tmp_path):
    folder = write_analysis(tmp_path / "a", pieces=PIECE, lines=talk(0, 400, "gioco questa carta normale."),
                            highlights=[])
    analysis = Analysis.load(folder)
    series = flat(analysis)
    series.arousal[int(60 / GRID_S):int(80 / GRID_S)] = 0.8  # shouting all along
    series.arousal[int(80 / GRID_S):int(84 / GRID_S)] = 1.0
    series.arousal[int(280 / GRID_S):int(284 / GRID_S)] = 1.0  # quiet, then the burst
    clips = find_clips(analysis, series, count=6, min_s=20, max_s=40)
    calm = next(c for c in clips if c.edited_start <= 280 <= c.edited_end and c.signals["tension"] > 0)
    loud = next(c for c in clips if c.edited_start <= 80 <= c.edited_end)
    assert calm.signals["tension"] > 0.9 > 0.4 > loud.signals["tension"]  # reported in the signals...
    assert all("calm before the burst" not in c.reason for c in clips)  # ...but weighs 0 (it hurt the ranking)
    on_climax = next(c for c in clips if abs(c.edited_start - 280) < 1)
    assert on_climax.signals["payoff"] == 0.4


def test_marks_parse_times_ranges_notes_and_comments():
    marks = parse_marks("# momenti\n12:34 lethal\n1:02:03-1:02:40  il topdeck\n754\n\n")
    assert [(m.start, m.end, m.note) for m in marks] == [(754.0, None, "lethal"), (3723.0, 3760.0, "il topdeck"),
                                                         (754.0, None, "")]
    for bad in ("ciao", "5:00-4:00", "# nothing"):
        with pytest.raises(ClipError):
            parse_marks(bad)


def clip(cid: str, a: float, b: float) -> ClipCandidate:
    return ClipCandidate(cid, a, b, 0.0, "")


def test_score_recall_precision_and_ranks():
    marks = [Mark(100.0, None), Mark(300.0, 330.0), Mark(500.0, None)]
    clips = [clip("C01", 290, 320), clip("C02", 10, 40), clip("C03", 80, 99)]  # C03 ends 1 s before: slack
    result = score(clips, marks)
    assert result["ranks"] == [3, 1, None] and result["recall"] == pytest.approx(2 / 3, abs=1e-3)
    assert result["precision"] == pytest.approx(2 / 3, abs=1e-3) and result["mean_rank"] == 2.0
    text = report({"v2": score(clips[1:], marks), "v3": result}, marks)
    assert "missed" in text and "#3" in text and "v3" in text


def test_session_evaluate_compares_profiles_and_saved_proposals_read_only(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    write_analysis(tmp_path / "v_packaging", digest="d", pieces=PIECE, lines=talk(0, 140) + talk(172, 400),
                   highlights=[], games=[win_game(150.0, end=300.0)])
    session = ClipSession(video=str(video), edit_map_factory=lambda: FakeMap("d"), series_factory=quiet_series)
    session.candidates(count=3)
    before = session.store.read_text(encoding="utf-8")
    result = session.evaluate([Mark(150.0, None), Mark(900.0, None)], count=3, min_s=20, max_s=40)
    assert set(result) == {"v2", "v3", "saved"} and result["saved"]["engine"] == "clip_finder_v3"
    assert result["v3"]["ranks"][0] == 1 and result["v2"]["ranks"][0] is None
    assert session.store.read_text(encoding="utf-8") == before and "after the end of the edit" in session.warnings[0]


def test_cli_marks_option_reports_the_evaluation(tmp_path, monkeypatch):
    from automation import clips_cli
    from automation.cli import build_parser
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    write_analysis(tmp_path / "v_packaging", digest="d", pieces=PIECE, lines=talk(0, 400), highlights=[],
                   games=[win_game(150.0, end=300.0)])
    marks = tmp_path / "marks.txt"
    marks.write_text("2:30 vittoria\n", encoding="utf-8")
    real = ClipSession
    monkeypatch.setattr("analysis.clips.service.ClipSession", lambda **kw: real(
        **kw, edit_map_factory=lambda: FakeMap("d"), series_factory=quiet_series))
    args = build_parser().parse_args(["clips", "--video", str(video), "--marks", str(marks), "--count", "3"])
    result = clips_cli.execute(args, open(tmp_path / "log.txt", "w", encoding="utf-8"))
    assert result["marks"] == ["2:30 vittoria"] and result["evaluation"]["v3"]["found"] == 1
    out = tmp_path / "out.txt"
    with out.open("w", encoding="utf-8") as stream:
        clips_cli.human(result, stream)
    assert "recall" in out.read_text(encoding="utf-8") and not (tmp_path / "v_clips" / "clips.json").exists()
    json.dumps(result)


def test_board_changes_are_ranked_within_the_video():
    from analysis.clips.scoring import _ranked
    samples = [(0, float(t), 0.1 + 0.001 * (t % 7)) for t in range(100)] + [(0, 200.0, 0.6), (0, 202.0, 0.7)]
    ranked = {t: v for _, t, v in _ranked(samples)}
    assert ranked[202.0] == 1.0 and ranked[200.0] == 1.0 and max(ranked[t] for t in range(100)) < 0.2
    assert _ranked([(0, 1.0, 0.35)]) == [(0, 1.0, 1.0)]  # too few keyframes: fixed share


def test_games_without_a_seen_end_are_cut_at_the_next_game_so_the_cap_stays_per_game(tmp_path):
    games = [{"game_id": f"g{n}", "origin": "detector", "edited_start": float(a), "edited_end": 400.0,
              "result": "UNKNOWN", "markers": {"vs_screen": {"source": float(a), "edited": float(a)}}}
             for n, a in enumerate((0, 100, 200, 300))]  # the detector saw no end: all run to the end of the video
    folder = write_analysis(tmp_path / "a", pieces=PIECE, lines=talk(0, 400), highlights=[], games=games)
    analysis = Analysis.load(folder)
    assert [(g["edited_start"], g["edited_end"]) for g in real_games(analysis)] == \
        [(0.0, 100.0), (100.0, 200.0), (200.0, 300.0), (300.0, 400.0)]
    clips = find_clips(analysis, flat(analysis), count=10, min_s=20, max_s=30)
    assert len(clips) == 10 and len({c.game_id for c in clips}) == 4  # not 3 clips of the "first" game
    assert all(c.signals["game"] <= 0.5 for c in clips)  # no result time: only the v2 closing bonus (UNKNOWN)


def test_v3_may_end_where_unpunctuated_speech_runs_on_with_a_small_penalty(tmp_path):
    lines = [(t, t + 3.8, "e allora gioco questa carta qua") for t in range(0, 120, 4)]  # no punctuation, no pauses
    folder = write_analysis(tmp_path / "a", pieces=PIECE, lines=lines, highlights=[])
    analysis = Analysis.load(folder)
    assert find_clips(analysis, flat(analysis), count=3, min_s=20, max_s=40, profile="v2") == [] or \
        all(c.edited_end >= 119 for c in find_clips(analysis, flat(analysis), count=3, min_s=20, max_s=40,
                                                     profile="v2"))  # v2: only the very last line may end a clip
    clips = find_clips(analysis, flat(analysis), count=3, min_s=20, max_s=40)
    assert len(clips) == 3 and any("ends mid-sentence" in c.notes for c in clips)


def test_ai_pool_is_not_capped_at_three_clips_per_game(tmp_path):
    games = [{"game_id": f"g{n}", "origin": "detector", "edited_start": float(a), "edited_end": float(a + 100),
              "result": "WIN"} for n, a in enumerate((0, 100, 200, 300))]
    folder = write_analysis(tmp_path / "a", pieces=PIECE, lines=talk(0, 400), highlights=[], games=games)
    analysis = Analysis.load(folder)
    final = find_clips(analysis, flat(analysis), count=10, min_s=20, max_s=24)
    pool = find_clips(analysis, flat(analysis), count=10, pool=20, min_s=20, max_s=24)
    assert len(final) == 10 and max(sum(c.game_id == g for c in final) for g in ("g0", "g1", "g2", "g3")) <= 3
    assert len(pool) == 20  # 4 games x 3 would have stopped the AI candidates at 12
