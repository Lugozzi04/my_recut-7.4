"""Twitch clip -> YouTube Short: one ready-to-publish 1080x1920 video that follows short-form rules.

  1. FIRST SECOND = the climax or a strong reaction, no intro. The peak (loud voice + reaction words) is found;
     when it is not already at the start, ~2 s of it are played first (cold open), then the clip runs from its
     setup. Dead air before the first words is trimmed.
  2. VERTICAL: the stream webcam on top, the board below ZOOMED ON THE PLAY: the crop is centred where the
     board changes in the seconds before the peak (webcam and stream trackers masked) and zoomed just enough to
     keep that area (at most MAX_ZOOM; no zoom when the change is spread over the whole board).
  3. BIG CAPTIONS, word by word (captions.BIG_FONT_SCALE, shrunk only when a line would not fit).
  4. 20-40 s, ENDING right after the reaction (no tail): the loop goes back to the cold open, which invites a
     rewatch.

The source is a Twitch clip link (downloaded with yt-dlp) or a video file already on disk. The analysis is the
same as for Clips (ClipSession: Whisper words, voice loudness, webcam ROI), so nothing is sent to any provider.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np

from .candidates import LEAD_S, TAIL_S, Analysis
from .models import ClipCandidate, ClipError
from .scoring import GRID_S, Series, reaction

MIN_S, MAX_S = 20.0, 40.0
TARGET_S = 32.0  # preferred total length when the clip is longer (setup before the peak is cut first)
TEASER_S = (1.2, 3.0)  # cold open length
TEASER_AROUND = (0.7, 1.5)  # cold open: from 0.7 s before the peak to 1.5 s after it (snapped to words)
COLD_OPEN_AFTER_S = 2.0  # a peak later than this from the start of the Short gets a cold open
REACTION_TAIL_S = 6.0  # speech after the peak that still belongs to the reaction
REACTION_GAP_S = 0.8  # a pause this long after the peak ends the reaction
DEAD_INTRO_S = 1.5  # leading silence longer than this is trimmed
SILENT_TAIL_S = 1.5  # silence after the last words longer than this is trimmed (unless the voice is still loud)
FOCUS_WINDOW = (8.0, 1.5)  # board changes measured from 8 s before the peak to 1.5 s after it
FOCUS_REGION = (0.2, 0.17, 0.8, 0.83)  # Hearthstone play area (x0, y0, x1, y1), as packaging.visual.BOARD_REGION
FOCUS_MARGIN = 1.35  # the zoomed crop keeps the changing area plus this margin
MIN_ZOOM, MAX_ZOOM = 1.12, 1.5  # below MIN_ZOOM the board is not zoomed; above MAX_ZOOM it gets too soft
FOCUS_SIZE = (320, 180)
FOCUS_FPS = 4
PIXEL_CHANGE = 20
WEBCAM_FRAMES = 24
CLIP_URL = re.compile(r"^https?://(?:clips\.twitch\.tv/(?:embed\?clip=)?[\w-]+|"
                      r"(?:www\.|m\.)?twitch\.tv/[\w]+/clip/[\w-]+)(?:[/?#].*)?$", re.IGNORECASE)

Progress = Callable[[str, str], None]


def is_clip_url(text: str) -> bool:
    return bool(CLIP_URL.match(text.strip()))


# ---- download --------------------------------------------------------------------------------------------
def default_clip_folder() -> Path:
    from automation.downloader import default_download_dir
    return default_download_dir() / "twitch_clips"


def download_clip(url: str, folder: Path | None = None, *, module_loader: Callable[[], Any] | None = None,
                  cancellation: Any = None) -> tuple[Path, dict[str, Any]]:
    """Downloads a Twitch clip (one small MP4) with yt-dlp; reuses a previous download of the same clip.
    Returns the file and the public metadata (also saved as <file>.twitch.json)."""
    from automation.downloader import _load_yt_dlp, _YtDlpLogger
    if not is_clip_url(url):
        raise ClipError("invalid_clip_url", "Not a Twitch clip link (clips.twitch.tv/… or twitch.tv/<channel>/clip/…).")
    folder = Path(folder or default_clip_folder())
    folder.mkdir(parents=True, exist_ok=True)

    def check(_status: dict[str, Any]) -> None:
        if cancellation is not None and cancellation.is_cancelled:
            raise ClipError("cancelled", "Download cancelled")
    options = {"format": "best[ext=mp4]/best", "outtmpl": str(folder / "%(id)s.%(ext)s"), "noplaylist": True,
               "quiet": True, "no_warnings": True, "ignoreconfig": True, "logger": _YtDlpLogger(),
               "socket_timeout": 20, "progress_hooks": [check], "overwrites": False}
    try:
        with (module_loader or _load_yt_dlp)().YoutubeDL(options) as downloader:
            info = downloader.extract_info(url.strip(), download=True)
            done = (info.get("requested_downloads") or [{}])[0].get("filepath") if isinstance(info, dict) else None
            path = Path(done or downloader.prepare_filename(info))
    except ClipError:
        raise
    except Exception as exc:  # noqa: BLE001 - network / extractor errors, shown to the user
        if cancellation is not None and cancellation.is_cancelled:
            raise ClipError("cancelled", "Download cancelled") from exc
        raise ClipError("clip_download_failed", f"Could not download the Twitch clip: {exc}") from exc
    if not path.is_file():
        raise ClipError("clip_download_failed", "yt-dlp finished without a video file.")
    meta = {"url": url.strip(), "id": str(info.get("id") or ""), "title": str(info.get("title") or "").strip(),
            "channel": str(info.get("channel") or info.get("uploader") or ""),
            "creator": str(info.get("creator") or ""), "duration_s": info.get("duration"),
            "game": str(info.get("game") or "")}
    path.with_suffix(".twitch.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return path, meta


def clip_metadata(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.with_suffix(".twitch.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# ---- plan --------------------------------------------------------------------------------------------------
@dataclass
class ShortPlan:
    main: tuple[float, float]  # the clip from its setup to the end of the reaction (source seconds)
    peak: float
    teaser: tuple[float, float] | None = None  # cold open, played first
    focus: tuple[float, float, float] | None = None  # board zoom: normalized centre x, y and zoom
    reason: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def parts(self) -> list[tuple[int, float, float]]:
        return ([(0, *self.teaser)] if self.teaser else []) + [(0, *self.main)]

    @property
    def duration(self) -> float:
        return sum(b - a for _, a, b in self.parts)


def _words(analysis: Analysis) -> list[tuple[float, float, str]]:
    out = []
    for line in analysis.lines:
        if line.words:
            out.extend(line.words)
        else:
            tokens = line.text.split()
            step = (line.end - line.start) / max(1, len(tokens))
            out.extend((line.start + n * step, line.start + (n + 1) * step, t) for n, t in enumerate(tokens))
    return sorted(out)


def find_peak(analysis: Analysis, series: Series) -> tuple[float, str]:
    """The climax: loud voice (smoothed over 1.5 s) plus reaction words of the line being spoken."""
    n = min(len(series.arousal), int(np.ceil(analysis.duration / GRID_S)))
    if n <= 0:
        return 0.0, "empty clip"
    loud = np.convolve(series.arousal[:n], np.ones(3) / 3.0, mode="same")
    words = np.zeros(n)
    labels: dict[int, list[str]] = {}
    for line in analysis.lines:
        value, found = reaction(line.text)
        a, b = int(line.start / GRID_S), min(n, int(np.ceil(line.end / GRID_S)))
        for cell in range(max(0, a), b):
            if value > words[cell]:
                words[cell], labels[cell] = value, found
    score = loud + 0.6 * words
    if float(score.max()) <= 0.0:
        return analysis.duration * 0.6, "no clear peak (no loud reaction or reaction words)"
    cell = int(np.argmax(score))
    parts = (["excited voice"] if loud[cell] >= 0.6 else []) + \
        ([("reaction: " + ", ".join(labels.get(cell, [])[:3]))] if words[cell] > 0 else [])
    return (cell + 0.5) * GRID_S, "; ".join(parts) or "loudest moment"


def _teaser(words: Sequence[tuple[float, float, str]], peak: float, duration: float) -> tuple[float, float]:
    """~2 s around the peak, starting and ending on word boundaries."""
    a, b = peak - TEASER_AROUND[0], peak + TEASER_AROUND[1]
    for start, end, _ in words:
        if start < a < end:
            a = start if a - start <= 0.5 else end
        if start < b < end:
            b = end if end - b <= 0.8 else start
    a = max(0.0, a)
    b = min(duration, max(b, a + TEASER_S[0]), a + TEASER_S[1])
    return round(a, 3), round(b, 3)


def _reaction_end(analysis: Analysis, peak: float) -> float:
    """End of the reaction: speech that follows the peak without a real pause (at most REACTION_TAIL_S)."""
    end = min(analysis.duration, peak + TEASER_AROUND[1])
    for line in analysis.lines:
        if line.end <= peak:
            continue
        if line.start - end > REACTION_GAP_S or line.end > peak + REACTION_TAIL_S:
            break
        end = max(end, line.end)
    return min(analysis.duration, end + TAIL_S)


def plan_short(analysis: Analysis, series: Series, *, max_s: float = MAX_S, cold_open: bool = True) -> ShortPlan:
    """A Twitch clip was already chosen by a person: when it fits, it is kept whole (dead intro and silent tail
    trimmed); a longer one is cut to the setup before the peak and the reaction after it."""
    duration = analysis.duration
    lines = analysis.lines
    peak, why = find_peak(analysis, series)
    notes: list[str] = []
    teaser = _teaser(_words(analysis), peak, duration) if cold_open else None
    budget = max_s - (teaser[1] - teaser[0] if teaser else 0.0)
    start, end = 0.0, duration
    if lines and DEAD_INTRO_S < lines[0].start < peak:
        start = lines[0].start - LEAD_S
        notes.append("silent intro trimmed")
    last = max(line.end for line in lines) if lines else duration
    after = series.window(series.arousal, last, duration) if lines else np.zeros(0)
    if peak < last and duration - last > SILENT_TAIL_S and (after.size == 0 or float(after.max()) < 0.5):
        end = min(duration, last + TAIL_S + 0.3)
        notes.append("silent tail trimmed")
    if end - start > budget:
        end = _reaction_end(analysis, peak)
        start = max(start, end - min(budget, TARGET_S - (teaser[1] - teaser[0] if teaser else 0.0)))
        covering = next((line for line in lines if line.start < start < line.end), None)
        if covering is not None:  # do not start in the middle of a sentence
            start = covering.start - LEAD_S if end - (covering.start - LEAD_S) <= budget else covering.end
        notes.append(f"cut to the setup and the reaction around the peak ({end - start:.0f} s of {duration:.0f})")
    start = max(0.0, start)
    if teaser is not None and peak - start < COLD_OPEN_AFTER_S:
        teaser = None  # the climax is already in the first seconds
    if teaser is not None:
        notes.append("cold open: the peak is shown first")
    total = end - start + (teaser[1] - teaser[0] if teaser else 0.0)
    if total < MIN_S:
        notes.append(f"the clip is short: the Short lasts {total:.0f} s")
    return ShortPlan((round(start, 3), round(end, 3)), round(peak, 2), teaser, None, why, notes)


# ---- zoom on the play ------------------------------------------------------------------------------------
def decode_gray(ffmpeg: str, video: str, start: float, end: float) -> np.ndarray:
    from utils.subprocess_utils import run_no_window
    w, h = FOCUS_SIZE
    done = run_no_window([ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-ss", f"{max(0.0, start):.3f}",
                          "-t", f"{max(0.1, end - start):.3f}", "-i", video, "-an",
                          "-vf", f"fps={FOCUS_FPS},scale={w}:{h},format=gray", "-f", "rawvideo", "-"],
                         capture_output=True, timeout=120)
    data = done.stdout or b""
    count = len(data) // (w * h)
    return np.frombuffer(data[: count * w * h], dtype=np.uint8).reshape(count, h, w)


def play_focus(frames: np.ndarray, roi: tuple[float, float, float, float] | None, panel_width: float
               ) -> tuple[tuple[float, float, float] | None, str]:
    """Where the board changes (webcam and trackers masked) -> (centre x, centre y, zoom).

    panel_width: width of the full-height board crop as a share of the frame width (zoom 1)."""
    if len(frames) < 3:
        return None, "board zoom off: too few frames"
    count, h, w = frames.shape
    ys, xs = (np.arange(h)[:, None] + 0.5) / h, (np.arange(w)[None, :] + 0.5) / w
    x0, y0, x1, y1 = FOCUS_REGION
    mask = (xs >= x0) & (xs < x1) & (ys >= y0) & (ys < y1)
    if roi is not None:
        rx, ry, rw, rh = roi
        mask &= ~((xs >= rx - 0.02) & (xs < rx + rw + 0.02) & (ys >= ry - 0.02) & (ys < ry + rh + 0.02))
    diff = np.abs(np.diff(frames.astype(np.int16), axis=0)) > PIXEL_CHANGE
    heat = diff.sum(axis=0).astype(np.float64) * mask
    total = float(heat.sum())
    if total < 0.002 * mask.sum() * (count - 1):
        return None, "board zoom off: the board barely changes"
    weights_x, weights_y = heat.sum(axis=0), heat.sum(axis=1)

    def spread(weights: np.ndarray, size: int) -> tuple[float, float, float]:
        cumulative = np.cumsum(weights) / weights.sum()
        lo, hi = np.searchsorted(cumulative, 0.1), np.searchsorted(cumulative, 0.9)
        return float((np.arange(size) * weights).sum() / weights.sum() + 0.5) / size, lo / size, (hi + 1) / size
    cx, lo_x, hi_x = spread(weights_x, w)
    cy, lo_y, hi_y = spread(weights_y, h)
    need_w, need_h = (hi_x - lo_x) * FOCUS_MARGIN, (hi_y - lo_y) * FOCUS_MARGIN
    zoom = float(min(MAX_ZOOM, panel_width / max(need_w, 1e-6), 1.0 / max(need_h, 1e-6)))
    if zoom < MIN_ZOOM:
        return (round(cx, 3), 0.5, 1.0), "board centred on the play (the action covers the whole board: no zoom)"
    return (round(cx, 3), round(cy, 3), round(zoom, 2)), f"board zoomed x{zoom:.2f} on the play"


def _clip_faces(image: np.ndarray, cascades: Sequence[Any]) -> list[tuple[float, float, float, float]]:
    """Faces at FULL resolution with the frontal and profile cascades (profile also mirrored): a webcam face in
    a corner is ~60 px tall in 1080p and often turned to the screen, which the 960 px frontal pass misses."""
    import cv2
    gray = cv2.equalizeHist(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY))
    h, w = gray.shape
    side = max(24, int(0.035 * h))
    out = []
    for n, cascade in enumerate(cascades):
        for flipped in ((False, True) if n == len(cascades) - 1 else (False,)):
            found = cascade.detectMultiScale(gray[:, ::-1] if flipped else gray, 1.1, 4, minSize=(side, side))
            for x, y, fw, fh in found:
                x = w - x - fw if flipped else x
                out.append((x / w, y / h, fw / w, fh / h))
    return out


def detect_clip_webcam(ffmpeg: str, video: str, duration: float, size: tuple[int, int],
                       count: int = WEBCAM_FRAMES) -> tuple[tuple[float, float, float, float] | None, str]:
    """Webcam of a short clip: the packaging analysis samples it from a handful of highlight frames, too few
    for a stable face. Here WEBCAM_FRAMES frames across the clip; a face counts only when it sits at the same
    place in >= 40 % of them AND that place moves (a person does, game art does not); the overlay rectangle
    around it is then found as in the packaging analysis."""
    import cv2
    from utils.subprocess_utils import run_no_window
    from analysis.packaging.webcam import overlay_borders, parse_roi
    w, h = size
    done = run_no_window([ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-i", video, "-an",
                          "-vf", f"fps={count / max(duration, 1.0):.4f}", "-pix_fmt", "bgr24", "-f", "rawvideo", "-"],
                         capture_output=True, timeout=180)
    data, size = done.stdout or b"", w * h * 3
    frames = [np.frombuffer(data[n * size:(n + 1) * size], dtype=np.uint8).reshape(h, w, 3)
              for n in range(len(data) // size)]
    if len(frames) < 4:
        return None, "too few frames"
    cascades = [cv2.CascadeClassifier(cv2.data.haarcascades + name) for name in
                ("haarcascade_frontalface_default.xml", "haarcascade_frontalface_alt2.xml",
                 "haarcascade_profileface.xml")]
    clusters: list[list[tuple[int, tuple[float, float, float, float]]]] = []
    for i, frame in enumerate(frames):
        for box in _clip_faces(frame, cascades):
            cx, cy = box[0] + box[2] / 2, box[1] + box[3] / 2
            for cluster in clusters:
                ref = cluster[0][1]
                if abs(cx - (ref[0] + ref[2] / 2)) < 0.05 and abs(cy - (ref[1] + ref[3] / 2)) < 0.07:
                    cluster.append((i, box))
                    break
            else:
                clusters.append([(i, box)])
    gray = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.int16) for f in frames]
    for cluster in sorted(clusters, key=lambda c: -len({i for i, _ in c})):
        share = len({i for i, _ in cluster}) / len(frames)
        if share < 0.4:
            break
        fx, fy, fw, fh = (float(np.median([b[k] for _, b in cluster])) for k in range(4))
        x0, y0, x1, y1 = int(fx * w), int(fy * h), int((fx + fw) * w), int((fy + fh) * h)
        motion = float(np.mean([np.mean(np.abs(b[y0:y1, x0:x1] - a[y0:y1, x0:x1])) for a, b in zip(gray, gray[1:])]))
        if motion < 3.0:
            continue  # a face drawn in the game (hero portrait, card art): it does not move
        box, _sides = overlay_borders([frames[i] for i in sorted({i for i, _ in cluster})], (fx, fy, fw, fh))
        check = parse_roi(box, origin="auto")
        if check.rect is not None:
            r = check.rect
            return (r.x, r.y, r.w, r.h), f"face in {share:.0%} of the frames"
    return None, "no moving face at a stable place"


# ---- captions --------------------------------------------------------------------------------------------
def short_words(analysis: Analysis, parts: Sequence[tuple[int, float, float]]) -> list[tuple[float, float, str]]:
    """Words of every part on the Short's own timeline (the cold open first, then the clip)."""
    out, offset = [], 0.0
    words = _words(analysis)
    for _, a, b in parts:
        out.extend((max(0.0, s - a) + offset, min(b, e) - a + offset, t) for s, e, t in words
                   if t and e > a and s < b)
        offset += b - a
    return sorted(out)


# ---- the whole job ---------------------------------------------------------------------------------------
def _safe_stem(path: Path) -> str:
    return re.sub(r"[^\w\-]+", "_", path.stem, flags=re.UNICODE).strip("_")[:50] or "clip"


def make_short(source: str, *, output: str | None = None, max_s: float = MAX_S, cold_open: bool = True,
               zoom: bool = True, subtitles: bool = True, codec: str = "auto", loudnorm: bool = True,
               progress: Progress | None = None, cancellation: Any = None, downloader: Callable[..., Any] | None
               = None, session_factory: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Twitch clip link or video file -> <clips folder>/<name>_short.mp4 (+ preview PNG and short.json)."""
    from utils.codec_detection import resolve_video_codec
    from utils.ffmpeg import ensure_ffmpeg
    from .captions import BIG_FONT_SCALE, ass_document, install_font
    from .layout import compute_layout
    from .render import render_clip
    from .service import ClipSession, _video_size

    report = progress or (lambda stage, message: None)
    if not MIN_S / 2 <= max_s <= 60:
        raise ClipError("invalid_arguments", "Short length must be between 10 and 60 seconds.")
    text = source.strip().strip('"')
    if is_clip_url(text):
        report("download", "downloading the Twitch clip")
        path, meta = (downloader or download_clip)(text, cancellation=cancellation)
    else:
        path = Path(text).expanduser().resolve()
        if not path.is_file():
            raise ClipError("source_missing", f"Not a Twitch clip link nor a video file: {source}")
        meta = clip_metadata(path)
    path = Path(path)
    session = (session_factory or ClipSession)(video=str(path), output=output, progress=report,
                                                cancellation=cancellation)
    analysis = session.load_analysis()
    series = session._series(analysis)
    report("plan", "finding the climax and the end of the reaction")
    plan = plan_short(analysis, series, max_s=max_s, cold_open=cold_open)
    ffmpeg, ffprobe = ensure_ffmpeg()
    width, height = _video_size(ffprobe, str(path))
    roi = analysis.webcam_roi
    if roi is None:
        report("webcam", "looking for the webcam on frames across the whole clip")
        roi, message = detect_clip_webcam(ffmpeg, str(path), analysis.duration, (width, height))
        plan.notes.append("webcam found on the clip frames" if roi is not None else
                          f"no webcam: board only ({message})")
    base = compute_layout(width, height, roi)
    if zoom:
        report("zoom", "measuring where the play happens on the board")
        a = max(plan.main[0], plan.peak - FOCUS_WINDOW[0])
        frames = decode_gray(ffmpeg, str(path), a, min(analysis.duration, plan.peak + FOCUS_WINDOW[1]))
        plan.focus, note = play_focus(frames, roi, base.board[2] / width)
        plan.notes.append(note)
    layout = compute_layout(width, height, roi, focus=plan.focus)
    stem = _safe_stem(path)
    folder = session.folder
    folder.mkdir(parents=True, exist_ok=True)
    clip = ClipCandidate("SHORT", 0.0, round(plan.duration, 3), 0.0, plan.reason,
                         text=" ".join(t for _, _, t in short_words(analysis, plan.parts))[:400], parts=plan.parts)
    captions = None
    if subtitles:
        font = install_font(folder)
        words = short_words(analysis, plan.parts)
        captions = f"{stem}_short.ass"
        (folder / captions).write_text(ass_document(words, duration=clip.duration, size=layout.size,
                                                    caption_y=layout.caption_y, font=font, scale=BIG_FONT_SCALE),
                                       encoding="utf-8")
    preview = folder / f"{stem}_short_preview.png"
    _preview(ffmpeg, str(path), plan, layout, preview, analysis, folder, subtitles)
    report("render", f"rendering the Short ({clip.duration:.1f} s, 1080x1920)")
    shown = [-1]

    def rendered(value: float) -> None:  # every 10 %
        if int(value * 10) != shown[0]:
            shown[0] = int(value * 10)
            report("render", f"{shown[0] * 10}%")
    result = render_clip(clip, [str(path)], folder / f"{stem}_short.mp4", ffmpeg=ffmpeg,
                         codec=resolve_video_codec(ffmpeg, codec).resolved, cancellation=cancellation,
                         loudnorm=loudnorm, layout=layout, captions=captions, fonts=".fonts" if captions else None,
                         on_progress=rendered)
    title = meta.get("title") or ""
    summary = {"format": "recut_short", "version": 1, "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "source": str(path), "twitch": meta, "output": result["path"], "duration_s": result["duration_s"],
               "preview": str(preview) if preview.is_file() else None, "plan": asdict(plan),
               "layout": {"mode": layout.mode, "board_crop": list(layout.board), "webcam": layout.webcam,
                          "webcam_roi": list(roi) if roi else None},
               "youtube": {"title": (title[:90] + " #Shorts") if title else "", "description": title},
               "codec": result["codec"], "warnings": session.warnings}
    (folder / f"{stem}_short.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    return summary


def _preview(ffmpeg: str, video: str, plan: ShortPlan, layout: Any, target: Path, analysis: Analysis,
             folder: Path, subtitles: bool) -> None:
    """The first frame of the Short (the peak when there is a cold open) with layout and captions."""
    from utils.subprocess_utils import run_no_window
    from .captions import BIG_FONT_SCALE, ass_document, install_font
    from .render import preview_command
    at = plan.parts[0][1] + 0.4
    still = folder / ".short_frame.png"
    run_no_window([ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-ss", f"{at:.3f}", "-i", video,
                   "-frames:v", "1", "-update", "1", str(still)], capture_output=True, timeout=60)
    if not still.is_file():
        return
    captions = None
    if subtitles:
        words = [w for w in short_words(analysis, plan.parts) if w[0] < 3.0][:3]
        if words:
            captions = ".short_preview.ass"
            shown = [(0.0, 10.0, words[0][2])] + [(10.0 + n, 11.0 + n, w[2]) for n, w in enumerate(words[1:])]
            (folder / captions).write_text(ass_document(shown, duration=10.0, size=layout.size,
                                                        caption_y=layout.caption_y, font=install_font(folder),
                                                        scale=BIG_FONT_SCALE), encoding="utf-8")
    run_no_window(preview_command(ffmpeg, Path(still.name), layout, Path(target.name), captions=captions,
                                  fonts=".fonts" if captions else None),
                  capture_output=True, cwd=str(folder), timeout=60)
