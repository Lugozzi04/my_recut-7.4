"""Clips engine: candidates on sentence boundaries, source mapping through cuts, FFmpeg command and render.

No provider call. The render test uses the bundled FFmpeg on a few seconds of synthetic video.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from analysis.clips.candidates import Analysis, Line, _snap, build_candidates
from analysis.clips.models import ClipCandidate, ClipError
from analysis.clips.render import clip_command, encoder_args, partial_path
from analysis.clips.service import ClipSession


def write_analysis(folder: Path, *, pieces: list[dict[str, Any]], lines: list[tuple[float, float, str]],
                   highlights: list[dict[str, Any]], games: list[dict[str, Any]] | None = None,
                   sources: list[str] | None = None, digest: str = "d1") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    timeline = {"sources": sources or ["vod.mp4"], "pieces": pieces, "digest": digest}
    (folder / "content_analysis.json").write_text(json.dumps({"timeline": timeline, "frames": [
        {"id": "F001", "path": "frames/F001.jpg"}]}), encoding="utf-8")
    (folder / "transcript.json").write_text(json.dumps({"lines": [
        {"edited_start": a, "edited_end": b, "text": t} for a, b, t in lines]}), encoding="utf-8")
    (folder / "highlights.json").write_text(json.dumps({"highlights": highlights}), encoding="utf-8")
    if games is not None:
        (folder / "game_contexts.json").write_text(json.dumps({"games": games}), encoding="utf-8")
    return folder


def sentences(start: float, end: float, step: float = 4.0) -> list[tuple[float, float, str]]:
    out, t, n = [], start, 0
    while t + step <= end:
        out.append((t, t + step - 0.3, f"frase numero {n}."))
        t, n = t + step, n + 1
    return out


def highlight(hid: str, a: float, b: float, score: float, game: str = "g1") -> dict[str, Any]:
    return {"id": hid, "edited_start": a, "edited_end": b, "local_score": score, "reason": "peak",
            "game_id": game, "frame_ids": ["F001"]}


def test_source_parts_follow_the_cuts_of_the_edit(tmp_path):
    folder = write_analysis(tmp_path / "a", pieces=[
        {"source_index": 0, "source_start": 100.0, "source_end": 130.0, "edited_start": 0.0},
        {"source_index": 0, "source_start": 200.0, "source_end": 260.0, "edited_start": 30.0}],
        lines=sentences(0, 90), highlights=[])
    analysis = Analysis.load(folder)
    assert analysis.duration == 90.0
    assert analysis.source_parts(20.0, 45.0) == [(0, 120.0, 130.0), (0, 200.0, 215.0)]


def test_snap_starts_and_ends_on_sentences_inside_min_max():
    lines = [Line(0, 3.7, "Ciao."), Line(4, 7.7, "questa partita"), Line(8, 11.7, "è assurda."),
             Line(12, 15.7, "Davvero."), Line(16, 19.7, "Poi gioco."), Line(20, 23.7, "Fine.")]
    a, b = _snap(lines, 9.0, 13.0, lo=0, hi=24, min_s=10, max_s=20)
    assert a == pytest.approx(4 - 0.15)  # back to the start of "questa partita"
    assert b - a >= 10 and b - a <= 20 and b == pytest.approx(15.7 + 0.35)
    a, b = _snap(lines, 0.0, 24.0, lo=0, hi=24, min_s=5, max_s=10)
    assert b - a <= 10


def test_candidates_rank_dedupe_avoid_greetings_and_stay_inside_their_game(tmp_path):
    games = [{"game_id": "g1", "edited_start": 0.0, "edited_end": 120.0, "result": "WIN"},
             {"game_id": "g2", "edited_start": 120.0, "edited_end": 300.0, "result": "LOSS"}]
    lines = sentences(0, 300)
    lines[0] = (0, 3.7, "Ciao a tutti e buona visione.")
    folder = write_analysis(tmp_path / "a", pieces=[
        {"source_index": 0, "source_start": 0.0, "source_end": 300.0, "edited_start": 0.0}],
        lines=lines, games=games, highlights=[
            highlight("H01", 0.0, 15.0, 3.0),            # channel intro: penalised
            highlight("H02", 105.0, 125.0, 2.0),         # crosses into game 2: clamped to game 1
            highlight("H03", 106.0, 124.0, 1.5),         # same moment: removed as duplicate
            highlight("H04", 200.0, 215.0, 1.0, "g2")])
    clips = build_candidates(Analysis.load(folder), count=5, min_s=20, max_s=40)
    assert [c.highlight_id for c in clips] == ["H01", "H02", "H04"]  # H03 duplicates H02
    by_id = {c.highlight_id: c for c in clips}
    assert by_id["H02"].edited_end <= 120.0 and by_id["H02"].result == "WIN"
    assert by_id["H01"].score == pytest.approx(2.4) and by_id["H01"].notes == ["greeting / call to action"]
    assert all(20 <= c.duration <= 40 for c in clips)
    assert [c.id for c in clips] == [f"C{n:02d}" for n in range(1, len(clips) + 1)]
    assert by_id["H04"].frame == "frames/F001.jpg" and by_id["H04"].parts[0][0] == 0


def test_clip_command_trims_each_kept_part_and_normalizes(tmp_path):
    clip = ClipCandidate("C01", 0, 12, 1.0, "", parts=[(0, 10.0, 15.0), (1, 40.0, 47.0)])
    cmd = clip_command("ffmpeg", clip, ["a.mp4", "b.mp4"], tmp_path / "x.partial.mp4", codec="libx264", fps=60)
    joined = " ".join(cmd)
    assert cmd.count("-i") == 2 and "-ss 10.000 -t 5.000 -i a.mp4" in joined and "-ss 40.000 -t 7.000 -i b.mp4" in joined
    assert "concat=n=2:v=1:a=1" in joined and "loudnorm=I=-14" in joined and "fps=60" in joined
    assert "afade=t=in" in joined and cmd[-1].endswith("x.partial.mp4")
    assert "-crf" in encoder_args("libx264") and "cqp" in encoder_args("h264_amf")
    assert partial_path(Path("C01.mp4")).name == "C01.partial.mp4"
    with pytest.raises(ClipError):
        clip_command("ffmpeg", ClipCandidate("C02", 0, 1, 0, ""), [], tmp_path / "y.mp4", codec="libx264")


def quiet_series(analysis: Analysis):
    """No audio/visual evidence: transcript-only ranking (tests never decode media)."""
    from analysis.clips.scoring import build_series
    return build_series(analysis)


class FakeMap:
    def __init__(self, digest: str) -> None:
        self._digest = digest

    def digest(self) -> str:
        return self._digest


def test_session_reuses_matching_packaging_run_and_keeps_ticks(tmp_path):
    video = tmp_path / "Hunter.mp4"
    video.write_bytes(b"x")
    write_analysis(tmp_path / "Hunter_packaging", digest="same", pieces=[
        {"source_index": 0, "source_start": 0.0, "source_end": 200.0, "edited_start": 0.0}],
        lines=sentences(0, 200), highlights=[highlight("H01", 50, 65, 2.0, "video"),
                                             highlight("H02", 120, 135, 1.0, "video")])
    called = []
    session = ClipSession(video=str(video), edit_map_factory=lambda: FakeMap("same"),
                          analyzer=lambda folder: called.append(folder), series_factory=quiet_series)
    clips = session.candidates(count=2, ai=None)
    assert not called and session.load_analysis().folder == tmp_path / "Hunter_packaging"
    assert session.folder == tmp_path / "Hunter_clips" and session.store.is_file()
    clips[1].selected = True
    session.save(clips)
    again = ClipSession(video=str(video), edit_map_factory=lambda: FakeMap("same"),
                        series_factory=quiet_series).candidates(count=2, refresh=True)
    assert [c.selected for c in again] == [False, True]


def test_session_builds_local_analysis_when_the_edit_changed(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    write_analysis(tmp_path / "v_packaging", digest="old", pieces=[
        {"source_index": 0, "source_start": 0.0, "source_end": 100.0, "edited_start": 0.0}],
        lines=sentences(0, 100), highlights=[])

    def analyze(folder: Path) -> None:
        write_analysis(folder, digest="new", pieces=[
            {"source_index": 0, "source_start": 0.0, "source_end": 100.0, "edited_start": 0.0}],
            lines=sentences(0, 100), highlights=[highlight("H01", 30, 45, 1.0, "video")])
    session = ClipSession(video=str(video), edit_map_factory=lambda: FakeMap("new"), analyzer=analyze,
                          series_factory=quiet_series)
    assert len(session.candidates(count=1)) == 1 and session.load_analysis().folder == tmp_path / "v_clips" / "analysis"
    with pytest.raises(ClipError):
        ClipSession(video=str(video), project="p.autocutter")


def test_cli_parses_clips_command():
    from automation.cli import build_parser
    args = build_parser().parse_args(["clips", "--video", "v.mp4", "--render", "C01,C03", "--max", "45"])
    assert args.command == "clips" and args.render == "C01,C03" and args.max_s == 45.0


def test_render_real_ffmpeg_clip_across_a_cut(tmp_path):
    from analysis.clips.render import render_clip
    from utils.ffmpeg import ensure_ffmpeg, ffprobe_duration_seconds
    from utils.subprocess_utils import run_no_window
    try:
        ffmpeg, _ = ensure_ffmpeg()
    except Exception:
        pytest.skip("FFmpeg not available")
    source = tmp_path / "source.mp4"
    run_no_window([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                   "testsrc2=size=640x360:rate=30:duration=12", "-f", "lavfi", "-i", "sine=frequency=440:duration=12",
                   "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(source)], check=True)
    clip = ClipCandidate("C01", 0.0, 5.0, 1.0, "", parts=[(0, 1.0, 3.0), (0, 7.0, 10.0)])
    target = tmp_path / "out" / "C01.mp4"
    seen: list[float] = []
    result = render_clip(clip, [str(source)], target, ffmpeg=ffmpeg, codec="libx264", on_progress=seen.append)
    assert target.is_file() and not partial_path(target).exists()
    assert abs(ffprobe_duration_seconds(str(target)) - 5.0) < 0.3 and result["codec"] == "libx264"
    assert seen and seen[-1] == 1.0


# ---- phase 3: Shorts 9:16 ----------------------------------------------------------------------------------
WARLOCK_ROI = (0.001, 0.513, 0.2052, 0.2667)


def test_vertical_layout_webcam_on_top_board_below_without_overlap():
    from analysis.clips.layout import compute_layout
    layout = compute_layout(1920, 1080, WARLOCK_ROI)
    assert layout.mode == "webcam_board" and layout.size == (1080, 1920)
    assert 0.30 * 1920 <= layout.cam_h <= 0.42 * 1920 and layout.cam_h + layout.board_h == 1920
    cx, cy, cw, ch = layout.webcam
    assert abs(cw / ch - 1080 / layout.cam_h) < 0.02  # cropped to the panel aspect, no stretching
    assert cx >= 0 and cx + cw <= 0.2062 * 1920 and cy >= 0.51 * 1080 and cy + ch <= 0.781 * 1080
    bx, by, bw, bh = layout.board
    assert bx > cx + cw and abs(bw / bh - 1080 / layout.board_h) < 0.02 and bh == 1080
    assert layout.caption_y == layout.cam_h  # captions on the seam
    graph = ";".join(layout.filter_graph())
    assert "vstack=inputs=2[stack]" in graph and f"crop={cw}:{ch}:{cx}:{cy}" in graph
    fallback = compute_layout(1920, 1080, None)
    assert fallback.mode == "board_only" and fallback.webcam is None and "boxblur" in ";".join(fallback.filter_graph())


def test_captions_chunks_highlight_the_spoken_word_relative_to_the_clip(tmp_path):
    from analysis.clips.captions import ass_document, chunk_words, clip_words
    words = [(0.0, 0.3, "Ma"), (0.3, 0.6, "che"), (0.6, 1.0, "succede?"), (1.0, 1.4, "Questa"),
             (2.5, 2.9, "partita"), (2.9, 3.4, "assurdissimamente"), (3.4, 3.8, "lunga")]
    chunks = chunk_words(words)
    assert [[w[2] for w in c] for c in chunks] == [["Ma", "che", "succede?"], ["Questa"], ["partita"],
                                                    ["assurdissimamente"], ["lunga"]]
    doc = ass_document(words, duration=5.0, size=(1080, 1920), caption_y=700)
    events = [line for line in doc.splitlines() if line.startswith("Dialogue:")]
    assert len(events) == 7 and "{\pos(540,700)" in events[0]
    assert "\\fs" in events[0] and "\\fs" not in events[3]  # the long line is shrunk to fit, "QUESTA" is not
    assert events[0].endswith("{\c&H0000D7FF&}MA{\c&H00FFFFFF&} CHE SUCCEDE?")
    assert "0:00:01.00,0:00:01.80" in events[3]  # held 0.4 s, then nothing until the next word
    folder = write_analysis(tmp_path / "a", pieces=[
        {"source_index": 0, "source_start": 0.0, "source_end": 100.0, "edited_start": 0.0}],
        lines=[(10.0, 12.0, "parole senza tempi")], highlights=[])
    analysis = Analysis.load(folder)
    clip = ClipCandidate("C01", 9.0, 20.0, 1.0, "")
    assert [(round(a, 2), w) for a, _b, w in clip_words(analysis, clip)] == [(1.0, "parole"), (1.67, "senza"),
                                                                            (2.33, "tempi")]


def test_vertical_command_normalizes_to_source_and_burns_relative_captions(tmp_path):
    from analysis.clips.layout import compute_layout
    layout = compute_layout(1920, 1080, WARLOCK_ROI)
    clip = ClipCandidate("C01", 0, 12, 1.0, "", parts=[(0, 10.0, 15.0), (0, 40.0, 47.0)])
    cmd = clip_command("ffmpeg", clip, ["a.mp4"], tmp_path / "x.partial.mp4", codec="libx264", layout=layout,
                       captions="C01_9x16.ass", fonts=".fonts")
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "scale=1920:1080:force_original_aspect_ratio=decrease" in graph
    assert "[stack]ass=C01_9x16.ass:fontsdir=.fonts,format=yuv420p[vo]" in graph
    assert cmd[cmd.index("-map") + 1] == "[vo]"


def test_render_real_ffmpeg_short_with_captions(tmp_path):
    from analysis.clips.captions import ass_document, install_font
    from analysis.clips.layout import compute_layout
    from analysis.clips.render import render_clip
    from utils.ffmpeg import ensure_ffmpeg
    from utils.subprocess_utils import run_no_window
    try:
        ffmpeg, ffprobe = ensure_ffmpeg()
    except Exception:
        pytest.skip("FFmpeg not available")
    source = tmp_path / "source.mp4"
    run_no_window([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                   "testsrc2=size=1280x720:rate=30:duration=8", "-f", "lavfi", "-i", "sine=frequency=440:duration=8",
                   "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(source)], check=True)
    out = tmp_path / "clips"
    out.mkdir()
    layout = compute_layout(1280, 720, WARLOCK_ROI)
    font = install_font(out)
    (out / "C01_9x16.ass").write_text(ass_document([(0.2, 0.8, "ciao"), (0.8, 1.5, "mondo")], duration=4.0,
                                                   size=layout.size, caption_y=layout.caption_y, font=font),
                                      encoding="utf-8")
    clip = ClipCandidate("C01", 0.0, 4.0, 1.0, "", parts=[(0, 1.0, 3.0), (0, 5.0, 7.0)])
    target = out / "C01_9x16.mp4"
    render_clip(clip, [str(source)], target, ffmpeg=ffmpeg, codec="libx264", layout=layout,
                captions="C01_9x16.ass", fonts=".fonts")
    size = run_no_window([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                          "-of", "csv=p=0:s=x", str(target)], capture_output=True, text=True).stdout.strip()
    assert size == "1080x1920" and not partial_path(target).exists()


def test_render_rejects_unknown_format(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    write_analysis(tmp_path / "v_packaging", digest="d", pieces=[
        {"source_index": 0, "source_start": 0.0, "source_end": 100.0, "edited_start": 0.0}],
        lines=sentences(0, 100), highlights=[highlight("H01", 30, 45, 1.0, "video")])
    session = ClipSession(video=str(video), edit_map_factory=lambda: FakeMap("d"), series_factory=quiet_series)
    with pytest.raises(ClipError):
        session.render(session.candidates(count=1), formats=("square",))
