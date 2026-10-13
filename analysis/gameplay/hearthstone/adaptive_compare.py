"""Event-by-event diagnostics against a reference, never a tuning oracle."""
from __future__ import annotations

from statistics import mean
from typing import Any

from analysis.gameplay.models import GameplayAnalysisResult


def compare_analyses(reference: GameplayAnalysisResult, candidate: GameplayAnalysisResult, *, tolerance_s: float = 1.0
                     ) -> dict[str, Any]:
    if tolerance_s < 0:
        raise ValueError("Negative comparison tolerance")
    if reference.video_path != candidate.video_path or reference.duration != candidate.duration:
        raise ValueError("Compare analyses of the same source interval")
    available = set(range(len(candidate.events)))
    rows = []
    errors = []
    for event in reference.events:
        matches = [i for i in available if candidate.events[i].type == event.type
                   and abs(candidate.events[i].timestamp-event.timestamp) <= tolerance_s]
        index = min(matches, key=lambda i: (abs(candidate.events[i].timestamp-event.timestamp), i)) if matches else None
        other = candidate.events[index] if index is not None else None
        if index is not None:
            available.remove(index)
            assert other is not None
            errors.append(abs(other.timestamp-event.timestamp))
        rows.append({"type": event.type.value, "reference_timestamp": event.timestamp,
                     "candidate_timestamp": other.timestamp if other else None,
                     "error_s": abs(other.timestamp-event.timestamp) if other else None,
                     "status": "matched" if other else "missing"})
    extras = [{"type": candidate.events[i].type.value, "timestamp": candidate.events[i].timestamp} for i in sorted(available)]

    def same(a: float | None, b: float | None) -> bool:
        return a is None and b is None or a is not None and b is not None and abs(a-b) <= tolerance_s

    unused = set(range(len(candidate.games)))
    games = []
    for game in reference.games:
        matches = [i for i in unused if same(game.start, candidate.games[i].start) and same(game.end, candidate.games[i].end)]
        index = matches[0] if matches else None
        other_game = candidate.games[index] if index is not None else None
        if index is not None:
            unused.remove(index)
        games.append({"reference_index": game.index, "start": game.start, "end": game.end, "result": game.result.value,
                      "candidate_index": other_game.index if other_game else None,
                      "candidate_result": other_game.result.value if other_game else None,
                      "status": "matched" if other_game and other_game.result == game.result else "different"})
    pairing_errors = 0
    for game in candidate.games:
        if not game.complete:
            continue
        start_owner = next((i for i, g in enumerate(reference.games) if g.start is not None and same(game.start, g.start)), None)
        end_owner = next((i for i, g in enumerate(reference.games) if g.end is not None and same(game.end, g.end)), None)
        pairing_errors += int(start_owner is not None and end_owner is not None and start_owner != end_owner)
    windows = candidate.metadata.get("result_windows", [])
    results = [e for e in reference.events if e.type.value != "VS_SCREEN"]
    covered_results = sum(any(w["start"] <= e.timestamp < w["end"] for w in windows) for e in results)
    vs_windows = candidate.metadata.get("adaptive", {}).get("vs_candidate_windows")
    starts = [e for e in reference.events if e.type.value == "VS_SCREEN"]
    return {
        "tolerance_s": tolerance_s, "reference_events": len(reference.events), "candidate_events": len(candidate.events),
        "matched_events": len(errors), "missing_events": len(reference.events)-len(errors), "extra_events": len(extras),
        "event_recall": len(errors)/len(reference.events) if reference.events else None,
        "event_precision": len(errors)/len(candidate.events) if candidate.events else None,
        "mean_timestamp_error_s": mean(errors) if errors else None, "max_timestamp_error_s": max(errors, default=None),
        "events": rows, "extras": extras, "games": games, "extra_games": [candidate.games[i].to_mapping() for i in sorted(unused)],
        "pairing_errors": pairing_errors, "reference_complete": sum(g.complete for g in reference.games),
        "candidate_complete": sum(g.complete for g in candidate.games),
        "result_window_candidate_recall": covered_results/len(results) if results else None,
        "vs_window_candidate_recall": (sum(any(a <= e.timestamp < b for a, b in vs_windows) for e in starts)/len(starts)
                                      if starts and vs_windows is not None else None),
        "equivalent": not extras and len(errors) == len(reference.events) and all(g["status"] == "matched" for g in games) and not unused,
        "exact_event_timestamps": len(errors) == len(reference.events) and not extras and all(e == 0 for e in errors),
    }
