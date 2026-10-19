"""Clip finder v2: what makes a window rank high (hook, peak, reactions, game closing) or low (chat reading,
greetings, slow openings), the AI rerank contract and the export recognition of the Clips page."""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from analysis.clips.ai_rank import build_prompt, rerank, validate  # noqa: E402
from analysis.clips.candidates import Analysis  # noqa: E402
from analysis.clips.scoring import GRID_S, Series, build_series, find_clips, reaction, real_games  # noqa: E402
from analysis.clips.service import ClipSession  # noqa: E402
from test_clips import FakeMap, quiet_series, write_analysis  # noqa: E402

PIECE = [{"source_index": 0, "source_start": 0.0, "source_end": 400.0, "edited_start": 0.0}]


def talk(start: float, end: float, text: str = "e poi gioco questa carta normale.") -> list[tuple[float, float, str]]:
    out, t = [], start
    while t + 4 <= end:
        out.append((t, t + 3.7, text))
        t += 4
    return out


def flat(analysis: Analysis) -> Series:
    n = int(np.ceil(analysis.duration / GRID_S)) + 1
    return Series(np.zeros(n), np.zeros(n))


def test_reaction_lexicon_scores_outbursts_and_laughter_over_long_talk():
    hype, labels = reaction("No no no, non ci credo! Letale!")
    laugh, laugh_labels = reaction("ahahahah ma che è")
    calm, _ = reaction("in questo turno gioco il due mana e poi passo il turno perché non ho altro da fare")
    assert hype > 0.5 and "letale" in labels and "non ci credo" in labels
    assert laugh >= 0.35 and "laughter" in laugh_labels
    assert calm < 0.1


def test_outburst_with_excited_voice_beats_chat_reading_and_greetings(tmp_path):
    lines = (talk(0, 40, "ciao a tutti e benvenuti, grazie mille per il sub!")
             + talk(40, 120)
             + talk(120, 160, "No no no, non ci credo! Letale! Ahahah")
             + talk(160, 240)
             + talk(240, 280, "grazie mille per i 5 mesi di sub, grazie per il follow"))
    folder = write_analysis(tmp_path / "a", pieces=PIECE, lines=lines, highlights=[])
    analysis = Analysis.load(folder)
    series = flat(analysis)
    series.arousal[int(130 / GRID_S):int(140 / GRID_S)] = 1.0  # the streamer shouts there
    clips = find_clips(analysis, series, count=3, min_s=20, max_s=45)
    best = clips[0]
    assert best.edited_start <= 130 <= best.edited_end and "excited voice" in best.reason
    assert best.signals["reaction"] > 0.5 and best.signals["payoff"] > 0
    chat = [c for c in clips if "chat / sub thanks" in c.notes or "greeting / call to action" in c.notes]
    assert all(c.score < best.score - 0.2 for c in chat)
    assert all(not (min(a.edited_end, b.edited_end) - max(a.edited_start, b.edited_start) > 0.25 * a.duration)
               for i, a in enumerate(clips) for b in clips[i + 1:])  # no overlapping proposals


def test_closing_of_a_won_game_is_preferred_and_games_are_capped(tmp_path):
    games = [{"game_id": "g1", "origin": "detector", "edited_start": 0.0, "edited_end": 200.0, "result": "WIN"},
             {"game_id": "g2", "origin": "detector", "edited_start": 200.0, "edited_end": 400.0, "result": "LOSS"},
             {"game_id": "video", "origin": "whole_video", "edited_start": 0.0, "edited_end": 400.0, "result": None}]
    folder = write_analysis(tmp_path / "a", pieces=PIECE, lines=talk(0, 400), highlights=[], games=games)
    analysis = Analysis.load(folder)
    assert [g["game_id"] for g in real_games(analysis)] == ["g1", "g2"]
    clips = find_clips(analysis, flat(analysis), count=10, min_s=20, max_s=40)
    assert clips[0].edited_end >= 200 - 75 and clips[0].result == "WIN"  # lethal/closing zone of the win
    per_game: dict[str | None, int] = {}
    for clip in clips:
        per_game[clip.game_id] = per_game.get(clip.game_id, 0) + 1
    assert max(per_game.values()) <= 3


def test_series_maps_source_loudness_through_the_cuts(tmp_path):
    pieces = [{"source_index": 0, "source_start": 100.0, "source_end": 110.0, "edited_start": 0.0},
              {"source_index": 0, "source_start": 500.0, "source_end": 520.0, "edited_start": 10.0}]
    folder = write_analysis(tmp_path / "a", pieces=pieces, lines=talk(0, 30), highlights=[])
    analysis = Analysis.load(folder)
    db = np.full(int(600 / 0.05), -40.0)
    db[int(505 / 0.05):int(506 / 0.05)] = -5.0  # shout at source 505 s = edited 15 s
    track = SimpleNamespace(source_index=0, slice=lambda a, b: db[int(a / 0.05):int(b / 0.05)])
    series = build_series(analysis, SimpleNamespace(available=True, tracks=[track]),
                          SimpleNamespace(samples=[(0, 108.0, 0.9)]))
    assert int(np.argmax(series.arousal)) * GRID_S == pytest.approx(15.0, abs=GRID_S)
    assert series.visual[int(8 / GRID_S)] == pytest.approx(0.9)


def ai_answer(clips, analysis, *, keep=lambda c: True, overall=None):
    from analysis.clips.ai_rank import offered_lines
    items = []
    for n, clip in enumerate(clips):
        lines = offered_lines(analysis, clip)
        inside = [i for i in lines if clip.edited_start - 0.5 <= analysis.lines[i].start < clip.edited_end]
        items.append({"id": clip.id, "keep": keep(clip), "hook": 8, "flow": 7, "value": 6, "emotion": 9,
                      "overall": overall(n) if overall else 50 + n, "start_line": f"L{inside[1]:04d}",
                      "end_line": f"L{inside[-1]:04d}", "title": f"Titolo {clip.id}", "reason": "Motivo."})
    return {"clips": items}


def test_ai_rerank_orders_by_the_model_tightens_and_marks_rejects(tmp_path):
    folder = write_analysis(tmp_path / "a", pieces=PIECE, lines=talk(0, 400), highlights=[])
    analysis = Analysis.load(folder)
    clips = find_clips(analysis, flat(analysis), count=4, min_s=20, max_s=40)
    originals = {c.id: (c.edited_start, c.edited_end) for c in clips}
    prompt = build_prompt(analysis, clips, min_s=20, max_s=40)
    assert "HOOK" in prompt and "REJECT" in prompt and "=== C01" in prompt and "> L" in prompt
    answer = ai_answer(clips, analysis, keep=lambda c: c.id != "C04", overall=lambda n: [40, 90, 70, 99][n])
    calls = []

    def run_agent(task, **kwargs):
        calls.append(task)
        kwargs["validate"](answer)
        return SimpleNamespace(answer=answer)
    old_ids = [c.id for c in clips]
    out = rerank(analysis, clips, run_agent=run_agent, count=4, min_s=20, max_s=40)
    assert calls == ["clips_rank"]
    assert [c.title for c in out] == ["Titolo C02", "Titolo C03", "Titolo C01", "Titolo C04"]  # reject last
    assert out[-1].notes[-1] == "AI: weak clip" and out[0].ai["overall"] == 90 and out[0].id == "C01"
    moved = out[0]
    assert moved.edited_start > originals[old_ids[1]][0]  # started one line later
    with pytest.raises(ValueError):
        validate({"clips": [{**answer["clips"][0], "start_line": "L9999"}]}, analysis, clips, min_s=20, max_s=40)


def test_session_ai_failure_keeps_the_local_ranking(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    write_analysis(tmp_path / "v_packaging", digest="d", pieces=PIECE, lines=talk(0, 400), highlights=[])

    class Broken:
        def run(self, *args, **kwargs):
            raise RuntimeError("codex not logged in")
    session = ClipSession(video=str(video), edit_map_factory=lambda: FakeMap("d"), series_factory=quiet_series,
                          agent_factory=lambda provider: Broken())
    clips = session.candidates(count=3, ai="codex")
    assert len(clips) == 3 and session.ranking["ai"] is None and "AI ranking failed" in session.warnings[0]
    saved = json.loads(session.store.read_text(encoding="utf-8"))
    assert saved["engine"] == "clip_finder_v3" and saved["settings"]["ai"] == "codex"
    with pytest.raises(Exception):
        session.candidates(count=3, ai="gpt")


def test_clips_page_recognises_an_export_and_lists_the_last_export(tmp_path):
    from test_clips_page import clips_owner
    video = tmp_path / "VOD final.mp4"
    video.write_bytes(b"render")
    snapshot_dir = tmp_path / "VOD final_recut_abc123"
    snapshot_dir.mkdir()
    source = tmp_path / "vod.mp4"
    source.write_bytes(b"source")
    (snapshot_dir / "export_edit.autocutter").write_text(json.dumps({
        "format": "autocutter_project", "version": 1, "project_id": "p1", "project_name": "VOD",
        "tracks": [{"path": str(source)}]}), encoding="utf-8")
    other = tmp_path / "random.mp4"
    other.write_bytes(b"x")
    owner = clips_owner(tmp_path)
    owner.clips.set_last_export(str(video))  # after Ctrl+E: listed as an export, source not switched
    assert owner.clips_source_combo.currentData() == "project"
    assert any(e["output"] == str(video.resolve()) for e in owner.project.packaging_outputs)
    owner.clips.add_video(str(other), select=True)
    assert owner.clips_video_combo.currentData() == "file:" + str(other.resolve())
    owner.clips_video_combo.setCurrentIndex(owner.clips_video_combo.findText("VOD — export"))
    args = owner.clips._session_args()
    assert args["project"].endswith("export_edit.autocutter") and args["output"].endswith("VOD final_clips")
    owner.close()


def test_ranking_choice_reaches_the_worker(tmp_path, monkeypatch):
    import ui.clips_page as module
    from test_clips_page import clips_owner
    owner = clips_owner(tmp_path)
    assert owner.clips_ranking_combo.currentData() == "codex" and "AI ranking" in owner.lbl_clips_hint.text()
    started = []
    monkeypatch.setattr(module, "_start", lambda owner_, worker, *a: (
        started.append(worker) or SimpleNamespace(finished=SimpleNamespace(connect=lambda *x: None))))
    monkeypatch.setenv("AUTO_CUTTER_OUTPUT_DIR", str(tmp_path / "exports"))
    owner.clips.find()
    assert started[0].ai == "codex"
    owner.clips._thread = owner.clips._worker = None
    owner.clips._finish_job()
    owner.clips_ranking_combo.setCurrentIndex(owner.clips_ranking_combo.findData("none"))
    assert "nothing is sent" in owner.lbl_clips_hint.text()
    owner.clips.find()
    assert started[1].ai is None
    owner.close()


def test_add_exported_video_button_switches_to_video_and_recognises_exports(tmp_path, monkeypatch):
    import ui.clips_page as module
    from test_clips_page import clips_owner
    plain = tmp_path / "old.mp4"
    plain.write_bytes(b"x")
    exported = tmp_path / "Mage.mp4"
    exported.write_bytes(b"render")
    (tmp_path / "vod.mp4").write_bytes(b"source")  # the original VOD is still there
    folder = tmp_path / "Mage_recut_x1"
    folder.mkdir()
    (folder / "export_edit.autocutter").write_text(json.dumps({
        "format": "autocutter_project", "version": 1, "project_id": "m1", "project_name": "Mage",
        "tracks": [{"path": str(tmp_path / "vod.mp4")}]}), encoding="utf-8")
    owner = clips_owner(tmp_path)
    assert not owner.btn_clips_add_video.isHidden()  # visible whatever the source
    picks = iter([str(plain), str(exported)])
    monkeypatch.setattr(module.QFileDialog, "getOpenFileName", lambda *a, **k: (next(picks), ""))
    owner.btn_clips_add_video.click()
    assert owner.clips_source_combo.currentData() == "video"
    assert owner.clips._session_args() == {"video": str(plain.resolve())}
    owner.btn_clips_add_video.click()
    assert owner.clips_video_combo.currentText() == "Mage — export"
    assert owner.clips._session_args()["project"].endswith("export_edit.autocutter")
    owner.close()
