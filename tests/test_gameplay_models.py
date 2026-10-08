from __future__ import annotations

import json
import math
import unittest

from analysis.gameplay.models import (
    DetectionSource,
    GameEvent,
    GameEventType,
    GameResult,
    GameSegment,
    GameplayAnalysisResult,
    GameplayFormatError,
)


class GameplayModelTests(unittest.TestCase):
    def event(self) -> GameEvent:
        return GameEvent(12.5, GameEventType.DEFEAT, 0.94, metadata={"template": {"id": "defeat_01"}})

    def game(self) -> GameSegment:
        return GameSegment(
            "game-1", 1, None, 12.5, GameResult.LOSS,
            end_confidence=0.94, result_confidence=0.94,
            end_source=DetectionSource.TEMPLATE_MATCHING,
            result_source=DetectionSource.TEMPLATE_MATCHING,
            markers=[self.event()], metadata={"issues": ["missing_start"]},
        )

    def test_event_round_trip_keeps_score_and_independent_metadata(self) -> None:
        original = self.event()
        payload = original.to_mapping()
        restored = GameEvent.from_mapping(payload)
        self.assertEqual(original, restored)
        payload["metadata"]["template"]["id"] = "edited"
        restored.metadata["template"]["id"] = "another edit"
        self.assertEqual(original.metadata["template"]["id"], "defeat_01")
        self.assertEqual(original.confidence, 0.94)

    def test_complete_result_round_trip_preserves_unknown_boundary_and_unicode(self) -> None:
        original = GameplayAnalysisResult(
            "D:/VOD/partita è pronta 🎮.mp4", 30.0, [self.event()], [self.game()],
            "template-v1", settings={"fps": 1.0}, metadata={"profile": "hearthstone"},
            warnings=["Missing VS screen"],
        )
        restored = GameplayAnalysisResult.from_mapping(json.loads(json.dumps(original.to_mapping())))
        self.assertEqual(original, restored)
        self.assertIsNone(restored.games[0].start)
        self.assertEqual(restored.games[0].start_confidence, 0.0)
        self.assertFalse(restored.games[0].complete)
        restored.warnings.append("another")
        restored.settings["fps"] = 5.0
        self.assertEqual(original.settings["fps"], 1.0)
        self.assertEqual(len(original.warnings), 1)

    def test_manual_game_override_round_trip(self) -> None:
        game = GameSegment(
            "manual-game", 1, 1.0, 10.0, GameResult.WIN,
            start_confidence=1.0, end_confidence=1.0, result_confidence=1.0,
            start_source=DetectionSource.MANUAL, end_source=DetectionSource.MANUAL,
            result_source=DetectionSource.MANUAL, user_override=True,
        )
        self.assertEqual(GameSegment.from_mapping(game.to_mapping()), game)
        self.assertTrue(game.complete)

    def test_invalid_event_timestamp_or_score_is_rejected(self) -> None:
        for timestamp in (-1.0, math.nan, math.inf, True, "12.0"):
            with self.subTest(timestamp=timestamp), self.assertRaises(GameplayFormatError):
                GameEvent(timestamp, GameEventType.VICTORY, 0.9)
        for confidence in (-0.1, 1.1, math.nan, math.inf, True, "0.9"):
            with self.subTest(confidence=confidence), self.assertRaises(GameplayFormatError):
                GameEvent(1.0, GameEventType.VICTORY, confidence)

    def test_invalid_enum_and_metadata_are_rejected(self) -> None:
        for changed in ({"type": "POWER_LOG"}, {"source": "telemetry"}, {"metadata": []}):
            payload = self.event().to_mapping()
            payload.update(changed)
            with self.subTest(changed=changed), self.assertRaises(GameplayFormatError):
                GameEvent.from_mapping(payload)

    def test_game_cannot_claim_observed_missing_boundary(self) -> None:
        for changed in ({"start_confidence": 0.7}, {"start_source": "template_matching"},
                        {"index": True}, {"index": 0}, {"index": 1.0}):
            payload = self.game().to_mapping()
            payload.update(changed)
            with self.subTest(changed=changed), self.assertRaises(GameplayFormatError):
                GameSegment.from_mapping(payload)

    def test_reversed_boundary_is_rejected(self) -> None:
        with self.assertRaises(GameplayFormatError):
            GameSegment("bad-game", 1, 20.0, 10.0, GameResult.UNKNOWN)

    def test_unknown_schema_and_non_finite_duration_are_rejected(self) -> None:
        result = GameplayAnalysisResult("video.mp4", 30.0, [], [], "template-v1")
        for changed in ({"schema_version": 2}, {"schema_version": True}, {"format": "another"},
                        {"duration": math.nan}, {"warnings": [42]}, {"events": {}}):
            payload = result.to_mapping()
            payload.update(changed)
            with self.subTest(changed=changed), self.assertRaises(GameplayFormatError):
                GameplayAnalysisResult.from_mapping(payload)

    def test_default_mutable_collections_are_not_shared(self) -> None:
        first = GameplayAnalysisResult("one.mp4", 1.0, [], [], "v1")
        second = GameplayAnalysisResult("two.mp4", 1.0, [], [], "v1")
        first.metadata["profile"] = "hearthstone"
        first.settings["fps"] = 5.0
        first.warnings.append("test")
        self.assertEqual(second.metadata, {})
        self.assertEqual(second.settings, {})
        self.assertEqual(second.warnings, [])


if __name__ == "__main__":
    unittest.main()
