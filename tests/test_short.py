"""Twitch clip -> YouTube Short: links, cold open, cut to 20-40 s, board zoom, big captions, download, CLI, page."""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from analysis.clips.candidates import Analysis  # noqa: E402
from analysis.clips.captions import ass_document, chunk_words, fitted_size  # noqa: E402
from analysis.clips.layout import compute_layout  # noqa: E402
from analysis.clips.models import ClipError  # noqa: E402
from analysis.clips.scoring import GRID_S, Series  # noqa: E402
from analysis.clips.short import (MAX_ZOOM, download_clip, find_peak, is_clip_url, plan_short,  # noqa: E402
                                  play_focus, short_words)
from test_clips import write_analysis  # noqa: E402


def clip_analysis(tmp_path: Path, seconds: float, lines: list[tuple[float, float, str]]) -> Analysis:
    folder = write_analysis(tmp_path / "a", pieces=[{"source_index": 0, "source_start": 0.0, "source_end": seconds,
                                                     "edited_start": 0.0}], lines=lines, highlights=[])
    return Analysis.load(folder)


def series_with_peak(analysis: Analysis, peak: float) -> Series:
    n = int(np.ceil(analysis.duration / GRID_S)) + 1
    arousal = np.zeros(n)
    arousal[int((peak - 0.5) / GRID_S):int((peak + 1.0) / GRID_S)] = 1.0
    return Series(arousal, np.zeros(n))


def talk(start: float, end: float, text: str = "e poi gioco questa carta") -> list[tuple[float, float, str]]:
    return [(t, t + 1.8, text) for t in np.arange(start, end - 1.8, 2.0)]


@pytest.mark.parametrize("url, ok", [
    ("https://clips.twitch.tv/HungryCreativeLapwingPanicBasket-yveJ0MtZsqttNYEe", True),
    ("https://www.twitch.tv/droodthund3r/clip/ManlyEnchantingSnailYee-9g-Dh15wdrqrdPQ8?filter=clips", True),
    ("https://m.twitch.tv/lugozzi/clip/Abc-123", True),
    ("https://www.twitch.tv/videos/123456", False),
    ("https://youtube.com/shorts/x", False),
    ("C:/clips/a.mp4", False),
])
def test_twitch_clip_links(url, ok):
    assert is_clip_url(url) is ok


def test_a_clip_that_fits_is_kept_whole_with_the_peak_shown_first(tmp_path):
    analysis = clip_analysis(tmp_path, 30.0, [(0.0, 1.8, "allora vediamo")] + talk(2, 18) +
                             [(18.0, 20.0, "no no no non ci credo!")] + talk(20, 30))
    plan = plan_short(analysis, series_with_peak(analysis, 18.8))
    assert plan.main == (0.0, 30.0) and plan.teaser is not None  # a person chose this clip: nothing is dropped
    a, b = plan.teaser
    assert a <= 18.8 <= b and 1.2 <= b - a <= 3.0 and "cold open" in " ".join(plan.notes)
    assert plan.parts[0] == (0, a, b) and plan.duration == pytest.approx(30.0 + b - a)


def test_a_long_clip_is_cut_around_the_peak_and_ends_after_the_reaction(tmp_path):
    analysis = clip_analysis(tmp_path, 60.0, talk(0, 40) + [(40.0, 42.0, "ma che fortuna! assurdo!")]
                             + [(42.2, 44.0, "ma dai")] + talk(48, 60))
    plan = plan_short(analysis, series_with_peak(analysis, 40.6))
    start, end = plan.main
    assert 43.9 <= end <= 45.0  # right after the reaction: no tail
    assert plan.duration <= 40.0 + 1e-6 and plan.duration >= 20.0
    assert any("cut to the setup" in n for n in plan.notes)


def test_no_cold_open_when_the_peak_is_already_at_the_start_or_when_disabled(tmp_path):
    analysis = clip_analysis(tmp_path, 25.0, [(0.2, 2.0, "non ci credo!")] + talk(3, 25))
    assert plan_short(analysis, series_with_peak(analysis, 0.8)).teaser is None
    later = clip_analysis(tmp_path / "b", 25.0, talk(0, 25))
    assert plan_short(later, series_with_peak(later, 15.0), cold_open=False).teaser is None


def test_silent_intro_and_silent_tail_are_trimmed(tmp_path):
    analysis = clip_analysis(tmp_path, 40.0, talk(4, 30) + [(30.0, 31.0, "letale!")])
    plan = plan_short(analysis, series_with_peak(analysis, 30.4), cold_open=False)
    assert plan.main[0] == pytest.approx(4.0 - 0.15) and plan.main[1] < 32.0
    assert {"silent intro trimmed", "silent tail trimmed"} <= set(plan.notes)


def test_peak_prefers_loud_reaction_words(tmp_path):
    analysis = clip_analysis(tmp_path, 30.0, talk(0, 10) + [(10.0, 12.0, "ma che cazzo, non ci credo")] + talk(12, 30))
    n = int(np.ceil(analysis.duration / GRID_S)) + 1
    flat = Series(np.full(n, 0.5), np.zeros(n))
    peak, why = find_peak(analysis, flat)
    assert 10.0 <= peak <= 12.0 and "reaction" in why


def test_short_words_follow_the_parts_on_the_short_timeline(tmp_path):
    analysis = clip_analysis(tmp_path, 20.0, [(2.0, 3.0, "uno"), (10.0, 11.0, "dieci")])
    words = short_words(analysis, [(0, 9.5, 11.5), (0, 0.0, 20.0)])
    assert [w[2] for w in words] == ["dieci", "uno", "dieci"]
    assert words[0][0] == pytest.approx(0.5) and words[1][0] == pytest.approx(2.0 + 2.0)


def frames_with_change(box: tuple[float, float, float, float], count: int = 8) -> np.ndarray:
    frames = np.zeros((count, 180, 320), dtype=np.uint8)
    x, y, w, h = box
    for n in range(count):
        frames[n, int(y * 180):int((y + h) * 180), int(x * 320):int((x + w) * 320)] = 200 if n % 2 else 30
    return frames


def test_board_zoom_follows_a_local_play_and_skips_the_webcam():
    focus, note = play_focus(frames_with_change((0.35, 0.4, 0.12, 0.15)), (0.0, 0.5, 0.2, 0.3), 0.5)
    assert focus is not None and focus[2] > 1.12 and focus[2] <= MAX_ZOOM and "zoomed" in note
    assert 0.35 <= focus[0] <= 0.47 and 0.4 <= focus[1] <= 0.55
    only_webcam, note = play_focus(frames_with_change((0.0, 0.55, 0.18, 0.25)), (0.0, 0.5, 0.2, 0.3), 0.5)
    assert only_webcam is None and "barely changes" in note  # the creator moving is not a play
    wide, _ = play_focus(frames_with_change((0.2, 0.2, 0.6, 0.6)), None, 0.5)
    assert wide is not None and wide[2] == 1.0  # the whole board changes: centred, no zoom


def test_layout_zoom_crops_inside_the_frame_with_the_panel_aspect():
    plain = compute_layout(1920, 1080, (0.0, 0.5, 0.2, 0.3))
    zoomed = compute_layout(1920, 1080, (0.0, 0.5, 0.2, 0.3), focus=(0.4, 0.45, 1.5))
    x, y, w, h = zoomed.board
    assert h == pytest.approx(plain.board[3] / 1.5, abs=2) and w == pytest.approx(plain.board[2] / 1.5, abs=2)
    assert 0 <= x and x + w <= 1920 and 0 <= y and y + h <= 1080
    edge = compute_layout(1920, 1080, None, focus=(0.98, 0.98, 1.4))
    ex, ey, ew, eh = edge.board
    assert ex + ew <= 1920 and ey + eh <= 1080


def test_big_captions_shrink_only_when_a_line_would_not_fit_and_keep_elisions():
    assert fitted_size("GG", 113, 1080) == 113
    assert fitted_size("PRECISAMENTE ASSURDO", 113, 1080) < 113
    chunks = chunk_words([(0.0, 0.2, "c'"), (0.2, 0.4, "è"), (0.5, 0.9, "incredibile")])
    assert [w[2] for w in chunks[0]][0] == "c'è"
    document = ass_document([(0.0, 0.5, "assolutamente"), (0.5, 1.0, "incredibile")], duration=2.0,
                            size=(1080, 1920), caption_y=700, scale=0.105)
    assert "Cap,Arial Black,113," in document and "\\fs" in document


def test_download_uses_yt_dlp_and_saves_the_metadata(tmp_path):
    seen = {}

    class Downloader:
        def __init__(self, options):
            seen.update(options)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download):
            target = Path(seen["outtmpl"].replace("%(id)s", "123").replace("%(ext)s", "mp4"))
            target.write_bytes(b"x")
            return {"id": "123", "title": "IL KING SE LA CHIAMA", "channel": "vkingplays", "duration": 30,
                    "requested_downloads": [{"filepath": str(target)}]}

    path, meta = download_clip("https://clips.twitch.tv/KawaiiInspiring-uq6", tmp_path,
                               module_loader=lambda: SimpleNamespace(YoutubeDL=Downloader))
    assert path == tmp_path / "123.mp4" and meta["title"] == "IL KING SE LA CHIAMA" and seen["noplaylist"]
    assert json.loads((tmp_path / "123.twitch.json").read_text(encoding="utf-8"))["channel"] == "vkingplays"
    with pytest.raises(ClipError):
        download_clip("https://www.twitch.tv/videos/1", tmp_path)


def test_cli_short_command_and_clip_links_route_to_it():
    from automation.cli import _implicit_command, build_parser
    args = build_parser().parse_args(["short", "https://clips.twitch.tv/Abc", "--no-cold-open", "--max", "35"])
    assert args.command == "short" and args.no_cold_open and args.max_s == 35.0 and not args.no_zoom
    assert _implicit_command(["https://clips.twitch.tv/Abc"]) == ["short", "https://clips.twitch.tv/Abc"]
    assert _implicit_command(["https://www.twitch.tv/videos/1"])[0] == "vod"


def test_clips_page_short_section_validates_and_starts_the_worker(tmp_path, monkeypatch):
    import ui.clips_page as module
    from test_clips_page import clips_owner
    owner = clips_owner(tmp_path)
    shown = []
    monkeypatch.setattr(module.QMessageBox, "information", lambda *a: shown.append(a[2]))
    owner.clips_short_source.setText("not a link")
    owner.clips.make_short()
    assert shown and "Twitch clip link" in shown[0]
    started = []
    monkeypatch.setattr(module, "_start", lambda owner_, worker, *a: (
        started.append(worker) or SimpleNamespace(finished=SimpleNamespace(connect=lambda *x: None))))
    owner.clips_short_cold_open.setChecked(False)
    owner.clips_short_source.setText(" https://clips.twitch.tv/Abc ")
    owner.clips.make_short()
    worker = started[0]
    assert worker.source == "https://clips.twitch.tv/Abc" and worker.cold_open is False and worker.zoom
    assert owner.btn_clips_short.text() == "Cancel" and not owner.btn_clips_find.isEnabled()
    preview = tmp_path / "p.png"
    from PySide6.QtGui import QImage
    QImage(54, 96, QImage.Format.Format_RGB32).save(str(preview))
    owner.clips._short_done({"output": str(tmp_path / "x_short.mp4"), "duration_s": 31.2, "preview": str(preview),
                             "plan": {"notes": ["cold open: the peak is shown first"]},
                             "youtube": {"title": "IL KING SE LA CHIAMA #Shorts"}, "warnings": []})
    assert not owner.clips_short_result.isHidden() and "IL KING" in owner.lbl_clips_short_title.text()
    assert "x_short.mp4 (31 s)" in owner.lbl_clips_status.text()
    owner.close()
