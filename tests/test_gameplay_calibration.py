from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

import numpy as np

from analysis.audio_service import AnalysisCancellation, AnalysisCancelled
from analysis.gameplay.cache import video_identity
from analysis.gameplay.hearthstone.calibration import CalibrationAnalyzer, CalibrationSettings, annotation_windows
from analysis.gameplay.hearthstone.visual.sampler import SampledFrame, VideoInfo
from analysis.gameplay.hearthstone.visual.templates import NormalizedROI


class Sampler:
    def __init__(self):
        self.calls, self.closed = [], []

    def probe(self, *args, **kwargs):
        return VideoInfo(10., 96, 64)

    def sample(self, *args, **kwargs):
        return self.sample_roi(*args, **kwargs)

    def sample_roi(self, *args, **kwargs):
        self.calls.append(kwargs)
        try:
            for t in np.arange(kwargs['start_s'], kwargs['end_s'], 1 / kwargs['fps']):
                kwargs['cancellation'].raise_if_cancelled()
                yield SampledFrame(float(t), np.zeros((64, 96, 3), dtype=np.uint8))
        finally:
            self.closed.append(kwargs)


class Detector:
    def __init__(self, pack):
        self.pack = SimpleNamespace(reference_size=(96, 64), viewport=NormalizedROI.from_value([0, 0, 1, 1]), path=pack)
        self.calls = 0

    def result_scan_roi(self):
        return NormalizedROI.from_value([0, 0, 1, 1])

    def describe(self):
        return {'pack_fingerprint': 'test-only'}

    def score(self, frame, t, **kwargs):
        self.calls += 1
        return {'timestamp': t, 'best_type': 'DEFEAT', 'classes': {
            'DEFEAT': {'similarity': .95, 'margin': .65, 'template_id': 'defeat/phase.png'},
            'VICTORY': {'similarity': .30, 'margin': -.65, 'template_id': 'victory/anchor.png'},
        }}

    score_roi = score


class GameplayCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.video = self.root / 'calibration è 🎮.mp4'
        self.video.write_bytes(b'fixture')
        self.sampler = Sampler()
        self.detector = Detector(self.root / 'templates.json')
        self.labels = {'role': 'calibration', 'games': [{'start': 1., 'result': 'loss', 'result_time': 5.,
                       'result_visible_start': 4.9, 'result_visible_end': 5.1}],
                       'negative_windows': [{'start': 7., 'end': 8.}]}
        self.settings = CalibrationSettings(result_fps=10, vs_fps=2, negative_fps=2, padding_s=.5, max_debug_frames=0)

    def analyze(self, **kwargs):
        return CalibrationAnalyzer(self.detector, sampler=self.sampler, settings=self.settings).analyze(self.video, self.labels, **kwargs)

    def test_only_annotated_windows_are_decoded_once(self):
        progress = []
        result = self.analyze(on_progress=progress.append)
        self.assertEqual(len(self.sampler.calls), 3)
        self.assertTrue(all(call['end_s'] - call['start_s'] <= 1 for call in self.sampler.calls))
        self.assertEqual(self.sampler.closed, self.sampler.calls)
        self.assertEqual(result['performance']['frames'], self.detector.calls)
        self.assertEqual(progress, [0, 33, 66, 100])
        self.assertFalse(result['policy_enabled'])
        self.assertNotIn('cuts', result)

    def test_only_reviewed_visible_frames_are_positive(self):
        result = self.analyze()
        summary = result['score_distributions']['DEFEAT']
        self.assertGreater(summary['reviewed_positive_scores']['count'], 0)
        self.assertLess(summary['reviewed_positive_scores']['count'], 10)
        self.assertEqual(summary['reviewed_positive_scores']['max'], .95)
        self.assertEqual(summary['positive_best_template_counts']['defeat/phase.png'], summary['reviewed_positive_scores']['count'])
        self.assertGreater(summary['reviewed_negative_or_other_class_scores']['count'], 0)
        self.assertTrue(any(row['label'] == 'UNREVIEWED' for window in result['windows'] for row in window['frames']))

    def test_percentiles_include_only_reviewed_scores(self):
        from analysis.gameplay.hearthstone.calibration import _distribution
        values = _distribution([0., 1., 2., 3., 4.])
        self.assertEqual(values['p10'], .4)
        self.assertEqual(values['median'], 2.)
        self.assertEqual(values['p90'], 3.6)
        self.assertEqual(values['p95'], 3.8)
        self.assertTrue(all(value is None for key, value in _distribution([]).items() if key != 'count'))
        result = self.analyze()
        self.assertEqual(result['score_distributions']['DEFEAT']['reviewed_positive_scores']['p10'], .95)

    def test_unreviewed_window_does_not_become_positive_distribution(self):
        self.labels = {'role': 'calibration', 'games': [{'result': 'loss', 'result_time': 5.}]}
        result = self.analyze()
        self.assertEqual(result['score_distributions']['DEFEAT']['reviewed_positive_scores']['count'], 0)
        self.assertEqual(len(result['score_distributions']['DEFEAT']['annotated_window_peaks']), 1)

    def test_wrong_video_fingerprint_is_rejected_before_decoding(self):
        self.labels['video_fingerprint'] = 'wrong'
        with self.assertRaisesRegex(ValueError, 'another video'):
            self.analyze()
        self.assertFalse(self.sampler.calls)

    def test_matching_video_fingerprint_is_recorded(self):
        self.labels['video_fingerprint'] = video_identity(self.video)['fingerprint']
        self.assertEqual(self.analyze()['video']['fingerprint'], self.labels['video_fingerprint'])

    def test_validation_or_unassigned_labels_are_forbidden(self):
        for role in ('validation', None):
            with self.subTest(role=role), self.assertRaisesRegex(ValueError, 'role=calibration'):
                annotation_windows({**self.labels, 'role': role}, 10, .5)

    def test_bad_annotation_geometry_and_types_are_rejected(self):
        bad = [
            {'games': [{'start': 11}]}, {'games': [{'start': True}]},
            {'games': [{'start': 5, 'result': 'loss', 'result_time': 4}]},
            {'games': [{'result': 'unknown', 'result_time': 5}]},
            {'events': [{'type': 'DEFEAT', 'timestamp': 1, 'visible_start': 0, 'visible_end': 2}]},
            {'events': [{'type': 'DEFEAT'}]}, {'events': [{'type': 'bad', 'timestamp': 1}]},
            {'events': None}, {'negative_windows': [{'start': 1, 'end': 1}]},
            {'games': []},
        ]
        for extra in bad:
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                annotation_windows({'role': 'calibration', **extra}, 10, .5)

    def test_negative_windows_must_not_overlap_anchors(self):
        self.labels['negative_windows'] = [{'start': 4.8, 'end': 5.2}]
        with self.assertRaisesRegex(ValueError, 'overlap'):
            self.analyze()

    def test_invalid_frame_budget_is_rejected_before_decoding(self):
        self.settings = replace(self.settings, max_frames=1)
        with self.assertRaisesRegex(ValueError, 'budget'):
            self.analyze()
        self.assertFalse(self.sampler.calls)

    def test_cancellation_closes_active_sampler(self):
        token = AnalysisCancellation()
        original = self.detector.score_roi
        def cancel(*args, **kwargs):
            result = original(*args, **kwargs)
            token.cancel()
            return result
        self.detector.score_roi = cancel
        with self.assertRaises(AnalysisCancelled):
            self.analyze(cancellation=token)
        self.assertEqual(self.sampler.calls, self.sampler.closed)

    def test_invalid_settings_rejected(self):
        for kwargs in ({'result_fps': float('nan')}, {'negative_fps': 0}, {'padding_s': 6},
                       {'max_frames': True}, {'max_debug_frames': -1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                CalibrationSettings(**kwargs)
