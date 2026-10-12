"""END policy of KEPT games (tight / balanced / extended): editorial relevance over sentence completeness.

Every case states an ACCEPTABLE END RANGE (seconds after the result anchor) and the editorial reason, instead of
one arbitrary timestamp. Segment labels play the agent's role (the agent labels segments; the policy decides);
no test calls Claude or Codex.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from analysis.boundaries.agents import (END_PROMPT_VERSION, PROMPT_VERSION, AgentCliResolver, apply_end_policy,
                                        build_prompt, prompt_version)
from analysis.boundaries.candidates import FEATURE_NAMES, CandidateSettings, Evidence, generate_candidates
from analysis.boundaries.end_policy import END_LABELS, choose_end, relevant_until
from analysis.boundaries.models import (BoundaryContext, BoundaryDecision, BoundaryType, CandidateSource,
                                        EvidenceStatus, GameDisposition, GameInfo, GameTermination, TimeRange,
                                        TranscriptSegment, TranscriptWord)
from analysis.boundaries.resolvers import ResolverOptions, parse_agent_response
from analysis.boundaries.shortlist import eligible, shortlist

ANCHOR = 100.0
MODES = ("tight", "balanced", "extended")
FIXTURE = Path(__file__).parent / "fixtures" / "boundaries" / "videoprova_kept_end_contexts.json"


def _words(start: float, end: float, text: str) -> tuple[TranscriptWord, ...]:
    tokens = text.split()
    step = (end - start) / len(tokens)
    return tuple(TranscriptWord(start + i * step, start + (i + 1) * step, tok) for i, tok in enumerate(tokens))


def make_context(segments: list[tuple[float, float, str]], *, window_end: float = 160.0) -> BoundaryContext:
    """END context of a kept WIN whose result is at ANCHOR; times in segments are offsets from ANCHOR."""
    segs = [TranscriptSegment(ANCHOR + s, ANCHOR + e, text, words=_words(ANCHOR + s, ANCHOR + e, text))
            for s, e, text in segments]
    window = TimeRange(ANCHOR - 5.0, ANCHOR + (window_end - 100.0))
    speech = [TimeRange(s.start, s.end) for s in segs]
    silences, cursor = [], window.start
    for s in segs:
        if s.start - cursor >= 0.3:
            silences.append(TimeRange(cursor, s.start))
        cursor = max(cursor, s.end)
    if window.end - cursor >= 0.3:
        silences.append(TimeRange(cursor, window.end))
    anchors: dict[str, float | None] = {"game_start": 10.0, "game_end": ANCHOR, "previous_game_end": None,
                                        "next_game_start": None, "end_of_file": None}
    ok, off = EvidenceStatus("ok"), EvidenceStatus("disabled")
    ev = Evidence(BoundaryType.END, window, ANCHOR, anchors, ok, silences, speech, ok, segs, off, [], [])
    candidates = generate_candidates(ev, CandidateSettings())
    game = GameInfo("g", 1, 10.0, ANCHOR, "WIN", GameTermination.COMPLETE, GameDisposition.KEEP, "complete WIN")
    return BoundaryContext(BoundaryType.END, {"duration": 1000.0}, game, anchors, window, (), ok, segs, ok, silences,
                           speech, off, [], [], candidates, list(FEATURE_NAMES),
                           {"anchor": ANCHOR, "anchor_kind": "game_end"})


def end_offset(context: BoundaryContext, labels: list[str], mode: str, agent_offset: float | None = None) -> float:
    agent = None
    if agent_offset is not None:
        agent = min(range(len(context.candidates)),
                    key=lambda i: abs(context.candidates[i].timestamp - (ANCHOR + agent_offset)))
    index, _ = choose_end(context, labels, agent, eligible(context), mode)
    assert index is not None
    return context.candidates[index].timestamp - ANCHOR


# (name, segments, labels, {mode: (lo, hi)}, editorial reason). The agent's own pick is "keep everything".
CASES: list[tuple[str, list[tuple[float, float, str]], list[str], dict[str, tuple[float, float]], str]] = [
    ("win_reaction_then_chat_reply",
     [(0.3, 2.0, "GG vinta finalmente"), (3.0, 10.0, "grazie mille Marco per il sub ci vediamo dopo ragazzi"),
      (11.0, 18.0, "allora ciao a tutti quelli che sono appena arrivati in live")],
     ["WIN_REACTION", "CHAT_INTERACTION", "CHAT_INTERACTION"],
     dict.fromkeys(MODES, (2.0, 3.0)), "keep the reaction, drop the chat reply that follows"),
    ("specific_recap_then_personal_topic",
     [(0.5, 2.0, "Vinta raga"), (2.5, 10.0, "il drago al turno sei ha chiuso la partita perche lui non aveva risposte"),
      (10.3, 25.0, "comunque ieri sono andato a cena fuori e il ristorante era veramente buono e poi siamo andati")],
     ["WIN_REACTION", "GAME_SPECIFIC_RECAP", "UNRELATED_COMMENTARY"],
     dict.fromkeys(MODES, (10.0, 11.4)), "the recap is short and specific; the dinner story is cut, mid-sentence if needed"),
    ("chat_question_pertinent_to_the_play",
     [(0.3, 2.0, "Ce l'abbiamo fatta"), (3.0, 8.0, "mi chiedono perche non ho giocato il drago al turno sei"),
      (8.5, 16.0, "perche lui aveva il removal in mano e quindi conveniva aspettare un turno"),
      (17.0, 25.0, "ok mettiamo in coda la prossima partita vediamo contro chi")],
     ["WIN_REACTION", "GAME_STRATEGY", "GAME_STRATEGY", "NEXT_GAME"],
     {"tight": (2.0, 3.0), "balanced": (16.0, 17.0), "extended": (16.0, 17.0)},
     "a chat question ABOUT this game's play is content: kept in balanced/extended, not in tight"),
    ("general_deck_comment_after_win",
     [(0.4, 2.0, "Che partita"), (3.0, 14.0, "questo mazzo secondo me e il migliore del meta adesso tutti dovrebbero giocarlo"),
      (15.0, 20.0, "andiamo avanti con la prossima")],
     ["WIN_REACTION", "GENERAL_DECK_TALK", "GENERAL_DECK_TALK"],
     dict.fromkeys(MODES, (2.0, 3.0)), "generic deck talk is not this game: cut after the reaction"),
    ("irrelevant_sentence_still_running",
     [(0.2, 1.8, "Vinta"), (1.85, 30.0, " ".join(["e poi stavo dicendo che la settimana prossima"] * 6))],
     ["WIN_REACTION", "UNRELATED_COMMENTARY"],
     dict.fromkeys(MODES, (1.8, 3.5)), "no pause for 28 s: cut inside the irrelevant sentence instead of waiting"),
    ("no_natural_pause",
     [(0.0, 4.0, "si si si vinta vinta vinta"), (4.0, 25.0, " ".join(["comunque grazie a tutti per le bit"] * 4))],
     ["WIN_REACTION", "CHAT_INTERACTION"],
     dict.fromkeys(MODES, (3.9, 5.5)), "speech never stops: end right after the reaction at a word boundary"),
    ("long_specific_conclusion",
     [(0.3, 2.0, "Vinta"), (2.5, 10.5, "il punto chiave e stato tenere il removal per il suo drago"),
      (10.8, 18.8, "se lo usavo prima sul servitore piccolo poi non avevo risposta"),
      (19.1, 27.1, "e infatti al turno otto lui ha giocato esattamente il drago"),
      (27.4, 35.4, "quindi la lezione e contro questo mazzo tieni sempre il removal"),
      (36.0, 42.0, "bene prossima partita")],
     ["WIN_REACTION", "GAME_SPECIFIC_RECAP", "GAME_STRATEGY", "GAME_SPECIFIC_RECAP", "GAME_STRATEGY", "NEXT_GAME"],
     {"tight": (10.5, 10.8), "balanced": (27.1, 27.4), "extended": (35.4, 36.0)},
     "an important specific explanation: tight keeps one point, balanced ~25 s, extended all of it"),
    ("game_ended_without_reaction",
     [(6.0, 14.0, "allora ragazzi rimettiamo in coda e vediamo cosa troviamo adesso")],
     ["NEXT_GAME"],
     {"tight": (1.4, 6.0), "balanced": (1.9, 6.0), "extended": (1.9, 6.0)},
     "nothing to keep: a short hold on the result screen, before the next-game talk"),
]


@pytest.mark.parametrize("name,segments,labels,expected,reason", CASES, ids=[c[0] for c in CASES])
def test_end_policy_cases(name, segments, labels, expected, reason):
    context = make_context(segments)
    keep_all = segments[-1][1] + 0.2  # the agent kept everything (old behaviour on these windows)
    for mode in MODES:
        offset = end_offset(context, labels, mode, agent_offset=keep_all)
        lo, hi = expected[mode]
        assert lo - 1e-6 <= offset <= hi + 1e-6, f"{name} [{mode}] END +{offset:.2f}s not in [{lo}, {hi}]: {reason}"


def test_reaction_is_never_cut_in_any_mode():
    for name, segments, labels, _, _ in CASES:
        context = make_context(segments)
        reaction_end = max([ANCHOR + e for (s, e, _), lab in zip(segments, labels) if lab == "WIN_REACTION"],
                           default=ANCHOR)
        for mode in MODES:
            assert ANCHOR + end_offset(context, labels, mode, agent_offset=0.0) >= reaction_end - 0.05, (name, mode)


def test_modes_are_ordered_tight_to_extended():
    for _, segments, labels, _, _ in CASES:
        context = make_context(segments)
        ends = [end_offset(context, labels, mode) for mode in MODES]
        assert ends[0] <= ends[1] + 1e-6 <= ends[2] + 2e-6


def test_agent_pick_is_kept_when_reasonable():
    segments, labels = CASES[0][1], CASES[0][2]
    context = make_context(segments)
    assert end_offset(context, labels, "balanced", agent_offset=2.5) == pytest.approx(2.5, abs=0.3)


def test_irrelevant_block_is_not_kept_for_later_relevant_talk():
    context = make_context([(0.3, 2.0, "vinta"), (3.0, 9.0, "ciao Luca grazie del follow"),
                            (10.0, 14.0, "comunque il turno sette era letale")])
    plan = relevant_until(context, ["WIN_REACTION", "CHAT_INTERACTION", "GAME_SPECIFIC_RECAP"],
                          __import__("analysis.boundaries.end_policy", fromlist=["mode"]).mode("extended"))
    assert plan["stopped_by"] == "CHAT_INTERACTION" and plan["target"] == pytest.approx(ANCHOR + 2.0)


def test_word_gap_candidates_only_after_the_result_of_an_end():
    context = make_context([(-3.0, -0.5, "ultimo turno prima"), (0.5, 20.0, " ".join(["parole"] * 30))])
    gaps = [c.timestamp for c in context.candidates if CandidateSource.WORD_GAP in c.sources]
    assert gaps and min(gaps) > ANCHOR and all(b - a >= 0.99 for a, b in zip(gaps, gaps[1:]))


# ---------- real transcripts (videoprova, kept WINs) ----------

REAL_LABELS = {
    # S0-S1 are the last turn; "Lanter e fuori", the rant, "Dai!", "Ma quanto e rotto? Nerfatelo!" react to the
    # win; then a general nerf/deck opinion, a card opinion and the "road to legend" transition.
    "2": ["GAME_SPECIFIC_RECAP", "GAME_SPECIFIC_RECAP", "WIN_REACTION", "WIN_REACTION", "WIN_REACTION", "WIN_REACTION",
          "GENERAL_DECK_TALK", "GENERAL_DECK_TALK", "GENERAL_DECK_TALK", "GENERAL_DECK_TALK", "NEXT_GAME"],
    # the streamer is talking with chat about pets/skins/subs before and after the result: no reaction at all
    "4": ["CHAT_INTERACTION", "CHAT_INTERACTION", "UNRELATED_COMMENTARY", "UNRELATED_COMMENTARY",
          "UNRELATED_COMMENTARY", "UNRELATED_COMMENTARY", "UNRELATED_COMMENTARY"],
}
REAL_EXPECTED = {"2": (14.5, 15.3), "4": (1.4, 5.0)}  # seconds after the result


def real_contexts() -> tuple[dict[str, BoundaryContext], dict[str, float]]:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return ({k: BoundaryContext.from_mapping(v) for k, v in raw["contexts"].items()},
            {k: float(v) for k, v in raw["codex_v3_end"].items()})


@pytest.mark.parametrize("game", ["2", "4"])
def test_real_videoprova_end_is_more_concise(game):
    contexts, codex_v3 = real_contexts()
    context = contexts[game]
    anchor = float(context.settings["anchor"])
    agent = min(range(len(context.candidates)), key=lambda i: abs(context.candidates[i].timestamp - codex_v3[game]))
    for mode in MODES:
        index, trace = choose_end(context, REAL_LABELS[game], agent, eligible(context), mode)
        assert index is not None
        offset = context.candidates[index].timestamp - anchor
        lo, hi = REAL_EXPECTED[game]
        assert lo <= offset <= hi, (game, mode, offset)
        assert offset < codex_v3[game] - anchor - 10.0  # v3 kept 31.5 s / 18.9 s after the win
        assert trace["overrode_agent"]


# ---------- resolver integration (no CLI) ----------

class StubResolver(AgentCliResolver):
    name = "codex"
    executable_names = ("codex",)

    def __init__(self, answer: dict[str, Any], **options: Any) -> None:
        super().__init__(ResolverOptions(use_cache=False, **options))
        self.answer = answer
        self.prompts: list[str] = []
        self.schemas: list[dict[str, Any]] = []

    def call(self, prompt: str, schema: dict[str, Any] | None = None) -> tuple[Any, dict[str, Any]]:
        self.prompts.append(prompt)
        self.schemas.append(schema or {})
        return dict(self.answer), {}


def test_resolver_applies_end_policy_to_kept_end():
    contexts, codex_v3 = real_contexts()
    context = contexts["2"]
    agent = min(shortlist(context), key=lambda i: abs(context.candidates[i].timestamp - codex_v3["2"]))
    stub = StubResolver({"segment_labels": REAL_LABELS["2"], "selected_candidate": agent, "confidence": 0.8,
                         "reason": "keeps the deck critique"}, end_policy="tight")
    decision = stub.resolve(context)
    assert decision.metadata["prompt_version"] == END_PROMPT_VERSION and decision.metadata["end_policy"] == "tight"
    assert decision.metadata["end_policy_trace"]["overrode_agent"] and not decision.fallback
    assert decision.timestamp - float(context.settings["anchor"]) < 16.0
    prompt = stub.prompts[0]
    assert "MODE tight" in prompt and "CHAT_INTERACTION" in prompt and "Sentence completeness never outweighs" in prompt
    assert stub.schemas[0]["properties"]["segment_labels"]["items"]["enum"] == list(END_LABELS)


def test_start_and_removed_games_keep_the_v3_prompt_and_cache_key():
    contexts, _ = real_contexts()
    end = contexts["2"]
    start = BoundaryContext.from_mapping({**end.to_mapping(), "boundary_type": "START"})
    removed = BoundaryContext.from_mapping({**end.to_mapping(), "game": {**end.game.to_mapping(),
                                                                         "disposition": "REMOVE"}})
    assert prompt_version(start) == PROMPT_VERSION and prompt_version(removed) == PROMPT_VERSION
    assert prompt_version(end) == END_PROMPT_VERSION
    assert "MODE" not in build_prompt(start) and "WIN_REACTION" not in build_prompt(removed)
    stub = StubResolver({})
    assert stub.cache_key("d", PROMPT_VERSION) == StubResolver({}).cache_key("d")  # unchanged for v3 contexts
    assert stub.cache_key("d", END_PROMPT_VERSION, "tight") != stub.cache_key("d", END_PROMPT_VERSION, "balanced")


def test_policy_without_labels_keeps_the_agent_decision():
    contexts, _ = real_contexts()
    context = contexts["4"]
    decision = parse_agent_response(context, {"selected_candidate": eligible(context)[0], "confidence": 0.5})
    assert apply_end_policy(context, decision, "balanced") == decision


def test_end_labels_are_accepted_by_the_parser():
    contexts, _ = real_contexts()
    context = contexts["4"]
    decision = parse_agent_response(context, {"selected_candidate": eligible(context)[0], "confidence": 0.5,
                                               "segment_labels": list(END_LABELS)})
    assert isinstance(decision, BoundaryDecision) and decision.metadata["segment_labels"] == list(END_LABELS)


def test_resolver_options_default_is_balanced_and_round_trips():
    from analysis.gameplay_editing import GameplayEditSettings

    settings = GameplayEditSettings(boundary_mode="codex")
    assert settings.resolver.end_policy == "balanced"
    raw = settings.to_mapping()
    assert GameplayEditSettings.from_mapping(raw).resolver.end_policy == "balanced"
    old = {**raw, "resolver": {k: v for k, v in raw["resolver"].items() if k != "end_policy"}}
    assert GameplayEditSettings.from_mapping(old).resolver.end_policy == "balanced"  # older projects/jobs
