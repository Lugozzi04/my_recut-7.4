"""Multi-signal highlight CANDIDATE discovery over the kept content.

Signals (each optional; a missing modality is reported, not invented):
  audio_peak      1 s energy well above the kept-content baseline (voice band RMS)
  reaction        exclamations / interjections / laughter in the transcript (heuristic lexicon)
  speech_burst    unusually dense speech (words per second) compared with the video
  pause_break     a long pause followed by speech (often a reveal or a realisation)
  explanation     long continuous speech: strategy or deck explanations
  near_result     the last seconds before a WIN result screen (decisive plays)
  game_intro      the start of a game (matchup / plan talk)
  visual_change   strong keyframe-to-keyframe change (board / scene transitions)

The local score only RANKS candidates for the agent; it never decides what is
interesting. Every kept game gets candidates even without any loud reaction.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

import numpy as np

from .audio import AudioProfile
from .games import game_for
from .models import GameContext, HighlightCandidate, TranscriptLine
from .timeline import EditMap
from .visual import VisualProfile

# Interjections that mark spoken reactions (Italian + English streams); a weak, documented heuristic.
REACTION_WORDS = {
    "no", "noo", "nooo", "dai", "vai", "andiamo", "incredibile", "assurdo", "assurda", "pazzesco", "pazzesca",
    "madonna", "oddio", "mamma", "porca", "boom", "wow", "letale", "lethal", "gg", "evvai", "finalmente",
    "cosa", "ma", "giuro", "clamoroso", "clamorosa", "impossibile", "perfetto", "perfetta", "sì", "si", "yes",
    "what", "insane", "omg", "lol", "ahah", "ahahah", "haha", "hahaha", "let's", "letsgo", "bravo", "grande",
}
STRONG_REACTIONS = {"incredibile", "assurdo", "assurda", "pazzesco", "pazzesca", "madonna", "oddio", "boom", "wow",
                    "letale", "lethal", "clamoroso", "clamorosa", "impossibile", "insane", "omg", "evvai", "finalmente"}
WEIGHTS = {"guided_focus": 1.1, "audio_peak": 1.0, "reaction": 0.9, "near_result": 0.9, "speech_burst": 0.6, "explanation": 0.55,
           "pause_break": 0.4, "visual_change": 0.4, "game_intro": 0.35, "coverage": 0.2}
_TOKEN = re.compile(r"[\wàèéìòóù']+", re.UNICODE)


@dataclass(frozen=True)
class Signal:
    edited: float
    kind: str
    strength: float  # 0..1
    detail: str


def reaction_strength(text: str) -> tuple[float, list[str]]:
    tokens = [t.lower() for t in _TOKEN.findall(text)]
    hits = [t for t in tokens if t in REACTION_WORDS]
    strong = [t for t in hits if t in STRONG_REACTIONS]
    exclaims = text.count("!")
    laughter = len(re.findall(r"\b(?:a?h?ah){2,}\b", text.lower()))
    repeated = sum(1 for a, b in zip(tokens, tokens[1:]) if a == b and a in REACTION_WORDS)
    value = 0.25 * len(strong) + 0.08 * (len(hits) - len(strong)) + 0.15 * min(exclaims, 3) + 0.3 * laughter \
        + 0.2 * repeated
    if tokens:
        value *= min(1.0, 0.5 + 6.0 / len(tokens) + 0.1 * len(hits))  # short outbursts weigh more than long talk
    return min(1.0, value), sorted(set(hits))[:6]


def collect_signals(edit_map: EditMap, lines: Sequence[TranscriptLine], audio: AudioProfile, visual: VisualProfile,
                    games: Sequence[GameContext], *, creative_focus: str = "") -> list[Signal]:
    signals: list[Signal] = []
    for source_index, t, rel in audio.peaks():
        edited = edit_map.to_edited(t, source_index)
        if edited is not None:
            signals.append(Signal(edited, "audio_peak", min(1.0, 0.3 + (rel - 6.0) / 12.0), f"+{rel:.1f} dB above baseline"))
    for line in lines:
        value, words = reaction_strength(line.text)
        if value >= 0.2:
            signals.append(Signal((line.edited_start + line.edited_end) / 2, "reaction", value,
                                  "reaction words: " + ", ".join(words) if words else "exclamation/laughter"))
    if creative_focus:
        from .direction import relevance, tokens
        for line in lines:
            score = relevance(creative_focus, line.text)
            matches = tokens(creative_focus) & tokens(line.text)
            if score >= .20 and (len(matches) >= 2 or len(tokens(creative_focus)) == 1):
                signals.append(Signal((line.edited_start + line.edited_end) / 2, "guided_focus", score,
                                      "lexical focus match (not combo verification): " + ", ".join(sorted(matches))))
    signals += _speech_signals(lines, edit_map.duration)
    for game in games:
        if game.edited_start is None or game.edited_end is None:
            continue
        result = (game.markers.get("result_screen") or {}).get("edited")
        if game.result == "WIN" and result is not None:
            signals.append(Signal(max(game.edited_start, result - 10.0), "near_result", 0.8,
                                  f"seconds before the WIN result of game {game.index}"))
        vs = (game.markers.get("vs_screen") or {}).get("edited")
        if vs is not None:
            signals.append(Signal(min(game.edited_end, vs + 6.0), "game_intro", 0.4, f"start of game {game.index}"))
    for source_index, t, change in visual.peaks():
        edited = edit_map.to_edited(t, source_index)
        if edited is not None:
            signals.append(Signal(edited, "visual_change", min(1.0, change * 1.6), f"keyframe change {change:.2f}"))
    return sorted(signals, key=lambda s: s.edited)


def _speech_signals(lines: Sequence[TranscriptLine], duration: float) -> list[Signal]:
    if not lines or duration <= 0:
        return []
    signals: list[Signal] = []
    words = np.array([(float(w["edited_start"]) + float(w["edited_end"])) / 2 for line in lines for w in line.words]
                     or [(line.edited_start + line.edited_end) / 2 for line in lines])
    bins = np.histogram(words, bins=max(1, int(duration // 2.0)), range=(0.0, duration))[0].astype(float)
    if bins.size >= 5:
        rate = np.convolve(bins, np.ones(5) / 10.0, mode="same")  # words/s over 10 s
        mean, std = float(rate.mean()), float(rate.std()) or 1.0
        order = np.argsort(rate)[::-1]
        taken: list[float] = []
        for i in order[:200]:
            z = (rate[i] - mean) / std
            if z < 1.5:
                break
            t = i * 2.0 + 1.0
            if all(abs(t - o) > 15.0 for o in taken):
                taken.append(t)
                signals.append(Signal(t, "speech_burst", min(1.0, 0.3 + 0.2 * (z - 1.5)),
                                      f"{rate[i]:.1f} words/s (video mean {mean:.1f})"))
            if len(taken) >= 12:
                break
    run_start = lines[0].edited_start
    previous_end = lines[0].edited_end
    for line in list(lines[1:]) + [None]:
        gap = (line.edited_start - previous_end) if line is not None else 99.0
        if line is not None and gap >= 2.5:
            signals.append(Signal(line.edited_start, "pause_break", min(1.0, 0.2 + gap / 10.0),
                                  f"{gap:.1f} s pause before speech"))
        if gap > 1.5:
            if previous_end - run_start >= 25.0:
                signals.append(Signal((run_start + previous_end) / 2, "explanation", 0.5,
                                      f"{previous_end - run_start:.0f} s of continuous speech"))
            if line is not None:
                run_start = line.edited_start
        if line is not None:
            previous_end = line.edited_end
    return signals


def _clock(t: float) -> str:
    return f"{int(t) // 60:02d}:{int(t) % 60:02d}"


def build_candidates(edit_map: EditMap, signals: Sequence[Signal], lines: Sequence[TranscriptLine],
                     audio: AudioProfile, visual: VisualProfile, games: Sequence[GameContext], *,
                     max_candidates: int = 16, per_game: int = 2, cluster_s: float = 14.0) -> list[HighlightCandidate]:
    clusters: list[list[Signal]] = []
    for signal in signals:
        game = game_for(games, signal.edited)
        if clusters and signal.edited - clusters[-1][0].edited <= cluster_s and \
                game_for(games, clusters[-1][0].edited) is game:
            clusters[-1].append(signal)
        else:
            clusters.append([signal])
    scored = []
    for cluster in clusters:
        best: dict[str, Signal] = {}
        for s in cluster:
            if s.kind not in best or s.strength > best[s.kind].strength:
                best[s.kind] = s
        score = sum(WEIGHTS.get(k, 0.3) * s.strength for k, s in best.items()) + 0.15 * (len(best) - 1)
        scored.append((score, cluster, best))
    chosen: list[tuple[float, list[Signal], dict[str, Signal], bool]] = []

    def overlaps(cluster: list[Signal]) -> bool:
        a, b = cluster[0].edited, cluster[-1].edited
        return any(not (b + 6 < c[1][0].edited - 6 or a - 6 > c[1][-1].edited + 6) for c in chosen)

    by_score = sorted(scored, key=lambda item: item[0], reverse=True)
    for game in games:  # coverage first: every kept game gets candidates
        mine = [item for item in by_score if game_for(games, item[1][0].edited) is game]
        automatic = sorted(mine, key=lambda item: item[0] - 1.1 * item[2].get("guided_focus", Signal(0, "", 0, "")).strength, reverse=True)
        priority = mine[:max(1, per_game - 1)] + automatic[:1] if any("guided_focus" in item[2] for item in mine) else mine[:per_game]
        for item in priority:
            if not overlaps(item[1]):
                chosen.append((*item, False))
        if not mine and game.edited_start is not None and game.edited_end is not None:
            middle = _densest_speech(lines, game.edited_start, game.edited_end)
            fallback = Signal(middle, "coverage", 0.5, "coverage: no strong signal in this game")
            chosen.append((0.1, [fallback], {"coverage": fallback}, True))
    for item in by_score:
        if len(chosen) >= max_candidates:
            break
        if not overlaps(item[1]):
            chosen.append((*item, False))
    chosen.sort(key=lambda item: item[1][0].edited)
    out: list[HighlightCandidate] = []
    for n, (score, cluster, best, coverage) in enumerate(chosen, start=1):
        game = game_for(games, cluster[0].edited)
        lo = game.edited_start if game and game.edited_start is not None else 0.0
        hi = game.edited_end if game and game.edited_end is not None else edit_map.duration
        start = max(lo, cluster[0].edited - 5.0)
        end = min(hi, max(cluster[-1].edited + 5.0, start + 8.0))
        end = min(end, start + 24.0)
        peak_signal = max(cluster, key=lambda s: WEIGHTS.get(s.kind, 0.3) * s.strength)
        peak_edited = min(max(peak_signal.edited, start), end)
        source_index, peak_source = edit_map.to_source(peak_edited)
        _, source_start = edit_map.to_source(start)
        _, source_end = edit_map.to_source(end)
        context_lines = [line for line in lines if line.edited_end >= start - 4.0 and line.edited_start <= end + 4.0]
        transcript = " ".join(f"[{_clock(line.edited_start)}] {line.text}" for line in context_lines)
        features = audio.features(source_index, source_start, source_end)
        out.append(HighlightCandidate(
            id=f"H{n:02d}", game_id=game.game_id if game else None, source_index=source_index,
            source_start=source_start, source_end=source_end, edited_start=start, edited_end=end,
            peak_source=peak_source, evidence_sources=sorted(best), audio=features,
            transcript_ids=[line.id for line in context_lines], transcript_context=transcript[:1600],
            reason="; ".join(best[k].detail for k in sorted(best, key=lambda k: -WEIGHTS.get(k, 0.3))),
            local_score=float(score),
            visual={"keyframe_change_max": round(visual.change_near(source_index, source_start, source_end), 3),
                    "guided_relevance": best.get("guided_focus", Signal(0, "", 0, "")).strength},
            coverage=coverage))
    for game in games:
        game.highlight_ids = [h.id for h in out if h.game_id == game.game_id]
    return out


def _densest_speech(lines: Sequence[TranscriptLine], start: float, end: float) -> float:
    mine = [line for line in lines if start <= line.edited_start <= end]
    if not mine:
        return (start + end) / 2
    best = max(mine, key=lambda line: len(line.words) / max(1.0, line.edited_end - line.edited_start + 2.0))
    return (best.edited_start + best.edited_end) / 2
