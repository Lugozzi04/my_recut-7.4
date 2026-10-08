from __future__ import annotations

from collections.abc import Mapping
import math
from statistics import median
from typing import Any

from analysis.gameplay.models import GameEventType, GameResult, GameplayAnalysisResult


def _time(value: Any, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be null or finite nonnegative seconds")
    return float(value)


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _match(predicted: list[dict[str, Any]], truth: list[dict[str, Any]], tolerance: float) -> dict[int, int]:
    """One-to-one maximum-cardinality boundary matching, independent of result labels."""
    edges: dict[int, list[int]] = {}
    for predicted_index, prediction in enumerate(predicted):
        candidates: list[tuple[float, int]] = []
        for truth_index, expected in enumerate(truth):
            errors = [abs(prediction[key] - expected[key]) for key in ("start", "end")
                      if prediction[key] is not None and expected[key] is not None]
            if errors and max(errors) <= tolerance:
                candidates.append((sum(errors), truth_index))
        edges[predicted_index] = [index for _cost, index in sorted(candidates)]
    return _assign(edges, len(predicted))


def _assign(edges: dict[int, list[int]], predicted_count: int) -> dict[int, int]:
    """Maximum-cardinality assignment; each prediction and truth anchor is used once."""
    matched: dict[int, int] = {}

    def assign(index: int, seen: set[int]) -> bool:
        for truth_index in edges[index]:
            if truth_index in seen:
                continue
            seen.add(truth_index)
            if truth_index not in matched or assign(matched[truth_index], seen):
                matched[truth_index] = index
                return True
        return False

    for index in range(predicted_count):
        assign(index, set())
    return matched



def _match_events(
    predicted: list[dict[str, Any]], truth: list[dict[str, Any]], tolerance: float,
) -> dict[int, int]:
    # Result labels deliberately do not enter the assignment. A VICTORY at the
    # DEFEAT anchor is a classification error, not a missed anchor plus lucky TP.
    edges = {
        index: [truth_index for _error, truth_index in sorted(
            (abs(item["timestamp"] - expected["timestamp"]), truth_index)
            for truth_index, expected in enumerate(truth)
            if abs(item["timestamp"] - expected["timestamp"]) <= tolerance
        )]
        for index, item in enumerate(predicted)
    }
    return _assign(edges, len(predicted))


def _timestamp_metrics(errors: list[float]) -> dict[str, Any]:
    return {
        "count": len(errors),
        "mean_absolute_error_s": sum(errors) / len(errors) if errors else None,
        "median_absolute_error_s": median(errors) if errors else None,
        "max_absolute_error_s": max(errors) if errors else None,
    }


def _prediction_summary(game: dict[str, Any]) -> dict[str, Any]:
    return {key: game[key] for key in ("id", "index", "start", "end", "result")}


def _event_metrics(
    analysis: GameplayAnalysisResult, truth: list[dict[str, Any]], first: float, last: float, tolerance: float,
) -> dict[str, Any]:
    expected_vs = [{"timestamp": item["start"], "type": GameEventType.VS_SCREEN.value} for item in truth]
    expected_results = [{
        "timestamp": item["end"],
        "type": GameEventType.VICTORY.value if item["result"] == GameResult.WIN.value else GameEventType.DEFEAT.value,
    } for item in truth]
    events = [event.to_mapping() for event in analysis.events if first <= event.timestamp <= last]
    predicted_vs = [item for item in events if item["type"] == GameEventType.VS_SCREEN.value]
    predicted_results = [item for item in events if item["type"] != GameEventType.VS_SCREEN.value]
    vs_matches = _match_events(predicted_vs, expected_vs, tolerance)
    result_matches = _match_events(predicted_results, expected_results, tolerance)
    class_metrics: dict[str, Any] = {}
    for kind in GameEventType:
        expected = expected_vs if kind is GameEventType.VS_SCREEN else expected_results
        predicted = predicted_vs if kind is GameEventType.VS_SCREEN else predicted_results
        matches = vs_matches if kind is GameEventType.VS_SCREEN else result_matches
        true_positive = sum(expected[ti]["type"] == predicted[pi]["type"] == kind.value
                            for ti, pi in matches.items())
        expected_count = sum(item["type"] == kind.value for item in expected)
        predicted_count = sum(item["type"] == kind.value for item in predicted)
        class_metrics[kind.value] = {
            "ground_truth": expected_count, "predicted": predicted_count, "true_positive": true_positive,
            "false_negative": expected_count - true_positive, "false_positive": predicted_count - true_positive,
            "recall": _ratio(true_positive, expected_count), "precision": _ratio(true_positive, predicted_count),
        }
    comparisons: list[dict[str, Any]] = []
    for index, expected_game in enumerate(truth):
        comparison: dict[str, Any] = {"ground_truth_index": expected_game["index"]}
        for key, expected, predicted, matches in (
            ("vs", expected_vs, predicted_vs, vs_matches),
            ("result", expected_results, predicted_results, result_matches),
        ):
            actual = predicted[matches[index]] if index in matches else None
            comparison[key] = {
                "ground_truth": expected[index], "detected": actual,
                "absolute_error_s": abs(actual["timestamp"] - expected[index]["timestamp"]) if actual else None,
                "status": ("FALSE_NEGATIVE" if actual is None else "OK" if actual["type"] == expected[index]["type"]
                           else "MISCLASSIFIED"),
            }
        comparisons.append(comparison)
    unmatched: list[dict[str, Any]] = []
    for predicted, matches in ((predicted_vs, vs_matches), (predicted_results, result_matches)):
        used = set(matches.values())
        unmatched.extend({**item, "status": "FALSE_POSITIVE"} for index, item in enumerate(predicted) if index not in used)
    result_correct = sum(expected_results[ti]["type"] == predicted_results[pi]["type"] for ti, pi in result_matches.items())
    result_confusion = {kind.value: {prediction.value: 0 for prediction in (GameEventType.VICTORY, GameEventType.DEFEAT)}
                        for kind in (GameEventType.VICTORY, GameEventType.DEFEAT)}
    for truth_index, predicted_index in result_matches.items():
        result_confusion[expected_results[truth_index]["type"]][predicted_results[predicted_index]["type"]] += 1
    return {
        "vs_recall": class_metrics[GameEventType.VS_SCREEN.value]["recall"],
        "result_detection_recall": _ratio(len(result_matches), len(expected_results)),
        "victory_recall": class_metrics[GameEventType.VICTORY.value]["recall"],
        "defeat_recall": class_metrics[GameEventType.DEFEAT.value]["recall"],
        "event_detection": class_metrics,
        "result_event_accuracy": _ratio(result_correct, len(result_matches)),
        "result_event_confusion": result_confusion,
        "false_positive_event_count": len(unmatched),
        "false_positive_result_event_count": len(predicted_results) - len(result_matches),
        "false_positive_defeat_event_count": class_metrics[GameEventType.DEFEAT.value]["false_positive"],
        "vs_timestamp_error": _timestamp_metrics([
            abs(predicted_vs[pi]["timestamp"] - expected_vs[ti]["timestamp"]) for ti, pi in vs_matches.items()
        ]),
        "result_timestamp_error": _timestamp_metrics([
            abs(predicted_results[pi]["timestamp"] - expected_results[ti]["timestamp"]) for ti, pi in result_matches.items()
        ]),
        "event_comparisons": comparisons, "unmatched_events": unmatched,
    }


def _game_comparisons(
    predicted: list[dict[str, Any]], truth: list[dict[str, Any]], matched: dict[int, int], tolerance: float,
) -> list[dict[str, Any]]:
    comparisons: list[dict[str, Any]] = []
    for index, expected in enumerate(truth):
        actual = predicted[matched[index]] if index in matched else None
        comparison: dict[str, Any] = {
            "ground_truth_index": expected["index"],
            "ground_truth": {key: expected[key] for key in ("start", "end", "result")},
            "detected": _prediction_summary(actual) if actual is not None else None,
            "start_error_s": abs(actual["start"] - expected["start"]) if actual and actual["start"] is not None else None,
            "result_error_s": abs(actual["end"] - expected["end"]) if actual and actual["end"] is not None else None,
            "status": ("FALSE_NEGATIVE" if actual is None else "PARTIAL"
                       if actual["start"] is None or actual["end"] is None or actual["result"] == GameResult.UNKNOWN.value
                       else "OK" if actual["result"] == expected["result"] else "MISCLASSIFIED"),
        }
        if actual is None:
            nearby = []
            for candidate in predicted:
                errors = [abs(candidate[key] - expected[key]) for key in ("start", "end") if candidate[key] is not None]
                if errors and min(errors) <= tolerance:
                    nearby.append((min(errors), sum(errors), candidate))
            if nearby:
                candidate = min(nearby, key=lambda item: (item[0], item[1]))[2]
                comparison["nearby_prediction"] = _prediction_summary(candidate)
                comparison["nearby_start_error_s"] = abs(candidate["start"] - expected["start"]) if candidate["start"] is not None else None
                comparison["nearby_result_error_s"] = abs(candidate["end"] - expected["end"]) if candidate["end"] is not None else None
                comparison["pairing_mismatch"] = (candidate["start"] is not None and candidate["end"] is not None
                                                   and max(abs(candidate[key] - expected[key]) for key in ("start", "end")) > tolerance)
        comparisons.append(comparison)
    return comparisons


def evaluate_analysis(
    analysis: GameplayAnalysisResult, ground_truth: Mapping[str, Any], *, tolerance_s: float = 5.0,
) -> dict[str, Any]:
    if not isinstance(ground_truth, Mapping):
        raise ValueError("Ground truth must be a JSON object")
    if ground_truth.get("video_fingerprint") is not None and ground_truth["video_fingerprint"] != analysis.metadata.get("video_fingerprint"):
        raise ValueError("Ground-truth video fingerprint does not match the analyzed video")
    if isinstance(tolerance_s, bool) or not isinstance(tolerance_s, (int, float)) or not math.isfinite(tolerance_s) or tolerance_s <= 0:
        raise ValueError("tolerance_s must be positive and finite")
    if ground_truth.get("format") != "recut_gameplay_ground_truth" or type(ground_truth.get("version")) is not int or ground_truth.get("version") != 1:
        raise ValueError("Expected recut_gameplay_ground_truth version=1")
    raw_games = ground_truth.get("games")
    if not isinstance(raw_games, list):
        raise ValueError("Ground-truth games must be an array")
    annotated = ground_truth.get("annotated_range", [0.0, analysis.duration])
    if not isinstance(annotated, list) or len(annotated) != 2:
        raise ValueError("annotated_range must contain [start_s, end_s]")
    first, last = (_time(value, "annotated_range") for value in annotated)
    if first is None or last is None or first >= last or last > analysis.duration:
        raise ValueError("annotated_range must be a nonempty interval within the analyzed video")
    complete = ground_truth.get("complete", False)
    if not isinstance(complete, bool):
        raise ValueError("complete must be a boolean")
    truth: list[dict[str, Any]] = []
    for index, item in enumerate(raw_games):
        if not isinstance(item, dict):
            raise ValueError("Each ground-truth game must be an object")
        start, end = _time(item.get("start"), "game.start"), _time(item.get("end"), "game.end")
        result = GameResult(item.get("result"))
        if start is None or end is None or not first <= start < end <= last or result is GameResult.UNKNOWN:
            raise ValueError("Ground truth requires known WIN/LOSS and both boundaries inside annotated_range")
        truth.append({"start": start, "end": end, "result": result.value, "index": index + 1})
    ordered_truth = sorted(truth, key=lambda game: game["start"])
    if any(left["end"] > right["start"] for left, right in zip(ordered_truth, ordered_truth[1:])):
        raise ValueError("Ground-truth games must not overlap or duplicate each other")
    predicted = [game.to_mapping() for game in analysis.games if
                 any(boundary is not None for boundary in (game.start, game.end))
                 and all(boundary is None or first <= boundary <= last for boundary in (game.start, game.end))]
    matched = _match(predicted, truth, tolerance_s)
    correct = 0
    loss_tp = loss_fp = nonloss_misclassified = 0
    start_errors: list[float] = []
    end_errors: list[float] = []
    confusion = {kind.value: {pred.value: 0 for pred in GameResult} for kind in (GameResult.WIN, GameResult.LOSS)}
    predicted_matches = set(matched.values())
    for truth_index, predicted_index in matched.items():
        expected, actual = truth[truth_index], predicted[predicted_index]
        confusion[expected["result"]][actual["result"]] += 1
        correct += actual["result"] == expected["result"]
        if actual["result"] == GameResult.LOSS.value:
            if expected["result"] == GameResult.LOSS.value:
                loss_tp += 1
            else:
                loss_fp += 1
                nonloss_misclassified += 1
        if actual["start"] is not None:
            start_errors.append(abs(actual["start"] - expected["start"]))
        if actual["end"] is not None:
            end_errors.append(abs(actual["end"] - expected["end"]))
    loss_fp += sum(item["result"] == GameResult.LOSS.value for index, item in enumerate(predicted) if index not in predicted_matches)
    actual_losses = sum(item["result"] == GameResult.LOSS.value for item in truth)
    actual_wins = len(truth) - actual_losses

    complete_predicted = [item for item in predicted if item["start"] is not None and item["end"] is not None]
    complete_matched = _match(complete_predicted, truth, tolerance_s)
    complete_correct = sum(complete_predicted[pi]["result"] == truth[ti]["result"] for ti, pi in complete_matched.items())
    complete_loss_tp = sum(complete_predicted[pi]["result"] == truth[ti]["result"] == GameResult.LOSS.value
                           for ti, pi in complete_matched.items())
    complete_predicted_losses = sum(item["result"] == GameResult.LOSS.value for item in complete_predicted)
    complete_false_losses = complete_predicted_losses - complete_loss_tp

    return {
        "format": "recut_gameplay_evaluation", "version": 1, "tolerance_s": tolerance_s,
        "annotated_range": [first, last], "annotations_complete": complete,
        "provisional": not complete, "ground_truth_games": len(truth), "predicted_games": len(predicted),
        "matched_games": len(matched), "game_detection_precision": _ratio(len(matched), len(predicted)),
        "game_detection_recall": _ratio(len(matched), len(truth)),
        "win_loss_accuracy": _ratio(correct, len(matched)), "confusion": confusion,
        "loss_recall": _ratio(loss_tp, actual_losses), "loss_precision": _ratio(loss_tp, loss_tp + loss_fp),
        "false_positive_loss_count": loss_fp,
        "false_positive_loss_fraction": _ratio(loss_fp, loss_tp + loss_fp),
        "false_positive_loss_rate": _ratio(nonloss_misclassified, actual_wins),
        "start_timestamp_error": _timestamp_metrics(start_errors), "end_timestamp_error": _timestamp_metrics(end_errors),
        "complete_predicted_games": len(complete_predicted), "complete_matched_games": len(complete_matched),
        "complete_game_detection_precision": _ratio(len(complete_matched), len(complete_predicted)),
        "complete_game_detection_recall": _ratio(len(complete_matched), len(truth)),
        "complete_win_loss_accuracy": _ratio(complete_correct, len(complete_matched)),
        "complete_loss_recall": _ratio(complete_loss_tp, actual_losses),
        "complete_loss_precision": _ratio(complete_loss_tp, complete_predicted_losses),
        "complete_false_positive_loss_count": complete_false_losses,
        "complete_game_comparisons": _game_comparisons(complete_predicted, truth, complete_matched, tolerance_s),
        "game_comparisons": _game_comparisons(predicted, truth, matched, tolerance_s),
        "unmatched_games": [{**_prediction_summary(item), "status": "FALSE_POSITIVE"}
                            for index, item in enumerate(predicted) if index not in predicted_matches],
        **_event_metrics(analysis, truth, first, last, tolerance_s),
        "incomplete_predictions": sum(item["start"] is None or item["end"] is None for item in predicted),
        "definitions": {
            "false_positive_loss_fraction": "Wrong or unmatched predicted LOSS / all predicted LOSS",
            "false_positive_loss_rate": "Annotated WIN incorrectly classified LOSS / all annotated WIN",
            "win_loss_accuracy": "Correct result / matched games; UNKNOWN counts as incorrect",
            "matched_games": "Legacy metric: all available boundaries match; incomplete predictions can match",
            "complete_game_detection_recall": "Both start and end within tolerance / all annotated games",
            "complete_loss_recall": "Correct LOSS with both matched boundaries / all annotated LOSS",
            "complete_loss_precision": "Correct LOSS with both matched boundaries / complete predicted LOSS",
            "result_detection_recall": "Result anchor observed within tolerance, independent of VICTORY/DEFEAT label",
            "victory_recall": "Correctly classified VICTORY anchors / annotated WIN results",
            "defeat_recall": "Correctly classified DEFEAT anchors / annotated LOSS results",
            "false_positive_event_count": "Unmatched temporal anchors; classified errors are separately visible per class",
            "result_timestamp_error": "Absolute timestamp errors of pooled result-anchor matches, independent of classification",
        },
    }
