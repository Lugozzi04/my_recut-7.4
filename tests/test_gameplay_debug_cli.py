"""Headless debug frontend contracts; media matching is tested separately."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from analysis.gameplay.assembler import GameAssembler
from analysis.gameplay.hearthstone import debug_analyze
from analysis.gameplay.hearthstone.visual.sampler import SampledFrame, VideoInfo
from analysis.gameplay.models import GameEvent, GameEventType, GameplayAnalysisResult


class GameplayDebugCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / "CLI è 🎮"
        self.root.mkdir()
        self.video = self.root / "old VOD è 🎮.mp4"
        self.video.write_bytes(b"frontend-test-stub-video")
        self.output = self.root / "diagnostics con spazi"
        self.events = [
            GameEvent(134.2, GameEventType.VS_SCREEN, 0.96),
            GameEvent(642.6, GameEventType.VICTORY, 0.99),
            GameEvent(680.1, GameEventType.VS_SCREEN, 0.97),
            GameEvent(1213.4, GameEventType.DEFEAT, 0.98),
        ]
        self.result = GameplayAnalysisResult(
            str(self.video), 1500, self.events, GameAssembler().assemble(self.events),
            "test-frontend-stub", settings={"coarse_fps": 1}, warnings=["uncalibrated fixture"],
        )
        self.arguments = [str(self.video), "--output-dir", str(self.output)]

    def invoke(self, arguments: list[str], *, result: GameplayAnalysisResult | None = None):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(debug_analyze, "HearthstoneAnalyzer") as factory:
            def analyze(_video, **kwargs):
                kwargs["on_progress"](25)
                return result or self.result
            factory.return_value.analyze.side_effect = analyze
            with redirect_stdout(out), redirect_stderr(err):
                code = debug_analyze.main(arguments)
        return code, out.getvalue(), err.getvalue()

    def test_human_output_lists_games_results_confidence_and_report(self) -> None:
        code, out, err = self.invoke(self.arguments)
        self.assertEqual(code, 0)
        self.assertIn("Game 1: 00:02:14.2 -> 00:10:42.6", out)
        self.assertIn("VICTORY", out)
        self.assertIn("RESULT     WIN", out)
        self.assertIn("RESULT     LOSS", out)
        self.assertIn("confidence=0.980", out)
        self.assertIn("Report:", out)
        self.assertIn("Gameplay analysis: 25%", err)
        self.assertIn("Warning: uncalibrated fixture", err)


    def test_human_unicode_paths_and_warnings_survive_restrictive_windows_encodings(self) -> None:
        from dataclasses import replace
        result = replace(self.result, warnings=["Commento è 🎮"])
        for encoding in ("cp1252", "cp850"):
            with self.subTest(encoding=encoding):
                out_bytes, err_bytes = io.BytesIO(), io.BytesIO()
                out = io.TextIOWrapper(out_bytes, encoding=encoding, errors="strict", write_through=True)
                err = io.TextIOWrapper(err_bytes, encoding=encoding, errors="strict", write_through=True)
                with patch.object(debug_analyze, "HearthstoneAnalyzer") as factory:
                    factory.return_value.analyze.return_value = result
                    with redirect_stdout(out), redirect_stderr(err):
                        code = debug_analyze.main(self.arguments)
                self.assertEqual(code, 0)
                text = out_bytes.getvalue().decode(encoding)
                warnings = err_bytes.getvalue().decode(encoding)
                self.assertIn("Report:", text)
                self.assertIn("RESULT     LOSS", text)
                self.assertIn(r"\U0001f3ae", text)
                self.assertIn("Commento è", warnings)
                self.assertIn(r"\U0001f3ae", warnings)
                self.assertTrue((self.output / "gameplay_analysis.json").is_file())
                out.detach()
                err.detach()

    def test_unicode_error_survives_restrictive_stderr_encoding(self) -> None:
        out_bytes, err_bytes = io.BytesIO(), io.BytesIO()
        out = io.TextIOWrapper(out_bytes, encoding="cp1252", errors="strict", write_through=True)
        err = io.TextIOWrapper(err_bytes, encoding="cp1252", errors="strict", write_through=True)
        with patch.object(debug_analyze, "HearthstoneAnalyzer", side_effect=ValueError(f"Invalid pack 🎮: {self.video}")):
            with redirect_stdout(out), redirect_stderr(err):
                code = debug_analyze.main(self.arguments + ["--json"])
        self.assertEqual(code, 2)
        payload = json.loads(out_bytes.getvalue().decode("cp1252"))
        self.assertIn("🎮", payload["error"])
        self.assertIn(r"\U0001f3ae", err_bytes.getvalue().decode("cp1252"))
        out.detach()
        err.detach()

    def test_json_stdout_is_clean_and_unicode_round_trips(self) -> None:
        code, out, err = self.invoke(self.arguments + ["--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["analysis"]["video_path"], str(self.video))
        self.assertEqual(payload["analysis"]["games"][1]["result"], "LOSS")
        self.assertNotIn("Gameplay analysis", out)
        self.assertIn("Gameplay analysis", err)
        report = Path(payload["report"])
        self.assertEqual(json.loads(report.read_text(encoding="utf-8")), self.result.to_mapping())
        self.assertEqual(list(self.output.glob("*.tmp")), [])

    def test_diagnostic_write_promotes_complete_json_in_same_directory(self) -> None:
        self.output.mkdir()
        target = self.output / "gameplay_analysis.json"
        target.write_text('{"previous":true}', encoding="utf-8")
        observed: list[Path] = []
        original_replace = os.replace

        def promote(source, destination):
            temporary = Path(source)
            observed.append(temporary)
            self.assertEqual(temporary.parent, target.parent)
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"previous": True})
            self.assertEqual(json.loads(temporary.read_text(encoding="utf-8")), self.result.to_mapping())
            original_replace(source, destination)

        with patch("analysis.gameplay.cache.os.replace", side_effect=promote):
            code, _out, _err = self.invoke(self.arguments)
        self.assertEqual(code, 0)
        self.assertEqual(len(observed), 1)
        self.assertFalse(observed[0].exists())
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), self.result.to_mapping())

    def test_failed_atomic_promotion_preserves_previous_report(self) -> None:
        self.output.mkdir()
        target = self.output / "gameplay_analysis.json"
        target.write_text('{"previous":true}', encoding="utf-8")
        with patch("analysis.gameplay.cache.os.replace", side_effect=OSError("disk failure")):
            code, out, err = self.invoke(self.arguments + ["--json"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["exit_code"], 2)
        self.assertIn("disk failure", err)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"previous": True})
        self.assertEqual(list(self.output.glob("*.tmp")), [])


    def test_diagnostic_report_cannot_replace_source_video(self) -> None:
        self.output.mkdir()
        source = self.output / "gameplay_analysis.json"
        original = b"source-media-must-survive"
        source.write_bytes(original)
        arguments = [str(source), "--output-dir", str(self.output), "--json"]
        code, out, _err = self.invoke(arguments)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["exit_code"], 2)
        self.assertEqual(source.read_bytes(), original)

    def test_diagnostic_report_cannot_replace_ground_truth(self) -> None:
        self.output.mkdir()
        labels = self.output / "gameplay_analysis.json"
        original = b'{"games":[]}'
        labels.write_bytes(original)
        with patch.object(debug_analyze, "evaluate_analysis", return_value={"game_detection_precision": 1.0}):
            code, out, _err = self.invoke(self.arguments + ["--ground-truth", str(labels), "--json"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["exit_code"], 2)
        self.assertEqual(labels.read_bytes(), original)

    def test_diagnostic_report_cannot_replace_template_pack(self) -> None:
        self.output.mkdir()
        pack = self.output / "gameplay_analysis.json"
        original = b'{"schema_version":1,"profile":"hearthstone"}'
        pack.write_bytes(original)
        code, out, _err = self.invoke(self.arguments + ["--template-pack", str(pack), "--json"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["exit_code"], 2)
        self.assertEqual(pack.read_bytes(), original)

    def test_ground_truth_evaluation_is_written_separately(self) -> None:
        labels = self.root / "labels.json"
        labels.write_text('{"games":[]}', encoding="utf-8")
        metrics = {"game_detection_precision": 1.0, "loss_recall": 1.0}
        with patch.object(debug_analyze, "evaluate_analysis", return_value=metrics) as evaluate:
            code, out, _err = self.invoke(self.arguments + ["--ground-truth", str(labels), "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["evaluation"], metrics)
        self.assertEqual(json.loads((self.output / "gameplay_evaluation.json").read_text(encoding="utf-8")), metrics)
        evaluate.assert_called_once_with(self.result, {"games": []}, tolerance_s=5.0)

    def test_optional_flags_are_forwarded_without_gui(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with patch.object(debug_analyze, "HearthstoneAnalyzer") as factory:
            factory.return_value.analyze.return_value = self.result
            with redirect_stdout(out), redirect_stderr(err):
                code = debug_analyze.main(self.arguments + ["--no-cache", "--debug-frames", "--max-debug-frames", "7"])
        self.assertEqual(code, 0)
        self.assertFalse(factory.call_args.kwargs["use_cache"])
        kwargs = factory.return_value.analyze.call_args.kwargs
        self.assertEqual(kwargs["debug_frames"], self.output / "debug_frames")
        self.assertEqual(kwargs["max_debug_frames"], 7)

    def test_analyzer_configuration_error_returns_two_and_restores_sigint(self) -> None:
        previous = signal.getsignal(signal.SIGINT)
        out, err = io.StringIO(), io.StringIO()
        with patch.object(debug_analyze, "HearthstoneAnalyzer", side_effect=ValueError("invalid template pack")):
            with redirect_stdout(out), redirect_stderr(err):
                code = debug_analyze.main(self.arguments + ["--json"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out.getvalue()), {"error": "invalid template pack", "exit_code": 2})
        self.assertIn("invalid template pack", err.getvalue())
        self.assertIs(signal.getsignal(signal.SIGINT), previous)
        self.assertFalse(self.output.exists())

    def test_sigint_cancels_and_restores_previous_handler(self) -> None:
        previous = signal.getsignal(signal.SIGINT)
        out, err = io.StringIO(), io.StringIO()
        with patch.object(debug_analyze, "HearthstoneAnalyzer") as factory:
            def interrupt(_video, **kwargs):
                handler = signal.getsignal(signal.SIGINT)
                handler(signal.SIGINT, None)
                kwargs["cancellation"].raise_if_cancelled()
            factory.return_value.analyze.side_effect = interrupt
            with redirect_stdout(out), redirect_stderr(err):
                code = debug_analyze.main(self.arguments)
        self.assertEqual(code, 70)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("cancelled", err.getvalue())
        self.assertIs(signal.getsignal(signal.SIGINT), previous)
        self.assertFalse(self.output.exists())

    def test_keyboard_interrupt_returns_cancelled(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with patch.object(debug_analyze, "HearthstoneAnalyzer") as factory:
            factory.return_value.analyze.side_effect = KeyboardInterrupt
            with redirect_stdout(out), redirect_stderr(err):
                code = debug_analyze.main(self.arguments)
        self.assertEqual(code, 70)
        self.assertIn("cancelled", err.getvalue())

    def test_capture_only_flags_are_rejected_during_analysis(self) -> None:
        for extra in (["--at", "00:01"], ["--name", "anchor"], ["--roi", "0", "0", "1", "1"]):
            with self.subTest(extra=extra):
                code, out, err = self.invoke(self.arguments + list(extra) + ["--json"])
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(out)["exit_code"], 2)
                self.assertIn("only valid with --capture", err)

    def test_missing_capture_arguments_return_two_without_opencv(self) -> None:
        code, out, err = self.invoke(self.arguments + ["--capture", "DEFEAT", "--json"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["exit_code"], 2)
        self.assertIn("requires --at", err)

    def test_capture_name_rejects_traversal_and_extension_patterns(self) -> None:
        for name in ("../foreign", "C:/foreign", "name.png", "name.", "name "):
            with self.subTest(name=name):
                code, _out, err = self.invoke(self.arguments + [
                    "--capture", "DEFEAT", "--at", "1", "--roi", "0", "0", "1", "1", "--name", name,
                ])
                self.assertEqual(code, 2)
                self.assertIn("--name", err)

    def test_invalid_arguments_exit_two_from_parser(self) -> None:
        for extra in (["--unknown"], ["--at", "01:70"], ["--capture", "MULLIGAN"]):
            with self.subTest(extra=extra), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    debug_analyze.main(self.arguments + list(extra))
                self.assertEqual(caught.exception.code, 2)

    def test_time_parser_accepts_seconds_minutes_and_hours(self) -> None:
        for text, seconds in (("90", 90), ("01:30", 90), ("01:02:30", 3750), ("00:01.5", 1.5)):
            self.assertEqual(debug_analyze.parse_time(text), seconds)

    def test_time_parser_rejects_negative_nan_and_invalid_clock_values(self) -> None:
        import argparse
        for text in ("-1", "nan", "inf", "1:60", "1::2", "1:2:3:4", ""):
            with self.subTest(text=text), self.assertRaises(argparse.ArgumentTypeError):
                debug_analyze.parse_time(text)

    def test_help_and_import_are_headless_from_a_different_working_directory(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(repository)
        environment["PYTHONIOENCODING"] = "utf-8"
        code = (
            "import importlib.abc,sys; "
            "exec('class Blocker(importlib.abc.MetaPathFinder):\\n def find_spec(self, fullname, path=None, target=None):\\n  if fullname.split(\".\")[0] in (\"PySide6\",\"cv2\"):\\n   raise RuntimeError(\"forbidden GUI/CV import: \"+fullname)'); "
            "sys.meta_path.insert(0,Blocker()); "
            "from analysis.gameplay.hearthstone.debug_analyze import main; "
            "main(['--help'])"
        )
        process = subprocess.run(
            [sys.executable, "-c", code], cwd=self.root, env=environment,
            capture_output=True, text=True, encoding="utf-8", timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("--template-pack", process.stdout)
        self.assertIn("never creates cuts", process.stdout)
        self.assertNotIn("Traceback", process.stderr)

    def test_module_help_entry_point_runs_from_different_directory(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        environment = dict(os.environ, PYTHONPATH=str(repository), PYTHONIOENCODING="utf-8")
        process = subprocess.run(
            [sys.executable, "-m", "analysis.gameplay.hearthstone.debug_analyze", "--help"],
            cwd=self.root, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("--capture", process.stdout)

    def test_capture_malformed_pack_returns_two_without_traceback(self) -> None:
        pack = self.root / "invalid.json"
        arguments = self.arguments + [
            "--capture", "DEFEAT", "--at", "1", "--roi", "0", "0", "1", "1",
            "--template-pack", str(pack), "--json",
        ]
        for raw in ({}, {"classes": None}, {"classes": {"DEFEAT": {"directory": None}}}):
            pack.write_text(json.dumps(raw), encoding="utf-8")
            with self.subTest(raw=raw), patch.object(debug_analyze, "require_opencv", return_value=object()):
                code, out, err = self.invoke(arguments)
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(out)["exit_code"], 2)
                self.assertIn("failed", err)


    def test_capture_rejects_wrong_pack_schema_before_decoding(self) -> None:
        resource = Path(__file__).resolve().parents[1] / "resources" / "gameplay" / "hearthstone" / "templates.json"
        original = json.loads(resource.read_text(encoding="utf-8"))
        pack = self.root / "invalid schema.json"
        arguments = self.arguments + [
            "--capture", "DEFEAT", "--at", "1", "--roi", "0", "0", "1", "1",
            "--template-pack", str(pack), "--json",
        ]
        for key, value in (("schema_version", 999), ("profile", "other_game")):
            raw = dict(original)
            raw[key] = value
            pack.write_text(json.dumps(raw), encoding="utf-8")
            with self.subTest(key=key), patch.object(debug_analyze, "require_opencv", return_value=object()):
                with patch.object(debug_analyze, "FFmpegFrameSampler") as sampler:
                    code, out, _err = self.invoke(arguments)
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(out)["exit_code"], 2)
                sampler.assert_not_called()

    def test_invalid_capture_roi_does_not_create_configuration(self) -> None:
        pack = self.root / "new config" / "templates.json"
        arguments = self.arguments + [
            "--capture", "DEFEAT", "--at", "1", "--roi", "0.9", "0", "0.9", "1",
            "--template-pack", str(pack), "--json",
        ]
        with patch.object(debug_analyze, "require_opencv", return_value=object()):
            code, _out, _err = self.invoke(arguments)
        self.assertEqual(code, 2)
        self.assertFalse(pack.exists())
        self.assertFalse(pack.parent.exists())

    def test_capture_missing_video_does_not_create_configuration(self) -> None:
        pack = self.root / "new config" / "templates.json"
        arguments = self.arguments + [
            "--capture", "DEFEAT", "--at", "1", "--roi", "0", "0", "1", "1",
            "--template-pack", str(pack), "--json",
        ]
        with patch.object(debug_analyze, "require_opencv", return_value=object()):
            with patch.object(debug_analyze, "FFmpegFrameSampler") as factory:
                factory.return_value.probe.side_effect = OSError("video not found")
                code, _out, err = self.invoke(arguments)
        self.assertEqual(code, 2)
        self.assertIn("video not found", err)
        self.assertFalse(pack.exists())

    def test_capture_directory_traversal_is_rejected_without_writes(self) -> None:
        resource = Path(__file__).resolve().parents[1] / "resources" / "gameplay" / "hearthstone" / "templates.json"
        raw = json.loads(resource.read_text(encoding="utf-8"))
        pack = self.root / "traversal config.json"
        raw["classes"]["DEFEAT"]["directory"] = "../foreign"
        pack.write_text(json.dumps(raw), encoding="utf-8")
        original = pack.read_bytes()
        arguments = self.arguments + [
            "--capture", "DEFEAT", "--at", "1", "--roi", "0", "0", "1", "1",
            "--template-pack", str(pack), "--json",
        ]
        with patch.object(debug_analyze, "require_opencv", return_value=object()):
            code, _out, err = self.invoke(arguments)
        self.assertEqual(code, 2)
        self.assertIn("traversal", err)
        self.assertEqual(pack.read_bytes(), original)
        self.assertFalse((self.root.parent / "foreign").exists())

    @unittest.skipUnless(importlib.util.find_spec("cv2") is not None, "Install requirements-gameplay.txt for capture tests")
    def test_capture_with_fake_sampler_saves_patch_without_overwriting_existing_file(self) -> None:
        from analysis.gameplay.hearthstone.visual.templates import require_opencv
        pack = self.root / "capture templates.json"
        resource = Path(__file__).resolve().parents[1] / "resources" / "gameplay" / "hearthstone" / "templates.json"
        raw = json.loads(resource.read_text(encoding="utf-8"))
        raw["reference_size"] = [96, 64]
        pack.write_text(json.dumps(raw), encoding="utf-8")
        image = np.random.default_rng(77).integers(0, 256, (64, 96, 3), dtype=np.uint8)
        arguments = self.arguments + [
            "--capture", "DEFEAT", "--at", "00:01", "--roi", "0.25", "0.25", "0.5", "0.5",
            "--template-pack", str(pack), "--name", "distinctive_anchor", "--json",
        ]
        with patch.object(debug_analyze, "FFmpegFrameSampler") as factory:
            factory.return_value.probe.return_value = VideoInfo(10, 96, 64)
            factory.return_value.sample.side_effect = lambda *args, **kwargs: (item for item in [SampledFrame(1.0, image)])
            code, out, _err = self.invoke(arguments)
        self.assertEqual(code, 0)
        destination = Path(json.loads(out)["template"])
        content = destination.read_bytes()
        decoded = require_opencv().imdecode(np.frombuffer(content, dtype=np.uint8), 1)
        np.testing.assert_array_equal(decoded, image[16:48, 24:72])
        with patch.object(debug_analyze, "FFmpegFrameSampler") as factory:
            factory.return_value.probe.return_value = VideoInfo(10, 96, 64)
            factory.return_value.sample.side_effect = lambda *args, **kwargs: (item for item in [SampledFrame(1.0, image)])
            code, _out, err = self.invoke(arguments)
        self.assertEqual(code, 2)
        self.assertIn("already exists", err)
        self.assertEqual(destination.read_bytes(), content)
