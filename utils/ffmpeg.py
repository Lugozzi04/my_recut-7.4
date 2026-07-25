import json
import os
import shutil
import subprocess
import hashlib
import importlib
import threading
import bisect
from pathlib import Path
from typing import Any, Optional, Tuple, List, Iterator

from utils.runtime_paths import cache_root as runtime_cache_root
from utils.runtime_paths import project_root as runtime_project_root
from utils.subprocess_utils import run_no_window

av: Any
try:
    av = importlib.import_module("av")
except Exception:  # pragma: no cover - optional dependency
    av = None


class FFmpegNotFound(RuntimeError):
    pass


def _project_root() -> Path:
    return runtime_project_root()

def _candidate_dirs() -> List[Path]:
    root = _project_root()
    return [root / "bin", root]


def _find_exe(env_key: str, names: List[str]) -> Optional[str]:
    # 1) Explicit override.
    v = os.environ.get(env_key)
    if v and Path(v).exists():
        return str(Path(v))

    # 2) Bundled binaries. Keep runtime behavior stable across machines.
    for d in _candidate_dirs():
        for n in names:
            p = d / n
            if p.exists():
                return str(p)

    # 3) System PATH fallback for development installs without bundled binaries.
    for n in names:
        resolved = shutil.which(n)
        if resolved:
            return resolved

    return None


def ensure_ffmpeg() -> Tuple[str, str]:
    ffmpeg = _find_exe("FFMPEG_BIN", ["ffmpeg", "ffmpeg.exe"])
    ffprobe = _find_exe("FFPROBE_BIN", ["ffprobe", "ffprobe.exe"])
    if not ffmpeg or not ffprobe:
        root = _project_root()
        raise FFmpegNotFound(
            "FFmpeg/FFprobe non trovati.\n\n"
            "Controlla che esistano:\n"
            f"  {root / 'bin' / 'ffmpeg.exe'}\n"
            f"  {root / 'bin' / 'ffprobe.exe'}\n\n"
            "Soluzioni:\n"
            "1) Installa FFmpeg e aggiungi ffmpeg/ffprobe al PATH\n"
            "oppure\n"
            "2) Copia ffmpeg.exe e ffprobe.exe nella cartella bin/ dell'app\n"
            "oppure\n"
            "3) Imposta variabili ambiente FFMPEG_BIN e FFPROBE_BIN."
        )
    return ffmpeg, ffprobe


def run_cmd(cmd: List[str], timeout_s: float | None = None) -> subprocess.CompletedProcess:
    # Keep ffprobe bounded: some malformed files can hang probing indefinitely.
    # Default timeout can be overridden with AUTO_CUTTER_FFPROBE_TIMEOUT (seconds), 0 = no timeout.
    if timeout_s is None:
        timeout_s = 180.0
        try:
            raw = str(os.environ.get("AUTO_CUTTER_FFPROBE_TIMEOUT", "180")).strip()
            timeout_s = float(raw or "180")
        except Exception:
            timeout_s = 180.0
    else:
        try:
            timeout_s = float(timeout_s)
        except Exception:
            timeout_s = 180.0
    if timeout_s <= 0:
        timeout_s = None

    try:
        # Su Windows ffprobe puo emettere bytes non decodificabili in cp1252:
        # forziamo UTF-8 e non falliamo mai sulla decodifica.
        return run_no_window(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as e:
        if timeout_s is None:
            raise RuntimeError("ffprobe timeout") from e
        raise RuntimeError(f"ffprobe timeout after {int(float(timeout_s))}s") from e

def ffprobe_duration_seconds(path: str) -> float:
    _, ffprobe = ensure_ffmpeg()
    cmd = [
        ffprobe, "-v", "error",
        "-probesize", "50M", "-analyzeduration", "100M",
        "-print_format", "json",
        "-show_format",
        path
    ]
    p = run_cmd(cmd)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or "ffprobe failed")
    if not p.stdout:
        raise RuntimeError(p.stderr.strip() or "ffprobe returned empty output")
    data = json.loads(p.stdout)
    return float(data["format"]["duration"])


def has_audio_stream(path: str) -> bool:
    _, ffprobe = ensure_ffmpeg()
    cmd = [
        ffprobe, "-v", "error",
        "-probesize", "50M", "-analyzeduration", "100M",
        "-select_streams", "a",
        "-show_entries", "stream=index",
        "-of", "json",
        path
    ]
    p = run_cmd(cmd)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or "ffprobe failed")
    if not p.stdout:
        raise RuntimeError(p.stderr.strip() or "ffprobe returned empty output") 
    data = json.loads(p.stdout)
    return bool(data.get("streams"))


def has_video_stream(path: str) -> bool:
    _, ffprobe = ensure_ffmpeg()
    cmd = [
        ffprobe, "-v", "error",
        "-probesize", "50M", "-analyzeduration", "100M",
        "-select_streams", "v",
        "-show_entries", "stream=index",
        "-of", "json",
        path
    ]
    p = run_cmd(cmd)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or "ffprobe failed")
    if not p.stdout:
        raise RuntimeError(p.stderr.strip() or "ffprobe returned empty output")
    data = json.loads(p.stdout)
    return bool(data.get("streams"))


# -----------------------------
# Splice-safe RAP scan cache (PyAV)
# -----------------------------
_SPLICE_LOCK_GUARD = threading.Lock()
_SPLICE_LOCKS: dict[str, threading.Lock] = {}


def _get_splice_lock(path: str) -> threading.Lock:
    key = str(_splice_cache_path(path))
    with _SPLICE_LOCK_GUARD:
        lock = _SPLICE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _SPLICE_LOCKS[key] = lock
        return lock


def _splice_cache_root() -> Path:
    d = runtime_cache_root() / "splice_points"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _splice_cache_path(path: str) -> Path:
    p = str(Path(path).resolve())
    if os.name == "nt":
        p = p.lower()
    h = hashlib.sha1(p.encode("utf-8")).hexdigest()
    return _splice_cache_root() / f"{h}.json"


def _load_splice_cache(path: str) -> Optional[List[float]]:
    cache_path = _splice_cache_path(path)
    if not cache_path.exists():
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    try:
        mtime, size = _keyframe_cache_meta(path)
    except Exception:
        return None
    if float(data.get("mtime", -1)) != float(mtime):
        return None
    if int(data.get("size", -1)) != int(size):
        return None
    if int(data.get("version", 0)) != 1:
        return None
    pts = data.get("points", [])
    if not isinstance(pts, list):
        return None
    out: List[float] = []
    for v in pts:
        try:
            out.append(float(v))
        except Exception:
            continue
    return out if out else None


def _save_splice_cache(path: str, points: List[float]) -> None:
    cache_path = _splice_cache_path(path)
    try:
        mtime, size = _keyframe_cache_meta(path)
    except Exception:
        return
    payload = {
        "path": str(Path(path).resolve()),
        "mtime": float(mtime),
        "size": int(size),
        "points": [float(v) for v in points],
        "version": 1,
    }
    tmp = cache_path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, cache_path)
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


def _merge_intervals(intervals: Optional[List[Tuple[float, float]]]) -> List[Tuple[float, float]]:
    if not intervals:
        return []
    ranges: List[Tuple[float, float]] = []
    for start, end in intervals:
        try:
            s = float(start)
            e = float(end)
        except Exception:
            continue
        if e <= s:
            continue
        ranges.append((max(0.0, s), max(0.0, e)))
    if not ranges:
        return []
    ranges.sort()
    merged: List[Tuple[float, float]] = [ranges[0]]
    for s, e in ranges[1:]:
        ls, le = merged[-1]
        if s <= le + 1e-3:
            merged[-1] = (ls, max(le, e))
        else:
            merged.append((s, e))
    return merged


def _filter_points_by_intervals(points: List[float], intervals: Optional[List[Tuple[float, float]]]) -> List[float]:
    merged = _merge_intervals(intervals)
    if not merged:
        return points
    out: List[float] = []
    j = 0
    for t in points:
        while j < len(merged) and t > merged[j][1] + 1e-6:
            j += 1
        if j >= len(merged):
            break
        if t + 1e-6 < merged[j][0]:
            continue
        out.append(t)
    return out


def _iter_nals_annexb(data: bytes) -> Iterator[bytes]:
    i = 0
    n = len(data)

    def _find_start(pos: int):
        while pos + 3 < n:
            if data[pos] == 0 and data[pos + 1] == 0 and data[pos + 2] == 1:
                return pos, 3
            if (
                pos + 4 < n
                and data[pos] == 0
                and data[pos + 1] == 0
                and data[pos + 2] == 0
                and data[pos + 3] == 1
            ):
                return pos, 4
            pos += 1
        return None

    first = _find_start(0)
    if not first:
        return
    start, sc_len = first
    i = start + sc_len
    while True:
        nxt = _find_start(i)
        if not nxt:
            nal = data[i:]
            if nal:
                yield nal
            break
        j, sc2 = nxt
        nal = data[i:j]
        if nal:
            yield nal
        i = j + sc2


def _iter_nals_length_prefixed(data: bytes, nal_len_size: int) -> Iterator[bytes]:
    i = 0
    n = len(data)
    while i + nal_len_size <= n:
        nal_len = int.from_bytes(data[i:i + nal_len_size], "big")
        i += nal_len_size
        if nal_len <= 0 or i + nal_len > n:
            break
        yield data[i:i + nal_len]
        i += nal_len


def _h264_is_idr(nal: bytes) -> bool:
    if not nal:
        return False
    return (nal[0] & 0x1F) == 5


def _hevc_is_idr_or_bla(nal: bytes) -> bool:
    if len(nal) < 2:
        return False
    nal_type = (nal[0] >> 1) & 0x3F
    # BLA + IDR; intentionally excludes CRA for stricter splice points.
    return nal_type in (16, 17, 18, 19, 20)


def _scan_splice_safe_pts_pyav(
    path: str,
    intervals: Optional[List[Tuple[float, float]]] = None,
) -> List[float]:
    if av is None:
        return []
    try:
        container = av.open(path)
    except Exception:
        return []

    out: List[float] = []
    try:
        vstream = next((s for s in container.streams if s.type == "video"), None)
        if vstream is None:
            return []

        codec = str(getattr(vstream.codec_context, "name", "") or "").lower()
        if codec not in ("h264", "hevc", "h265"):
            return []

        extradata = bytes(getattr(vstream.codec_context, "extradata", b"") or b"")
        nal_len_size: Optional[int] = None
        if codec == "h264" and len(extradata) >= 7 and extradata[0] == 1:
            nal_len_size = (extradata[4] & 0x03) + 1
        elif codec in ("hevc", "h265") and len(extradata) >= 22 and extradata[0] in (0, 1):
            nal_len_size = (extradata[21] & 0x03) + 1

        merged = _merge_intervals(intervals)
        merged_starts = [s for s, _ in merged] if merged else []

        def _in_intervals(ts: float) -> bool:
            if not merged:
                return True
            idx = bisect.bisect_right(merged_starts, ts) - 1
            if idx < 0:
                return False
            s, e = merged[idx]
            return (ts + 1e-6) >= s and ts <= (e + 1e-6)

        for packet in container.demux(vstream):
            pts_val = packet.pts if packet.pts is not None else packet.dts
            if pts_val is None:
                continue
            try:
                pts_time = float(pts_val * packet.time_base)
            except Exception:
                continue

            if not _in_intervals(pts_time):
                continue

            data = bytes(packet)
            if not data:
                continue

            is_annexb = (
                (b"\x00\x00\x01" in data[:64]) or
                (b"\x00\x00\x00\x01" in data[:64])
            )
            if is_annexb:
                nals = _iter_nals_annexb(data)
            elif nal_len_size:
                nals = _iter_nals_length_prefixed(data, nal_len_size)
            else:
                # AVCC/HVCC packets are often 4-byte length prefixed in MP4.
                nals = _iter_nals_length_prefixed(data, 4)

            if codec == "h264":
                ok = any(_h264_is_idr(nal) for nal in nals)
            else:
                ok = any(_hevc_is_idr_or_bla(nal) for nal in nals)
            if ok:
                out.append(pts_time)
    except Exception:
        return []
    finally:
        try:
            container.close()
        except Exception:
            pass

    # normalize/unique
    uniq = sorted(set(round(float(t), 6) for t in out if t is not None and t >= 0.0))
    return [float(t) for t in uniq]


def scan_splice_safe_pts(
    path: str,
    use_cache: bool = True,
    intervals: Optional[List[Tuple[float, float]]] = None,
) -> List[float]:
    """
    Return splice-safe packet timestamps (seconds):
      - H.264: IDR (NAL type 5)
      - HEVC: BLA/IDR (NAL types 16,17,18,19,20)
    Uses PyAV packet parsing; empty list when unavailable/unsupported.
    """
    lock = _get_splice_lock(path)
    with lock:
        cached = _load_splice_cache(path) if use_cache else None

        if cached:
            return _filter_points_by_intervals(cached, intervals)

        pts = _scan_splice_safe_pts_pyav(path, intervals=intervals)
        if pts and use_cache and not intervals:
            _save_splice_cache(path, pts)
        return pts


# -----------------------------
# Keyframe cache (ffprobe)
# -----------------------------
_KEYFRAME_LOCK_GUARD = threading.Lock()
_KEYFRAME_LOCKS: dict[str, threading.Lock] = {}


def _get_keyframe_lock(path: str) -> threading.Lock:
    key = str(_keyframe_cache_path(path))
    with _KEYFRAME_LOCK_GUARD:
        lock = _KEYFRAME_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _KEYFRAME_LOCKS[key] = lock
        return lock


def _keyframe_cache_root() -> Path:
    d = runtime_cache_root() / "keyframes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _keyframe_cache_path(path: str) -> Path:
    p = str(Path(path).resolve())
    if os.name == "nt":
        p = p.lower()
    h = hashlib.sha1(p.encode("utf-8")).hexdigest()
    return _keyframe_cache_root() / f"{h}.json"


def _keyframe_cache_meta(path: str) -> tuple[float, int]:
    st = os.stat(path)
    return float(st.st_mtime), int(st.st_size)


def _load_keyframe_cache(path: str) -> Optional[List[float]]:
    cache_path = _keyframe_cache_path(path)
    if not cache_path.exists():
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    try:
        mtime, size = _keyframe_cache_meta(path)
    except Exception:
        return None
    if float(data.get("mtime", -1)) != float(mtime):
        return None
    if int(data.get("size", -1)) != int(size):
        return None
    kf = data.get("keyframes", [])
    if not isinstance(kf, list):
        return None
    out: List[float] = []
    for v in kf:
        try:
            out.append(float(v))
        except Exception:
            continue
    return out if out else None


def _save_keyframe_cache(path: str, keyframes: List[float]) -> None:
    cache_path = _keyframe_cache_path(path)
    try:
        mtime, size = _keyframe_cache_meta(path)
    except Exception:
        return
    payload = {
        "path": str(Path(path).resolve()),
        "mtime": float(mtime),
        "size": int(size),
        "keyframes": [float(v) for v in keyframes],
        "version": 1,
    }
    tmp = cache_path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, cache_path)
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


def ffprobe_keyframes(
    path: str,
    use_cache: bool = True,
    intervals: Optional[List[Tuple[float, float]]] = None,
    timeout_s: float | None = None,
) -> List[float]:
    """
    Return sorted keyframe timestamps (seconds) for the first video stream.
    Uses a local cache keyed by path + mtime + size.
    """
    lock = _get_keyframe_lock(path)
    with lock:
        if use_cache:
            cached = _load_keyframe_cache(path)
            if cached:
                return cached

        _, ffprobe = ensure_ffmpeg()

    def _build_intervals_arg() -> Optional[str]:
        if not intervals:
            return None
        ranges: List[Tuple[float, float]] = []
        for start, end in intervals:
            try:
                s = float(start)
                e = float(end)
            except Exception:
                continue
            if e <= s:
                continue
            ranges.append((max(0.0, s), max(0.0, e)))
        if not ranges:
            return None
        # Merge overlapping ranges to keep the command short
        ranges.sort()
        merged: List[Tuple[float, float]] = [ranges[0]]
        for s, e in ranges[1:]:
            last_s, last_e = merged[-1]
            if s <= last_e + 1e-3:
                merged[-1] = (last_s, max(last_e, e))
            else:
                merged.append((s, e))
        parts: List[str] = []
        for s, e in merged:
            dur = max(0.0, e - s)
            parts.append(f"{s:.3f}%+{dur:.3f}")
        return ",".join(parts)

    def _run_probe(skip_nokey: bool) -> List[dict]:
        cmd = [
            ffprobe, "-v", "error",
            "-select_streams", "v:0",
            "-show_frames",
            "-show_entries", "frame=pkt_pts_time,best_effort_timestamp_time,key_frame",
            "-of", "json",
            path,
        ]
        if skip_nokey:
            cmd.insert(3, "nokey")
            cmd.insert(3, "-skip_frame")
        intervals_arg = _build_intervals_arg()
        if intervals_arg:
            cmd.insert(1, intervals_arg)
            cmd.insert(1, "-read_intervals")
        p = run_cmd(cmd, timeout_s=timeout_s)
        if p.returncode != 0:
            raise RuntimeError(p.stderr.strip() or "ffprobe failed (keyframes)")
        if not p.stdout:
            return []
        try:
            data = json.loads(p.stdout)
        except Exception:
            return []
        return data.get("frames") or []

    def _parse_frames(frames: List[dict]) -> List[float]:
        out: List[float] = []
        for fr in frames:
            try:
                if str(fr.get("key_frame", "0")) not in ("1", "True", "true"):
                    continue
                ts = fr.get("pkt_pts_time", None)
                if ts is None or ts == "N/A":
                    ts = fr.get("best_effort_timestamp_time", None)
                if ts is None or ts == "N/A":
                    continue
                out.append(float(ts))
            except Exception:
                continue
        return out

    frames = _run_probe(skip_nokey=True)
    out = _parse_frames(frames)
    if not out:
        # Some sources don't flag keyframes with skip_nokey; retry without skipping.
        frames = _run_probe(skip_nokey=False)
        out = _parse_frames(frames)

    out = sorted(set(out))
    if out and use_cache and not intervals:
        _save_keyframe_cache(path, out)
    return out


def clear_keyframe_cache() -> int:
    """
    Remove persisted ffprobe keyframe cache files.
    Returns number of removed filesystem entries.
    """
    removed = 0
    roots = [
        _project_root() / "cache" / "keyframes",
        _project_root() / "cache" / "splice_points",
    ]
    for root in roots:
        if not root.exists():
            continue
        try:
            for entry in list(root.iterdir()):
                try:
                    if entry.is_dir():
                        shutil.rmtree(entry, ignore_errors=True)
                    else:
                        entry.unlink(missing_ok=True)
                    removed += 1
                except Exception:
                    pass
        except Exception:
            pass

    with _KEYFRAME_LOCK_GUARD:
        _KEYFRAME_LOCKS.clear()
    with _SPLICE_LOCK_GUARD:
        _SPLICE_LOCKS.clear()
    return removed
