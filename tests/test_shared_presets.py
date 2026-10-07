from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from analysis.audio_service import AnalysisCancellation, AnalysisResult
from analysis.classic import compute_classic_cuts, threshold_amp_to_pct, threshold_pct_to_amp
from analysis.cut_engine import Segment
from automation.analyzer import PipelineAnalysisCancelled, PipelineAnalysisError, PipelineProjectAnalyzer
from automation.models import PipelineJob
from core.presets import PresetRepository, default_preset_name, default_presets_catalog, normalize_preset_cfg
from export.settings import ExportSettings


def fixture_analysis(_request, *, cancellation=None, on_progress=None) -> AnalysisResult:
    if cancellation is not None:
        cancellation.raise_if_cancelled()
    return AnalysisResult(
        duration=8.0,
        rms=np.concatenate([
            np.zeros(60, dtype=np.float32),
            np.full(40, 0.003, dtype=np.float32),
            np.full(30, 0.08, dtype=np.float32),
            np.zeros(70, dtype=np.float32),
        ]),
        hop_s=0.04,
        auto_threshold=0.01,
    )


class SharedPresetTests(unittest.TestCase):
    def make_job(self, root: Path, preset: dict | None = None) -> PipelineJob:
        source = root / "source.mp4"
        source.write_bytes(b"media-fixture")
        job = PipelineJob.create(vod_id="preset-test", vod_url="https://example.invalid/vod")
        job.local_source_path = str(source)
        if preset is not None:
            job.metadata["preset"] = preset
        return job

    def test_catalog_matches_all_three_shipped_gui_presets_and_is_independent(self) -> None:
        shipped = Path(__file__).resolve().parents[1] / "presets.json"
        catalog = default_presets_catalog()
        self.assertEqual(catalog, json.loads(shipped.read_text(encoding="utf-8")))
        self.assertEqual(len(catalog), 3)
        catalog["Balanced (Default)"]["threshold_pct"] = 99
        self.assertEqual(default_presets_catalog()["Balanced (Default)"]["threshold_pct"], 6)

    def test_normalization_retains_unknown_fields_aliases_and_custom_defaults(self) -> None:
        raw = {"preroll_s": "0.4", "aggressiveness": 70, "future": {"mode": "new"}}
        normalized = normalize_preset_cfg(raw, defaults={"attack_ms": 321, "threshold_pct": 6})
        self.assertEqual(normalized["pre_pad_s"], 0.4)
        self.assertEqual(normalized["post_pad_s"], 0.71)
        self.assertEqual(normalized["attack_ms"], 321)
        self.assertEqual(normalized["threshold_pct"], 6)
        self.assertIn("preroll_s", normalized)
        self.assertIn("aggressiveness", normalized)
        normalized["future"]["mode"] = "changed"
        self.assertEqual(raw["future"]["mode"], "new")
        explicit = normalize_preset_cfg({**raw, "pre_pad_s": 0.1, "post_pad_s": 0.2})
        self.assertEqual((explicit["pre_pad_s"], explicit["post_pad_s"]), (0.1, 0.2))

    def test_default_name_matches_editor_priority_and_fallback(self) -> None:
        self.assertIsNone(default_preset_name({}))
        self.assertEqual(default_preset_name({"z": {}, "a": {}}), "a")
        self.assertEqual(default_preset_name({"Default": {}, "gameplay (default)": {}}), "gameplay (default)")
        self.assertEqual(default_preset_name({"Voice (Default)": {}, "a": {}}), "Voice (Default)")
        self.assertEqual(default_preset_name(default_presets_catalog()), "Balanced (Default)")

    def test_missing_repository_load_and_resolve_do_not_create_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "uncreated" / "presets.json"
            repo = PresetRepository(path)
            self.assertEqual(repo.load(), default_presets_catalog())
            self.assertEqual(repo.resolve("Natural Speech")["threshold_pct"], 3)
            self.assertFalse(path.parent.exists())

    def test_repository_roundtrip_and_failed_save_preserve_previous_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "presets.json"
            repo = PresetRepository(path)
            catalog = {"My voice": {"threshold_pct": 13, "future": [1, 2], "preroll_s": 0.8}}
            repo.save(catalog)
            before = path.read_bytes()
            self.assertEqual(repo.resolve("My voice")["pre_pad_s"], 0.8)
            self.assertEqual(repo.resolve("My voice")["future"], [1, 2])
            with patch("core.presets.os.replace", side_effect=OSError("disk error")):
                with self.assertRaises(OSError):
                    repo.save({"Other voice": {"threshold_pct": 45}})
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(path.parent.glob(".*.tmp")), [])

    def test_repository_preserves_custom_presets_but_excludes_reserved_manual(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "presets.json"
            path.write_text(json.dumps({"Manual": {}, " mine ": {"threshold_pct": 8}, "invalid": 1}))
            repo = PresetRepository(path)
            self.assertEqual(list(repo.load()), [" mine "])
            with self.assertRaises(KeyError):
                repo.resolve("missing")

    def test_threshold_roundtrip_gain_and_limits(self) -> None:
        for percent in (0, 3, 6, 45, 100):
            amplitude = threshold_pct_to_amp(percent, 0.002, 0.08)
            self.assertEqual(threshold_amp_to_pct(amplitude, 0.002, 0.08), percent)
            with_gain = threshold_pct_to_amp(percent, 0.002, 0.08, gain_db=20, gain_affects_detection=True)
            self.assertAlmostEqual(with_gain, amplitude / 10)
        self.assertEqual(threshold_pct_to_amp(-20, 0.002, 0.08), 0.002)
        self.assertEqual(threshold_amp_to_pct(100, 0.002, 0.08), 100)

    def test_manual_cuts_override_suppressed_auto_fragments(self) -> None:
        rms = np.concatenate([np.zeros(30), np.ones(10), np.zeros(40)])
        cfg = {
            "intensity": 100, "threshold_pct": 50, "pre_pad_s": 0,
            "post_pad_s": 0, "attack_ms": 0, "release_ms": 0,
            "smoothing_mode": "Off", "merge_pauses_ms": 0,
        }
        cuts, keeps = compute_classic_cuts(
            rms, 8.0, 0.1, cfg,
            manual_cuts=[Segment(1.0, 1.5)], suppressed_cuts=[Segment(1.0, 2.0)],
        )
        np.testing.assert_allclose([(cut.start, cut.end) for cut in cuts], [(0.15, 1.5), (2, 2.95), (4.15, 7.95)])
        np.testing.assert_allclose([(keep.start, keep.end) for keep in keeps], [(0, 0.15), (1.5, 2), (2.95, 4.15)])

    def test_low_intensity_uses_only_manual_cuts(self) -> None:
        cuts, keeps = compute_classic_cuts(
            np.zeros(100), 10.0, 0.1, {"intensity": 0},
            manual_cuts=[Segment(2, 3)], suppressed_cuts=[Segment(0, 10)],
        )
        self.assertEqual(cuts, [Segment(2, 3)])
        self.assertEqual(keeps, [Segment(0, 2), Segment(3, 10)])

    def test_pipeline_snapshot_matches_shared_gui_calculation_for_each_preset(self) -> None:
        for name, cfg in default_presets_catalog().items():
            with self.subTest(preset=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                job = self.make_job(root, {"name": name, "config": cfg, "version": 1})
                analysis = fixture_analysis(None)
                expected_cuts, expected_keeps = compute_classic_cuts(
                    analysis.rms, analysis.duration, analysis.hop_s, cfg,
                )
                result = PipelineProjectAnalyzer(analyze_fn=fixture_analysis).analyze(job)
                payload = json.loads(result.path.read_text(encoding="utf-8"))
                expected = lambda segments: [{"start": round(s.start, 6), "end": round(s.end, 6)} for s in segments]
                self.assertEqual(payload["tracks"][0]["cfg"], normalize_preset_cfg(cfg))
                self.assertEqual(payload["tracks"][0]["cuts"], expected(expected_cuts))
                self.assertEqual(payload["tracks"][0]["keeps"], expected(expected_keeps))
                self.assertEqual(payload["global"]["preset_name"], name)
                self.assertEqual(payload["automation"]["preset"]["name"], name)

    def test_snapshot_changes_and_export_settings_invalidate_project_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            cfg = default_presets_catalog()["Balanced (Default)"]
            job = self.make_job(root, {"name": "Balanced (Default)", "config": cfg, "version": 1})
            calls = []

            def analyze(*args, **kwargs):
                calls.append(True)
                return fixture_analysis(*args, **kwargs)

            analyzer = PipelineProjectAnalyzer(analyze_fn=analyze)
            analyzer.analyze(job)
            self.assertTrue(analyzer.analyze(job).reused)
            job.metadata["preset"]["config"]["threshold_pct"] = 12
            self.assertFalse(analyzer.analyze(job).reused)
            job.metadata["preset"]["version"] = 2
            self.assertFalse(analyzer.analyze(job).reused)
            job.metadata["export_settings"] = {"codec": "libx264", "container": "mkv"}
            result = analyzer.analyze(job)
            self.assertFalse(result.reused)
            payload = json.loads(result.path.read_text(encoding="utf-8"))
            self.assertEqual(payload["global"]["export_settings"], ExportSettings.from_mapping(job.metadata["export_settings"]).to_mapping())
            self.assertEqual(len(calls), 4)

    def test_legacy_jobs_keep_auto_threshold_and_legacy_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job = self.make_job(Path(tmp))
            analyzer = PipelineProjectAnalyzer(analyze_fn=fixture_analysis)
            first = analyzer.analyze(job)
            payload = json.loads(first.path.read_text(encoding="utf-8"))
            self.assertNotEqual(payload["tracks"][0]["cfg"]["threshold_pct"], 6)
            self.assertNotIn("analysis_request_signature", payload["automation"])
            self.assertTrue(analyzer.analyze(job).reused)

    def test_project_override_keeps_source_directory_unmodified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            job = self.make_job(root)
            requested = root / "artifacts" / "edit.autocutter"
            job.metadata["delivery"] = {"project_path": str(requested)}
            result = PipelineProjectAnalyzer(analyze_fn=fixture_analysis).analyze(job)
            self.assertEqual(result.path, requested)
            self.assertFalse(root.joinpath("source.autocutter").exists())
            self.assertEqual(Path(job.local_source_path).read_bytes(), b"media-fixture")

    def test_invalid_snapshot_and_source_collision_do_not_start_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            job = self.make_job(root, {"name": "broken"})
            calls = []

            def analyze(*args, **kwargs):
                calls.append(True)
                return fixture_analysis(*args, **kwargs)

            analyzer = PipelineProjectAnalyzer(analyze_fn=analyze)
            with self.assertRaises(PipelineAnalysisError) as caught:
                analyzer.analyze(job)
            self.assertEqual(caught.exception.code, "invalid_preset")
            job.metadata.clear()
            job.metadata["delivery"] = {"project_path": job.local_source_path}
            with self.assertRaises(PipelineAnalysisError) as caught:
                analyzer.analyze(job)
            self.assertEqual(caught.exception.code, "source_collision")
            self.assertEqual(calls, [])

    def test_cancelled_request_does_not_reuse_cached_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job = self.make_job(Path(tmp))
            analyzer = PipelineProjectAnalyzer(analyze_fn=fixture_analysis)
            analyzer.analyze(job)
            cancellation = AnalysisCancellation()
            cancellation.cancel()
            with self.assertRaises(PipelineAnalysisCancelled):
                analyzer.analyze(job, cancellation=cancellation)

    def test_local_source_range_is_retained_as_manual_cuts_and_invalidates_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            cfg = normalize_preset_cfg({"intensity": 0})
            job = self.make_job(root, {"name": "Manual range", "config": cfg, "version": 1})
            job.metadata["source_range"] = {"start_s": 2.0, "end_s": 5.0}
            analyzer = PipelineProjectAnalyzer(analyze_fn=fixture_analysis)
            first = analyzer.analyze(job)
            payload = json.loads(first.path.read_text(encoding="utf-8"))
            self.assertEqual(payload["tracks"][0]["keeps"], [{"start": 2.0, "end": 5.0}])
            self.assertEqual(payload["tracks"][0]["manual_cuts"], [{"start": 0.0, "end": 2.0}, {"start": 5.0, "end": 8.0}])
            self.assertTrue(analyzer.analyze(job).reused)
            job.metadata["source_range"]["start_s"] = 3.0
            self.assertFalse(analyzer.analyze(job).reused)

    def test_invalid_and_outside_source_ranges_are_rejected(self) -> None:
        for source_range in ({"start_s": -1}, {"start_s": 4, "end_s": 3}, {"start_s": 9, "end_s": 12}):
            with self.subTest(source_range=source_range), tempfile.TemporaryDirectory() as tmp:
                job = self.make_job(Path(tmp))
                job.metadata["source_range"] = source_range
                with self.assertRaises(PipelineAnalysisError) as caught:
                    PipelineProjectAnalyzer(analyze_fn=fixture_analysis).analyze(job)
                self.assertEqual(caught.exception.code, "invalid_source_range")


if __name__ == "__main__":
    unittest.main()
