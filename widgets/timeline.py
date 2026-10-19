"""Editor timeline.

Lanes (top → bottom): ruler · CUTS · VIDEO · AUDIO, with a header column on the left. Gameplay
editing is shown on these lanes: kept WIN islands are large green overlays, everything outside them
is dimmed (no separate lane).

Rendering: everything except playhead, hover line and marquee is drawn into a cached pixmap that is
only rebuilt when data/view/selection/size change; a playhead move repaints two thin strips. This
keeps the GUI thread free for video frames regardless of zoom (see docs/GUI_REDESIGN.md §17). Cuts are coloured by provenance (automatic / manual / gameplay)
and restored ranges are shown hatched, so the timeline explains *why* something is removed.

Data model is unchanged: MainWindow passes `tracks` (dicts) with `clips`; audio clips carry
segment-local `cuts` plus optional provenance lists (`manual_cuts`, `gameplay_cuts`,
`restored_cuts`, `gameplay_keeps`). Every mapping between time and x goes through
`_t_to_x/_x_to_t` (which account for the header column).
"""
from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from typing import Any, List, Optional

import numpy as np
from PySide6.QtCore import QLineF, QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QPixmap,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import QToolTip, QWidget

from analysis.cut_engine import Segment
from analysis.hero_groups.labels import CLASS_COLORS, class_id, class_label
from utils.timefmt import fmt_hms

HEADER_W = 92
RULER_H = 28
LANE_THIN_H = 20
LANE_GAP = 4
TOP_PAD = 6
BOTTOM_PAD = 8
RIGHT_PAD = 10
EDGE_GRAB_PX = 5
DRAG_THRESHOLD_PX = 4
MIN_CUT_S = 0.05

_LABEL_STEPS = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200]


def _choose_major_step(duration_s: float) -> int:
    """Kept for backwards compatibility (old tick logic)."""
    candidates = [5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600]
    target = max(1.0, duration_s / 10.0)
    return min(candidates, key=lambda c: abs(c - target))


def _track_attr(track: Any, name: str, default=None):
    if isinstance(track, dict):
        return track.get(name, default)
    return getattr(track, name, default)


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def classify_cuts(cuts: List[Segment], manual: Optional[List[Segment]] = None,
                  gameplay: Optional[List[Segment]] = None) -> List[str]:
    """Label each effective cut by provenance: 'manual', 'gameplay' or 'auto'.

    A cut is attributed to the source covering most of it (manual wins ties because the user's
    explicit edit is the most specific reason). Pure function, no cut logic is changed.
    """
    manual = list(manual or [])
    gameplay = list(gameplay or [])
    out: List[str] = []
    for c in cuts or []:
        try:
            a, b = float(c.start), float(c.end)
        except Exception:
            out.append("auto")
            continue
        dur = max(1e-9, b - a)
        m = sum(_overlap(a, b, float(s.start), float(s.end)) for s in manual)
        g = sum(_overlap(a, b, float(s.start), float(s.end)) for s in gameplay)
        if m >= 0.5 * dur and m >= g:
            out.append("manual")
        elif g >= 0.5 * dur:
            out.append("gameplay")
        elif m > 0 and m >= g:
            out.append("manual")
        else:
            out.append("auto")
    return out


@dataclass
class TimelineTrack:
    name: str
    duration: float
    rms: Optional[np.ndarray]
    hop_s: float
    cuts: List[Segment]
    clips: List[dict] = field(default_factory=list)
    kind: str = "audio"


@dataclass
class _TrackCache:
    cached_w: int = -1
    cached_env: Optional[np.ndarray] = None
    cached_view: tuple[float, float, int] = (-1.0, -1.0, -1)


@dataclass
class _Lane:
    kind: str          # "cuts" | "video" | "audio" | "gameplay"
    track_idx: int     # timeline track the lane belongs to
    top: float
    h: float

    @property
    def bottom(self) -> float:
        return self.top + self.h


class _Colors:
    """Resolved colours for painting (from ui.design_tokens.Palette or defaults)."""

    def __init__(self, pal: Any = None):
        def g(name: str, default: str) -> QColor:
            return QColor(getattr(pal, name, default) if pal is not None else default)

        self.panel = g("panel", "#14171C")
        self.app = g("app", "#0D0F12")
        self.raised = g("raised", "#1C2027")
        self.hover = g("hover", "#252A33")
        self.border = g("border", "#22262E")
        self.border_strong = g("border_strong", "#323843")
        self.text = g("text", "#E9EDF3")
        self.text_2 = g("text_2", "#A3ACB9")
        self.text_3 = g("text_3", "#6A7383")
        self.accent = g("accent", "#4D8DFF")
        self.auto = g("cut_auto", "#F2555A")
        self.manual = g("cut_manual", "#F5A524")
        self.gameplay = g("cut_gameplay", "#A579F7")
        self.restored = g("restored", "#3DD68C")
        self.keep_island = g("keep_island", "#2FBF7F")
        self.wave = g("wave", "#5AA2FF")
        self.wave_removed = g("wave_removed", "#3A414D")
        self.playhead = g("playhead", "#FF4F55")
        self.clip_video = g("clip_video", "#2E5F8F")
        self.pending = QColor("#FFC94D")

    def cut(self, kind: str) -> QColor:
        return {"manual": self.manual, "gameplay": self.gameplay}.get(kind, self.auto)


def _alpha(c: QColor, a: int) -> QColor:
    out = QColor(c)
    out.setAlpha(max(0, min(255, int(a))))
    return out


class TimelineWidget(QWidget):
    seekRequested = Signal(float)
    trackSelected = Signal(int)
    clipMoveRequested = Signal(str, float)  # clip_id, new_start
    clipSwapRequested = Signal(str, str)  # dragged_clip_id, target_clip_id
    clipActivated = Signal(int)  # track_state_idx (for single audio row)
    clipSplitRequested = Signal(str, float)  # clip_id, timeline_time

    # emitted when user clicks a single cut (selection; MainWindow decides what to show)
    cutClicked = Signal(int, object)   # index, globalPos
    # emitted after marquee selection
    cutsSelected = Signal(list, object)  # indices, globalPos
    # selection changed for the active clip (indices into its cuts)
    selectionChanged = Signal(list)
    # drag of a cut edge finished: index, new start, new end (segment-local seconds)
    cutResizeRequested = Signal(int, float, float)
    contextMenuRequested = Signal(float, object, object)  # timeSec, globalPos, meta
    # zoom / pan changed (zoom index)
    viewChanged = Signal(int)
    # gameplay WIN islands: selection ((track_state_idx, island_index) or None) and boundary edit
    islandSelected = Signal(object)
    gameToggleRequested = Signal(int, str)
    gameResizeRequested = Signal(int, str, float, float)
    islandResizeRequested = Signal(int, int, float, float)  # track_state_idx, island, start, end (local)

    # scrubbing lifecycle (for MainWindow sync)
    scrubStarted = Signal()
    scrubEnded = Signal()

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(160)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.ClickFocus)
        self.duration: float = 0.0  # global max duration across tracks
        self.playhead: float = 0.0

        self.tracks: List[Any] = []
        self.active_track: int = 0
        self._track_cache: List[_TrackCache] = []

        # viewport / zoom
        self.view_start: float = 0.0
        self.view_span: float = 0.0  # 0 = auto (usa durata)
        self.follow_playhead: bool = True
        self._zoom_levels = [1, 2, 3, 4, 6, 8, 12, 16, 24, 32, 48, 64]
        self._zoom_index = 0

        self._cached_w = -1
        self._cached_env: Optional[np.ndarray] = None
        self._cached_view: tuple[float, float] = (-1.0, -1.0)

        # selection state for cuts (per timeline track)
        self._selected_by_track: dict[int, set[int]] = {}
        self._sel_start_x: Optional[float] = None
        self._sel_current_x: Optional[float] = None
        self._sel_track_idx: Optional[int] = None
        self._sel_clip: Optional[dict] = None
        self._sel_base: set[int] = set()

        # pending cut markers (per-track, from MainWindow)
        self._pending_by_track: dict[int, tuple[Optional[float], Optional[float]]] = {}

        # scrubbing state (drag playhead / ruler)
        self._scrubbing: bool = False
        self._scrub_offset_px: float = 0.0

        # deferred click on empty lane space (seek on release, marquee on drag)
        self._press_pos: Optional[QPointF] = None
        self._press_t: float = 0.0
        self._press_shift: bool = False

        # cut edge resize
        self._resize: Optional[dict] = None

        # clip drag state
        self._drag_clip: Optional[dict] = None
        self._drag_track_idx: Optional[int] = None
        self._drag_start_t: float = 0.0
        self._drag_clip_start: float = 0.0
        self._drag_clip_end: float = 0.0
        self._drag_offset_t: float = 0.0
        self._drag_preview_start: Optional[float] = None
        self._drag_started: bool = False
        self._drag_press_x: float = 0.0

        # hover
        self._hover_x: Optional[float] = None
        self._hover_cut: Optional[int] = None
        self._hover_edge: Optional[str] = None

        self._snap_enabled: bool = False
        self._tool_mode: str = "select"
        self._colors = _Colors()
        # voice-threshold preview (fraction of the clip's peak RMS) or None
        self._threshold_guide: Optional[float] = None
        # gameplay islands
        self._selected_island: Optional[tuple[int, int]] = None
        self._island_drag: Optional[dict] = None
        # static layer cache
        self._static_rev = 0
        self._static_cache: Optional[QPixmap] = None
        self._static_cache_key: tuple = ()
        self.static_renders = 0  # diagnostics: how often the static layer was rebuilt
        # Temporary, view-only highlight of one detected game (track state index, start, end
        # in the same domain as the gameplay islands); never part of the project or the cuts.
        self._game_highlight: Optional[tuple[int, float, float]] = None

    # -----------------------------
    # Appearance / modes
    # -----------------------------
    def setGameHighlight(self, track_state_idx: Optional[int], start: float = 0.0, end: float = 0.0) -> None:
        """Yellow band over [start, end] of a track's games (gameplay-island domain); None clears it."""
        self._game_highlight = (int(track_state_idx), float(start), float(end))             if track_state_idx is not None and end > start else None
        self.update()  # dynamic layer only: no static rebuild

    def gameHighlight(self) -> Optional[tuple[int, float, float]]:
        return self._game_highlight

    def _paint_game_highlight(self, p: QPainter, lanes: list[_Lane]) -> None:
        if self._game_highlight is None or not lanes:
            return
        ts, a, b = self._game_highlight
        top, bottom = lanes[0].top, lanes[-1].bottom
        yellow = QColor(255, 214, 10)
        for clip in self._audio_clips():
            if clip.get("track_state_idx") != ts:
                continue
            c0 = float(clip.get("start", 0.0))
            c1 = float(clip.get("end", c0))
            t0, t1 = max(c0, c0 + a), min(c1, c0 + b)  # same mapping as _gameplay_islands
            if t1 <= t0:
                continue
            x1, x2 = self._t_to_xf(t0), self._t_to_xf(t1)
            rect = QRectF(x1, top, max(2.0, x2 - x1), bottom - top)
            p.fillRect(rect, _alpha(yellow, 48))
            p.setPen(QPen(yellow, 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(rect.adjusted(1, 1, -1, -1))

    def setPalette_tokens(self, pal: Any) -> None:
        self._colors = _Colors(pal)
        self._changed()

    def setToolMode(self, mode: str) -> None:
        m = str(mode).lower().strip()
        if m not in {"select", "split", "cut"}:
            m = "select"
        self._tool_mode = m
        if m in {"split", "cut"}:
            self.setCursor(Qt.CrossCursor)
        else:
            self.unsetCursor()
        self._changed()

    def setThresholdGuide(self, level: Optional[float]) -> None:
        """Show the voice threshold as a dashed level on the AUDIO lane (None hides it).

        `level` is the detection amplitude relative to the clip's peak RMS, the same reference the
        waveform is normalised to, so the line sits exactly where quiet audio becomes "silence".
        """
        value = None if level is None else max(0.0, min(1.0, float(level)))
        if value != self._threshold_guide:
            self._threshold_guide = value
            self._changed()

    def setSnapEnabled(self, enabled: bool) -> None:
        self._snap_enabled = bool(enabled)
        self._changed()

    # -----------------------------
    # Data / view
    # -----------------------------
    def _track_count(self) -> int:
        return int(len(self.tracks))

    def _track(self, idx: int) -> Optional[Any]:
        if 0 <= idx < len(self.tracks):
            return self.tracks[idx]
        return None

    def _track_kind(self, idx: int) -> str:
        t = self._track(idx)
        return str(_track_attr(t, "kind", "audio") or "audio") if t is not None else ""

    def _audio_track_index(self) -> Optional[int]:
        for i, t in enumerate(self.tracks):
            if str(_track_attr(t, "kind", "audio") or "audio") == "audio":
                return i
        return None

    def _track_cuts(self, idx: int) -> List[Segment]:
        """Cuts of the active clip on that track (selection indices refer to this list)."""
        clip = self._active_clip(idx)
        if clip is not None:
            return list(clip.get("cuts") or [])
        t = self._track(idx)
        return list(_track_attr(t, "cuts", []) or []) if t is not None else []

    def _track_active_state(self, track: Any) -> Optional[int]:
        v = _track_attr(track, "active_state_idx", None)
        if v is None:
            return None
        try:
            return int(v)
        except Exception:
            return None

    def _clip_matches_active(self, track: Any, clip: dict) -> bool:
        active = self._track_active_state(track)
        if active is None:
            return True
        try:
            clip_state = clip.get("track_state_idx", None)
        except Exception:
            clip_state = None
        if clip_state is None:
            return True
        try:
            return int(clip_state) == int(active)
        except Exception:
            return True

    def _active_clip(self, track_idx: int) -> Optional[dict]:
        t = self._track(track_idx)
        if t is None:
            return None
        for c in _track_attr(t, "clips", []) or []:
            if self._clip_matches_active(t, c):
                return c
        return None

    def _cut_index_at_x(self, track_idx: int, x: float) -> Optional[int]:
        """Return index of cut under x (in widget coords) in the clip under x, if any."""
        if self.duration <= 0:
            return None
        hit = self._cut_hit(track_idx, x)
        return hit[0] if hit else None

    def _cut_hit(self, track_idx: Optional[int], x: float, *, pad_px: float = 0.0,
                 require_active: bool = True) -> Optional[tuple[int, dict, Segment]]:
        if track_idx is None:
            return None
        t = self._track(track_idx)
        if t is None:
            return None
        clip = self._clip_at_x(track_idx, x)
        if clip is None:
            cuts = _track_attr(t, "cuts", []) or []
            time = self._x_to_t(x)
            pad = self._px_to_s(pad_px)
            for i, c in enumerate(cuts):
                if float(c.start) - pad <= time <= float(c.end) + pad:
                    return i, {}, c
            return None
        if require_active and not self._clip_matches_active(t, clip):
            return None
        c_start = float(clip.get("start", 0.0))
        local = self._x_to_t(x) - c_start
        pad = self._px_to_s(pad_px)
        kinds = self._clip_cut_kinds(clip)
        for i, c in enumerate(clip.get("cuts") or []):
            if i < len(kinds) and kinds[i] == "gameplay":
                continue
            try:
                if float(c.start) - pad <= local <= float(c.end) + pad:
                    return i, clip, c
            except Exception:
                continue
        return None

    def _restored_hit(self, track_idx: int, x: float) -> Optional[tuple[dict, Segment]]:
        clip = self._clip_at_x(track_idx, x)
        if clip is None:
            return None
        local = self._x_to_t(x) - float(clip.get("start", 0.0))
        for r in clip.get("restored_cuts") or []:
            if float(r.start) <= local <= float(r.end):
                return clip, r
        return None

    def _invalidate_all_caches(self) -> None:
        self._cached_w = -1
        self._cached_env = None
        for c in self._track_cache:
            c.cached_w = -1
            c.cached_env = None

    def setTracks(self, tracks: List[Any], reset_view: bool = False, global_duration: float | None = None):
        old_duration = float(self.duration)
        old_count = len(self.tracks)

        self.tracks = list(tracks or [])
        if self.tracks:
            self.active_track = max(0, min(self.active_track, len(self.tracks) - 1))
        else:
            self.active_track = 0
        self._track_cache = [_TrackCache() for _ in self.tracks]

        max_idx = len(self.tracks) - 1
        self._pending_by_track = {i: v for i, v in self._pending_by_track.items() if i <= max_idx}
        pruned: dict[int, set[int]] = {}
        for i, sel in self._selected_by_track.items():
            if i > max_idx:
                continue
            n = len(self._track_cuts(i))
            keep = {j for j in sel if 0 <= j < n}
            if keep:
                pruned[i] = keep
        if pruned != self._selected_by_track:
            self._selected_by_track = pruned
            self.selectionChanged.emit(self.selectedCutIndices())

        if self.tracks:
            track_max = max(0.0, max(float(_track_attr(t, "duration", 0.0)) for t in self.tracks))
        else:
            track_max = 0.0
        if global_duration is not None and float(global_duration) > 0:
            self.duration = max(track_max, float(global_duration))
        else:
            self.duration = track_max

        media_changed = (float(self.duration) != old_duration) or (len(self.tracks) != old_count)
        if reset_view or media_changed:
            self.view_start = 0.0
            self.view_span = float(self.duration) if self.duration > 0 else 0.0
            self._zoom_index = 0
            self.viewChanged.emit(self._zoom_index)

        thin = LANE_THIN_H
        min_h = RULER_H + TOP_PAD + thin + 56 * max(1, len(self.tracks)) + BOTTOM_PAD
        self.setMinimumHeight(max(170, int(min_h)))

        self._invalidate_all_caches()
        self._changed()

    def setData(self, duration: float, rms: Optional[np.ndarray], hop_s: float, cuts: List[Segment]):
        """Backward-compatible single-track update (updates active track)."""
        if not self.tracks:
            self.tracks = [TimelineTrack(name="Track 1", duration=float(duration), rms=rms, hop_s=float(hop_s),
                                         cuts=cuts[:])]
            self.active_track = 0
            self._track_cache = [_TrackCache()]
            self.setTracks(self.tracks, reset_view=True)
            return
        idx = max(0, min(self.active_track, len(self.tracks) - 1))
        t = self.tracks[idx]
        if isinstance(t, dict):
            t.update(duration=float(duration), rms=rms, hop_s=float(hop_s), cuts=cuts[:])
        else:
            t.duration = float(duration)
            t.rms = rms
            t.hop_s = float(hop_s)
            t.cuts = cuts[:]
        self.setTracks(self.tracks, reset_view=False)

    def setCuts(self, cuts: List[Segment], track_index: Optional[int] = None):
        """Update only cuts without resetting zoom/viewport."""
        if not self.tracks:
            return
        idx = self.active_track if track_index is None else int(track_index)
        if not (0 <= idx < len(self.tracks)):
            return
        t = self.tracks[idx]
        if isinstance(t, dict):
            t["cuts"] = cuts[:]
        else:
            t.cuts = cuts[:]
        self._changed()

    def setActiveTrack(self, index: int, emit: bool = False):
        if not self.tracks:
            self.active_track = 0
            return
        idx = max(0, min(int(index), len(self.tracks) - 1))
        if idx == self.active_track:
            return
        self.active_track = idx
        if emit:
            self.trackSelected.emit(idx)
        self._changed()

    def set_pending_cut(self, start: Optional[float], end: Optional[float], track_index: Optional[int] = None):
        idx = self.active_track if track_index is None else int(track_index)
        if idx < 0 or (self.tracks and idx >= len(self.tracks)):
            return
        self._pending_by_track[idx] = (start, end)
        self._changed()

    def setPlayhead(self, t: float):
        old = float(self.playhead)
        self.playhead = float(t)
        if self.duration > 0 and self.follow_playhead and not self._scrubbing:
            span = self._span()
            start = float(self.view_start)
            end = start + span
            # Page-turn follow: when the playhead leaves the view (or nears its right edge) the view
            # jumps once, instead of scrolling — and re-rendering everything — on every tick.
            if span < self.duration and (self.playhead < start or self.playhead > end - 0.04 * span):
                new_start = max(0.0, min(self.playhead - 0.1 * span, self.duration - span))
                if abs(new_start - start) > 1e-9:
                    self.view_start = new_start
                    self._invalidate_all_caches()
                    self.update()
                    return
        self._update_playhead_strip(old)
        self._update_playhead_strip(self.playhead)

    def _update_playhead_strip(self, t: float) -> None:
        if self.duration <= 0:
            self.update()
            return
        x = int(round(self._t_to_xf(t)))
        self.update(QRect(x - 8, 0, 17, self.height()))

    # ---- selection API ----------------------------------------------------------------------
    def _selection_track(self) -> int:
        idx = self._audio_track_index()
        return int(idx if idx is not None else self.active_track)

    def selectedCutIndices(self) -> list[int]:
        return sorted(self._selected_by_track.get(self._selection_track(), set()))

    def setCutSelection(self, indices: list[int]) -> None:
        idx = self._selection_track()
        n = len(self._track_cuts(idx))
        sel = {int(i) for i in indices if 0 <= int(i) < n}
        if sel != self._selected_by_track.get(idx, set()):
            self._selected_by_track[idx] = sel
            self.selectionChanged.emit(sorted(sel))
        self._changed()

    def clearCutSelection(self) -> None:
        self.setCutSelection([])

    def selectedIsland(self) -> Optional[tuple[int, int]]:
        return self._selected_island

    def setSelectedIsland(self, sel: Optional[tuple[int, int]], emit: bool = False) -> None:
        sel = (int(sel[0]), int(sel[1])) if sel is not None else None
        if sel != self._selected_island:
            self._selected_island = sel
            if emit:
                self.islandSelected.emit(sel)
            self._changed()

    def _gameplay_islands(self) -> list[dict]:
        """Kept WIN islands of every audio clip with gameplay editing active (global time)."""
        out: list[dict] = []
        for clip in self._audio_clips():
            boxes = clip.get("gameplay_boxes")
            if boxes is not None:
                c0 = float(clip.get("start", 0.0))
                c1 = float(clip.get("end", c0))
                for i, info in enumerate(boxes):
                    a, b = float(info["start"]), float(info["end"])
                    drag = self._island_drag
                    if drag is not None and drag.get("clip") is clip and drag.get("index") == i:
                        a, b = float(drag["start"]), float(drag["end"])
                    out.append({"clip": clip, "index": i, "ts": clip.get("track_state_idx"),
                                "start": a, "end": b, "t0": c0+a, "t1": min(c1, c0+b), "meta": info})
                continue
            keeps = clip.get("gameplay_keeps") or []
            if not keeps:
                continue
            c0 = float(clip.get("start", 0.0))
            c1 = float(clip.get("end", c0))
            ts = clip.get("track_state_idx")
            meta = clip.get("gameplay_islands") or []
            for i, k in enumerate(keeps):
                a, b = float(k.start), float(k.end)
                drag = self._island_drag
                if drag is not None and drag.get("clip") is clip and drag.get("index") == i:
                    a, b = float(drag["start"]), float(drag["end"])
                info = None
                best = 0.0
                for m in meta:
                    ov = _overlap(a, b, float(m.get("start", 0.0)), float(m.get("end", 0.0)))
                    if ov > best:
                        best, info = ov, m
                out.append({"clip": clip, "index": i, "ts": ts, "start": a, "end": b,
                            "t0": c0 + a, "t1": min(c1, c0 + b), "meta": info or {}})
        return out

    def _island_at_x(self, x: float) -> Optional[dict]:
        t = self._x_to_t(x)
        for isl in self._gameplay_islands():
            if isl["t0"] <= t <= isl["t1"]:
                return isl
        return None

    def _island_edge_at_x(self, x: float) -> Optional[tuple[dict, str]]:
        best = None
        best_d = EDGE_GRAB_PX + 1.5
        for isl in self._gameplay_islands():
            for edge, t in (("start", isl["t0"]), ("end", isl["t1"])):
                d = abs(self._t_to_xf(t) - x)
                if d < best_d:
                    best, best_d = (isl, edge), d
        return best

    # -----------------------------
    # Mapping & env
    # -----------------------------
    def _span(self) -> float:
        if self.duration <= 0:
            return 0.0
        s = float(self.view_span) if self.view_span > 0 else float(self.duration)
        return max(1e-9, min(s, float(self.duration)))

    def _content_w(self) -> int:
        return max(1, self.width() - HEADER_W - RIGHT_PAD)

    def _px_to_s(self, px: float) -> float:
        return (float(px) / float(self._content_w())) * self._span() if self.duration > 0 else 0.0

    def _t_to_x(self, t: float) -> int:
        if self.duration <= 0:
            return HEADER_W
        span = self._span()
        rel = (float(t) - float(self.view_start)) / span
        w = self._content_w()
        x = rel * w
        x = max(-4.0, min(float(w) + 4.0, x))
        return int(round(HEADER_W + x))

    def _t_to_xf(self, t: float) -> float:
        if self.duration <= 0:
            return float(HEADER_W)
        rel = (float(t) - float(self.view_start)) / self._span()
        return HEADER_W + rel * self._content_w()

    def _x_to_t(self, x: float) -> float:
        if self.duration <= 0:
            return 0.0
        w = self._content_w()
        rel_x = max(0.0, min(float(w), float(x) - HEADER_W))
        return float(self.view_start) + (rel_x / float(w)) * self._span()

    def _env_for_rms(self, rms: Any, hop_s: float, w: int, start_s: float, end_s: float) -> Optional[np.ndarray]:
        if rms is not None and not isinstance(rms, np.ndarray):
            try:
                rms = np.asarray(rms, dtype=np.float32)
            except Exception:
                rms = None
        if rms is None or getattr(rms, "size", 0) == 0 or w <= 2 or self.duration <= 0:
            return None
        hop_s = float(hop_s or 0.03)
        n = int(rms.size)
        i0 = int(max(0, min(n, start_s / hop_s)))
        i1 = int(max(0, min(n, end_s / hop_s)))
        if i1 <= i0:
            return None
        bins = max(2, int(w))
        window = rms[i0:i1]
        starts = np.linspace(0, window.size, bins + 1).astype(int)[:-1]
        starts = np.minimum(starts, window.size - 1)
        # max per pixel column (reduceat reduces [starts[i], starts[i+1]) and handles repeats)
        env = np.maximum.reduceat(window, starts).astype(np.float32)
        # normalise against the whole clip so loudness reads the same at every zoom level
        m = float(np.max(rms))
        if m > 1e-9:
            env = env / m
        # gentle perceptual curve: quiet speech stays visible without saturating the lane
        return np.power(np.clip(env, 0.0, 1.0), 0.7)

    def _env_for_width(self, track_idx: int, w: int, start_s: float, end_s: float) -> Optional[np.ndarray]:
        t = self._track(track_idx)
        if t is None:
            return None
        return self._env_for_rms(_track_attr(t, "rms", None), float(_track_attr(t, "hop_s", 0.03) or 0.03),
                                 w, start_s, end_s)

    # ---- lanes --------------------------------------------------------------------------------
    def _has_gameplay(self) -> bool:
        return any(c.get("gameplay_boxes") or c.get("gameplay_keeps") for c in self._audio_clips())

    def _lanes(self) -> list[_Lane]:
        lanes: list[_Lane] = []
        if not self.tracks:
            return lanes
        audio_idx = self._audio_track_index()
        thin_kinds: list[str] = []
        if audio_idx is not None:
            thin_kinds.append("cuts")
        weights = {"video": 0.8, "audio": 1.5}
        y = float(RULER_H + TOP_PAD)
        avail = float(self.height() - y - BOTTOM_PAD)
        n_lanes = len(self.tracks) + len(thin_kinds)
        avail -= LANE_THIN_H * len(thin_kinds) + LANE_GAP * max(0, n_lanes - 1)
        total_w = sum(weights.get(str(_track_attr(t, "kind", "audio")), 1.0) for t in self.tracks) or 1.0
        if audio_idx is not None:
            lanes.append(_Lane("cuts", audio_idx, y, LANE_THIN_H))
            y += LANE_THIN_H + LANE_GAP
        for i, t in enumerate(self.tracks):
            kind = str(_track_attr(t, "kind", "audio") or "audio")
            h = max(28.0, avail * weights.get(kind, 1.0) / total_w)
            lanes.append(_Lane(kind, i, y, h))
            y += h + LANE_GAP
        return lanes

    def _lane_at_y(self, y: float) -> Optional[_Lane]:
        for lane in self._lanes():
            if lane.top - LANE_GAP / 2 <= y <= lane.bottom + LANE_GAP / 2:
                return lane
        return None

    def _track_at_y(self, y: float) -> Optional[int]:
        lane = self._lane_at_y(y)
        return lane.track_idx if lane is not None else None

    def _clip_at_x(self, track_idx: int, x: float) -> Optional[dict]:
        t = self._track(track_idx)
        if t is None:
            return None
        clips = _track_attr(t, "clips", []) or []
        if not clips:
            return None
        time = self._x_to_t(x)
        for c in clips:
            try:
                cs = float(c.get("start", 0.0))
                ce = float(c.get("end", 0.0))
            except Exception:
                continue
            if cs <= time <= ce:
                return c
        return None

    def _snap_time(self, proposed_start: float, dur: float, ignore_clip_id: Optional[str]) -> float:
        if not self._snap_enabled or self.duration <= 0:
            return proposed_start
        thr = self._px_to_s(10.0)
        best = proposed_start
        best_diff = thr + 1.0

        def consider(edge: float, mode: str):
            nonlocal best, best_diff
            cand = edge if mode == "start" else edge - dur
            diff = abs(cand - proposed_start)
            if diff < best_diff:
                best_diff = diff
                best = cand

        consider(0.0, "start")
        consider(float(self.playhead), "start")
        consider(float(self.playhead), "end")
        for t in self.tracks:
            for c in _track_attr(t, "clips", []) or []:
                if ignore_clip_id and c.get("clip_id") == ignore_clip_id:
                    continue
                try:
                    cs = float(c.get("start", 0.0))
                    ce = float(c.get("end", 0.0))
                except Exception:
                    continue
                for edge in (cs, ce):
                    consider(edge, "start")
                    consider(edge, "end")
        return best if best_diff <= thr else proposed_start

    # -----------------------------
    # Zoom
    # -----------------------------
    def zoom_in(self):
        if self.duration <= 0:
            return
        if self._zoom_index < len(self._zoom_levels) - 1:
            self._zoom_index += 1
        self._apply_zoom_level()

    def zoom_out(self):
        if self.duration <= 0:
            return
        if self._zoom_index > 0:
            self._zoom_index -= 1
        self._apply_zoom_level()

    def zoom_reset(self):
        if self.duration <= 0:
            return
        self._zoom_index = 0
        self._apply_zoom_level(reset=True)

    def setZoomIndex(self, index: int, anchor_t: Optional[float] = None) -> None:
        if self.duration <= 0:
            return
        index = max(0, min(len(self._zoom_levels) - 1, int(index)))
        if index == self._zoom_index:
            return
        self._zoom_index = index
        self._apply_zoom_level(reset=(index == 0), anchor_t=anchor_t)

    def _apply_zoom_level(self, reset: bool = False, anchor_t: Optional[float] = None):
        z = self._zoom_levels[self._zoom_index]
        span = max(0.01, float(self.duration) / float(z))
        if reset:
            self.view_start = 0.0
        elif anchor_t is not None:
            # keep the time under the cursor fixed
            old_span = self._span() or span
            rel = (float(anchor_t) - float(self.view_start)) / old_span if old_span > 0 else 0.5
            rel = max(0.0, min(1.0, rel))
            new_start = float(anchor_t) - rel * span
            self.view_start = max(0.0, min(new_start, max(0.0, self.duration - span)))
        else:
            center = float(self.playhead)
            new_start = center - span * 0.5
            self.view_start = max(0.0, min(new_start, max(0.0, self.duration - span)))
        self.view_span = span
        self._invalidate_all_caches()
        self.viewChanged.emit(self._zoom_index)
        self._changed()

    def wheelEvent(self, e):
        if self.duration <= 0:
            return super().wheelEvent(e)
        delta = e.angleDelta().y() or e.angleDelta().x()
        if not delta:
            return
        if e.modifiers() & Qt.ControlModifier:
            anchor = self._x_to_t(float(e.position().x()))
            self.setZoomIndex(self._zoom_index + (1 if delta > 0 else -1), anchor_t=anchor)
            e.accept()
            return
        span = self._span()
        if span >= self.duration - 1e-6:
            return super().wheelEvent(e)
        step = span * 0.12 * (-1 if delta > 0 else 1)
        self.view_start = max(0.0, min(self.view_start + step, max(0.0, self.duration - span)))
        self.follow_playhead = False
        self._invalidate_all_caches()
        self._changed()
        e.accept()

    # -----------------------------
    # Paint
    # -----------------------------
    def _clip_cut_kinds(self, clip: dict) -> list[str]:
        kinds = clip.get("cut_kinds")
        cuts = clip.get("cuts") or []
        if isinstance(kinds, list) and len(kinds) == len(cuts):
            return kinds
        kinds = classify_cuts(cuts, clip.get("manual_cuts"), clip.get("gameplay_cuts"))
        clip["cut_kinds"] = kinds
        return kinds

    def _audio_clips(self) -> list[dict]:
        idx = self._audio_track_index()
        if idx is None:
            return []
        return list(_track_attr(self.tracks[idx], "clips", []) or [])

    def _removed_spans(self) -> list[tuple[float, float, str, bool]]:
        """(t0, t1, kind, selected) for every effective cut on the timeline."""
        out: list[tuple[float, float, str, bool]] = []
        audio_idx = self._audio_track_index()
        selected = self._selected_by_track.get(audio_idx, set()) if audio_idx is not None else set()
        atrack = self._track(audio_idx) if audio_idx is not None else None
        for clip in self._audio_clips():
            c0 = float(clip.get("start", 0.0))
            c1 = float(clip.get("end", c0))
            kinds = self._clip_cut_kinds(clip)
            sel = selected if (atrack is not None and self._clip_matches_active(atrack, clip)) else set()
            islands = [(c0 + float(k.start), c0 + float(k.end)) for k in (clip.get("gameplay_keeps") or [])]
            for i, c in enumerate(clip.get("cuts") or []):
                a = c0 + float(c.start)
                b = min(c1, c0 + float(c.end))
                if self._resize is not None and self._resize.get("clip") is clip and self._resize.get("idx") == i:
                    a = c0 + float(self._resize["start"])
                    b = c0 + float(self._resize["end"])
                if b <= a:
                    continue
                kind = kinds[i] if i < len(kinds) else "auto"
                if kind == "gameplay" and islands:
                    # outside the islands is shown dimmed by the gameplay overlay; only the parts of
                    # this cut that remove footage *inside* a kept game are drawn as ordinary cuts
                    for ia, ib in islands:
                        sa, sb = max(a, ia), min(b, ib)
                        if sb > sa:
                            out.append((sa, sb, "auto", i in sel))
                    continue
                out.append((a, b, kind, i in sel))
        return out

    def _changed(self) -> None:
        """Rendered content changed (data, selection, hover target, …): rebuild the static layer."""
        self._static_rev += 1
        self.update()

    def resizeEvent(self, e):
        self._static_cache = None
        return super().resizeEvent(e)

    def _ensure_static(self) -> QPixmap:
        dpr = float(self.devicePixelRatioF())
        key = (self.width(), self.height(), round(dpr, 3), round(float(self.view_start), 9),
               round(self._span(), 9), self._static_rev)
        if self._static_cache is None or key != self._static_cache_key:
            pm = QPixmap(max(1, int(round(self.width() * dpr))), max(1, int(round(self.height() * dpr))))
            pm.setDevicePixelRatio(dpr)
            pm.fill(self._colors.panel)
            painter = QPainter(pm)
            try:
                self._render_static(painter)
            except Exception:
                traceback.print_exc()
            finally:
                painter.end()
            self._static_cache = pm
            self._static_cache_key = key
            self.static_renders += 1
        return self._static_cache

    def paintEvent(self, _e):
        pm = self._ensure_static()
        p = QPainter(self)
        try:
            p.drawPixmap(0, 0, pm)  # clipped to the update region by Qt
            if self.duration > 0:
                lanes = self._lanes()
                p.save()
                p.setClipRect(QRectF(HEADER_W, 0, self.width() - HEADER_W, self.height()))
                self._paint_game_highlight(p, lanes)
                self._paint_hover_cut(p, lanes)
                self._paint_marquee(p, lanes)
                self._paint_hover_line(p, lanes)
                p.restore()
                self._paint_playhead(p, lanes, self.height())
        except Exception:
            traceback.print_exc()
        finally:
            p.end()

    def _render_static(self, p: QPainter) -> None:
        col = self._colors
        w, h = self.width(), self.height()
        base_font = QFont(self.font())
        if base_font.pixelSize() <= 0 and base_font.pointSize() <= 0:
            base_font.setPixelSize(12)
        p.setFont(base_font)
        p.fillRect(QRectF(0, 0, w, h), col.panel)
        lanes = self._lanes()
        for lane in lanes:
            r = QRectF(HEADER_W, lane.top, w - HEADER_W, lane.h)
            p.fillRect(r, col.app if lane.kind in ("video", "audio") else _alpha(col.app, 150))
        if self.duration > 0:
            spans = self._removed_spans()
            p.save()
            p.setClipRect(QRectF(HEADER_W, 0, w - HEADER_W, h))
            for lane in lanes:
                if lane.kind == "video":
                    self._paint_video_lane(p, lane, spans)
                elif lane.kind == "audio":
                    self._paint_audio_lane(p, lane, spans)
                elif lane.kind == "cuts":
                    self._paint_cuts_lane(p, lane, spans)
            self._paint_gameplay_overlay(p, lanes)
            self._paint_pending(p, lanes)
            p.restore()
            self._paint_ruler(p, w)
            self._paint_island_ruler_marks(p)
        else:
            self._paint_ruler(p, w)
            p.setPen(col.text_3)
            f = QFont(base_font)
            f.setPixelSize(13)
            p.setFont(f)
            p.drawText(QRectF(HEADER_W, RULER_H, w - HEADER_W, h - RULER_H), Qt.AlignCenter,
                       "Import a video to see its timeline")
            p.setFont(base_font)
        self._paint_headers(p, lanes, h)

    def _paint_headers(self, p: QPainter, lanes: list[_Lane], h: int) -> None:
        col = self._colors
        p.fillRect(QRectF(0, 0, HEADER_W, h), col.panel)
        p.setPen(QPen(col.border, 1))
        p.drawLine(HEADER_W - 1, 0, HEADER_W - 1, h)
        p.drawLine(0, RULER_H - 1, self.width(), RULER_H - 1)
        names = {"cuts": "CUTS", "video": "VIDEO", "audio": "AUDIO", "gameplay": "GAMEPLAY"}
        glyphs = {"cuts": "scissors", "video": "film", "audio": "wave", "gameplay": "gamepad"}
        try:
            from ui.widgets.glyphs import draw_glyph
        except Exception:  # pragma: no cover - glyphs are part of the app
            draw_glyph = None  # type: ignore[assignment]
        f = QFont(p.font())
        f.setPixelSize(10)
        f.setBold(True)
        f.setLetterSpacing(QFont.AbsoluteSpacing, 0.6)
        p.setFont(f)
        for lane in lanes:
            thin = lane.kind in ("cuts", "gameplay")
            text_c = col.text_2 if not thin else col.text_3
            if lane.kind in ("audio", "video") and lane.track_idx == self.active_track:
                p.fillRect(QRectF(0, lane.top, 3, lane.h), col.accent)
            cy = lane.top + lane.h / 2
            if draw_glyph is not None:
                draw_glyph(p, glyphs[lane.kind], QRectF(12, cy - 7, 14, 14), text_c, 1.9)
            p.setPen(text_c)
            p.drawText(QRectF(32, lane.top, HEADER_W - 36, lane.h), Qt.AlignVCenter | Qt.AlignLeft, names[lane.kind])
        p.setFont(QFont(self.font()))

    def _paint_ruler(self, p: QPainter, w: int) -> None:
        col = self._colors
        p.fillRect(QRectF(HEADER_W, 0, w - HEADER_W, RULER_H), col.panel)
        if self.duration <= 0:
            return
        span = self._span()
        vs = float(self.view_start)
        ve = vs + span
        px_per_s = self._content_w() / span
        step = next((s for s in _LABEL_STEPS if s * px_per_s >= 84), _LABEL_STEPS[-1])
        minor = step / 5.0 if step >= 5 else step / 2.0
        f = QFont(p.font())
        f.setPixelSize(10)
        p.setFont(f)
        t = int(vs / minor) * minor
        while t <= ve + minor:
            x = self._t_to_xf(t)
            if HEADER_W <= x <= w:
                is_major = abs((t / step) - round(t / step)) < 1e-6
                p.setPen(QPen(col.border_strong if not is_major else col.text_3, 1))
                p.drawLine(QPointF(x, RULER_H - (9 if is_major else 4)), QPointF(x, RULER_H - 1))
                if is_major:
                    label = fmt_hms(t) if step >= 1 else f"{fmt_hms(int(t))}.{int(round((t % 1) * 10))}"
                    p.setPen(col.text_2)
                    p.drawText(QPointF(x + 4, 13), label)
            t += minor
        p.setFont(QFont(self.font()))

    def _hatch(self, p: QPainter, rect: QRectF, color: QColor, spacing: float = 6.0) -> None:
        """Diagonal hatch in one call (pattern brush) instead of one drawLine per stroke."""
        if rect.width() <= 1:
            return
        p.fillRect(rect, QBrush(color, Qt.BDiagPattern))

    def _paint_video_lane(self, p: QPainter, lane: _Lane, spans) -> None:
        col = self._colors
        t = self._track(lane.track_idx)
        clips = _track_attr(t, "clips", []) or []
        top = lane.top + 2
        hh = lane.h - 4
        p.setRenderHint(QPainter.Antialiasing, True)
        labels = []
        for clip in clips:
            try:
                c_start = float(clip.get("start", 0.0))
                c_end = float(clip.get("end", 0.0))
            except Exception:
                continue
            if self._drag_clip is not None and clip.get("clip_id") == self._drag_clip.get("clip_id") \
                    and self._drag_preview_start is not None:
                dur = c_end - c_start
                c_start = float(self._drag_preview_start)
                c_end = c_start + dur
            x1, x2 = self._t_to_xf(c_start), self._t_to_xf(c_end)
            if x2 - x1 < 1:
                continue
            r = QRectF(x1 + 0.5, top, x2 - x1 - 1, hh)
            # neutral clip body so cut colours stay the loudest signal; each video keeps its own
            # identity colour as a thin stripe along the top edge
            base = col.clip_video
            g = QLinearGradient(0, top, 0, top + hh)
            g.setColorAt(0.0, base.lighter(112))
            g.setColorAt(1.0, base.darker(118))
            path = QPainterPath()
            path.addRoundedRect(r, 5, 5)
            p.fillPath(path, g)
            c_val = clip.get("edge") or clip.get("color")
            # identity stripe only distinguishes videos: pointless (and confusable with WIN green)
            # when the project has a single video
            if len(clips) > 1 and isinstance(c_val, (list, tuple)) and len(c_val) >= 3:
                ident = QColor(int(c_val[0]), int(c_val[1]), int(c_val[2]))
                p.save()
                p.setClipPath(path)
                p.fillRect(QRectF(r.left(), r.top(), r.width(), 3), ident)
                p.restore()
            p.setPen(QPen(_alpha(base.lighter(160), 170), 1))
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)
            labels.append((r, str(clip.get("name") or "")))
        # removed ranges: darken + hatch
        p.setRenderHint(QPainter.Antialiasing, False)
        for a, b, kind, selected in spans:
            x1, x2 = self._t_to_xf(a), self._t_to_xf(b)
            if x2 < HEADER_W or x1 > self.width():
                continue
            rr = QRectF(x1, top, max(1.0, x2 - x1), hh)
            p.fillRect(rr, QColor(0, 0, 0, 120))
            self._hatch(p, rr, _alpha(col.cut(kind), 70))
            if selected:
                p.fillRect(rr, _alpha(col.accent, 40))
        # clip names on top
        f = QFont(p.font())
        f.setPixelSize(11)
        f.setBold(True)
        p.setFont(f)
        fm = QFontMetrics(f)
        for r, name in labels:
            if r.width() < 40 or not name:
                continue
            # keep the name visible when the clip starts left of the viewport
            left = max(r.left(), float(HEADER_W))
            visible_w = r.right() - left
            if visible_w < 40:
                continue
            text = fm.elidedText(name, Qt.ElideRight, int(visible_w - 16))
            tw = fm.horizontalAdvance(text) + 12
            chip = QRectF(left + 6, r.top() + 8, tw, fm.height() + 4)
            p.setRenderHint(QPainter.Antialiasing, True)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, 120))
            p.drawRoundedRect(chip, 4, 4)
            p.setPen(QColor(255, 255, 255, 235))
            p.drawText(chip, Qt.AlignCenter, text)
        p.setFont(QFont(self.font()))
        p.setRenderHint(QPainter.Antialiasing, False)

    def _paint_audio_lane(self, p: QPainter, lane: _Lane, spans) -> None:
        col = self._colors
        t = self._track(lane.track_idx)
        clips = _track_attr(t, "clips", []) or []
        top = lane.top + 2
        hh = lane.h - 4
        mid = top + hh / 2
        vs = float(self.view_start)
        ve = vs + self._span()
        # clip backgrounds
        p.setRenderHint(QPainter.Antialiasing, True)
        for clip in clips:
            c_start = float(clip.get("start", 0.0))
            c_end = float(clip.get("end", 0.0))
            x1, x2 = self._t_to_xf(c_start), self._t_to_xf(c_end)
            if x2 - x1 < 1:
                continue
            r = QRectF(x1 + 0.5, top, x2 - x1 - 1, hh)
            p.setPen(QPen(_alpha(col.wave, 60), 1))
            p.setBrush(_alpha(col.wave, 18))
            p.drawRoundedRect(r, 5, 5)
        p.setRenderHint(QPainter.Antialiasing, False)

        # removed mask per pixel column
        width = self.width()
        removed = np.zeros(width + 2, dtype=np.int8)  # 0 keep, 1 removed, 2 removed+selected
        for a, b, _kind, selected in spans:
            x1 = int(max(0, self._t_to_xf(a)))
            x2 = int(min(width, self._t_to_xf(b) + 0.999))
            if x2 > x1:
                removed[x1:x2] = np.maximum(removed[x1:x2], 2 if selected else 1)

        for clip in clips:
            try:
                c_start = float(clip.get("start", 0.0))
                c_end = float(clip.get("end", 0.0))
                s_in = float(clip.get("source_in", 0.0))
                s_out = float(clip.get("source_out", s_in + (c_end - c_start)))
            except Exception:
                continue
            seg_start = max(vs, c_start)
            seg_end = min(ve, c_end)
            if seg_end <= seg_start:
                continue
            src_start = max(0.0, min(s_in + (seg_start - c_start), s_out))
            src_end = max(0.0, min(s_in + (seg_end - c_start), s_out))
            if src_end <= src_start:
                continue
            x0 = int(round(self._t_to_xf(seg_start)))
            x1 = int(round(self._t_to_xf(seg_end)))
            npx = max(1, x1 - x0)
            clip_rms = clip.get("rms", None)
            if clip_rms is None:
                clip_rms = _track_attr(t, "rms", None)
            env = self._env_for_rms(clip_rms, clip.get("hop_s", _track_attr(t, "hop_s", 0.03)), npx, src_start, src_end)
            if env is None:
                continue
            amp_h = hh * 0.44
            lim = min(npx, int(env.size))
            xs = np.arange(lim) + x0
            amps = np.maximum(0.6, env[:lim] * amp_h)
            inside = (xs >= 0) & (xs < removed.size)
            states = np.zeros(lim, dtype=np.int8)
            states[inside] = removed[xs[inside]]
            for state, pen in ((0, QPen(_alpha(col.wave, 215), 1)), (1, QPen(col.wave_removed, 1)),
                               (2, QPen(_alpha(col.accent, 200), 1))):
                idx = np.nonzero(states == state)[0]
                if idx.size == 0:
                    continue
                p.setPen(pen)
                p.drawLines([QLineF(float(xs[j]) + 0.5, mid - float(amps[j]), float(xs[j]) + 0.5, mid + float(amps[j]))
                             for j in idx])
        # tint removed ranges
        for a, b, kind, selected in spans:
            x1, x2 = self._t_to_xf(a), self._t_to_xf(b)
            if x2 < HEADER_W or x1 > width:
                continue
            rr = QRectF(x1, top, max(1.0, x2 - x1), hh)
            p.fillRect(rr, _alpha(col.cut(kind), 30 if not selected else 18))
            if selected:
                p.setPen(QPen(col.accent, 1))
                p.drawLine(QPointF(x1, top), QPointF(x1, top + hh))
                p.drawLine(QPointF(x2, top), QPointF(x2, top + hh))
        # centre line
        p.setPen(QPen(_alpha(col.wave, 40), 1))
        p.drawLine(QPointF(HEADER_W, mid), QPointF(width, mid))
        # threshold preview (waveform uses a sqrt curve, so the guide does too)
        if self._threshold_guide is not None:
            a = max(0.6, float(np.power(self._threshold_guide, 0.7)) * hh * 0.44)
            pen = QPen(col.pending, 1, Qt.DashLine)
            p.setPen(pen)
            p.drawLine(QPointF(HEADER_W, mid - a), QPointF(width, mid - a))
            p.drawLine(QPointF(HEADER_W, mid + a), QPointF(width, mid + a))
            f = QFont(p.font())
            f.setPixelSize(10)
            f.setBold(True)
            p.setFont(f)
            label = "Voice threshold"
            fm = QFontMetrics(f)
            chip = QRectF(HEADER_W + 6, mid - a - fm.height() - 6, fm.horizontalAdvance(label) + 12, fm.height() + 4)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, 170))
            p.drawRoundedRect(chip, 3, 3)
            p.setPen(col.pending)
            p.drawText(chip, Qt.AlignCenter, label)
            p.setFont(QFont(self.font()))

    def _paint_cuts_lane(self, p: QPainter, lane: _Lane, spans) -> None:
        col = self._colors
        top = lane.top + 3
        hh = lane.h - 6
        p.setRenderHint(QPainter.Antialiasing, True)
        # restored (suppressed) ranges first — hatched green outlines
        for clip in self._audio_clips():
            c0 = float(clip.get("start", 0.0))
            for r in clip.get("restored_cuts") or []:
                x1, x2 = self._t_to_xf(c0 + float(r.start)), self._t_to_xf(c0 + float(r.end))
                if x2 < HEADER_W or x1 > self.width():
                    continue
                rr = QRectF(x1, top + 1, max(2.0, x2 - x1), hh - 2)
                p.setPen(QPen(_alpha(col.restored, 190), 1))
                p.setBrush(_alpha(col.restored, 26))
                p.drawRoundedRect(rr, 3, 3)
                self._hatch(p, rr, _alpha(col.restored, 70), 5.0)
        audio_idx = self._audio_track_index()
        atrack = self._track(audio_idx) if audio_idx is not None else None
        selected = self._selected_by_track.get(audio_idx, set()) if audio_idx is not None else set()
        for clip in self._audio_clips():
            c0 = float(clip.get("start", 0.0))
            kinds = self._clip_cut_kinds(clip)
            active = atrack is not None and self._clip_matches_active(atrack, clip)
            islands = [(float(k.start), float(k.end)) for k in (clip.get("gameplay_keeps") or [])]
            for i, c in enumerate(clip.get("cuts") or []):
                a, b = float(c.start), float(c.end)
                if self._resize is not None and self._resize.get("clip") is clip and self._resize.get("idx") == i:
                    a, b = float(self._resize["start"]), float(self._resize["end"])
                x1, x2 = self._t_to_xf(c0 + a), self._t_to_xf(c0 + b)
                if x2 < HEADER_W or x1 > self.width():
                    continue
                kind = kinds[i] if i < len(kinds) else "auto"
                if kind == "gameplay":
                    p.setPen(Qt.NoPen)
                    p.setBrush(_alpha(col.auto, 225))
                    for ia, ib in islands:
                        sa, sb = max(a, ia), min(b, ib)
                        if sb > sa:
                            xa, xb = self._t_to_xf(c0 + sa), self._t_to_xf(c0 + sb)
                            p.drawRoundedRect(QRectF(xa, top, max(2.0, xb - xa), hh), 2, 2)
                    continue
                base = col.cut(kind)
                is_sel = active and i in selected
                ww = max(2.0, x2 - x1)
                rr = QRectF(x1, top, ww, hh)
                p.setPen(Qt.NoPen)
                p.setBrush(_alpha(base, 225))
                p.drawRoundedRect(rr, 3 if ww > 6 else 1, 3 if ww > 6 else 1)
                if is_sel:
                    p.setPen(QPen(QColor("#FFFFFF"), 1.5))
                    p.setBrush(Qt.NoBrush)
                    p.drawRoundedRect(rr.adjusted(-1, -1, 1, 1), 3, 3)
        p.setRenderHint(QPainter.Antialiasing, False)

    def _paint_hover_cut(self, p: QPainter, lanes: list[_Lane]) -> None:
        """Hover highlight + edge grips of the cut under the mouse (dynamic layer: no static rebuild)."""
        if self._hover_cut is None or self._hover_x is None or self._tool_mode != "select":
            return
        lane = next((ln for ln in lanes if ln.kind == "cuts"), None)
        hit = self._cut_hit(self._audio_track_index(), self._hover_x)
        if lane is None or hit is None:
            return
        _i, clip, c = hit
        c0 = float(clip.get("start", 0.0)) if clip else 0.0
        x1, x2 = self._t_to_xf(c0 + float(c.start)), self._t_to_xf(c0 + float(c.end))
        top, hh = lane.top + 3, lane.h - 6
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 55))
        p.drawRoundedRect(QRectF(x1, top, max(2.0, x2 - x1), hh), 3, 3)
        if x2 - x1 > 14:
            p.setPen(QPen(QColor(255, 255, 255, 210), 1.5))
            p.drawLine(QPointF(x1 + 3, top + 4), QPointF(x1 + 3, top + hh - 4))
            p.drawLine(QPointF(x2 - 3, top + 4), QPointF(x2 - 3, top + hh - 4))
        p.setRenderHint(QPainter.Antialiasing, False)

    def _island_color(self, island: dict) -> QColor:
        if island.get("meta", {}).get("included") is False or island.get("meta", {}).get("export_filtered"):
            return QColor("#81858E")
        hero_class = class_id(island.get("meta", {}).get("hero_class"))
        return QColor(CLASS_COLORS[hero_class]) if hero_class else self._colors.keep_island

    @staticmethod
    def _island_text_color(color: QColor) -> QColor:
        brightness = .299 * color.redF() + .587 * color.greenF() + .114 * color.blueF()
        return QColor("#171A20") if brightness > .55 else QColor("#FFFFFF")

    def _paint_gameplay_overlay(self, p: QPainter, lanes: list[_Lane]) -> None:
        """Kept WIN islands as large color overlays; everything outside them dimmed."""
        islands = self._gameplay_islands()
        if not islands or not lanes:
            return
        top = lanes[0].top
        bottom = lanes[-1].bottom
        video = next((ln for ln in lanes if ln.kind == "video"), lanes[0])
        # dim everything that gameplay editing removes (outside the islands)
        for clip in self._audio_clips():
            if not clip.get("gameplay_keeps") and not clip.get("gameplay_boxes"):
                continue
            c0 = float(clip.get("start", 0.0))
            for g in clip.get("gameplay_cuts") or []:
                x1, x2 = self._t_to_xf(c0 + float(g.start)), self._t_to_xf(c0 + float(g.end))
                if x2 < HEADER_W or x1 > self.width():
                    continue
                p.fillRect(QRectF(x1, top, x2 - x1, bottom - top), QColor(4, 5, 7, 165))
        f = QFont(p.font())
        f.setPixelSize(11)
        f.setBold(True)
        fm = QFontMetrics(f)
        p.setRenderHint(QPainter.Antialiasing, True)
        for n, isl in enumerate(islands, start=1):
            color = self._island_color(isl)
            x1, x2 = self._t_to_xf(isl["t0"]), self._t_to_xf(isl["t1"])
            if x2 < HEADER_W or x1 > self.width():
                continue
            selected = self._selected_island == (isl["ts"], isl["index"])
            rect = QRectF(x1, top, max(2.0, x2 - x1), bottom - top)
            # dominant color; lighter when selected so the cuts inside become readable
            p.fillRect(rect, _alpha(color, 42 if selected else 96))
            p.setPen(QPen(color.lighter(150) if selected else color.lighter(118), 2.5 if selected else 2.0))
            p.setBrush(Qt.NoBrush)
            p.drawRect(rect.adjusted(1, 1, -1, -1))
            # label tab at the top of the VIDEO lane
            game = isl["meta"].get("game_index")
            hero_class = class_id(isl["meta"].get("hero_class"))
            result = str(isl["meta"].get("result", "WIN"))
            label = ("MANUAL " if isl["meta"].get("manual") else "") + result + (" · " + class_label(hero_class) if hero_class else "") + (f" · game {game}" if game is not None else f" {n}")
            dur = isl["t1"] - isl["t0"]
            included = isl["meta"].get("included", True)
            status = "included" if included else "excluded"
            if isl["meta"].get("export_filtered"):
                status += " · " + isl["meta"]["export_filtered"]
            if not included or isl["meta"].get("export_filtered"):
                p.setPen(QPen(color, 1.5, Qt.PenStyle.DashLine))
                p.drawRect(rect.adjusted(2, 2, -2, -2))
            long_label = f"{label}   {fmt_hms(dur)} {status}"
            text = long_label if fm.horizontalAdvance(long_label) + 20 < rect.width() else label
            if fm.horizontalAdvance(text) + 16 < rect.width():
                tab_x = max(rect.left(), float(HEADER_W)) + 6
                tab = QRectF(tab_x, video.top + 4, fm.horizontalAdvance(text) + 16, fm.height() + 6)
                p.setPen(Qt.NoPen)
                p.setBrush(color.darker(115) if not selected else color)
                p.drawRoundedRect(tab, 4, 4)
                p.setFont(f)
                p.setPen(self._island_text_color(color))
                p.drawText(tab, Qt.AlignCenter, text)
                p.setFont(QFont(self.font()))
            if selected:
                # boundary handles
                p.setPen(Qt.NoPen)
                p.setBrush(QColor("#FFFFFF"))
                for xe in (x1, x2):
                    p.drawRoundedRect(QRectF(xe - 3, video.top + video.h / 2 - 12, 6, 24), 3, 3)
        p.setRenderHint(QPainter.Antialiasing, False)

    def _paint_island_ruler_marks(self, p: QPainter) -> None:
        """Class-color bars under the ruler; times at the edges of the selected island."""
        islands = self._gameplay_islands()
        if not islands:
            return
        f = QFont(p.font())
        f.setPixelSize(10)
        f.setBold(True)
        p.setFont(f)
        fm = QFontMetrics(f)
        for isl in islands:
            color = self._island_color(isl)
            x1, x2 = self._t_to_xf(isl["t0"]), self._t_to_xf(isl["t1"])
            x1c, x2c = max(x1, float(HEADER_W)), min(x2, float(self.width()))
            if x2c <= x1c:
                continue
            p.fillRect(QRectF(x1c, RULER_H - 4, x2c - x1c, 3), color)
            if self._selected_island == (isl["ts"], isl["index"]):
                for t, align_right in ((isl["t0"], False), (isl["t1"], True)):
                    label = fmt_hms(t)
                    x = self._t_to_xf(t)
                    tw = fm.horizontalAdvance(label) + 8
                    box = QRectF(x - tw if align_right else x, 2, tw, 14)
                    p.fillRect(box, color)
                    p.setPen(self._island_text_color(color))
                    p.drawText(box, Qt.AlignCenter, label)
        p.setFont(QFont(self.font()))

    def _paint_pending(self, p: QPainter, lanes: list[_Lane]) -> None:
        if not lanes:
            return
        col = self._colors
        audio_idx = self._audio_track_index()
        idx = audio_idx if audio_idx is not None else self.active_track
        ps, pe = self._pending_by_track.get(idx, (None, None))
        if ps is None and pe is None:
            return
        clip = self._active_clip(idx) if idx is not None else None
        c_start = float(clip.get("start", 0.0)) if clip else 0.0
        c_end = float(clip.get("end", c_start)) if clip else self.duration
        s_in = float(clip.get("source_in", 0.0)) if clip else 0.0
        s_out = float(clip.get("source_out", s_in + max(0.0, c_end - c_start))) if clip else 0.0

        def to_t(v: Optional[float]) -> Optional[float]:
            if v is None:
                return None
            vv = float(v)
            if clip is None:
                return vv
            clip_len = max(0.0, c_end - c_start)
            if clip_len > 0.0 and -1e-6 <= vv <= clip_len + 1e-6:
                return c_start + max(0.0, min(vv, clip_len))
            if s_out > s_in + 1e-6 and s_in - 1e-6 <= vv <= s_out + 1e-6:
                return c_start + (vv - s_in)
            if c_start - 1e-6 <= vv <= c_end + 1e-6:
                return vv
            return None

        top = lanes[0].top
        bottom = lanes[-1].bottom
        ts, te = to_t(ps), to_t(pe)
        if ts is not None and te is not None:
            a, b = sorted((ts, te))
            x1, x2 = self._t_to_xf(a), self._t_to_xf(b)
            p.fillRect(QRectF(x1, top, max(1.0, x2 - x1), bottom - top), _alpha(col.pending, 40))
        for tv, label in ((ts, "IN"), (te, "OUT")):
            if tv is None:
                continue
            x = self._t_to_xf(tv)
            p.setPen(QPen(col.pending, 2))
            p.drawLine(QPointF(x, top), QPointF(x, bottom))
            f = QFont(p.font())
            f.setPixelSize(9)
            f.setBold(True)
            p.setFont(f)
            chip = QRectF(x - 13, top, 26, 13)
            p.fillRect(chip, col.pending)
            p.setPen(QColor("#1A1300"))
            p.drawText(chip, Qt.AlignCenter, label)
            p.setFont(QFont(self.font()))

    def _paint_marquee(self, p: QPainter, lanes: list[_Lane]) -> None:
        if self._sel_start_x is None or self._sel_current_x is None or not lanes:
            return
        col = self._colors
        x1 = min(self._sel_start_x, self._sel_current_x)
        x2 = max(self._sel_start_x, self._sel_current_x)
        r = QRectF(x1, lanes[0].top, x2 - x1, lanes[-1].bottom - lanes[0].top)
        p.fillRect(r, _alpha(col.accent, 34))
        p.setPen(QPen(_alpha(col.accent, 200), 1, Qt.DashLine))
        p.drawRect(r)

    def _paint_hover_line(self, p: QPainter, lanes: list[_Lane]) -> None:
        if self._hover_x is None or self._scrubbing or not lanes or self._hover_x < HEADER_W:
            return
        col = self._colors
        x = float(self._hover_x)
        p.setPen(QPen(_alpha(col.text, 46), 1))
        p.drawLine(QPointF(x, RULER_H), QPointF(x, lanes[-1].bottom))

    def _paint_playhead(self, p: QPainter, lanes: list[_Lane], h: int) -> None:
        col = self._colors
        x = self._t_to_xf(self.playhead)
        if x < HEADER_W - 1 or x > self.width():
            return
        p.setRenderHint(QPainter.Antialiasing, True)
        bottom = lanes[-1].bottom if lanes else h
        p.setPen(QPen(col.playhead, 1.5))
        p.drawLine(QPointF(x, RULER_H - 6), QPointF(x, bottom))
        head = QPainterPath()
        head.moveTo(x - 6, 3)
        head.lineTo(x + 6, 3)
        head.lineTo(x + 6, RULER_H - 12)
        head.lineTo(x, RULER_H - 5)
        head.lineTo(x - 6, RULER_H - 12)
        head.closeSubpath()
        p.setPen(Qt.NoPen)
        p.setBrush(col.playhead)
        p.drawPath(head)
        p.setRenderHint(QPainter.Antialiasing, False)

    # -----------------------------
    # Mouse interactions
    # -----------------------------
    def _meta_for(self, track_idx: Optional[int], x: float, t: float) -> dict:
        row_kind = None
        track_state_idx = None
        clip_id = None
        source_t = None
        local_t = None
        if track_idx is not None:
            row_kind = self._track_kind(track_idx)
            clip = self._clip_at_x(track_idx, x)
            if isinstance(clip, dict):
                track_state_idx = clip.get("track_state_idx", None)
                clip_id = clip.get("clip_id", None)
                try:
                    cs = float(clip.get("start", 0.0))
                    s_in = float(clip.get("source_in", 0.0))
                    local_t = float(t) - cs
                    source_t = s_in + local_t
                except Exception:
                    local_t = None
                    source_t = None
        return {"row_kind": row_kind, "track_state_idx": track_state_idx, "clip_id": clip_id,
                "source_t": source_t, "local_t": local_t}

    def _edge_hit(self, x: float) -> Optional[tuple[int, dict, str]]:
        """(cut index, clip, 'start'|'end') when x is on a cut edge of the active clip."""
        audio_idx = self._audio_track_index()
        if audio_idx is None:
            return None
        atrack = self._track(audio_idx)
        best = None
        best_d = EDGE_GRAB_PX + 0.5
        for clip in self._audio_clips():
            if atrack is not None and not self._clip_matches_active(atrack, clip):
                continue
            c0 = float(clip.get("start", 0.0))
            kinds = self._clip_cut_kinds(clip)
            for i, c in enumerate(clip.get("cuts") or []):
                if i < len(kinds) and kinds[i] == "gameplay":
                    continue
                x1, x2 = self._t_to_xf(c0 + float(c.start)), self._t_to_xf(c0 + float(c.end))
                if x2 - x1 < 10:
                    continue
                for edge, ex in (("start", x1), ("end", x2)):
                    d = abs(ex - x)
                    if d < best_d:
                        best, best_d = (i, clip, edge), d
        return best

    def mousePressEvent(self, e):
        if self.duration <= 0:
            return
        x = float(e.position().x())
        y = float(e.position().y())
        if x < HEADER_W:
            lane = self._lane_at_y(y)
            if lane is not None and lane.track_idx != self.active_track:
                self.setActiveTrack(lane.track_idx, emit=True)
            return
        lane = self._lane_at_y(y)
        track_idx = lane.track_idx if lane is not None else None
        t = self._x_to_t(x)

        if e.button() == Qt.RightButton:
            if track_idx is not None and track_idx != self.active_track:
                self.setActiveTrack(track_idx, emit=True)
            meta = self._meta_for(track_idx, x, t)
            hit = self._cut_hit(self._audio_track_index(), x)
            meta["cut_idx"] = hit[0] if hit else None
            if hit is not None:
                cur = set(self.selectedCutIndices())
                if hit[0] not in cur:
                    self.setCutSelection([hit[0]])
            restored = self._restored_hit(self._audio_track_index() or 0, x) if lane is not None else None
            meta["restored_range"] = (float(restored[1].start), float(restored[1].end)) if restored else None
            meta["selected"] = self.selectedCutIndices()
            meta["lane"] = lane.kind if lane is not None else None
            island = self._island_at_x(x) if lane is not None and lane.kind in ("video", "audio", "cuts") else None
            meta["island"] = {"track_state_idx": island["ts"], "index": island["index"]} if island else None
            if island and island["clip"].get("gameplay_boxes") is not None:
                meta["island"]["game_key"] = island["meta"].get("game_id")
            self.contextMenuRequested.emit(float(t), e.globalPosition(), meta)
            return

        if e.button() != Qt.LeftButton:
            return
        if track_idx is not None and track_idx != self.active_track and lane is not None \
                and lane.kind in ("video", "audio"):
            self.setActiveTrack(track_idx, emit=True)
        active_idx = self.active_track

        if self._tool_mode == "split":
            if track_idx is None:
                return
            clip = self._clip_at_x(track_idx, x)
            track = self._track(track_idx)
            if clip is not None and (track is None or self._clip_matches_active(track, clip)):
                clip_id = clip.get("clip_id", None)
                if clip_id:
                    self.clipSplitRequested.emit(str(clip_id), float(t))
            return

        if self._tool_mode == "cut":
            meta = self._meta_for(self._audio_track_index() if lane is not None and lane.kind != "video"
                                  else track_idx, x, t)
            meta["cut_tool_click"] = True
            self.contextMenuRequested.emit(float(t), e.globalPosition(), meta)
            return

        # ruler or playhead grab → scrub
        playhead_x = self._t_to_xf(self.playhead)
        if y <= RULER_H or (abs(x - playhead_x) <= 5.0 and y <= RULER_H + 10):
            self._start_scrub(x, playhead_x)
            return

        # cut edge → resize
        if lane is not None and lane.kind in ("cuts", "audio"):
            edge = self._edge_hit(x)
            if edge is not None:
                i, clip, which = edge
                c = (clip.get("cuts") or [])[i]
                self._resize = {"idx": i, "clip": clip, "edge": which, "start": float(c.start),
                                "end": float(c.end), "orig": (float(c.start), float(c.end))}
                self.setCutSelection([i])
                self._changed()
                return

        # gameplay islands on the VIDEO lane: drag an edge to move a boundary, click to select
        if lane is not None and lane.kind == "video" and self._has_gameplay():
            edge = self._island_edge_at_x(x)
            if edge is not None:
                isl, which = edge
                keeps = sorted(isl["clip"].get("gameplay_keeps") or [], key=lambda k: float(k.start))
                i = isl["index"]
                boxes = isl["clip"].get("gameplay_boxes")
                if boxes is not None:
                    keeps = [Segment(float(b["start"]), float(b["end"])) for b in boxes]
                c0 = float(isl["clip"].get("start", 0.0))
                clip_len = float(isl["clip"].get("end", c0)) - c0
                self._island_drag = {
                    "clip": isl["clip"], "index": i, "ts": isl["ts"], "edge": which, "c0": c0,
                    "game_key": isl["meta"].get("game_id") if boxes is not None else None,
                    "start": isl["start"], "end": isl["end"], "orig": (isl["start"], isl["end"]),
                    "lo": float(keeps[i - 1].end) if i > 0 else 0.0,
                    "hi": float(keeps[i + 1].start) if i + 1 < len(keeps) else clip_len,
                }
                if isl["ts"] is not None:
                    self.setSelectedIsland((int(isl["ts"]), i), emit=True)
                return
            isl = self._island_at_x(x)
            if isl is not None and isl["ts"] is not None:
                self.setSelectedIsland((int(isl["ts"]), isl["index"]), emit=True)
                if isl["clip"].get("gameplay_boxes") is not None and isl["meta"].get("game_id"):
                    self.gameToggleRequested.emit(int(isl["ts"]), str(isl["meta"]["game_id"]))
                return

        # clip drag on the video lane (sequential mode: swap)
        if lane is not None and lane.kind == "video":
            clip = self._clip_at_x(track_idx, x) if track_idx is not None else None
            if clip is not None:
                ts = clip.get("track_state_idx", None)
                if ts is not None:
                    try:
                        self.clipActivated.emit(int(ts))
                    except Exception:
                        pass
                self._drag_clip = clip
                self._drag_track_idx = track_idx
                self._drag_start_t = t
                self._drag_clip_start = float(clip.get("start", 0.0))
                self._drag_clip_end = float(clip.get("end", 0.0))
                self._drag_offset_t = t - self._drag_clip_start
                self._drag_preview_start = None
                self._drag_started = False
                self._drag_press_x = x
                return

        # click on a cut → select
        if lane is not None and lane.kind in ("cuts", "audio", "gameplay"):
            sel_track = self._audio_track_index()
            hit = self._cut_hit(sel_track, x)
            if hit is None:
                other = self._cut_hit(sel_track, x, require_active=False)
                if sel_track is not None and other is not None and other[1].get("track_state_idx") is not None:
                    # cut of another video in the sequence: activate that video first
                    self._selected_by_track[sel_track] = set()
                    self.clipActivated.emit(int(other[1]["track_state_idx"]))
                    hit = self._cut_hit(sel_track, x) or other
            if hit is not None:
                i = hit[0]
                cur = set(self.selectedCutIndices())
                if e.modifiers() & (Qt.ShiftModifier | Qt.ControlModifier):
                    cur ^= {i}
                else:
                    cur = {i}
                self.setCutSelection(sorted(cur))
                self.cutClicked.emit(i, e.globalPosition())
                return

        # empty space: click = seek (on release), drag = marquee
        self._press_pos = QPointF(x, y)
        self._press_t = t
        self._press_shift = bool(e.modifiers() & (Qt.ShiftModifier | Qt.ControlModifier))
        _ = active_idx

    def _start_scrub(self, x: float, playhead_x: float) -> None:
        self._scrubbing = True
        self.follow_playhead = True
        self._scrub_offset_px = (x - playhead_x) if abs(x - playhead_x) <= 5.0 else 0.0
        self.scrubStarted.emit()
        self.seekRequested.emit(float(self._x_to_t(x - self._scrub_offset_px)))

    def mouseDoubleClickEvent(self, e):
        if self.duration <= 0 or e.button() != Qt.LeftButton:
            return
        x = float(e.position().x())
        sel_track = self._audio_track_index()
        hit = self._cut_hit(sel_track, x) if sel_track is not None else None
        if hit is not None:
            _i, clip, c = hit
            self.follow_playhead = True
            self.seekRequested.emit(float(clip.get("start", 0.0)) + float(c.start) if clip else float(c.start))

    def _update_hover(self, x: float, y: float, global_pos) -> None:
        prev = (self._hover_cut, self._hover_edge)
        self._hover_x = x if x >= HEADER_W else None
        lane = self._lane_at_y(y)
        self._hover_cut = None
        self._hover_edge = None
        tip = ""
        if lane is not None and x >= HEADER_W and lane.kind in ("cuts", "audio", "gameplay"):
            sel_track = self._audio_track_index()
            if self._tool_mode == "select" and lane.kind in ("cuts", "audio"):
                edge = self._edge_hit(x)
                if edge is not None:
                    self._hover_cut = edge[0]
                    self._hover_edge = edge[2]
            hit = self._cut_hit(sel_track, x) if sel_track is not None else None
            if hit is not None:
                i, clip, c = hit
                self._hover_cut = i
                kinds = self._clip_cut_kinds(clip) if clip else []
                kind = kinds[i] if i < len(kinds) else "auto"
                label = {"auto": "Automatic cut", "manual": "Manual cut", "gameplay": "Gameplay cut"}[kind]
                c0 = float(clip.get("start", 0.0)) if clip else 0.0
                a, b = c0 + float(c.start), c0 + float(c.end)
                tip = (f"{label}\n{fmt_hms(a)} – {fmt_hms(b)}  ({b - a:.2f} s)\n"
                       f"Click to select · Del restores · drag edges to adjust")
            elif lane.kind == "cuts":
                r = self._restored_hit(sel_track or 0, x)
                if r is not None:
                    clip, seg = r
                    c0 = float(clip.get("start", 0.0))
                    tip = (f"Restored (kept)\n{fmt_hms(c0 + float(seg.start))} – {fmt_hms(c0 + float(seg.end))}\n"
                           "Right-click to cut it again")
        island_edge = None
        if lane is not None and lane.kind == "video" and x >= HEADER_W and self._has_gameplay():
            island_edge = self._island_edge_at_x(x)
            if island_edge is not None:
                isl, which = island_edge
                tip = (f"Game interval {which}: {fmt_hms(isl['t0'] if which == 'start' else isl['t1'])}\n"
                       "Drag to adjust this boundary")
            else:
                isl = self._island_at_x(x)
                if isl is not None:
                    tip = (f"Game interval  {fmt_hms(isl['t0'])} – {fmt_hms(isl['t1'])}  "
                           f"({fmt_hms(isl['t1'] - isl['t0'])})\nClick to include/exclude · right-click for game actions · drag edges to adjust")
        if self._tool_mode == "select":
            if self._hover_edge is not None or island_edge is not None:
                self.setCursor(Qt.SizeHorCursor)
            else:
                self.unsetCursor()
        if tip:
            QToolTip.showText(global_pos.toPoint(), tip, self)
        elif prev[0] is not None:
            QToolTip.hideText()
        self.update()  # hover line/highlight are drawn on the dynamic layer: cheap blit, no rebuild

    def mouseMoveEvent(self, e):
        if self.duration <= 0:
            return
        x = float(e.position().x())
        y = float(e.position().y())

        if not (e.buttons() & Qt.LeftButton):
            self._update_hover(x, y, e.globalPosition())
            return

        if self._island_drag is not None:
            d = self._island_drag
            local = self._x_to_t(x) - d["c0"]
            if self._snap_enabled and abs(self._t_to_xf(self.playhead) - x) <= 8:
                local = float(self.playhead) - d["c0"]
            if d["edge"] == "start":
                d["start"] = max(d["lo"], min(local, d["end"] - 1.0))
            else:
                d["end"] = min(d["hi"], max(local, d["start"] + 1.0))
            QToolTip.showText(e.globalPosition().toPoint(),
                              f"WIN island  {fmt_hms(d['c0'] + d['start'])} – {fmt_hms(d['c0'] + d['end'])}"
                              f"  ({fmt_hms(d['end'] - d['start'])} kept)", self)
            self._changed()
            return

        if self._resize is not None:
            clip = self._resize["clip"]
            c0 = float(clip.get("start", 0.0)) if clip else 0.0
            clip_len = float(clip.get("end", c0)) - c0 if clip else self.duration
            local = self._x_to_t(x) - c0
            if self._snap_enabled:
                ph_local = float(self.playhead) - c0
                if abs(self._t_to_xf(c0 + ph_local) - x) <= 8:
                    local = ph_local
            if self._resize["edge"] == "start":
                self._resize["start"] = max(0.0, min(local, self._resize["end"] - MIN_CUT_S))
            else:
                self._resize["end"] = min(clip_len, max(local, self._resize["start"] + MIN_CUT_S))
            QToolTip.showText(e.globalPosition().toPoint(),
                              f"{fmt_hms(c0 + self._resize['start'])} – {fmt_hms(c0 + self._resize['end'])}"
                              f"  ({self._resize['end'] - self._resize['start']:.2f} s)", self)
            self._changed()
            return

        if self._drag_clip is not None:
            if not self._drag_started and abs(x - self._drag_press_x) < DRAG_THRESHOLD_PX:
                return
            self._drag_started = True
            t = self._x_to_t(x)
            dur = max(0.0, self._drag_clip_end - self._drag_clip_start)
            new_start = max(0.0, t - self._drag_offset_t)
            new_start = self._snap_time(new_start, dur, self._drag_clip.get("clip_id"))
            self._drag_preview_start = new_start
            self._changed()
            return

        if self._scrubbing:
            self.seekRequested.emit(float(self._x_to_t(x - self._scrub_offset_px)))
            return

        if self._press_pos is not None and self._sel_start_x is None:
            if abs(x - self._press_pos.x()) >= DRAG_THRESHOLD_PX:
                self._sel_start_x = float(self._press_pos.x())
                self._sel_track_idx = self._audio_track_index()
                self._sel_base = set(self.selectedCutIndices()) if self._press_shift else set()
        if self._sel_start_x is not None and self._sel_track_idx is not None:
            self._sel_current_x = x
            t1 = self._x_to_t(min(self._sel_start_x, x))
            t2 = self._x_to_t(max(self._sel_start_x, x))
            sel = set(self._sel_base)
            atrack = self._track(self._sel_track_idx)
            for clip in self._audio_clips():
                if atrack is not None and not self._clip_matches_active(atrack, clip):
                    continue
                c0 = float(clip.get("start", 0.0))
                kinds = self._clip_cut_kinds(clip)
                for i, c in enumerate(clip.get("cuts") or []):
                    if i < len(kinds) and kinds[i] == "gameplay":
                        continue
                    if c0 + float(c.end) >= t1 and c0 + float(c.start) <= t2:
                        sel.add(i)
            self._selected_by_track[self._sel_track_idx] = sel
            self._changed()

    def mouseReleaseEvent(self, e):
        if self._scrubbing:
            self._scrubbing = False
            self.scrubEnded.emit()
            return

        if self._island_drag is not None:
            d = self._island_drag
            self._island_drag = None
            if d["ts"] is not None and (abs(d["start"] - d["orig"][0]) > 1e-3 or abs(d["end"] - d["orig"][1]) > 1e-3):
                if d.get("game_key"):
                    self.gameResizeRequested.emit(int(d["ts"]), str(d["game_key"]), float(d["start"]), float(d["end"]))
                else:
                    self.islandResizeRequested.emit(int(d["ts"]), int(d["index"]), float(d["start"]), float(d["end"]))
            self._changed()
            return

        if self._resize is not None:
            r = self._resize
            self._resize = None
            if (abs(r["start"] - r["orig"][0]) > 1e-3) or (abs(r["end"] - r["orig"][1]) > 1e-3):
                self.cutResizeRequested.emit(int(r["idx"]), float(r["start"]), float(r["end"]))
            self._changed()
            return

        if self._drag_clip is not None:
            clip_id = self._drag_clip.get("clip_id") if isinstance(self._drag_clip, dict) else None
            target_id = None
            started = self._drag_started
            if started and self._drag_track_idx is not None:
                target = self._clip_at_x(self._drag_track_idx, float(e.position().x()))
                if target is not None:
                    target_id = target.get("clip_id")
            new_start = self._drag_preview_start
            self._drag_clip = None
            self._drag_track_idx = None
            self._drag_preview_start = None
            self._drag_started = False
            if not started:
                self.follow_playhead = True
                self.seekRequested.emit(float(self._x_to_t(float(e.position().x()))))
            elif clip_id is not None and target_id is not None and str(target_id) != str(clip_id):
                self.clipSwapRequested.emit(str(clip_id), str(target_id))
            elif clip_id is not None and new_start is not None:
                self.clipMoveRequested.emit(str(clip_id), float(new_start))
            self._changed()
            return

        if self._sel_start_x is not None and self._sel_track_idx is not None:
            selected = sorted(self._selected_by_track.get(self._sel_track_idx, set()))
            self._sel_start_x = None
            self._sel_current_x = None
            self._sel_track_idx = None
            self._sel_clip = None
            self._press_pos = None
            self.selectionChanged.emit(selected)
            if selected:
                self.cutsSelected.emit(selected, e.globalPosition())
            self._changed()
            return

        if self._press_pos is not None:
            self._press_pos = None
            if not self._press_shift:
                self.clearCutSelection()
                if self._selected_island is not None:
                    self.setSelectedIsland(None, emit=True)
            self.follow_playhead = True
            self.seekRequested.emit(float(self._press_t))
            return

    def leaveEvent(self, e):
        self._hover_x = None
        self._hover_cut = None
        self._hover_edge = None
        self.update()
        return super().leaveEvent(e)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape and (self._resize is not None or self._sel_start_x is not None):
            self._resize = None
            self._sel_start_x = None
            self._sel_current_x = None
            self._changed()
            return
        return super().keyPressEvent(e)
