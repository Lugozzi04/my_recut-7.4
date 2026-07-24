from dataclasses import dataclass, field
from typing import List, Optional, Any
import traceback
import numpy as np

from PySide6.QtCore import Qt, Signal, QPointF
from PySide6.QtGui import QPainter, QColor, QPen, QBrush, QFontMetrics, QPolygonF
from PySide6.QtWidgets import QWidget

from analysis.cut_engine import Segment
from utils.timefmt import fmt_hms


def _choose_major_step(duration_s: float) -> int:
    # step (sec) per avere ~8-12 ticks maggiori
    candidates = [5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600]
    target = max(1.0, duration_s / 10.0)
    return min(candidates, key=lambda c: abs(c - target))


def _track_attr(track: Any, name: str, default=None):
    if isinstance(track, dict):
        return track.get(name, default)
    return getattr(track, name, default)


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


class TimelineWidget(QWidget):
    seekRequested = Signal(float)
    trackSelected = Signal(int)
    clipMoveRequested = Signal(str, float)  # clip_id, new_start
    clipSwapRequested = Signal(str, str)  # dragged_clip_id, target_clip_id
    clipActivated = Signal(int)  # track_state_idx (for single audio row)
    clipSplitRequested = Signal(str, float)  # clip_id, timeline_time

    # emitted when user clicks a single cut
    cutClicked = Signal(int, object)   # index, globalPos

    # emitted after rectangle selection
    cutsSelected = Signal(list, object)  # indices, globalPos
    contextMenuRequested = Signal(float, object, object)  # timeSec, globalPos, cutIndexOrNone

    # scrubbing lifecycle (for MainWindow sync)
    scrubStarted = Signal()
    scrubEnded = Signal()

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(160)
        self.duration: float = 0.0  # global max duration across tracks
        self.playhead: float = 0.0

        self.tracks: List[TimelineTrack] = []
        self.active_track: int = 0
        self._track_cache: List[_TrackCache] = []

        # viewport / zoom
        self.view_start: float = 0.0
        self.view_span: float = 0.0  # 0 = auto (usa durata)
        self.follow_playhead: bool = True

        # zoom levels (1x = full duration)
        self._zoom_levels = [1, 2, 3, 4, 6, 8, 12, 16, 24, 32, 48, 64]
        self._zoom_index = 0

        self._cached_w = -1
        self._cached_env: Optional[np.ndarray] = None
        self._cached_view: tuple[float, float] = (-1.0, -1.0)

        # selection state for cuts (per-track)
        self._selected_by_track: dict[int, set[int]] = {}
        self._sel_start_x: Optional[float] = None
        self._sel_current_x: Optional[float] = None
        self._sel_track_idx: Optional[int] = None
        self._sel_clip: Optional[dict] = None

        # pending cut markers (per-track, from MainWindow)
        self._pending_by_track: dict[int, tuple[Optional[float], Optional[float]]] = {}

        # scrubbing state (drag playhead / ruler)
        self._scrubbing: bool = False
        self._scrub_offset_px: float = 0.0

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

        # snapping
        self._snap_enabled: bool = False
        self._tool_mode: str = "select"

    def setToolMode(self, mode: str) -> None:
        m = str(mode).lower().strip()
        if m not in {"select", "split", "cut"}:
            m = "select"
        mode = m
        self._tool_mode = mode
        if mode in {"split", "cut"}:
            self.setCursor(Qt.CrossCursor)
        else:
            self.unsetCursor()

    def setSnapEnabled(self, enabled: bool) -> None:
        self._snap_enabled = bool(enabled)
        self.update()

    # -----------------------------
    # Data / view
    # -----------------------------
    def _track_count(self) -> int:
        return int(len(self.tracks))

    def _track(self, idx: int) -> Optional[Any]:
        if 0 <= idx < len(self.tracks):
            return self.tracks[idx]
        return None

    def _track_cuts(self, idx: int) -> List[Segment]:
        t = self._track(idx)
        return _track_attr(t, "cuts", []) if t is not None else []

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

    def _cut_index_at_x(self, track_idx: int, x: float) -> Optional[int]:
        """Return index of cut under x (in widget coords), if any."""
        if self.duration <= 0:
            return None
        t = self._x_to_t(x)
        cuts = self._track_cuts(track_idx)
        if not cuts:
            return None

        # If clips exist, map timeline time to source time
        track = self._track(track_idx)
        clips = _track_attr(track, "clips", []) if track is not None else []
        clip_cuts = cuts
        if clips:
            hit = None
            for c in clips:
                try:
                    cs = float(c.get("start", 0.0))
                    ce = float(c.get("end", 0.0))
                except Exception:
                    continue
                if cs <= t <= ce:
                    if not self._clip_matches_active(track, c):
                        return None
                    hit = c
                    break
            if hit is None:
                return None
            clip_start = float(hit.get("start", 0.0) or 0.0)
            s_in = float(hit.get("source_in", 0.0) or 0.0)
            local_t = float(t) - clip_start
            source_t = s_in + local_t
            try:
                clip_cuts = hit.get("cuts") or cuts
            except Exception:
                clip_cuts = cuts
            # clip cuts are normally segment-local; keep a source-time fallback
            for i, c in enumerate(clip_cuts):
                try:
                    if float(c.start) <= float(local_t) <= float(c.end):
                        return i
                except Exception:
                    continue
            for i, c in enumerate(clip_cuts):
                try:
                    if float(c.start) <= float(source_t) <= float(c.end):
                        return i
                except Exception:
                    continue
            return None

        for i, c in enumerate(clip_cuts):
            if c.start <= t <= c.end:
                return i
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

        # refresh caches
        self._track_cache = [_TrackCache() for _ in self.tracks]

        # prune selections/pending beyond range
        max_idx = len(self.tracks) - 1
        self._selected_by_track = {
            i: s for i, s in self._selected_by_track.items() if i <= max_idx
        }
        self._pending_by_track = {
            i: v for i, v in self._pending_by_track.items() if i <= max_idx
        }

        # recompute global duration
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

        # Keep rows readable without forcing an oversized timeline area.
        base_row_h = 48
        min_h = 22 + 18 + base_row_h * max(1, len(self.tracks))
        self.setMinimumHeight(max(150, min_h))

        self._invalidate_all_caches()
        self.update()

    def setData(self, duration: float, rms: Optional[np.ndarray], hop_s: float, cuts: List[Segment]):
        """
        Backward-compatible single-track update (updates active track).
        """
        if not self.tracks:
            name = "Track 1"
            self.tracks = [TimelineTrack(name=name, duration=float(duration), rms=rms, hop_s=float(hop_s), cuts=cuts[:])]
            self.active_track = 0
            self._track_cache = [_TrackCache()]
            self.setTracks(self.tracks, reset_view=True)
            return

        idx = max(0, min(self.active_track, len(self.tracks) - 1))
        t = self.tracks[idx]
        if isinstance(t, dict):
            t["duration"] = float(duration)
            t["rms"] = rms
            t["hop_s"] = float(hop_s)
            t["cuts"] = cuts[:]
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
        self.update()

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
        self.update()

    def set_pending_cut(self, start: Optional[float], end: Optional[float], track_index: Optional[int] = None):
        idx = self.active_track if track_index is None else int(track_index)
        if idx < 0 or (self.tracks and idx >= len(self.tracks)):
            return
        self._pending_by_track[idx] = (start, end)
        self.update()

    def setPlayhead(self, t: float):
        self.playhead = float(t)

        if self.duration > 0 and self.follow_playhead and not self._scrubbing:
            span = self._span()
            start = float(self.view_start)
            end = start + span

            # keep playhead inside viewport with margin
            margin = 0.15 * span
            if self.playhead < start + margin:
                new_start = max(0.0, self.playhead - margin)
                self.view_start = min(new_start, max(0.0, self.duration - span))
                self._invalidate_all_caches()
            elif self.playhead > end - margin:
                new_start = max(0.0, self.playhead - (span - margin))
                self.view_start = min(new_start, max(0.0, self.duration - span))
                self._invalidate_all_caches()

        self.update()

    # -----------------------------
    # Mapping & env
    # -----------------------------
    def _span(self) -> float:
        if self.duration <= 0:
            return 0.0
        s = float(self.view_span) if self.view_span > 0 else float(self.duration)
        return max(1e-9, min(s, float(self.duration)))

    def _t_to_x(self, t: float) -> int:
        if self.duration <= 0:
            return 0
        span = self._span()
        start = float(self.view_start)
        rel = (float(t) - start) / span

        w = max(1, self.width() - 1)
        x = rel * w
        if x < 0:
            return 0
        if x > w:
            return int(w)
        return int(x)

    def _x_to_t(self, x: float) -> float:
        if self.duration <= 0:
            return 0.0

        w = max(1, self.width() - 1)
        x = max(0.0, min(float(w), float(x)))

        span = self._span()
        start = float(self.view_start)
        return start + (x / float(w)) * span

    def _env_for_width(self, track_idx: int, w: int, start_s: float, end_s: float) -> Optional[np.ndarray]:
        t = self._track(track_idx)
        if t is None:
            return None
        rms = _track_attr(t, "rms", None)
        if rms is not None and not isinstance(rms, np.ndarray):
            try:
                rms = np.asarray(rms, dtype=np.float32)
            except Exception:
                rms = None
        hop_s = float(_track_attr(t, "hop_s", 0.03) or 0.03)
        if rms is None or getattr(rms, "size", 0) == 0 or w <= 10 or self.duration <= 0:
            return None

        cache = self._track_cache[track_idx] if track_idx < len(self._track_cache) else _TrackCache()
        view_key = (round(start_s, 6), round(end_s, 6), int(w))
        if cache.cached_w == w and cache.cached_env is not None and cache.cached_view == view_key:
            return cache.cached_env

        n = int(rms.size)
        i0 = int(max(0, min(n, start_s / hop_s)))
        i1 = int(max(0, min(n, end_s / hop_s)))
        if i1 <= i0:
            return None

        bins = max(10, int(w))
        idx = np.linspace(i0, i1, bins + 1).astype(int)
        env = np.zeros(bins, dtype=np.float32)

        for i in range(bins):
            a, b = int(idx[i]), int(idx[i + 1])
            if b > a:
                env[i] = float(np.max(rms[a:b]))

        m = float(np.max(env)) if env.size else 1.0
        if m > 1e-9:
            env = env / m

        if track_idx < len(self._track_cache):
            cache.cached_w = w
            cache.cached_view = view_key
            cache.cached_env = env
        return env

    def _env_for_rms(self, rms: Any, hop_s: float, w: int, start_s: float, end_s: float) -> Optional[np.ndarray]:
        if rms is not None and not isinstance(rms, np.ndarray):
            try:
                rms = np.asarray(rms, dtype=np.float32)
            except Exception:
                rms = None
        if rms is None or getattr(rms, "size", 0) == 0 or w <= 10 or self.duration <= 0:
            return None
        hop_s = float(hop_s or 0.03)

        n = int(rms.size)
        i0 = int(max(0, min(n, start_s / hop_s)))
        i1 = int(max(0, min(n, end_s / hop_s)))
        if i1 <= i0:
            return None

        bins = max(10, int(w))
        idx = np.linspace(i0, i1, bins + 1).astype(int)
        env = np.zeros(bins, dtype=np.float32)

        for i in range(bins):
            a, b = int(idx[i]), int(idx[i + 1])
            if b > a:
                env[i] = float(np.max(rms[a:b]))

        m = float(np.max(env)) if env.size else 1.0
        if m > 1e-9:
            env = env / m
        return env

    def _track_at_y(self, y: float) -> Optional[int]:
        if not self.tracks:
            return None
        ruler_h = 26
        bottom_pad = 24
        track_top = ruler_h + 6
        track_bot = self.height() - bottom_pad
        if y < track_top or y > track_bot:
            return None
        track_area_h = max(1, track_bot - track_top)
        row_h = track_area_h / max(1, len(self.tracks))
        idx = int((y - track_top) / row_h)
        return max(0, min(idx, len(self.tracks) - 1))

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
        w = max(1, self.width())
        span = self._span()
        # snap threshold ~10px
        thr = (span / float(w)) * 10.0

        best = proposed_start
        best_diff = thr + 1.0

        def consider(edge: float, mode: str):
            nonlocal best, best_diff
            if mode == "start":
                cand = edge
            else:
                cand = edge - dur
            diff = abs(cand - proposed_start)
            if diff < best_diff:
                best_diff = diff
                best = cand

        consider(0.0, "start")
        # snap to playhead as well
        try:
            consider(float(self.playhead), "start")
            consider(float(self.playhead), "end")
        except Exception:
            pass

        for t in self.tracks:
            clips = _track_attr(t, "clips", []) or []
            for c in clips:
                if ignore_clip_id and c.get("clip_id") == ignore_clip_id:
                    continue
                try:
                    cs = float(c.get("start", 0.0))
                    ce = float(c.get("end", 0.0))
                except Exception:
                    continue
                consider(cs, "start")
                consider(ce, "start")
                consider(cs, "end")
                consider(ce, "end")

        if best_diff <= thr:
            return best
        return proposed_start

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

    def _apply_zoom_level(self, reset: bool = False):
        z = self._zoom_levels[self._zoom_index]
        span = max(0.01, float(self.duration) / float(z))

        if reset:
            self.view_start = 0.0
        else:
            center = float(self.playhead)
            new_start = center - span * 0.5
            new_start = max(0.0, min(new_start, max(0.0, self.duration - span)))
            self.view_start = new_start

        self.view_span = span
        self._invalidate_all_caches()
        self.update()

    # -----------------------------
    # Paint
    # -----------------------------
    def paintEvent(self, _):
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.Antialiasing, False)
            f = p.font()
            if f.pointSize() <= 0:
                f.setPointSize(10)
                p.setFont(f)

            rect = self.rect()
            w, h = rect.width(), rect.height()

            # palette
            bg_color = QColor(18, 18, 20)
            row_active = QColor(26, 30, 36)
            row_even = QColor(22, 22, 24)
            row_odd = QColor(20, 20, 22)
            row_top_line = QColor(34, 36, 40)
            row_bot_line = QColor(44, 46, 52)
            label_bg = QColor(14, 14, 16, 200)
            label_border = QColor(90, 90, 105)
            label_text_active = QColor(245, 245, 245)
            label_text = QColor(195, 200, 210)

            # background
            p.fillRect(rect, bg_color)

            # zones
            ruler_h = 26
            bottom_pad = 24
            track_top = ruler_h + 6
            track_bot = h - bottom_pad
            track_area_h = max(1, track_bot - track_top)
            track_count = max(0, len(self.tracks))
            row_h = track_area_h / max(1, track_count) if track_count > 0 else track_area_h

            # ruler ticks (viewport-aware)
            if self.duration > 0:
                span = self._span()
                vs = max(0.0, min(self.view_start, self.duration))
                ve = max(0.0, min(vs + span, self.duration))

                major = _choose_major_step(span)
                minor = max(1, major // 5)

                t0 = int(vs // minor) * minor
                if t0 < 0:
                    t0 = 0

                p.setPen(QPen(QColor(120, 120, 120), 1))
                t = t0
                while t <= int(ve) + minor:
                    x = self._t_to_x(t)
                    if 0 <= x <= w:
                        tick_h = 6 if (t % major) else 12
                        p.drawLine(x, ruler_h - tick_h, x, ruler_h)
                    t += minor

                p.setPen(QColor(200, 200, 200))
                fm = QFontMetrics(p.font())
                t = int(vs // major) * major
                if t < 0:
                    t = 0
                while t <= int(ve) + major:
                    if t % major == 0:
                        label = fmt_hms(t)
                        x = self._t_to_x(t)
                        if -50 <= x <= w + 50:
                            p.drawText(max(0, x - fm.horizontalAdvance(label) // 2), 16, label)
                    t += major

            # tracks
            if track_count > 0:
                span = self._span()
                vs = max(0.0, min(self.view_start, self.duration))
                ve = max(0.0, min(vs + span, self.duration))

                for i, t in enumerate(self.tracks):
                    row_top = track_top + i * row_h
                    row_bot = row_top + row_h
                    wave_top = int(row_top) + 4
                    wave_bot = int(row_bot) - 4
                    wave_h = max(1, wave_bot - wave_top)

                    # row background
                    if i == self.active_track:
                        bg = row_active
                    else:
                        bg = row_even if (i % 2 == 0) else row_odd
                    p.fillRect(0, int(row_top), w, int(row_bot - row_top), bg)

                    # separators (top + bottom for better contour)
                    p.setPen(QPen(row_top_line, 1))
                    p.drawLine(0, int(row_top), w, int(row_top))
                    p.setPen(QPen(row_bot_line, 1))
                    p.drawLine(0, int(row_bot), w, int(row_bot))

                    # clip blocks (NLE-style)
                    clips = _track_attr(t, "clips", []) or []
                    clip_labels = []
                    if self.duration > 0 and clips:
                        track_kind = str(_track_attr(t, "kind", "audio") or "audio")
                        for clip in clips:
                            try:
                                c_start = float(clip.get("start", 0.0))
                                c_end = float(clip.get("end", 0.0))
                            except Exception:
                                continue
                            if c_end <= c_start:
                                continue
                            # preview drag (ghost)
                            if self._drag_clip is not None and clip.get("clip_id") == self._drag_clip.get("clip_id"):
                                if self._drag_preview_start is not None:
                                    dur = max(0.0, c_end - c_start)
                                    c_start = float(self._drag_preview_start)
                                    c_end = float(c_start + dur)
                            x1 = self._t_to_x(c_start)
                            x2 = self._t_to_x(c_end)
                            if x2 <= x1:
                                continue
                            if track_kind == "video" or clip.get("kind") == "video":
                                c_val = clip.get("color")
                                e_val = clip.get("edge")
                                if isinstance(c_val, (list, tuple)) and len(c_val) >= 4:
                                    color = QColor(int(c_val[0]), int(c_val[1]), int(c_val[2]), int(c_val[3]))
                                else:
                                    color = QColor(70, 140, 90, 130)
                                if isinstance(e_val, (list, tuple)) and len(e_val) >= 3:
                                    edge = QColor(int(e_val[0]), int(e_val[1]), int(e_val[2]))
                                else:
                                    edge = QColor(90, 170, 110)
                            else:
                                color = QColor(70, 120, 170, 120)
                                edge = QColor(90, 150, 210)
                            p.setPen(QPen(edge, 2))
                            p.setBrush(QBrush(color))
                            p.drawRoundedRect(x1, wave_top, x2 - x1, wave_h, 4, 4)
                            name = str(clip.get("name") or "")
                            if name and track_kind == "video":
                                clip_labels.append((x1, x2, name, row_top, wave_top))

                    # waveform (align to clips if present)
                    track_kind = str(_track_attr(t, "kind", "audio") or "audio")
                    track_dur = float(_track_attr(t, "duration", 0.0) or 0.0)
                    if track_kind == "audio" and self.duration > 0 and ve > vs:
                        if clips:
                            for clip in clips:
                                try:
                                    c_start = float(clip.get("start", 0.0))
                                    c_end = float(clip.get("end", 0.0))
                                    s_in = float(clip.get("source_in", 0.0))
                                    s_out = float(clip.get("source_out", s_in + (c_end - c_start)))
                                except Exception:
                                    continue
                                if c_end <= c_start or s_out <= 0.0:
                                    continue
                                seg_start = max(vs, c_start)
                                seg_end = min(ve, c_end)
                                if seg_end <= seg_start:
                                    continue
                                # map timeline -> source
                                src_start = s_in + (seg_start - c_start)
                                src_end = s_in + (seg_end - c_start)
                                src_start = max(0.0, min(src_start, s_out))
                                src_end = max(0.0, min(src_end, s_out))
                                if src_end <= src_start:
                                    continue
                                x0 = self._t_to_x(seg_start)
                                x1 = self._t_to_x(seg_end)
                                width = max(1, x1 - x0)

                                clip_rms = clip.get("rms", None) if isinstance(clip, dict) else None
                                clip_hop = clip.get("hop_s", _track_attr(t, "hop_s", 0.03))
                                if clip_rms is not None:
                                    env = self._env_for_rms(clip_rms, clip_hop, width, src_start, src_end)
                                else:
                                    env = self._env_for_width(i, width, src_start, src_end)

                                if env is not None:
                                    p.setPen(QPen(QColor(110, 160, 230), 1))
                                    mid = wave_top + wave_h // 2
                                    lim = min(width, int(env.size))
                                    for xi in range(lim):
                                        amp = int(env[xi] * (wave_h * 0.45))
                                        x = x0 + xi
                                        p.drawLine(x, mid - amp, x, mid + amp)
                        else:
                            if track_dur > 0.0:
                                seg_start = max(vs, 0.0)
                                seg_end = min(ve, track_dur)
                                if seg_end > seg_start:
                                    x0 = self._t_to_x(seg_start)
                                    x1 = self._t_to_x(seg_end)
                                    width = max(1, x1 - x0)
                                    env = self._env_for_width(i, width, seg_start, seg_end)
                                    if env is not None:
                                        p.setPen(QPen(QColor(110, 160, 230), 1))
                                        mid = wave_top + wave_h // 2
                                        lim = min(width, int(env.size))
                                        for xi in range(lim):
                                            amp = int(env[xi] * (wave_h * 0.45))
                                            x = x0 + xi
                                            p.drawLine(x, mid - amp, x, mid + amp)

                    # cuts overlay (align to clip positions)
                    cuts = _track_attr(t, "cuts", []) or []
                    if self.duration > 0 and track_kind == "audio":
                        selected = self._selected_by_track.get(i, set())
                        if clips:
                            for clip in clips:
                                try:
                                    c_start = float(clip.get("start", 0.0))
                                    c_end = float(clip.get("end", 0.0))
                                    s_in = float(clip.get("source_in", 0.0))
                                except Exception:
                                    continue
                                if c_end <= c_start:
                                    continue
                                try:
                                    clip_cuts = clip.get("cuts") or cuts
                                except Exception:
                                    clip_cuts = cuts
                                if not clip_cuts:
                                    continue
                                is_active_clip = self._clip_matches_active(t, clip)
                                sel = selected if is_active_clip else set()
                                for ci, c in enumerate(clip_cuts):
                                    # cuts are segment-relative (0..segment duration)
                                    t1 = c_start + float(c.start)
                                    t2 = c_start + float(c.end)
                                    x1 = self._t_to_x(t1)
                                    x2 = self._t_to_x(t2)
                                    if ci in sel:
                                        fill = QColor(90, 170, 255, 160)
                                        edge = QColor(140, 210, 255, 220)
                                    else:
                                        fill = QColor(255, 90, 90, 130)
                                        edge = QColor(255, 140, 140, 200)
                                    width = x2 - x1
                                    if width < 2:
                                        # Draw a visible marker for very small cuts
                                        p.setPen(QPen(edge, 2))
                                        p.drawLine(x1, wave_top, x1, wave_top + wave_h)
                                    else:
                                        p.setPen(QPen(edge, 1))
                                        p.setBrush(QBrush(fill))
                                        p.drawRect(x1, wave_top, width, wave_h)
                        else:
                            if cuts:
                                for ci, c in enumerate(cuts):
                                    x1 = self._t_to_x(c.start)
                                    x2 = self._t_to_x(c.end)
                                    if ci in selected:
                                        fill = QColor(90, 170, 255, 160)
                                        edge = QColor(140, 210, 255, 220)
                                    else:
                                        fill = QColor(255, 90, 90, 130)
                                        edge = QColor(255, 140, 140, 200)
                                    width = x2 - x1
                                    if width < 2:
                                        p.setPen(QPen(edge, 2))
                                        p.drawLine(x1, wave_top, x1, wave_top + wave_h)
                                    else:
                                        p.setPen(QPen(edge, 1))
                                        p.setBrush(QBrush(fill))
                                        p.drawRect(x1, wave_top, width, wave_h)

                    # pending manual cut markers (start/end + highlight)
                    ps, pe = self._pending_by_track.get(i, (None, None))
                    if self.duration > 0 and track_kind == "audio":
                        if clips:
                            # show on active clip only (source-aligned)
                            clip0 = None
                            for c in clips:
                                if self._clip_matches_active(t, c):
                                    clip0 = c
                                    break
                            if clip0 is None and clips:
                                clip0 = clips[0]
                            try:
                                c_start = float(clip0.get("start", 0.0)) if clip0 else 0.0
                                c_end = float(clip0.get("end", c_start)) if clip0 else c_start
                                s_in = float(clip0.get("source_in", 0.0)) if clip0 else 0.0
                                s_out = float(clip0.get("source_out", s_in + max(0.0, c_end - c_start))) if clip0 else s_in
                            except Exception:
                                c_start = 0.0
                                c_end = c_start
                                s_in = 0.0

                            def _pending_to_t(v: Optional[float]) -> Optional[float]:
                                if v is None:
                                    return None
                                try:
                                    vv = float(v)
                                except Exception:
                                    return None
                                clip_len = max(0.0, c_end - c_start)
                                # New behavior: pending values are segment-local (0..segment_len)
                                if clip_len > 0.0 and (-1e-6 <= vv <= clip_len + 1e-6):
                                    vv = max(0.0, min(vv, clip_len))
                                    return c_start + vv
                                # Backward compatibility: source-domain values
                                if s_out > s_in + 1e-6 and (s_in - 1e-6 <= vv <= s_out + 1e-6):
                                    return c_start + (vv - s_in)
                                # Fallback: already in timeline domain
                                if c_start - 1e-6 <= vv <= c_end + 1e-6:
                                    return vv
                                return None

                            tp_s = _pending_to_t(ps)
                            tp_e = _pending_to_t(pe)
                            if tp_s is not None and tp_e is not None:
                                a, b = (tp_s, tp_e) if tp_s <= tp_e else (tp_e, tp_s)
                                x1 = self._t_to_x(a)
                                x2 = self._t_to_x(b)
                                if x2 > x1:
                                    p.fillRect(x1, wave_top, x2 - x1, wave_h, QColor(255, 200, 60, 45))
                            if tp_s is not None:
                                x = self._t_to_x(tp_s)
                                p.setPen(QPen(QColor(255, 200, 60), 2))
                                p.drawLine(x, wave_top, x, wave_top + wave_h)
                            if tp_e is not None:
                                x = self._t_to_x(tp_e)
                                p.setPen(QPen(QColor(255, 200, 60), 2))
                                p.drawLine(x, wave_top, x, wave_top + wave_h)
                        else:
                            if ps is not None and pe is not None:
                                a, b = (ps, pe) if ps <= pe else (pe, ps)
                                x1 = self._t_to_x(a)
                                x2 = self._t_to_x(b)
                                if x2 > x1:
                                    p.fillRect(x1, wave_top, x2 - x1, wave_h, QColor(255, 200, 60, 45))
                            if ps is not None:
                                x = self._t_to_x(ps)
                                p.setPen(QPen(QColor(255, 200, 60), 2))
                                p.drawLine(x, wave_top, x, wave_top + wave_h)
                            if pe is not None:
                                x = self._t_to_x(pe)
                                p.setPen(QPen(QColor(255, 200, 60), 2))
                                p.drawLine(x, wave_top, x, wave_top + wave_h)

                    # clip labels (on top)
                    if clip_labels:
                        base_font = p.font()
                        label_font = base_font
                        label_font.setBold(True)
                        label_font.setPointSize(max(9, base_font.pointSize() + 1))
                        p.setFont(label_font)
                        fm = QFontMetrics(p.font())
                        for x1, x2, name, rtop, wtop in clip_labels:
                            label = fm.elidedText(str(name), Qt.ElideRight, max(10, x2 - x1 - 10))
                            if not label:
                                continue
                            text_w = fm.horizontalAdvance(label)
                            text_h = fm.height()
                            bx = x1 + 6
                            by = max(int(rtop) + 2, int(wtop) - (text_h + 8))
                            bw = min(text_w + 10, max(12, x2 - x1 - 6))
                            bh = text_h + 6
                            p.setPen(QPen(QColor(0, 0, 0, 0), 1))
                            p.setBrush(QBrush(QColor(10, 10, 12, 190)))
                            p.drawRoundedRect(bx - 2, by - 2, bw, bh, 4, 4)
                            p.setPen(QColor(245, 245, 245))
                            p.drawText(bx + 2, by + text_h, label)
                        p.setFont(base_font)

                    # track label (draw last, above overlays)
                    name = _track_attr(t, "name", f"Track {i + 1}")
                    base_font = p.font()
                    label_font = base_font
                    label_font.setBold(True)
                    p.setFont(label_font)
                    fm = QFontMetrics(p.font())
                    label = fm.elidedText(str(name), Qt.ElideRight, max(80, w // 2))
                    label_w = fm.horizontalAdvance(label) + 16
                    label_h = fm.height() + 6
                    lx = 12
                    ly = int(row_bot) - label_h - 6
                    p.setPen(QPen(label_border, 1))
                    p.setBrush(QBrush(label_bg))
                    p.drawRoundedRect(lx, ly, label_w, label_h, 6, 6)
                    if i == self.active_track:
                        p.setPen(label_text_active)
                    else:
                        p.setPen(label_text)
                    p.drawText(lx + 8, ly + label_h - 6, label)
                    p.setFont(base_font)

            # bottom time labels
            p.setPen(QColor(200, 200, 200))
            span = self._span()
            vs = max(0.0, min(self.view_start, self.duration))
            ve = max(0.0, min(vs + span, self.duration))
            left = fmt_hms(vs)
            right = fmt_hms(ve)
            fm = QFontMetrics(p.font())
            p.drawText(6, h - 6, left)
            p.drawText(w - fm.horizontalAdvance(right) - 6, h - 6, right)

            # outer border for clearer separation
            p.setPen(QPen(QColor(40, 40, 46), 1))
            p.setBrush(Qt.NoBrush)
            p.drawRect(0, 0, max(0, w - 1), max(0, h - 1))

            # playhead line + triangle (foreground)
            if self.duration > 0:
                x = self._t_to_x(self.playhead)
                x = max(0, min(w - 1, x))

                p.setPen(QPen(QColor(255, 80, 80), 3))
                p.drawLine(x, 0, x, h)

                p.setPen(Qt.NoPen)
                p.setBrush(QBrush(QColor(255, 80, 80)))
                tri = QPolygonF([
                    QPointF(x, ruler_h),
                    QPointF(x - 7, ruler_h - 11),
                    QPointF(x + 7, ruler_h - 11),
                ])
                p.drawPolygon(tri)

        except Exception:
            traceback.print_exc()
        finally:
            p.end()

    # -----------------------------
    # Mouse interactions
    # -----------------------------
    def mousePressEvent(self, e):
        if self.duration <= 0:
            return

        # Right click always opens context menu (create/remove cuts)
        if e.button() == Qt.RightButton:
            x = float(e.position().x())
            y = float(e.position().y())
            track_idx = self._track_at_y(y)
            if track_idx is not None and track_idx != self.active_track:
                self.setActiveTrack(track_idx, emit=True)
            active_idx = self.active_track
            t = self._x_to_t(x)
            cut_idx = self._cut_index_at_x(active_idx, x)

            row_kind = None
            track_state_idx = None
            clip_id = None
            source_t = None
            local_t = None
            if track_idx is not None:
                row = self._track(track_idx)
                row_kind = str(_track_attr(row, "kind", "audio") or "audio")
                clip = self._clip_at_x(track_idx, x)
                if isinstance(clip, dict):
                    try:
                        track_state_idx = clip.get("track_state_idx", None)
                    except Exception:
                        track_state_idx = None
                    try:
                        clip_id = clip.get("clip_id", None)
                    except Exception:
                        clip_id = None
                    try:
                        cs = float(clip.get("start", 0.0))
                        s_in = float(clip.get("source_in", 0.0))
                        local_t = float(t) - cs
                        source_t = s_in + local_t
                    except Exception:
                        local_t = None
                        source_t = None
                    if cut_idx is None:
                        try:
                            clip_cuts = clip.get("cuts") or []
                        except Exception:
                            clip_cuts = []
                        if clip_cuts and local_t is not None:
                            for i, c in enumerate(clip_cuts):
                                try:
                                    if float(c.start) <= float(local_t) <= float(c.end):
                                        cut_idx = i
                                        break
                                except Exception:
                                    continue
                        if cut_idx is None and clip_cuts and source_t is not None:
                            for i, c in enumerate(clip_cuts):
                                try:
                                    if float(c.start) <= float(source_t) <= float(c.end):
                                        cut_idx = i
                                        break
                                except Exception:
                                    continue

            meta = {
                "cut_idx": cut_idx,
                "track_state_idx": track_state_idx,
                "clip_id": clip_id,
                "row_kind": row_kind,
                "source_t": source_t,
                "local_t": local_t,
            }
            self.contextMenuRequested.emit(float(t), e.globalPosition(), meta)
            return

        x = float(e.position().x())
        y = float(e.position().y())

        ruler_h = 26
        playhead_x = float(self._t_to_x(self.playhead))

        if e.button() == Qt.LeftButton:
            track_idx = self._track_at_y(y)
            if track_idx is not None and track_idx != self.active_track:
                self.setActiveTrack(track_idx, emit=True)

            active_idx = self.active_track

            # Split tool: click on a clip to split
            if self._tool_mode == "split":
                if track_idx is None:
                    return
                clip = self._clip_at_x(track_idx, x)
                track = self._track(track_idx)
                if clip is not None and (track is None or self._clip_matches_active(track, clip)):
                    try:
                        clip_id = clip.get("clip_id", None)
                    except Exception:
                        clip_id = None
                    if clip_id:
                        t = self._x_to_t(x)
                        self.clipSplitRequested.emit(str(clip_id), float(t))
                return

            # Cut tool: let MainWindow handle 2-click pending cut workflow
            if self._tool_mode == "cut":
                t = self._x_to_t(x)
                row_kind = None
                track_state_idx = None
                source_t = None
                local_t = None
                if track_idx is not None:
                    row = self._track(track_idx)
                    row_kind = str(_track_attr(row, "kind", "audio") or "audio")
                    clip = self._clip_at_x(track_idx, x)
                    if isinstance(clip, dict):
                        try:
                            track_state_idx = clip.get("track_state_idx", None)
                        except Exception:
                            track_state_idx = None
                        try:
                            cs = float(clip.get("start", 0.0))
                            s_in = float(clip.get("source_in", 0.0))
                            local_t = float(t) - cs
                            source_t = s_in + local_t
                        except Exception:
                            local_t = None
                            source_t = None
                meta = {
                    "track_state_idx": track_state_idx,
                    "row_kind": row_kind,
                    "source_t": source_t,
                    "local_t": local_t,
                    "cut_tool_click": True,
                }
                self.contextMenuRequested.emit(float(t), e.globalPosition(), meta)
                return
            # SCRUB only when user intends it:
            # - near playhead (triangle/line)
            # - OR on ruler area
            if abs(x - playhead_x) <= 12.0 or y <= ruler_h:
                self._scrubbing = True
                self._scrub_offset_px = (x - playhead_x) if abs(x - playhead_x) <= 12.0 else 0.0
                self.scrubStarted.emit()
                t = self._x_to_t(x - self._scrub_offset_px)
                self.seekRequested.emit(float(t))
                return
            # clip drag
            clip = self._clip_at_x(active_idx, x)
            if clip is not None:
                try:
                    ts = clip.get("track_state_idx", None)
                    if ts is not None:
                        self.clipActivated.emit(int(ts))
                except Exception:
                    pass
                try:
                    c_start = float(clip.get("start", 0.0))
                    c_end = float(clip.get("end", 0.0))
                except Exception:
                    c_start = 0.0
                    c_end = 0.0
                t = self._x_to_t(x)
                self._drag_clip = clip
                self._drag_track_idx = active_idx
                self._drag_start_t = t
                self._drag_clip_start = c_start
                self._drag_clip_end = c_end
                self._drag_offset_t = t - c_start
                self._drag_preview_start = c_start
                self.update()
                return

            # Click on a cut -> select + click action (remove etc in MainWindow)
            cut_idx = self._cut_index_at_x(active_idx, x)
            if cut_idx is not None:
                self._selected_by_track[active_idx] = {cut_idx}
                self.update()
                self.cutClicked.emit(cut_idx, e.globalPosition())
                return

            # Rectangle selection only with Shift; otherwise scrub anywhere
            if e.modifiers() & Qt.ShiftModifier:
                track = self._track(active_idx)
                clips = _track_attr(track, "clips", []) if track is not None else []
                if clips:
                    clip = self._clip_at_x(active_idx, x)
                    if clip is None or not self._clip_matches_active(track, clip):
                        return
                    self._sel_clip = clip
                else:
                    self._sel_clip = None

                self._sel_start_x = x
                self._sel_current_x = x
                self._sel_track_idx = active_idx
                self._selected_by_track[active_idx] = set()
                self.update()
                return

            # default: click anywhere to move/scrub playhead
            self._scrubbing = True
            self._scrub_offset_px = 0.0
            self.scrubStarted.emit()
            t = self._x_to_t(x)
            self.seekRequested.emit(float(t))
            return

    def mouseMoveEvent(self, e):
        if self.duration <= 0:
            return

        x = float(e.position().x())

        # clip drag
        if self._drag_clip is not None:
            t = self._x_to_t(x)
            dur = max(0.0, self._drag_clip_end - self._drag_clip_start)
            new_start = max(0.0, t - self._drag_offset_t)
            new_start = self._snap_time(new_start, dur, self._drag_clip.get("clip_id"))
            self._drag_preview_start = new_start
            self.update()
            return

        # scrubbing
        if self._scrubbing:
            t = self._x_to_t(x - self._scrub_offset_px)
            self.seekRequested.emit(float(t))
            return

        # selection box
        if self._sel_start_x is not None and self._sel_track_idx is not None:
            self._sel_current_x = x

            x1 = min(self._sel_start_x, self._sel_current_x)
            x2 = max(self._sel_start_x, self._sel_current_x)

            t1 = self._x_to_t(x1)
            t2 = self._x_to_t(x2)

            sel = set()
            cuts = self._track_cuts(self._sel_track_idx)
            # map to source time if clips exist
            track = self._track(self._sel_track_idx)
            clips = _track_attr(track, "clips", []) if track is not None else []
            if clips:
                clip0 = self._sel_clip
                if clip0 is None or not self._clip_matches_active(track, clip0):
                    self.update()
                    return
                try:
                    cs = float(clip0.get("start", 0.0))
                    s_in = float(clip0.get("source_in", 0.0))
                except Exception:
                    cs = 0.0
                    s_in = 0.0
                t1 = s_in + (t1 - cs)
                t2 = s_in + (t2 - cs)
            for i, c in enumerate(cuts):
                if c.end >= t1 and c.start <= t2:
                    sel.add(i)
            self._selected_by_track[self._sel_track_idx] = sel

            self.update()
            return

    def mouseReleaseEvent(self, e):
        # end scrubbing
        if self._scrubbing:
            self._scrubbing = False
            self.scrubEnded.emit()
            return

        # end clip drag
        if self._drag_clip is not None:
            new_start = self._drag_preview_start
            clip_id = self._drag_clip.get("clip_id") if isinstance(self._drag_clip, dict) else None
            target_id = None
            try:
                x = float(e.position().x())
            except Exception:
                x = None
            if x is not None and self._drag_track_idx is not None:
                target = self._clip_at_x(self._drag_track_idx, x)
                if target is not None:
                    try:
                        target_id = target.get("clip_id")
                    except Exception:
                        target_id = None
            self._drag_clip = None
            self._drag_track_idx = None
            self._drag_preview_start = None
            if clip_id is not None and target_id is not None and str(target_id) != str(clip_id):
                self.clipSwapRequested.emit(str(clip_id), str(target_id))
            elif clip_id is not None and new_start is not None:
                self.clipMoveRequested.emit(str(clip_id), float(new_start))
            self.update()
            return

        # finalize selection
        if self._sel_start_x is not None and self._sel_track_idx is not None:
            selected = self._selected_by_track.get(self._sel_track_idx, set())
            if selected:
                self.cutsSelected.emit(sorted(selected), e.globalPosition())
            self._sel_start_x = None
            self._sel_current_x = None
            self._sel_track_idx = None
            self._sel_clip = None
            return
