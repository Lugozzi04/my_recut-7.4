from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from analysis.gameplay.cache import CACHE_VERSION, GameplayCache, atomic_write_json, cache_key, video_identity
from analysis.gameplay.models import GameEvent, GameEventType, GameplayAnalysisResult


class GameplayCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.cache = GameplayCache(self.root / "cache locale è 🎮")
        self.video = self.root / "VOD locale.mp4"
        self.video.write_bytes(b"source video bytes")
        self.result = GameplayAnalysisResult(
            str(self.video), 60.0, [GameEvent(10.0, GameEventType.VS_SCREEN, 0.96)], [],
            "test-detector-v1", settings={"coarse_fps": 1.0}, metadata={"cache_hit": False},
        )
        self.key = cache_key(video_identity(self.video), {"pack": "v1"}, self.result.settings)

    def payload(self) -> dict:
        return {"format": "recut_gameplay_cache", "version": CACHE_VERSION,
                "key": self.key, "result": self.result.to_mapping()}

    def test_missing_cache_does_not_create_writable_directories(self) -> None:
        self.assertIsNone(self.cache.load(self.key))
        self.assertFalse(self.cache.directory.exists())

    def test_save_load_round_trip_retains_unicode_and_separate_mutable_state(self) -> None:
        self.cache.save(self.key, self.result)
        restored = self.cache.load(self.key)
        self.assertEqual(restored, self.result)
        restored.metadata["cache_hit"] = True
        self.assertFalse(self.result.metadata["cache_hit"])
        self.assertEqual(self.cache.load(self.key), self.result)
        self.assertEqual(list(self.cache.directory.glob("*.tmp")), [])

    def test_malformed_json_invalid_envelope_and_invalid_result_are_cache_misses(self) -> None:
        self.cache.directory.mkdir(parents=True)
        file = self.cache.directory / (self.key + ".json")
        bad_payloads = [None, [], {}, {**self.payload(), "format": "wrong"},
                        {**self.payload(), "version": CACHE_VERSION + 1},
                        {**self.payload(), "key": "different-key"},
                        {**self.payload(), "result": {"duration": "bad"}}]
        for payload in bad_payloads:
            file.write_text(json.dumps(payload), encoding="utf-8")
            with self.subTest(payload=payload):
                self.assertIsNone(self.cache.load(self.key))
        file.write_text("{incomplete", encoding="utf-8")
        self.assertIsNone(self.cache.load(self.key))

    def test_boolean_or_float_cache_version_is_not_accepted_as_integer_version(self) -> None:
        for version in (True, 1.0):
            payload = {**self.payload(), "version": version}
            atomic_write_json(self.cache.directory / (self.key + ".json"), payload)
            with self.subTest(version=version):
                self.assertIsNone(self.cache.load(self.key))

    def test_cached_out_of_range_event_is_not_accepted(self) -> None:
        payload = self.payload()
        payload["result"]["events"][0]["timestamp"] = 61.0
        atomic_write_json(self.cache.directory / (self.key + ".json"), payload)
        self.assertIsNone(self.cache.load(self.key))

    def test_cached_out_of_range_game_boundary_is_not_accepted(self) -> None:
        payload = self.payload()
        payload["result"]["games"] = [{
            "id": "bad-game", "index": 1, "start": 10.0, "end": 61.0,
            "result": "UNKNOWN", "markers": [],
        }]
        atomic_write_json(self.cache.directory / (self.key + ".json"), payload)
        self.assertIsNone(self.cache.load(self.key))

    def test_cache_key_is_order_independent_and_tracks_settings_detector_and_identity(self) -> None:
        identity = video_identity(self.video)
        original = cache_key(identity, {"pack": "v1", "method": "template"}, {"fps": 1.0, "scale": 1.0})
        reordered = cache_key(dict(reversed(list(identity.items()))),
                              {"method": "template", "pack": "v1"}, {"scale": 1.0, "fps": 1.0})
        self.assertEqual(original, reordered)
        variants = [cache_key(identity, {"pack": "v2", "method": "template"}, {"fps": 1.0, "scale": 1.0}),
                    cache_key(identity, {"pack": "v1", "method": "template"}, {"fps": 2.0, "scale": 1.0}),
                    cache_key({**identity, "mtime_ns": identity["mtime_ns"] + 1},
                              {"pack": "v1", "method": "template"}, {"fps": 1.0, "scale": 1.0})]
        self.assertTrue(all(key != original for key in variants))
        self.assertEqual(len({original, *variants}), 4)

    def test_cache_key_rejects_non_finite_configuration(self) -> None:
        with self.assertRaises(ValueError):
            cache_key(video_identity(self.video), {}, {"fps": float("nan")})

    def test_video_identity_includes_source_bytes_even_when_size_and_mtime_are_preserved(self) -> None:
        before = video_identity(self.video)
        stat = self.video.stat()
        self.video.write_bytes(b"changed vide bytes")
        self.assertEqual(self.video.stat().st_size, stat.st_size)
        os.utime(self.video, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        after = video_identity(self.video)
        self.assertEqual(before["size"], after["size"])
        self.assertEqual(before["mtime_ns"], after["mtime_ns"])
        self.assertNotEqual(before["fingerprint"], after["fingerprint"])
        self.assertNotEqual(cache_key(before, {}, {}), cache_key(after, {}, {}))

    def test_atomic_writer_stages_in_destination_directory_and_replaces_complete_json(self) -> None:
        destination = self.root / "another output directory" / "analisi è pronta 🎮.json"
        seen: list[Path] = []
        real_replace = os.replace

        def replace(source: str | Path, target: str | Path) -> None:
            temporary = Path(source)
            self.assertEqual(temporary.parent, destination.parent)
            self.assertEqual(Path(target), destination)
            self.assertEqual(json.loads(temporary.read_text(encoding="utf-8")), {"result": "ok è 🎮"})
            seen.append(temporary)
            real_replace(source, target)

        with patch("analysis.gameplay.cache.os.replace", side_effect=replace):
            result = atomic_write_json(destination, {"result": "ok è 🎮"})
        self.assertEqual(result, destination)
        self.assertEqual(len(seen), 1)
        self.assertFalse(seen[0].exists())
        self.assertEqual(json.loads(destination.read_text(encoding="utf-8")), {"result": "ok è 🎮"})

    def test_replace_failure_preserves_previous_artifact_and_cleans_temporary_file(self) -> None:
        destination = self.root / "analysis.json"
        destination.write_bytes(b"previous complete diagnostic artifact")
        with patch("analysis.gameplay.cache.os.replace", side_effect=OSError("simulated crash before publish")):
            with self.assertRaises(OSError):
                atomic_write_json(destination, {"new": "analysis"})
        self.assertEqual(destination.read_bytes(), b"previous complete diagnostic artifact")
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_serialization_failure_preserves_previous_artifact_and_cleans_temporary_file(self) -> None:
        destination = self.root / "analysis.json"
        destination.write_bytes(b"previous complete diagnostic artifact")
        with self.assertRaises(ValueError):
            atomic_write_json(destination, {"bad_score": float("nan")})
        self.assertEqual(destination.read_bytes(), b"previous complete diagnostic artifact")
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_flush_failure_does_not_replace_previous_artifact(self) -> None:
        destination = self.root / "analysis.json"
        destination.write_bytes(b"previous complete diagnostic artifact")
        with patch("analysis.gameplay.cache.os.fsync", side_effect=OSError("flush failed")):
            with self.assertRaises(OSError):
                atomic_write_json(destination, {"new": "analysis"})
        self.assertEqual(destination.read_bytes(), b"previous complete diagnostic artifact")
        self.assertEqual(list(self.root.glob(".*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
