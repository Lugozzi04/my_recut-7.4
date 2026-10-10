"""keep-games wins: completeness, GameKeepPolicy (keep islands of complete WINs), smart boundaries, composition."""
from __future__ import annotations

from contextlib import contextmanager
import json
from unittest.mock import patch

import numpy as np
import pytest

from analysis.boundaries.analyzer import BoundaryAnalyzer, BoundarySettings
from analysis.boundaries.audio import AudioEvidence, complement
from analysis.boundaries.keep_policy import GameKeepPolicy
from analysis.boundaries.gameplay import GameplayTimeline
from analysis.boundaries.models import (BoundaryContext, BoundaryDecision, BoundaryType, EvidenceStatus,
                                        GameDisposition, GameTermination, TimeRange)
from analysis.boundaries.resolvers import ExactBoundaryResolver, ResolverOptions, create_resolver
from analysis.classic import compute_classic_cuts
from analysis.cut_engine import Segment, invert_to_keeps
from analysis.gameplay.models import GameplayAnalysisResult, GameResult, GameSegment
from analysis.gameplay_editing import EDITORIAL_WINDOWS, GameplayEditingService, GameplayEditSettings
from tests.test_boundaries import FakeEngine, seg

OK = EvidenceStatus("ok", "test")
OFF = EvidenceStatus("unavailable", "none")


def game(index, start, end, result):
    return GameSegment(f"g{index}", index, start, end, GameResult(result))


def analysis(games, *, duration=1000.0, board=lambda t: 0.5):
    """Games plus 1 FPS monitor board scores from `board(t)` (state-aware metadata shape)."""
    rows = [{"timestamp": float(t), "board_score": float(board(t))} for t in range(int(duration))]
    return GameplayAnalysisResult("v.mp4", duration, [], games, "test",
                                  settings={"monitor": {"board_high": 0.38, "board_observations": 2},
                                            "maximum_game_duration_s": 1800.0},
                                  metadata={"monitor_evidence": rows, "board_reacquisitions": [],
                                            "result_windows": []})


def board_during(*spans):
    return lambda t: 0.5 if any(a <= t <= b for a, b in spans) else 0.05


def infos(result):
    timeline = GameplayTimeline(result, policy="wins")
    return {g.index: timeline.info(g) for g in timeline.games}


# ---------- 30. completeness / keep policy ----------

def test_wins_policy_dispositions():
    result = analysis([game(1, None, 100, "WIN"), game(2, 150, 300, "WIN"), game(3, 350, 500, "LOSS"),
                       game(4, None, 600, "LOSS"), game(5, 650, None, "UNKNOWN"), game(6, 750, 800, "UNKNOWN"),
                       game(7, 850, None, "UNKNOWN")],
                      board=board_during((0, 95), (155, 295), (355, 495), (560, 595), (655, 700), (755, 795), (855, 999)))
    i = infos(result)
    assert (i[1].termination, i[1].disposition) == (GameTermination.TRUNCATED_AT_START, GameDisposition.REMOVE_FROM_START)
    assert (i[2].termination, i[2].disposition) == (GameTermination.COMPLETE, GameDisposition.KEEP)
    assert (i[3].termination, i[3].disposition) == (GameTermination.COMPLETE, GameDisposition.REMOVE)
    assert (i[4].termination, i[4].disposition) == (GameTermination.MISSING_START, GameDisposition.REMOVE)
    assert (i[5].termination, i[5].disposition) == (GameTermination.MISSING_RESULT, GameDisposition.REMOVE)
    assert (i[6].termination, i[6].disposition) == (GameTermination.AMBIGUOUS_RESULT, GameDisposition.REMOVE)
    assert (i[7].termination, i[7].disposition) == (GameTermination.IN_PROGRESS_AT_EOF, GameDisposition.REMOVE_TO_EOF)


def test_win_without_start_is_never_kept_even_without_truncation_evidence():
    result = analysis([game(1, None, 300, "WIN"), game(2, 400, 700, "WIN")], board=board_during((100, 290), (405, 695)))
    first = infos(result)[1]
    assert first.termination == GameTermination.MISSING_START and first.disposition == GameDisposition.REMOVE
    region = GameplayTimeline(result, policy="wins").removal_anchors(result.games[0])
    assert (region.start_kind, region.start) == ("estimated_board_start", 90.0)  # board at 100 minus 10 s margin


def test_last_game_without_result_or_eof_evidence_is_removed_to_eof_under_wins():
    result = analysis([game(1, 100, 300, "WIN"), game(2, 400, None, "UNKNOWN")], board=board_during((105, 295), (405, 600)))
    i = infos(result)[2]
    assert (i.termination, i.disposition) == (GameTermination.MISSING_RESULT, GameDisposition.REMOVE_TO_EOF)


def test_internal_missing_end_is_estimated_from_board_disappearance():
    result = analysis([game(1, 100, None, "UNKNOWN"), game(2, 500, 700, "WIN")], board=board_during((105, 380), (505, 695)))
    region = GameplayTimeline(result, policy="wins").removal_anchors(result.games[0])
    assert (region.end_kind, region.end) == ("estimated_board_end", 385.0)


def test_legacy_policy_is_unchanged():
    result = analysis([game(1, None, 100, "WIN"), game(2, 150, 300, "LOSS"), game(3, 350, 500, "UNKNOWN")],
                      board=board_during((0, 95)))
    timeline = GameplayTimeline(result)
    assert [timeline.info(g).disposition for g in timeline.games] == [
        GameDisposition.KEEP, GameDisposition.REMOVE, GameDisposition.KEEP]
    with pytest.raises(ValueError):
        GameplayTimeline(result, policy="everything")


# ---------- GameKeepPolicy (pure) ----------

def decision(kind, t, index=0, resolver="stub"):
    return BoundaryDecision(kind, t, index, 0.9, resolver, "", resolver)


def keep_policy(result, decisions):
    policy = GameKeepPolicy(GameplayTimeline(result, policy="wins"))
    return policy.keeps(decisions)


def islands(keeps):
    return [(s.start, s.end) for s in GameKeepPolicy.segments(keeps)]


def test_exact_keeps_are_literally_vs_to_result_of_each_complete_win():
    result = analysis([game(1, 100, 300, "WIN"), game(2, 400, 600, "LOSS"), game(3, 700, 900, "WIN")])
    keeps = keep_policy(result, {})
    assert islands(keeps) == [(100.0, 300.0), (700.0, 900.0)]
    assert [k.game.index for k in keeps] == [1, 3]
    cuts = GameKeepPolicy.cuts(keeps, 1000.0)
    assert [(c.start, c.end) for c in cuts] == [(0.0, 100.0), (300.0, 700.0), (900.0, 1000.0)]


def test_smart_boundaries_extend_a_kept_win():
    result = analysis([game(1, 100, 300, "WIN")])
    keeps = keep_policy(result, {1: {BoundaryType.START: decision(BoundaryType.START, 80.0),
                                     BoundaryType.END: decision(BoundaryType.END, 330.0)}})
    assert islands(keeps) == [(80.0, 330.0)] and keeps[0].start_source == keeps[0].end_source == "decision"
    assert keeps[0].adjustments == []


def test_coverage_and_isolation_clamps():
    result = analysis([game(1, 100, 200, "LOSS"), game(2, 300, 500, "WIN"), game(3, 600, 700, "LOSS")])
    inside = keep_policy(result, {2: {BoundaryType.START: decision(BoundaryType.START, 320.0),
                                      BoundaryType.END: decision(BoundaryType.END, 480.0)}})[0]
    assert (inside.segment.start, inside.segment.end) == (300.0, 500.0)  # the whole WIN always survives
    assert sum(a.startswith("coverage") for a in inside.adjustments) == 2
    into_losses = keep_policy(result, {2: {BoundaryType.START: decision(BoundaryType.START, 150.0),
                                           BoundaryType.END: decision(BoundaryType.END, 650.0)}})[0]
    assert (into_losses.segment.start, into_losses.segment.end) == (200.0, 600.0)  # no LOSS gameplay leaks in
    assert sum(a.startswith("isolation") for a in into_losses.adjustments) == 2


def test_overlapping_islands_of_consecutive_wins_merge():
    result = analysis([game(1, 100, 300, "WIN"), game(2, 340, 500, "WIN")])
    keeps = keep_policy(result, {1: {BoundaryType.END: decision(BoundaryType.END, 330.0)},
                                 2: {BoundaryType.START: decision(BoundaryType.START, 320.0)}})
    assert islands(keeps) == [(100.0, 500.0)]


def test_no_complete_win_means_nothing_is_kept():
    result = analysis([game(1, 100, 300, "LOSS"), game(2, None, 500, "WIN")], board=board_during((105, 295), (400, 495)))
    keeps = keep_policy(result, {})
    assert keeps == [] and [(c.start, c.end) for c in GameKeepPolicy.cuts(keeps, 1000.0)] == [(0.0, 1000.0)]


# ---------- service with real BoundaryAnalyzer (local evidence mocked) ----------

@contextmanager
def local_evidence(speech_gaps=()):
    """Every window: silences at the given absolute ranges (clipped to the window)."""
    def audio(video, window, settings, **kwargs):
        silences = [TimeRange(max(a, window.start), min(b, window.end)) for a, b in speech_gaps
                    if b > window.start and a < window.end]
        return AudioEvidence(OK, silences, complement(window, silences), None)

    with patch("analysis.boundaries.analyzer.analyze_audio_window", side_effect=audio), \
         patch("analysis.boundaries.analyzer.analyze_visual_window", return_value=(OFF, [])):
        yield


class ScriptedEngine(FakeEngine):
    """Transcript per window from a fixed absolute script."""

    def __init__(self, script):
        super().__init__()
        self.script = script

    def transcribe(self, video, windows, *, cancellation=None):
        self.calls.append(list(windows))
        return OK, [[seg(a, b, text) for a, b, text in self.script if a >= w.start and b <= w.end] for w in windows]


class PickResolver:
    """Stands in for a semantic resolver: picks the shown candidate closest to a target time."""

    name = "stub"

    def __init__(self, targets):
        self.targets = targets

    def resolve(self, context: BoundaryContext) -> BoundaryDecision:
        from analysis.boundaries.shortlist import shortlist

        target = self.targets.get((context.game.index, context.boundary_type))
        if target is None:
            return ExactBoundaryResolver().resolve(context)
        shown = shortlist(context)
        index = min(shown, key=lambda i: abs(context.candidates[i].timestamp - target))
        return BoundaryDecision(context.boundary_type, context.candidates[index].timestamp, index, 0.8, self.name, "", self.name)


def service(resolver, engine):
    settings = GameplayEditSettings()
    analyzer = BoundaryAnalyzer(BoundarySettings(windows=EDITORIAL_WINDOWS, keep_policy="wins"), transcription=engine)
    return GameplayEditingService(settings, boundary_analyzer=analyzer, resolver=resolver)


def final_keeps(edit, duration=1000.0):
    return [(k.start, k.end) for k in invert_to_keeps(duration, edit.gameplay_cuts, min_keep=0.0)]


def test_regression_intro_before_a_loss_is_removed():
    """Observed bug: VIDEO START -> chatter -> LOSS -> WIN kept the chatter (cut policy only cut the LOSS)."""
    result = analysis([game(1, 100, 500, "LOSS"), game(2, 550, 900, "WIN")])
    with local_evidence():
        edit = service(ExactBoundaryResolver(), ScriptedEngine([])).run("v.mp4", analysis=result)
    assert final_keeps(edit) == [(550.0, 900.0)]  # old policy kept [(0, 100), (500, 1000)]
    assert [(c.start, c.end) for c in edit.gameplay_cuts] == [(0.0, 550.0), (900.0, 1000.0)]


def test_complete_win_only_keeps_only_that_win():
    result = analysis([game(1, 200, 600, "WIN")])
    with local_evidence():
        edit = service(ExactBoundaryResolver(), ScriptedEngine([])).run("v.mp4", analysis=result)
    assert final_keeps(edit) == [(200.0, 600.0)]


def test_chatter_after_a_win_is_kept_only_when_the_smart_end_includes_it():
    result = analysis([game(1, 100, 300, "WIN"), game(2, 450, 700, "LOSS")])
    script = [(301, 310, "grande vittoria, bella giocata"), (314, 330, "in generale questo mazzo è fortissimo"),
              (380, 395, "ok vediamo la prossima")]
    gaps = [(310, 314), (330, 380)]
    with local_evidence(speech_gaps=gaps):
        including = service(PickResolver({(1, BoundaryType.END): 355.0}), ScriptedEngine(script)).run("v.mp4", analysis=result)
        excluding = service(PickResolver({(1, BoundaryType.END): 312.0}), ScriptedEngine(script)).run("v.mp4", analysis=result)
    assert final_keeps(including)[0][0] == 100.0 and 330.0 <= final_keeps(including)[0][1] <= 380.0
    assert final_keeps(excluding)[0][0] == 100.0 and 310.0 <= final_keeps(excluding)[0][1] <= 314.0
    for edit in (including, excluding):  # nothing of the LOSS or of the transition survives
        assert all(end <= 380.0 for _, end in final_keeps(edit))


def test_partial_initial_win_is_removed_and_its_reaction_is_not_kept():
    """VIDEO START mid-game -> WIN -> reaction -> general talk -> complete WIN."""
    # Partial game ends at 250; the complete WIN's VS is at 300 (START window = 255..310, clipped at 250).
    result = analysis([game(1, None, 250, "WIN"), game(2, 300, 600, "WIN")], board=board_during((0, 245), (305, 595)))
    script = [(251, 256, "ha ha vinta, che giocata!"), (259, 275, "comunque questo mazzo in generale è molto forte."),
              (277, 288, "le carte chiave della lista sono queste."), (291, 299, "dai, vediamo contro chi giochiamo.")]
    engine = ScriptedEngine(script)
    with local_evidence(speech_gaps=[(256, 259), (275, 277), (288, 291)]):
        smart = service(PickResolver({(2, BoundaryType.START): 257.5}), engine).run("v.mp4", analysis=result)
        exact = service(ExactBoundaryResolver(), ScriptedEngine(script)).run("v.mp4", analysis=result)
    info = smart.timeline.info(result.games[0])
    assert info.disposition == GameDisposition.REMOVE_FROM_START and list(smart.contexts) == [2]
    start, end = final_keeps(smart)[0]
    assert 256.0 <= start <= 259.0 and end == 600.0  # general talk owned by the next WIN's intro is kept
    assert final_keeps(exact) == [(300.0, 600.0)]  # exact: nothing before the VS of the complete WIN
    assert all(s >= 250.0 for s, _ in final_keeps(smart))  # the partial game and its reaction never survive


def test_complete_win_then_partial_game_at_eof_keeps_only_the_win():
    result = analysis([game(1, 100, 400, "WIN"), game(2, 500, None, "UNKNOWN")], board=board_during((105, 395), (505, 999)))
    script = [(405, 430, "bella vittoria, il mazzo funziona."), (472, 480, "ok entriamo in coda"),
              (481, 499, "vediamo chi troviamo")]
    with local_evidence(speech_gaps=[(430, 472), (480, 481)]):
        edit = service(PickResolver({(1, BoundaryType.END): 450.0}), ScriptedEngine(script)).run("v.mp4", analysis=result)
    keeps = final_keeps(edit)
    assert len(keeps) == 1 and keeps[0][0] == 100.0 and 430.0 <= keeps[0][1] <= 472.0
    assert edit.timeline.info(result.games[1]).disposition == GameDisposition.REMOVE_TO_EOF
    assert list(edit.contexts) == [1]  # the incomplete game needs no boundary work


def test_two_wins_separated_by_a_loss_are_two_islands_and_contexts_reference_valid_candidates():
    result = analysis([game(1, 100, 300, "WIN"), game(2, 400, 600, "LOSS"), game(3, 700, 900, "WIN")])
    engine = ScriptedEngine([])
    with local_evidence(speech_gaps=[(310, 330), (640, 660)]):
        edit = service(ExactBoundaryResolver(), engine).run("v.mp4", analysis=result)
    assert final_keeps(edit) == [(100.0, 300.0), (700.0, 900.0)]
    assert sorted(edit.contexts) == [1, 3] and len(engine.calls) == 2
    for index, per_game in edit.decisions.items():
        assert set(per_game) == {BoundaryType.START, BoundaryType.END}
        for kind, chosen in per_game.items():
            context = edit.contexts[index][kind]
            assert chosen.candidate_index is not None
            assert context.candidates[chosen.candidate_index].timestamp == chosen.timestamp
    report = json.loads(json.dumps(edit.report(), allow_nan=False))
    assert [(g["complete"], g["publishable"]) for g in report["games"]] == [(True, True), (True, False), (True, True)]
    assert report["games"][0]["smart"]["start"] == 100.0 and report["games"][0]["raw_anchors"]["start"] == 100.0
    assert report["games"][0]["boundaries"]["START"]["candidate"]["sources"][0] == "GAME_START_ANCHOR"
    assert report["gameplay_keeps"] == [{"start": 100.0, "end": 300.0}, {"start": 700.0, "end": 900.0}]


def test_agent_failure_falls_back_to_exact_keep_boundaries(tmp_path):
    result = analysis([game(1, 100, 300, "WIN")])
    resolver = create_resolver("codex", ResolverOptions(executable=str(tmp_path / "missing"), cache_directory=tmp_path))
    with local_evidence(), patch("analysis.boundaries.agents.shutil.which", return_value=None):
        edit = service(resolver, ScriptedEngine([])).run("v.mp4", analysis=result)
    chosen = edit.decisions[1]
    assert all(d.fallback and d.resolver == "exact" and d.metadata["error_code"] == "cli_missing" for d in chosen.values())
    assert final_keeps(edit) == [(100.0, 300.0)]
    assert edit.timings["resolver_fallbacks"] == 2.0 and edit.timings["agent_calls"] == 0.0


def test_agent_cache_hit_avoids_subprocess(tmp_path):
    from tests.test_boundary_agents import FakePopen, claude_stdout

    result = analysis([game(1, 100, 300, "WIN")])
    cli = tmp_path / "claude.exe"
    cli.write_text("")
    options = ResolverOptions(executable=str(cli), cache_directory=tmp_path / "cache")

    def first_shown(prompt):  # answer with the first candidate listed in the prompt
        index = int(prompt.split("\n#", 1)[1].split(" ", 1)[0])
        return claude_stdout({"selected_candidate": index, "confidence": 0.7, "reason": "", "segment_labels": []})

    FakePopen.calls, FakePopen.behaviour = [], {"stdout_fn": first_shown}
    with local_evidence(), patch("analysis.boundaries.agents.subprocess.Popen", FakePopen):
        first = service(create_resolver("claude", options), ScriptedEngine([])).run("v.mp4", analysis=result)
        calls = len(FakePopen.calls)
        second = service(create_resolver("claude", options), ScriptedEngine([])).run("v.mp4", analysis=result)
    assert calls == 2 and len(FakePopen.calls) == 2  # START + END once; the second run is fully cached
    assert first.timings["agent_calls"] == 2.0 and second.timings["agent_cache_hits"] == 2.0
    assert first.timings["resolver_fallbacks"] == 0.0


def test_v1_and_v2_agent_decisions_are_never_reused_for_v3(tmp_path):
    from analysis.boundaries import agents

    result = analysis([game(1, 100, 300, "WIN")])
    with local_evidence():
        edit = service(ExactBoundaryResolver(), ScriptedEngine([])).run("v.mp4", analysis=result)
    context = edit.contexts[1][BoundaryType.START]
    resolver = create_resolver("claude", ResolverOptions(cache_directory=tmp_path))
    key_v3 = resolver.cache_key(agents.context_digest(context))
    with patch.object(agents, "PROMPT_VERSION", "boundary_agent_prompt_v2"):
        key_v2 = resolver.cache_key(agents.context_digest(context))
    assert agents.PROMPT_VERSION == "boundary_agent_prompt_v3" and key_v2 != key_v3


# ---------- 32/36. composition with classic and manual cuts ----------

HOP = 0.1
CFG = {"intensity": 50, "threshold_pct": 6, "pre_pad_s": 0.0, "post_pad_s": 0.0, "min_cut_s": 0.0}


def rms_with_silences(duration, silences):
    rms = np.full(int(duration / HOP), 0.2, dtype=np.float32)
    for a, b in silences:
        rms[int(a / HOP):int(b / HOP)] = 0.0
    return rms


def compose(gameplay=(), manual=(), suppressed=(), silences=((20, 25), (60, 64))):
    return compute_classic_cuts(rms_with_silences(100, silences), 100.0, HOP, CFG, manual_cuts=list(manual),
                                suppressed_cuts=list(suppressed), threshold_amp=0.01, gameplay_cuts=list(gameplay))


def spans(segments):
    return [(round(s.start, 1), round(s.end, 1)) for s in segments]


def test_gameplay_disabled_is_exact_legacy_behaviour():
    legacy = compute_classic_cuts(rms_with_silences(100, [(20, 25), (60, 64)]), 100.0, HOP, CFG, threshold_amp=0.01)
    assert spans(compose()[0]) == spans(legacy[0]) and spans(compose()[1]) == spans(legacy[1])
    assert spans(compose()[0])  # classic cuts exist without gameplay


def test_classic_inside_overlapping_and_disjoint_gameplay_cuts():
    classic = spans(compose()[0])
    inside = compose(gameplay=[Segment(15, 30)])[0]
    assert all(not (15 < a < 30 and 15 < b < 30) or (a, b) == (15, 30) for a, b in spans(inside))
    assert any(a <= 15 and b >= 30 for a, b in spans(inside))  # classic inside gameplay is absorbed
    overlap = compose(gameplay=[Segment(23, 40)])[0]
    assert any(a <= classic[0][0] + 0.01 and b >= 40 for a, b in spans(overlap))  # merged across the boundary
    disjoint = compose(gameplay=[Segment(80, 90)])[0]
    assert classic[1] in spans(disjoint) and (80.0, 90.0) in spans(disjoint)


def test_classic_silence_cuts_still_apply_inside_kept_wins():
    cuts, keeps = compose(gameplay=[Segment(0, 10)], silences=((30, 34), (70, 73)))
    assert (0.0, 10.0) in spans(cuts)
    assert any(29 <= a <= 31 and 33 <= b <= 35 for a, b in spans(cuts))  # dead air in the published WIN removed
    assert spans(keeps)[0][0] == 10.0


def test_manual_and_suppressed_cuts_have_the_last_word():
    with_manual = compose(gameplay=[Segment(40, 50)], manual=[Segment(90, 95)])[0]
    assert (40.0, 50.0) in spans(with_manual) and (90.0, 95.0) in spans(with_manual)
    restored = compose(gameplay=[Segment(40, 50)], suppressed=[Segment(44, 46)])[0]
    assert (40.0, 44.0) in spans(restored) and (46.0, 50.0) in spans(restored)
    forced = compose(gameplay=[Segment(40, 50)], suppressed=[Segment(40, 50)], manual=[Segment(42, 43)])[0]
    assert (42.0, 43.0) in spans(forced) and (40.0, 50.0) not in spans(forced)


def test_gameplay_cuts_apply_when_classic_intensity_is_off():
    cuts, keeps = compute_classic_cuts(np.array([]), 100.0, HOP, {**CFG, "intensity": 0},
                                       gameplay_cuts=[Segment(10, 20)], manual_cuts=[Segment(50, 55)])
    assert spans(cuts) == [(10.0, 20.0), (50.0, 55.0)]
    assert spans(keeps) == [(0.0, 10.0), (20.0, 50.0), (55.0, 100.0)]
    assert spans(invert_to_keeps(100.0, cuts, min_keep=0.0)) == spans(keeps)


# ---------- settings ----------

def test_gameplay_settings_round_trip_and_validation():
    settings = GameplayEditSettings(boundary_mode="codex", resolver=ResolverOptions(timeout_s=60.0, model="m"))
    again = GameplayEditSettings.from_mapping(json.loads(json.dumps(settings.to_mapping())))
    assert again.boundary_mode == "codex" and again.resolver.timeout_s == 60.0 and again.resolver.model == "m"
    assert again.boundary.windows == EDITORIAL_WINDOWS and again.boundary.keep_policy == "wins"
    assert settings.sends_remote_context and not GameplayEditSettings().sends_remote_context
    for bad in ({"game": "chess"}, {"keep_games": "all"}, {"boundary_mode": "gpt"}):
        with pytest.raises(ValueError):
            GameplayEditSettings(**bad)


def test_old_contexts_without_neighbors_still_load():
    from tests.test_boundaries import gameplay as legacy_gameplay, game as legacy_game, run_analyzer

    result, _ = run_analyzer(legacy_gameplay([legacy_game(1, 10, 100, "LOSS")]), 1)
    raw = result.contexts[BoundaryType.END].to_mapping()
    raw.pop("neighbors")
    assert BoundaryContext.from_mapping(raw).neighbors == {}


def keep_composition(silences, keep=(30.0, 70.0)):
    islands = [Segment(*keep)]
    cuts = [Segment(s.start, s.end) for s in invert_to_keeps(100.0, islands, min_keep=0.0)]  # = GameKeepPolicy.cuts
    return compose(gameplay=cuts, silences=silences)


def test_classic_silence_inside_a_kept_win_is_removed():
    _cuts, keeps = keep_composition(silences=((45, 50),))
    (first_start, first_end), (second_start, second_end) = spans(keeps)
    assert (first_start, second_end) == (30.0, 70.0)  # island edges untouched
    assert 45.0 <= first_end <= 46.0 and 49.0 <= second_start <= 50.0  # dead air inside the WIN is still cut


def test_classic_silence_outside_the_keeps_is_irrelevant():
    with_outside, keeps_outside = keep_composition(silences=((10, 15), (80, 90)))
    assert spans(keeps_outside) == [(30.0, 70.0)]  # classic analysis cannot bring back content outside a keep
    assert spans(with_outside) == [(0.0, 30.0), (70.0, 100.0)]
