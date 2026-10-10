from __future__ import annotations

from dataclasses import asdict, replace
import unittest

from analysis.gameplay.hearthstone.result_policy import SingleFrameResultPolicy, SingleFrameRule
from analysis.gameplay.models import DetectionSource, GameEvent, GameEventType


# SYNTHETIC gates exercise mechanics, never production calibration parameters.
class GameplaySingleFramePolicyTests(unittest.TestCase):
    def setUp(self):
        self.rule = SingleFrameRule(.98, .30, .80, .10)
        self.policy = SingleFrameResultPolicy(self.rule, self.rule, 'a' * 64, 'synthetic-unit-test-only')
        self.event = GameEvent(1., GameEventType.DEFEAT, .99)
        self.context = [self.row(.98, .82, .25), self.row(1., .99, .25), self.row(1.02, .75, .25)]

    @staticmethod
    def row(t, defeat, victory):
        return {'timestamp': t, 'classes': {'DEFEAT': {'similarity': defeat}, 'VICTORY': {'similarity': victory}}}

    def test_strong_defeat_requires_enabled_policy(self):
        self.assertEqual(SingleFrameResultPolicy().evaluate(self.event, self.context), (False, 'disabled'))
        self.assertEqual(self.policy.evaluate(self.event, self.context), (True, 'calibrated_single_frame_peak'))

    def test_mediocre_single_frame_is_rejected(self):
        self.assertEqual(self.policy.evaluate(replace(self.event, confidence=.95), self.context)[1], 'peak_below_gate')

    def test_isolated_random_high_score_without_neighbor_signal_is_rejected(self):
        rows = [self.row(.98, .35, .25), self.row(1., .99, .25), self.row(1.02, .32, .25)]
        self.assertEqual(self.policy.evaluate(self.event, rows), (False, 'unsupported_isolated_peak'))

    def test_close_competing_class_is_rejected(self):
        self.context[1] = self.row(1., .99, .97)
        self.assertEqual(self.policy.evaluate(self.event, self.context)[1], 'ambiguous_peak')

    def test_single_victory_is_symmetric(self):
        event = replace(self.event, type=GameEventType.VICTORY)
        rows = [self.row(.98, .25, .82), self.row(1., .25, .99), self.row(1.02, .25, .75)]
        self.assertTrue(self.policy.evaluate(event, rows)[0])

    def test_vs_is_never_eligible(self):
        self.assertEqual(self.policy.evaluate(replace(self.event, type=GameEventType.VS_SCREEN), self.context)[1], 'not_a_result')

    def test_both_temporal_sides_are_required(self):
        for rows in (self.context[:2], self.context[1:], [self.context[1]]):
            self.assertEqual(self.policy.evaluate(self.event, rows)[1], 'incomplete_neighborhood')

    def test_replayed_timestamp_is_not_neighbor_evidence(self):
        rows = [self.context[1]] * 3
        self.assertFalse(self.policy.evaluate(self.event, rows)[0])

    def test_remote_neighbors_do_not_support_peak(self):
        rows = [self.row(.5, .85, .25), self.context[1], self.row(1.5, .85, .25)]
        self.assertFalse(self.policy.evaluate(self.event, rows)[0])

    def test_larger_or_equal_neighbor_is_not_a_strict_peak(self):
        for score in (.99, 1.):
            rows = [self.row(.98, score, .25), *self.context[1:]]
            self.assertEqual(self.policy.evaluate(self.event, rows)[1], 'not_strict_local_peak')

    def test_missing_competing_scores_fail_closed(self):
        for i in range(3):
            rows = [self.row(row['timestamp'], row['classes']['DEFEAT']['similarity'], .25) for row in self.context]
            del rows[i]['classes']['VICTORY']
            self.assertFalse(self.policy.evaluate(self.event, rows)[0])

    def test_mismatched_peak_score_fails_closed(self):
        self.context[1] = self.row(1., .98, .25)
        self.assertEqual(self.policy.evaluate(self.event, self.context)[1], 'missing_peak_scores')

    def test_only_one_measured_template_observation_is_eligible(self):
        for event in (replace(self.event, metadata={'observation_count': 2}),
                      replace(self.event, source=DetectionSource.MANUAL)):
            self.assertFalse(self.policy.evaluate(event, self.context)[0])

    def test_rules_are_independent_per_result_class(self):
        policy = replace(self.policy, defeat=None)
        self.assertFalse(policy.evaluate(self.event, self.context)[0])

    def test_configuration_round_trip_and_pack_binding(self):
        raw = {'format': 'recut_single_frame_result_policy', 'version': 1, 'role': 'calibration',
               'pack_fingerprint': 'a' * 64, 'calibration_id': 'synthetic-unit-test-only',
               'VICTORY': asdict(self.rule), 'DEFEAT': asdict(self.rule)}
        self.assertEqual(SingleFrameResultPolicy.from_mapping(raw), self.policy)
        description = {'pack_fingerprint': 'a' * 64, 'ambiguity_margin': .04,
                       'classes': {kind: {'threshold': .92} for kind in ('VICTORY', 'DEFEAT')}}
        self.policy.validate_detector(description)
        with self.assertRaisesRegex(ValueError, 'another template pack'):
            self.policy.validate_detector({**description, 'pack_fingerprint': 'b' * 64})
        with self.assertRaisesRegex(ValueError, 'cannot relax'):
            self.policy.validate_detector({**description, 'ambiguity_margin': .4})
        with self.assertRaisesRegex(ValueError, 'cannot relax'):
            self.policy.validate_detector({**description, 'classes': {kind: {'threshold': 1.} for kind in ('VICTORY', 'DEFEAT')}})

    def test_invalid_and_unprovenanced_configuration_is_rejected(self):
        for kwargs in ({'peak_similarity': float('nan')}, {'neighbor_similarity': .99},
                       {'minimum_class_margin': 0}, {'maximum_neighbor_gap_s': -1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                replace(self.rule, **kwargs)
        with self.assertRaises(ValueError):
            SingleFrameResultPolicy(defeat=self.rule)
        with self.assertRaises(ValueError):
            SingleFrameResultPolicy.from_mapping({'role': 'validation'})
