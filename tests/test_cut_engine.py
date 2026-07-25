from __future__ import annotations

import unittest

import numpy as np

from analysis.cut_engine import _apply_attack_release_to_silent_mask


class AttackReleaseTests(unittest.TestCase):
    def test_attack_requires_the_configured_number_of_voice_frames(self) -> None:
        mask = np.array([True, False, False, True], dtype=bool)

        result = _apply_attack_release_to_silent_mask(
            mask,
            hop_s=0.1,
            attack_ms=200.0,
            release_ms=0.0,
        )

        self.assertEqual(result.tolist(), [True, True, False, True])

    def test_release_requires_the_configured_number_of_silent_frames(self) -> None:
        mask = np.array([False, True, True, False], dtype=bool)

        result = _apply_attack_release_to_silent_mask(
            mask,
            hop_s=0.1,
            attack_ms=0.0,
            release_ms=200.0,
        )

        self.assertEqual(result.tolist(), [False, False, True, False])

    def test_interrupted_transition_resets_the_counter(self) -> None:
        mask = np.array([True, False, True, False, False], dtype=bool)

        result = _apply_attack_release_to_silent_mask(
            mask,
            hop_s=0.1,
            attack_ms=200.0,
            release_ms=0.0,
        )

        self.assertEqual(result.tolist(), [True, True, True, True, False])


if __name__ == "__main__":
    unittest.main()
