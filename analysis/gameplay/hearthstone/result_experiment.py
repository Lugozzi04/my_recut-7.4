"""Calibration-only representation/robustness experiment. Never chooses gates."""
from __future__ import annotations

import argparse
from contextlib import closing
from copy import copy, deepcopy
import json
from pathlib import Path
import tempfile
import time
from typing import Any, Iterable, Iterator

import numpy as np

from analysis.audio_service import AnalysisCancellation
from analysis.gameplay.cache import atomic_write_json, video_identity
from analysis.gameplay.hearthstone.calibration import annotation_windows, _distribution
from analysis.gameplay.hearthstone.visual.result_text import ResultTextDetector
from analysis.gameplay.hearthstone.visual.sampler import FFmpegFrameSampler
from analysis.gameplay.hearthstone.visual.state_profile import StateAwareProfile
from analysis.gameplay.hearthstone.visual.templates import require_opencv
from export.output_safety import ensure_output_is_safe


def robustness_variants(image: np.ndarray) -> Iterator[tuple[str, np.ndarray]]:
    """Mild rendering perturbations for evaluation; none become templates."""
    cv = require_opencv()
    yield 'original', image.copy()
    for brightness in (-20, 20):
        yield f'brightness_{brightness}', np.clip(image.astype(np.float32) + brightness, 0, 255).astype(np.uint8)
    for contrast in (.8, 1.2):
        yield f'contrast_{contrast}', np.clip((image.astype(np.float32)-128)*contrast+128, 0, 255).astype(np.uint8)
    for gamma in (.85, 1.15):
        yield f'gamma_{gamma}', np.clip(255*(image.astype(np.float32)/255)**gamma, 0, 255).astype(np.uint8)
    for saturation in (0.0, .7, 1.3):
        hsv = cv.cvtColor(image, cv.COLOR_BGR2HSV).astype(np.float32)
        hsv[:, :, 1] *= saturation
        yield f'saturation_{saturation}', cv.cvtColor(np.clip(hsv, 0, 255).astype(np.uint8), cv.COLOR_HSV2BGR)
    yield 'blur', cv.GaussianBlur(image, (3, 3), .55)
    yield 'sharpen', cv.addWeighted(image, 1.3, cv.GaussianBlur(image, (3, 3), .6), -.3, 0)
    yield 'jpeg35', cv.imdecode(cv.imencode('.jpg', image, [cv.IMWRITE_JPEG_QUALITY, 35])[1], cv.IMREAD_COLOR)
    height, width = image.shape[:2]
    for scale in (.95, 1.05):
        resized = cv.resize(image, (round(width*scale), round(height*scale)))
        h, w = resized.shape[:2]
        if scale < 1:
            canvas = np.full_like(image, 80)
            canvas[(height-h)//2:(height-h)//2+h, (width-w)//2:(width-w)//2+w] = resized
        else:
            canvas = resized[(h-height)//2:(h-height)//2+height, (w-width)//2:(w-width)//2+width]
        yield f'scale_{scale}', canvas


def representation_detectors(profile: StateAwareProfile) -> dict[str, ResultTextDetector]:
    detectors = {}
    for method in ('gray', 'clahe', 'gradient'):
        variant = copy(profile)
        variant.config = deepcopy(profile.config)
        variant.config['result']['representation'] = method
        detectors[method] = ResultTextDetector(variant)
    return detectors


def compare_samples(profile: StateAwareProfile, samples: Iterable[dict[str, Any]], *,
                    cancellation: AnalysisCancellation | None = None) -> dict[str, Any]:
    """Shared, decoding-free comparison; labels remain independent annotations."""
    token = cancellation or AnalysisCancellation()
    detectors = representation_detectors(profile)
    rows = []
    elapsed = dict.fromkeys(detectors, 0.0)
    for sample in samples:
        token.raise_if_cancelled()
        for method, detector in detectors.items():
            before = time.perf_counter()
            scores = detector.score_roi(sample['image'])
            prediction = detector.event(scores, sample['timestamp'])
            elapsed[method] += time.perf_counter() - before
            rows.append({key: value for key, value in sample.items() if key != 'image'} |
                        {'method': method, **scores, 'prediction': prediction.type.value if prediction else 'UNKNOWN'})
    summary = {}
    for method in detectors:
        measured = [row for row in rows if row['method'] == method and row['label'] != 'UNREVIEWED']
        summary[method] = {'matching_seconds': elapsed[method], 'frames': len(measured),
            'true_defeat_scores': _distribution([row['scores']['DEFEAT'] for row in measured if row['label'] == 'DEFEAT']),
            'non_defeat_scores': _distribution([row['scores']['DEFEAT'] for row in measured if row['label'] != 'DEFEAT']),
            'false_defeat_frames': sum(row['prediction'] == 'DEFEAT' and row['label'] != 'DEFEAT' for row in measured),
            'correct_frames': sum(row['prediction'] == row['label'] or
                                  (row['prediction'] == 'UNKNOWN' and row['label'] == 'NEGATIVE') for row in measured)}
    return {'profile': profile.describe(), 'rows': rows, 'summary': summary,
            'note': 'Frame metrics, not event/game recall. UNREVIEWED phases excluded. Parameters are not auto-tuned.'}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('video', type=Path)
    parser.add_argument('--annotations', type=Path, required=True)
    parser.add_argument('--state-profile', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--robustness', action='store_true')
    parser.add_argument('--ocr-manifest', action='store_true', help='Export reviewed/perturbed ROI images for optional OCR experiment')
    args = parser.parse_args(argv)
    try:
        token = AnalysisCancellation()
        video = args.video.expanduser().resolve()
        identity = video_identity(video)
        labels = json.loads(args.annotations.read_text(encoding='utf-8'))
        if labels.get('video_fingerprint') != identity['fingerprint']:
            raise ValueError('Calibration annotations must match the source fingerprint')
        profile = StateAwareProfile(args.state_profile)
        sampler = FFmpegFrameSampler()
        info = sampler.probe(video, cancellation=token)
        windows = annotation_windows(labels, info.duration_s, 1.0)
        detector = ResultTextDetector(profile)
        output = args.output_dir.expanduser().resolve()
        protected = [video, args.annotations, args.state_profile, *profile.asset_paths]
        report_path = ensure_output_is_safe(output/'result_representation_experiment.json', protected)
        samples: list[dict[str, Any]] = []
        viewport, roi = profile.viewport, profile.result_roi
        source_roi = (viewport.x + roi.x*viewport.width, viewport.y + roi.y*viewport.height,
                      roi.width*viewport.width, roi.height*viewport.height)
        for index, window in enumerate(windows):
            if window['kind'] == 'VS_SCREEN':
                continue
            with closing(sampler.sample_roi(video, roi=source_roi, reference_size=profile.reference_size,
                         fps=60, start_s=window['start'], end_s=window['end'], output_size=detector.output_size,
                         cancellation=token)) as frames:
                for frame in frames:
                    token.raise_if_cancelled()
                    visible = window['visible']
                    label = ('NEGATIVE' if window['kind'] == 'NEGATIVE' else window['kind']
                             if visible is not None and visible[0] <= frame.timestamp <= visible[1] else 'UNREVIEWED')
                    samples.append({'timestamp': frame.timestamp, 'group': index, 'label': label,
                                    'variant': 'native', 'image': frame.image})
                    if len(samples) > 20000:
                        raise ValueError('Experiment exceeds 20000 frames; use smaller annotation batches')
        native = compare_samples(profile, samples, cancellation=token)
        synthetic: list[dict[str, Any]] = []
        if args.robustness:
            for index, window in enumerate(windows):
                reviewed = [sample for sample in samples if sample['group'] == index and sample['label'] in ('VICTORY','DEFEAT')]
                if not reviewed:
                    continue
                peak = max(reviewed, key=lambda sample: detector.score_roi(sample['image'])['scores'][sample['label']])
                for name, image in robustness_variants(peak['image']):
                    synthetic.append({**peak, 'variant': name, 'image': image})
            native['robustness'] = compare_samples(profile, synthetic, cancellation=token)
        if args.ocr_manifest:
            output.mkdir(parents=True, exist_ok=True)
            directory = Path(tempfile.mkdtemp(prefix='ocr-frames-', dir=output))
            cv = require_opencv()
            manifest = []
            for index, sample in enumerate([*samples, *synthetic]):
                if sample['label'] == 'UNREVIEWED':
                    continue
                file = directory/f'{index:06}.png'
                upscaled = cv.resize(sample['image'], None, fx=4, fy=4, interpolation=cv.INTER_CUBIC)
                file.write_bytes(cv.imencode('.png', upscaled)[1].tobytes())
                manifest.append({'id': str(index), 'path': str(file), **{key: value for key, value in sample.items() if key != 'image'}})
            manifest_path = ensure_output_is_safe(output/'ocr_manifest.json', protected)
            # The manifest is an array for the small optional Windows OCR helper.
            atomic_write_json(output/'ocr_provenance.json', {'source_fingerprint':identity['fingerprint'], 'role':'calibration'})
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=True), encoding='utf-8')
            native['ocr_manifest'] = str(manifest_path)
        if video_identity(video) != identity:
            raise ValueError('Video changed during the calibration experiment')
        native['video_fingerprint'] = identity['fingerprint']
        native['role'] = 'calibration'
        atomic_write_json(report_path, native)
        print(report_path)
        return 0
    except KeyboardInterrupt:
        return 70
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(2, f'{exc}\n')


if __name__ == '__main__':
    raise SystemExit(main())
