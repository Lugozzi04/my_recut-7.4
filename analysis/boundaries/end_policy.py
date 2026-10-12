"""Editorial END policy for KEPT games: relevance and concision over sentence completeness.

The agent labels every transcript segment of a kept game's END window with one of END_LABELS and picks a
candidate. This module turns those labels into the END the selected mode wants, deterministically:

  1. walk the speech that follows the result; keep it while it is relevant for the mode (the reaction to
     the win always; a game-specific recap / strategy within the mode's budget), stop at the first
     irrelevant block (chat small talk, personal or unrelated talk, generic deck/stream talk, the next
     game) - irrelevant speech is never kept because something relevant comes after it;
  2. target = end of the last relevant segment (never before the reaction, never less than a short hold
     on the result screen);
  3. score every eligible candidate: seconds of irrelevant speech it would keep (weighted by the mode),
     relevant speech it would cut (weighted much more), and how natural the cut is (pause/sentence edge
     best, a word gap inside speech acceptable, inside a word worst). A word gap close to the target beats
     a pause 15 s later: cutting an irrelevant sentence short is better than keeping it;
  4. the agent's own pick is kept unless the policy pick is clearly better (cost margin).

Modes change the END CHOICE (what counts as relevant, budgets, weights), never subtract fixed seconds.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .models import BoundaryContext, CandidateSource

END_POLICY_VERSION = "end_policy_v1"
END_LABELS = ("WIN_REACTION", "GAME_SPECIFIC_RECAP", "GAME_STRATEGY", "GENERAL_DECK_TALK", "CHAT_INTERACTION",
              "UNRELATED_COMMENTARY", "NEXT_GAME", "UNCERTAIN")
END_MODES = ("tight", "balanced", "extended")
DEFAULT_END_MODE = "balanced"
RELEVANT = {"WIN_REACTION", "GAME_SPECIFIC_RECAP", "GAME_STRATEGY"}


@dataclass(frozen=True)
class EndMode:
    name: str
    keep: frozenset[str]  # labels kept after the result (WIN_REACTION is always kept)
    budget_s: float  # max seconds of recap/strategy speech kept after the reaction
    irrelevant_cost: float  # cost per second of irrelevant speech kept
    lost_cost: float = 4.0  # cost per second of relevant speech cut
    min_hold_s: float = 2.0  # minimum time on the result screen
    override_margin: float = 1.0  # keep the agent's pick unless the policy pick saves more than this


MODES = {
    "tight": EndMode("tight", frozenset({"WIN_REACTION", "GAME_SPECIFIC_RECAP"}), 10.0, 1.6, min_hold_s=1.5),
    "balanced": EndMode("balanced", frozenset({"WIN_REACTION", "GAME_SPECIFIC_RECAP", "GAME_STRATEGY"}), 25.0, 1.1),
    "extended": EndMode("extended", frozenset({"WIN_REACTION", "GAME_SPECIFIC_RECAP", "GAME_STRATEGY"}), 60.0, 0.7),
}

MODE_PROMPT = {
    "tight": "MODE tight: keep the reaction to the win and at most one essential sentence that explains the play or "
             "the result. Everything else after the result is removed.",
    "balanced": "MODE balanced: keep the reaction to the win and a SHORT recap or explanation specific to this game "
                "(about 25 s at most). Stop at the first irrelevant topic.",
    "extended": "MODE extended: keep the reaction and a longer explanation specific to this game (its plays, "
                "decisions, the opponent's deck in this game). Still remove chat small talk, unrelated talk and "
                "the transition to the next game.",
}

LABEL_GUIDE = [
    "WIN_REACTION: the immediate reaction to THIS game's result (joy, relief, 'GG', 'it's over', surprise).",
    "GAME_SPECIFIC_RECAP: what happened in THIS game and why it was won (key turn, decisive card, lethal).",
    "GAME_STRATEGY: a lesson drawn from THIS game's plays; a chat question about THIS game's plays answered "
    "with game content is GAME_STRATEGY, not CHAT_INTERACTION (judge the content, not who asked).",
    "GENERAL_DECK_TALK: generic opinions on the deck, list, meta or balance not tied to a moment of this game.",
    "CHAT_INTERACTION: reading or answering chat about anything that is not this game's plays: greetings, "
    "thanks, subs, donations, personal questions, giveaways.",
    "UNRELATED_COMMENTARY: personal stories, off-topic talk, stream/channel talk, promotions.",
    "NEXT_GAME: transition to what follows: queueing, 'next one', ladder progress, the next opponent.",
    "UNCERTAIN: cannot tell (ASR errors).",
]


def mode(name: str | None) -> EndMode:
    return MODES.get(str(name or DEFAULT_END_MODE), MODES[DEFAULT_END_MODE])


def relevant_until(context: BoundaryContext, labels: Sequence[str], end_mode: EndMode) -> dict[str, Any]:
    """Where relevant post-result speech ends: {'target', 'reaction_end', 'stop_at', 'kept', 'stopped_by'}."""
    anchor = float(context.settings.get("anchor", context.anchors.get("game_end") or context.window.start))
    segments = list(context.transcript)
    reaction_end = anchor
    last_keep = anchor
    spent = 0.0
    stopped_by, stop_at = None, context.window.end
    kept: list[int] = []
    pending: list[int] = []  # UNCERTAIN segments kept only when relevant speech follows them
    for i, segment in enumerate(segments):
        if segment.end <= anchor + 0.05:
            continue  # speech before the result belongs to the game itself
        label = labels[i] if i < len(labels) else "UNCERTAIN"
        if label == "WIN_REACTION":
            kept += pending + [i]
            pending = []
            last_keep = max(last_keep, segment.end)
            reaction_end = max(reaction_end, segment.end)
            continue
        if label in end_mode.keep and label != "WIN_REACTION":
            duration = segment.end - max(segment.start, anchor)
            if spent + duration > end_mode.budget_s + 1e-6:
                stopped_by, stop_at = f"budget {end_mode.budget_s:.0f}s", segment.start
                break
            spent += duration
            kept += pending + [i]
            pending = []
            last_keep = max(last_keep, segment.end)
            continue
        if label == "UNCERTAIN" and segment.end - segment.start <= 4.0:
            pending.append(i)
            continue
        stopped_by, stop_at = label, segment.start
        break
    target = max(last_keep, anchor + end_mode.min_hold_s)
    return {"anchor": anchor, "target": target, "reaction_end": reaction_end, "stop_at": max(stop_at, target),
            "kept_segments": kept, "stopped_by": stopped_by, "relevant_s": round(spent, 3)}


def _speech_seconds(context: BoundaryContext, start: float, end: float) -> float:
    if end <= start:
        return 0.0
    total = 0.0
    for segment in context.transcript:
        total += max(0.0, min(end, segment.end) - max(start, segment.start))
    return total


def _naturalness(context: BoundaryContext, index: int) -> float:
    """0 = clean cut (pause or sentence edge) ... 1 = inside a word."""
    candidate = context.candidates[index]
    sources = set(candidate.sources)
    features = candidate.features
    if sources & {CandidateSource.SILENCE_MID, CandidateSource.SILENCE_START, CandidateSource.SILENCE_END,
                  CandidateSource.SENTENCE_END, CandidateSource.TRANSCRIPT_SEGMENT_END,
                  CandidateSource.END_OF_FILE, CandidateSource.NEXT_GAME_START} \
            or features.get("is_inside_transcript_segment") == 0.0:
        return 0.0
    if CandidateSource.WORD_GAP in sources or CandidateSource.SENTENCE_START in sources \
            or CandidateSource.TRANSCRIPT_SEGMENT_START in sources:
        return 0.4
    return 1.0


def candidate_cost(context: BoundaryContext, index: int, plan: dict[str, Any], end_mode: EndMode) -> float:
    t = context.candidates[index].timestamp
    target, reaction_end = plan["target"], plan["reaction_end"]
    kept_irrelevant = _speech_seconds(context, plan["stop_at"], t) + 0.25 * max(0.0, t - max(target, plan["stop_at"]))
    lost = _speech_seconds(context, t, target) if t < target else 0.0
    hold_short = max(0.0, plan["anchor"] + end_mode.min_hold_s - t)
    cost = end_mode.irrelevant_cost * kept_irrelevant + end_mode.lost_cost * lost + 6.0 * hold_short
    cost += 0.6 * _naturalness(context, index)
    if t < reaction_end - 0.05:
        cost += 100.0  # never cut the reaction to the win
    return cost


def choose_end(context: BoundaryContext, labels: Sequence[str], agent_index: int | None, allowed: Sequence[int],
               end_mode_name: str | None = None) -> tuple[int | None, dict[str, Any]]:
    """(candidate index, trace). Returns the agent's index when it is within the policy margin."""
    end_mode = mode(end_mode_name)
    plan = relevant_until(context, labels, end_mode)
    eligible = [i for i in allowed if context.candidates[i].timestamp >= plan["anchor"] - 0.3]
    if not eligible:
        return agent_index, {"policy": END_POLICY_VERSION, "mode": end_mode.name, "applied": False,
                             "reason": "no eligible candidate"}
    costs = {i: candidate_cost(context, i, plan, end_mode) for i in eligible}
    best = min(eligible, key=lambda i: (costs[i], context.candidates[i].timestamp))
    agent_cost = costs.get(agent_index) if agent_index is not None else None
    override = agent_cost is None or agent_cost > costs[best] + end_mode.override_margin
    chosen = best if override else agent_index
    trace = {"policy": END_POLICY_VERSION, "mode": end_mode.name, "applied": True,
             "target": round(plan["target"], 3), "stop_at": round(plan["stop_at"], 3),
             "reaction_end": round(plan["reaction_end"], 3), "stopped_by": plan["stopped_by"],
             "relevant_s": plan["relevant_s"], "agent_candidate": agent_index,
             "agent_cost": None if agent_cost is None else round(agent_cost, 3),
             "policy_candidate": best, "policy_cost": round(costs[best], 3), "overrode_agent": bool(override and
                                                                                                   chosen != agent_index)}
    return chosen, trace
