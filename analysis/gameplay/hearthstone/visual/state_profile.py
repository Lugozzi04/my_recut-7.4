"""Explicit experimental profiles; no defaults replace the established detector."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from .templates import NormalizedROI, TemplatePackError, TemplateRepository, require_opencv


def number(raw: Any, name: str, low: float, high: float) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (float, int)) or not math.isfinite(raw) or not low <= raw <= high:
        raise TemplatePackError(f'{name} must be a finite number in [{low}, {high}]')
    return float(raw)


class StateAwareProfile:
    """Load bounded, pack-relative assets and fingerprint their actual bytes.

    Provenance is descriptive, never an authentication or validation oracle.
    Profiles are explicit opt-in calibration artifacts, not runtime user state.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        if self.path.stat().st_size > 1024 * 1024:
            raise TemplatePackError('State profile is too large')
        raw = json.loads(self.path.read_text(encoding='utf-8'))
        if not isinstance(raw, dict) or type(raw.get('schema_version')) is not int or raw['schema_version'] != 1:
            raise TemplatePackError('Expected state-aware profile schema_version=1')
        if raw.get('profile') != 'hearthstone-state-aware':
            raise TemplatePackError('Expected hearthstone-state-aware profile')
        size = raw.get('reference_size')
        if not isinstance(size, list) or len(size) != 2 or any(type(v) is not int or not 16 <= v <= 2048 for v in size):
            raise TemplatePackError('reference_size must contain two integers in 16..2048')
        from ..state_monitor import MonitorSettings
        try:
            MonitorSettings(**raw.get('monitor', {}))
        except (TypeError, ValueError) as exc:
            raise TemplatePackError(f'Invalid monitor configuration: {exc}') from exc
        self.reference_size = (size[0], size[1])
        self.viewport = NormalizedROI.from_value(raw.get('viewport', [0, 0, 1, 1]), name='viewport')
        self.config: dict[str, Any] = deepcopy(raw)
        repository = TemplateRepository(self.path)
        self.assets: dict[str, np.ndarray] = {}
        self.asset_paths: list[Path] = []
        digest = hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(',', ':')).encode('utf-8'))
        cv = require_opencv()
        result = raw.get('result')
        board = raw.get('board')
        if not isinstance(result, dict) or not isinstance(board, dict):
            raise TemplatePackError('State profile needs result and board configurations')
        self.result_roi = NormalizedROI.from_value(result.get('roi'), name='result.roi')
        if 'context_roi' in result:
            NormalizedROI.from_value(result['context_roi'], name='result.context_roi')
        if board.get('representation', 'masked_canny') != 'masked_canny' or board.get('aggregation', 'minimum') != 'minimum':
            raise TemplatePackError('Board representation must be masked_canny with minimum aggregation')
        if result.get('representation') not in ('gray', 'clahe', 'gradient'):
            raise TemplatePackError('result.representation must be gray, clahe or gradient')
        number(result.get('threshold'), 'result.threshold', 0.01, 1)
        number(result.get('margin'), 'result.margin', 0, 1)
        scales = result.get('scales')
        if not isinstance(scales, list) or not 1 <= len(scales) <= 16:
            raise TemplatePackError('result.scales must contain 1..16 values')
        for scale in scales:
            number(scale, 'result.scale', 0.5, 2)
        if len(set(scales)) != len(scales):
            raise TemplatePackError('result.scales must be unique')
        templates = result.get('templates')
        anchors = board.get('anchors')
        if not isinstance(templates, list) or not 2 <= len(templates) <= 32:
            raise TemplatePackError('result.templates must contain 2..32 templates')
        if not isinstance(anchors, list) or not 1 <= len(anchors) <= 3:
            raise TemplatePackError('board.anchors must contain 1..3 anchors')
        kinds: set[str] = set()
        for item in templates:
            if not isinstance(item, dict) or item.get('class') not in ('VICTORY', 'DEFEAT'):
                raise TemplatePackError('Result templates must be VICTORY or DEFEAT')
            kinds.add(item['class'])
        if kinds != {'VICTORY', 'DEFEAT'}:
            raise TemplatePackError('Both result classes are required')
        all_assets = list(templates)
        names: set[str] = set()
        for anchor in anchors:
            if not isinstance(anchor, dict) or not isinstance(anchor.get('id'), str) or not anchor['id']:
                raise TemplatePackError('Each board anchor requires an id')
            if anchor['id'] in names:
                raise TemplatePackError('Board anchor ids must be unique')
            names.add(anchor['id'])
            NormalizedROI.from_value(anchor.get('roi'), name='board.anchor.roi')
            NormalizedROI.from_value(anchor.get('exclude'), name='board.anchor.exclude')
            number(anchor.get('padding_px', 8), 'board.anchor.padding_px', 0, 32)
            images = anchor.get('templates')
            if not isinstance(images, list) or not 1 <= len(images) <= 8:
                raise TemplatePackError('Each board anchor needs 1..8 templates')
            all_assets.extend(images)
        for item in all_assets:
            if not isinstance(item, dict) or not isinstance(item.get('path'), str):
                raise TemplatePackError('Each asset needs a relative path')
            relative = item['path']
            file = repository.resolve_directory(relative)
            if file.stat().st_size > 4 * 1024 * 1024:
                raise TemplatePackError('State-aware image asset is too large')
            data = file.read_bytes()
            image = cv.imdecode(np.frombuffer(data, dtype=np.uint8), cv.IMREAD_GRAYSCALE)
            if image is None or min(image.shape) < 2 or max(image.shape) > 2048 or float(image.std()) < 1:
                raise TemplatePackError(f'Invalid or constant state-aware asset: {relative}')
            if relative in self.assets:
                continue
            self.assets[relative] = image
            self.asset_paths.append(file)
            digest.update(relative.encode('utf-8'))
            digest.update(data)
        self.fingerprint = digest.hexdigest()

    def describe(self) -> dict[str, Any]:
        return {'profile': deepcopy(self.config), 'fingerprint': self.fingerprint}
