from __future__ import annotations

import unittest

from analysis.gameplay.evaluation import evaluate_analysis
from analysis.gameplay.models import GameEvent, GameEventType, GameResult, GameSegment, GameplayAnalysisResult


class GameplayEvaluationTests(unittest.TestCase):
    def analysis(self, *games) -> GameplayAnalysisResult:
        segments = [GameSegment(str(index), index, start, end, GameResult(result))
                    for index, (start, end, result) in enumerate(games, 1)]
        return GameplayAnalysisResult("fixture.mp4", 120.0, [], segments, "fixture-detector-v1")

    def truth(self, *games, **options):
        return {
            "format": "recut_gameplay_ground_truth", "version": 1, "complete": True,
            "games": [{"start": start, "end": end, "result": result} for start, end, result in games],
            **options,
        }

    def test_one_to_one_matching_maximizes_cardinality(self) -> None:
        # The first prediction fits either game. The second only fits game 1;
        # a greedy nearest-neighbor matcher would incorrectly miss game 2.
        result = evaluate_analysis(
            self.analysis((20, 30, "LOSS"), (10, 20, "WIN")),
            self.truth((10, 20, "WIN"), (30, 40, "LOSS")), tolerance_s=11,
        )
        self.assertEqual(result["matched_games"], 2)
        self.assertEqual(result["game_detection_precision"], 1)
        self.assertEqual(result["game_detection_recall"], 1)
        self.assertEqual(result["win_loss_accuracy"], 1)

    def test_matching_does_not_use_result_to_hide_wrong_classifications(self) -> None:
        result = evaluate_analysis(
            self.analysis((10, 20, "LOSS"), (40, 50, "WIN")),
            self.truth((10, 20, "WIN"), (40, 50, "LOSS")),
        )
        self.assertEqual(result["matched_games"], 2)
        self.assertEqual(result["win_loss_accuracy"], 0)
        self.assertEqual(result["confusion"]["WIN"]["LOSS"], 1)
        self.assertEqual(result["confusion"]["LOSS"]["WIN"], 1)
        self.assertEqual(result["loss_recall"], 0)
        self.assertEqual(result["false_positive_loss_count"], 1)

    def test_duplicate_predictions_cannot_match_one_game_twice(self) -> None:
        result = evaluate_analysis(
            self.analysis((10, 20, "WIN"), (10.1, 20.1, "WIN")), self.truth((10, 20, "WIN")),
        )
        self.assertEqual(result["matched_games"], 1)
        self.assertEqual(result["game_detection_precision"], 0.5)
        self.assertEqual(result["game_detection_recall"], 1)

    def test_incomplete_game_matches_only_its_known_boundary(self) -> None:
        result = evaluate_analysis(self.analysis((None, 20.5, "LOSS")), self.truth((10, 20, "LOSS")))
        self.assertEqual(result["matched_games"], 1)
        self.assertEqual(result["incomplete_predictions"], 1)
        self.assertEqual(result["start_timestamp_error"], {
            "count": 0, "mean_absolute_error_s": None, "median_absolute_error_s": None, "max_absolute_error_s": None,
        })
        self.assertEqual(result["end_timestamp_error"]["mean_absolute_error_s"], 0.5)
        self.assertEqual(result["loss_recall"], 1)
        self.assertEqual(result["complete_loss_recall"], 0)
        self.assertIsNone(result["complete_loss_precision"])
        self.assertEqual(result["complete_false_positive_loss_count"], 0)

    def test_both_known_boundaries_must_be_within_tolerance(self) -> None:
        result = evaluate_analysis(self.analysis((10, 90, "LOSS")), self.truth((10, 20, "LOSS")))
        self.assertEqual(result["matched_games"], 0)
        self.assertEqual(result["loss_recall"], 0)
        self.assertEqual(result["false_positive_loss_count"], 1)

    def test_loss_metrics_include_unmatched_predictions_and_missed_losses(self) -> None:
        result = evaluate_analysis(
            self.analysis((10, 20, "LOSS"), (40, 50, "LOSS"), (70, 80, "WIN"), (100, 110, "LOSS")),
            self.truth((10, 20, "WIN"), (40, 50, "LOSS"), (70, 80, "LOSS")),
        )
        self.assertEqual(result["matched_games"], 3)
        self.assertEqual(result["game_detection_precision"], 0.75)
        self.assertEqual(result["game_detection_recall"], 1)
        self.assertAlmostEqual(result["win_loss_accuracy"], 1 / 3)
        self.assertEqual(result["loss_recall"], 0.5)
        self.assertAlmostEqual(result["loss_precision"], 1 / 3)
        self.assertEqual(result["false_positive_loss_count"], 2)
        self.assertEqual(result["complete_loss_recall"], 0.5)
        self.assertAlmostEqual(result["complete_loss_precision"], 1 / 3)
        self.assertAlmostEqual(result["complete_win_loss_accuracy"], 1 / 3)
        self.assertEqual(result["complete_false_positive_loss_count"], 2)
        self.assertAlmostEqual(result["false_positive_loss_fraction"], 2 / 3)
        self.assertEqual(result["false_positive_loss_rate"], 1)

    def test_unknown_prediction_counts_as_wrong_result(self) -> None:
        result = evaluate_analysis(self.analysis((10, 20, "UNKNOWN")), self.truth((10, 20, "WIN")))
        self.assertEqual(result["matched_games"], 1)
        self.assertEqual(result["confusion"]["WIN"]["UNKNOWN"], 1)
        self.assertEqual(result["win_loss_accuracy"], 0)

    def test_annotated_range_excludes_predictions_outside_it(self) -> None:
        result = evaluate_analysis(
            self.analysis((10, 20, "LOSS"), (50, 60, "WIN"), (None, 70, "LOSS"), (100, 110, "LOSS")),
            self.truth((50, 60, "WIN"), annotated_range=[40, 90]),
        )
        self.assertEqual(result["predicted_games"], 2)
        self.assertEqual(result["matched_games"], 1)
        self.assertEqual(result["false_positive_loss_count"], 1)
        self.assertEqual(result["incomplete_predictions"], 1)

    def test_games_crossing_annotation_edges_do_not_become_false_losses(self) -> None:
        result = evaluate_analysis(
            self.analysis((20, 50, "LOSS"), (50, 60, "WIN"), (80, 110, "LOSS"), (None, 70, "LOSS")),
            self.truth((50, 60, "WIN"), annotated_range=[40, 90]),
        )
        self.assertEqual(result["predicted_games"], 2)
        self.assertEqual(result["matched_games"], 1)
        self.assertEqual(result["false_positive_loss_count"], 1)

    def test_duplicate_or_overlapping_ground_truth_cannot_inflate_missed_games(self) -> None:
        for games in (((10, 20, "WIN"), (10, 20, "LOSS")), ((10, 20, "WIN"), (15, 25, "LOSS"))):
            with self.subTest(games=games), self.assertRaisesRegex(ValueError, "overlap"):
                evaluate_analysis(self.analysis(), self.truth(*games))

    def test_empty_metrics_are_undefined_and_partial_annotations_are_provisional(self) -> None:
        result = evaluate_analysis(self.analysis(), self.truth(complete=False))
        self.assertTrue(result["provisional"])
        self.assertFalse(result["annotations_complete"])
        for name in ("game_detection_precision", "game_detection_recall", "win_loss_accuracy", "loss_recall",
                     "loss_precision", "false_positive_loss_rate", "false_positive_loss_fraction"):
            self.assertIsNone(result[name])

    def with_events(self, games, *events):
        analysis = self.analysis(*games)
        analysis.events = [GameEvent(timestamp, GameEventType(kind), 0.95) for timestamp, kind in events]
        return analysis

    def test_anchor_recalls_include_missing_vs_and_missing_results(self) -> None:
        analysis = self.with_events(
            [(None, 20, "WIN"), (40, None, "UNKNOWN"), (70, 80, "LOSS")],
            (20, "VICTORY"), (40, "VS_SCREEN"), (70, "VS_SCREEN"), (80, "DEFEAT"),
        )
        result = evaluate_analysis(analysis, self.truth((10, 20, "WIN"), (40, 50, "WIN"), (70, 80, "LOSS")))
        self.assertAlmostEqual(result["vs_recall"], 2 / 3)
        self.assertAlmostEqual(result["result_detection_recall"], 2 / 3)
        self.assertEqual(result["victory_recall"], 0.5)
        self.assertEqual(result["defeat_recall"], 1)
        self.assertEqual(result["event_comparisons"][0]["vs"]["status"], "FALSE_NEGATIVE")
        self.assertEqual(result["event_comparisons"][1]["result"]["status"], "FALSE_NEGATIVE")
        self.assertEqual(result["game_comparisons"][0]["status"], "PARTIAL")
        self.assertEqual(result["game_comparisons"][1]["status"], "PARTIAL")
        self.assertEqual(result["complete_matched_games"], 1)
        self.assertAlmostEqual(result["complete_game_detection_recall"], 1 / 3)
        self.assertEqual(result["matched_games"], 3)  # Documented backward-compatible metric.

    def test_result_anchor_matching_ignores_class_for_confusion(self) -> None:
        analysis = self.with_events(
            [(10, 20, "LOSS"), (40, 50, "WIN")],
            (10, "VS_SCREEN"), (20.2, "DEFEAT"), (40, "VS_SCREEN"), (50.3, "VICTORY"),
        )
        result = evaluate_analysis(analysis, self.truth((10, 20, "WIN"), (40, 50, "LOSS")))
        self.assertEqual(result["result_detection_recall"], 1)
        self.assertEqual(result["result_event_accuracy"], 0)
        self.assertEqual(result["victory_recall"], 0)
        self.assertEqual(result["defeat_recall"], 0)
        self.assertEqual(result["result_event_confusion"]["VICTORY"]["DEFEAT"], 1)
        self.assertEqual(result["result_event_confusion"]["DEFEAT"]["VICTORY"], 1)
        self.assertEqual(result["false_positive_event_count"], 0)
        self.assertEqual(result["false_positive_defeat_event_count"], 1)
        self.assertEqual(result["event_comparisons"][0]["result"]["status"], "MISCLASSIFIED")
        self.assertEqual(result["game_comparisons"][0]["status"], "MISCLASSIFIED")

    def test_duplicate_events_cannot_inflate_recall_and_are_reported_false_positive(self) -> None:
        analysis = self.with_events([(10, 20, "LOSS")], (10, "VS_SCREEN"), (10.2, "VS_SCREEN"),
                                    (20, "DEFEAT"), (20.1, "DEFEAT"), (90, "VICTORY"))
        result = evaluate_analysis(analysis, self.truth((10, 20, "LOSS")))
        self.assertEqual(result["vs_recall"], 1)
        self.assertEqual(result["defeat_recall"], 1)
        self.assertEqual(result["false_positive_event_count"], 3)
        self.assertEqual(result["false_positive_result_event_count"], 2)
        self.assertEqual(result["false_positive_defeat_event_count"], 1)
        self.assertEqual(result["event_detection"]["VS_SCREEN"]["precision"], 0.5)
        self.assertEqual(len(result["unmatched_events"]), 3)
        self.assertTrue(all(item["status"] == "FALSE_POSITIVE" for item in result["unmatched_events"]))

    def test_event_matching_maximizes_cardinality_independent_of_input_order(self) -> None:
        analysis = self.with_events([], (30, "VICTORY"), (20, "DEFEAT"))
        result = evaluate_analysis(analysis, self.truth((10, 20, "LOSS"), (30, 40, "WIN")), tolerance_s=11)
        self.assertEqual(result["result_detection_recall"], 1)
        self.assertEqual(result["result_event_accuracy"], 1)

    def test_raw_event_metrics_do_not_infer_events_from_games(self) -> None:
        result = evaluate_analysis(self.analysis((10, 20, "WIN")), self.truth((10, 20, "WIN")))
        self.assertEqual(result["complete_game_detection_recall"], 1)
        self.assertEqual(result["vs_recall"], 0)
        self.assertEqual(result["result_detection_recall"], 0)
        self.assertEqual(result["event_detection"]["VICTORY"]["false_negative"], 1)

    def test_anchors_outside_annotated_range_do_not_enter_metrics(self) -> None:
        analysis = self.with_events([(50, 60, "WIN")], (10, "DEFEAT"), (50, "VS_SCREEN"),
                                    (60, "VICTORY"), (100, "DEFEAT"))
        result = evaluate_analysis(analysis, self.truth((50, 60, "WIN"), annotated_range=[40, 90], complete=False))
        self.assertTrue(result["provisional"])
        self.assertEqual(result["false_positive_event_count"], 0)
        self.assertEqual(result["false_positive_defeat_event_count"], 0)
        self.assertIsNone(result["defeat_recall"])
        self.assertEqual(result["event_detection"]["DEFEAT"]["predicted"], 0)

    def test_wrong_pairing_remains_unmatched_despite_correct_individual_events(self) -> None:
        analysis = self.with_events([(10, 50, "LOSS")], (10, "VS_SCREEN"), (20, "VICTORY"),
                                    (40, "VS_SCREEN"), (50, "DEFEAT"))
        result = evaluate_analysis(analysis, self.truth((10, 20, "WIN"), (40, 50, "LOSS")))
        self.assertEqual(result["complete_matched_games"], 0)
        self.assertEqual(result["complete_game_detection_recall"], 0)
        self.assertEqual(result["vs_recall"], 1)
        self.assertEqual(result["result_detection_recall"], 1)
        self.assertEqual(result["false_positive_loss_count"], 1)
        self.assertEqual(result["game_comparisons"][0]["status"], "FALSE_NEGATIVE")
        self.assertTrue(result["game_comparisons"][0]["pairing_mismatch"])
        self.assertEqual(result["game_comparisons"][0]["nearby_start_error_s"], 0)
        self.assertEqual(result["game_comparisons"][0]["nearby_result_error_s"], 30)
        self.assertEqual(result["unmatched_games"][0]["status"], "FALSE_POSITIVE")

    def test_competing_match_is_not_misreported_as_wrong_pairing(self) -> None:
        result = evaluate_analysis(self.analysis((20, 30, "WIN")), self.truth((10, 20, "WIN"), (30, 40, "WIN")),
                                   tolerance_s=11)
        missed = next(item for item in result["game_comparisons"] if item["status"] == "FALSE_NEGATIVE")
        self.assertIn("nearby_prediction", missed)
        self.assertFalse(missed["pairing_mismatch"])

    def test_timestamp_medians_and_game_comparison_errors(self) -> None:
        analysis = self.with_events([(10.2, 20.6, "WIN"), (40.4, 51, "LOSS"), (70.9, 83, "WIN")],
                                    (10.2, "VS_SCREEN"), (20.6, "VICTORY"), (40.4, "VS_SCREEN"),
                                    (51, "DEFEAT"), (70.9, "VS_SCREEN"), (83, "VICTORY"))
        result = evaluate_analysis(analysis, self.truth((10, 20, "WIN"), (40, 50, "LOSS"), (70, 80, "WIN")))
        self.assertAlmostEqual(result["start_timestamp_error"]["median_absolute_error_s"], 0.4)
        self.assertAlmostEqual(result["result_timestamp_error"]["median_absolute_error_s"], 1)
        self.assertAlmostEqual(result["result_timestamp_error"]["mean_absolute_error_s"], 4.6 / 3)
        self.assertAlmostEqual(result["result_timestamp_error"]["max_absolute_error_s"], 3)
        self.assertAlmostEqual(result["game_comparisons"][0]["start_error_s"], 0.2)
        self.assertAlmostEqual(result["game_comparisons"][0]["result_error_s"], 0.6)
        self.assertTrue(all(item["status"] == "OK" for item in result["game_comparisons"]))

    def test_incomplete_predictions_do_not_steal_strict_complete_matches(self) -> None:
        result = evaluate_analysis(self.analysis((10, 20, "LOSS"), (None, 20, "LOSS")), self.truth((10, 20, "LOSS")))
        self.assertEqual(result["complete_predicted_games"], 1)
        self.assertEqual(result["complete_matched_games"], 1)
        self.assertEqual(result["complete_game_detection_precision"], 1)
        self.assertEqual(result["complete_game_detection_recall"], 1)
        self.assertEqual(result["complete_loss_recall"], 1)
        self.assertEqual(result["complete_loss_precision"], 1)
        self.assertEqual(result["complete_game_comparisons"][0]["status"], "OK")

    def test_empty_event_metrics_are_undefined_and_comparisons_empty(self) -> None:
        result = evaluate_analysis(self.analysis(), self.truth())
        for key in ("vs_recall", "result_detection_recall", "victory_recall", "defeat_recall",
                    "result_event_accuracy", "complete_game_detection_precision", "complete_game_detection_recall"):
            self.assertIsNone(result[key])
        self.assertEqual(result["event_comparisons"], [])
        self.assertEqual(result["game_comparisons"], [])
        self.assertIsNone(result["result_timestamp_error"]["median_absolute_error_s"])

    def test_malformed_ground_truth_is_rejected(self) -> None:
        invalid = [
            {"format": "wrong"}, {"version": 2}, {"version": True}, {"games": "wrong"},
            {"games": [None]}, {"games": [{"start": 10, "end": 20, "result": "UNKNOWN"}]},
            {"games": [{"start": None, "end": 20, "result": "LOSS"}]},
            {"games": [{"start": 20, "end": 10, "result": "LOSS"}]},
            {"games": [{"start": True, "end": 20, "result": "LOSS"}]},
            {"games": [{"start": 10, "end": float("inf"), "result": "LOSS"}]},
            {"annotated_range": [0, 121]}, {"annotated_range": [10, 10]},
            {"annotated_range": [None, 120]}, {"annotated_range": [0, True]},
            {"annotated_range": "0..120"}, {"complete": 1},
        ]
        for override in invalid:
            with self.subTest(override=override), self.assertRaises(ValueError):
                evaluate_analysis(self.analysis(), self.truth(**override))

    def test_top_level_annotations_must_be_an_object(self) -> None:
        for annotations in (None, [], "invalid", 1, True):
            with self.subTest(annotations=annotations), self.assertRaises(ValueError):
                evaluate_analysis(self.analysis(), annotations)

    def test_invalid_tolerance_is_rejected(self) -> None:
        for tolerance in (0, -1, float("inf"), float("nan"), True, "bad"):
            with self.subTest(tolerance=tolerance), self.assertRaises(ValueError):
                evaluate_analysis(self.analysis(), self.truth(), tolerance_s=tolerance)


if __name__ == "__main__":
    unittest.main()
