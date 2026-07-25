from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from analysis.cut_engine import Segment


@dataclass
class TrackState:
    path: Optional[str] = None
    media_id: Optional[str] = None
    audio_track_id: Optional[str] = None
    video_track_id: Optional[str] = None
    audio_clip_id: Optional[str] = None
    video_clip_id: Optional[str] = None
    duration: float = 0.0
    rms: Optional[np.ndarray] = None
    hop_s: float = 0.03
    cuts: list[Segment] = field(default_factory=list)
    keeps: list[Segment] = field(default_factory=list)
    cuts_enabled: bool = False
    manual_cuts: list[Segment] = field(default_factory=list)
    suppressed_cuts: list[Segment] = field(default_factory=list)
    undo_stack: list[tuple[list[Segment], list[Segment]]] = field(default_factory=list)
    redo_stack: list[tuple[list[Segment], list[Segment]]] = field(default_factory=list)
    pending_cut_start: float | None = None
    pending_cut_end: float | None = None
    rms_min: float = 0.0
    rms_max: float = 0.0
    rms_eps: float = 1e-9
    cfg: dict = field(default_factory=dict)
    filters_restored: bool = False
    cuts_restored: bool = False
    video_color: tuple[int, int, int, int] | None = None
    video_edge: tuple[int, int, int] | None = None
    segment_group_id: str | None = None
    segment_index: int = 1
    segment_source_in: float = 0.0
    segment_source_out: float = 0.0
    classic_cuts: list[Segment] = field(default_factory=list)
    classic_keeps: list[Segment] = field(default_factory=list)
    classic_manual_cuts: list[Segment] = field(default_factory=list)
    classic_suppressed_cuts: list[Segment] = field(default_factory=list)
    classic_cuts_enabled: bool = False
    ai_cuts: list[Segment] = field(default_factory=list)
    ai_keeps: list[Segment] = field(default_factory=list)
    ai_manual_cuts: list[Segment] = field(default_factory=list)
    ai_suppressed_cuts: list[Segment] = field(default_factory=list)
    ai_cuts_enabled: bool = False
    ai_speech: list[Segment] = field(default_factory=list)
    ai_speech_raw: list[Segment] = field(default_factory=list)
    ai_speaker_ids: list[int] | None = None
    copy_source_id: str | None = None
    copy_index: int = 0
