from __future__ import annotations

import unittest

from analysis.gameplay.assembler import GameAssembler, consolidate_events
from analysis.gameplay.models import DetectionSource, GameEvent, GameEventType, GameResult


def event(timestamp: float, kind: GameEventType, confidence: float = 0.95) -> GameEvent:
    return GameEvent(timestamp, kind, confidence)


class GameplayConsolidationTests(unittest.TestCase):
    def test_repeated_detections_keep_earliest_timestamp_and_peak_similarity(self) -> None:
        detections = [event(1117.0, GameEventType.DEFEAT, 0.91),
                      event(1118.0, GameEventType.DEFEAT, 0.97),
                      event(1119.0, GameEventType.DEFEAT, 0.94)]
        merged = consolidate_events(reversed(detections))
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].timestamp, 1117.0)
        self.assertEqual(merged[0].confidence, 0.97)
        self.assertEqual(merged[0].metadata["observation_count"], 3)
        self.assertEqual(merged[0].metadata["last_seen"], 1119.0)
        self.assertEqual(merged[0].metadata["peak_timestamp"], 1118.0)
        self.assertEqual(merged[0].metadata["temporal_span_s"], 2.0)
        self.assertEqual(detections[0].metadata, {})

    def test_reconsolidation_preserves_counts_and_span(self) -> None:
        raw = [event(1.0, GameEventType.VS_SCREEN), event(2.0, GameEventType.VS_SCREEN)]
        merged = consolidate_events(raw)
        again = consolidate_events(merged + [event(3.0, GameEventType.VS_SCREEN)])
        self.assertEqual(again[0].timestamp, 1.0)
        self.assertEqual(again[0].metadata["observation_count"], 3)
        self.assertEqual(again[0].metadata["temporal_span_s"], 2.0)

    def test_different_occurrences_classes_and_sources_remain_distinct(self) -> None:
        observations = [event(1.0, GameEventType.DEFEAT), event(5.0, GameEventType.DEFEAT),
                        event(1.1, GameEventType.VICTORY),
                        GameEvent(1.2, GameEventType.DEFEAT, 1.0, DetectionSource.MANUAL)]
        self.assertEqual(len(consolidate_events(observations)), 4)

    def test_invalid_gap_is_rejected(self) -> None:
        for gap in (-1.0, float("nan"), float("inf"), True):
            with self.subTest(gap=gap), self.assertRaises(ValueError):
                consolidate_events([], max_gap_s=gap)


class GameplayAssemblerTests(unittest.TestCase):
    def test_happy_path_win_loss_win(self) -> None:
        events = [event(134.0, GameEventType.VS_SCREEN, 0.96), event(642.0, GameEventType.VICTORY, 0.99),
                  event(680.0, GameEventType.VS_SCREEN, 0.97), event(1213.0, GameEventType.DEFEAT, 0.98),
                  event(1262.0, GameEventType.VS_SCREEN), event(1791.0, GameEventType.VICTORY)]
        games = GameAssembler().assemble(reversed(events))
        self.assertEqual([game.result for game in games], [GameResult.WIN, GameResult.LOSS, GameResult.WIN])
        self.assertEqual([(game.start, game.end) for game in games],
                         [(134.0, 642.0), (680.0, 1213.0), (1262.0, 1791.0)])
        self.assertEqual([game.index for game in games], [1, 2, 3])
        self.assertEqual(games[0].start_confidence, 0.96)
        self.assertEqual(games[0].end_confidence, 0.99)
        self.assertEqual(games[0].result_confidence, 0.99)
        self.assertEqual([game.id for game in games], [game.id for game in GameAssembler().assemble(events)])

    def test_orphan_result_has_no_invented_start(self) -> None:
        game = GameAssembler().assemble([event(1117.0, GameEventType.DEFEAT, 0.97)])[0]
        self.assertEqual(game.result, GameResult.LOSS)
        self.assertIsNone(game.start)
        self.assertEqual(game.start_confidence, 0.0)
        self.assertIsNone(game.start_source)
        self.assertEqual(game.end, 1117.0)
        self.assertEqual(game.result_confidence, 0.97)
        self.assertIn("missing_start", game.metadata["issues"])

    def test_new_start_does_not_become_previous_game_end(self) -> None:
        games = GameAssembler().assemble([event(10.0, GameEventType.VS_SCREEN),
                                          event(100.0, GameEventType.VS_SCREEN),
                                          event(200.0, GameEventType.VICTORY)])
        self.assertEqual(len(games), 2)
        self.assertIsNone(games[0].end)
        self.assertEqual(games[0].result, GameResult.UNKNOWN)
        self.assertEqual(games[0].end_confidence, 0.0)
        self.assertEqual(games[1].start, 100.0)
        self.assertEqual(games[1].result, GameResult.WIN)

    def test_trailing_start_remains_unknown_with_missing_end(self) -> None:
        game = GameAssembler().assemble([event(100.0, GameEventType.VS_SCREEN)])[0]
        self.assertIsNone(game.end)
        self.assertEqual(game.result, GameResult.UNKNOWN)
        self.assertEqual(game.result_confidence, 0.0)
        self.assertIsNone(game.result_source)

    def test_contradictory_nearby_outcomes_never_choose_loss(self) -> None:
        games = GameAssembler().assemble([event(10.0, GameEventType.VS_SCREEN),
                                          event(100.0, GameEventType.VICTORY, 0.91),
                                          event(102.0, GameEventType.DEFEAT, 0.99)])
        self.assertEqual(len(games), 1)
        self.assertEqual(games[0].result, GameResult.UNKNOWN)
        self.assertEqual(games[0].end, 100.0)
        self.assertEqual(games[0].result_confidence, 0.0)
        self.assertEqual(len(games[0].markers), 3)
        self.assertIn("conflicting_results", games[0].metadata["issues"])

    def test_result_without_next_vs_never_infers_new_start(self) -> None:
        games = GameAssembler().assemble([event(10.0, GameEventType.VS_SCREEN),
                                          event(100.0, GameEventType.VICTORY),
                                          event(500.0, GameEventType.DEFEAT)])
        self.assertEqual(len(games), 2)
        self.assertEqual(games[0].result, GameResult.WIN)
        self.assertEqual(games[1].result, GameResult.LOSS)
        self.assertIsNone(games[1].start)

    def test_new_vs_separates_nearby_results_of_distinct_games(self) -> None:
        games = GameAssembler().assemble([event(10.0, GameEventType.VS_SCREEN),
                                          event(100.0, GameEventType.VICTORY),
                                          event(101.0, GameEventType.VS_SCREEN),
                                          event(103.0, GameEventType.DEFEAT)])
        self.assertEqual([game.result for game in games], [GameResult.WIN, GameResult.LOSS])

    def test_same_timestamp_start_and_result_is_not_safe_classification(self) -> None:
        game = GameAssembler().assemble([event(100.0, GameEventType.DEFEAT),
                                         event(100.0, GameEventType.VS_SCREEN)])[0]
        self.assertEqual(game.result, GameResult.UNKNOWN)
        self.assertEqual((game.start, game.end), (100.0, 100.0))
        self.assertIn("non_positive_duration", game.metadata["issues"])

    def test_game_markers_do_not_alias_detector_metadata(self) -> None:
        original = event(10.0, GameEventType.VS_SCREEN)
        original.metadata["rect"] = [0.1, 0.2]
        game = GameAssembler().assemble([original])[0]
        game.markers[0].metadata["rect"].append(0.3)
        self.assertEqual(original.metadata["rect"], [0.1, 0.2])

    def test_empty_events_and_invalid_conflict_window(self) -> None:
        self.assertEqual(GameAssembler().assemble([]), [])
        with self.assertRaises(ValueError):
            GameAssembler(conflicting_result_window_s=-1.0)

    def test_missing_vs_does_not_shift_following_pair(self) -> None:
        games = GameAssembler().assemble([event(10, GameEventType.VS_SCREEN), event(100, GameEventType.VICTORY),
                                          event(200, GameEventType.DEFEAT), event(250, GameEventType.VS_SCREEN),
                                          event(400, GameEventType.VICTORY)])
        self.assertEqual([(g.start, g.end, g.result.value) for g in games],
                         [(10, 100, "WIN"), (None, 200, "LOSS"), (250, 400, "WIN")])

    def test_same_class_results_never_merge_across_intervening_start(self) -> None:
        games = GameAssembler().assemble([event(10, GameEventType.VS_SCREEN), event(100, GameEventType.VICTORY),
                                          event(100.5, GameEventType.VS_SCREEN), event(101, GameEventType.VICTORY)])
        self.assertEqual([(g.start, g.end) for g in games], [(10, 100), (100.5, 101)])

    def test_starts_never_merge_across_intervening_result(self) -> None:
        games = GameAssembler().assemble([event(10, GameEventType.VS_SCREEN), event(10.5, GameEventType.VICTORY),
                                          event(11, GameEventType.VS_SCREEN), event(100, GameEventType.DEFEAT)])
        self.assertEqual([(g.start, g.end) for g in games], [(10, 10.5), (11, 100)])

    def test_expired_start_becomes_partial_and_never_consumes_later_game(self) -> None:
        games = GameAssembler(maximum_game_duration_s=300).assemble([
            event(10, GameEventType.VS_SCREEN), event(500, GameEventType.DEFEAT),
            event(600, GameEventType.VS_SCREEN), event(800, GameEventType.VICTORY)])
        self.assertEqual([(g.start, g.end, g.result.value) for g in games],
                         [(10, None, "UNKNOWN"), (None, 500, "LOSS"), (600, 800, "WIN")])
        self.assertIn("implausible_result_duration", games[0].metadata["issues"])

    def test_minimum_duration_is_configurable_not_a_universal_game_rule(self) -> None:
        games = GameAssembler(minimum_game_duration_s=20).assemble([
            event(10, GameEventType.VS_SCREEN), event(15, GameEventType.DEFEAT)])
        self.assertEqual([(g.start, g.end) for g in games], [(10, None), (None, 15)])

    def test_invalid_duration_guards(self) -> None:
        for options in ({"maximum_game_duration_s": 0}, {"maximum_game_duration_s": float("nan")},
                        {"minimum_game_duration_s": True}, {"minimum_game_duration_s": 5, "maximum_game_duration_s": 5}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                GameAssembler(**options)


if __name__ == "__main__":
    unittest.main()
