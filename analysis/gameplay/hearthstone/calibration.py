"""Localized, explicitly labeled calibration diagnostics, independent of Qt.

Annotation windows are NOT all positive frames. Only manually reviewed visible
intervals are labeled positive. Negative windows must be independently reviewed.
This tool reports measurements; it never chooses or enables acceptance gates.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from contextlib import closing
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
import time
from typing import Any

import numpy as np

from analysis.audio_service import AnalysisCancellation
from analysis.gameplay.analyzer import ProgressCallback
from analysis.gameplay.cache import video_identity
from analysis.gameplay.hearthstone.analyzer import HearthstoneAnalyzer
from analysis.gameplay.hearthstone.visual.detector import TemplateVisualDetector
from analysis.gameplay.hearthstone.visual.sampler import FFmpegFrameSampler
from analysis.gameplay.models import GameEvent, GameEventType
from export.output_safety import ensure_output_is_safe


def seconds(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f'{name} must be finite nonnegative seconds')
    return float(value)


@dataclass(frozen=True)
class CalibrationSettings:
    result_fps: float = 60.0
    vs_fps: float = 6.0
    negative_fps: float = 20.0
    padding_s: float = 1.0
    max_frames: int = 100_000
    max_debug_frames: int = 240

    def __post_init__(self) -> None:
        for name in ('result_fps', 'vs_fps', 'negative_fps', 'padding_s'):
            value = seconds(getattr(self, name), name)
            limit = 120 if name != 'padding_s' else 5
            if not 0 < value <= limit:
                raise ValueError(f'{name} must be positive and <= {limit}')
        for name in ('max_frames', 'max_debug_frames'):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100_000:
                raise ValueError(f'{name} must be an integer within 0..100000')
        if self.max_frames == 0:
            raise ValueError('max_frames must be positive')


def annotation_windows(raw: Mapping[str, Any], duration: float, padding: float) -> list[dict[str, Any]]:
    """Accept game annotations or explicit anchors; reject validation role."""
    if not isinstance(raw, Mapping) or raw.get('role') != 'calibration':
        raise ValueError('Annotations must explicitly declare role=calibration; validation input is forbidden')
    if raw.get('format', 'recut_gameplay_calibration') != 'recut_gameplay_calibration' or type(raw.get('version', 1)) is not int or raw.get('version', 1) != 1:
        raise ValueError('Expected recut_gameplay_calibration version 1')
    if not isinstance(raw.get('events', []), list):
        raise ValueError('events must be an array')
    anchors = list(raw.get('events', []))
    games = raw.get('games', [])
    negatives = raw.get('negative_windows', [])
    if not isinstance(games, list) or not isinstance(negatives, list) or not isinstance(raw.get('events', []), list):
        raise ValueError('games, events and negative_windows must be arrays')
    if len(games) + len(anchors) + len(negatives) > 1000:
        raise ValueError('Too many calibration annotations')
    for game in games:
        if not isinstance(game, dict):
            raise ValueError('Each game must be an object')
        if game.get('start') is not None:
            anchors.append({'type': 'VS_SCREEN', 'timestamp': game['start'],
                            'visible_start': game.get('start_visible_start'), 'visible_end': game.get('start_visible_end')})
        end = game.get('result_time', game.get('end'))
        if end is not None:
            result = str(game.get('result', '')).upper()
            if result not in ('WIN', 'LOSS'):
                raise ValueError('Annotated results must be WIN or LOSS')
            if game.get('start') is not None and seconds(end, 'result_time') <= seconds(game['start'], 'start'):
                raise ValueError('result_time must follow start')
            anchors.append({'type': 'VICTORY' if result == 'WIN' else 'DEFEAT', 'timestamp': end,
                            'visible_start': game.get('result_visible_start'), 'visible_end': game.get('result_visible_end')})
    windows: list[dict[str, Any]] = []
    seen: set[tuple[str, float]] = set()
    for anchor in anchors:
        if not isinstance(anchor, dict):
            raise ValueError('Each event must be an object')
        try:
            kind = GameEventType(anchor.get('type'))
        except (ValueError, TypeError) as exc:
            raise ValueError('Unknown calibration event type') from exc
        timestamp = seconds(anchor.get('timestamp'), 'timestamp')
        if timestamp >= duration or (kind.value, timestamp) in seen:
            raise ValueError('Duplicate anchor or timestamp outside video')
        seen.add((kind.value, timestamp))
        start, end = max(0.0, timestamp - padding), min(duration, timestamp + padding)
        visible = None
        if anchor.get('visible_start') is not None or anchor.get('visible_end') is not None:
            a = seconds(anchor.get('visible_start'), 'visible_start')
            b = seconds(anchor.get('visible_end'), 'visible_end')
            if not start <= a <= b <= end:
                raise ValueError('Reviewed visible interval must be contained in its sampling window')
            visible = [a, b]
        windows.append({'kind': kind.value, 'timestamp': timestamp, 'start': start, 'end': end, 'visible': visible})
    for negative in negatives:
        if not isinstance(negative, dict):
            raise ValueError('Each negative window must be an object')
        start, end = seconds(negative.get('start'), 'negative start'), seconds(negative.get('end'), 'negative end')
        if not start < end <= duration or end - start > 60:
            raise ValueError('Negative windows must cover >0 and <=60 seconds within the video')
        if any(start < window['end'] and end > window['start'] for window in windows):
            raise ValueError('Negative windows must not overlap anchors or other negative windows')
        windows.append({'kind': 'NEGATIVE', 'timestamp': (start + end) / 2, 'start': start, 'end': end, 'visible': None})
    if not windows:
        raise ValueError('Supply at least one annotated anchor or negative window')
    return sorted(windows, key=lambda window: (window['start'], window['kind']))


def _distribution(values: list[float]) -> dict[str, Any]:
    if not values:
        return {'count': 0, **{name: None for name in ('min', 'p10', 'median', 'p90', 'p95', 'max')}}
    ordered = sorted(values)
    middle = len(values) // 2
    median = ordered[middle] if len(values) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    return {'count': len(values), 'min': ordered[0], 'p10': float(np.percentile(ordered, 10)),
            'median': median, 'p90': float(np.percentile(ordered, 90)),
            'p95': float(np.percentile(ordered, 95)), 'max': ordered[-1]}


def summarize_windows(windows: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for kind in GameEventType:
        positives, negatives, margins, peaks = [], [], [], []
        identities: Counter[str] = Counter()
        for window in windows:
            rows = window['frames']
            if window['kind'] == kind.value and rows:
                best = max(rows, key=lambda row: row['classes'].get(kind.value, {}).get('similarity', -1))
                if kind.value in best['classes']:
                    peaks.append({'timestamp': best['timestamp'], **best['classes'][kind.value]})
            for row in rows:
                score = row['classes'].get(kind.value)
                if score is None:
                    continue
                if row['label'] == kind.value:
                    positives.append(score['similarity'])
                    margins.append(score['margin'])
                    identities[score['template_id']] += 1
                elif row['label'] == 'NEGATIVE' or row['label'] in {event.value for event in GameEventType}:
                    negatives.append(score['similarity'])
        summary[kind.value] = {
            'reviewed_positive_scores': _distribution(positives),
            'reviewed_negative_or_other_class_scores': _distribution(negatives),
            'positive_class_margin': _distribution(margins),
            'positive_best_template_counts': dict(identities), 'annotated_window_peaks': peaks,
        }
    return summary


class CalibrationAnalyzer:
    def __init__(self, detector: TemplateVisualDetector, *, sampler: Any = None, settings: CalibrationSettings | None = None) -> None:
        self.detector = detector
        self.sampler = sampler or FFmpegFrameSampler()
        self.settings = settings or CalibrationSettings()

    def analyze(
        self, video: str | Path, annotations: Mapping[str, Any], *, cancellation: AnalysisCancellation | None = None,
        on_progress: ProgressCallback | None = None, debug_frames: Path | None = None,
    ) -> dict[str, Any]:
        token = cancellation or AnalysisCancellation()
        token.raise_if_cancelled()
        source = Path(video).expanduser().resolve()
        identity = video_identity(source)
        if not isinstance(annotations, Mapping):
            raise ValueError('Calibration annotations must be an object')
        if annotations.get('video_fingerprint') is not None and annotations['video_fingerprint'] != identity['fingerprint']:
            raise ValueError('Calibration annotations belong to another video')
        info = self.sampler.probe(source, cancellation=token)
        windows = annotation_windows(annotations, info.duration_s, self.settings.padding_s)
        result_types = (GameEventType.VICTORY, GameEventType.DEFEAT)
        roi = self.detector.result_scan_roi()
        reference = self.detector.pack.reference_size
        viewport = self.detector.pack.viewport
        left, top, right, bottom = roi.pixel_bounds(*reference)
        source_roi = (viewport.x + left / reference[0] * viewport.width,
                      viewport.y + top / reference[1] * viewport.height,
                      (right - left) / reference[0] * viewport.width, (bottom - top) / reference[1] * viewport.height)
        output_size = (right - left, bottom - top)
        estimated = sum((w['end'] - w['start']) * (self.settings.vs_fps if w['kind'] == 'VS_SCREEN' else
                        self.settings.negative_fps if w['kind'] == 'NEGATIVE' else self.settings.result_fps) for w in windows)
        if estimated > self.settings.max_frames:
            raise ValueError('Calibration frame budget exceeded; split annotations into smaller batches')
        started = time.perf_counter()
        count = saved = 0
        matching_seconds = sampling_seconds = 0.0
        per_window = self.settings.max_debug_frames // len(windows)
        if on_progress:
            on_progress(0)
        for index, window in enumerate(windows):
            token.raise_if_cancelled()
            vs = window['kind'] == 'VS_SCREEN'
            fps = self.settings.vs_fps if vs else self.settings.negative_fps if window['kind'] == 'NEGATIVE' else self.settings.result_fps
            options = dict(fps=fps, start_s=window['start'], end_s=window['end'], cancellation=token)
            frames = (self.sampler.sample(source, max_width=reference[0], **options) if vs else
                      self.sampler.sample_roi(source, roi=source_roi, reference_size=reference, output_size=output_size, **options))
            rows: list[dict[str, Any]] = []
            selected: dict[float, Any] = {}
            best_image, best_row = None, None
            stride = max(1, math.ceil((window['end'] - window['start']) * fps / max(1, per_window - 1)))
            with closing(frames):
                iterator = iter(frames)
                while True:
                    before = time.perf_counter()
                    try:
                        frame = next(iterator)
                    except StopIteration:
                        sampling_seconds += time.perf_counter() - before
                        break
                    sampling_seconds += time.perf_counter() - before
                    token.raise_if_cancelled()
                    count += 1
                    if count > self.settings.max_frames:
                        raise ValueError('Calibration frame budget exceeded')
                    before = time.perf_counter()
                    scores = (self.detector.score(frame.image, frame.timestamp) if vs else
                              self.detector.score_roi(frame.image, frame.timestamp, roi=roi, event_types=result_types,
                                                      include_template_scores=True))
                    matching_seconds += time.perf_counter() - before
                    visible = window['visible']
                    label = ('NEGATIVE' if window['kind'] == 'NEGATIVE' else window['kind']
                             if visible is not None and visible[0] <= frame.timestamp <= visible[1] else 'UNREVIEWED')
                    row = {**scores, 'label': label}
                    rows.append(row)
                    target = scores['classes'].get(window['kind'])
                    strength = target['similarity'] if target else max(item['similarity'] for item in scores['classes'].values())
                    if best_row is None or strength > best_row[0]:
                        best_row, best_image = (strength, row), frame.image.copy() if debug_frames is not None else None
                    if debug_frames is not None and per_window > 1 and (len(rows) - 1) % stride == 0 and len(selected) < per_window - 1:
                        selected[frame.timestamp] = (frame.image.copy(), row)
            if debug_frames is not None and per_window and best_image is not None and best_row is not None:
                selected[best_row[1]['timestamp']] = (best_image, best_row[1])
                directory = debug_frames / f'window_{index + 1:04d}'
                for image, row in selected.values():
                    token.raise_if_cancelled()
                    kind = GameEventType(row['best_type'])
                    value = max(0.0, row['classes'][kind.value]['similarity'])
                    # Reuse bounded, atomic no-overwrite diagnostics publication.
                    ensure_output_is_safe(directory, [source, self.detector.pack.path])
                    HearthstoneAnalyzer._save_frame(directory, image, GameEvent(row['timestamp'], kind, value), saved)
                    saved += 1
                window['debug_directory'] = str(directory)
            window['frames'] = rows
            window['fps'] = fps
            if on_progress:
                on_progress(int(100 * (index + 1) / len(windows)))
        token.raise_if_cancelled()
        if video_identity(source) != identity:
            raise ValueError('Source changed during calibration')
        return {
            'format': 'recut_gameplay_calibration_report', 'version': 1, 'role': 'calibration',
            'video': identity, 'detector': self.detector.describe(),
            'annotation_sha256': hashlib.sha256(json.dumps(dict(annotations), sort_keys=True).encode('utf-8')).hexdigest(),
            'settings': asdict(self.settings),
            'windows': windows, 'score_distributions': summarize_windows(windows),
            'performance': {'elapsed_seconds': time.perf_counter() - started, 'matching_seconds': matching_seconds,
                            'sampling_wait_seconds': sampling_seconds, 'frames': count, 'debug_frames_saved': saved,
                            'streams': len(windows)},
            'policy_enabled': False,
            'warnings': ['Calibration only; no cuts, game evaluation, threshold recommendation or policy activation.',
                         'UNREVIEWED frames do not count as positives or negatives.',
                         'Sampled negatives do not establish zero false positives on unseen VODs.'],
        }
