"""Clip finder v3: dense sentence-bounded windows scored like a short-form editor would.

What makes a clip work (clip tools such as OpusClip/Eklipse and livestream-highlight research agree):
  * a HOOK in the first ~3 s (no slow setup, no filler opening),
  * a PEAK/payoff inside the clip with the reaction after it (not cut off, not before the start),
  * EMOTION: the streamer's excitement (loudness relative to their own baseline), laughter, outbursts,
  * game EVENTS: in Hearthstone the end of a game (lethal, the result) and big board swings,
  * STANDALONE: starts on a sentence, ends after the reaction, 20-45 s ideally,
  * NOT chat reading, sub/donation thanks, greetings or calls to action.

v3 (profile "v3", the default) over v2, each change kept only if it helped on hand-marked moments of 3 real
edits without hurting any (docs/CLIPS.md, "Taratura"):
  * RESULT ANCHOR: the exact edited time of the VICTORY/DEFEAT screen (detector/project marker, else the end of
    the game interval, which ends on that screen) replaces the coarse "last 75 s of the game": a window that
    contains the result and stops a few seconds after it scores full, the decisive turns just before it score
    part, lingering in the lobby decays; windows ending RESULT_TAIL_S after the result are also tried. Without a
    known result time the v2 rule applies;
  * BOARD change: keyframe change measured on the Hearthstone board only (webcam and stream overlays masked),
    so alerts, overlays and the creator moving do not count as a board swing (full-frame change otherwise);
  * a clip starting ON its climax (no setup) gets less payoff; windows may end where unpunctuated speech runs on
    (ASR punctuates a third of the lines); reaction weighs more and the opening less.
  * TENSION (calm before the burst) is computed and reported but weighs 0: it made the ranking worse.
The v2 profile is kept unchanged for the evaluation against marked moments (evaluate.py).

Every candidate keeps its per-signal values, so the ranking is explainable (and testable). The optional AI
rerank (ai_rank.py) judges hook/flow/value on the transcript of the best local windows.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np

from .candidates import CTA, LEAD_S, SENTENCE_END, TAIL_S, Analysis, Line
from .models import ClipCandidate

GRID_S = 0.5
HOOK_S = 3.0
IDEAL_S = (20.0, 45.0)
MAX_PER_GAME = 3
MAX_OVERLAP = 0.25
CLOSING_S = 75.0  # v2: the last seconds of a game: lethal turn, concede, result
RESULT_TAIL_S = 4.0  # v3: a clip ending on the result keeps this much of the reaction after the screen
RESULT_LINGER_S = 8.0  # v3: beyond this after the result the clip drifts into the lobby
LEAD_UP_S = 35.0  # v3: a clip ending this close before the result holds the decisive turn
CALM_S = (12.0, 2.0)  # v3 tension: the calm is measured from 12 s to 2 s before the peak
TENSION_JUMP = (0.45, 0.9)  # v3 tension: peak - calm (arousal units) from which it counts / where it is full;
# measured on a real Hearthstone edit: median jump before a peak 0.44, 90th percentile 0.9
BOARD_RANK = (75.0, 98.0)  # v3 board change: percentiles of the video's own board changes mapped to 0..1
BOARD_FALLBACK_SHARE = 0.35  # too few keyframes to rank: this share of changed board pixels counts as 1
BOARD_MIN_SPAN = 0.1  # a quiet video's noise is not stretched into "big changes"
WEIGHTS = {"arousal": 0.28, "reaction": 0.24, "hook": 0.16, "payoff": 0.10, "game": 0.12, "visual": 0.05,
           "speech": 0.05}
PROFILES: dict[str, dict[str, Any]] = {
    "v2": {"weights": WEIGHTS, "result_anchor": False, "board": False, "early_peak": 0.6, "run_on_ends": False},
    # v3 = v2 + the changes that helped on hand-marked moments of 3 real edits without hurting any (docs/CLIPS.md):
    # result anchor, board-only change, early_peak 0.4 (a clip starting ON its climax), ends where unpunctuated
    # speech runs on (small penalty), reaction 0.24 -> 0.32, hook 0.16 -> 0.08. Tension is measured and shown but
    # weighs 0: adding it made the ranking worse on 2 of the 3 edits.
    "v3": {"weights": {"arousal": 0.28, "reaction": 0.32, "hook": 0.08, "payoff": 0.10, "game": 0.12,
                       "visual": 0.05, "speech": 0.05, "tension": 0.0},
           "result_anchor": True, "board": True, "early_peak": 0.4, "run_on_ends": True},
}
# v3 allows a window to end where speech runs on without punctuation (only 31-37 % of the ASR lines end with
# punctuation, so v2 discarded most good ends), at this small cost:
RUN_ON_PENALTY = 0.04
DEFAULT_PROFILE = "v3"

_TOKEN = re.compile(r"[\wàèéìòóù']+", re.UNICODE)
LAUGH = re.compile(r"\b(?:a?h?ah){2,}h?\b|\b(?:e?h?eh){2,}\b|\bahah\w*|\bhahah\w*|\bxd\b", re.IGNORECASE)
HYPE = {"letale", "lethal", "lethale", "gg", "topdeck", "top", "clutch", "rotto", "rotta", "broken", "vinta", "vinto",
        "persa", "perso", "assurdo", "assurda", "pazzesco", "pazzesca", "incredibile", "oddio", "madonna", "dio",
        "cazzo", "porca", "porco", "merda", "minchia", "raga", "ragazzi", "aiuto", "basta", "fortuna", "sfiga",
        "rng", "impossibile", "folle", "follia", "devastante", "sbagliato", "sbagliata", "errore", "misplay", "wow",
        "noo", "nooo", "sìì", "siii", "daje", "andiamo", "vai", "ecco", "boom"}
PHRASES = ("non ci credo", "ma dai", "ma come", "ma che", "no no", "che cosa", "ti prego", "è finita", "ce l'ho",
           "ce l'abbiamo", "non è possibile", "ma stai", "mi prendi", "non può", "che fortuna", "che sfiga")
CHAT = re.compile(r"\b(grazie (?:mille |tante )?(?:per|a|del|della|dei|delle)|sub(?:bato|scription|s)?\b|"
                  r"\bfollow\b|\bbits?\b|\bprime\b|\bdona(?:zione|to)\b|\braid\b|benvenut\w*|"
                  r"\bmes[ei] di\b|\bciao (?:a|ragazzi|tutti)\b|\bbuonasera\b|\bbuona serata\b|\bchat\b)",
                  re.IGNORECASE)
FILLER_START = {"e", "ma", "però", "quindi", "comunque", "allora", "cioè", "vabbè", "eh", "ehm", "uhm", "che", "perché",
                "poi", "anche", "tipo", "insomma"}


@dataclass
class Series:
    """Signals on the EDITED timeline, one value per GRID_S."""

    arousal: np.ndarray  # 0..~1.2: loudness above the streamer's median, p95 = 1
    visual: np.ndarray   # 0..1 keyframe change of the whole frame
    board: np.ndarray | None = None  # 0..1 keyframe change of the game board only (None: not measured)

    def window(self, values: np.ndarray, start: float, end: float) -> np.ndarray:
        a, b = int(start / GRID_S), max(int(start / GRID_S) + 1, int(np.ceil(end / GRID_S)))
        return values[a:b]


def _map_samples(analysis: Analysis, samples: Any, n: int) -> np.ndarray:
    """(source, source time, value) samples -> max per edited grid cell."""
    out = np.zeros(n, dtype=np.float64)
    for source, t, value in samples or []:
        for index, s0, s1, e0 in analysis.pieces:
            if source == index and s0 <= t < s1:
                slot = int((e0 + t - s0) / GRID_S)
                if 0 <= slot < n:
                    out[slot] = max(out[slot], float(value))
    return out


def build_series(analysis: Analysis, audio: Any = None, visual: Any = None, board: Any = None) -> Series:
    """Audio/visual evidence of the packaging analysis (source time) mapped onto the edited timeline.

    board: a VisualProfile measured on the board region only (analysis.packaging.visual.analyze_kept_board)."""
    n = int(np.ceil(analysis.duration / GRID_S)) + 1
    loud = np.full(n, np.nan, dtype=np.float64)
    tracks = list(getattr(audio, "tracks", []) or []) if audio is not None and getattr(audio, "available", False) else []
    for index, s0, s1, e0 in analysis.pieces:
        cells = np.arange(int(e0 / GRID_S), min(n, int(np.ceil((e0 + s1 - s0) / GRID_S))))
        for cell in cells:
            t = s0 + cell * GRID_S - e0
            values = [tr.slice(t, t + GRID_S) for tr in tracks if tr.source_index == index]
            frames = np.concatenate(values) if values else np.zeros(0)
            frames = frames[np.isfinite(frames)]
            if frames.size:
                loud[cell] = float(np.max(frames))
    arousal = np.zeros(n, dtype=np.float64)
    known = np.isfinite(loud)
    if known.sum() > 20:
        smooth = np.convolve(np.where(known, loud, np.nanmedian(loud)), np.ones(2) / 2.0, mode="same")
        median, p95 = np.percentile(smooth[known], 50), np.percentile(smooth[known], 95)
        arousal = np.clip((smooth - median) / max(1e-6, p95 - median), 0.0, 1.2)
        arousal[~known] = 0.0
    board_samples = getattr(board, "samples", None) if getattr(board, "status", "ok") == "ok" else None
    return Series(arousal, _map_samples(analysis, getattr(visual, "samples", None), n),
                  _map_samples(analysis, _ranked(board_samples), n) if board_samples else None)


def _ranked(samples: Sequence[tuple[int, float, float]]) -> list[tuple[int, float, float]]:
    """Raw board-change shares -> 0..1 within the video: the board always animates (hand, glows), so only the
    top of this video's own changes (BOARD_RANK) means a board swing."""
    values = np.array([v for _, _, v in samples], dtype=np.float64)
    if values.size < 20:
        return [(i, t, min(1.0, v / BOARD_FALLBACK_SHARE)) for i, t, v in samples]
    low, high = np.percentile(values, BOARD_RANK)
    span = max(BOARD_MIN_SPAN, high - low)
    return [(i, t, float(np.clip((v - low) / span, 0.0, 1.0))) for i, t, v in samples]


def reaction(text: str) -> tuple[float, list[str]]:
    """Outburst strength of a line: hype words, phrases, laughter, exclamations, repetitions (0..1)."""
    low = text.lower()
    tokens = _TOKEN.findall(low)
    hits = [t for t in tokens if t in HYPE]
    phrases = [p for p in PHRASES if p in low]
    laughter = len(LAUGH.findall(low))
    repeats = sum(1 for a, b in zip(tokens, tokens[1:]) if a == b and len(a) <= 5)
    value = 0.12 * len(hits) + 0.25 * len(phrases) + 0.35 * laughter + 0.1 * min(text.count("!"), 3) \
        + 0.12 * min(repeats, 3)
    if tokens:
        value *= min(1.0, 0.45 + 5.0 / len(tokens) + 0.1 * (len(hits) + len(phrases)))  # outbursts > long talk
    labels = sorted(set(hits))[:4] + phrases[:2] + (["laughter"] if laughter else [])
    return min(1.0, value), labels


def _lines_in(lines: Sequence[Line], start: float, end: float) -> list[Line]:
    return [line for line in lines if line.end > start and line.start < end]


def result_at(game: dict[str, Any]) -> float | None:
    """Edited time of the game's VICTORY/DEFEAT screen: the detector/project marker, else the end of the game
    interval (gameplay islands and export selections carry no markers and end on the result screen). Markers
    without a result screen mean the detector did not see the end: the interval end is then only a guess (often
    the end of the video), so the result time is unknown."""
    markers = game.get("markers") or {}
    if markers:
        value = (markers.get("result_screen") or {}).get("edited")
    else:
        value = game.get("edited_end")
    return None if value is None else float(value)


def _result_bonus(game: dict[str, Any], start: float, end: float) -> tuple[float, str | None]:
    """v3 game feature: the result screen inside the clip (and not too far before its end), or just after it."""
    at = result_at(game)
    if at is None:
        return 0.0, None
    value = {"WIN": 1.0, "LOSS": 0.85}.get(str(game.get("result") or "").upper(), 0.6)
    if start <= at <= end + 1.0:
        after = end - at
        return value * (1.0 if after <= RESULT_LINGER_S else max(0.4, 1.0 - (after - RESULT_LINGER_S) / 25.0)), \
            "result"
    if 0.0 < at - end <= LEAD_UP_S:
        return 0.55 * value * (1.0 - 0.5 * (at - end) / LEAD_UP_S), "lead-up"
    return 0.0, None


def _tension(series: Series, start: float, peak_at: float, peak_value: float) -> float:
    """Calm before the burst, inside the clip: the peak against the 75th percentile of the seconds before it."""
    if peak_value < 0.5 or peak_at - CALM_S[1] - start < 4.0:
        return 0.0
    calm = series.window(series.arousal, max(start, peak_at - CALM_S[0]), peak_at - CALM_S[1])
    if calm.size < int(4.0 / GRID_S):
        return 0.0
    jump = peak_value - float(np.percentile(calm, 75))
    return float(np.clip((jump - TENSION_JUMP[0]) / (TENSION_JUMP[1] - TENSION_JUMP[0]), 0.0, 1.0))


def score_window(analysis: Analysis, series: Series, start: float, end: float,
                 reactions: dict[int, tuple[float, list[str]]], games: Sequence[dict[str, Any]],
                 inside: Sequence[int] | None = None, profile: str = DEFAULT_PROFILE,
                 runs_on: bool = False) -> tuple[float, dict[str, Any]]:
    """inside: indexes of the transcript lines in the window (computed when not given); runs_on: the window
    ends on an unpunctuated line followed by speech within 0.6 s."""
    rules = PROFILES[profile]
    duration = end - start
    arousal = series.window(series.arousal, start, end)
    if inside is None:
        inside = [n for n, line in enumerate(analysis.lines) if line.end > start and line.start < end]
    text = " ".join(analysis.lines[n].text for n in inside)
    peak_value = float(arousal.max()) if arousal.size else 0.0
    peak_at = start + (float(np.argmax(arousal)) * GRID_S if arousal.size else 0.0)
    top = np.sort(arousal)[-max(1, arousal.size // 5):] if arousal.size else np.zeros(1)
    f: dict[str, Any] = {}
    f["arousal"] = min(1.0, 0.6 * peak_value + 0.4 * float(top.mean()))
    values = sorted((reactions[n][0] for n in inside), reverse=True)
    f["reaction"] = min(1.0, (values[0] if values else 0.0) + 0.5 * sum(values[1:3]))
    first = analysis.lines[inside[0]] if inside else None
    hook_audio = float(series.window(series.arousal, start, min(end, start + HOOK_S)).mean()) if arousal.size else 0.0
    hook_words = max((reactions[n][0] for n in inside if analysis.lines[n].start < start + HOOK_S), default=0.0)
    f["hook"] = min(1.0, 0.6 * min(1.0, hook_audio * 1.4) + 0.6 * hook_words
                    + (0.15 if first is not None and re.search(r"[!?]", first.text) else 0.0))
    position = (peak_at - start) / max(duration, 1e-6)
    after = end - peak_at
    f["payoff"] = 0.0 if peak_value < 0.35 else (1.0 if 0.2 <= position <= 0.88 and after >= 1.5 else
                                                 rules["early_peak"] if position < 0.2 else 0.15)
    game_bonus, result, moment, overlap = 0.0, None, None, 0.0
    for game in games:
        g0, g1 = game.get("edited_start"), game.get("edited_end")
        if g0 is None or g1 is None or end <= g0 or start >= g1:
            continue
        if rules["result_anchor"] and result_at(game) is not None:
            bonus, kind = _result_bonus(game, start, end)
            if bonus > game_bonus:
                game_bonus, moment, result = bonus, kind, game.get("result")
            elif game_bonus == 0.0 and min(end, g1) - max(start, g0) > overlap:  # the game the clip is mostly in
                overlap, result = min(end, g1) - max(start, g0), game.get("result")
            continue
        result = game.get("result")  # v2, or v3 when the detector did not see the result screen
        if end >= g1 - CLOSING_S:  # contains the closing turns of the game
            game_bonus = max(game_bonus, 1.0 if result == "WIN" else 0.8 if result == "LOSS" else 0.5)
            moment = "result"
    f["game"] = game_bonus
    if "tension" in rules["weights"]:
        f["tension"] = _tension(series, start, peak_at, peak_value)
    change = series.board if rules["board"] and series.board is not None else series.visual
    f["visual"] = float(series.window(change, start, end).max()) if change.size else 0.0
    covered = sum(min(end, analysis.lines[n].end) - max(start, analysis.lines[n].start) for n in inside)
    voiced = covered / max(duration, 1e-6)
    f["speech"] = min(1.0, voiced / 0.6)
    penalties, notes = 0.0, []
    if CHAT.search(text):
        penalties += 0.25 + 0.1 * min(2, len(CHAT.findall(text)) - 1)
        notes.append("chat / sub thanks")
    if CTA.search(text):
        penalties += 0.3
        notes.append("greeting / call to action")
    if first is not None:
        words = _TOKEN.findall(first.text.lower())
        if words and words[0] in FILLER_START and f["hook"] < 0.4:
            penalties += 0.06
            notes.append("slow opening")
    if runs_on:
        penalties += RUN_ON_PENALTY
        notes.append("ends mid-sentence")
    if voiced < 0.3:
        penalties += 0.1
        notes.append("little speech")
    if games and result is None:
        penalties += 0.08
        notes.append("outside the games")
    if duration > IDEAL_S[1]:
        penalties += 0.004 * (duration - IDEAL_S[1])
    score = sum(weight * f[k] for k, weight in rules["weights"].items()) - penalties
    labels = sorted({w for n in inside for w in reactions[n][1]})[:5]
    reason = []
    if f["arousal"] >= 0.6:
        reason.append("excited voice")
    if labels:
        reason.append("reactions: " + ", ".join(labels))
    if f["hook"] >= 0.5:
        reason.append("strong opening")
    if rules["weights"].get("tension") and f["tension"] >= 0.5:
        reason.append("calm before the burst")
    if moment == "result" and game_bonus >= 0.8:
        reason.append(f"end of a {result} game" if result else "end of a game")
    elif moment == "lead-up" and game_bonus >= 0.3:
        reason.append(f"decisive turn before the {result}" if result else "decisive turn before the end")
    if f["visual"] >= 0.6:
        reason.append("big board change")
    return score, {"features": {k: round(float(v), 3) for k, v in f.items()}, "penalty": round(penalties, 3),
                   "notes": notes, "reason": "; ".join(reason) or "steady talk", "peak": round(peak_at, 2),
                   "result": result}


def _result_ends(lines: Sequence[Line], games: Sequence[dict[str, Any]], total: float) -> list[float]:
    """v3 extra window ends: RESULT_TAIL_S after each result screen, finishing the sentence running there."""
    ends = []
    for game in games:
        at = result_at(game)
        if at is None:
            continue
        end = min(total, at + RESULT_TAIL_S)
        running = next((line for line in lines if line.start < end < line.end), None)
        if running is not None and running.end - end <= 4.0:
            end = min(total, running.end + TAIL_S)
        ends.append(end)
    return ends


Scored = list[tuple[float, float, float, dict[str, Any]]]  # (score, start, end, info of score_window)


def score_windows(analysis: Analysis, series: Series, *, min_s: float = 20.0, max_s: float = 60.0,
                  profile: str = DEFAULT_PROFILE) -> Scored:
    """Every sentence-bounded window of [min_s, max_s] (plus the v3 result-anchored ends) with its score."""
    if profile not in PROFILES:
        raise ValueError(f"unknown clip scoring profile {profile!r}")
    lines = analysis.lines
    reactions = {n: reaction(line.text) for n, line in enumerate(lines)}
    games = real_games(analysis)
    total = analysis.duration
    extra_ends = _result_ends(lines, games, total) if PROFILES[profile]["result_anchor"] else []
    scored: list[tuple[float, float, float, dict[str, Any]]] = []
    for i, first in enumerate(lines):
        start = max(0.0, first.start - LEAD_S)
        for end in extra_ends:
            if min_s <= end - start <= max_s:
                inside = [n for n in range(i, len(lines)) if lines[n].start < end]
                score, info = score_window(analysis, series, start, end, reactions, games, inside, profile)
                scored.append((score, start, end, info))
        for j in range(i, len(lines)):
            end = min(total, lines[j].end + TAIL_S)
            duration = end - start
            if duration > max_s:
                break
            if duration < min_s:
                continue
            runs_on = not SENTENCE_END.search(lines[j].text) and j + 1 < len(lines) and                 lines[j + 1].start - lines[j].end < 0.6
            if runs_on and not PROFILES[profile]["run_on_ends"]:
                continue  # v2: ends mid-sentence with speech running on
            score, info = score_window(analysis, series, start, end, reactions, games, range(i, j + 1), profile,
                                       runs_on)
            scored.append((score, start, end, info))
    return scored


def select_windows(scored: Scored, games: Sequence[dict[str, Any]], limit: int,
                   per_game_cap: int = MAX_PER_GAME) -> Scored:
    """Best windows first, skipping overlaps (> MAX_OVERLAP) and games that already have per_game_cap clips."""
    chosen: Scored = []
    per_game: dict[Any, int] = {}
    for score, start, end, info in sorted(scored, key=lambda item: -item[0]):
        if any(min(end, e) - max(start, s) > MAX_OVERLAP * min(end - start, e - s) for _, s, e, _ in chosen):
            continue
        game = _game_of(games, start, end)
        if game is not None:
            if per_game.get(game, 0) >= per_game_cap:
                continue
            per_game[game] = per_game.get(game, 0) + 1
        chosen.append((score, start, end, info))
        if len(chosen) >= limit:
            break
    return chosen


def find_clips(analysis: Analysis, series: Series, *, count: int = 10, min_s: float = 20.0, max_s: float = 60.0,
               pool: int | None = None, profile: str = DEFAULT_PROFILE) -> list[ClipCandidate]:
    """Best non-overlapping sentence-bounded windows; at most MAX_PER_GAME per game (variety).

    pool: candidates for the AI rerank (more than count); the per-game cap grows in proportion, otherwise a VOD
    of 4 games could never offer more than 12 candidates to the agent."""
    lines = analysis.lines
    games = real_games(analysis)
    limit = pool or count
    cap = MAX_PER_GAME if limit <= count else -(-MAX_PER_GAME * limit // count)
    chosen = select_windows(score_windows(analysis, series, min_s=min_s, max_s=max_s, profile=profile), games,
                            limit, cap)
    out = []
    for n, (score, start, end, info) in enumerate(chosen, start=1):
        text = " ".join(line.text for line in _lines_in(lines, start, end))
        out.append(ClipCandidate(
            id=f"C{n:02d}", edited_start=start, edited_end=end, score=score, reason=info["reason"], text=text[:400],
            game_id=_game_of(games, start, end), result=info["result"], frame=_frame_near(analysis, info["peak"]),
            parts=analysis.source_parts(start, end), notes=list(info["notes"]),
            signals={**info["features"], "penalty": info["penalty"], "peak": info["peak"]}))
    return out


def real_games(analysis: Analysis) -> list[dict[str, Any]]:
    """Detected games with edited bounds; the 'whole_video' context of a plain file is not a game.

    A game whose end the detector did not see runs to the end of the video and overlaps the next games: it is
    cut where the next game starts, otherwise every later clip would belong to the first game (and its cap)."""
    games = sorted((g for g in analysis.games.values() if g.get("origin") != "whole_video"
                    and g.get("edited_start") is not None and g.get("edited_end") is not None),
                   key=lambda g: float(g["edited_start"]))
    out = []
    for n, game in enumerate(games):
        following = games[n + 1]["edited_start"] if n + 1 < len(games) else None
        if following is not None and float(game["edited_end"]) > float(following) > float(game["edited_start"]):
            game = {**game, "edited_end": following}
        out.append(game)
    return out


def _game_of(games: Sequence[dict[str, Any]], start: float, end: float) -> str | None:
    best, overlap = None, 0.0
    for game in games:
        g0, g1 = game.get("edited_start"), game.get("edited_end")
        if g0 is None or g1 is None:
            continue
        value = min(end, g1) - max(start, g0)
        if value > overlap:
            best, overlap = str(game.get("game_id")), value
    return best


def _frame_near(analysis: Analysis, t: float) -> str | None:
    """The saved frame closest to the peak (preview image), any highlight frame otherwise."""
    best = None
    for h in analysis.highlights:
        for frame_id in h.get("frame_ids") or []:
            if frame_id in analysis.frames:
                distance = abs(float(h.get("edited_start", 0)) - t)
                if best is None or distance < best[0]:
                    best = (distance, analysis.frames[frame_id])
    return best[1] if best else None
