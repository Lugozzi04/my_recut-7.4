"""Offline diagnostic frontend; no Qt, project edits, uploads or cuts."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import sys
import tempfile
from typing import Any, TextIO

from analysis.audio_service import AnalysisCancellation, AnalysisCancelled
from analysis.gameplay.cache import atomic_write_json, video_identity
from analysis.gameplay.hearthstone.result_policy import SingleFrameResultPolicy
from analysis.gameplay.evaluation import evaluate_analysis
from analysis.gameplay.hearthstone.analyzer import HearthstoneAnalyzer, HearthstoneSettings, default_template_pack
from analysis.gameplay.hearthstone.visual.detector import normalize_frame
from analysis.gameplay.hearthstone.visual.sampler import FFmpegFrameSampler
from analysis.gameplay.hearthstone.visual.templates import NormalizedROI, TemplateRepository, require_opencv
from analysis.gameplay.models import GameEventType
from utils.runtime_paths import config_root, data_root, resource_path
from export.output_safety import atomic_promote_output, ensure_output_is_safe


def _write_line(message: str, *, file: TextIO | None = None, flush: bool = False) -> None:
    """Preserve Unicode when supported; escape characters a terminal cannot encode."""
    stream = sys.stdout if file is None else file
    text = message + "\n"
    try:
        stream.write(text)
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "utf-8"
        stream.write(text.encode(encoding, errors="backslashreplace").decode(encoding))
    if flush:
        stream.flush()


def parse_time(value: str) -> float:
    try:
        parts = value.split(":")
        if not 1 <= len(parts) <= 3 or any(not part for part in parts):
            raise ValueError
        if len(parts) > 1 and any(not part.isdecimal() for part in parts[:-1]):
            raise ValueError
        values = [float(part) for part in parts]
        if any(not math.isfinite(number) or number < 0 for number in values):
            raise ValueError
        if len(values) > 1 and any(number >= 60 for number in values[1:]):
            raise ValueError
        result = 0.0
        for number in values:
            result = result * 60 + number
        return result
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Use seconds, MM:SS or HH:MM:SS with nonnegative finite values") from exc


def format_time(value: float | None) -> str:
    if value is None:
        return "UNKNOWN"
    tenths = round(value * 10)
    hours, remainder = divmod(tenths, 36000)
    minutes, remainder = divmod(remainder, 600)
    seconds = remainder / 10
    return f"{hours:02d}:{minutes:02d}:{seconds:04.1f}"


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="ReCut Hearthstone video-only gameplay diagnostics; never creates cuts")
    command.add_argument("video", type=Path)
    command.add_argument("--template-pack", type=Path, help="Path to templates.json; default user config, then bundled pack")
    command.add_argument("--output-dir", type=Path, help="Diagnostic output directory; default writable app-data reports")
    command.add_argument('--strategy', choices=('global', 'state-aware'), default='global',
                         help='Opt-in state-aware candidate; established global scan remains default')
    command.add_argument('--state-profile', type=Path, help='Explicit state-aware calibration profile JSON')
    command.add_argument('--window-fps', type=float, help='State-aware local window FPS override for comparison only')
    command.add_argument("--coarse-fps", type=float, default=1.0)
    command.add_argument("--fine-fps", type=float, default=6.0)
    command.add_argument("--result-fps", type=float, default=20.0, help="Dense result ROI sampling; 0 reproduces legacy diagnostics")
    command.add_argument("--result-refine-fps", type=float, default=120.0, help="Local unstable-candidate ROI refinement only; 0 disables")
    command.add_argument("--candidate-similarity", type=float, default=0.80, help="Refinement-only weak gate; never final acceptance")
    command.add_argument("--max-game-duration", type=float, default=1800.0, help="Conservative pairing limit in seconds; longer pairs remain partial")
    command.add_argument("--fine-padding", type=float, default=2.0, metavar="SECONDS")
    command.add_argument("--debug-frames", action="store_true", help="Save bounded candidate JPEG frames; bypasses cache")
    command.add_argument("--max-debug-frames", type=int, default=100)
    command.add_argument("--no-cache", action="store_true")
    command.add_argument("--json", action="store_true", help="Only JSON on stdout; human progress goes to stderr")
    command.add_argument("--ground-truth", type=Path, help="Manually annotated ground-truth JSON for evaluation")
    command.add_argument("--tolerance", type=float, default=5.0, metavar="SECONDS")
    command.add_argument("--capture", choices=[kind.value for kind in GameEventType], help="Capture one template; does not analyze the VOD")
    command.add_argument("--at", type=parse_time, help="Template capture timestamp")
    command.add_argument("--roi", nargs=4, type=float, metavar=("X", "Y", "W", "H"), help="Normalized anchor patch within reference viewport")
    command.add_argument("--name", help="Optional template filename stem; no extension or separators")
    command.add_argument("--single-frame-policy", type=Path, help="Frozen calibration-only result policy JSON; default disabled")
    command.add_argument("--calibrate", type=Path, metavar="ANNOTATIONS", help="Score only manually annotated calibration windows; no global scan")
    command.add_argument("--calibration-fps", type=float, default=60.0, help="Native result-window sampling (actual source frames only)")
    command.add_argument("--calibration-padding", type=float, default=1.0, help="Seconds on either side of each annotated anchor")
    command.add_argument("--capture-annotations", type=Path, help="Calibration provenance for template capture; video fingerprint checked")
    command.add_argument("--allow-similar-template", action="store_true", help="After review, allow near-duplicate phase capture; exact duplicates remain forbidden")
    return command


def _capture(arguments: argparse.Namespace, token: AnalysisCancellation) -> dict[str, Any]:
    if arguments.at is None or arguments.roi is None:
        raise ValueError("--capture requires --at TIME and --roi X Y W H")
    if arguments.name is not None and (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", arguments.name)
            or re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", arguments.name, flags=re.IGNORECASE)):
        raise ValueError("--name must contain 1..64 letters, digits, underscores or hyphens")
    roi = NormalizedROI.from_value(arguments.roi, name="capture ROI")
    pack_path = (arguments.template_pack or config_root() / "gameplay" / "hearthstone" / "templates.json").expanduser().resolve()
    default = resource_path("resources", "gameplay", "hearthstone", "templates.json")
    raw = json.loads((pack_path if pack_path.is_file() else default).read_text(encoding="utf-8"))
    kind = GameEventType(arguments.capture)
    if not isinstance(raw, dict) or type(raw.get("schema_version")) is not int or raw.get("schema_version") != 1 or raw.get("profile") != "hearthstone":
        raise ValueError("Expected Hearthstone template pack schema_version=1")
    classes = raw.get("classes")
    if not isinstance(classes, dict) or set(classes) != {event.value for event in GameEventType}:
        raise ValueError("Template pack must define VS_SCREEN, VICTORY and DEFEAT")
    item = classes[kind.value]
    if not isinstance(item, dict) or not isinstance(item.get("directory"), str):
        raise ValueError("Template class must define a relative directory")
    relative = item["directory"]
    # Apply the repository's exact traversal/symlink checks, even with an incomplete pack.
    repository = TemplateRepository(pack_path)
    directory = repository.resolve_directory(relative)
    reference = raw.get("reference_size")
    if not isinstance(reference, list) or len(reference) != 2 or any(
        isinstance(value, bool) or not isinstance(value, int) or not 16 <= value <= 4096 for value in reference
    ):
        raise ValueError("reference_size must contain two integers in 16..4096")
    viewport = NormalizedROI.from_value(raw.get("viewport", [0, 0, 1, 1]), name="viewport")
    left, top, right, bottom = roi.pixel_bounds(reference[0], reference[1])
    if right - left < 2 or bottom - top < 2:
        raise ValueError("Capture ROI must cover at least 2x2 reference pixels")
    cv2 = require_opencv()
    sampler = FFmpegFrameSampler()
    info = sampler.probe(arguments.video, cancellation=token)
    if arguments.at >= info.duration_s:
        raise ValueError("--at must precede the end of the video")
    with closing(sampler.sample(
        arguments.video, fps=1.0, start_s=arguments.at, end_s=min(info.duration_s, arguments.at + 1.0),
        max_width=reference[0], cancellation=token,
    )) as frames:
        frame = next(frames, None)
        if frame is None:
            raise ValueError("No frame available at the requested timestamp")
        normalized = normalize_frame(frame.image, (reference[0], reference[1]), viewport)
        left, top, right, bottom = roi.pixel_bounds(reference[0], reference[1])
        patch = normalized[top:bottom, left:right]
        if float(cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY).std()) < 1.0:
            raise ValueError("Capture ROI is effectively constant; select a distinctive visual anchor")
        success, encoded = cv2.imencode(".png", patch)
        if not success:
            raise ValueError("Could not encode the template")
        token.raise_if_cancelled()
        if arguments.name and (directory / f"{arguments.name}.png").exists():
            raise ValueError(f"Template already exists: {directory / (arguments.name + '.png')}")
        from analysis.gameplay.hearthstone.visual.template_tools import similar_templates
        similarities = similar_templates(patch, directory)
        if any(item['pixel_duplicate'] for item in similarities):
            raise ValueError('Pixel-identical template already exists; capture a different animation phase')
        if similarities and similarities[0]['similarity'] >= 0.995 and not arguments.allow_similar_template:
            raise ValueError('Near-duplicate template: review another phase or use --allow-similar-template after review')
        provenance = video_identity(Path(arguments.video))
        capture_role = 'unassigned'
        annotations_hash = None
        if arguments.capture_annotations:
            from analysis.gameplay.hearthstone.calibration import annotation_windows
            labels = json.loads(arguments.capture_annotations.read_text(encoding='utf-8'))
            if labels.get('video_fingerprint') != provenance['fingerprint']:
                raise ValueError('Template capture requires calibration annotations with the matching video fingerprint')
            annotated = annotation_windows(labels, info.duration_s, 1.0)
            if not any(w['kind'] == kind.value and w['start'] <= frame.timestamp <= w['end'] for w in annotated):
                raise ValueError('Captured anchor is not in an annotated calibration window of this class')
            capture_role = 'calibration'
            annotations_hash = hashlib.sha256(arguments.capture_annotations.read_bytes()).hexdigest()
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="wb", prefix=".template-", suffix=".tmp", dir=directory, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encoded.tobytes())
            stream.flush()
            os.fsync(stream.fileno())
        try:
            index = 1
            while True:
                stem = arguments.name or f"{kind.value.lower()}_{index:03d}"
                destination = directory / f"{stem}.png"
                if os.path.lexists(destination.with_suffix('.png.provenance.json')):
                    if arguments.name:
                        raise ValueError(f'Template provenance already exists: {destination}')
                    index += 1
                    continue
                try:
                    atomic_promote_output(temporary, destination, overwrite=False)
                    break
                except FileExistsError:
                    if arguments.name:
                        raise ValueError(f"Template already exists: {destination}")
                    index += 1
        finally:
            temporary.unlink(missing_ok=True)
        provenance_payload = {
            'role': capture_role, 'video_fingerprint': provenance['fingerprint'],
            'annotation_sha256': annotations_hash, 'timestamp': frame.timestamp,
            'roi': roi.to_list(), 'reference_size': reference,
            'patch_sha256': hashlib.sha256(encoded.tobytes()).hexdigest(),
            'nearest_templates': similarities[:5],
        }
        # A foreign companion appearing during capture must not be replaced.
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', prefix='.provenance-', suffix='.tmp',
                                         dir=directory, delete=False) as stream:
            provenance_temporary = Path(stream.name)
            json.dump(provenance_payload, stream, ensure_ascii=True, allow_nan=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            atomic_promote_output(provenance_temporary, destination.with_suffix('.png.provenance.json'), overwrite=False)
        finally:
            provenance_temporary.unlink(missing_ok=True)
        if not pack_path.exists():
            atomic_write_json(pack_path, raw)
        return {"template": str(destination), "template_pack": str(pack_path), "event_type": kind.value,
                "timestamp": frame.timestamp, "roi": roi.to_list(), "reference_size": reference}


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    token = AnalysisCancellation()
    previous_handler = signal.getsignal(signal.SIGINT)

    def cancel(_signal: int, _frame: Any) -> None:
        token.cancel()

    signal.signal(signal.SIGINT, cancel)
    last_progress = -1

    def progress(value: int) -> None:
        nonlocal last_progress
        if value // 5 != last_progress // 5:
            _write_line(f"Gameplay analysis: {value}%", file=sys.stderr, flush=True)
        last_progress = value

    try:
        if arguments.strategy == 'state-aware':
            if arguments.state_profile is None:
                raise ValueError('--strategy state-aware requires --state-profile')
            if arguments.capture or arguments.calibrate or arguments.single_frame_policy:
                raise ValueError('State-aware strategy cannot be combined with capture, calibration-window scoring or single-frame policy')
            if (arguments.coarse_fps, arguments.fine_fps, arguments.result_fps, arguments.result_refine_fps,
                arguments.candidate_similarity, arguments.max_game_duration, arguments.fine_padding) != (1.0, 6.0, 20.0, 120.0, 0.80, 1800.0, 2.0):
                raise ValueError('State-aware sampling uses --state-profile; global sampling overrides do not apply')
        elif arguments.state_profile is not None or arguments.window_fps is not None:
            raise ValueError('--state-profile and --window-fps require --strategy state-aware')
        if arguments.capture:
            if arguments.ground_truth or arguments.debug_frames or arguments.calibrate or arguments.single_frame_policy:
                raise ValueError("--capture cannot be combined with --ground-truth or --debug-frames")
            payload = _capture(arguments, token)
            if arguments.json:
                _write_line(json.dumps(payload, ensure_ascii=True, allow_nan=False))
            else:
                _write_line(f"Template saved: {payload['template']}\nPack: {payload['template_pack']}")
            return 0
        if arguments.capture_annotations or arguments.allow_similar_template:
            raise ValueError('--capture-annotations and --allow-similar-template require --capture')
        if arguments.calibrate:
            if arguments.ground_truth or arguments.single_frame_policy or arguments.at is not None or arguments.roi or arguments.name:
                raise ValueError('--calibrate cannot be combined with evaluation, capture or a result policy')
            from analysis.gameplay.hearthstone.calibration import CalibrationAnalyzer, CalibrationSettings
            from analysis.gameplay.hearthstone.visual.detector import TemplateVisualDetector
            labels = json.loads(arguments.calibrate.read_text(encoding='utf-8'))
            detector = TemplateVisualDetector(TemplateRepository(arguments.template_pack or default_template_pack()).load())
            calibration = CalibrationAnalyzer(detector, settings=CalibrationSettings(
                result_fps=arguments.calibration_fps, padding_s=arguments.calibration_padding,
                max_debug_frames=arguments.max_debug_frames,
            ))
            video = arguments.video.expanduser().resolve()
            output = (arguments.output_dir or data_root() / 'gameplay' / 'calibration' / video.stem).expanduser().resolve()
            report_path = ensure_output_is_safe(output / 'gameplay_calibration.json', [video, arguments.calibrate, detector.pack.path])
            measured = calibration.analyze(video, labels, cancellation=token, on_progress=progress,
                                          debug_frames=output / 'calibration_frames' if arguments.debug_frames else None)
            token.raise_if_cancelled()
            report = atomic_write_json(report_path, measured)
            if arguments.json:
                _write_line(json.dumps({'report': str(report), 'calibration': measured}, ensure_ascii=True, allow_nan=False))
            else:
                _write_line(f'Calibration report: {report}')
                _write_line(json.dumps(measured['score_distributions'], indent=2, ensure_ascii=True))
            return 0
        if arguments.at is not None or arguments.roi is not None or arguments.name is not None:
            raise ValueError("--at, --roi and --name are only valid with --capture")
        policy = SingleFrameResultPolicy()
        if arguments.single_frame_policy:
            policy = SingleFrameResultPolicy.from_mapping(json.loads(arguments.single_frame_policy.read_text(encoding='utf-8')))
        settings = HearthstoneSettings(single_frame_results=policy,coarse_fps=arguments.coarse_fps, fine_fps=arguments.fine_fps, fine_padding_s=arguments.fine_padding, result_fps=arguments.result_fps, result_refine_fps=arguments.result_refine_fps, candidate_similarity=arguments.candidate_similarity, maximum_game_duration_s=arguments.max_game_duration)
        if arguments.strategy == 'state-aware':
            from dataclasses import replace
            from analysis.gameplay.hearthstone.state_aware import StateAwareHearthstoneAnalyzer
            analyzer: Any = StateAwareHearthstoneAnalyzer(state_profile=arguments.state_profile,
                template_pack=arguments.template_pack or default_template_pack(), use_cache=not arguments.no_cache)
            if arguments.window_fps is not None:
                analyzer.monitor_settings = replace(analyzer.monitor_settings, high_fps=arguments.window_fps)
        else:
            analyzer = HearthstoneAnalyzer(template_pack=arguments.template_pack or default_template_pack(), settings=settings, use_cache=not arguments.no_cache)
        video = arguments.video.expanduser().resolve()
        output = (arguments.output_dir or data_root() / "gameplay" / "reports" / hashlib.sha256(str(video).encode("utf-8")).hexdigest()[:16]).expanduser().resolve()
        analysis = analyzer.analyze(video, cancellation=token, on_progress=progress,
                                    debug_frames=output / "debug_frames" if arguments.debug_frames else None,
                                    max_debug_frames=arguments.max_debug_frames)
        evaluation = None
        if arguments.ground_truth:
            labels = json.loads(arguments.ground_truth.read_text(encoding="utf-8"))
            evaluation = evaluate_analysis(analysis, labels, tolerance_s=arguments.tolerance)
        token.raise_if_cancelled()
        protected = [video]
        protected.extend(path for path in (arguments.ground_truth, arguments.template_pack, arguments.state_profile) if path is not None)
        report_path = ensure_output_is_safe(output / "gameplay_analysis.json", protected)
        evaluation_path = ensure_output_is_safe(output / "gameplay_evaluation.json", protected)
        report = atomic_write_json(report_path, analysis.to_mapping())
        if evaluation is not None:
            atomic_write_json(evaluation_path, evaluation)
        if arguments.json:
            payload = {"analysis": analysis.to_mapping(), "report": str(report)}
            if evaluation is not None:
                payload["evaluation"] = evaluation
            _write_line(json.dumps(payload, ensure_ascii=True, allow_nan=False))
        else:
            for game in analysis.games:
                _write_line(f"Game {game.index}: {format_time(game.start)} -> {format_time(game.end)}")
                for event in game.markers:
                    _write_line(f"  {event.type.value:<10} {format_time(event.timestamp)} confidence={event.confidence:.3f}")
                _write_line(f"  RESULT     {game.result.value}")
            _write_line(f"Report: {report}")
            for warning in analysis.warnings:
                _write_line(f"Warning: {warning}", file=sys.stderr)
        return 0
    except (AnalysisCancelled, KeyboardInterrupt):
        _write_line("Gameplay analysis cancelled", file=sys.stderr)
        return 70
    except (OSError, ValueError, RuntimeError) as exc:
        _write_line(f"Gameplay analysis failed: {exc}", file=sys.stderr)
        if arguments.json:
            _write_line(json.dumps({"error": str(exc), "exit_code": 2}, ensure_ascii=True))
        return 2
    finally:
        signal.signal(signal.SIGINT, previous_handler)


if __name__ == "__main__":
    raise SystemExit(main())
