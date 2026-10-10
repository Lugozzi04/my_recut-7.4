"""Boundary candidate infrastructure: synthetic evidence, no ASR/model required."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
from unittest.mock import patch

import numpy as np
import pytest

from analysis.boundaries.analyzer import BoundaryAnalyzer, BoundarySettings
from analysis.boundaries.audio import AudioEvidence, SilenceSettings, complement, silences_from_rms
from analysis.boundaries.candidates import (FEATURE_NAMES, CandidateSettings, Evidence, deduplicate, feature_vector,
                                            generate_candidates, raw_candidates)
from analysis.boundaries.dataset import BoundaryLabel, append_example, candidate_rows, iter_examples, match_candidate
from analysis.boundaries.gameplay import GameplayTimeline
from analysis.boundaries.models import (BoundaryContext, BoundaryFormatError, BoundaryType, CandidateSource,
                                        EvidenceStatus, GameDisposition, GameplayMarker, GameTermination, TimeRange,
                                        TranscriptSegment, TranscriptWord, VisualSample)
from analysis.boundaries.resolvers import ExactBoundaryResolver, agent_payload, parse_agent_response
from analysis.boundaries.transcript import FasterWhisperEngine, NullTranscriptionEngine, TranscriptSettings, _shift
from analysis.boundaries.windows import WindowSettings, search_window
from analysis.gameplay.models import GameplayAnalysisResult, GameResult, GameSegment

OK = EvidenceStatus("ok", "test")
OFF = EvidenceStatus("unavailable", "none")
S = CandidateSettings(visual_candidate_min_change=None)


def evidence(kind=BoundaryType.END, window=(95.0, 135.0), anchor=100.0, *, silences=(), speech=None,
             transcript=(), audio=OK, text=OK, visual_status=OFF, visual=(), markers=(), anchors=None):
    w = TimeRange(*window)
    sil = [TimeRange(*s) for s in silences]
    return Evidence(kind, w, anchor, anchors or {"game_start": 10.0, "game_end": 100.0, "previous_game_end": None,
                                                 "next_game_start": 138.0, "end_of_file": 500.0},
                    audio, sil, complement(w, sil) if speech is None else [TimeRange(*s) for s in speech],
                    text, list(transcript), visual_status, list(visual), list(markers))


def by_source(candidates, source):
    return [c for c in candidates if source in c.sources]


def seg(start, end, text, words=()):
    return TranscriptSegment(start, end, text, 0.8, 0.01, tuple(TranscriptWord(*w) for w in words))


# ---------- candidate generation ----------

def test_silence_candidates_edges_and_long_midpoint_only():
    cands = generate_candidates(evidence(silences=[(102.1, 103.0), (110.4, 111.7)]), S)
    assert [c.timestamp for c in by_source(cands, CandidateSource.SILENCE_START)] == [102.1, 110.4]
    assert [c.timestamp for c in by_source(cands, CandidateSource.SILENCE_END)] == [103.0, 111.7]
    assert [c.timestamp for c in by_source(cands, CandidateSource.SILENCE_MID)] == [111.05]  # 0.9 s pause has none


def test_silence_clipped_by_window_edge_is_not_a_boundary():
    cands = generate_candidates(evidence(silences=[(95.0, 97.0), (133.0, 135.0)]), S)
    assert not any(c.timestamp in (95.0, 135.0) and CandidateSource.SILENCE_START in c.sources for c in cands)
    assert [c.timestamp for c in by_source(cands, CandidateSource.SILENCE_END)] == [97.0]
    assert [c.timestamp for c in by_source(cands, CandidateSource.SILENCE_START)] == [133.0]


def test_transcript_segment_and_sentence_candidates():
    words = [(104.0, 104.5, "questa"), (104.6, 105.2, "impossibile."), (105.9, 106.3, "Ok"), (106.4, 107.8, "prossima")]
    cands = generate_candidates(evidence(transcript=[seg(104.0, 107.8, "questa impossibile. Ok prossima", words)]), S)
    assert [c.timestamp for c in by_source(cands, CandidateSource.TRANSCRIPT_SEGMENT_END)] == [107.8]
    assert [c.timestamp for c in by_source(cands, CandidateSource.SENTENCE_END)] == [105.2]
    assert [c.timestamp for c in by_source(cands, CandidateSource.SENTENCE_START)] == [105.9]
    sentence = by_source(cands, CandidateSource.SENTENCE_END)[0]
    assert sentence.features["is_sentence_boundary"] == 1.0
    assert sentence.evidence["transcript_segment_containing"]["text"].startswith("questa")


def test_anchor_candidates_for_start_and_end():
    end = generate_candidates(evidence(), S)
    assert by_source(end, CandidateSource.RESULT_ANCHOR)[0].timestamp == 100.0
    assert by_source(end, CandidateSource.NEXT_GAME_START) == []  # 138 is outside the window
    end2 = generate_candidates(evidence(window=(95.0, 138.0)), S)
    assert by_source(end2, CandidateSource.NEXT_GAME_START)[0].timestamp == 138.0
    start = generate_candidates(evidence(BoundaryType.START, (0.0, 15.0), 10.0,
                                         anchors={"game_start": 10.0, "game_end": 100.0, "previous_game_end": None,
                                                  "next_game_start": None, "end_of_file": 500.0}), S)
    assert [c.sources[0] for c in start] == [CandidateSource.START_OF_FILE, CandidateSource.GAME_START_ANCHOR]
    assert not by_source(start, CandidateSource.RESULT_ANCHOR)


def test_gameplay_markers_become_candidates():
    marker = GameplayMarker(101.0, CandidateSource.BOARD_DISAPPEARED.value)
    cands = generate_candidates(evidence(markers=[marker]), S)
    assert by_source(cands, CandidateSource.BOARD_DISAPPEARED)[0].timestamp == 101.0


def test_dedup_merges_close_candidates_keeping_anchor_time_and_all_sources():
    cands = generate_candidates(evidence(silences=[(100.15, 101.0)], transcript=[seg(97.0, 100.1, "fine")]), S)
    anchor = by_source(cands, CandidateSource.RESULT_ANCHOR)[0]
    assert anchor.timestamp == 100.0
    assert set(anchor.sources) == {CandidateSource.RESULT_ANCHOR, CandidateSource.SILENCE_START,
                                   CandidateSource.TRANSCRIPT_SEGMENT_END}
    assert anchor.evidence["merged_timestamps"] == [100.0, 100.1, 100.15]
    merged = deduplicate([(1.0, CandidateSource.SILENCE_MID, {}), (1.2, CandidateSource.SILENCE_END, {}),
                          (1.6, CandidateSource.SILENCE_START, {})], 0.25)
    assert [(t, s) for t, s, _ in merged] == [(1.2, (CandidateSource.SILENCE_END, CandidateSource.SILENCE_MID)),
                                             (1.6, (CandidateSource.SILENCE_START,))]


def test_visual_change_candidates_are_local_peaks_above_threshold():
    samples = [VisualSample(100.0 + i * 0.5, v) for i, v in enumerate([0.01, 0.5, 0.2, 0.36, 0.4, 0.05])]
    ev = evidence(visual_status=OK, visual=samples)
    cands = generate_candidates(ev, CandidateSettings(visual_candidate_min_change=0.35))
    assert [c.timestamp for c in by_source(cands, CandidateSource.VISUAL_CHANGE)] == [100.5, 102.0]
    assert by_source(cands, CandidateSource.RESULT_ANCHOR)[0].features["visual_change_score"] == 0.5


# ---------- features ----------

def test_feature_distances_and_audio_features():
    ev = evidence(silences=[(102.0, 104.0)], transcript=[seg(96.0, 101.0, "commento"), seg(105.0, 107.0, "altro")])
    cands = {c.timestamp: c for c in generate_candidates(ev, S)}
    start = cands[102.0].features
    assert start["distance_from_game_end_s"] == pytest.approx(2.0)
    assert start["distance_from_game_start_s"] == pytest.approx(92.0)
    assert start["distance_to_previous_game_end_s"] is None
    assert start["distance_to_next_game_start_s"] == pytest.approx(36.0)
    assert start["offset_from_anchor_s"] == pytest.approx(2.0)
    assert start["silence_before_s"] == 0.0 and start["silence_after_s"] == pytest.approx(2.0)
    assert start["speech_before_s"] == pytest.approx(7.0) and start["speech_after_s"] == 0.0
    assert start["transcript_gap_before_s"] == pytest.approx(1.0) and start["transcript_gap_after_s"] == pytest.approx(3.0)
    mid = cands[103.0].features
    assert mid["is_inside_silence"] == 1.0 and mid["is_inside_speech"] == 0.0
    assert mid["silence_before_s"] == pytest.approx(1.0) and mid["silence_after_s"] == pytest.approx(1.0)
    assert mid["speech_density_before"] == pytest.approx(4 / 5) and mid["speech_density_after"] == pytest.approx(4 / 5)
    assert cands[102.0].evidence["transcript_segment_before"]["text"] == "commento"
    assert set(FEATURE_NAMES) == set(start)


def test_start_and_end_contexts_are_distinguished():
    end = generate_candidates(evidence(), S)[0]
    start = generate_candidates(evidence(BoundaryType.START, (0.0, 15.0), 10.0), S)[0]
    assert end.features["is_end_boundary"] == 1.0 and start.features["is_end_boundary"] == 0.0
    assert end.boundary_type == BoundaryType.END and start.boundary_type == BoundaryType.START
    assert WindowSettings().start_before_s > WindowSettings().start_after_s
    assert WindowSettings().end_after_s > WindowSettings().end_before_s


# ---------- degradation ----------

def test_no_speech_in_window():
    cands = generate_candidates(evidence(silences=[(95.0, 135.0)], speech=[]), S)
    anchor = cands[0]
    assert [c.sources for c in cands] == [(CandidateSource.RESULT_ANCHOR,), (CandidateSource.SILENCE_MID,)]
    assert anchor.features["is_inside_silence"] == 1.0 and anchor.features["speech_density_after"] == 0.0


def test_no_useful_silence_leaves_only_anchors():
    cands = generate_candidates(evidence(silences=[]), S)
    assert [c.sources for c in cands] == [(CandidateSource.RESULT_ANCHOR,)]
    assert cands[0].features["silence_after_s"] == 0.0 and cands[0].features["is_inside_speech"] == 1.0


def test_missing_modalities_give_none_not_zero():
    cands = generate_candidates(evidence(audio=OFF, text=OFF), S)
    values, mask = feature_vector(cands[0])
    for name in ("silence_before_s", "speech_density_after", "transcript_gap_after_s", "words_after",
                 "visual_change_score", "rms_db_before"):
        assert cands[0].features[name] is None
        assert mask[FEATURE_NAMES.index(name)] == 0.0 and values[FEATURE_NAMES.index(name)] == 0.0
    assert mask[FEATURE_NAMES.index("offset_from_anchor_s")] == 1.0


def test_no_transcript_engine_reports_disabled():
    status, results = NullTranscriptionEngine().transcribe("x.mp4", [TimeRange(0, 1), TimeRange(2, 3)])
    assert status.status == "disabled" and not status.available and results == [[], []]


def test_missing_whisper_runtime_is_unavailable(tmp_path):
    engine = FasterWhisperEngine(TranscriptSettings(python=str(tmp_path / "missing-python"), use_cache=False),
                                 cache_directory=tmp_path)
    status, results = engine.transcribe(tmp_path / "video.mp4", [TimeRange(0, 1)])
    assert status.status == "unavailable" and results == [[]]


def test_asr_offsets_are_absolute_and_clamped():
    item = {"start": 1.0, "end": 99.0, "text": " ciao. ", "confidence": 0.7, "no_speech_probability": 0.1,
            "words": [{"start": 1.0, "end": 1.5, "text": "ciao.", "probability": 0.9}]}
    segment = _shift(item, TimeRange(100.0, 130.0))
    assert segment is not None and (segment.start, segment.end, segment.text) == (101.0, 130.0, "ciao.")
    assert segment.words[0].start == 101.0 and segment.ends_sentence


@pytest.mark.parametrize("anchor,expected", [(3.0, ((0.0, 8.0), ("start_of_file",))),
                                             (495.0, ((470.0, 500.0), ("end_of_file",)))])
def test_windows_near_file_bounds(anchor, expected):
    kind = BoundaryType.START if anchor < 10 else BoundaryType.END
    settings = WindowSettings(end_before_s=25.0, end_after_s=35.0)
    window, clipped = search_window(kind, anchor, duration=500.0, settings=settings)
    assert ((window.start, window.end), clipped) == expected


def test_windows_never_cross_neighbouring_games():
    start, clipped = search_window(BoundaryType.START, 200.0, duration=500.0, settings=WindowSettings(),
                                   previous_game_end=190.0)
    assert (start.start, start.end, clipped) == (190.0, 205.0, ("previous_game_end",))
    end, clipped = search_window(BoundaryType.END, 300.0, duration=500.0, settings=WindowSettings(),
                                 next_game_start=320.0)
    assert (end.start, end.end, clipped) == (295.0, 320.0, ("next_game_start",))
    with pytest.raises(ValueError):
        WindowSettings(start_before_s=-1)


# ---------- audio reuse ----------

def test_silence_settings_follow_editor_preset_and_engine():
    settings = SilenceSettings.from_preset()
    assert settings.min_silence_s == pytest.approx(0.48) and settings.smoothing_ms == 120.0
    assert (settings.attack_ms, settings.release_ms) == (120.0, 250.0)
    hop = 0.03
    rms = np.array([0.2] * 100 + [0.0005] * 50 + [0.2] * 100 + [0.0005] * 5 + [0.2] * 50, dtype=np.float32)
    silences = silences_from_rms(rms, offset=1000.0, settings=SilenceSettings(smoothing_ms=0, attack_ms=0, release_ms=0),
                                 threshold=0.01)
    assert len(silences) == 1  # the 0.15 s dip is shorter than min_silence
    assert silences[0].start == pytest.approx(1000 + 100 * hop) and silences[0].end == pytest.approx(1000 + 150 * hop)


def test_mean_db_uses_absolute_time():
    audio = AudioEvidence(OK, [], [], np.array([0.1] * 10 + [0.001] * 10, dtype=np.float32), 50.0, 0.1)
    assert audio.mean_db(50.0, 51.0) == pytest.approx(-20.0)
    assert audio.mean_db(51.0, 52.0) == pytest.approx(-60.0)
    assert audio.mean_db(60.0, 61.0) is None


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_local_rms_window_on_real_media(tmp_path):
    from analysis.boundaries.audio import analyze_audio_window

    source = tmp_path / "tone.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=800:duration=10,volume='if(between(t,4,6),0,1)':eval=frame",
                    "-ar", "16000", str(source)], check=True)
    audio = analyze_audio_window(source, TimeRange(2.0, 8.0), SilenceSettings.from_preset())
    assert audio.status.available
    assert len(audio.silences) == 1
    assert audio.silences[0].start == pytest.approx(4.0, abs=0.35) and audio.silences[0].end == pytest.approx(6.0, abs=0.35)


# ---------- gameplay adapter / terminal partial ----------

def game(index, start, end, result):
    return GameSegment(f"g{index}", index, start, end, GameResult(result))


def gameplay(games, *, duration=500.0, board_tail=(0.45, 0.46), tail_at=499.0):
    rows = [{"timestamp": tail_at - len(board_tail) + 1 + i, "board_score": s} for i, s in enumerate(board_tail)]
    return GameplayAnalysisResult("v.mp4", duration, [], games, "test",
                                  settings={"monitor": {"board_high": 0.38, "board_observations": 2},
                                            "maximum_game_duration_s": 1800.0},
                                  metadata={"monitor_evidence": rows, "board_reacquisitions": [15.0],
                                            "result_windows": [{"triggers": [[101.0, "board_disappeared"]]}]})


def test_disposition_policy():
    analysis = gameplay([game(1, 10, 100, "LOSS"), game(2, 140, 200, "WIN"), game(3, 240, None, "UNKNOWN"),
                         game(4, 300, 380, "UNKNOWN"), game(5, 420, None, "UNKNOWN")])
    infos = {i.index: i for i in map(GameplayTimeline(analysis).info, GameplayTimeline(analysis).games)}
    assert infos[1].disposition == GameDisposition.REMOVE
    assert infos[2].disposition == GameDisposition.KEEP
    assert (infos[3].termination, infos[3].disposition) == (GameTermination.MISSING_RESULT, GameDisposition.KEEP)
    assert infos[4].disposition == GameDisposition.KEEP  # complete but conflicting/unknown result
    assert (infos[5].termination, infos[5].disposition) == (GameTermination.IN_PROGRESS_AT_EOF, GameDisposition.REMOVE_TO_EOF)


@pytest.mark.parametrize("kwargs,reason", [({"board_tail": (0.45, 0.1)}, "no positive"),
                                           ({"tail_at": 400.0}, "far from EOF"),
                                           ({"board_tail": ()}, "no board monitor")])
def test_result_less_last_game_without_eof_evidence_is_kept(kwargs, reason):
    analysis = gameplay([game(1, 420, None, "UNKNOWN")], **kwargs)
    info = GameplayTimeline(analysis).info(analysis.games[0])
    assert info.termination == GameTermination.MISSING_RESULT and info.disposition == GameDisposition.KEEP
    assert reason in info.reason


def test_missing_start_is_kept_and_too_long_terminal_game_is_not_trusted():
    analysis = gameplay([game(1, None, 100, "LOSS")])
    assert GameplayTimeline(analysis).info(analysis.games[0]).disposition == GameDisposition.KEEP
    long_game = gameplay([game(1, 10, None, "UNKNOWN")], duration=5000.0, tail_at=4999.0)
    assert GameplayTimeline(long_game).info(long_game.games[0]).termination == GameTermination.MISSING_RESULT


class FakeEngine:
    def __init__(self):
        self.calls = []

    def transcribe(self, video, windows, *, cancellation=None):
        self.calls.append(list(windows))
        return OK, [[seg(w.start + 1.0, w.start + 2.5, "parlato.")] for w in windows]


def run_analyzer(analysis, index, **kwargs):
    engine = FakeEngine()
    audio = AudioEvidence(OK, [], [], None)
    with patch("analysis.boundaries.analyzer.analyze_audio_window", side_effect=lambda v, w, s, **k: AudioEvidence(
            OK, [TimeRange(w.start + 3.0, w.start + 4.0)], complement(w, [TimeRange(w.start + 3.0, w.start + 4.0)]),
            None)) as mocked_audio, \
         patch("analysis.boundaries.analyzer.analyze_visual_window", return_value=(OFF, [])):
        result = BoundaryAnalyzer(BoundarySettings(), transcription=engine).analyze_game("v.mp4", analysis, index, **kwargs)
    assert audio.status.available and mocked_audio.call_count == len(result.contexts)
    return result, engine


def test_analyzer_builds_start_and_end_contexts_with_one_transcription_call():
    result, engine = run_analyzer(gameplay([game(1, 10, 100, "LOSS"), game(2, 140, 200, "WIN")]), 1)
    assert len(engine.calls) == 1 and len(engine.calls[0]) == 2
    start, end = result.contexts[BoundaryType.START], result.contexts[BoundaryType.END]
    assert (start.window.start, start.window.end, start.window_clipped_by) == (0.0, 15.0, ("start_of_file",))
    assert (end.window.start, end.window.end) == (95.0, 135.0)
    assert any(CandidateSource.BOARD_DISAPPEARED in c.sources for c in end.candidates)
    assert ExactBoundaryResolver().resolve(start).timestamp == 10.0
    assert ExactBoundaryResolver().resolve(end).timestamp == 100.0
    with pytest.raises(ValueError, match="KEEP"):
        run_analyzer(gameplay([game(1, 10, 100, "WIN")]), 1)
    kept, _ = run_analyzer(gameplay([game(1, 10, 100, "WIN")]), 1, require_removable=False)
    assert kept.game.disposition == GameDisposition.KEEP


def test_terminal_partial_end_context_targets_eof():
    result, _ = run_analyzer(gameplay([game(1, 10, 100, "LOSS"), game(2, 420, None, "UNKNOWN")]), 2)
    end = result.contexts[BoundaryType.END]
    assert end.anchors["game_end"] is None and end.window.end == 500.0 and "end_of_file" in end.window_clipped_by
    decision = ExactBoundaryResolver().resolve(end)
    assert decision.timestamp == 500.0
    assert CandidateSource.END_OF_FILE in end.candidates[decision.candidate_index].sources
    assert result.contexts[BoundaryType.START].anchors["previous_game_end"] == 100.0


# ---------- serialization, agent, dataset ----------

@pytest.fixture
def context():
    result, _ = run_analyzer(gameplay([game(1, 10, 100, "LOSS"), game(2, 140, 200, "WIN")]), 1)
    return result.contexts[BoundaryType.END]


def test_context_round_trip_is_json_and_lossless(context):
    raw = json.loads(json.dumps(context.to_mapping(), allow_nan=False))
    again = BoundaryContext.from_mapping(raw)
    assert again.to_mapping() == context.to_mapping()
    assert again.candidates[0].features.keys() == set(FEATURE_NAMES)
    with pytest.raises(BoundaryFormatError):
        BoundaryContext.from_mapping({**raw, "version": 99})


def test_exact_resolver_and_agent_contract(context):
    decision = ExactBoundaryResolver().resolve(context)
    assert (decision.candidate_index, decision.confidence, decision.resolver) == (
        context.candidate_index([CandidateSource.RESULT_ANCHOR]), 1.0, "exact")
    payload = json.loads(json.dumps(agent_payload(context)))
    assert payload["boundary_type"] == "END" and payload["game_result"] == "LOSS" and payload["game_end"] == 100.0
    assert payload["next_game_start"] == 140.0 and len(payload["candidates"]) == len(context.candidates)
    assert payload["transcript"][0]["text"] == "parlato."
    chosen = parse_agent_response(context, '{"selected_candidate": 1, "confidence": 0.91}')
    assert chosen.timestamp == context.candidates[1].timestamp and chosen.confidence == 0.91
    for bad in ('{"selected_candidate": 999, "confidence": 0.5}', '{"selected_candidate": true, "confidence": 0.5}',
                '{"selected_candidate": 0, "confidence": 1.5}', "not json"):
        with pytest.raises(BoundaryFormatError):
            parse_agent_response(context, bad)


def test_dataset_examples_round_trip_and_rows(context, tmp_path):
    target = context.candidates[2].timestamp
    index = match_candidate(context, target + 0.1)
    assert index == 2 and match_candidate(context, 10_000.0) is None
    path = tmp_path / "examples.jsonl"
    append_example(path, context, BoundaryLabel(target, index, "manual"))
    append_example(path, context, BoundaryLabel(target, index, "manual"))
    examples = list(iter_examples(path))
    assert len(examples) == 2 and examples[0][0] == examples[1][0]  # stable id per boundary
    rows = candidate_rows(examples[0][1], examples[0][2])
    assert sum(r["target"] for r in rows) == 1.0 and rows[2]["target"] == 1.0
    assert len(rows[0]["features"]) == len(FEATURE_NAMES) == len(rows[0]["mask"])
    assert rows[2]["distance_to_label_s"] == 0.0


def test_debug_cli_lists_games(tmp_path, capsys):
    from analysis.boundaries.__main__ import main

    path = tmp_path / "analysis.json"
    path.write_text(json.dumps(gameplay([game(1, 10, 100, "LOSS"), game(2, 420, None, "UNKNOWN")]).to_mapping()))
    assert main(["v.mp4", "--gameplay-analysis", str(path), "--list"]) == 0
    out = capsys.readouterr().out
    assert "REMOVE " in out and "REMOVE_TO_EOF" in out and "IN_PROGRESS_AT_EOF" in out


def test_raw_candidates_ignore_unknown_marker_kinds():
    ev = evidence(markers=[GameplayMarker(101.0, "QUEUE_SCREEN")])
    assert [s for _, s, _ in raw_candidates(ev, S)] == [CandidateSource.RESULT_ANCHOR]


def test_boundary_import_does_not_initialize_qt_ml_or_asr():
    import sys

    command = [sys.executable, "-c", "import sys, analysis.boundaries.analyzer, analysis.boundaries.__main__;"
               "assert not any(m in sys.modules for m in ['PySide6.QtWidgets', 'torch', 'faster_whisper'])"]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr


def test_transcript_gaps_are_never_negative_at_touching_segments():
    cands = generate_candidates(evidence(transcript=[seg(96.0, 100.05, "fine"), seg(100.05, 103.0, "poi")]), S)
    anchor = by_source(cands, CandidateSource.RESULT_ANCHOR)[0].features
    assert anchor["transcript_gap_before_s"] == 0.0  # was -0.05 before the clamp
    assert anchor["transcript_gap_after_s"] == pytest.approx(0.05)
