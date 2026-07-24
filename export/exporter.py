import os
import tempfile
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import json
import hashlib
import bisect
import math

from PySide6.QtCore import QObject, Signal, Slot

from analysis.cut_engine import Segment, merge_overlaps
from utils.ffmpeg import (
    has_audio_stream,
    has_video_stream,
    ffprobe_keyframes,
    ffprobe_duration_seconds,
    scan_splice_safe_pts,
)
from utils.subprocess_utils import popen_no_window, run_no_window
from ui.render_filters import (
    build_filter_complex,
    build_filter_complex_multi,
    build_filter_complex_video_only,
)


def _env(name: str, default=None):
    """Read AUTO_CUTTER_* env vars and fall back to legacy RECUT_* names."""
    val = os.environ.get(name)
    if val is not None:
        return val
    if name.startswith("AUTO_CUTTER_"):
        legacy = "RECUT_" + name[len("AUTO_CUTTER_") :]
        legacy_val = os.environ.get(legacy)
        if legacy_val is not None:
            return legacy_val
    return default


def _safe_int(s: str):
    s = s.strip()
    if not s or s.upper() == "N/A":
        return None
    try:
        return int(s)
    except ValueError:
        return None


class _ExportCancelled(Exception):
    pass


class _SmartHybridFallback(Exception):
    pass


def _parse_out_time_hms(s: str):
    # "HH:MM:SS.micro" -> seconds (float)
    s = s.strip()
    if not s or s.upper() == "N/A":
        return None
    try:
        hh, mm, ss = s.split(":")
        return int(hh) * 3600.0 + int(mm) * 60.0 + float(ss)
    except Exception:
        return None


class ExportWorker(QObject):
    progress = Signal(int, str)  # percent, text
    finished = Signal()
    error = Signal(str)
    detail = Signal(str)

    def __init__(
        self,
        ffmpeg_path: str,
        input_path: str,
        output_path: str,
        keeps: list[Segment],
        codec: str = "h264_amf",
        use_hwaccel: bool = False,  # se vuoi provare hw decode, metti True (vedi nota sotto)
        export_method: str = "filter_concat",  # "filter_concat" | "chunked_parallel" | "smart_render" | "smart_hybrid"
        parallel_workers: int = 2,  # quanti ffmpeg contemporanei per chunked_parallel
        chunk_count: int = 0,  # 0 = Auto
        audio_gain_db: float = 0.0,

        # --- NEW (backward-compatible) ---
        normalize_lufs: bool = False,
        lufs_target: float = -14.0,     # YouTube-ish
        lufs_true_peak: float = -1.5,   # dBTP (safe)
        lufs_lra: float = 11.0,         # Loudness Range target
        apply_limiter: bool = True,
        limiter_limit: float = 0.98,    # ~ -0.17 dBFS peak limiter
        cut_hq_enabled: bool = True,
        cut_hq_max_seconds: float = 6.0,
        input_paths: list[str] | None = None,
        segments: list[dict] | None = None,
        smart_render_pad: float = 0.5,
    ):
        super().__init__()
        self.ffmpeg_path = ffmpeg_path
        self.input_path = input_path
        self.input_paths = [input_path]
        if input_paths:
            self.input_paths = [str(p) for p in input_paths if p]
        self.output_path = output_path
        self.keeps = keeps
        self.codec = codec
        self.use_hwaccel = use_hwaccel
        self.export_method = export_method

        # clamp di sicurezza (ma alto): l'utente può spingere, noi evitiamo numeri assurdi
        try:
            pw = int(parallel_workers)
        except Exception:
            pw = 0
        self._parallel_auto = False
        if pw <= 0:
            self._parallel_auto = True
            pw = self._auto_parallel_workers()
        self.parallel_workers = max(1, min(pw, 32))

        try:
            cc = int(chunk_count)
        except Exception:
            cc = 0
        self.chunk_count = max(0, min(cc, 200))

        self.audio_gain_db = float(audio_gain_db)

        # NEW
        self.normalize_lufs = bool(normalize_lufs)
        self.lufs_target = float(lufs_target)
        self.lufs_true_peak = float(lufs_true_peak)
        self.lufs_lra = float(lufs_lra)
        self.apply_limiter = bool(apply_limiter)
        self.limiter_limit = float(limiter_limit)
        self.cut_hq_enabled = bool(cut_hq_enabled)
        try:
            self.cut_hq_max_seconds = float(cut_hq_max_seconds)
        except Exception:
            self.cut_hq_max_seconds = 6.0
        self.segments = list(segments or [])
        try:
            self.smart_render_pad = max(0.0, float(smart_render_pad))
        except Exception:
            self.smart_render_pad = 0.5

        # Debug logging
        self.debug = _env("AUTO_CUTTER_EXPORT_DEBUG", "1").strip() not in ("0", "false", "False", "")
        self.debug_full = _env("AUTO_CUTTER_EXPORT_DEBUG_FULL", "0").strip() in ("1", "true", "True")
        self._cancelled = False
        self._proc_lock = threading.Lock()
        self._active_procs: set[subprocess.Popen] = set()
        self._chunk_times: list[float] = []
        self._export_start_ts: float | None = None
        self._export_method_used: str | None = None
        self._summary_keeps_count: int | None = None
        self._summary_keeps_total: float | None = None
        self._summary_segments_count: int | None = None
        self._summary_segments_total: float | None = None
        self._summary_input_duration: float | None = None
        self._ffmpeg_threads_override: int | None = None
        self._hwaccel_forced: bool | None = None
        self._hwaccel_state_lock = threading.Lock()
        self._hwaccel_disabled_logged: bool = False
        self._hwaccel_filtergraph_logged: bool = False
        self._tuner_prefer_macro: bool = False
        self._tuner_disk_tier: str | None = None
        self._tuner_ran: bool = False
        self._chunk_cache_index: dict[str, dict] | None = None
        self._chunk_cache_dirty: bool = False
        self._chunk_cache_lock = threading.Lock()
        self._chunk_cache_max_bytes: int | None = None
        self._input_sig_cache: dict[str, dict] = {}
        self._ffmpeg_version_cache: str | None = None
        self._perf_stats_lock = threading.Lock()
        self._render_media_seconds: float = 0.0
        self._render_wall_seconds: float = 0.0
        self._reuse_chunk_hits: int = 0
        self._reuse_chunk_media_seconds: float = 0.0
        self._reuse_hybrid_hits: int = 0
        self._reuse_hybrid_media_seconds: float = 0.0

    @staticmethod
    def _chunk_cache_dir_static() -> Path:
        custom = _env("AUTO_CUTTER_CHUNK_CACHE_DIR", "").strip()
        if custom:
            p = Path(custom)
        else:
            if os.name == "nt":
                base = _env("LOCALAPPDATA", "").strip()
                if base:
                    p = Path(base) / "Auto Cutter" / "chunk_cache"
                else:
                    p = Path.home() / ".auto_cutter" / "chunk_cache"
            else:
                base = _env("XDG_CACHE_HOME", "").strip()
                if base:
                    p = Path(base) / "auto_cutter" / "chunk_cache"
                else:
                    p = Path.home() / ".cache" / "auto_cutter" / "chunk_cache"
        return p

    @staticmethod
    def clear_persistent_chunk_cache() -> int:
        """
        Remove all persisted chunk-cache files.
        Returns number of removed filesystem entries.
        """
        cache_dir = ExportWorker._chunk_cache_dir_static()
        if not cache_dir.exists():
            return 0
        removed = 0
        try:
            for entry in list(cache_dir.iterdir()):
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
        return removed

    def _auto_parallel_workers(self) -> int:
        budget = self._thread_budget()
        if self._uses_gpu_encoder():
            # GPU encode: choose a safe default based on CPU budget and GPU cap.
            # Tuner may further refine this value at runtime.
            cap = self._gpu_max_workers_cap()
            guess = max(1, min(cap, int(math.ceil(budget / 8.0))))
            return self._cap_parallel_workers(guess, context="auto_default")

        # CPU encode: clamp(ceil(budget/4), 1, 8)
        guess = max(1, min(8, int(math.ceil(budget / 4.0))))
        return self._cap_parallel_workers(guess, context="auto_default")

    def _log(self, msg: str) -> None:
        if self.debug:
            print(f"[export] {msg}", flush=True)
        try:
            self.detail.emit(str(msg))
        except Exception:
            pass

    def _fmt_cmd(self, cmd: list[str]) -> str:
        try:
            return " ".join([f"\"{c}\"" if " " in c or "\t" in c else c for c in cmd])
        except Exception:
            return str(cmd)

    def _uses_gpu_encoder(self) -> bool:
        c = (self.codec or "").lower()
        return any(tag in c for tag in ("_amf", "_nvenc", "_qsv", "videotoolbox", "vaapi"))

    def _gpu_max_workers_cap(self) -> int:
        """
        Global safety cap for parallel workers with GPU encoders.
        Default is architecture-aware; user can override with AUTO_CUTTER_GPU_MAX_WORKERS.
        """
        env = _env("AUTO_CUTTER_GPU_MAX_WORKERS", "").strip()
        if env:
            try:
                return max(1, min(int(env), 16))
            except Exception:
                pass

        budget = self._thread_budget()
        if budget >= 24:
            cap = 4
        elif budget >= 16:
            cap = 3
        elif budget >= 8:
            cap = 2
        else:
            cap = 1

        # On slow storage many concurrent workers hurt more than help.
        if self._storage_tier() == "hdd":
            cap = min(cap, 2)
        return int(max(1, cap))

    def _hwaccel_args(self) -> list[str]:
        if self._hwaccel_forced is False:
            return []
        if not (self.use_hwaccel or self._uses_gpu_encoder() or self._hwaccel_forced is True):
            return []
        if os.name == "nt":
            return ["-hwaccel", "d3d11va", "-hwaccel_output_format", "d3d11"]
        return ["-hwaccel", "auto"]

    def _hwaccel_args_for_filtergraph(self) -> list[str]:
        """
        Decode HW with d3d11va + CPU filtergraph is fragile on Windows and often stalls/fails.
        For filter_complex paths we prefer stable CPU decode.
        """
        args = self._hwaccel_args()
        if not args:
            return []
        if os.name == "nt":
            if not self._hwaccel_filtergraph_logged:
                self._hwaccel_filtergraph_logged = True
                self._log("hwaccel_filtergraph_disabled reason=d3d11va_with_filtergraph")
            return []
        return args

    def _cap_parallel_workers(self, workers: int, context: str = "") -> int:
        w = max(1, int(workers or 1))
        budget = self._thread_budget()

        # Keep at least ~2 CPU threads per worker as a global safety floor.
        max_by_threads = max(1, int(budget // 2))
        cap = max(1, min(8, max_by_threads))

        # Additional GPU-specific cap.
        if self._uses_gpu_encoder():
            cap = min(cap, self._gpu_max_workers_cap())

        # Slow storage suffers from excessive parallelism.
        if self._storage_tier() == "hdd":
            cap = min(cap, 2)

        # Low available RAM: reduce parallel workers to avoid paging/OOM on laptops.
        avail_ram_gb = self._available_ram_gb()
        if avail_ram_gb is not None:
            if avail_ram_gb < 2.5:
                cap = min(cap, 1)
            elif avail_ram_gb < 4.0:
                cap = min(cap, 2)
            elif avail_ram_gb < 6.0:
                cap = min(cap, 3)

        if w > cap:
            reason_parts = ["system_cap"]
            if self._uses_gpu_encoder():
                reason_parts.append("gpu_encoder")
            if self._storage_tier() == "hdd":
                reason_parts.append("hdd")
            if avail_ram_gb is not None and avail_ram_gb < 6.0:
                reason_parts.append("low_ram")
            reason = "+".join(reason_parts)
            if context:
                self._log(
                    f"workers_capped context={context} requested={w} capped={cap} "
                    f"reason={reason} budget={budget} avail_ram_gb="
                    f"{(f'{avail_ram_gb:.2f}' if avail_ram_gb is not None else 'n/a')}"
                )
            else:
                self._log(
                    f"workers_capped requested={w} capped={cap} reason={reason}"
                )
            w = cap
        return max(1, w)

    def _thread_budget(self) -> int:
        cpu = os.cpu_count() or 4
        return max(2, int(cpu * 0.75))

    def _available_ram_gb(self) -> float | None:
        # Best-effort; used only for adaptive tuning, never as a hard requirement.
        try:
            if os.name == "nt":
                import ctypes

                class MEMORYSTATUSEX(ctypes.Structure):
                    _fields_ = [
                        ("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                    ]

                stat = MEMORYSTATUSEX()
                stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
                if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                    return float(stat.ullAvailPhys) / float(1024**3)
        except Exception:
            pass
        try:
            if hasattr(os, "sysconf"):
                page = int(os.sysconf("SC_PAGE_SIZE"))
                avail_pages = int(os.sysconf("SC_AVPHYS_PAGES"))
                if page > 0 and avail_pages > 0:
                    return float(page * avail_pages) / float(1024**3)
        except Exception:
            pass
        return None

    def _threads_per_process(self, workers: int) -> int:
        budget = self._thread_budget()
        return max(1, int(budget // max(1, workers)))

    def _input_thread_queue_size(self) -> int:
        try:
            q = int(str(_env("AUTO_CUTTER_THREAD_QUEUE_SIZE", "2048")).strip() or "2048")
        except Exception:
            q = 2048
        # Keep bounded to avoid excessive input buffering memory on long exports.
        return max(128, min(q, 8192))

    def _ffmpeg_thread_args(self) -> list[str]:
        t = self._ffmpeg_threads_override
        if not t or t <= 0:
            return ["-threads", "0"]
        filt = max(1, int(t // 2))
        return [
            "-threads", str(t),
            "-filter_threads", str(filt),
            "-filter_complex_threads", str(filt),
        ]

    def _storage_tier(self) -> str:
        tuner_tier = getattr(self, "_tuner_disk_tier", None)
        if tuner_tier in ("ssd", "hdd"):
            return str(tuner_tier)
        tier = _env("AUTO_CUTTER_STORAGE_TIER", "").strip().lower()
        if tier in ("ssd", "nvme"):
            return "ssd"
        if tier in ("hdd", "spinning"):
            return "hdd"
        return "ssd"

    def _smart_max_pieces(self, keeps_count: int, total_kept: float, segments_mode: bool = False) -> int:
        env = _env("AUTO_CUTTER_MAX_PIECES", "").strip()
        if env:
            try:
                v = int(env)
                if v > 0:
                    return v
            except Exception:
                pass

        base = max(2000, int(keeps_count * 10))
        tier = self._storage_tier()
        if tier == "ssd":
            base = max(base, 3000, int(keeps_count * 15))
        else:
            base = max(base, 1500, int(keeps_count * 8))

        # scale gently with total duration (avoid excessive values)
        try:
            base = max(base, int(total_kept / 2.0))
        except Exception:
            pass

        if segments_mode:
            base = max(500, int(base * 0.7))

        return max(500, min(base, 20000))

    def _chunk_cache_enabled(self) -> bool:
        v = _env("AUTO_CUTTER_CHUNK_CACHE", "1").strip().lower()
        return v not in ("0", "false", "no", "off")

    def _chunk_cache_dir(self) -> Path:
        p = self._chunk_cache_dir_static()
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _chunk_cache_index_path(self) -> Path:
        return self._chunk_cache_dir() / "index.json"

    def _chunk_cache_limit_bytes(self) -> int:
        if self._chunk_cache_max_bytes is not None:
            return self._chunk_cache_max_bytes
        env = _env("AUTO_CUTTER_CHUNK_CACHE_MAX_GB", "").strip()
        gb = 20.0
        if env:
            try:
                gb = max(1.0, float(env))
            except Exception:
                gb = 20.0
        self._chunk_cache_max_bytes = int(gb * 1024.0 * 1024.0 * 1024.0)
        return self._chunk_cache_max_bytes

    def _chunk_cache_load_index(self) -> dict[str, dict]:
        with self._chunk_cache_lock:
            if self._chunk_cache_index is not None:
                return self._chunk_cache_index
            idx: dict[str, dict] = {}
            p = self._chunk_cache_index_path()
            try:
                if p.exists():
                    raw = json.loads(p.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        for k, v in raw.items():
                            if isinstance(k, str) and isinstance(v, dict):
                                idx[k] = dict(v)
            except Exception:
                idx = {}
            self._chunk_cache_index = idx
            self._chunk_cache_dirty = False
            return idx

    def _chunk_cache_key_path(self, key: str, ext: str) -> str:
        return str(self._chunk_cache_dir() / f"{key}.{ext}")

    def _chunk_cache_lookup(self, key: str, ext: str) -> str | None:
        if not self._chunk_cache_enabled() or not key:
            return None
        idx = self._chunk_cache_load_index()
        now = time.time()
        with self._chunk_cache_lock:
            ent = idx.get(key)
            cand = None
            if isinstance(ent, dict):
                cand = str(ent.get("path") or "").strip() or self._chunk_cache_key_path(key, ext)
            else:
                cand = self._chunk_cache_key_path(key, ext)
            if not cand or not os.path.exists(cand):
                idx.pop(key, None)
                self._chunk_cache_dirty = True
                return None
            try:
                size = int(os.path.getsize(cand))
            except Exception:
                size = 0
            idx[key] = {
                "path": str(cand),
                "ext": str(ext),
                "size": int(max(0, size)),
                "last_used": float(now),
            }
            self._chunk_cache_dirty = True
            return str(cand)

    def _chunk_cache_store(self, key: str, ext: str, render_out: str) -> str:
        if not key or not render_out:
            return render_out
        target = self._chunk_cache_key_path(key, ext)
        if self._chunk_cache_enabled():
            try:
                if os.path.abspath(render_out) != os.path.abspath(target):
                    if os.path.exists(target):
                        try:
                            os.remove(render_out)
                        except Exception:
                            pass
                    else:
                        shutil.move(render_out, target)
                out = target
            except Exception:
                out = render_out
        else:
            out = render_out

        idx = self._chunk_cache_load_index() if self._chunk_cache_enabled() else {}
        if self._chunk_cache_enabled():
            now = time.time()
            with self._chunk_cache_lock:
                try:
                    size = int(os.path.getsize(out))
                except Exception:
                    size = 0
                idx[key] = {
                    "path": str(out),
                    "ext": str(ext),
                    "size": int(max(0, size)),
                    "last_used": float(now),
                }
                self._chunk_cache_dirty = True
        return out

    def _chunk_cache_flush(self) -> None:
        if not self._chunk_cache_enabled():
            return
        idx = self._chunk_cache_load_index()
        with self._chunk_cache_lock:
            if not self._chunk_cache_dirty:
                return
            # prune missing files first
            for k in list(idx.keys()):
                p = str((idx.get(k) or {}).get("path") or "")
                if not p or not os.path.exists(p):
                    idx.pop(k, None)

            # compute total and evict LRU if needed
            total = 0
            rows: list[tuple[str, float, int, str]] = []
            for k, v in list(idx.items()):
                p = str(v.get("path") or "")
                if not p or not os.path.exists(p):
                    idx.pop(k, None)
                    continue
                try:
                    sz = int(os.path.getsize(p))
                except Exception:
                    sz = int(v.get("size") or 0)
                lu = float(v.get("last_used") or 0.0)
                total += max(0, sz)
                rows.append((k, lu, max(0, sz), p))
                v["size"] = int(max(0, sz))

            limit = self._chunk_cache_limit_bytes()
            if total > limit:
                rows.sort(key=lambda it: (float(it[1]), str(it[0])))
                for k, _lu, sz, p in rows:
                    if total <= limit:
                        break
                    try:
                        os.remove(p)
                    except Exception:
                        pass
                    idx.pop(k, None)
                    total -= int(max(0, sz))

            p = self._chunk_cache_index_path()
            tmp = p.with_suffix(".tmp")
            try:
                tmp.write_text(json.dumps(idx, ensure_ascii=True, separators=(",", ":")), encoding="utf-8")
                os.replace(tmp, p)
                self._chunk_cache_dirty = False
            except Exception:
                try:
                    if tmp.exists():
                        tmp.unlink()
                except Exception:
                    pass

    def _ffmpeg_version_tag(self) -> str:
        if self._ffmpeg_version_cache is not None:
            return self._ffmpeg_version_cache
        try:
            p = run_no_window(
                [self.ffmpeg_path, "-version"],
                capture_output=True,
                text=True,
                timeout=3.0,
            )
            line = ""
            if p.returncode == 0:
                line = (p.stdout or "").splitlines()[0].strip()
            self._ffmpeg_version_cache = line or str(self.ffmpeg_path)
        except Exception:
            self._ffmpeg_version_cache = str(self.ffmpeg_path)
        return self._ffmpeg_version_cache

    def _source_signature(self, path: str) -> dict:
        p = str(path or "")
        if not p:
            return {"path": "", "size": 0, "mtime_ns": 0}
        if p in self._input_sig_cache:
            return dict(self._input_sig_cache[p])
        sig = {"path": os.path.abspath(p), "size": 0, "mtime_ns": 0}
        try:
            st = os.stat(p)
            sig["size"] = int(st.st_size)
            sig["mtime_ns"] = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
        except Exception:
            pass
        self._input_sig_cache[p] = dict(sig)
        return sig

    @staticmethod
    def _time_quantum(fps: float = 0.0) -> float:
        try:
            f = float(fps)
        except Exception:
            f = 0.0
        if f > 1.0:
            return max(1e-6, 1.0 / f)
        return 0.001

    def _cut_boundary_epsilon(self, fps: float = 0.0) -> float:
        """
        Small half-open boundary epsilon used to avoid duplicate boundary frame/packet.
        Priority:
          1) AUTO_CUTTER_CUT_EPS (seconds) if set (>0)
          2) 0.25 / fps when fps is known
          3) 0.004s fallback
        """
        try:
            raw = str(_env("AUTO_CUTTER_CUT_EPS", "")).strip()
        except Exception:
            raw = ""

        eps = 0.0
        if raw:
            try:
                eps = float(raw)
            except Exception:
                eps = 0.0
        else:
            try:
                f = float(fps)
            except Exception:
                f = 0.0
            eps = (0.25 / f) if f > 1.0 else 0.004

        if not math.isfinite(eps):
            eps = 0.0
        return max(0.0, min(0.050, float(eps)))

    def _copy_seek_epsilon(self, fps: float = 0.0) -> float:
        """
        Tiny forward shift for -ss in stream-copy mode to avoid falling just before
        the intended boundary due timestamp rounding.
        Priority:
          1) AUTO_CUTTER_COPY_SEEK_EPS (seconds) if set (>0)
          2) 0.10 / fps when fps is known
          3) 0.002s fallback
        """
        try:
            raw = str(_env("AUTO_CUTTER_COPY_SEEK_EPS", "")).strip()
        except Exception:
            raw = ""

        eps = 0.0
        if raw:
            try:
                eps = float(raw)
            except Exception:
                eps = 0.0
        else:
            try:
                f = float(fps)
            except Exception:
                f = 0.0
            eps = (0.10 / f) if f > 1.0 else 0.002

        if not math.isfinite(eps):
            eps = 0.0
        return max(0.0, min(0.020, float(eps)))

    @staticmethod
    def _snap_tick(t: float, quantum: float) -> int:
        q = max(1e-9, float(quantum))
        return int(round(float(t) / q))

    def _chunk_signature_keeps(
        self,
        keeps: list[Segment],
        force_fps: float,
        container: str,
        apply_audio_filters: bool | None,
        video_only: bool,
        src_path: str | None = None,
    ) -> str:
        q = self._time_quantum(force_fps)
        src = str(src_path or self.input_path or "")
        audio_chain = ""
        audio_mode = "none"
        if not video_only:
            if apply_audio_filters is None:
                audio_mode = "gain_only" if abs(self.audio_gain_db) > 1e-6 else "plain"
                if abs(self.audio_gain_db) > 1e-6:
                    audio_chain = self._build_audio_filter_chain(include_legacy_gain_limiter=True)
            elif apply_audio_filters:
                audio_mode = "filtered"
                audio_chain = self._build_audio_filter_chain(include_legacy_gain_limiter=True)
            else:
                audio_mode = "plain"
        payload = {
            "v": 5,
            "kind": "keeps_chunk",
            "source": self._source_signature(src),
            "keeps": [[self._snap_tick(s.start, q), self._snap_tick(s.end, q)] for s in keeps],
            "codec": str(self.codec),
            "video_args": self._video_args(),
            "cut_hq_policy": self._cut_hq_policy(),
            "fps": round(float(force_fps or 0.0), 6),
            "container": str(container),
            "video_only": bool(video_only),
            "audio_mode": audio_mode,
            "audio_chain": str(audio_chain),
            "ffmpeg": self._ffmpeg_version_tag(),
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _chunk_signature_segments(
        self,
        segs: list[dict],
        force_fps: float,
        container: str,
        apply_audio_filters: bool,
    ) -> str:
        q = self._time_quantum(force_fps)
        seg_rows: list[list[int]] = []
        for s in segs:
            try:
                v_idx = int(s.get("v_idx", -1))
            except Exception:
                v_idx = -1
            try:
                a_idx = int(s.get("a_idx", -1))
            except Exception:
                a_idx = -1
            try:
                v_in = float(s.get("v_in", 0.0) or 0.0)
                v_out = float(s.get("v_out", 0.0) or 0.0)
                a_in = float(s.get("a_in", 0.0) or 0.0)
                a_out = float(s.get("a_out", 0.0) or 0.0)
                dur = float(s.get("duration", 0.0) or 0.0)
            except Exception:
                continue
            seg_rows.append(
                [
                    v_idx,
                    a_idx,
                    self._snap_tick(v_in, q),
                    self._snap_tick(v_out, q),
                    self._snap_tick(a_in, q),
                    self._snap_tick(a_out, q),
                    self._snap_tick(dur, q),
                ]
            )

        audio_chain = self._build_audio_filter_chain(include_legacy_gain_limiter=True) if apply_audio_filters else ""
        payload = {
            "v": 5,
            "kind": "segments_chunk",
            "sources": [self._source_signature(p) for p in self.input_paths],
            "segments": seg_rows,
            "codec": str(self.codec),
            "video_args": self._video_args(),
            "cut_hq_policy": self._cut_hq_policy(),
            "fps": round(float(force_fps or 0.0), 6),
            "container": str(container),
            "audio_filters": bool(apply_audio_filters),
            "audio_chain": str(audio_chain),
            "ffmpeg": self._ffmpeg_version_tag(),
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _keyframe_intervals_for_keeps(
        self,
        keeps_sorted: list[Segment],
        pad: float,
        max_intervals: int = 200,
    ) -> list[tuple[float, float]]:
        if not keeps_sorted:
            return []
        if len(keeps_sorted) > max_intervals:
            return []
        ranges: list[tuple[float, float]] = []
        for s in keeps_sorted:
            a = max(0.0, float(s.start) - float(pad))
            b = max(a, float(s.end) + float(pad))
            ranges.append((a, b))
        ranges.sort()
        merged: list[tuple[float, float]] = [ranges[0]]
        for s, e in ranges[1:]:
            last_s, last_e = merged[-1]
            if s <= last_e + 1e-3:
                merged[-1] = (last_s, max(last_e, e))
            else:
                merged.append((s, e))
        return merged

    def _ts_video_bsf(self, src_path: str) -> list[str]:
        codec = self._probe_video_codec(src_path)
        if codec in ("h264", "avc1"):
            return ["-bsf:v", "h264_mp4toannexb"]
        if codec in ("hevc", "h265"):
            return ["-bsf:v", "hevc_mp4toannexb"]
        return []

    def _codec_family(self) -> str:
        c = (self.codec or "").lower()
        if "h264" in c or "x264" in c or "avc" in c:
            return "H.264"
        if "hevc" in c or "h265" in c or "x265" in c:
            return "H.265/HEVC"
        if "av1" in c:
            return "AV1"
        return c or "unknown"

    def _log_export_summary(self) -> None:
        if self._export_start_ts is None:
            return
        elapsed = max(0.0, time.time() - self._export_start_ts)
        method_used = self._export_method_used or self.export_method
        codec_label = f"{self.codec} ({self._codec_family()})"

        parts = [
            f"export_summary method={method_used}",
            f"codec={codec_label}",
        ]
        if self._summary_input_duration is not None:
            parts.append(f"input_duration={self._summary_input_duration:.1f}s")
        if self._summary_keeps_count is not None and self._summary_keeps_total is not None:
            parts.append(
                f"keeps={self._summary_keeps_count} keeps_total={self._summary_keeps_total:.3f}s"
            )
        elif self._summary_segments_count is not None and self._summary_segments_total is not None:
            parts.append(
                f"segments={self._summary_segments_count} segments_total={self._summary_segments_total:.3f}s"
            )
        parts.append(f"elapsed={elapsed:.2f}s")

        if self._chunk_times:
            avg_chunk = sum(self._chunk_times) / max(1, len(self._chunk_times))
            parts.append(f"avg_chunk_time={avg_chunk:.2f}s chunks={len(self._chunk_times)}")

        with self._perf_stats_lock:
            render_media = float(self._render_media_seconds)
            render_wall = float(self._render_wall_seconds)
            chunk_hits = int(self._reuse_chunk_hits)
            chunk_media = float(self._reuse_chunk_media_seconds)
            hybrid_hits = int(self._reuse_hybrid_hits)
            hybrid_media = float(self._reuse_hybrid_media_seconds)
        reuse_hits = chunk_hits + hybrid_hits
        reuse_media = chunk_media + hybrid_media
        if render_media > 1e-6 and render_wall > 1e-6:
            render_speed = render_media / render_wall
            parts.append(
                f"render_work={render_media:.3f}s render_wall={render_wall:.2f}s "
                f"render_speed={render_speed:.2f}x"
            )
            if chunk_media > 1e-6:
                saved_time = chunk_media / max(1e-6, render_speed)
                parts.append(
                    f"chunk_dedup_hits={chunk_hits} chunk_dedup_media={chunk_media:.3f}s "
                    f"chunk_dedup_est_saved={saved_time:.2f}s"
                )
            if hybrid_media > 1e-6:
                saved_time = hybrid_media / max(1e-6, render_speed)
                parts.append(
                    f"hybrid_reuse_hits={hybrid_hits} hybrid_reuse_media={hybrid_media:.3f}s "
                    f"hybrid_reuse_est_saved={saved_time:.2f}s"
                )
            if reuse_media > 1e-6:
                saved_time = reuse_media / max(1e-6, render_speed)
                parts.append(
                    f"reuse_total_hits={reuse_hits} reuse_total_media={reuse_media:.3f}s "
                    f"reuse_total_est_saved={saved_time:.2f}s"
                )
        elif reuse_media > 1e-6:
            if chunk_media > 1e-6:
                parts.append(f"chunk_dedup_hits={chunk_hits} chunk_dedup_media={chunk_media:.3f}s")
            if hybrid_media > 1e-6:
                parts.append(f"hybrid_reuse_hits={hybrid_hits} hybrid_reuse_media={hybrid_media:.3f}s")
            parts.append(f"reuse_total_hits={reuse_hits} reuse_total_media={reuse_media:.3f}s")

        self._log(" ".join(parts))

    def _record_render_work(self, media_seconds: float, wall_seconds: float) -> None:
        try:
            m = max(0.0, float(media_seconds))
            w = max(0.0, float(wall_seconds))
        except Exception:
            return
        if m <= 0.0 or w <= 0.0:
            return
        with self._perf_stats_lock:
            self._render_media_seconds += m
            self._render_wall_seconds += w

    def _record_reuse(self, hits: int, media_seconds: float, category: str = "chunk_dedup") -> None:
        try:
            h = max(0, int(hits))
            m = max(0.0, float(media_seconds))
        except Exception:
            return
        if h <= 0 or m <= 0.0:
            return
        with self._perf_stats_lock:
            if category == "hybrid_piece_pack_reuse":
                self._reuse_hybrid_hits += h
                self._reuse_hybrid_media_seconds += m
            else:
                self._reuse_chunk_hits += h
                self._reuse_chunk_media_seconds += m

    def _strip_hwaccel_args(self, cmd: list[str]) -> list[str]:
        if "-hwaccel" not in cmd and "-hwaccel_output_format" not in cmd:
            return list(cmd)
        out: list[str] = []
        skip_next = False
        i = 0
        while i < len(cmd):
            if skip_next:
                skip_next = False
                i += 1
                continue
            tok = cmd[i]
            if tok in ("-hwaccel", "-hwaccel_output_format", "-hwaccel_device"):
                skip_next = True
                i += 1
                continue
            out.append(tok)
            i += 1
        return out

    def _disable_hwaccel_for_export(self, reason: str = "") -> None:
        with self._hwaccel_state_lock:
            self._hwaccel_forced = False
            self.use_hwaccel = False
            if not self._hwaccel_disabled_logged:
                self._hwaccel_disabled_logged = True
                if reason:
                    self._log(f"hwaccel_disabled_for_export reason={reason}")
                else:
                    self._log("hwaccel_disabled_for_export")

    def _auto_tune(self, src_path: str, keeps_count: int, total_kept: float) -> None:
        # Allow disabling tuner via env
        if _env("AUTO_CUTTER_TUNER", "1").strip() in ("0", "false", "False"):
            return
        # Only auto-tune when user left workers on Auto (unless forced)
        if not self._parallel_auto and _env("AUTO_CUTTER_TUNER_FORCE", "0").strip() not in ("1", "true", "True"):
            return
        if self._tuner_ran:
            return
        self._tuner_ran = True

        t_start = time.perf_counter()
        self._log("tuner: start")

        # Disk write test
        disk_mb_s = 0.0
        try:
            tmp_dir = tempfile.gettempdir()
            test_path = os.path.join(tmp_dir, f"auto_cutter_tuner_{os.getpid()}.bin")
            size_mb = 32
            payload = b"\x00" * (1024 * 1024)
            t0 = time.perf_counter()
            with open(test_path, "wb") as f:
                for _ in range(size_mb):
                    f.write(payload)
            t1 = time.perf_counter()
            try:
                os.remove(test_path)
            except Exception:
                pass
            dt = max(1e-6, t1 - t0)
            disk_mb_s = float(size_mb / dt)
        except Exception:
            disk_mb_s = 0.0

        disk_tier = "ssd" if disk_mb_s >= 120 else "hdd"
        self._tuner_disk_tier = disk_tier

        # Short ffmpeg test window
        try:
            dur = float(ffprobe_duration_seconds(src_path) or 0.0)
        except Exception:
            dur = 0.0
        seek = 0.0
        if dur > 30.0:
            seek = min(60.0, dur * 0.1)
        test_dur = 1.5

        def _run_test(cmd: list[str]) -> float | None:
            try:
                t0 = time.perf_counter()
                p = run_no_window(cmd, capture_output=True, text=True)
                if p.returncode != 0:
                    return None
                return max(1e-6, time.perf_counter() - t0)
            except Exception:
                return None

        # Decode CPU test
        decode_cpu = None
        try:
            cmd = [
                self.ffmpeg_path, "-hide_banner", "-v", "error", "-y",
                "-ss", f"{seek:.3f}", "-t", f"{test_dur:.3f}",
                "-i", src_path,
                "-map", "0:v:0", "-an", "-sn", "-dn",
                "-threads", "1",
                "-f", "null", "-",
            ]
            decode_cpu = _run_test(cmd)
        except Exception:
            decode_cpu = None

        # Decode HW test
        decode_hw = None
        hwaccel_ok = False
        try:
            hw_args = self._hwaccel_args()
            if hw_args:
                cmd = [
                    self.ffmpeg_path, "-hide_banner", "-v", "error", "-y",
                    *hw_args,
                    "-ss", f"{seek:.3f}", "-t", f"{test_dur:.3f}",
                    "-i", src_path,
                    "-map", "0:v:0", "-an", "-sn", "-dn",
                    "-threads", "1",
                    "-f", "null", "-",
                ]
                decode_hw = _run_test(cmd)
                if decode_hw is not None:
                    hwaccel_ok = True
        except Exception:
            decode_hw = None

        # Decide hwaccel
        try:
            hw_auto_ratio = float(_env("AUTO_CUTTER_HWACCEL_AUTO_RATIO", "1.35").strip() or "1.35")
        except Exception:
            hw_auto_ratio = 1.35
        hw_auto_ratio = max(1.0, min(2.0, float(hw_auto_ratio)))
        if hwaccel_ok:
            if decode_cpu is None or (decode_hw is not None and decode_hw <= (decode_cpu * hw_auto_ratio)):
                self._hwaccel_forced = True
            else:
                self._hwaccel_forced = False
        else:
            self._hwaccel_forced = False

        # Encode test (current codec)
        encode_time = None
        try:
            cmd = [
                self.ffmpeg_path, "-hide_banner", "-v", "error", "-y",
                "-ss", f"{seek:.3f}", "-t", f"{test_dur:.3f}",
                "-i", src_path,
                "-map", "0:v:0", "-an", "-sn", "-dn",
                "-threads", "1",
                "-c:v", self.codec,
                "-f", "null", "-",
            ]
            encode_time = _run_test(cmd)
        except Exception:
            encode_time = None

        budget = self._thread_budget()
        workers = self.parallel_workers
        gpu_tune_info = ""
        if self._uses_gpu_encoder():
            # GPU encode: estimate a reasonable worker count from measured speed,
            # CPU thread budget and storage tier.
            try:
                min_thr = int(_env("AUTO_CUTTER_GPU_MIN_THREADS_PER_WORKER", "4").strip() or "4")
            except Exception:
                min_thr = 4
            min_thr = max(2, min(min_thr, 8))

            max_gpu = self._gpu_max_workers_cap()
            max_by_threads = max(1, int(budget // max(1, min_thr)))
            max_allowed = max(1, min(max_gpu, max_by_threads))

            enc_ratio = (float(encode_time) / test_dur) if encode_time is not None else None
            dec_ratio = (float(decode_cpu) / test_dur) if decode_cpu is not None else None

            if enc_ratio is None:
                workers_target = 2
            elif enc_ratio <= 0.45:
                workers_target = 4
            elif enc_ratio <= 0.75:
                workers_target = 3
            elif enc_ratio <= 1.10:
                workers_target = 2
            else:
                workers_target = 1

            # If decode path is very fast, allow one extra worker when possible.
            if dec_ratio is not None and dec_ratio <= 0.35:
                workers_target = max(workers_target, 3)

            if disk_tier == "hdd":
                workers_target = min(workers_target, 2)

            workers = max(1, min(max_allowed, workers_target))
            try:
                gpu_tune_info = (
                    f"gpu_auto cap={max_gpu} max_by_threads={max_by_threads} "
                    f"allowed={max_allowed} target={workers_target} min_thr={min_thr}"
                )
            except Exception:
                gpu_tune_info = ""
        else:
            workers = max(1, min(8, int(math.ceil(budget / 4.0))))

        if disk_tier == "hdd":
            workers = max(1, min(workers, 2))

        # Low-RAM safety: on long/dense jobs, reduce worker parallelism to avoid
        # memory pressure and OS paging spikes on mid/low-end laptops.
        avail_ram_gb = self._available_ram_gb()
        if avail_ram_gb is not None:
            heavy_job = bool(
                keeps_count >= 120
                or total_kept >= 1200.0
                or dur >= 5400.0
            )
            if heavy_job and avail_ram_gb < 2.5:
                workers = 1
            elif heavy_job and avail_ram_gb < 4.0:
                workers = min(workers, 2)

        workers = self._cap_parallel_workers(workers, context="auto_tuner")
        threads_per = max(1, int(budget // max(1, workers)))

        self.parallel_workers = int(workers)
        self._ffmpeg_threads_override = int(threads_per)

        avg_keep = (total_kept / keeps_count) if keeps_count > 0 else 0.0
        prefer_macro = keeps_count > 150 and avg_keep > 0 and avg_keep < 6.0
        if decode_cpu and decode_hw and decode_hw < decode_cpu:
            # If HW decode is significantly faster, prefer chunked_parallel
            prefer_macro = False
        self._tuner_prefer_macro = bool(prefer_macro)
        avail_ram_txt = f"{avail_ram_gb:.2f}" if avail_ram_gb is not None else "n/a"

        self._log(
            "tuner: "
            f"disk={disk_mb_s:.1f}MB/s tier={disk_tier} "
            f"decode_cpu={decode_cpu if decode_cpu is not None else 'n/a'} "
            f"decode_hw={decode_hw if decode_hw is not None else 'n/a'} "
            f"encode={encode_time if encode_time is not None else 'n/a'} "
            f"workers={self.parallel_workers} threads_per={self._ffmpeg_threads_override} "
            f"avail_ram_gb={avail_ram_txt} "
            f"hwaccel={'on' if self._hwaccel_forced else 'off'} "
            f"hw_auto_ratio={hw_auto_ratio:.2f} "
            f"{gpu_tune_info + ' ' if gpu_tune_info else ''}"
            f"prefer_macro={'yes' if self._tuner_prefer_macro else 'no'} "
            f"elapsed={time.perf_counter() - t_start:.2f}s"
        )

    def _validate_output(self, path: str) -> bool:
        try:
            metrics = self._probe_duration_metrics(path)
            if not metrics:
                return False
            dur = float(metrics.get("effective_duration", 0.0) or 0.0)
            has_video = bool(metrics.get("has_video", False))
            return dur > 0.5 and has_video
        except Exception:
            return False

    def _probe_duration_metrics(self, path: str) -> dict:
        """
        Probe duration using both container and stream timestamps.
        Some MP4 files are "playable" but have broken stream timestamp spans
        (e.g. format duration looks OK while a player reads a much longer
        timeline from stream PTS/DTS). We track both.
        """
        ffprobe = self._find_ffprobe()
        if not ffprobe:
            return {}

        def _f(v) -> float | None:
            try:
                if v is None:
                    return None
                s = str(v).strip()
                if not s or s.upper() == "N/A":
                    return None
                return float(s)
            except Exception:
                return None

        cmd = [
            ffprobe, "-v", "error",
            "-show_entries", "format=duration,start_time",
            "-show_streams",
            "-of", "json",
            path,
        ]
        p = run_no_window(cmd, capture_output=True, text=True)
        if p.returncode != 0 or not p.stdout:
            return {}

        data = json.loads(p.stdout)
        fmt = data.get("format") or {}
        streams = data.get("streams") or []

        format_duration = float(_f(fmt.get("duration")) or 0.0)
        format_start = _f(fmt.get("start_time"))

        has_video = False
        max_stream_duration = 0.0
        starts: list[float] = []
        ends: list[float] = []

        for s in streams:
            if not isinstance(s, dict):
                continue
            ctype = str(s.get("codec_type", "") or "")
            if ctype == "video":
                has_video = True
            if ctype not in ("video", "audio"):
                continue
            sd = _f(s.get("duration"))
            ss = _f(s.get("start_time"))
            if sd is not None and sd > 0.0:
                max_stream_duration = max(max_stream_duration, float(sd))
            if ss is not None:
                starts.append(float(ss))
                if sd is not None and sd > 0.0:
                    ends.append(float(ss + sd))

        span_duration = 0.0
        if starts and ends:
            try:
                span_duration = max(0.0, float(max(ends) - min(starts)))
            except Exception:
                span_duration = 0.0

        effective = max(float(format_duration or 0.0), float(max_stream_duration or 0.0), float(span_duration or 0.0))
        min_stream_start = float(min(starts)) if starts else None
        max_stream_end = float(max(ends)) if ends else None
        return {
            "format_duration": float(format_duration or 0.0),
            "format_start": float(format_start or 0.0) if format_start is not None else None,
            "max_stream_duration": float(max_stream_duration or 0.0),
            "span_duration": float(span_duration or 0.0),
            "effective_duration": float(effective or 0.0),
            "min_stream_start": min_stream_start,
            "max_stream_end": max_stream_end,
            "has_video": bool(has_video),
        }

    @staticmethod
    def _drop_chapters_args() -> list[str]:
        # Source files (e.g. Twitch exports) may contain chapter tables spanning the
        # original full timeline. If copied to a trimmed output, some players (notably
        # Windows Media Player) display the chapter span as total duration even when
        # stream timestamps are correct.
        return ["-map_chapters", "-1"]

    def _log_ts_debug(self, path: str, label: str = "output") -> None:
        """
        Compact ffprobe dump for timestamp issues.
        Logs container metrics + per-stream timing metadata.
        """
        try:
            ffprobe = self._find_ffprobe()
            if not ffprobe:
                self._log(f"ts_debug[{label}] skipped reason=no_ffprobe")
                return
            p_abs = os.path.abspath(str(path))
            if not os.path.exists(p_abs):
                self._log(f"ts_debug[{label}] skipped reason=missing path={p_abs}")
                return

            try:
                mm = self._probe_duration_metrics(p_abs)
            except Exception:
                mm = {}

            self._log(
                f"ts_debug[{label}] path={p_abs} "
                f"effective={float(mm.get('effective_duration', 0.0) or 0.0):.3f}s "
                f"format_dur={float(mm.get('format_duration', 0.0) or 0.0):.3f}s "
                f"span={float(mm.get('span_duration', 0.0) or 0.0):.3f}s "
                f"min_start={float(mm.get('min_stream_start', 0.0) or 0.0):.3f}s "
                f"max_end={float(mm.get('max_stream_end', 0.0) or 0.0):.3f}s"
            )

            cmd = [
                ffprobe, "-v", "error",
                "-show_entries",
                "format=format_name,start_time,duration:stream=index,codec_type,codec_name,start_time,duration,time_base,avg_frame_rate,r_frame_rate,nb_frames",
                "-of", "json",
                p_abs,
            ]
            p = run_no_window(cmd, capture_output=True, text=True)
            if p.returncode != 0:
                self._log(f"ts_debug[{label}] ffprobe_failed rc={p.returncode}")
                return
            data = json.loads(p.stdout or "{}")
            fmt = data.get("format") or {}
            self._log(
                f"ts_debug[{label}] format "
                f"name={str(fmt.get('format_name') or '')} "
                f"start={str(fmt.get('start_time') or 'n/a')} "
                f"duration={str(fmt.get('duration') or 'n/a')}"
            )
            streams = data.get("streams") or []
            for s in streams:
                if not isinstance(s, dict):
                    continue
                ctype = str(s.get("codec_type") or "")
                if ctype not in ("video", "audio"):
                    continue
                self._log(
                    f"ts_debug[{label}] stream "
                    f"idx={int(s.get('index', -1))} "
                    f"type={ctype} "
                    f"codec={str(s.get('codec_name') or '')} "
                    f"tb={str(s.get('time_base') or 'n/a')} "
                    f"start={str(s.get('start_time') or 'n/a')} "
                    f"duration={str(s.get('duration') or 'n/a')} "
                    f"avg_fps={str(s.get('avg_frame_rate') or 'n/a')} "
                    f"r_fps={str(s.get('r_frame_rate') or 'n/a')} "
                    f"nb_frames={str(s.get('nb_frames') or 'n/a')}"
                )
        except Exception as e:
            try:
                self._log(f"ts_debug[{label}] exception={e}")
            except Exception:
                pass


    def _finish_success(self) -> None:
        self._log_export_summary()
        self.finished.emit()

    def cancel(self) -> None:
        self._cancelled = True
        self._terminate_all_procs()

    def _register_proc(self, proc: subprocess.Popen) -> None:
        try:
            with self._proc_lock:
                self._active_procs.add(proc)
        except Exception:
            pass

    def _unregister_proc(self, proc: subprocess.Popen) -> None:
        try:
            with self._proc_lock:
                self._active_procs.discard(proc)
        except Exception:
            pass

    def _terminate_proc(self, proc: subprocess.Popen) -> None:
        try:
            if proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=1.5)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        except Exception:
            pass

    def _terminate_all_procs(self) -> None:
        try:
            with self._proc_lock:
                procs = list(self._active_procs)
            for p in procs:
                self._terminate_proc(p)
        except Exception:
            pass

    def _check_cancelled(self) -> None:
        if self._cancelled:
            raise _ExportCancelled("Export cancelled.")

    def _run_ffmpeg(self, cmd: list[str]) -> tuple[int, str, str]:
        self._check_cancelled()

        def _run_once(cmd_run: list[str]) -> tuple[int, str]:
            proc = popen_no_window(
                cmd_run,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            self._register_proc(proc)
            err_lines = deque(maxlen=200)

            def _drain_stderr() -> None:
                try:
                    assert proc.stderr is not None
                    for line in proc.stderr:
                        if line:
                            err_lines.append(line.rstrip())
                except Exception:
                    pass

            t = threading.Thread(target=_drain_stderr, daemon=True)
            t.start()
            try:
                heartbeat_sec = float(_env("AUTO_CUTTER_FFMPEG_HEARTBEAT_SEC", "5").strip() or "5")
            except Exception:
                heartbeat_sec = 5.0
            if heartbeat_sec < 0.0:
                heartbeat_sec = 0.0
            t_start = time.monotonic()
            t_last_hb = t_start
            out_hint = ""
            try:
                out_hint = str(cmd_run[-1]) if cmd_run else ""
            except Exception:
                out_hint = ""

            try:
                while True:
                    if self._cancelled:
                        self._terminate_proc(proc)
                        raise _ExportCancelled("Export cancelled.")
                    if heartbeat_sec > 0.0:
                        now = time.monotonic()
                        if (now - t_last_hb) >= heartbeat_sec:
                            self._log(
                                f"ffmpeg_wait elapsed={now - t_start:.1f}s "
                                f"target={out_hint}"
                            )
                            t_last_hb = now
                    rc = proc.poll()
                    if rc is not None:
                        try:
                            t.join(timeout=1.0)
                        except Exception:
                            pass
                        return int(rc), "\n".join(err_lines)
                    time.sleep(0.1)
            finally:
                self._unregister_proc(proc)

        rc, err = _run_once(cmd)
        if rc != 0 and ("-hwaccel" in cmd or "-hwaccel_output_format" in cmd):
            cmd_no_hw = self._strip_hwaccel_args(cmd)
            self._log("hwaccel_failed -> retry cpu decode")
            self._disable_hwaccel_for_export("ffmpeg_run_failed")
            rc, err = _run_once(cmd_no_hw)

        return int(rc), "", (err or "")

    def _run_ffmpeg_progress(
        self,
        cmd: list[str],
        on_progress: Callable[[float], None] | None = None,
    ) -> tuple[int, str]:
        self._check_cancelled()

        def _run_once(cmd_run: list[str]) -> tuple[int, str]:
            proc = popen_no_window(
                cmd_run,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            self._register_proc(proc)
            err_lines = deque(maxlen=200)
            line_q = deque()
            q_lock = threading.Lock()
            reader_done = threading.Event()
            out_time = 0.0
            try:
                stall_warn_s = float(
                    _env("AUTO_CUTTER_FFMPEG_PROGRESS_STALL_WARN_SEC", "90").strip() or "90"
                )
            except Exception:
                stall_warn_s = 90.0
            try:
                stall_kill_s = float(
                    _env("AUTO_CUTTER_FFMPEG_PROGRESS_STALL_KILL_SEC", "420").strip() or "420"
                )
            except Exception:
                stall_kill_s = 420.0
            try:
                stall_eps = float(
                    _env("AUTO_CUTTER_FFMPEG_PROGRESS_STALL_EPS", "0.20").strip() or "0.20"
                )
            except Exception:
                stall_eps = 0.20
            stall_warn_s = max(0.0, float(stall_warn_s))
            stall_kill_s = max(0.0, float(stall_kill_s))
            stall_eps = max(0.001, float(stall_eps))
            try:
                heartbeat_sec = float(
                    _env("AUTO_CUTTER_FFMPEG_PROGRESS_HEARTBEAT_SEC", "5").strip() or "5"
                )
            except Exception:
                heartbeat_sec = 5.0
            if heartbeat_sec < 0.0:
                heartbeat_sec = 0.0
            t_start = time.monotonic()
            t_last_hb = t_start
            t_last_activity = t_start
            t_last_progress = t_start
            last_reported_out = 0.0
            stall_warned = False
            out_hint = ""
            try:
                out_hint = str(cmd_run[-1]) if cmd_run else ""
            except Exception:
                out_hint = ""

            def _drain_stderr() -> None:
                nonlocal t_last_activity
                try:
                    assert proc.stderr is not None
                    for raw in proc.stderr:
                        if raw is None:
                            continue
                        line = raw.strip()
                        if not line:
                            continue
                        with q_lock:
                            line_q.append(line)
                            err_lines.append(line)
                        t_last_activity = time.monotonic()
                except Exception:
                    pass
                finally:
                    reader_done.set()

            def _consume_progress() -> None:
                nonlocal out_time, t_last_activity, t_last_progress, last_reported_out, stall_warned
                while True:
                    with q_lock:
                        if not line_q:
                            break
                        line = line_q.popleft()
                    if line.startswith("out_time_ms="):
                        v = _safe_int(line.split("=", 1)[1])
                        if v is not None:
                            out_time = v / 1_000_000.0
                            if out_time >= (last_reported_out + stall_eps):
                                last_reported_out = out_time
                                t_last_progress = time.monotonic()
                                t_last_activity = t_last_progress
                                stall_warned = False
                            if on_progress:
                                on_progress(out_time)
                    elif line.startswith("out_time_us="):
                        v = _safe_int(line.split("=", 1)[1])
                        if v is not None:
                            out_time = v / 1_000_000.0
                            if out_time >= (last_reported_out + stall_eps):
                                last_reported_out = out_time
                                t_last_progress = time.monotonic()
                                t_last_activity = t_last_progress
                                stall_warned = False
                            if on_progress:
                                on_progress(out_time)
                    elif line.startswith("out_time="):
                        v = _parse_out_time_hms(line.split("=", 1)[1])
                        if v is not None:
                            out_time = v
                            if out_time >= (last_reported_out + stall_eps):
                                last_reported_out = out_time
                                t_last_progress = time.monotonic()
                                t_last_activity = t_last_progress
                                stall_warned = False
                            if on_progress:
                                on_progress(out_time)

            t = threading.Thread(target=_drain_stderr, daemon=True)
            t.start()
            try:
                while True:
                    if self._cancelled:
                        self._terminate_proc(proc)
                        raise _ExportCancelled("Export cancelled.")
                    _consume_progress()
                    if heartbeat_sec > 0.0:
                        now = time.monotonic()
                        if (now - t_last_hb) >= heartbeat_sec:
                            self._log(
                                f"ffmpeg_progress_wait elapsed={now - t_start:.1f}s "
                                f"out_time={out_time:.1f}s target={out_hint}"
                            )
                            t_last_hb = now
                    now = time.monotonic()
                    idle_for = max(0.0, now - max(t_last_activity, t_last_progress))
                    if stall_warn_s > 0.0 and (not stall_warned) and idle_for >= stall_warn_s:
                        self._log(
                            f"ffmpeg_stall_warning idle={idle_for:.1f}s out_time={out_time:.1f}s "
                            f"target={out_hint}"
                        )
                        stall_warned = True
                    if stall_kill_s > 0.0 and idle_for >= stall_kill_s:
                        stall_msg = (
                            f"ffmpeg_stall_detected idle={idle_for:.1f}s out_time={out_time:.1f}s "
                            f"target={out_hint}"
                        )
                        self._log(stall_msg + " -> terminate")
                        try:
                            err_lines.append(stall_msg)
                        except Exception:
                            pass
                        self._terminate_proc(proc)
                    rc = proc.poll()
                    if rc is not None:
                        try:
                            t.join(timeout=1.0)
                        except Exception:
                            pass
                        _consume_progress()
                        return int(rc), "\n".join(err_lines)
                    if reader_done.is_set():
                        # Reader ended unexpectedly: keep polling process until completion.
                        pass
                    time.sleep(0.1)
            finally:
                self._unregister_proc(proc)

        rc, err = _run_once(cmd)
        if rc != 0 and ("-hwaccel" in cmd or "-hwaccel_output_format" in cmd):
            cmd_no_hw = self._strip_hwaccel_args(cmd)
            self._log("hwaccel_failed -> retry cpu decode")
            self._disable_hwaccel_for_export("ffmpeg_progress_failed")
            rc, err = _run_once(cmd_no_hw)

        return int(rc), err

    def _summarize_keeps(self, keeps: list[Segment]) -> str:
        if not keeps:
            return "keeps=0"
        durs = [float(s.end - s.start) for s in keeps]
        return (
            f"keeps={len(keeps)} total={sum(durs):.3f}s "
            f"min={min(durs):.3f}s max={max(durs):.3f}s"
        )

    def _summarize_segments_dicts(self, segments: list[dict]) -> str:
        if not segments:
            return "segments=0"
        durs = []
        for s in segments:
            try:
                durs.append(float(s.get("duration", 0.0) or 0.0))
            except Exception:
                pass
        if not durs:
            return "segments=0"
        return (
            f"segments={len(segments)} total={sum(durs):.3f}s "
            f"min={min(durs):.3f}s max={max(durs):.3f}s"
        )

    def _segments_timeline_diagnostics(self, segments: list[dict]) -> dict:
        diag = {
            "count": 0,
            "single_input_idx": None,
            "non_monotonic_jumps": 0,
            "duplicate_exact": 0,
            "unique_exact": 0,
            "top_duplicates": [],
            "preview": [],
        }
        if not segments:
            return diag

        q = 1000.0  # millisecond quantization for stable duplicate keys
        keys_count: dict[tuple, int] = {}
        preview: list[str] = []
        last_v_in: float | None = None
        jumps = 0

        for i, s in enumerate(segments):
            try:
                v_idx = int(s.get("v_idx", -1))
            except Exception:
                v_idx = -1
            try:
                a_idx = int(s.get("a_idx", -1))
            except Exception:
                a_idx = -1
            try:
                v_in = float(s.get("v_in", 0.0) or 0.0)
            except Exception:
                v_in = 0.0
            try:
                v_out = float(s.get("v_out", v_in) or v_in)
            except Exception:
                v_out = v_in
            try:
                a_in = float(s.get("a_in", v_in) or v_in)
            except Exception:
                a_in = v_in
            try:
                a_out = float(s.get("a_out", v_out) or v_out)
            except Exception:
                a_out = v_out
            try:
                d = float(s.get("duration", max(0.0, v_out - v_in)) or 0.0)
            except Exception:
                d = max(0.0, v_out - v_in)

            if last_v_in is not None and (v_in + 1e-6) < last_v_in:
                jumps += 1
            last_v_in = v_in

            key = (
                v_idx,
                a_idx,
                int(round(v_in * q)),
                int(round(v_out * q)),
                int(round(a_in * q)),
                int(round(a_out * q)),
                int(round(max(0.0, d) * q)),
            )
            keys_count[key] = int(keys_count.get(key, 0)) + 1

            if i < 8:
                preview.append(
                    f"#{i+1} src={v_idx} {v_in:.3f}-{v_out:.3f} dur={max(0.0, d):.3f}s"
                )

        duplicate_exact = sum(max(0, c - 1) for c in keys_count.values())
        top_dup = sorted(
            ((k, c) for k, c in keys_count.items() if c > 1),
            key=lambda it: it[1],
            reverse=True,
        )[:3]
        top_dup_str = [
            (
                f"src={k[0]} {k[2] / q:.3f}-{k[3] / q:.3f} "
                f"dur={k[6] / q:.3f}s x{c}"
            )
            for k, c in top_dup
        ]

        idx = self._segments_single_input_idx(segments)
        diag.update(
            {
                "count": len(segments),
                "single_input_idx": idx,
                "non_monotonic_jumps": int(jumps),
                "duplicate_exact": int(duplicate_exact),
                "unique_exact": int(len(keys_count)),
                "top_duplicates": top_dup_str,
                "preview": preview,
            }
        )
        return diag

    def _log_segments_timeline(self, segments: list[dict]) -> None:
        diag = self._segments_timeline_diagnostics(segments)
        self._log(
            "segments_timeline "
            f"count={diag.get('count', 0)} "
            f"single_input_idx={diag.get('single_input_idx') if diag.get('single_input_idx') is not None else 'none'} "
            f"non_monotonic_jumps={diag.get('non_monotonic_jumps', 0)} "
            f"duplicates_exact={diag.get('duplicate_exact', 0)} "
            f"unique_exact={diag.get('unique_exact', 0)}"
        )
        preview = list(diag.get("preview") or [])
        if preview:
            self._log(f"segments_preview {' | '.join(preview)}")
        top_dup = list(diag.get("top_duplicates") or [])
        if top_dup:
            self._log(f"segments_duplicates_top {' | '.join(top_dup)}")

    # -----------------------------
    # Audio filter chain helpers
    # -----------------------------
    def _needs_audio_processing(self) -> bool:
        """
        Quando TRUE, dobbiamo ricodificare audio per applicare filtri.
        Nota: nel metodo filter_concat l'audio è comunque ricodificato, ma qui serve
        per sapere se aggiungere -af / appendere filtri al filtergraph e per il concat finale in chunked.
        """
        want_gain = abs(self.audio_gain_db) > 1e-6
        want_norm = bool(self.normalize_lufs)
        # Limiter alone should not force processing when no gain/normalization is applied.
        want_limiter = bool(self.apply_limiter and (want_gain or want_norm))
        return bool(want_gain or want_norm or want_limiter)

    def _build_audio_filter_chain(self, include_legacy_gain_limiter: bool = True) -> str:
        """
        Costruisce una chain ffmpeg -af / filtergraph audio.
        include_legacy_gain_limiter=True mantiene compatibilità: se gain!=0, applica limiter anche senza LUFS.
        """
        parts: list[str] = []

        if abs(self.audio_gain_db) > 1e-6:
            parts.append(f"volume={self.audio_gain_db:.2f}dB")

        if self.normalize_lufs:
            # Single-pass loudnorm (robusto, semplice). Per uso “pro” potresti fare 2-pass, ma qui è ok.
            # linear=true evita pumping aggressivo; print_format=summary utile se vuoi loggare.
            parts.append(
                f"loudnorm=I={self.lufs_target:.1f}:TP={self.lufs_true_peak:.1f}:LRA={self.lufs_lra:.1f}:"
                f"linear=true:print_format=summary"
            )

        # Limiter: raccomandato se abbiamo aumentato loudness o gain.
        want_limiter = False
        if self.apply_limiter:
            if self.normalize_lufs:
                want_limiter = True
            elif include_legacy_gain_limiter and abs(self.audio_gain_db) > 1e-6:
                want_limiter = True

        if want_limiter:
            parts.append(f"alimiter=limit={self.limiter_limit:.3f}")

        return ",".join(parts)

    def _audio_mode_label(
        self,
        audio_processing: bool,
        audio_copy_ok: bool,
    ) -> str:
        if audio_processing:
            # Filters are applied while rendering segments; output audio is already AAC.
            return "encoded_segments_filtered"
        if audio_copy_ok:
            return "copy"
        # No filters, but source is not MP4-friendly for copy; segments are encoded to AAC.
        return "encoded_segments_aac"

    def _log_audio_mode(
        self,
        context: str,
        audio_processing: bool,
        audio_copy_ok: bool,
        filters_in_segments: bool = False,
    ) -> str:
        mode = self._audio_mode_label(audio_processing, audio_copy_ok)
        self._log(
            f"{context}_audio "
            f"processing={'on' if audio_processing else 'off'} "
            f"copy_ok={'yes' if audio_copy_ok else 'no'} "
            f"filters_in_segments={'yes' if filters_in_segments else 'no'} "
            f"audio_mode={mode}"
        )
        return mode

    # -----------------------------
    # Video encoding profiles
    # -----------------------------
    def _cut_hq_policy(self) -> tuple[bool, float]:
        enabled = bool(getattr(self, "cut_hq_enabled", True))
        max_sec = float(getattr(self, "cut_hq_max_seconds", 6.0))
        # Optional env overrides for power users / CLI runs.
        try:
            raw = _env("AUTO_CUTTER_CUT_HQ", "").strip()
            if raw:
                enabled = raw not in ("0", "false", "False")
        except Exception:
            pass
        try:
            raw = _env("AUTO_CUTTER_CUT_HQ_MAX_SECONDS", "").strip()
            if raw:
                max_sec = float(raw)
        except Exception:
            pass
        return bool(enabled), float(max_sec)

    def _jump_guard_seconds(self) -> float:
        """
        Extra re-encode guard after backward timeline jumps.
        Default 2.0s; set 0 to disable.
        """
        val = 2.0
        try:
            raw = str(_env("AUTO_CUTTER_HYBRID_JUMP_GUARD_SECONDS", "2.0")).strip()
            if raw:
                val = float(raw)
        except Exception:
            val = 2.0
        if not math.isfinite(val):
            val = 0.0
        return max(0.0, min(10.0, float(val)))

    def _hybrid_stitch_guard_seconds(self, segments_context: bool, fps: float = 0.0) -> float:
        """
        Extra re-encode guard for reencode->copy joins in smart_hybrid.
        Helps avoid delayed single-frame decode glitches on bitstream joins.
        Env:
          - AUTO_CUTTER_HYBRID_STITCH_GUARD_SECONDS=auto|<seconds>
            auto:
              - 2.0s on segments timeline
              - lightweight frame-aware guard on keeps timeline
        """
        raw = ""
        try:
            raw = str(_env("AUTO_CUTTER_HYBRID_STITCH_GUARD_SECONDS", "auto")).strip().lower()
        except Exception:
            raw = "auto"
        if raw in ("", "auto"):
            if segments_context:
                val = 2.0
            else:
                # Lightweight default for keeps-only exports: enough to smooth
                # boundary decode glitches without noticeably reducing copy ratio.
                try:
                    f = float(fps)
                except Exception:
                    f = 0.0
                if f > 1.0:
                    # ~4 frames, bounded for predictable speed impact.
                    val = max(0.10, min(0.22, 4.0 / f))
                else:
                    val = 0.12
        else:
            try:
                val = float(raw)
            except Exception:
                val = 0.0
        if not math.isfinite(val):
            val = 0.0
        return max(0.0, min(6.0, float(val)))

    def _hybrid_stitch_guard_min_copy(self) -> float:
        """
        Minimum remaining copy span after applying stitch guard.
        If remaining copy is shorter, convert whole span to re-encode.
        """
        val = 0.25
        try:
            raw = str(_env("AUTO_CUTTER_HYBRID_STITCH_GUARD_MIN_COPY", "0.25")).strip()
            if raw:
                val = float(raw)
        except Exception:
            val = 0.25
        if not math.isfinite(val):
            val = 0.25
        return max(0.0, min(2.0, float(val)))

    def _apply_hybrid_stitch_guard(
        self,
        pieces: list[tuple[str, object]],
        guard_seconds: float,
        min_copy_seconds: float,
        keyframes: list[float] | None = None,
    ) -> tuple[list[tuple[str, object]], int, float]:
        """
        Convert head of copy spans to re-encode when they start right after a re-encode span.
        Returns: (new_pieces, transitions_touched, added_reencode_seconds).
        """
        guard = max(0.0, float(guard_seconds))
        min_copy = max(0.0, float(min_copy_seconds))
        if guard <= 1e-6 or not pieces:
            return pieces, 0, 0.0

        out: list[tuple[str, object]] = []
        prev_kind: str | None = None  # "copy" | "reencode"
        transitions = 0
        added_reencode = 0.0

        for kind, payload in pieces:
            cur_kind = "copy" if kind == "copy" else "reencode"
            if kind == "copy" and prev_kind == "reencode":
                try:
                    s, e = payload  # type: ignore[misc]
                    s = float(s)
                    e = float(e)
                except Exception:
                    out.append((kind, payload))
                    prev_kind = cur_kind
                    continue
                d = max(0.0, e - s)
                if d <= 1e-6:
                    out.append((kind, payload))
                    prev_kind = cur_kind
                    continue

                transitions += 1
                head = min(guard, d)
                tail_start = s + head
                if keyframes:
                    try:
                        i_kf = bisect.bisect_left(keyframes, tail_start - 1e-9)
                        while i_kf < len(keyframes) and float(keyframes[i_kf]) <= (s + 1e-6):
                            i_kf += 1
                        if i_kf < len(keyframes):
                            tail_start = float(keyframes[i_kf])
                    except Exception:
                        pass
                tail_dur = max(0.0, e - tail_start)
                if tail_dur <= min_copy + 1e-6:
                    out.append(("reencode", (s, e)))
                    added_reencode += d
                    prev_kind = "reencode"
                else:
                    out.append(("reencode", (s, tail_start)))
                    out.append(("copy", (tail_start, e)))
                    added_reencode += head
                    prev_kind = "copy"
                continue

            out.append((kind, payload))
            prev_kind = cur_kind

        return out, transitions, added_reencode

    def _hybrid_audio_unify_policy(
        self,
        segments_context: bool,
        non_monotonic_timeline: bool,
    ) -> bool:
        """
        When enabled, smart_hybrid encodes audio in both copy/reencode packs
        to avoid audible boundary stutter from mixed copy+encoded pack joins.
        Env:
          - AUTO_CUTTER_HYBRID_UNIFIED_AUDIO=1|0|auto (default auto)
        """
        raw = ""
        try:
            raw = str(_env("AUTO_CUTTER_HYBRID_UNIFIED_AUDIO", "auto")).strip().lower()
        except Exception:
            raw = "auto"
        if raw in ("1", "true", "yes", "on"):
            return True
        if raw in ("0", "false", "no", "off"):
            return False
        return bool(segments_context and non_monotonic_timeline)

    def _max_conservative_fix_mode(self) -> str:
        """
        Maximum conservative export mode.
        Env:
          AUTO_CUTTER_EXPORT_MAX_CONSERVATIVE=
            0|false|off  -> disabled
            1|true|on    -> enabled with adaptive guard
            force|strict -> always enabled (no adaptive guard)
        Default: 1
        """
        raw = ""
        try:
            raw = str(_env("AUTO_CUTTER_EXPORT_MAX_CONSERVATIVE", "1")).strip().lower()
        except Exception:
            raw = "1"
        if raw in ("0", "false", "no", "off"):
            return "off"
        if raw in ("force", "strict", "always"):
            return "force"
        return "on"

    def _max_conservative_fix_enabled(self) -> bool:
        return self._max_conservative_fix_mode() != "off"

    def _max_conservative_should_apply(
        self,
        context: str,
        item_count: int,
        total_output_s: float,
        input_duration_s: float | None,
    ) -> tuple[bool, str]:
        mode = self._max_conservative_fix_mode()
        if mode == "off":
            return False, "env_off"
        if mode == "force":
            return True, "env_force"

        items = max(0, int(item_count or 0))
        total = max(0.0, float(total_output_s or 0.0))
        inp = max(0.0, float(input_duration_s or 0.0))
        avg = (total / items) if items > 0 else total

        # Large single-pass filtergraphs with many short keeps over very long sources
        # can stall for minutes before making timeline progress.
        if items >= 320:
            return False, f"adaptive_disable_{context}_very_dense"
        if items >= 220 and inp >= 7200.0 and avg <= 15.0:
            return False, f"adaptive_disable_{context}_long_dense_short"
        if items >= 160 and inp >= 10800.0 and avg <= 12.0:
            return False, f"adaptive_disable_{context}_verylong_dense"
        if items >= 260 and total >= 1800.0:
            return False, f"adaptive_disable_{context}_heavy_graph"

        return True, "env_on"

    def _use_cut_hq_for_reencode(self, duration: float) -> bool:
        enabled, max_sec = self._cut_hq_policy()
        if not enabled:
            return False
        if max_sec > 0.0 and float(duration) > float(max_sec):
            return False
        return True

    def _video_args(self, preset: str = "balanced") -> list[str]:
        """
        Profilo HIGH QUALITY (AMF 'quality') con impostazioni che aumentano throughput senza
        abbassare la qualità in modo evidente:
          - GOP più lungo (meno I-frame)
          - B-frames disabilitati (più veloce e più stabile vicino ai tagli)
        """
        preset = (preset or "balanced").strip().lower()
        is_cut_hq = preset == "cut_hq"

        if self.codec == "h264_amf":
            qp_i, qp_p, qp_b = (12, 14, 16) if is_cut_hq else (16, 18, 20)
            return [
                "-c:v", "h264_amf",
                "-quality", ("quality" if is_cut_hq else "balanced"),
                "-rc", "cqp",
                "-qp_i", str(qp_i),
                "-qp_p", str(qp_p),
                "-qp_b", str(qp_b),
                "-g", "240",
                "-bf", "0",
                "-pix_fmt", "yuv420p",
            ]

        if self.codec == "hevc_amf":
            qp_i, qp_p, qp_b = (14, 16, 18) if is_cut_hq else (18, 20, 22)
            return [
                "-c:v", "hevc_amf",
                "-quality", ("quality" if is_cut_hq else "balanced"),
                "-rc", "cqp",
                "-qp_i", str(qp_i),
                "-qp_p", str(qp_p),
                "-qp_b", str(qp_b),
                "-g", "240",
                "-bf", "0",
                "-pix_fmt", "yuv420p",
            ]

        if self.codec == "av1_amf":
            qp_i, qp_p, qp_b = (20, 22, 24) if is_cut_hq else (24, 26, 28)
            return [
                "-c:v", "av1_amf",
                "-quality", ("quality" if is_cut_hq else "balanced"),
                "-rc", "cqp",
                "-qp_i", str(qp_i),
                "-qp_p", str(qp_p),
                "-qp_b", str(qp_b),
                "-g", "240",
                "-bf", "0",
                "-pix_fmt", "yuv420p",
            ]

        # Fallback software
        crf = "14" if is_cut_hq else "16"
        return [
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", crf,
            "-pix_fmt", "yuv420p",
        ]

    # -----------------------------
    # Multi-track layout helper
    # -----------------------------
    def _xstack_layout(self, n: int) -> str:
        """
        Grid layout for xstack using w0/h0 units.
        """
        n = max(1, int(n))
        import math
        cols = int(math.ceil(math.sqrt(n)))
        rows = int(math.ceil(n / cols))
        parts = []
        for i in range(n):
            r = i // cols
            c = i % cols
            x = "0" if c == 0 else "w0" + ("*" + str(c) if c > 1 else "")
            y = "0" if r == 0 else "h0" + ("*" + str(r) if r > 1 else "")
            parts.append(f"{x}_{y}")
        return "|".join(parts)

    # -----------------------------
    # Keeps normalization / chunking
    # -----------------------------
    def _plan_chunking(
        self,
        total_dur: float,
        num_segments: int,
        chunk_count: int,
        workers: int,
        max_segments_default: int = 150,
        max_chunk_seconds_default: float = 600.0,
        label: str = "keeps",
    ) -> tuple[int, int, float]:
        total_dur = max(1e-6, float(total_dur or 0.0))
        num_segments = max(1, int(num_segments or 0))
        workers = max(1, int(workers or 1))

        req_chunks = int(chunk_count) if chunk_count and chunk_count > 0 else 0
        min_chunks = max(1, workers * 2)
        if label == "segments":
            min_chunks = max(min_chunks, workers * 3)

        if req_chunks:
            target_chunks = max(1, req_chunks)
            mode = "user"
        else:
            # Auto: prefer more (smaller) chunks to keep workers busy
            if total_dur >= 3 * 3600:
                target_sec = 300.0
            elif total_dur >= 2 * 3600:
                target_sec = 240.0
            elif total_dur >= 3600:
                target_sec = 180.0
            else:
                target_sec = 120.0
            if label == "segments":
                target_sec = max(60.0, target_sec * 0.75)
            target_chunks = max(min_chunks, int(math.ceil(total_dur / target_sec)))
            mode = "auto"

        # Avoid too tiny chunks
        min_chunk_sec = 30.0 if label == "segments" else 45.0
        max_chunks_by_dur = int(max(1, total_dur / min_chunk_sec))
        target_chunks = max(1, min(target_chunks, max_chunks_by_dur, num_segments))

        avg_seg = max(1, int(math.ceil(num_segments / target_chunks)))
        max_segments = int(min(400, max(40, avg_seg * 2)))
        max_chunk_seconds = max(120.0, (total_dur / target_chunks) * 1.35)

        max_segments = min(max_segments_default, max_segments)
        max_chunk_seconds = min(max_chunk_seconds_default, max_chunk_seconds)

        self._log(
            f"chunk_plan {label} mode={mode} req_chunks={req_chunks} workers={workers} "
            f"total={total_dur:.1f}s segs={num_segments} -> chunks={target_chunks} "
            f"max_segments={max_segments} max_chunk_seconds={max_chunk_seconds:.1f}s"
        )
        return target_chunks, max_segments, max_chunk_seconds

    def _chunk_source_span_limit(
        self,
        max_chunk_seconds: float,
        label: str = "keeps",
    ) -> float:
        raw = _env("AUTO_CUTTER_CHUNK_SOURCE_SPAN_MAX", "").strip()
        if raw:
            try:
                parsed = float(raw)
                if parsed > 0.0:
                    return parsed
            except Exception:
                pass
        sec = max(1.0, float(max_chunk_seconds or 0.0))
        if label == "segments":
            # Segment-based timelines may reference far source ranges; keep spans tighter.
            return max(420.0, min(1800.0, sec * 3.0))
        return max(540.0, min(2400.0, sec * 4.0))

    def _normalize_keeps(self, keeps: list[Segment], merge_gap: float = 0.25, min_dur: float = 0.08) -> list[Segment]:
        ks = sorted((Segment(max(0.0, s.start), max(0.0, s.end)) for s in keeps), key=lambda s: s.start)
        ks = [s for s in ks if s.end > s.start and (s.end - s.start) >= min_dur]
        if not ks:
            return []

        out = [ks[0]]
        for s in ks[1:]:
            last = out[-1]
            if s.start <= last.end + merge_gap:
                out[-1] = Segment(last.start, max(last.end, s.end))
            else:
                out.append(s)
        return out

    def _keyframe_boundary_penalty(
        self,
        ts: float,
        keyframes: list[float] | None,
        keyframe_tol: float,
    ) -> float:
        if not keyframes:
            return 0.0
        i = bisect.bisect_left(keyframes, ts)
        best = float("inf")
        if i < len(keyframes):
            best = min(best, abs(float(keyframes[i]) - float(ts)))
        if i > 0:
            best = min(best, abs(float(ts) - float(keyframes[i - 1])))
        if not math.isfinite(best):
            return 0.0
        if best <= float(keyframe_tol):
            return 0.0
        return float(best)

    def _segment_boundary_time(self, seg: dict, end: bool = True) -> float:
        keys = ("end", "v_out", "a_out") if end else ("start", "v_in", "a_in")
        for k in keys:
            try:
                v = seg.get(k, None)
                if v is not None:
                    return float(v)
            except Exception:
                continue
        try:
            s = float(seg.get("start", 0.0) or 0.0)
            d = max(0.0, float(seg.get("duration", 0.0) or 0.0))
            return s + d if end else s
        except Exception:
            return 0.0

    def _split_segments_by_source_monotonic(
        self,
        segments: list[dict],
        eps: float = 1e-3,
    ) -> list[list[dict]]:
        """
        Split timeline segments into blocks where source in-points are monotonic.
        This avoids long no-output stalls in single-pass filtergraphs when timeline
        order contains backward source jumps (split/duplicate/reorder edits).
        """
        if not segments:
            return []
        def _source_start(seg: dict) -> float:
            # Source monotonicity must be evaluated on source in-points,
            # not timeline "start".
            for k in ("v_in", "a_in", "start"):
                try:
                    v = seg.get(k, None)
                    if v is not None:
                        return float(v)
                except Exception:
                    continue
            return 0.0

        blocks: list[list[dict]] = []
        cur: list[dict] = []
        last_start: float | None = None
        for s in segments:
            st = _source_start(s)
            if cur and last_start is not None and (st + float(eps)) < float(last_start):
                blocks.append(cur)
                cur = []
            cur.append(s)
            last_start = st
        if cur:
            blocks.append(cur)
        return blocks

    def _split_chunks(
        self,
        keeps: list[Segment],
        max_segments: int = 150,
        max_chunk_seconds: float = 600.0,
        chunk_count: int = 0,
        keyframes: list[float] | None = None,
        keyframe_tol: float = 0.050,
        max_source_span: float = 0.0,
    ) -> list[list[Segment]]:
        if not keeps:
            return []

        HARD_MAX_SEGMENTS = 400
        if float(max_source_span or 0.0) <= 0.0:
            max_source_span = self._chunk_source_span_limit(max_chunk_seconds, label="keeps")
        max_source_span = max(30.0, float(max_source_span))
        total_dur = sum((s.end - s.start) for s in keeps)
        total_dur = max(1e-6, total_dur)

        # Modalità: numero chunk desiderato
        if chunk_count and chunk_count > 0:
            chunk_count = max(1, int(chunk_count))
            target = total_dur / chunk_count

            chunks: list[list[Segment]] = []
            cur: list[Segment] = []
            cur_dur = 0.0
            cur_src_start: float | None = None
            cur_src_end = 0.0

            for s in keeps:
                sdur = s.end - s.start
                s_start = float(s.start)
                s_end = float(s.end)
                projected = cur_dur + sdur
                if cur_src_start is None:
                    projected_span = max(0.0, s_end - s_start)
                else:
                    projected_span = max(0.0, max(cur_src_end, s_end) - cur_src_start)
                want_split_for_target = (len(chunks) < chunk_count - 1) and cur and (projected >= target)
                want_split_for_safety = cur and (
                    len(cur) >= HARD_MAX_SEGMENTS
                    or projected > max_chunk_seconds
                    or projected_span > max_source_span
                )

                if want_split_for_target and not want_split_for_safety:
                    before_boundary = float(cur[-1].end)
                    after_boundary = float(s.end)
                    before_score = abs(target - cur_dur) + (
                        2.0 * self._keyframe_boundary_penalty(before_boundary, keyframes, keyframe_tol)
                    )
                    after_score = abs(target - projected) + (
                        2.0 * self._keyframe_boundary_penalty(after_boundary, keyframes, keyframe_tol)
                    )
                    can_split_after = (
                        (len(cur) + 1) <= HARD_MAX_SEGMENTS
                        and projected <= max_chunk_seconds
                        and projected_span <= max_source_span
                    )
                    if can_split_after and after_score < before_score:
                        cur.append(s)
                        cur_dur = projected
                        if cur_src_start is None:
                            cur_src_start = s_start
                            cur_src_end = s_end
                        else:
                            cur_src_end = max(cur_src_end, s_end)
                        chunks.append(cur)
                        cur = []
                        cur_dur = 0.0
                        cur_src_start = None
                        cur_src_end = 0.0
                        continue

                if want_split_for_target or want_split_for_safety:
                    chunks.append(cur)
                    cur = []
                    cur_dur = 0.0
                    cur_src_start = None
                    cur_src_end = 0.0

                cur.append(s)
                cur_dur += sdur
                if cur_src_start is None:
                    cur_src_start = s_start
                    cur_src_end = s_end
                else:
                    cur_src_end = max(cur_src_end, s_end)

            if cur:
                chunks.append(cur)
            return chunks

        # Modalità: auto
        chunks: list[list[Segment]] = []
        cur: list[Segment] = []
        cur_dur = 0.0
        cur_src_start: float | None = None
        cur_src_end = 0.0

        for s in keeps:
            sdur = s.end - s.start
            s_start = float(s.start)
            s_end = float(s.end)
            projected = cur_dur + sdur
            if cur_src_start is None:
                projected_span = max(0.0, s_end - s_start)
            else:
                projected_span = max(0.0, max(cur_src_end, s_end) - cur_src_start)
            if cur and (
                len(cur) >= max_segments
                or projected > max_chunk_seconds
                or projected_span > max_source_span
            ):
                chunks.append(cur)
                cur = []
                cur_dur = 0.0
                cur_src_start = None
                cur_src_end = 0.0
            cur.append(s)
            cur_dur += sdur
            if cur_src_start is None:
                cur_src_start = s_start
                cur_src_end = s_end
            else:
                cur_src_end = max(cur_src_end, s_end)

        if cur:
            chunks.append(cur)

        return chunks

    def _split_segment_dicts(
        self,
        segments: list[dict],
        max_segments: int = 120,
        max_chunk_seconds: float = 480.0,
        chunk_count: int = 0,
        keyframes: list[float] | None = None,
        keyframe_tol: float = 0.050,
        max_source_span: float = 0.0,
    ) -> list[list[dict]]:
        if not segments:
            return []

        hard_max_segments = max(40, int(max_segments))
        if float(max_source_span or 0.0) <= 0.0:
            max_source_span = self._chunk_source_span_limit(max_chunk_seconds, label="segments")
        max_source_span = max(20.0, float(max_source_span))
        segs = [s for s in segments if float(s.get("duration", 0.0) or 0.0) > 1e-6]
        if not segs:
            return []

        total_dur = sum(float(s.get("duration", 0.0) or 0.0) for s in segs)
        total_dur = max(1e-6, total_dur)

        def _src_span(seg: dict) -> tuple[float, float]:
            start = self._segment_boundary_time(seg, end=False)
            end = self._segment_boundary_time(seg, end=True)
            if end < start:
                end = start
            return start, end

        if chunk_count and chunk_count > 0:
            chunk_count = max(1, int(chunk_count))
            target = total_dur / float(chunk_count)

            chunks: list[list[dict]] = []
            cur: list[dict] = []
            cur_dur = 0.0
            cur_src_start: float | None = None
            cur_src_end = 0.0

            for s in segs:
                sdur = float(s.get("duration", 0.0) or 0.0)
                projected = cur_dur + sdur
                s_src_start, s_src_end = _src_span(s)
                if cur_src_start is None:
                    projected_span = max(0.0, s_src_end - s_src_start)
                else:
                    projected_span = max(0.0, max(cur_src_end, s_src_end) - cur_src_start)
                want_split_for_target = (len(chunks) < chunk_count - 1) and bool(cur) and (projected >= target)
                want_split_for_safety = bool(cur) and (
                    len(cur) >= hard_max_segments
                    or projected > max_chunk_seconds
                    or projected_span > max_source_span
                )

                if want_split_for_target and not want_split_for_safety:
                    before_boundary = self._segment_boundary_time(cur[-1], end=True)
                    after_boundary = self._segment_boundary_time(s, end=True)
                    before_score = abs(target - cur_dur) + (
                        2.0 * self._keyframe_boundary_penalty(before_boundary, keyframes, keyframe_tol)
                    )
                    after_score = abs(target - projected) + (
                        2.0 * self._keyframe_boundary_penalty(after_boundary, keyframes, keyframe_tol)
                    )
                    can_split_after = (
                        (len(cur) + 1) <= hard_max_segments
                        and projected <= max_chunk_seconds
                        and projected_span <= max_source_span
                    )
                    if can_split_after and after_score < before_score:
                        cur.append(s)
                        cur_dur = projected
                        if cur_src_start is None:
                            cur_src_start = s_src_start
                            cur_src_end = s_src_end
                        else:
                            cur_src_end = max(cur_src_end, s_src_end)
                        chunks.append(cur)
                        cur = []
                        cur_dur = 0.0
                        cur_src_start = None
                        cur_src_end = 0.0
                        continue

                if want_split_for_target or want_split_for_safety:
                    chunks.append(cur)
                    cur = []
                    cur_dur = 0.0
                    cur_src_start = None
                    cur_src_end = 0.0

                cur.append(s)
                cur_dur += sdur
                if cur_src_start is None:
                    cur_src_start = s_src_start
                    cur_src_end = s_src_end
                else:
                    cur_src_end = max(cur_src_end, s_src_end)

            if cur:
                chunks.append(cur)
            return chunks

        # Auto mode
        chunks: list[list[dict]] = []
        cur: list[dict] = []
        cur_dur = 0.0
        cur_src_start: float | None = None
        cur_src_end = 0.0
        for s in segs:
            sdur = float(s.get("duration", 0.0) or 0.0)
            projected = cur_dur + sdur
            s_src_start, s_src_end = _src_span(s)
            if cur_src_start is None:
                projected_span = max(0.0, s_src_end - s_src_start)
            else:
                projected_span = max(0.0, max(cur_src_end, s_src_end) - cur_src_start)
            if cur and (
                len(cur) >= hard_max_segments
                or projected > max_chunk_seconds
                or projected_span > max_source_span
            ):
                chunks.append(cur)
                cur = []
                cur_dur = 0.0
                cur_src_start = None
                cur_src_end = 0.0
            cur.append(s)
            cur_dur += sdur
            if cur_src_start is None:
                cur_src_start = s_src_start
                cur_src_end = s_src_end
            else:
                cur_src_end = max(cur_src_end, s_src_end)
        if cur:
            chunks.append(cur)
        return chunks

    # -----------------------------
    # Chunk final concat
    # -----------------------------
    def _concat_ts_list(self, files: list[str], temp_files: list[str], out_ts: str) -> str:
        def _q(p: str) -> str:
            p = os.path.abspath(p).replace("\\", "/")
            p = p.replace("'", "''")
            return f"'{p}'"

        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8", newline="\n") as f:
            for cf in files:
                f.write(f"file {_q(cf)}\n")
            list_file = f.name
        temp_files.append(list_file)

        cmd = [
            self.ffmpeg_path, "-hide_banner", "-v", "error", "-y", "-nostdin",
            "-fflags", "+genpts",
            "-f", "concat", "-safe", "0", "-i", list_file,
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-c", "copy",
            *self._drop_chapters_args(),
            "-avoid_negative_ts", "make_zero",
            "-max_interleave_delta", "0",
            "-muxpreload", "0",
            "-muxdelay", "0",
            out_ts,
        ]
        self._log(f"ffmpeg_cmd_concat_ts={self._fmt_cmd(cmd)}")
        rc, _out, err = self._run_ffmpeg(cmd)
        if rc != 0:
            raise RuntimeError((err or "").strip() or "FFmpeg concat (ts) failed")
        try:
            self._log_ts_debug(out_ts, label="concat_ts")
        except Exception:
            pass
        return out_ts

    def _build_cmd_concat_copy_to_ts(
        self,
        ranges: list[tuple[float, float]],
        out_ts: str,
        temp_files: list[str],
        src_path: str,
        audio_copy: bool,
        audio_filters: str = "",
        apply_boundary_epsilon: bool = False,
    ) -> list[str]:
        def _q(path: str) -> str:
            p = os.path.abspath(path).replace("\\", "/")
            p = p.replace("'", "''")
            return f"'{p}'"

        if not ranges:
            raise RuntimeError("concat_copy_to_ts: empty ranges")

        lines = ["ffconcat version 1.0"]
        entry_count = 0
        eps = self._cut_boundary_epsilon() if apply_boundary_epsilon else 0.0
        for s, e in ranges:
            s = float(s)
            e = float(e)
            if e <= s + 1e-9:
                continue
            out_e = e
            if eps > 0.0 and (e - s) > (eps + 1e-9):
                out_e = e - eps
            lines.append(f"file {_q(src_path)}")
            lines.append(f"inpoint {s:.6f}")
            lines.append(f"outpoint {out_e:.6f}")
            entry_count += 1
        if entry_count <= 0:
            raise RuntimeError("concat_copy_to_ts: no valid ranges after epsilon trim")

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".ffconcat", delete=False, encoding="utf-8", newline="\n"
        ) as f:
            f.write("\n".join(lines))
            f.write("\n")
            concat_file = f.name

        temp_files.append(concat_file)
        cmd = [
            self.ffmpeg_path, "-hide_banner", "-v", "error", "-y", "-nostdin",
            "-fflags", "+genpts",
            "-f", "concat", "-safe", "0", "-i", concat_file,
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-dn", "-sn",
            *self._drop_chapters_args(),
            *self._ffmpeg_thread_args(),
            "-c:v", "copy",
        ]

        video_bsf = self._ts_video_bsf(src_path)
        if video_bsf:
            cmd += list(video_bsf)

        if audio_copy:
            cmd += ["-c:a", "copy"]
        else:
            cmd += ["-c:a", "aac", "-b:a", "320k"]
            if audio_filters:
                cmd += ["-af", audio_filters]

        cmd += [
            "-avoid_negative_ts", "make_zero",
            "-max_interleave_delta", "0",
            "-muxpreload", "0",
            "-muxdelay", "0",
            "-f", "mpegts",
            out_ts,
        ]
        self._log(f"ffmpeg_cmd_concat_copy_ts={self._fmt_cmd(cmd)}")
        return cmd

    def _pack_hybrid_pieces(
        self,
        pieces: list[tuple[str, object]],
        pack_target_seconds: float,
        pack_max_pieces: int,
        reencode_max_source_span: float = 0.0,
        reencode_max_source_jump: float = 0.0,
    ) -> list[tuple[str, list[tuple[str, object]]]]:
        packs: list[tuple[str, list[tuple[str, object]]]] = []
        cur_kind: str | None = None
        cur: list[tuple[str, object]] = []
        cur_dur = 0.0
        cur_src_min = 0.0
        cur_src_max = 0.0
        cur_src_last_start = 0.0
        cur_src_has = False

        def _piece_kind(k: str) -> str:
            return "copy" if k == "copy" else "reencode"

        def _piece_dur(k: str, payload: object) -> float:
            if k == "cluster":
                try:
                    ckeeps = payload  # type: ignore[assignment]
                    return sum(max(0.0, s.end - s.start) for s in ckeeps)
                except Exception:
                    return 0.0
            try:
                s, e = payload  # type: ignore[misc]
                return max(0.0, float(e) - float(s))
            except Exception:
                return 0.0

        def _piece_bounds(k: str, payload: object) -> tuple[float, float] | None:
            if k == "cluster":
                try:
                    ckeeps = payload  # type: ignore[assignment]
                    if not ckeeps:
                        return None
                    mn = min(float(s.start) for s in ckeeps)
                    mx = max(float(s.end) for s in ckeeps)
                    return mn, mx
                except Exception:
                    return None
            try:
                s, e = payload  # type: ignore[misc]
                s = float(s)
                e = float(e)
                if e <= s:
                    return None
                return s, e
            except Exception:
                return None

        for kind, payload in pieces:
            pk = _piece_kind(kind)
            pdur = _piece_dur(kind, payload)
            pb = _piece_bounds(kind, payload)
            if not cur:
                cur_kind = pk
                cur = [(kind, payload)]
                cur_dur = pdur
                if pb is not None:
                    cur_src_min, cur_src_max = pb
                    cur_src_last_start = pb[0]
                    cur_src_has = True
                else:
                    cur_src_has = False
                continue

            split_by_source_locality = False
            if (
                pk == "reencode"
                and cur_kind == "reencode"
                and pb is not None
                and cur_src_has
            ):
                p_start, p_end = pb
                proj_min = min(cur_src_min, p_start)
                proj_max = max(cur_src_max, p_end)
                proj_span = max(0.0, proj_max - proj_min)
                src_jump = abs(float(p_start) - float(cur_src_last_start))
                if reencode_max_source_span > 0.0 and proj_span > reencode_max_source_span:
                    split_by_source_locality = True
                if reencode_max_source_jump > 0.0 and src_jump > reencode_max_source_jump:
                    split_by_source_locality = True

            if (
                pk != cur_kind
                or (len(cur) + 1) > pack_max_pieces
                or (cur_dur + pdur) > pack_target_seconds
                or split_by_source_locality
            ):
                packs.append((cur_kind or pk, cur))
                cur_kind = pk
                cur = [(kind, payload)]
                cur_dur = pdur
                if pb is not None:
                    cur_src_min, cur_src_max = pb
                    cur_src_last_start = pb[0]
                    cur_src_has = True
                else:
                    cur_src_has = False
            else:
                cur.append((kind, payload))
                cur_dur += pdur
                if pb is not None:
                    if not cur_src_has:
                        cur_src_min, cur_src_max = pb
                        cur_src_last_start = pb[0]
                        cur_src_has = True
                    else:
                        cur_src_min = min(cur_src_min, pb[0])
                        cur_src_max = max(cur_src_max, pb[1])
                        cur_src_last_start = pb[0]

        if cur:
            packs.append((cur_kind or "reencode", cur))

        return packs

    def _tree_concat_ts(
        self,
        files: list[str],
        temp_files: list[str],
        group_size: int,
        label: str = "tree",
    ) -> str:
        if not files:
            raise RuntimeError("Tree concat: empty file list")
        t0 = time.time()
        self._log(f"tree_concat start label={label} files={len(files)} group_size={group_size}")
        if len(files) <= group_size:
            out_ts = os.path.join(os.path.dirname(files[0]), f"{label}_final.ts")
            temp_files.append(out_ts)
            out = self._concat_ts_list(files, temp_files, out_ts)
            self._log(f"tree_concat done label={label} elapsed={time.time() - t0:.2f}s")
            return out

        packs: list[str] = []
        for idx in range(0, len(files), group_size):
            group = files[idx:idx + group_size]
            out_ts = os.path.join(os.path.dirname(files[0]), f"{label}_pack_{idx//group_size:03d}.ts")
            temp_files.append(out_ts)
            self._concat_ts_list(group, temp_files, out_ts)
            packs.append(out_ts)

        out_ts = os.path.join(os.path.dirname(files[0]), f"{label}_final.ts")
        temp_files.append(out_ts)
        out = self._concat_ts_list(packs, temp_files, out_ts)
        self._log(f"tree_concat done label={label} elapsed={time.time() - t0:.2f}s")
        return out

    def _concat_chunks_copy(
        self,
        chunk_files: list[str],
        temp_files: list[str],
        output_path: str | None = None,
        use_ts: bool = False,
    ) -> None:
        def _q(p: str) -> str:
            p = os.path.abspath(p).replace("\\", "/")
            p = p.replace("'", "''")
            return f"'{p}'"

        # lista concat
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8", newline="\n") as f:
            for cf in chunk_files:
                f.write(f"file {_q(cf)}\n")
            list_file = f.name
        temp_files.append(list_file)
        self._log(f"concat_chunks list_file={list_file} count={len(chunk_files)}")

        audio_processing = self._needs_audio_processing()
        audio_copy_ok = False
        try:
            if not audio_processing and chunk_files:
                audio_copy_ok = self._smart_audio_copy_ok(chunk_files[0])
        except Exception:
            audio_copy_ok = False
        self._log_audio_mode(
            "concat_chunks",
            audio_processing=audio_processing,
            audio_copy_ok=audio_copy_ok,
            filters_in_segments=False,
        )

        t0 = time.time()
        self._log(f"final_concat start count={len(chunk_files)} use_ts={use_ts}")
        if use_ts:
            group_size = 50
            if len(chunk_files) > 1200:
                group_size = 100
            elif len(chunk_files) > 400:
                group_size = 60
            out_ts = self._tree_concat_ts(chunk_files, temp_files, group_size, label="chunks")
            target = output_path or self.output_path
            if not target:
                raise RuntimeError("Final remux target path is empty")
            t_remux = time.time()
            remux_cmd = [
                self.ffmpeg_path, "-hide_banner", "-v", "error", "-y", "-nostdin",
                "-fflags", "+genpts",
                "-i", out_ts,
                "-map", "0:v:0",
                "-map", "0:a:0?",
                *self._drop_chapters_args(),
            ]
            if not self._needs_audio_processing():
                remux_cmd += ["-c", "copy"]
                try:
                    if has_audio_stream(out_ts) and (self._probe_audio_codec(out_ts) in ("aac", "aac_latm")):
                        remux_cmd += ["-bsf:a", "aac_adtstoasc"]
                except Exception:
                    pass
            else:
                af = self._build_audio_filter_chain(include_legacy_gain_limiter=True)
                async_af = "aresample=async=1:first_pts=0"
                af = f"{af},{async_af}" if af else async_af
                remux_cmd += [
                    "-c:v", "copy",
                    "-c:a", "aac", "-b:a", "320k",
                ]
                if af:
                    remux_cmd += ["-af", af]
            remux_cmd += [
                "-avoid_negative_ts", "make_zero",
                "-max_interleave_delta", "0",
                "-muxpreload", "0",
                "-muxdelay", "0",
                "-movflags", "+faststart",
                "-use_editlist", "0",
                target,
            ]
            self._log(f"ffmpeg_cmd_chunks_remux={self._fmt_cmd(remux_cmd)}")
            rc, _out, err = self._run_ffmpeg(remux_cmd)
            if rc != 0:
                raise RuntimeError((err or "").strip() or "FFmpeg remux failed")
            self._log_ts_debug(target, label="chunks_remux_mp4")
            self._log(f"final_remux done elapsed={time.time() - t_remux:.2f}s")
            self._log(f"final_concat done elapsed={time.time() - t0:.2f}s")
            return

        cmd = [
            self.ffmpeg_path, "-hide_banner", "-v", "error", "-y", "-nostdin",
            "-f", "concat", "-safe", "0", "-i", list_file,
            "-map", "0:v:0",
            "-map", "0:a:0?",   # audio opzionale
        ]

        # Se non serve processing audio -> stream copy totale (massima velocità)
        # Se serve processing audio (gain/lufs) -> video copy + audio re-encode con -af
        if not self._needs_audio_processing():
            cmd += ["-c", "copy"]
        else:
            af = self._build_audio_filter_chain(include_legacy_gain_limiter=True)
            cmd += [
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "320k",
            ]
            if af:
                cmd += ["-af", af]

        cmd += [
            "-movflags", "+faststart",
            "-use_editlist", "0",
            output_path or self.output_path
        ]
        self._log(f"ffmpeg_cmd_concat_chunks={self._fmt_cmd(cmd)}")

        rc, _out, err = self._run_ffmpeg(cmd)
        if rc != 0:
            raise RuntimeError((err or "").strip() or "FFmpeg concat failed")
        self._log(f"final_concat done elapsed={time.time() - t0:.2f}s")

    def _remux_mp4_from_ts(
        self,
        input_ts: str,
        output_mp4: str,
        audio_mode: str,
        src_probe_path: str | None = None,
    ) -> None:
        # Remux policy:
        # - `copy`: copy A/V streams as-is (used when source audio is already AAC).
        # - `encoded_segments_*`: segment stage already encoded audio to AAC -> copy in remux.
        # - `encode_aac*`: force MP4-friendly AAC at remux stage (compat path).
        has_audio = False
        try:
            has_audio = has_audio_stream(input_ts)
        except Exception:
            has_audio = False
        ts_audio_codec = self._probe_audio_codec(input_ts) if has_audio else ""
        probe_src = src_probe_path or (self.input_paths[0] if self.input_paths else self.input_path)
        src_audio_codec = self._probe_audio_codec(probe_src) if probe_src else ""

        copy_modes = {"copy"}
        encoded_in_segments_modes = {
            "encoded_segments_aac",
            "encoded_segments_aac_filtered",
            "encoded_segments_filtered",
            "segment_aac",
            "segment_aac_filtered",
        }
        encode_in_remux_modes = {"encode_aac", "encode_aac_filtered"}

        remux_action = "copy"
        if not has_audio:
            remux_action = "copy_video_only"
        elif audio_mode in copy_modes:
            remux_action = "copy"
        elif audio_mode in encoded_in_segments_modes:
            # Defensive: if segment output isn't AAC for any reason, enforce friendly AAC now.
            remux_action = "copy" if ts_audio_codec in ("aac", "aac_latm") else "force_encode_aac"
        elif audio_mode in encode_in_remux_modes:
            remux_action = "encode_aac"
        else:
            remux_action = "copy" if ts_audio_codec in ("aac", "aac_latm") else "force_encode_aac"

        self._log(
            f"remux_audio_policy mode={audio_mode} action={remux_action} "
            f"ts_audio_codec={ts_audio_codec or 'none'} src_audio_codec={src_audio_codec or 'none'} "
            f"has_audio={'yes' if has_audio else 'no'}"
        )

        cmd = [
            self.ffmpeg_path, "-hide_banner", "-v", "error", "-y", "-nostdin",
            "-fflags", "+genpts",
            "-i", input_ts,
            "-map", "0:v:0",
            *self._drop_chapters_args(),
        ]
        if has_audio:
            cmd += ["-map", "0:a:0?"]

        if remux_action in ("copy", "copy_video_only"):
            cmd += ["-c", "copy"]
            if has_audio and remux_action == "copy" and ts_audio_codec in ("aac", "aac_latm"):
                # AAC in TS may be ADTS-framed; this bitstream filter improves MP4 copy compatibility.
                cmd += ["-bsf:a", "aac_adtstoasc"]
        else:
            cmd += [
                "-c:v", "copy",
                "-c:a", "aac",
                "-profile:a", "aac_low",
                "-b:a", "192k",
                "-ar", "48000",
                "-ac", "2",
            ]
        cmd += [
            "-max_muxing_queue_size", "1024",
            "-max_interleave_delta", "0",
            "-avoid_negative_ts", "make_zero",
            "-muxpreload", "0",
            "-muxdelay", "0",
            "-movflags", "+faststart",
            "-use_editlist", "0",
            output_mp4,
        ]
        self._log(f"ffmpeg_cmd_remux_ts={self._fmt_cmd(cmd)}")
        rc, _out, err = self._run_ffmpeg(cmd)
        if rc == 0:
            self._log_ts_debug(output_mp4, label="remux_ts_mp4")
            return

        err_txt = (err or "").strip()
        err_low = err_txt.lower()
        audio_issue_patterns = (
            "codec not currently supported in container",
            "not compatible with output codec id",
            "tag mp4a",
            "could not find codec parameters",
            "error initializing output stream 0:1",
            "malformed aac",
            "invalid audio",
            "audio",
            "aac",
            "mp4a",
        )
        ts_issue_patterns = (
            "non monotonous dts",
            "non-monotonous dts",
            "invalid, non monotonically increasing dts",
            "invalid, non-monotonically increasing dts",
            "timestamp",
            "pts",
            "dts",
            "av_interleaved_write_frame",
        )
        is_audio_issue = any(pat in err_low for pat in audio_issue_patterns)
        is_ts_issue = any(pat in err_low for pat in ts_issue_patterns)

        retry_reasons: list[str] = []
        if has_audio and remux_action == "copy":
            retry_reasons.append("copy_audio_to_mp4")
        if has_audio and is_audio_issue:
            retry_reasons.append("audio_mux_error")
        if has_audio and is_ts_issue:
            retry_reasons.append("timestamp_error")

        if not retry_reasons:
            raise RuntimeError(err_txt or "FFmpeg remux failed")

        self._log(
            f"remux_failed mode={audio_mode} action={remux_action} "
            f"retry_aac_mp4_friendly reasons={','.join(retry_reasons)}"
        )
        cmd_retry = [
            self.ffmpeg_path, "-hide_banner", "-v", "error", "-y", "-nostdin",
        ]
        if is_ts_issue:
            cmd_retry += ["-fflags", "+genpts"]
        cmd_retry += ["-i", input_ts, "-map", "0:v:0", *self._drop_chapters_args(), "-c:v", "copy"]
        if has_audio:
            cmd_retry += [
                "-map", "0:a:0?",
                "-c:a", "aac",
                "-profile:a", "aac_low",
                "-b:a", "192k",
                "-ar", "48000",
                "-ac", "2",
                "-af", "aresample=async=1:first_pts=0",
                "-shortest",
            ]
        cmd_retry += [
            "-max_muxing_queue_size", "1024",
            "-max_interleave_delta", "0",
            "-avoid_negative_ts", "make_zero",
            "-muxpreload", "0",
            "-muxdelay", "0",
            "-movflags", "+faststart",
            "-use_editlist", "0",
            output_mp4,
        ]
        self._log(f"ffmpeg_cmd_remux_ts_retry={self._fmt_cmd(cmd_retry)}")
        rc2, _out2, err2 = self._run_ffmpeg(cmd_retry)
        if rc2 == 0:
            self._log_ts_debug(output_mp4, label="remux_ts_mp4_retry")
            return
        raise RuntimeError((err2 or "").strip() or err_txt or "FFmpeg remux retry failed")


    # -----------------------------
    # Filter-concat builder (legacy accurate)
    # -----------------------------
    def _build_cmd_filter_concat(
        self,
        keeps_sorted: list[Segment],
        temp_files: list[str],
        force_fps: float = 0.0,
    ) -> tuple[list[str], float]:
        cmd, total = self._build_cmd_filter_concat_to(
            keeps_sorted,
            self.output_path,
            temp_files,
            tmp_dir=None,
            force_fps=force_fps,
            src_path=self.input_path,
            apply_audio_filters=True,
        )
        return cmd, total

    def _build_cmd_filter_concat_multi(
        self,
        keeps_sorted: list[Segment],
        temp_files: list[str],
        force_fps: float = 0.0,
    ) -> tuple[list[str], float]:
        # preseek is unsafe for multi-input concat+mix (different timelines)
        keeps_shifted = keeps_sorted

        total = sum(s.dur for s in keeps_shifted)
        total = max(1e-6, total)

        n_inputs = len(self.input_paths)
        force_async_audio = bool(n_inputs > 1 or self._needs_audio_processing())
        filt = build_filter_complex_multi(
            keeps_shifted,
            n_inputs,
            "",
            fps=force_fps,
            force_audio_async=force_async_audio,
        )
        self._log(
            f"filter_concat_multi_async_aresample={'on' if force_async_audio else 'off'} "
            f"reason={'multi_input_or_processing' if force_async_audio else 'aligned_single_audio'}"
        )

        audio_label = "outa"
        af = self._build_audio_filter_chain(include_legacy_gain_limiter=True)
        audio_processing = bool(af)
        if af:
            filt = filt.rstrip()
            filt += f";[outa]{af}[outa_f]"
            audio_label = "outa_f"
        self._log_audio_mode(
            "filter_concat_multi",
            audio_processing=audio_processing,
            audio_copy_ok=False,
            filters_in_segments=audio_processing,
        )

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8", newline="\n"
        ) as f:
            f.write(filt)
            f.write("\n")
            filter_file = f.name
            temp_files.append(filter_file)

        self._log(
            f"mode=filter_concat_multi {self._summarize_keeps(keeps_sorted)} "
            f"total_kept={total:.3f}s fps={float(force_fps or 0.0):.3f} inputs={n_inputs}"
        )

        cmd = [
            self.ffmpeg_path, "-hide_banner", "-v", "error", "-y", "-nostdin",
            "-thread_queue_size", str(self._input_thread_queue_size()),
        ]
        cmd += self._hwaccel_args_for_filtergraph()

        for path in self.input_paths:
            cmd += ["-i", path]

        cmd += [
            "-filter_complex_script", filter_file,
            "-map", "[outv]", "-map", f"[{audio_label}]",
            *self._ffmpeg_thread_args(),
            *self._video_args(),
            "-c:a", "aac", "-b:a", "320k",
            "-max_muxing_queue_size", "4096",
            "-movflags", "+faststart",
            "-use_editlist", "0",
            "-progress", "pipe:2",
            "-nostats",
            self.output_path
        ]
        return cmd, total

    def _build_cmd_filter_concat_to(
        self,
        keeps_sorted: list[Segment],
        out_path: str,
        temp_files: list[str],
        tmp_dir: str | None = None,
        force_fps: float = 0.0,
        src_path: str | None = None,
        apply_audio_filters: bool | None = None,
        video_only: bool = False,
        container: str = "mp4",
        video_preset: str = "balanced",
    ) -> tuple[list[str], float]:
        preseek_pad = 2.0
        preseek = 0.0
        monotonic_starts = True
        min_start = 0.0
        try:
            last_start = float("-inf")
            min_start = float("inf")
            for s in keeps_sorted:
                cur = float(s.start)
                if cur < min_start:
                    min_start = cur
                if cur + 1e-6 < last_start:
                    monotonic_starts = False
                last_start = cur
        except Exception:
            monotonic_starts = False
            min_start = 0.0

        if keeps_sorted:
            if not math.isfinite(min_start):
                try:
                    min_start = min(float(s.start) for s in keeps_sorted)
                except Exception:
                    min_start = 0.0
            if min_start > preseek_pad:
                preseek = max(0.0, float(min_start) - preseek_pad)
                if not monotonic_starts:
                    self._log(
                        f"filter_concat_preseek=on reason=non_monotonic_min_start "
                        f"min_start={min_start:.3f}s preseek={preseek:.3f}s"
                    )
            elif not monotonic_starts:
                self._log("filter_concat_preseek=off reason=non_monotonic_min_start_near_zero")

        if preseek > 0.0:
            keeps_shifted = [Segment(s.start - preseek, s.end - preseek) for s in keeps_sorted]
            keeps_shifted = [
                Segment(max(0.0, s.start), max(0.0, s.end))
                for s in keeps_shifted
                if s.end > 0.0 and s.end > s.start
            ]
        else:
            keeps_shifted = keeps_sorted

        input_read_limit = 0.0
        try:
            if keeps_shifted:
                max_end = max(float(s.end) for s in keeps_shifted)
                if max_end > 1e-6:
                    # Limit demux/decode to the useful time window for this chunk.
                    input_read_limit = max_end + 2.0
        except Exception:
            input_read_limit = 0.0

        total = sum(s.dur for s in keeps_shifted)
        total = max(1e-6, total)

        self._log(
            f"mode=filter_concat_chunk {self._summarize_keeps(keeps_sorted)} "
            f"preseek={preseek:.3f}s total_kept={total:.3f}s fps={force_fps:.3f} "
            f"container={container} video_only={'yes' if video_only else 'no'}"
        )

        if video_only:
            filt = build_filter_complex_video_only(keeps_shifted, fps=force_fps)
            audio_label = ""
            af = ""
        else:
            audio_label = "outa"
            af = ""
            audio_processing = False
            if apply_audio_filters is None:
                if abs(self.audio_gain_db) > 1e-6:
                    af = self._build_audio_filter_chain(include_legacy_gain_limiter=True)
                    audio_processing = bool(af)
            elif apply_audio_filters:
                af = self._build_audio_filter_chain(include_legacy_gain_limiter=True)
                audio_processing = bool(af)

            force_async_audio = bool(audio_processing)
            filt = build_filter_complex(
                keeps_shifted,
                fps=force_fps,
                force_audio_async=force_async_audio,
            )
            self._log(
                f"filter_concat_async_aresample={'on' if force_async_audio else 'off'} "
                f"reason={'audio_processing' if force_async_audio else 'aligned_single_input'}"
            )

            if af:
                filt = filt.rstrip()
                filt += f";[outa]{af}[outa_f]"
                audio_label = "outa_f"
            self._log_audio_mode(
                "filter_concat_chunk",
                audio_processing=audio_processing,
                audio_copy_ok=False,
                filters_in_segments=bool(audio_processing),
            )

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8", newline="\n",
            dir=tmp_dir if tmp_dir else None
        ) as f:
            f.write(filt)
            f.write("\n")
            filter_file = f.name
            temp_files.append(filter_file)

        cmd = [
            self.ffmpeg_path, "-hide_banner", "-v", "error", "-y", "-nostdin",
            "-thread_queue_size", str(self._input_thread_queue_size()),
        ]
        cmd += self._hwaccel_args_for_filtergraph()

        if preseek > 0.0:
            cmd += ["-ss", f"{preseek:.3f}"]
        if input_read_limit > 1e-6:
            cmd += ["-t", f"{input_read_limit:.3f}"]
            self._log(
                f"filter_concat_input_limit preseek={preseek:.3f}s t={input_read_limit:.3f}s"
            )

        cmd += [
            "-i", str(src_path or self.input_path),
            "-filter_complex_script", filter_file,
            "-map", "[outv]",
        ]
        if not video_only:
            cmd += ["-map", f"[{audio_label}]"]
        else:
            cmd += ["-an"]
        cmd += [
            *self._ffmpeg_thread_args(),
            *self._video_args(video_preset),
            *self._drop_chapters_args(),
        ]
        if force_fps and force_fps > 1.0:
            cmd += ["-fps_mode", "cfr", "-r", f"{force_fps:.3f}"]

        cmd += [
            "-max_muxing_queue_size", "4096",
            "-progress", "pipe:2",
            "-nostats",
        ]
        if not video_only:
            cmd += ["-c:a", "aac", "-b:a", "320k"]
        if container == "ts":
            cmd += [
                "-avoid_negative_ts", "make_zero",
                "-max_interleave_delta", "0",
                "-muxpreload", "0",
                "-muxdelay", "0",
                "-f", "mpegts",
                out_path,
            ]
        else:
            cmd += ["-movflags", "+faststart", "-use_editlist", "0", out_path]
        self._log(
            f"ffmpeg_cmd_chunk={self._fmt_cmd(cmd)} "
            f"preset={video_preset}"
        )
        return cmd, total


    # -----------------------------
    # Filter-complex builder for flattened segments
    # -----------------------------
    def _build_cmd_filter_segments(
        self,
        segments: list[dict],
        temp_files: list[str],
        total: float,
        out_path: str | None = None,
        apply_audio_filters: bool = True,
        tmp_dir: str | None = None,
        input_preseek: dict[int, float] | None = None,
        force_fps: float = 0.0,
        container: str = "mp4",
    ) -> tuple[list[str], float]:
        segs = []
        for s in segments:
            try:
                dur = float(s.get("duration", 0.0) or 0.0)
            except Exception:
                dur = 0.0
            if dur <= 1e-6:
                continue
            segs.append(s)
        if not segs:
            raise RuntimeError("No export segments available.")

        total = sum(float(s.get("duration", 0.0) or 0.0) for s in segs)
        total = max(1e-6, total)

        parts: list[str] = []
        out_n = 0
        for seg in segs:
            try:
                v_idx = seg.get("v_idx", None)
                a_idx = seg.get("a_idx", None)
                v_in = float(seg.get("v_in", 0.0) or 0.0)
                v_out = float(seg.get("v_out", 0.0) or 0.0)
                a_in = float(seg.get("a_in", 0.0) or 0.0)
                a_out = float(seg.get("a_out", 0.0) or 0.0)
            except Exception:
                continue

            if v_idx is None:
                continue
            try:
                v_idx = int(v_idx)
            except Exception:
                continue

            v_shift = 0.0
            if input_preseek:
                try:
                    v_shift = float(input_preseek.get(v_idx, 0.0) or 0.0)
                except Exception:
                    v_shift = 0.0
            v_in_eff = max(0.0, float(v_in) - float(v_shift))
            v_out_eff = max(v_in_eff, float(v_out) - float(v_shift))
            if v_out_eff <= v_in_eff + 1e-9:
                continue

            lbl = out_n
            out_n += 1

            v_chain = f"[{v_idx}:v]trim=start={v_in_eff}:end={v_out_eff},setpts=PTS-STARTPTS"
            if force_fps and force_fps > 1.0:
                v_chain += f",fps={force_fps:.3f}"
            parts.append(f"{v_chain}[v{lbl}];")

            if a_idx is not None:
                try:
                    a_idx = int(a_idx)
                except Exception:
                    a_idx = None
            if a_idx is not None:
                a_shift = 0.0
                if input_preseek:
                    try:
                        a_shift = float(input_preseek.get(a_idx, 0.0) or 0.0)
                    except Exception:
                        a_shift = 0.0
                a_in_eff = max(0.0, float(a_in) - float(a_shift))
                a_out_eff = max(a_in_eff, float(a_out) - float(a_shift))
                parts.append(
                    f"[{a_idx}:a]atrim=start={a_in_eff}:end={a_out_eff},asetpts=PTS-STARTPTS[a{lbl}];"
                )
            else:
                dur = max(0.0, float(seg.get("duration", 0.0) or 0.0))
                parts.append(
                    f"anullsrc=channel_layout=stereo:sample_rate=48000,"
                    f"atrim=start=0:end={dur:.6f},asetpts=PTS-STARTPTS[a{lbl}];"
                )

        if out_n <= 0:
            raise RuntimeError("No valid export segments after preseek shift.")

        concat_inputs = "".join([f"[v{i}][a{i}]" for i in range(out_n)])
        parts.append(f"{concat_inputs}concat=n={out_n}:v=1:a=1[outv][outa0];")

        audio_processing = bool(apply_audio_filters and self._needs_audio_processing())
        async_reasons: list[str] = []
        if audio_processing:
            async_reasons.append("audio_processing")
        try:
            a_idxs = set()
            missing_audio = False
            mismatch = False
            for seg in segs:
                a_idx = seg.get("a_idx", None)
                if a_idx is None:
                    missing_audio = True
                else:
                    try:
                        a_idxs.add(int(a_idx))
                    except Exception:
                        missing_audio = True
                try:
                    v_in = float(seg.get("v_in", 0.0) or 0.0)
                    v_out = float(seg.get("v_out", 0.0) or 0.0)
                    a_in = float(seg.get("a_in", 0.0) or 0.0)
                    a_out = float(seg.get("a_out", 0.0) or 0.0)
                    if abs((v_out - v_in) - (a_out - a_in)) > 2e-3:
                        mismatch = True
                except Exception:
                    mismatch = True
            if len(a_idxs) > 1:
                async_reasons.append("multi_audio_input")
            if missing_audio:
                async_reasons.append("missing_audio")
            if mismatch:
                async_reasons.append("av_dur_mismatch")
        except Exception:
            async_reasons.append("analysis_error")

        force_async_audio = bool(async_reasons)
        if force_async_audio:
            parts.append("[outa0]aresample=async=1:first_pts=0[outa]")
        else:
            parts.append("[outa0]anull[outa]")
        self._log(
            f"filter_segments_async_aresample={'on' if force_async_audio else 'off'} "
            f"reasons={','.join(async_reasons) if async_reasons else 'aligned_single_audio'}"
        )

        audio_label = "outa"
        if apply_audio_filters:
            af = self._build_audio_filter_chain(include_legacy_gain_limiter=True)
            if af:
                parts.append(f"[{audio_label}]{af}[outa_f]")
                audio_label = "outa_f"

        # filter_segments always encodes audio to AAC in this path.
        self._log_audio_mode(
            "filter_segments",
            audio_processing=audio_processing,
            audio_copy_ok=False,
            filters_in_segments=bool(apply_audio_filters),
        )

        filt = "".join(parts)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8", newline="\n",
            dir=tmp_dir if tmp_dir else None
        ) as f:
            f.write(filt)
            f.write("\n")
            filter_file = f.name
            temp_files.append(filter_file)

        cmd = [
            self.ffmpeg_path, "-hide_banner", "-v", "error", "-y", "-nostdin",
            "-thread_queue_size", str(self._input_thread_queue_size()),
        ]
        cmd += self._hwaccel_args_for_filtergraph()

        # Limit input read window when preseek is active:
        # this avoids decoding from seek point to EOF when only a bounded range is needed.
        input_limits: dict[int, float] = {}
        if input_preseek:
            try:
                t_pad = float(_env("AUTO_CUTTER_SEG_INPUT_T_PAD", "2.0").strip() or "2.0")
            except Exception:
                t_pad = 2.0
            t_pad = max(0.0, min(10.0, float(t_pad)))

            max_end_by_idx: dict[int, float] = {}
            for seg in segs:
                try:
                    v_idx_raw = seg.get("v_idx", None)
                    if v_idx_raw is not None:
                        v_idx_i = int(v_idx_raw)
                        v_end = float(seg.get("v_out", seg.get("end", 0.0)) or 0.0)
                        if v_end > max_end_by_idx.get(v_idx_i, 0.0):
                            max_end_by_idx[v_idx_i] = v_end
                except Exception:
                    pass
                try:
                    a_idx_raw = seg.get("a_idx", None)
                    if a_idx_raw is not None:
                        a_idx_i = int(a_idx_raw)
                        a_end = float(seg.get("a_out", seg.get("end", 0.0)) or 0.0)
                        if a_end > max_end_by_idx.get(a_idx_i, 0.0):
                            max_end_by_idx[a_idx_i] = a_end
                except Exception:
                    pass

            for idx_i, end_ts in max_end_by_idx.items():
                try:
                    seek_ts = float(input_preseek.get(idx_i, 0.0) or 0.0)
                except Exception:
                    seek_ts = 0.0
                if seek_ts <= 1e-6:
                    continue
                if end_ts <= seek_ts + 1e-6:
                    continue
                lim = max(0.1, (float(end_ts) - float(seek_ts)) + float(t_pad))
                input_limits[idx_i] = lim
            if input_limits:
                try:
                    lim_parts = []
                    for k in sorted(input_limits):
                        lim_parts.append(f"{k}:ss={float(input_preseek.get(k, 0.0) or 0.0):.3f},t={input_limits[k]:.3f}")
                    self._log(f"filter_segments_input_limits {' | '.join(lim_parts)}")
                except Exception:
                    pass

        for idx, path in enumerate(self.input_paths):
            pre = 0.0
            if input_preseek:
                try:
                    pre = float(input_preseek.get(idx, 0.0) or 0.0)
                except Exception:
                    pre = 0.0
            if pre > 1e-6:
                cmd += ["-ss", f"{pre:.3f}"]
                t_lim = float(input_limits.get(idx, 0.0) or 0.0)
                if t_lim > 1e-6:
                    cmd += ["-t", f"{t_lim:.3f}"]
            cmd += ["-i", path]

        cmd += [
            "-filter_complex_script", filter_file,
            "-map", "[outv]", "-map", f"[{audio_label}]",
            *self._ffmpeg_thread_args(),
            *self._video_args(),
            *self._drop_chapters_args(),
        ]
        if force_fps and force_fps > 1.0:
            cmd += ["-fps_mode", "cfr", "-r", f"{force_fps:.3f}"]
        cmd += [
            "-c:a", "aac", "-b:a", "320k",
            "-max_muxing_queue_size", "4096",
            "-progress", "pipe:2",
            "-nostats",
        ]
        target = out_path if out_path else self.output_path
        if container == "ts":
            cmd += [
                "-avoid_negative_ts", "make_zero",
                "-max_interleave_delta", "0",
                "-muxpreload", "0",
                "-muxdelay", "0",
                "-f", "mpegts",
                target,
            ]
        else:
            cmd += ["-movflags", "+faststart", "-use_editlist", "0", target]
        self._log(f"ffmpeg_cmd_segments={self._fmt_cmd(cmd)}")
        return cmd, total

    def _compute_segment_preseek(
        self,
        segments: list[dict],
        preseek_pad: float = 2.0,
    ) -> dict[int, float]:
        min_ts: dict[int, float] = {}
        for s in segments:
            v_idx = s.get("v_idx", None)
            a_idx = s.get("a_idx", None)
            try:
                if v_idx is not None:
                    v_idx = int(v_idx)
                    v_in = float(s.get("v_in", 0.0) or 0.0)
                    min_ts[v_idx] = v_in if v_idx not in min_ts else min(min_ts[v_idx], v_in)
                if a_idx is not None:
                    a_idx = int(a_idx)
                    a_in = float(s.get("a_in", 0.0) or 0.0)
                    min_ts[a_idx] = a_in if a_idx not in min_ts else min(min_ts[a_idx], a_in)
            except Exception:
                continue

        preseek: dict[int, float] = {}
        for idx, t in min_ts.items():
            try:
                pre = max(0.0, float(t) - float(preseek_pad))
            except Exception:
                pre = 0.0
            if pre > 1e-6:
                preseek[idx] = pre
        return preseek

    def _run_chunked_parallel_segments(
        self,
        segments: list[dict],
        temp_files: list[str],
        prefer_hybrid: bool = False,
    ) -> None:
        self._export_method_used = "chunked_parallel_segments"
        total_all = sum(float(s.get("duration", 0.0) or 0.0) for s in segments)
        total_all = max(1e-6, total_all)
        force_fps = self._segments_base_fps(segments)
        hybrid_mode = bool(prefer_hybrid or self.export_method == "smart_hybrid")
        self._log(f"chunked_parallel_segments_hybrid={'on' if hybrid_mode else 'off'}")
        fps_cache: dict[int, float] = {}
        chunk_keyframes: list[float] = []
        split_idx = self._segments_single_input_idx(segments)
        if split_idx is not None and 0 <= split_idx < len(self.input_paths):
            try:
                src_path = self.input_paths[split_idx]
                keeps_for_split = self._segments_to_keeps_in_order(segments, fps=0.0)
                intervals = self._keyframe_intervals_for_keeps(
                    keeps_for_split,
                    pad=0.0,
                    max_intervals=160,
                )
                if intervals:
                    chunk_kf_source = "rap"
                    chunk_keyframes = scan_splice_safe_pts(src_path, use_cache=True, intervals=intervals)
                    if not chunk_keyframes:
                        chunk_keyframes = ffprobe_keyframes(src_path, use_cache=True, intervals=intervals)
                        chunk_kf_source = "ffprobe_keyframe"
                    self._log(
                        f"chunk_keyframes segments source={split_idx} mode={chunk_kf_source} "
                        f"count={len(chunk_keyframes)} "
                        f"enabled={'yes' if bool(chunk_keyframes) else 'no'}"
                    )
                else:
                    self._log(f"chunk_keyframes segments source={split_idx} skipped reason=dense_timeline")
            except Exception as e:
                self._log(f"chunk_keyframes segments disabled ({e})")

        workers = self._cap_parallel_workers(self.parallel_workers, context="chunked_parallel_segments")
        plan_max_segments_default = 120
        plan_max_chunk_seconds_default = 480.0
        if hybrid_mode and split_idx is not None:
            # Single-input hybrid chunks: prefer larger chunks to reduce edge re-encode overhead.
            plan_max_segments_default = 240
            if len(segments) >= 120:
                plan_max_chunk_seconds_default = 600.0
            elif len(segments) >= 60:
                plan_max_chunk_seconds_default = 480.0
            else:
                plan_max_chunk_seconds_default = 300.0
        plan_chunks, plan_max_segments, plan_max_chunk_seconds = self._plan_chunking(
            total_all,
            len(segments),
            self.chunk_count,
            workers,
            max_segments_default=plan_max_segments_default,
            max_chunk_seconds_default=plan_max_chunk_seconds_default,
            label="segments",
        )
        chunk_source_span = self._chunk_source_span_limit(plan_max_chunk_seconds, label="segments")
        self._log(f"chunk_source_span_limit segments={chunk_source_span:.1f}s")
        chunks = self._split_segment_dicts(
            segments,
            max_segments=plan_max_segments,
            max_chunk_seconds=plan_max_chunk_seconds,
            chunk_count=plan_chunks,
            keyframes=chunk_keyframes if chunk_keyframes else None,
            max_source_span=chunk_source_span,
        )
        if not chunks:
            raise RuntimeError("Chunking (segments) ha prodotto 0 chunk.")

        def _seg_start(seg: dict) -> float:
            for k in ("start", "v_in", "a_in"):
                try:
                    v = seg.get(k, None)
                    if v is not None:
                        return float(v)
                except Exception:
                    continue
            return 0.0

        # Keep chunk order aligned to timeline order.
        # (Sorting here breaks duplicated/reordered timelines.)
        chunk_durs = [sum(float(s.get("duration", 0.0) or 0.0) for s in ch) for ch in chunks]
        threads_per = self._threads_per_process(workers)
        self._ffmpeg_threads_override = threads_per
        self._log(
            f"thread_budget={self._thread_budget()} threads_per_proc={threads_per} workers={workers}"
        )

        chunk_dir = tempfile.mkdtemp(prefix="auto_cutter_seg_chunks_")
        temp_files.append(chunk_dir)
        cache_on = self._chunk_cache_enabled()
        use_ts = False
        if _env("AUTO_CUTTER_CHUNKS_TS", "0").strip() in ("1", "true", "True"):
            use_ts = True
        elif cache_on:
            use_ts = True
        elif len(chunks) > 120:
            use_ts = True
        chunk_ext = "ts" if use_ts else "mp4"
        chunk_files: list[str] = ["" for _ in range(len(chunks))]

        self._log(
            f"mode=chunked_parallel_segments chunks={len(chunks)} workers={workers} total={total_all:.3f}s "
            f"chunk_cache={'on' if cache_on else 'off'} container={chunk_ext}"
        )

        groups: list[dict] = []
        sig_to_group: dict[str, int] = {}
        for i, ch in enumerate(chunks):
            key = self._chunk_signature_segments(
                ch,
                force_fps=force_fps,
                container=chunk_ext,
                apply_audio_filters=False,
            )
            sig = f"k:{key}" if key else f"i:{i}"
            gi = sig_to_group.get(sig)
            if gi is None:
                sig_to_group[sig] = len(groups)
                groups.append({"key": key, "indices": [i]})
            else:
                groups[gi]["indices"].append(i)

        render_jobs: list[dict] = []
        cache_hit_chunks = 0
        cache_hit_media = 0.0
        dedup_reused = 0
        dedup_reused_media = 0.0
        pre_done_indices: list[int] = []
        for g in groups:
            idxs: list[int] = list(g.get("indices") or [])
            if not idxs:
                continue
            rep = int(idxs[0])
            key = str(g.get("key") or "")
            out_path = ""
            if key and cache_on:
                hit = self._chunk_cache_lookup(key, chunk_ext)
                if hit:
                    out_path = hit
                    cache_hit_chunks += len(idxs)
                    cache_hit_media += sum(chunk_durs[j] for j in idxs if 0 <= j < len(chunk_durs))
                    pre_done_indices.extend(idxs)
            if not out_path:
                if key and cache_on:
                    out_path = self._chunk_cache_key_path(key, chunk_ext)
                else:
                    out_path = os.path.join(chunk_dir, f"chunk_{rep:03d}.{chunk_ext}")
                render_jobs.append(
                    {
                        "rep": rep,
                        "indices": idxs,
                        "key": key,
                        "out_path": out_path,
                        "cacheable": bool(cache_on and key),
                    }
                )
            for j in idxs:
                chunk_files[j] = out_path
            if len(idxs) > 1:
                dedup_reused += (len(idxs) - 1)
                dedup_reused_media += sum(
                    chunk_durs[j] for j in idxs[1:] if 0 <= j < len(chunk_durs)
                )

        if cache_on:
            self._log(
                f"chunk_cache_plan segments groups={len(groups)} render_jobs={len(render_jobs)} "
                f"cache_hits={cache_hit_chunks} dedup_reused={dedup_reused} "
                f"cache_dir={self._chunk_cache_dir()}"
            )
        else:
            self._log(
                f"chunk_dedup_plan segments groups={len(groups)} render_jobs={len(render_jobs)} "
                f"dedup_reused={dedup_reused}"
            )
        if cache_hit_media > 0.0:
            self._record_reuse(cache_hit_chunks, cache_hit_media, category="chunk_dedup")
        if dedup_reused_media > 0.0:
            self._record_reuse(dedup_reused, dedup_reused_media, category="chunk_dedup")

        # Seek-friendly execution order without changing output timeline order.
        render_jobs.sort(
            key=lambda j: _seg_start(chunks[int(j.get("rep", 0))][0])
            if chunks and chunks[int(j.get("rep", 0))]
            else 0.0
        )

        self.progress.emit(
            0,
            f"Rendering {len(render_jobs)} chunk job (from {len(chunks)} chunks, parallel={workers})...",
        )

        progress_lock = threading.Lock()
        chunk_out = [0.0 for _ in range(len(chunks))]
        chunk_last_pct = [-1 for _ in range(len(chunks))]
        last_emit = 0.0
        state = {"finished": 0}

        def _emit_global_locked() -> None:
            nonlocal last_emit
            now = time.monotonic()
            if now - last_emit < 0.5:
                return
            last_emit = now
            done = sum(chunk_out)
            pct = int(max(0.0, min(99.0, (done / total_all) * 100.0)))
            self.progress.emit(pct, f"Chunks {state['finished']}/{len(chunks)}  {pct}%")

        with progress_lock:
            for i in pre_done_indices:
                chunk_out[i] = chunk_durs[i]
            state["finished"] = len(pre_done_indices)
            _emit_global_locked()

        def _render_one(job: dict) -> dict:
            t0 = time.time()
            i = int(job.get("rep", 0))
            idxs = list(job.get("indices") or [i])
            cacheable = bool(job.get("cacheable"))
            key = str(job.get("key") or "")
            out_path = str(job.get("out_path") or "")
            render_out = out_path
            if cacheable:
                render_out = os.path.join(chunk_dir, f"chunk_render_{i:03d}.{chunk_ext}")
            self._log(
                f"chunk_start {i+1}/{len(chunks)} group={len(idxs)} "
                f"cacheable={'yes' if cacheable else 'no'}"
            )
            ch = chunks[i]
            if hybrid_mode:
                idx = self._segments_single_input_idx(ch)
                if idx is not None and 0 <= idx < len(self.input_paths):
                    if idx not in fps_cache:
                        try:
                            fps_cache[idx] = float(self._probe_fps(self.input_paths[idx]) or 0.0)
                        except Exception:
                            fps_cache[idx] = 0.0
                    keeps = self._segments_to_keeps_in_order(ch, fps=fps_cache.get(idx, 0.0))
                    if keeps:
                        def _on_hybrid_piece_progress(done_seconds: float, total_seconds: float) -> None:
                            if total_seconds <= 1e-6:
                                return
                            out_time = max(0.0, min(chunk_durs[i], float(done_seconds)))
                            with progress_lock:
                                chunk_out[i] = out_time
                                pct = int(
                                    max(0.0, min(99.0, (out_time / max(1e-6, chunk_durs[i])) * 100.0))
                                )
                                if pct != chunk_last_pct[i] and (pct % 5 == 0 or pct >= 99):
                                    chunk_last_pct[i] = pct
                                    self._log(
                                        f"chunk_hybrid_progress {i+1}/{len(chunks)} {pct}% "
                                        f"{out_time:.1f}/{chunk_durs[i]:.1f}s"
                                    )
                                _emit_global_locked()

                        try:
                            self._run_smart_hybrid(
                                keeps,
                                temp_files,
                                src_path=self.input_paths[idx],
                                out_path=render_out,
                                emit_progress=False,
                                progress_cb=_on_hybrid_piece_progress,
                                segments_context=True,
                            )
                            with progress_lock:
                                chunk_out[i] = chunk_durs[i]
                                _emit_global_locked()
                            self._log(f"chunk_hybrid_done {i+1}/{len(chunks)}")
                            elapsed = max(0.0, time.time() - t0)
                            try:
                                with progress_lock:
                                    self._chunk_times.append(elapsed)
                            except Exception:
                                pass
                            self._record_render_work(chunk_durs[i], elapsed)
                            final_out = render_out
                            if cacheable:
                                final_out = self._chunk_cache_store(key, chunk_ext, render_out)
                            return {
                                "rep": i,
                                "indices": idxs,
                                "out_path": final_out,
                            }
                        except _SmartHybridFallback:
                            self._log("smart_hybrid_chunk_fallback -> filter_segments")
                        except _ExportCancelled:
                            raise
                        except Exception as e:
                            self._log(f"smart_hybrid_chunk_failed -> filter_segments ({e})")

            preseek = self._compute_segment_preseek(ch, preseek_pad=2.0)
            cmd, _ = self._build_cmd_filter_segments(
                ch,
                temp_files,
                total_all,
                out_path=render_out,
                apply_audio_filters=False,
                tmp_dir=chunk_dir,
                input_preseek=preseek,
                force_fps=force_fps,
                container=chunk_ext,
            )

            def _on_progress(out_time: float) -> None:
                if out_time < 0.0:
                    return
                if out_time > chunk_durs[i]:
                    out_time = chunk_durs[i]
                with progress_lock:
                    chunk_out[i] = out_time
                    pct = int(max(0.0, min(99.0, (out_time / max(1e-6, chunk_durs[i])) * 100.0)))
                    if pct != chunk_last_pct[i] and (pct % 5 == 0 or pct >= 99):
                        chunk_last_pct[i] = pct
                        self._log(
                            f"chunk_progress {i+1}/{len(chunks)} {pct}% "
                            f"{out_time:.1f}/{chunk_durs[i]:.1f}s"
                        )
                    _emit_global_locked()

            rc, err = self._run_ffmpeg_progress(cmd, on_progress=_on_progress)
            if rc != 0:
                err = (err or "").strip()
                raise RuntimeError(err or f"FFmpeg chunk {i} failed")
            final_out = render_out
            if cacheable:
                final_out = self._chunk_cache_store(key, chunk_ext, render_out)
            elapsed = max(0.0, time.time() - t0)
            try:
                with progress_lock:
                    self._chunk_times.append(elapsed)
            except Exception:
                pass
            self._record_render_work(chunk_durs[i], elapsed)
            return {
                "rep": i,
                "indices": idxs,
                "out_path": final_out,
            }

        try:
            if render_jobs:
                with ThreadPoolExecutor(max_workers=workers) as ex:
                    futs = [ex.submit(_render_one, j) for j in render_jobs]
                    try:
                        for fut in as_completed(futs):
                            try:
                                res = fut.result()
                            except Exception:
                                # Fail-fast: stop other ffmpeg processes to avoid apparent "hang".
                                self._terminate_all_procs()
                                for other in futs:
                                    other.cancel()
                                raise
                            i = int(res.get("rep", 0))
                            idxs = [int(x) for x in list(res.get("indices") or [i])]
                            out_path = str(res.get("out_path") or "")
                            with progress_lock:
                                for j in idxs:
                                    if 0 <= j < len(chunk_out):
                                        chunk_out[j] = chunk_durs[j]
                                        chunk_files[j] = out_path
                                state["finished"] += len(idxs)
                                _emit_global_locked()
                            pct = int(max(0.0, min(99.0, (sum(chunk_out) / total_all) * 100.0)))
                            self._log(
                                f"chunk_done {i+1}/{len(chunks)} rendered={len(idxs)} "
                                f"pct={pct}% path={out_path}"
                            )
                            self.progress.emit(pct, f"Chunk {state['finished']}/{len(chunks)} completati...")
                    finally:
                        for fut in futs:
                            fut.cancel()
        finally:
            self._ffmpeg_threads_override = None

        self.progress.emit(99, "Concatenazione finale...")
        self._concat_chunks_copy(chunk_files, temp_files, use_ts=use_ts)

    # -----------------------------
    # Main worker entry
    # -----------------------------
    @Slot()
    def run(self):
        temp_files: list[str] = []
        keeps_sorted: list[Segment] = []
        force_fps: float = 0.0
        start_logged = False
        max_conservative_mode = self._max_conservative_fix_mode()
        max_conservative_requested = max_conservative_mode != "off"
        self._log("worker_run_begin")
        try:
            self.progress.emit(0, "Initializing export...")
        except Exception:
            pass

        def _run_single(cmd: list[str], total: float, label: str = "Export") -> None:
            last_pct = -1

            def _on_progress(out_time: float) -> None:
                nonlocal last_pct
                if out_time < 0.0:
                    return
                if total <= 1e-6:
                    pct = 0
                else:
                    pct = int(min(100, max(0, (out_time / total) * 100.0)))
                if pct != last_pct:
                    self._log(f"progress {pct}%  out_time={out_time:.1f}s / {total:.1f}s")
                    last_pct = pct
                self.progress.emit(pct, f"{label} {pct}%")

            rc, err = self._run_ffmpeg_progress(cmd, on_progress=_on_progress)
            if rc != 0:
                raise RuntimeError((err or "").strip() or "FFmpeg export failed")

        def _log_start() -> None:
            nonlocal start_logged
            if start_logged:
                return
            start_logged = True
            hw_decode = bool(self._hwaccel_args())
            threads_per = self._ffmpeg_threads_override
            self._log(
                f"start codec={self.codec} hwaccel={hw_decode} method={self.export_method} "
                f"workers={self.parallel_workers} threads_per={threads_per} chunks={self.chunk_count} "
                f"input_paths={self.input_paths} output={self.output_path}"
            )
            self._log(
                f"audio: gain_db={self.audio_gain_db:.2f} normalize_lufs={self.normalize_lufs} "
                f"lufs_target={self.lufs_target:.1f} limiter={self.apply_limiter} limit={self.limiter_limit:.3f}"
            )
            cut_hq_enabled, cut_hq_max = self._cut_hq_policy()
            self._log(
                f"cut_hq: enabled={'yes' if cut_hq_enabled else 'no'} "
                f"max_seconds={cut_hq_max:.1f}"
            )

        def _expected_output_duration_hint() -> float:
            try:
                if self._summary_segments_total is not None and float(self._summary_segments_total) > 0:
                    return float(self._summary_segments_total)
            except Exception:
                pass
            try:
                if self._summary_keeps_total is not None and float(self._summary_keeps_total) > 0:
                    return float(self._summary_keeps_total)
            except Exception:
                pass
            return 0.0

        def _duration_looks_wrong(actual_s: float, expected_s: float) -> bool:
            if expected_s <= 1.0 or actual_s <= 1.0:
                return False
            # Post-export validator: detect both oversized and undersized timelines
            # against the expected output duration.
            abs_tol = max(3.0, min(25.0, expected_s * 0.08))
            diff = abs(actual_s - expected_s)
            if diff <= abs_tol:
                return False
            return (actual_s < (expected_s * 0.90)) or (actual_s > (expected_s * 1.10))

        def _start_offset_looks_wrong(metrics: dict, expected_s: float) -> bool:
            """
            Some exports are content-correct but carry a large positive stream start offset.
            Certain players then display a fake total duration (often close to source timeline).
            """
            try:
                start_s = float(metrics.get("min_stream_start", 0.0) or 0.0)
            except Exception:
                start_s = 0.0
            if start_s <= 2.0:
                return False
            # Ignore small-ish offsets that can appear in normal MP4/TS remuxes.
            # Trigger on clearly suspicious values (e.g. minutes/hours).
            lim = max(5.0, min(300.0, max(10.0, expected_s * 0.25)))
            return start_s > lim

        def _stream_end_looks_wrong(metrics: dict, expected_s: float) -> bool:
            try:
                end_s = float(metrics.get("max_stream_end", 0.0) or 0.0)
            except Exception:
                end_s = 0.0
            if end_s <= 2.0:
                return False
            abs_tol = max(8.0, min(60.0, expected_s * 0.15))
            return (end_s > (expected_s + abs_tol)) and (end_s > (expected_s * 1.25))

        def _repair_output_duration_if_needed() -> None:
            expected_s = _expected_output_duration_hint()
            if expected_s <= 1.0:
                return
            # When the timestamp-gap bug happens, the real content length is usually
            # correct but the MP4 timeline gets stretched. Clamp repair outputs to the
            # expected kept duration (with a tiny slack) so players don't show a fake tail.
            trim_target_s = max(0.5, float(expected_s) + min(0.75, max(0.05, expected_s * 0.005)))
            try:
                m0 = self._probe_duration_metrics(self.output_path)
            except Exception:
                m0 = {}
            try:
                actual_s = float(m0.get("effective_duration", 0.0) or 0.0)
            except Exception:
                actual_s = 0.0
            duration_bad = _duration_looks_wrong(actual_s, expected_s)
            start_offset_bad = _start_offset_looks_wrong(m0, expected_s)
            stream_end_bad = _stream_end_looks_wrong(m0, expected_s)
            if not (duration_bad or start_offset_bad or stream_end_bad):
                return

            self._log(
                f"duration_sanity suspicious actual_effective={actual_s:.3f}s "
                f"format={float(m0.get('format_duration', 0.0) or 0.0):.3f}s "
                f"stream_span={float(m0.get('span_duration', 0.0) or 0.0):.3f}s "
                f"stream_start={float(m0.get('min_stream_start', 0.0) or 0.0):.3f}s "
                f"stream_end={float(m0.get('max_stream_end', 0.0) or 0.0):.3f}s "
                f"expected={expected_s:.3f}s "
                f"reasons="
                f"{'dur_mismatch' if duration_bad else ''}"
                f"{'+start_offset' if start_offset_bad else ''}"
                f"{'+stream_end' if stream_end_bad else ''} "
                "-> timestamp normalization retry"
            )

            out_dir = os.path.dirname(self.output_path) or None

            def _mk_tmp_mp4(tag: str) -> str:
                fd, p = tempfile.mkstemp(prefix=f"ac_{tag}_", suffix=".mp4", dir=out_dir)
                os.close(fd)
                try:
                    os.remove(p)
                except Exception:
                    pass
                temp_files.append(p)
                return p

            def _probe_dur(path: str) -> float:
                try:
                    mm = self._probe_duration_metrics(path)
                    return float(mm.get("effective_duration", 0.0) or 0.0)
                except Exception:
                    return 0.0

            def _metrics_bad(mm: dict, expected_val: float) -> bool:
                try:
                    actual_val = float(mm.get("effective_duration", 0.0) or 0.0)
                except Exception:
                    actual_val = 0.0
                return bool(
                    _duration_looks_wrong(actual_val, expected_val)
                    or _start_offset_looks_wrong(mm, expected_val)
                    or _stream_end_looks_wrong(mm, expected_val)
                )

            def _rebuild_from_source_if_needed() -> bool:
                """
                Last-resort deterministic fix:
                rebuild from original source timeline using trim/setpts paths.
                This is slower, but it guarantees a continuous MP4 timeline.
                """
                try:
                    tmp_src = _mk_tmp_mp4("durfix_source_rebuild")
                    local_force_fps = 0.0
                    try:
                        local_force_fps = float(force_fps or 0.0)  # noqa: F821 - closure from run()
                    except Exception:
                        local_force_fps = 0.0

                    used_segments = False
                    segs_local = []
                    try:
                        segs_local = [dict(s) for s in (segments or [])]  # noqa: F821 - closure from run()
                    except Exception:
                        segs_local = []
                    segs_local = [s for s in segs_local if float(s.get("duration", 0.0) or 0.0) > 1e-6]

                    if segs_local:
                        used_segments = True
                        preseek = self._compute_segment_preseek(segs_local, preseek_pad=2.0)
                        cmd_src, _tot = self._build_cmd_filter_segments(
                            segs_local,
                            temp_files,
                            sum(float(s.get("duration", 0.0) or 0.0) for s in segs_local),
                            out_path=tmp_src,
                            apply_audio_filters=True,
                            input_preseek=preseek,
                            force_fps=local_force_fps,
                        )
                    else:
                        try:
                            ks_local = list(keeps_sorted)  # noqa: F821 - closure from run()
                        except Exception:
                            ks_local = []
                        if not ks_local:
                            try:
                                ks_local = sorted(self.keeps, key=lambda s: s.start)
                                ks_local = [
                                    Segment(max(0.0, s.start), max(0.0, s.end))
                                    for s in ks_local
                                    if s.end > s.start
                                ]
                                ks_local = self._normalize_keeps(ks_local, merge_gap=0.25, min_dur=0.08)
                                if local_force_fps and local_force_fps > 1.0 and ks_local:
                                    ks_local = self._snap_keeps_to_frames(ks_local, local_force_fps)
                            except Exception:
                                ks_local = []
                        if not ks_local:
                            self._log("duration_sanity_source_rebuild skipped reason=no_segments_or_keeps")
                            return False
                        if len(self.input_paths) > 1:
                            self._log("duration_sanity_source_rebuild skipped reason=multi_input_not_supported")
                            return False
                        cmd_src, _tot = self._build_cmd_filter_concat_to(
                            ks_local,
                            tmp_src,
                            temp_files,
                            tmp_dir=None,
                            force_fps=local_force_fps,
                            src_path=self.input_path,
                            apply_audio_filters=True,
                            video_only=False,
                            container="mp4",
                        )

                    self._log(
                        "duration_sanity_source_rebuild "
                        f"mode={'segments' if used_segments else 'keeps'} "
                        f"cmd={self._fmt_cmd(cmd_src)}"
                    )
                    rc_s, _out_s, err_s = self._run_ffmpeg(cmd_src)
                    if rc_s != 0:
                        self._log(f"duration_sanity_source_rebuild failed err={(err_s or '').strip()}")
                        return False

                    try:
                        mm_src = self._probe_duration_metrics(tmp_src)
                    except Exception:
                        mm_src = {}
                    self._log(
                        "duration_sanity_source_rebuild result "
                        f"effective={float(mm_src.get('effective_duration', 0.0) or 0.0):.3f}s "
                        f"format={float(mm_src.get('format_duration', 0.0) or 0.0):.3f}s "
                        f"span={float(mm_src.get('span_duration', 0.0) or 0.0):.3f}s "
                        f"start={float(mm_src.get('min_stream_start', 0.0) or 0.0):.3f}s "
                        f"end={float(mm_src.get('max_stream_end', 0.0) or 0.0):.3f}s "
                        f"expected={expected_s:.3f}s"
                    )
                    if _metrics_bad(mm_src, expected_s):
                        self._log("duration_sanity_source_rebuild rejected reason=metrics_still_bad")
                        return False

                    os.replace(tmp_src, self.output_path)
                    return True
                except Exception as e:
                    self._log(f"duration_sanity_source_rebuild exception={e}")
                    return False

            # Pass 1: remux-copy with stronger timestamp normalization flags.
            try:
                tmp_copy = _mk_tmp_mp4("durfix_copy")
                has_audio_out = False
                try:
                    has_audio_out = bool(has_audio_stream(self.output_path))
                except Exception:
                    has_audio_out = False
                cmd_copy = [
                    self.ffmpeg_path, "-hide_banner", "-v", "error", "-y", "-nostdin",
                    "-fflags", "+genpts",
                    "-i", self.output_path,
                    "-map", "0:v:0",
                    "-map", "0:a:0?",
                    "-c", "copy",
                    *self._drop_chapters_args(),
                ]
                if has_audio_out:
                    try:
                        if self._probe_audio_codec(self.output_path) in ("aac", "aac_latm"):
                            cmd_copy += ["-bsf:a", "aac_adtstoasc"]
                    except Exception:
                        pass
                cmd_copy += [
                    "-avoid_negative_ts", "make_zero",
                    "-max_interleave_delta", "0",
                    "-muxpreload", "0",
                    "-muxdelay", "0",
                    "-t", f"{trim_target_s:.6f}",
                    "-movflags", "+faststart",
                    tmp_copy,
                ]
                self._log(f"duration_sanity_copy_fix cmd={self._fmt_cmd(cmd_copy)}")
                rc_c, _out_c, err_c = self._run_ffmpeg(cmd_copy)
                if rc_c == 0:
                    fixed_s = _probe_dur(tmp_copy)
                    try:
                        mm = self._probe_duration_metrics(tmp_copy)
                    except Exception:
                        mm = {}
                    self._log(
                        f"duration_sanity_copy_fix result actual_effective={fixed_s:.3f}s "
                        f"format={float(mm.get('format_duration', 0.0) or 0.0):.3f}s "
                        f"stream_span={float(mm.get('span_duration', 0.0) or 0.0):.3f}s "
                        f"stream_start={float(mm.get('min_stream_start', 0.0) or 0.0):.3f}s "
                        f"stream_end={float(mm.get('max_stream_end', 0.0) or 0.0):.3f}s "
                        f"expected={expected_s:.3f}s"
                    )
                    if (
                        (fixed_s > 1.0)
                        and (not _duration_looks_wrong(fixed_s, expected_s))
                        and (not _start_offset_looks_wrong(mm, expected_s))
                        and (not _stream_end_looks_wrong(mm, expected_s))
                    ):
                        os.replace(tmp_copy, self.output_path)
                        return
                else:
                    self._log(f"duration_sanity_copy_fix failed err={(err_c or '').strip()}")
            except Exception as e:
                self._log(f"duration_sanity_copy_fix exception={e}")

            # Pass 2 (rare): rebuild timestamps by re-encoding the already-exported file.
            # This is slower, but guarantees a continuous timeline in MP4 players.
            try:
                tmp_re = _mk_tmp_mp4("durfix_reencode")
                has_audio_out = False
                try:
                    has_audio_out = bool(has_audio_stream(self.output_path))
                except Exception:
                    has_audio_out = False

                cmd_re = [
                    self.ffmpeg_path, "-hide_banner", "-v", "error", "-y", "-nostdin",
                    "-fflags", "+genpts",
                    "-i", self.output_path,
                    "-map", "0:v:0",
                    "-vf", "setpts=PTS-STARTPTS",
                    *self._drop_chapters_args(),
                    *self._video_args(),
                ]
                if has_audio_out:
                    cmd_re += [
                        "-map", "0:a:0?",
                        "-c:a", "aac", "-b:a", "192k",
                        "-af", "aresample=async=1:first_pts=0",
                    ]
                else:
                    cmd_re += ["-an"]
                cmd_re += [
                    "-avoid_negative_ts", "make_zero",
                    "-max_interleave_delta", "0",
                    "-t", f"{trim_target_s:.6f}",
                    "-movflags", "+faststart",
                    tmp_re,
                ]
                self._log(f"duration_sanity_reencode_fix cmd={self._fmt_cmd(cmd_re)}")
                rc_r, _out_r, err_r = self._run_ffmpeg(cmd_re)
                if rc_r != 0:
                    self._log(f"duration_sanity_reencode_fix failed err={(err_r or '').strip()}")
                    return
                fixed_s = _probe_dur(tmp_re)
                try:
                    mm = self._probe_duration_metrics(tmp_re)
                except Exception:
                    mm = {}
                self._log(
                    f"duration_sanity_reencode_fix result actual_effective={fixed_s:.3f}s "
                    f"format={float(mm.get('format_duration', 0.0) or 0.0):.3f}s "
                    f"stream_span={float(mm.get('span_duration', 0.0) or 0.0):.3f}s "
                    f"stream_start={float(mm.get('min_stream_start', 0.0) or 0.0):.3f}s "
                    f"stream_end={float(mm.get('max_stream_end', 0.0) or 0.0):.3f}s "
                    f"expected={expected_s:.3f}s"
                )
                if (
                    (fixed_s > 1.0)
                    and (not _duration_looks_wrong(fixed_s, expected_s))
                    and (not _start_offset_looks_wrong(mm, expected_s))
                    and (not _stream_end_looks_wrong(mm, expected_s))
                ):
                    os.replace(tmp_re, self.output_path)
                else:
                    self._log(
                        f"duration_sanity_reencode_fix rejected actual_effective={fixed_s:.3f}s "
                        f"stream_start={float(mm.get('min_stream_start', 0.0) or 0.0):.3f}s "
                        f"expected={expected_s:.3f}s"
                    )
            except Exception as e:
                self._log(f"duration_sanity_reencode_fix exception={e}")

            # Pass 3 (deterministic fallback): rebuild from source timeline with trim/setpts.
            _rebuild_from_source_if_needed()

        def _finalize() -> None:
            _repair_output_duration_if_needed()
            expected_s = _expected_output_duration_hint()
            mmf: dict = {}
            try:
                mmf = self._probe_duration_metrics(self.output_path)
                if mmf:
                    self._log(
                        "final_output_metrics "
                        f"effective={float(mmf.get('effective_duration', 0.0) or 0.0):.3f}s "
                        f"format={float(mmf.get('format_duration', 0.0) or 0.0):.3f}s "
                        f"span={float(mmf.get('span_duration', 0.0) or 0.0):.3f}s "
                        f"start={float(mmf.get('min_stream_start', 0.0) or 0.0):.3f}s "
                        f"end={float(mmf.get('max_stream_end', 0.0) or 0.0):.3f}s"
                    )
            except Exception:
                mmf = {}
            try:
                self._log_ts_debug(self.output_path, label="final_output")
            except Exception:
                pass
            try:
                actual_s = float(mmf.get("effective_duration", 0.0) or 0.0)
            except Exception:
                actual_s = 0.0
            post_mismatch = False
            if expected_s > 1.0 and actual_s > 1.0:
                post_mismatch = bool(
                    _duration_looks_wrong(actual_s, expected_s)
                    or _start_offset_looks_wrong(mmf, expected_s)
                    or _stream_end_looks_wrong(mmf, expected_s)
                )
                self._log(
                    "post_export_validator "
                    f"expected={expected_s:.3f}s "
                    f"actual={actual_s:.3f}s "
                    f"status={'mismatch' if post_mismatch else 'ok'}"
                )
            elif expected_s > 1.0:
                self._log(
                    "post_export_validator "
                    f"expected={expected_s:.3f}s actual={actual_s:.3f}s status=insufficient_metrics"
                )
                post_mismatch = True
            try:
                if not self._validate_output(self.output_path):
                    raise RuntimeError("Validation failed: output is not playable")
                if post_mismatch:
                    raise RuntimeError(
                        "Post-export validation failed: output duration mismatch "
                        f"(expected {expected_s:.2f}s, actual {actual_s:.2f}s)."
                    )
            except Exception as e:
                raise RuntimeError(str(e))
            self._finish_success()

        def _ensure_thread_override() -> None:
            if self._ffmpeg_threads_override is None or self._ffmpeg_threads_override <= 0:
                self._ffmpeg_threads_override = self._threads_per_process(self.parallel_workers)

        self._export_start_ts = time.time()

        try:
            try:
                if self.input_paths:
                    self._summary_input_duration = float(ffprobe_duration_seconds(self.input_paths[0]) or 0.0)
                else:
                    self._summary_input_duration = float(ffprobe_duration_seconds(self.input_path) or 0.0)
            except Exception:
                self._summary_input_duration = None

            if self._summary_input_duration is not None and self._summary_input_duration > 0:
                self._log(f"input_duration={self._summary_input_duration:.1f}s")

            if self.export_method == "smart_hybrid":
                self._log(f"smart_hybrid=on pad={self.smart_render_pad:.2f}s")
            self._log(
                f"max_conservative_export={'on' if max_conservative_requested else 'off'} "
                f"mode={max_conservative_mode} env=AUTO_CUTTER_EXPORT_MAX_CONSERVATIVE"
            )

            # Segments path (multi-input or timeline segments)
            segments = []
            for s in self.segments or []:
                try:
                    dur = float(s.get("duration", 0.0) or 0.0)
                except Exception:
                    dur = 0.0
                if dur > 1e-6:
                    segments.append(s)

            if segments:
                total_seg = sum(float(s.get("duration", 0.0) or 0.0) for s in segments)
                total_seg = max(1e-6, total_seg)
                self._summary_segments_count = len(segments)
                self._summary_segments_total = total_seg
                force_fps = self._segments_base_fps(segments)
                try:
                    src_for_tuner = self.input_paths[0] if self.input_paths else self.input_path
                    self._auto_tune(src_for_tuner, len(segments), total_seg)
                except Exception:
                    pass
                _ensure_thread_override()
                _log_start()
                self._log(
                    f"segments_after_norm {self._summarize_segments_dicts(segments)} "
                    f"fps={float(force_fps or 0.0):.3f}"
                )
                self._log_segments_timeline(segments)
                self._log(
                    f"checklist context=segments inputs={len(self.input_paths)} "
                    f"segments={len(segments)} total={total_seg:.3f}s requested_method={self.export_method}"
                )

                max_conservative_segments, mc_segments_reason = self._max_conservative_should_apply(
                    context="segments",
                    item_count=len(segments),
                    total_output_s=total_seg,
                    input_duration_s=self._summary_input_duration,
                )
                self._log(
                    f"max_conservative decision context=segments apply="
                    f"{'yes' if max_conservative_segments else 'no'} "
                    f"reason={mc_segments_reason} segments={len(segments)} "
                    f"total={total_seg:.3f}s input={float(self._summary_input_duration or 0.0):.1f}s"
                )
                if max_conservative_segments:
                    self._export_method_used = "max_conservative_segments"
                    mono_blocks = self._split_segments_by_source_monotonic(segments)
                    if len(mono_blocks) <= 1:
                        self._log("max_conservative path=segments force=filter_segments_single_pass")
                        preseek = self._compute_segment_preseek(segments, preseek_pad=2.0)
                        cmd, total = self._build_cmd_filter_segments(
                            segments,
                            temp_files,
                            total_seg,
                            out_path=None,
                            apply_audio_filters=True,
                            input_preseek=preseek,
                            force_fps=force_fps,
                        )
                        _run_single(cmd, total, label="Export")
                        self.progress.emit(100, "100%")
                        _finalize()
                        return

                    self._log(
                        f"max_conservative path=segments force=filter_segments_blocked blocks={len(mono_blocks)}"
                    )
                    block_dir = tempfile.mkdtemp(prefix="auto_cutter_mc_blocks_")
                    temp_files.append(block_dir)
                    block_files: list[str] = []
                    done_total = 0.0

                    for bi, block in enumerate(mono_blocks):
                        block_total = sum(float(s.get("duration", 0.0) or 0.0) for s in block)
                        block_total = max(1e-6, block_total)
                        block_out = os.path.join(block_dir, f"block_{bi:03d}.ts")
                        preseek = self._compute_segment_preseek(block, preseek_pad=2.0)
                        cmd, _ = self._build_cmd_filter_segments(
                            block,
                            temp_files,
                            block_total,
                            out_path=block_out,
                            apply_audio_filters=True,
                            input_preseek=preseek,
                            force_fps=force_fps,
                            container="ts",
                        )
                        self._log(
                            f"max_conservative_block {bi+1}/{len(mono_blocks)} "
                            f"segments={len(block)} total={block_total:.3f}s"
                        )

                        last_pct = -1

                        def _on_block_progress(out_time: float) -> None:
                            nonlocal last_pct
                            out_local = max(0.0, min(block_total, float(out_time)))
                            pct = int(
                                min(
                                    99,
                                    max(0, ((done_total + out_local) / max(1e-6, total_seg)) * 100.0),
                                )
                            )
                            if pct != last_pct:
                                last_pct = pct
                                self.progress.emit(pct, f"Export {pct}%")

                        rc, err = self._run_ffmpeg_progress(cmd, on_progress=_on_block_progress)
                        if rc != 0:
                            raise RuntimeError((err or "").strip() or "FFmpeg export block failed")
                        block_files.append(block_out)
                        done_total += block_total

                    self.progress.emit(99, "Concatenazione finale...")
                    self._concat_chunks_copy(block_files, temp_files, use_ts=True)
                    self.progress.emit(100, "100%")
                    _finalize()
                    return

                segments_method = self.export_method
                if segments_method == "auto":
                    idx = self._segments_single_input_idx(segments)
                    if idx is not None and 0 <= idx < len(self.input_paths):
                        if self._smart_render_supported(self.input_paths[idx]):
                            segments_method = "smart_hybrid"
                        else:
                            segments_method = "chunked_parallel"
                    else:
                        segments_method = "chunked_parallel"
                    self._log(f"auto_method -> {segments_method} (segments)")
                else:
                    idx = self._segments_single_input_idx(segments)
                self._log(
                    f"checklist decision method={segments_method} "
                    f"single_input_idx={idx if idx is not None else 'none'}"
                )

                if segments_method == "smart_hybrid":
                    # High-impact fast path for split/duplicate/reorder on single source:
                    # run one global hybrid render in timeline order (avoid N chunk edge re-encodes).
                    if idx is not None and 0 <= idx < len(self.input_paths):
                        keeps_timeline = self._segments_to_keeps_in_order(segments, fps=force_fps)
                        if keeps_timeline:
                            self._log("checklist step=run_smart_hybrid_segments_single_input")
                            try:
                                self._run_smart_hybrid(
                                    keeps_timeline,
                                    temp_files,
                                    src_path=self.input_paths[idx],
                                    out_path=self.output_path,
                                    emit_progress=True,
                                    segments_context=True,
                                )
                                self.progress.emit(100, "100%")
                                _finalize()
                                return
                            except _SmartHybridFallback as e:
                                self._log(f"smart_hybrid_segments_fallback -> chunked_parallel_segments ({e})")
                            except Exception as e:
                                self._log(f"smart_hybrid_segments_failed -> chunked_parallel_segments ({e})")
                    self._log("checklist step=run_chunked_parallel_segments_hybrid")
                    self._run_chunked_parallel_segments(
                        segments,
                        temp_files,
                        prefer_hybrid=True,
                    )
                    self.progress.emit(100, "100%")
                    _finalize()
                    return

                if segments_method == "chunked_parallel":
                    self._log("checklist step=run_chunked_parallel_segments")
                    self._run_chunked_parallel_segments(
                        segments,
                        temp_files,
                        prefer_hybrid=False,
                    )
                    self.progress.emit(100, "100%")
                    _finalize()
                    return

                idx = self._segments_single_input_idx(segments)
                if idx is not None and 0 <= idx < len(self.input_paths):
                    self._log("segments_single_input=True -> concat demuxer path")
                    self._log("checklist step=segments_to_keeps_concat_demuxer")
                    keeps = self._segments_to_keeps_in_order(segments, fps=force_fps)
                    if keeps:
                        self._summary_keeps_count = len(keeps)
                        self._summary_keeps_total = sum(float(s.end - s.start) for s in keeps)
                        self._export_method_used = "concat_demuxer"
                        cmd, total = self._build_cmd_concat_demuxer(
                            keeps,
                            temp_files,
                            src_path=self.input_paths[idx],
                            force_fps=force_fps,
                        )
                        _run_single(cmd, total, label="Export")
                        self.progress.emit(100, "100%")
                        _finalize()
                        return

                self._export_method_used = "filter_segments"
                self._log("checklist step=run_filter_segments")
                preseek = self._compute_segment_preseek(segments, preseek_pad=2.0)
                cmd, total = self._build_cmd_filter_segments(
                    segments,
                    temp_files,
                    total_seg,
                    out_path=None,
                    apply_audio_filters=True,
                    input_preseek=preseek,
                    force_fps=force_fps,
                )
                _run_single(cmd, total, label="Export")
                self.progress.emit(100, "100%")
                _finalize()
                return

            # Keeps path (single or multi input)
            keeps_sorted = sorted(self.keeps, key=lambda s: s.start)
            keeps_sorted = [
                Segment(max(0.0, s.start), max(0.0, s.end))
                for s in keeps_sorted
                if s.end > s.start
            ]
            if not keeps_sorted:
                raise RuntimeError("Nessun segmento da esportare (keeps vuoto).")

            keeps_sorted = self._normalize_keeps(keeps_sorted, merge_gap=0.25, min_dur=0.08)
            if not keeps_sorted:
                raise RuntimeError("Nessun segmento da esportare dopo normalizzazione.")

            force_fps = 0.0
            try:
                if self.input_paths:
                    force_fps = float(self._probe_fps(self.input_paths[0]) or 0.0)
                else:
                    force_fps = float(self._probe_fps(self.input_path) or 0.0)
            except Exception:
                force_fps = 0.0

            if force_fps and force_fps > 1.0:
                keeps_sorted = self._snap_keeps_to_frames(keeps_sorted, force_fps)

            self._summary_keeps_count = len(keeps_sorted)
            self._summary_keeps_total = sum(float(s.end - s.start) for s in keeps_sorted)
            avg_keep = 0.0
            try:
                avg_keep = float(self._summary_keeps_total or 0.0) / float(len(keeps_sorted))
            except Exception:
                avg_keep = 0.0
            prefer_macro = bool(
                self._tuner_prefer_macro
                or (len(keeps_sorted) > 150 and avg_keep > 0.0 and avg_keep < 6.0)
            )
            try:
                self._auto_tune(self.input_path, len(keeps_sorted), self._summary_keeps_total)
            except Exception:
                pass
            _ensure_thread_override()
            _log_start()
            self._log(
                f"keeps_after_norm {self._summarize_keeps(keeps_sorted)} "
                f"fps={float(force_fps or 0.0):.3f}"
            )
            self._log(
                f"checklist context=keeps inputs={len(self.input_paths)} keeps={len(keeps_sorted)} "
                f"total={float(self._summary_keeps_total or 0.0):.3f}s requested_method={self.export_method} "
                f"prefer_macro={'yes' if prefer_macro else 'no'}"
            )

            multi = len(self.input_paths) > 1
            if multi:
                self._export_method_used = "filter_concat_multi"
                self._log("checklist decision method=filter_concat_multi reason=multi_input")
                cmd, total = self._build_cmd_filter_concat_multi(keeps_sorted, temp_files, force_fps=force_fps)
                _run_single(cmd, total, label="Export")
                self.progress.emit(100, "100%")
                _finalize()
                return

            max_conservative_keeps, mc_keeps_reason = self._max_conservative_should_apply(
                context="keeps",
                item_count=len(keeps_sorted),
                total_output_s=float(self._summary_keeps_total or 0.0),
                input_duration_s=self._summary_input_duration,
            )
            self._log(
                f"max_conservative decision context=keeps apply="
                f"{'yes' if max_conservative_keeps else 'no'} "
                f"reason={mc_keeps_reason} keeps={len(keeps_sorted)} "
                f"total={float(self._summary_keeps_total or 0.0):.3f}s "
                f"input={float(self._summary_input_duration or 0.0):.1f}s"
            )
            if max_conservative_keeps:
                self._export_method_used = "max_conservative_keeps"
                self._log("max_conservative path=keeps force=filter_concat_single_pass")
                cmd, total = self._build_cmd_filter_concat(keeps_sorted, temp_files, force_fps=force_fps)
                _run_single(cmd, total, label="Export")
                self.progress.emit(100, "100%")
                _finalize()
                return

            method = self.export_method
            if method == "auto":
                dense_keeps_for_chunked = bool(
                    len(keeps_sorted) >= 180
                    and avg_keep > 0.0
                    and avg_keep <= 15.0
                    and float(self._summary_input_duration or 0.0) >= 3600.0
                )
                if dense_keeps_for_chunked:
                    method = "chunked_parallel"
                    self._log(
                        "auto_method -> chunked_parallel (keeps dense timeline over long source)"
                    )
                elif self._smart_render_supported(self.input_path):
                    method = "smart_hybrid"
                else:
                    method = "chunked_parallel"
                self._log(f"auto_method -> {method} (keeps)")
            self._log(f"checklist decision method={method}")

            if method == "chunked_parallel":
                if prefer_macro:
                    try:
                        self._log("auto_macro: enabled")
                        self._log("checklist step=run_single_process_macro")
                        self._run_single_process_macro(keeps_sorted, temp_files, force_fps=force_fps)
                        self.progress.emit(100, "100%")
                        _finalize()
                        return
                    except Exception as e:
                        self._log(f"auto_macro_failed -> chunked_parallel ({e})")
                self._log("checklist step=run_chunked_parallel")
                self._run_chunked_parallel(keeps_sorted, temp_files, force_fps=force_fps)
                self.progress.emit(100, "100%")
                _finalize()
                return

            if method in ("smart_render", "smart_hybrid"):
                try:
                    if method == "smart_render":
                        self._log("keeps_single_input=True -> smart_render path")
                        self._log("checklist step=run_smart_render")
                        self._run_smart_render(keeps_sorted, temp_files, self.input_path)
                    else:
                        self._log("keeps_single_input=True -> smart_hybrid path")
                        self._log("checklist step=run_smart_hybrid")
                        self._run_smart_hybrid(
                            keeps_sorted,
                            temp_files,
                            src_path=self.input_path,
                            out_path=None,
                            emit_progress=True,
                        )
                    self.progress.emit(100, "100%")
                    _finalize()
                    return
                except _SmartHybridFallback as e:
                    self._log(f"smart_render_or_hybrid_fallback -> chunked_parallel ({e})")
                    self._log("checklist step=fallback_chunked_parallel")
                    self._run_chunked_parallel(keeps_sorted, temp_files, force_fps=force_fps)
                    self.progress.emit(100, "100%")
                    _finalize()
                    return
                except _ExportCancelled:
                    raise
                except Exception as e:
                    self._log(f"{self.export_method}_failed -> chunked_parallel ({e})")
                    try:
                        self._log("checklist step=error_fallback_chunked_parallel")
                        self._run_chunked_parallel(keeps_sorted, temp_files, force_fps=force_fps)
                        self.progress.emit(100, "100%")
                        _finalize()
                        return
                    except Exception as e2:
                        self._log(f"chunked_parallel_failed -> concat_demuxer ({e2})")

            # Default single-input fallback
            self._log("keeps_single_input=True -> concat demuxer path")
            self._log("checklist step=default_concat_demuxer")
            self._export_method_used = "concat_demuxer"
            cmd, total = self._build_cmd_concat_demuxer(
                keeps_sorted,
                temp_files,
                src_path=self.input_path,
                force_fps=force_fps,
            )
            _run_single(cmd, total, label="Export")
            self.progress.emit(100, "100%")
            _finalize()

        except _ExportCancelled as e:
            self.error.emit(str(e))
        except Exception as e:
            msg = str(e)
            if "Validation failed" in msg:
                try:
                    self._log(f"validation_failed -> fallback ({self._export_method_used})")
                    if self._export_method_used in ("smart_hybrid", "smart_render", "single_process_macro"):
                        self._run_chunked_parallel(keeps_sorted, temp_files, force_fps=force_fps)
                        self.progress.emit(100, "100%")
                        _finalize()
                        return
                    if self._export_method_used in ("chunked_parallel", "chunked_parallel_segments"):
                        # last resort
                        cmd, total = self._build_cmd_filter_concat(keeps_sorted, temp_files, force_fps=force_fps)
                        _run_single(cmd, total, label="Export")
                        self.progress.emit(100, "100%")
                        _finalize()
                        return
                except Exception as e2:
                    msg = f"{msg} | fallback failed: {e2}"
            self.error.emit(msg)
        finally:
            try:
                self._chunk_cache_flush()
            except Exception:
                pass
            for p in temp_files:
                try:
                    if os.path.isdir(p):
                        shutil.rmtree(p, ignore_errors=True)
                    else:
                        os.remove(p)
                except Exception:
                    pass

    def _run_chunked_parallel(
        self,
        keeps_sorted: list[Segment],
        temp_files: list[str],
        force_fps: float = 0.0,
    ) -> None:
        self._export_method_used = "chunked_parallel"
        total_all = sum(s.end - s.start for s in keeps_sorted)
        total_all = max(1e-6, total_all)

        workers = self._cap_parallel_workers(self.parallel_workers, context="chunked_parallel")
        plan_chunks, plan_max_segments, plan_max_chunk_seconds = self._plan_chunking(
            total_all,
            len(keeps_sorted),
            self.chunk_count,
            workers,
            max_segments_default=150,
            max_chunk_seconds_default=600.0,
            label="keeps",
        )
        chunk_keyframes: list[float] = []
        try:
            intervals = self._keyframe_intervals_for_keeps(
                keeps_sorted,
                pad=0.0,
                max_intervals=160,
            )
            if intervals:
                chunk_kf_source = "rap"
                chunk_keyframes = scan_splice_safe_pts(self.input_path, use_cache=True, intervals=intervals)
                if not chunk_keyframes:
                    chunk_keyframes = ffprobe_keyframes(self.input_path, use_cache=True, intervals=intervals)
                    chunk_kf_source = "ffprobe_keyframe"
                self._log(
                    f"chunk_keyframes keeps mode={chunk_kf_source} count={len(chunk_keyframes)} "
                    f"enabled={'yes' if bool(chunk_keyframes) else 'no'}"
                )
            else:
                self._log("chunk_keyframes keeps skipped reason=dense_timeline")
        except Exception as e:
            self._log(f"chunk_keyframes keeps disabled ({e})")
        chunk_source_span = self._chunk_source_span_limit(plan_max_chunk_seconds, label="keeps")
        self._log(f"chunk_source_span_limit keeps={chunk_source_span:.1f}s")
        chunks = self._split_chunks(
            keeps_sorted,
            max_segments=plan_max_segments,
            max_chunk_seconds=plan_max_chunk_seconds,
            chunk_count=plan_chunks,
            keyframes=chunk_keyframes if chunk_keyframes else None,
            max_source_span=chunk_source_span,
        )
        if not chunks:
            raise RuntimeError("Chunking ha prodotto 0 chunk.")

        # Seek-friendly ordering
        chunks = sorted(chunks, key=lambda ch: float(ch[0].start) if ch else 0.0)
        chunk_durs = [sum(s.end - s.start for s in ch) for ch in chunks]
        threads_per = self._threads_per_process(workers)
        self._ffmpeg_threads_override = threads_per
        self._log(
            f"thread_budget={self._thread_budget()} threads_per_proc={threads_per} workers={workers}"
        )

        chunk_dir = tempfile.mkdtemp(prefix="auto_cutter_chunks_")
        temp_files.append(chunk_dir)
        cache_on = self._chunk_cache_enabled()
        use_ts = False
        if _env("AUTO_CUTTER_CHUNKS_TS", "0").strip() in ("1", "true", "True"):
            use_ts = True
        elif cache_on:
            # Cached intermediates are more robust in TS and can be reused as-is.
            use_ts = True
        elif len(chunks) > 120:
            use_ts = True
        chunk_ext = "ts" if use_ts else "mp4"
        chunk_files: list[str] = ["" for _ in range(len(chunks))]

        self._log(
            f"mode=chunked_parallel chunks={len(chunks)} workers={workers} total_kept={total_all:.3f}s "
            f"chunk_cache={'on' if cache_on else 'off'} container={chunk_ext}"
        )

        # Group identical chunks so we render once and reference many in final concat.
        groups: list[dict] = []
        sig_to_group: dict[str, int] = {}
        for i, ch in enumerate(chunks):
            key = self._chunk_signature_keeps(
                ch,
                force_fps=force_fps,
                container=chunk_ext,
                apply_audio_filters=None,
                video_only=False,
                src_path=self.input_path,
            )
            sig = f"k:{key}" if key else f"i:{i}"
            gi = sig_to_group.get(sig)
            if gi is None:
                sig_to_group[sig] = len(groups)
                groups.append({"key": key, "indices": [i]})
            else:
                groups[gi]["indices"].append(i)

        render_jobs: list[dict] = []
        cache_hit_chunks = 0
        cache_hit_media = 0.0
        dedup_reused = 0
        dedup_reused_media = 0.0
        pre_done_indices: list[int] = []

        for g in groups:
            idxs: list[int] = list(g.get("indices") or [])
            if not idxs:
                continue
            rep = int(idxs[0])
            key = str(g.get("key") or "")

            out_path = ""
            if key and cache_on:
                hit = self._chunk_cache_lookup(key, chunk_ext)
                if hit:
                    out_path = hit
                    cache_hit_chunks += len(idxs)
                    cache_hit_media += sum(chunk_durs[j] for j in idxs if 0 <= j < len(chunk_durs))
                    pre_done_indices.extend(idxs)
            if not out_path:
                if key and cache_on:
                    out_path = self._chunk_cache_key_path(key, chunk_ext)
                else:
                    out_path = os.path.join(chunk_dir, f"chunk_{rep:03d}.{chunk_ext}")
                render_jobs.append(
                    {
                        "rep": rep,
                        "indices": idxs,
                        "key": key,
                        "out_path": out_path,
                        "cacheable": bool(cache_on and key),
                    }
                )

            for j in idxs:
                chunk_files[j] = out_path
            if len(idxs) > 1:
                dedup_reused += (len(idxs) - 1)
                dedup_reused_media += sum(
                    chunk_durs[j] for j in idxs[1:] if 0 <= j < len(chunk_durs)
                )

        if cache_on:
            self._log(
                f"chunk_cache_plan groups={len(groups)} render_jobs={len(render_jobs)} "
                f"cache_hits={cache_hit_chunks} dedup_reused={dedup_reused} "
                f"cache_dir={self._chunk_cache_dir()}"
            )
        else:
            self._log(
                f"chunk_dedup_plan groups={len(groups)} render_jobs={len(render_jobs)} "
                f"dedup_reused={dedup_reused}"
            )
        if cache_hit_media > 0.0:
            self._record_reuse(cache_hit_chunks, cache_hit_media, category="chunk_dedup")
        if dedup_reused_media > 0.0:
            self._record_reuse(dedup_reused, dedup_reused_media, category="chunk_dedup")

        self.progress.emit(
            0,
            f"Rendering {len(render_jobs)} chunk job (from {len(chunks)} chunks, parallel={workers})...",
        )

        progress_lock = threading.Lock()
        chunk_out = [0.0 for _ in range(len(chunks))]
        chunk_last_pct = [-1 for _ in range(len(chunks))]
        last_emit = 0.0
        state = {"finished": 0}

        def _emit_global_locked() -> None:
            nonlocal last_emit
            now = time.monotonic()
            if now - last_emit < 0.5:
                return
            last_emit = now
            done = sum(chunk_out)
            pct = int(max(0.0, min(99.0, (done / total_all) * 100.0)))
            self.progress.emit(pct, f"Chunks {state['finished']}/{len(chunks)}  {pct}%")

        with progress_lock:
            for i in pre_done_indices:
                chunk_out[i] = chunk_durs[i]
            state["finished"] = len(pre_done_indices)
            _emit_global_locked()

        def _render_one(job: dict) -> dict:
            t0 = time.time()
            i = int(job.get("rep", 0))
            idxs = list(job.get("indices") or [i])
            cacheable = bool(job.get("cacheable"))
            key = str(job.get("key") or "")
            out_path = str(job.get("out_path") or "")
            render_out = out_path
            if cacheable:
                render_out = os.path.join(chunk_dir, f"chunk_render_{i:03d}.{chunk_ext}")
            cmd, _ = self._build_cmd_filter_concat_to(
                chunks[i],
                render_out,
                temp_files,
                tmp_dir=chunk_dir,
                force_fps=force_fps,
                container=chunk_ext,
            )
            self._log(
                f"chunk_start {i+1}/{len(chunks)} group={len(idxs)} "
                f"cacheable={'yes' if cacheable else 'no'}"
            )

            def _on_progress(out_time: float) -> None:
                if out_time < 0.0:
                    return
                if out_time > chunk_durs[i]:
                    out_time = chunk_durs[i]
                with progress_lock:
                    chunk_out[i] = out_time
                    pct = int(max(0.0, min(99.0, (out_time / max(1e-6, chunk_durs[i])) * 100.0)))
                    if pct != chunk_last_pct[i] and (pct % 5 == 0 or pct >= 99):
                        chunk_last_pct[i] = pct
                        self._log(
                            f"chunk_progress {i+1}/{len(chunks)} {pct}% "
                            f"{out_time:.1f}/{chunk_durs[i]:.1f}s"
                        )
                    _emit_global_locked()

            rc, err = self._run_ffmpeg_progress(cmd, on_progress=_on_progress)
            if rc != 0:
                err = (err or "").strip()
                raise RuntimeError(err or f"FFmpeg chunk {i} failed")
            final_out = render_out
            if cacheable:
                final_out = self._chunk_cache_store(key, chunk_ext, render_out)
            elapsed = max(0.0, time.time() - t0)
            try:
                with progress_lock:
                    self._chunk_times.append(elapsed)
            except Exception:
                pass
            self._record_render_work(chunk_durs[i], elapsed)
            return {
                "rep": i,
                "indices": idxs,
                "out_path": final_out,
            }

        try:
            if render_jobs:
                with ThreadPoolExecutor(max_workers=workers) as ex:
                    futs = [ex.submit(_render_one, j) for j in render_jobs]
                    try:
                        for fut in as_completed(futs):
                            try:
                                res = fut.result()
                            except Exception:
                                # Fail-fast: stop other ffmpeg processes to avoid apparent "hang".
                                self._terminate_all_procs()
                                for other in futs:
                                    other.cancel()
                                raise
                            i = int(res.get("rep", 0))
                            idxs = [int(x) for x in list(res.get("indices") or [i])]
                            out_path = str(res.get("out_path") or "")
                            with progress_lock:
                                for j in idxs:
                                    if 0 <= j < len(chunk_out):
                                        chunk_out[j] = chunk_durs[j]
                                        chunk_files[j] = out_path
                                state["finished"] += len(idxs)
                                _emit_global_locked()
                            pct = int(max(0.0, min(99.0, (sum(chunk_out) / total_all) * 100.0)))
                            self._log(
                                f"chunk_done {i+1}/{len(chunks)} rendered={len(idxs)} "
                                f"pct={pct}% path={out_path}"
                            )
                            self.progress.emit(pct, f"Chunk {state['finished']}/{len(chunks)} completati...")
                    finally:
                        for fut in futs:
                            fut.cancel()
        finally:
            self._ffmpeg_threads_override = None

        self.progress.emit(99, "Concatenazione finale...")
        self._concat_chunks_copy(chunk_files, temp_files, use_ts=use_ts)

    def _build_filter_complex_multi_output(
        self,
        groups: list[list[Segment]],
        fps: float,
        has_audio: bool,
    ) -> tuple[str, list[tuple[str, str]]]:
        parts: list[str] = []
        out_labels: list[tuple[str, str]] = []
        force_async_audio = bool(self._needs_audio_processing())
        for gi, group in enumerate(groups):
            for si, seg in enumerate(group):
                v_chain = f"[0:v]trim=start={seg.start}:end={seg.end},setpts=PTS-STARTPTS"
                if fps and fps > 1.0:
                    v_chain += f",fps={fps:.3f}"
                parts.append(f"{v_chain}[v{gi}_{si}];")
                if has_audio:
                    parts.append(
                        f"[0:a]atrim=start={seg.start}:end={seg.end},asetpts=PTS-STARTPTS[a{gi}_{si}];"
                    )
                else:
                    dur = max(0.0, float(seg.end - seg.start))
                    parts.append(
                        f"anullsrc=channel_layout=stereo:sample_rate=48000,"
                        f"atrim=start=0:end={dur:.6f},asetpts=PTS-STARTPTS[a{gi}_{si}];"
                    )
            concat_inputs = "".join([f"[v{gi}_{si}][a{gi}_{si}]" for si in range(len(group))])
            parts.append(f"{concat_inputs}concat=n={len(group)}:v=1:a=1[outv{gi}][outa{gi}0];")
            if force_async_audio:
                parts.append(f"[outa{gi}0]aresample=async=1:first_pts=0[outa{gi}];")
            else:
                parts.append(f"[outa{gi}0]anull[outa{gi}];")
            out_labels.append((f"outv{gi}", f"outa{gi}"))
        return "".join(parts), out_labels

    def _run_single_process_macro(
        self,
        keeps_sorted: list[Segment],
        temp_files: list[str],
        force_fps: float,
    ) -> None:
        self._export_method_used = "single_process_macro"
        if not keeps_sorted:
            raise RuntimeError("Macro export: empty keeps.")

        # Choose a small number of macro outputs to keep command size reasonable
        target_groups = min(8, max(2, int(math.ceil(len(keeps_sorted) / 40.0))))
        groups = self._split_chunks(
            keeps_sorted,
            max_segments=400,
            max_chunk_seconds=600.0,
            chunk_count=target_groups,
        )
        if not groups or len(groups) > 8:
            raise RuntimeError("Macro export: too many groups.")

        has_audio = True
        try:
            has_audio = has_audio_stream(self.input_path)
        except Exception:
            has_audio = True

        filt, out_labels = self._build_filter_complex_multi_output(groups, force_fps, has_audio)
        macro_dir = tempfile.mkdtemp(prefix="auto_cutter_macro_")
        temp_files.append(macro_dir)
        macro_ext = "ts"
        macro_files = [os.path.join(macro_dir, f"macro_{i:03d}.{macro_ext}") for i in range(len(groups))]

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8", newline="\n", dir=macro_dir
        ) as f:
            f.write(filt)
            f.write("\n")
            filter_file = f.name
        temp_files.append(filter_file)

        cmd = [
            self.ffmpeg_path, "-hide_banner", "-v", "error", "-y", "-nostdin",
        ]
        cmd += self._hwaccel_args_for_filtergraph()
        cmd += ["-i", self.input_path, "-filter_complex_script", filter_file]

        for i, (outv, outa) in enumerate(out_labels):
            cmd += [
                "-map", f"[{outv}]", "-map", f"[{outa}]",
                *self._ffmpeg_thread_args(),
                *self._video_args(),
                "-c:a", "aac", "-b:a", "320k",
                "-max_muxing_queue_size", "4096",
                "-f", "mpegts",
                macro_files[i],
            ]

        self._log(f"ffmpeg_cmd_macro={self._fmt_cmd(cmd)}")
        rc, _out, err = self._run_ffmpeg(cmd)
        if rc != 0:
            err = (err or "").strip()
            raise RuntimeError(err or "FFmpeg macro export failed.")

        self._concat_chunks_copy(macro_files, temp_files, use_ts=True)

    # -----------------------------
    # Concat demuxer (unused currently, kept for future)
    # -----------------------------
    def _build_cmd_concat_demuxer(
        self,
        keeps_sorted: list[Segment],
        temp_files: list[str],
        src_path: str | None = None,
        force_fps: float = 0.0,
    ) -> tuple[list[str], float]:
        """
        Metodo fast robusto:
        - crea un file .ffconcat con inpoint/outpoint per ogni segmento
        - ffmpeg legge quel "EDL" e ricodifica una sola volta (qualità alta)
        """
        def _q(path: str) -> str:
            p = os.path.abspath(path).replace("\\", "/")
            p = p.replace("'", "''")
            return f"'{p}'"

        total = sum(s.dur for s in keeps_sorted)
        total = max(1e-6, total)

        ascii_src = None
        try:
            tmp_dir = tempfile.gettempdir()
            ascii_src = os.path.join(tmp_dir, "auto_cutter_input_ascii.mp4")
            if os.path.exists(ascii_src):
                try:
                    os.remove(ascii_src)
                except Exception:
                    pass
            os.link(self.input_path, ascii_src)
        except Exception:
            ascii_src = None

        src_for_concat = ascii_src if ascii_src else (src_path or self.input_path)
        if ascii_src:
            temp_files.append(ascii_src)

        lines = []
        for seg in keeps_sorted:
            lines.append(f"file {_q(src_for_concat)}")
            lines.append(f"inpoint {seg.start:.6f}")
            lines.append(f"outpoint {seg.end:.6f}")

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".ffconcat", delete=False, encoding="utf-8", newline="\n"
        ) as f:
            f.write("ffconcat version 1.0\n")
            f.write("\n".join(lines))
            f.write("\n")
            concat_file = f.name

        temp_files.append(concat_file)

        self._log(
            f"mode=concat_demuxer {self._summarize_keeps(keeps_sorted)} "
            f"total_kept={total:.3f}s src={src_for_concat} fps={float(force_fps or 0.0):.3f}"
        )

        cmd = [
            self.ffmpeg_path, "-hide_banner", "-v", "error", "-y", "-nostdin",
            "-thread_queue_size", str(self._input_thread_queue_size()),
        ]

        cmd += self._hwaccel_args()

        cmd += [
            "-f", "concat",
            "-safe", "0",
            "-i", concat_file,
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-dn", "-sn",
            *self._drop_chapters_args(),
            *self._ffmpeg_thread_args(),
            *self._video_args(),
        ]

        if force_fps and force_fps > 1.0:
            cmd += ["-fps_mode", "cfr", "-r", f"{force_fps:.3f}"]

        audio_processing = self._needs_audio_processing()
        audio_copy_ok = False
        try:
            if not audio_processing and src_for_concat:
                audio_copy_ok = self._smart_audio_copy_ok(str(src_for_concat))
        except Exception:
            audio_copy_ok = False
        self._log_audio_mode(
            "concat_demuxer",
            audio_processing=audio_processing,
            audio_copy_ok=audio_copy_ok,
            filters_in_segments=False,
        )

        if audio_copy_ok:
            cmd += ["-c:a", "copy"]
        else:
            cmd += ["-c:a", "aac", "-b:a", "320k"]

        # Keep audio/video aligned after cut points (only when re-encoding)
        try:
            has_audio = bool(src_for_concat) and has_audio_stream(str(src_for_concat))
        except Exception:
            has_audio = False
        if has_audio and not audio_copy_ok:
            af = self._build_audio_filter_chain(include_legacy_gain_limiter=True)
            async_af = "aresample=async=1:first_pts=0"
            af = f"{af},{async_af}" if af else async_af
            cmd += ["-af", af]

        cmd += [
            "-max_muxing_queue_size", "4096",
            "-movflags", "+faststart",
            "-use_editlist", "0",
            "-avoid_negative_ts", "make_zero",
            "-progress", "pipe:2",
            "-nostats",
            self.output_path
        ]
        self._log(f"concat_list={concat_file} entries={len(keeps_sorted)}")
        self._log(f"ffmpeg_cmd_concat_demuxer={self._fmt_cmd(cmd)}")

        return cmd, total

    def _segments_single_input(self, segments: list[dict]) -> bool:
        if not segments:
            return False
        for s in segments:
            v_idx = s.get("v_idx", None)
            a_idx = s.get("a_idx", None)
            if v_idx is None or a_idx is None:
                return False
            try:
                v_idx = int(v_idx)
                a_idx = int(a_idx)
            except Exception:
                return False
            if v_idx != 0 or a_idx != 0:
                return False
            try:
                v_in = float(s.get("v_in", 0.0) or 0.0)
                v_out = float(s.get("v_out", 0.0) or 0.0)
                a_in = float(s.get("a_in", 0.0) or 0.0)
                a_out = float(s.get("a_out", 0.0) or 0.0)
            except Exception:
                return False
            # Require audio/video alignment for concat demuxer
            if abs(v_in - a_in) > 1e-3 or abs(v_out - a_out) > 1e-3:
                return False
        return True

    def _segments_single_input_idx(self, segments: list[dict]) -> int | None:
        """
        Return input index if all segments use the same v_idx/a_idx and are A/V aligned.
        """
        if not segments:
            return None
        idx = None
        for s in segments:
            v_idx = s.get("v_idx", None)
            a_idx = s.get("a_idx", None)
            if v_idx is None or a_idx is None:
                return None
            try:
                v_idx = int(v_idx)
                a_idx = int(a_idx)
            except Exception:
                return None
            if v_idx != a_idx:
                return None
            try:
                v_in = float(s.get("v_in", 0.0) or 0.0)
                v_out = float(s.get("v_out", 0.0) or 0.0)
                a_in = float(s.get("a_in", 0.0) or 0.0)
                a_out = float(s.get("a_out", 0.0) or 0.0)
            except Exception:
                return None
            if abs(v_in - a_in) > 1e-3 or abs(v_out - a_out) > 1e-3:
                return None
            if idx is None:
                idx = v_idx
            elif idx != v_idx:
                return None
        return idx

    def _segments_base_fps(self, segments: list[dict]) -> float:
        """
        Pick a stable FPS from the first video input referenced by segments.
        Falls back to input_paths[0] if needed.
        """
        try:
            for s in segments or []:
                v_idx = s.get("v_idx", None)
                if v_idx is None:
                    continue
                try:
                    v_idx = int(v_idx)
                except Exception:
                    continue
                if 0 <= v_idx < len(self.input_paths):
                    fps = float(self._probe_fps(self.input_paths[v_idx]) or 0.0)
                    if fps > 1.0:
                        return fps
        except Exception:
            pass
        try:
            if self.input_paths:
                return float(self._probe_fps(self.input_paths[0]) or 0.0)
        except Exception:
            pass
        return 0.0

    def _segments_to_keeps_in_order(self, segments: list[dict], fps: float = 0.0) -> list[Segment]:
        keeps: list[Segment] = []
        frame = 0.0
        eps = 0.0
        if fps and fps > 1e-3:
            frame = 1.0 / float(fps)
            eps = frame * 1e-3

        def _snap_in(t: float) -> float:
            if frame <= 0.0:
                return t
            # Conservative start snap: never pull frames from before requested in-point.
            return math.ceil((t - eps) / frame) * frame

        def _snap_out(t: float) -> float:
            if frame <= 0.0:
                return t
            # Conservative end snap: never pull frames from after requested out-point.
            return math.floor((t + eps) / frame) * frame

        for s in segments:
            try:
                v_in = float(s.get("v_in", 0.0) or 0.0)
                v_out = float(s.get("v_out", 0.0) or 0.0)
            except Exception:
                continue
            if frame > 0.0:
                v_in = max(0.0, _snap_in(v_in))
                v_out = max(v_in + frame, _snap_out(v_out))
            if v_out > v_in:
                keeps.append(Segment(v_in, v_out))
        return keeps

    # -----------------------------
    # Smart render (copy + re-encode edges)
    # -----------------------------
    def _probe_video_codec(self, path: str) -> str:
        try:
            ffprobe = self._find_ffprobe()
            if not ffprobe:
                return ""
            cmd = [
                ffprobe, "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=codec_name",
                "-of", "default=nk=1:nw=1",
                path,
            ]
            p = run_no_window(cmd, capture_output=True, text=True)
            if p.returncode != 0:
                return ""
            return (p.stdout or "").strip().splitlines()[0].strip().lower()
        except Exception:
            return ""

    def _probe_audio_codec(self, path: str) -> str:
        try:
            ffprobe = self._find_ffprobe()
            if not ffprobe:
                return ""
            cmd = [
                ffprobe,
                "-v", "error",
                "-select_streams", "a:0",
                "-show_entries", "stream=codec_name",
                "-of", "default=nk=1:nw=1",
                path,
            ]
            p = run_no_window(cmd, capture_output=True, text=True)
            if p.returncode != 0:
                return ""
            return (p.stdout or "").strip().splitlines()[0].strip().lower()
        except Exception:
            return ""

    def _smart_audio_copy_ok(self, src_path: str) -> bool:
        try:
            if not has_audio_stream(src_path):
                return False
        except Exception:
            return False
        codec = self._probe_audio_codec(src_path)
        # MP4-friendly copy policy: only keep AAC in copy.
        return codec in ("aac",)

    def _smart_render_supported(self, src_path: str) -> bool:
        src_codec = self._probe_video_codec(src_path)
        if not src_codec:
            return False
        if self.codec in ("h264_amf", "libx264") and src_codec == "h264":
            return True
        if self.codec == "hevc_amf" and src_codec in ("hevc", "h265"):
            return True
        if self.codec == "av1_amf" and src_codec == "av1":
            return True
        return False

    def _median(self, xs: list[float]) -> float:
        xs = [float(x) for x in xs if x and x > 1e-6]
        if not xs:
            return 0.0
        xs.sort()
        n = len(xs)
        if n % 2 == 1:
            return xs[n // 2]
        return 0.5 * (xs[n // 2 - 1] + xs[n // 2])

    def _splice_epsilon(self, fps: float) -> float:
        """
        Epsilon < 1 frame usata per rendere i segmenti "half-open" e prevenire 1-frame overlap.
        """
        try:
            f = float(fps)
        except Exception:
            f = 0.0
        if f > 1.0:
            # ~0.45 frame, cap a 10ms
            return min(0.01, 0.45 / f)
        return 0.001

    def _filter_splice_keyframes(self, keyframes: list[float]) -> list[float]:
        """
        Tenta di preferire keyframe "veri GOP boundary" filtrando keyframe troppo ravvicinati
        (spesso scenecut I-frame in open GOP).
        """
        if not keyframes or len(keyframes) < 4:
            return keyframes

        kfs = [float(t) for t in keyframes if t is not None]
        kfs.sort()

        diffs = [(b - a) for a, b in zip(kfs, kfs[1:]) if (b - a) > 1e-3]
        med = self._median(diffs)
        if med <= 0.0:
            return kfs

        # Se il GOP medio è ~2s, min_gap ~1.2s elimina i keyframe extra "scenecut"
        min_gap = max(0.25, 0.60 * med)

        out = [kfs[0]]
        last = kfs[0]
        for t in kfs[1:]:
            if (t - last) >= min_gap:
                out.append(t)
                last = t

        # non over-filtrare: se restano troppo pochi, torna all'originale
        return out if len(out) >= 2 else kfs


    def _smart_split_keep(
        self,
        seg: Segment,
        keyframes: list[float],
        pad: float,
        force_fps: float = 0.0,
    ) -> list[tuple[str, float, float]]:
        a = float(seg.start)
        b = float(seg.end)
        if b <= a:
            return []
        if not keyframes or (b - a) <= 2.0 * pad:
            return [("reencode", a, b)]

        eps = self._splice_epsilon(force_fps)

        head_end = min(b, a + pad)
        tail_start = max(a, b - pad)
        # middle must be aligned to keyframes
        i_start = bisect.bisect_left(keyframes, head_end)
        i_end = bisect.bisect_right(keyframes, tail_start) - 1
        if i_start >= len(keyframes) or i_end < 0 or i_start >= i_end:
            return [("reencode", a, b)]

        mid_start = float(keyframes[i_start])
        mid_end = float(keyframes[i_end])
        if mid_end <= mid_start + 1e-6:
            return [("reencode", a, b)]

        # If boundaries are too close, full re-encode is safer than tiny stitch regions.
        edge_min = max(1e-6, 0.5 * eps)
        if (mid_start - a) <= edge_min or (b - mid_end) <= edge_min:
            return [("reencode", a, b)]

        out: list[tuple[str, float, float]] = []
        # Exact contiguous boundaries: no intentional temporal gaps around cut points.
        head_re_end = float(mid_start)  # reencode: [a, mid_start]
        copy_end = float(mid_end)       # copy:     [mid_start, mid_end]

        if head_re_end > a + 1e-6:
            out.append(("reencode", a, head_re_end))
        if copy_end > mid_start + 1e-6:
            out.append(("copy", mid_start, copy_end))

        # Tail re-encode starts at exact splice point.
        if b > mid_end + 1e-6:
            out.append(("reencode", mid_end, b))
        else:
            # No tail re-encode: extend copy to end.
            if out and out[-1][0] == "copy" and b > out[-1][2] + 1e-6:
                out[-1] = ("copy", out[-1][1], b)

        if not out:
            return [("reencode", a, b)]
        return out

    def _build_cmd_smart_segment(
        self,
        src_path: str,
        out_path: str,
        start: float,
        end: float,
        mode: str,
        audio_copy: bool,
        audio_filters: str = "",
        container: str = "mp4",
        video_bsf: list[str] | None = None,
        video_only: bool = False,
        force_fps: float = 0.0,
    ) -> list[str]:
        dur = max(0.0, float(end) - float(start))
        if mode == "copy":
            # fast seek (keyframe-aligned)
            copy_ss = float(start)
            copy_dur = max(1e-6, dur)
            seek_eps = self._copy_seek_epsilon(force_fps)
            tail_eps = self._cut_boundary_epsilon(force_fps)
            trim_total = max(0.0, seek_eps) + max(0.0, tail_eps)
            if trim_total > 0.0 and copy_dur > (trim_total + 1e-6):
                copy_ss = max(0.0, copy_ss + seek_eps)
                copy_dur = max(1e-6, copy_dur - trim_total)
            elif seek_eps > 0.0 and copy_dur > (seek_eps + 1e-6):
                copy_ss = max(0.0, copy_ss + seek_eps)
                copy_dur = max(1e-6, copy_dur - seek_eps)
            cmd = [
                self.ffmpeg_path, "-hide_banner", "-v", "error", "-y", "-nostdin",
                "-fflags", "+genpts",
                *self._hwaccel_args(),
                "-ss", f"{copy_ss:.6f}",
                "-i", src_path,
                "-t", f"{copy_dur:.6f}",
                "-map", "0:v:0",
                "-dn", "-sn",
                *self._drop_chapters_args(),
                *self._ffmpeg_thread_args(),
            ]
        else:
            # Accurate+fast seek for re-encode:
            # use a coarse pre-seek before input, then a fine seek after input.
            # This avoids decoding from 0 for late timeline cuts (e.g. 3h sources).
            try:
                seek_pad = float(_env("AUTO_CUTTER_REENCODE_PRESEEK_PAD", "2.0").strip() or "2.0")
            except Exception:
                seek_pad = 2.0
            if seek_pad < 0.0:
                seek_pad = 0.0
            coarse_ss = max(0.0, float(start) - float(seek_pad))
            fine_ss = max(0.0, float(start) - float(coarse_ss))
            cmd = [
                self.ffmpeg_path, "-hide_banner", "-v", "error", "-y", "-nostdin",
                *self._hwaccel_args(),
            ]
            if coarse_ss > 1e-6:
                cmd += ["-ss", f"{coarse_ss:.6f}"]
            cmd += [
                "-i", src_path,
            ]
            if fine_ss > 1e-6:
                cmd += ["-ss", f"{fine_ss:.6f}"]
            cmd += [
                "-t", f"{dur:.6f}",
                "-map", "0:v:0",
                "-dn", "-sn",
                *self._drop_chapters_args(),
                *self._ffmpeg_thread_args(),
            ]

        if not video_only:
            cmd += ["-map", "0:a:0?"]
        else:
            cmd += ["-an"]

        if mode == "copy":
            cmd += ["-c:v", "copy"]
            if video_bsf:
                cmd += list(video_bsf)
        else:
            preset = "cut_hq" if self._use_cut_hq_for_reencode(dur) else "balanced"
            cmd += [*self._video_args(preset)]
            if force_fps and force_fps > 1.0:
                cmd += ["-fps_mode", "cfr", "-r", f"{force_fps:.3f}"]

        if not video_only:
            if audio_copy:
                cmd += ["-c:a", "copy"]
            else:
                cmd += ["-c:a", "aac", "-b:a", "320k"]
                if audio_filters:
                    cmd += ["-af", audio_filters]

        if container == "ts":
            cmd += [
                "-avoid_negative_ts", "make_zero",
                "-max_interleave_delta", "0",
                "-muxpreload", "0",
                "-muxdelay", "0",
                "-f", "mpegts",
                out_path,
            ]
        else:
            cmd += ["-movflags", "+faststart", "-use_editlist", "0", "-avoid_negative_ts", "make_zero", out_path]
        return cmd

    def _run_smart_render(
        self,
        keeps_sorted: list[Segment],
        temp_files: list[str],
        src_path: str,
    ) -> None:
        self._export_method_used = "smart_render"
        if not self._smart_render_supported(src_path):
            raise RuntimeError("Smart render not supported for this source/codec.")

        pad = float(self.smart_render_pad)
        total_kept = sum(max(0.0, s.end - s.start) for s in keeps_sorted)
        total_kept = max(1e-6, total_kept)
        max_pieces = self._smart_max_pieces(len(keeps_sorted), total_kept)
        self._log(f"smart_render_max_pieces={max_pieces}")
        # Cheap pre-check to avoid expensive keyframe scan if hybrid would likely explode in pieces.
        expected_pieces = 0
        for k in keeps_sorted:
            dur = float(k.end - k.start)
            if dur <= (2.0 * pad + 0.02):
                expected_pieces += 1
            else:
                expected_pieces += 3
        if expected_pieces > (max_pieces * 1.2):
            self._log(
                f"smart_render_skip expected_pieces={expected_pieces} max_pieces={max_pieces}"
            )
            raise _SmartHybridFallback("smart_render_skip")

        self._log("keyframes_scan start")
        t0 = time.time()
        intervals = self._keyframe_intervals_for_keeps(keeps_sorted, pad)
        keyframes_source = "rap"
        keyframes = scan_splice_safe_pts(src_path, use_cache=True, intervals=intervals)
        if not keyframes:
            keyframes = ffprobe_keyframes(src_path, use_cache=True, intervals=intervals)
            keyframes_source = "ffprobe_keyframe"
        self._log(
            f"keyframes_scan done frames={len(keyframes)} source={keyframes_source} "
            f"took={time.time() - t0:.2f}s"
        )
        if not keyframes:
            raise RuntimeError("No keyframes found for smart render.")

        force_fps = 0.0
        try:
            force_fps = float(self._probe_fps(src_path) or 0.0)
        except Exception:
            force_fps = 0.0

        # Filtra keyframe troppo ravvicinati (scenecut/open GOP) -> splice più pulito
        if keyframes_source != "rap":
            kf_before = len(keyframes)
            keyframes = self._filter_splice_keyframes(keyframes)
            if len(keyframes) != kf_before:
                self._log(f"keyframes_filtered {len(keyframes)}/{kf_before} (splice-safe)")

        # build smart segments
        segments: list[tuple[str, float, float]] = []
        for k in keeps_sorted:
            segments.extend(self._smart_split_keep(k, keyframes, pad, force_fps=force_fps))
        if not segments:
            raise RuntimeError("No smart render segments generated.")

        total = sum(max(0.0, e - s) for _m, s, e in segments)
        total = max(1e-6, total)
        copy_dur = sum(max(0.0, e - s) for m, s, e in segments if m == "copy")
        reenc_dur = max(0.0, total - copy_dur)
        copy_pct = (copy_dur / total) * 100.0 if total > 0.0 else 0.0
        self._log(
            f"smart_render_stats segments={len(segments)} copy={copy_dur:.3f}s "
            f"reencode={reenc_dur:.3f}s copy_pct={copy_pct:.1f}%"
        )

        audio_processing = self._needs_audio_processing()
        audio_copy_ok = self._smart_audio_copy_ok(src_path)
        apply_audio_filters_in_segments = bool(audio_processing)
        segment_audio_copy = bool((not audio_processing) and audio_copy_ok)
        audio_mode = self._log_audio_mode(
            "smart_render",
            audio_processing=audio_processing,
            audio_copy_ok=audio_copy_ok,
            filters_in_segments=apply_audio_filters_in_segments,
        )

        audio_filters = ""
        if apply_audio_filters_in_segments:
            af = self._build_audio_filter_chain(include_legacy_gain_limiter=True)
            async_af = "aresample=async=1:first_pts=0"
            audio_filters = f"{af},{async_af}" if af else async_af

        seg_dir = tempfile.mkdtemp(prefix="auto_cutter_smart_")
        temp_files.append(seg_dir)

        seg_files: list[str] = []
        done = 0.0
        seg_ext = "ts"
        video_bsf = self._ts_video_bsf(src_path)
        for i, (mode, s, e) in enumerate(segments):
            out_path = os.path.join(seg_dir, f"seg_{i:04d}.{seg_ext}")
            cmd = self._build_cmd_smart_segment(
                src_path, out_path, s, e, mode,
                audio_copy=segment_audio_copy,
                audio_filters=audio_filters,
                container=seg_ext,
                video_bsf=video_bsf if mode == "copy" else None,
                video_only=False,
                force_fps=force_fps,
            )
            self._log(f"smart_render_segment {i+1}/{len(segments)} mode={mode} {s:.3f}-{e:.3f}")
            rc, _out, err = self._run_ffmpeg(cmd)
            if rc != 0:
                err = (err or "").strip()
                raise RuntimeError(err or f"FFmpeg smart segment {i} failed")
            seg_files.append(out_path)
            done += max(0.0, e - s)
            pct = int(max(0.0, min(99.0, (done / total) * 100.0)))
            self.progress.emit(pct, f"Smart render {pct}%")

        # concat lossless (TS) then remux to MP4
        group_size = 50
        if len(seg_files) > 1200:
            group_size = 100
        elif len(seg_files) > 400:
            group_size = 60
        concat_ts = self._tree_concat_ts(seg_files, temp_files, group_size, label="smart")

        t_remux = time.time()
        self._remux_mp4_from_ts(
            concat_ts,
            self.output_path,
            audio_mode,
            src_probe_path=src_path,
        )
        self._log(f"smart_remux done elapsed={time.time() - t_remux:.2f}s")

    def _run_smart_hybrid(
        self,
        keeps_sorted: list[Segment],
        temp_files: list[str],
        src_path: str,
        out_path: str | None = None,
        emit_progress: bool = True,
        progress_cb: Callable[[float, float], None] | None = None,
        segments_context: bool = False,
    ) -> None:
        self._export_method_used = "smart_hybrid"
        if not self._smart_render_supported(src_path):
            raise RuntimeError("Smart hybrid not supported for this source/codec.")

        pad = float(self.smart_render_pad)
        total_kept = sum(max(0.0, s.end - s.start) for s in keeps_sorted)
        total_kept = max(1e-6, total_kept)

        def _notify_piece_progress(done_seconds: float) -> None:
            if progress_cb is None:
                return
            try:
                progress_cb(max(0.0, min(total_kept, float(done_seconds))), total_kept)
            except Exception:
                pass
        max_pieces = self._smart_max_pieces(len(keeps_sorted), total_kept)
        self._log(f"smart_hybrid_max_pieces={max_pieces}")

        self._log("keyframes_scan start")
        t0 = time.time()
        intervals = self._keyframe_intervals_for_keeps(keeps_sorted, pad)
        keyframes_source = "rap"
        keyframes = scan_splice_safe_pts(src_path, use_cache=True, intervals=intervals)
        if not keyframes:
            keyframes = ffprobe_keyframes(src_path, use_cache=True, intervals=intervals)
            keyframes_source = "ffprobe_keyframe"
        self._log(
            f"keyframes_scan done frames={len(keyframes)} source={keyframes_source} "
            f"took={time.time() - t0:.2f}s"
        )
        if not keyframes:
            raise RuntimeError("No keyframes found for hybrid render.")

        force_fps = 0.0
        try:
            force_fps = float(self._probe_fps(src_path) or 0.0)
        except Exception:
            force_fps = 0.0

        # Filtra keyframe troppo ravvicinati (scenecut/open GOP) -> splice più pulito
        if keyframes_source != "rap":
            kf_before = len(keyframes)
            keyframes = self._filter_splice_keyframes(keyframes)
            if len(keyframes) != kf_before:
                self._log(f"keyframes_filtered {len(keyframes)}/{kf_before} (splice-safe)")

        # Hybrid clustering params (dense short cuts)
        short_max = max(2.5, pad * 3.0)
        gap_max = max(0.5, pad * 0.75)
        cluster_max_span = max(12.0, short_max * 6.0)
        cluster_min_keeps = 2
        non_monotonic_timeline = False
        for j in range(1, len(keeps_sorted)):
            try:
                if float(keeps_sorted[j].start) + 1e-6 < float(keeps_sorted[j - 1].start):
                    non_monotonic_timeline = True
                    break
            except Exception:
                continue

        # Build clusters of short, dense keeps (by index)
        clusters: list[tuple[int, int]] = []
        cur: list[int] = []
        cur_start_idx = 0

        def _flush_cluster() -> None:
            nonlocal cur
            if len(cur) >= cluster_min_keeps:
                clusters.append((cur[0], cur[-1]))
            cur = []

        if non_monotonic_timeline:
            self._log("smart_hybrid_timeline_non_monotonic=yes clusters=off")
        else:
            for i, k in enumerate(keeps_sorted):
                dur = float(k.end - k.start)
                if dur > short_max:
                    _flush_cluster()
                    continue
                if not cur:
                    cur = [i]
                    cur_start_idx = i
                    continue
                gap = float(k.start - keeps_sorted[cur[-1]].end)
                span = float(k.end - keeps_sorted[cur_start_idx].start)
                if gap <= gap_max and span <= cluster_max_span:
                    cur.append(i)
                else:
                    _flush_cluster()
                    cur = [i]
                    cur_start_idx = i

            _flush_cluster()

        cluster_by_start = {s: e for s, e in clusters}
        clusters_exist = bool(clusters)
        jump_guard_seconds = self._jump_guard_seconds() if non_monotonic_timeline else 0.0
        self._log(
            f"smart_hybrid_jump_guard_seconds={jump_guard_seconds:.3f}"
        )

        self._log(
            "smart_hybrid_params "
            f"short_max={short_max:.2f}s gap_max={gap_max:.2f}s "
            f"cluster_max_span={cluster_max_span:.2f}s cluster_min={cluster_min_keeps} "
            f"clusters={len(clusters)}"
        )

        # Build output pieces in timeline order
        pieces: list[tuple[str, object]] = []
        copy_dur = 0.0
        reenc_dur = 0.0

        i = 0
        while i < len(keeps_sorted):
            if i in cluster_by_start:
                end_idx = cluster_by_start[i]
                ckeeps = keeps_sorted[i:end_idx + 1]
                c_dur = sum(max(0.0, s.end - s.start) for s in ckeeps)
                pieces.append(("cluster", ckeeps))
                reenc_dur += c_dur
                i = end_idx + 1
                continue

            local_pad = pad
            if jump_guard_seconds > 0.0 and i > 0:
                cur_start = float(keeps_sorted[i].start)
                prev_start = float(keeps_sorted[i - 1].start)
                if cur_start + 1e-6 < prev_start:
                    local_pad = max(local_pad, jump_guard_seconds)
                    self._log(
                        f"smart_hybrid_jump_guard_apply idx={i+1} "
                        f"prev_start={prev_start:.3f}s cur_start={cur_start:.3f}s "
                        f"pad={local_pad:.3f}s"
                    )

            segs = self._smart_split_keep(keeps_sorted[i], keyframes, local_pad, force_fps=force_fps)
            for mode, s, e in segs:
                d = max(0.0, e - s)
                pieces.append((mode, (float(s), float(e))))
                if mode == "copy":
                    copy_dur += d
                else:
                    reenc_dur += d
            i += 1

        if not pieces:
            raise RuntimeError("No hybrid segments generated.")

        micro_dur = 0.05
        if force_fps and force_fps > 1.0:
            micro_dur = max(0.03, (2.0 / float(force_fps)))

        def _merge_micro_pieces(pcs: list[tuple[str, object]], micro: float) -> list[tuple[str, object]]:
            if not pcs:
                return pcs
            out: list[tuple[str, object]] = []
            i = 0
            while i < len(pcs):
                kind, payload = pcs[i]
                if kind == "cluster":
                    out.append((kind, payload))
                    i += 1
                    continue
                if i + 2 < len(pcs):
                    k1, p1 = pcs[i]
                    k2, p2 = pcs[i + 1]
                    k3, p3 = pcs[i + 2]
                    if k1 != "cluster" and k2 != "cluster" and k3 != "cluster":
                        if k1 == "copy" and k2 != "copy" and k3 == "copy":
                            s1, e1 = p1  # type: ignore[misc]
                            s2, e2 = p2  # type: ignore[misc]
                            s3, e3 = p3  # type: ignore[misc]
                            if (float(e2) - float(s2)) <= micro:
                                out.append(("reencode", (float(s1), float(e3))))
                                i += 3
                                continue
                        if k1 != "copy" and k2 == "copy" and k3 != "copy":
                            s1, e1 = p1  # type: ignore[misc]
                            s2, e2 = p2  # type: ignore[misc]
                            s3, e3 = p3  # type: ignore[misc]
                            if (float(e2) - float(s2)) <= micro:
                                out.append(("reencode", (float(s1), float(e3))))
                                i += 3
                                continue
                out.append((kind, payload))
                i += 1
            return out

        def _piece_start_time(k: str, payload: object) -> float | None:
            if k == "cluster":
                try:
                    ckeeps = payload  # type: ignore[assignment]
                    if not ckeeps:
                        return None
                    return float(ckeeps[0].start)
                except Exception:
                    return None
            try:
                s, _e = payload  # type: ignore[misc]
                return float(s)
            except Exception:
                return None

        def _merge_micro_monotonic_blocks(
            pcs: list[tuple[str, object]],
            micro: float,
        ) -> tuple[list[tuple[str, object]], int, int]:
            if not pcs:
                return pcs, 0, 0

            out: list[tuple[str, object]] = []
            block: list[tuple[str, object]] = []
            prev_start: float | None = None
            blocks = 0
            merged_delta = 0

            def _flush_block() -> None:
                nonlocal block, prev_start, blocks, merged_delta
                if not block:
                    return
                before = len(block)
                merged = _merge_micro_pieces(block, micro)
                out.extend(merged)
                blocks += 1
                merged_delta += max(0, before - len(merged))
                block = []
                prev_start = None

            for k, payload in pcs:
                st = _piece_start_time(k, payload)
                if st is None:
                    _flush_block()
                    out.append((k, payload))
                    continue
                if prev_start is not None and (st + 1e-6) < prev_start:
                    _flush_block()
                block.append((k, payload))
                prev_start = st

            _flush_block()
            return out, blocks, merged_delta

        if non_monotonic_timeline:
            pieces_before = len(pieces)
            pieces, mono_blocks, merged_delta = _merge_micro_monotonic_blocks(pieces, micro_dur)
            self._log(
                "smart_hybrid_merge_micro "
                "mode=monotonic_blocks "
                f"blocks={mono_blocks} pieces_before={pieces_before} "
                f"after={len(pieces)} merged_delta={merged_delta} micro={micro_dur:.3f}s"
            )
        else:
            pieces_before = len(pieces)
            pieces = _merge_micro_pieces(pieces, micro_dur)
            if len(pieces) != pieces_before:
                self._log(
                    f"smart_hybrid_merge_micro pieces_before={pieces_before} "
                    f"after={len(pieces)} micro={micro_dur:.3f}s"
                )

        stitch_guard_seconds = self._hybrid_stitch_guard_seconds(segments_context, force_fps)
        stitch_guard_min_copy = self._hybrid_stitch_guard_min_copy()
        self._log(
            f"smart_hybrid_stitch_guard_seconds={stitch_guard_seconds:.3f} "
            f"min_copy={stitch_guard_min_copy:.3f}s"
        )
        if stitch_guard_seconds > 1e-6:
            pieces_before = len(pieces)
            pieces, stitch_touched, stitch_added = self._apply_hybrid_stitch_guard(
                pieces,
                stitch_guard_seconds,
                stitch_guard_min_copy,
                keyframes=keyframes,
            )
            if stitch_touched > 0:
                self._log(
                    "smart_hybrid_stitch_guard_apply "
                    f"transitions={stitch_touched} "
                    f"added_reencode={stitch_added:.3f}s "
                    f"pieces_before={pieces_before} after={len(pieces)}"
                )

        # Recompute stats after merge
        copy_dur = 0.0
        reenc_dur = 0.0
        for kind, payload in pieces:
            if kind == "cluster":
                try:
                    ckeeps = payload  # type: ignore[assignment]
                    reenc_dur += sum(max(0.0, s.end - s.start) for s in ckeeps)
                except Exception:
                    pass
            else:
                s, e = payload  # type: ignore[misc]
                d = max(0.0, float(e) - float(s))
                if kind == "copy":
                    copy_dur += d
                else:
                    reenc_dur += d

        copy_pct = (copy_dur / total_kept) * 100.0
        self._log(
            f"smart_hybrid_stats pieces={len(pieces)} copy={copy_dur:.3f}s "
            f"reencode={reenc_dur:.3f}s copy_pct={copy_pct:.1f}%"
        )

        # Decide pack behavior early (used for fallback rules too)
        pack_min = 24 if segments_context else 250
        env_pack_min = "AUTO_CUTTER_HYBRID_PACK_MIN_SEGMENTS" if segments_context else "AUTO_CUTTER_HYBRID_PACK_MIN"
        try:
            env_min = int(_env(env_pack_min, "").strip() or str(pack_min))
            if env_min > 0:
                pack_min = env_min
        except Exception:
            pass
        pack_on = len(pieces) >= pack_min
        if _env("AUTO_CUTTER_HYBRID_PACK", "1").strip() in ("0", "false", "False"):
            pack_on = False

        tier = self._storage_tier()
        if segments_context:
            # Segmented timelines (split/dup/reorder) benefit from aggressive packing.
            pack_target_seconds = 240.0 if tier == "ssd" else 360.0
            pack_max_pieces = 200 if tier == "ssd" else 320
        else:
            pack_target_seconds = 90.0 if tier == "ssd" else 150.0
            pack_max_pieces = 60 if tier == "ssd" else 90
        if len(pieces) > 1200:
            pack_target_seconds *= 1.3
            pack_max_pieces = int(pack_max_pieces * 1.5)

        # Keep reencode packs source-local; otherwise filter-concat may decode huge ranges
        # when timeline order is non-monotonic (split/dup/reorder).
        reencode_pack_max_source_span = 0.0
        reencode_pack_max_source_jump = 0.0
        if segments_context:
            reencode_pack_max_source_span = 180.0
            reencode_pack_max_source_jump = 75.0
            if non_monotonic_timeline:
                reencode_pack_max_source_span = 60.0
                reencode_pack_max_source_jump = 25.0
        try:
            v = float(_env("AUTO_CUTTER_HYBRID_PACK_REENCODE_MAX_SPAN", "").strip() or "0")
            if v > 0.0:
                reencode_pack_max_source_span = v
        except Exception:
            pass
        try:
            v = float(_env("AUTO_CUTTER_HYBRID_PACK_REENCODE_MAX_JUMP", "").strip() or "0")
            if v > 0.0:
                reencode_pack_max_source_jump = v
        except Exception:
            pass

        # Dynamic fallback when copy ratio is too low or too many pieces
        min_copy_pct = 55.0
        if copy_pct < min_copy_pct:
            self._log(
                "smart_hybrid_fallback "
                f"copy_pct={copy_pct:.1f}% min_copy_pct={min_copy_pct:.1f}% "
                f"pieces={len(pieces)} max_pieces={max_pieces}"
            )
            raise _SmartHybridFallback("smart_hybrid_fallback")

        if len(pieces) > max_pieces:
            # If copy ratio is strong, prefer hybrid + packing instead of full re-encode
            if copy_pct >= 70.0 and pack_on and len(pieces) <= (max_pieces * 3):
                self._log(
                    "smart_hybrid_pieces_high allow "
                    f"pieces={len(pieces)} max_pieces={max_pieces} copy_pct={copy_pct:.1f}%"
                )
            else:
                self._log(
                    "smart_hybrid_fallback "
                    f"copy_pct={copy_pct:.1f}% min_copy_pct={min_copy_pct:.1f}% "
                    f"pieces={len(pieces)} max_pieces={max_pieces}"
                )
                raise _SmartHybridFallback("smart_hybrid_fallback")

        # Audio strategy
        audio_processing = self._needs_audio_processing()
        audio_copy_ok = self._smart_audio_copy_ok(src_path)
        if clusters_exist and audio_processing:
            audio_copy_ok = False

        apply_audio_filters_in_segments = bool(audio_processing)
        segment_audio_copy = bool((not audio_processing) and audio_copy_ok)
        unify_audio = self._hybrid_audio_unify_policy(segments_context, non_monotonic_timeline)
        if unify_audio and (not audio_processing) and segment_audio_copy:
            segment_audio_copy = False
            self._log(
                "smart_hybrid_audio_unify=on "
                f"segments_context={'yes' if segments_context else 'no'} "
                f"non_monotonic={'yes' if non_monotonic_timeline else 'no'}"
            )
        else:
            self._log(
                f"smart_hybrid_audio_unify={'on' if unify_audio else 'off'}"
            )
        audio_mode = self._log_audio_mode(
            "smart_hybrid",
            audio_processing=audio_processing,
            audio_copy_ok=segment_audio_copy,
            filters_in_segments=apply_audio_filters_in_segments,
        )

        audio_filters = ""
        if apply_audio_filters_in_segments:
            af = self._build_audio_filter_chain(include_legacy_gain_limiter=True)
            async_af = "aresample=async=1:first_pts=0"
            audio_filters = f"{af},{async_af}" if af else async_af

        # Build segments (TS intermediates)
        seg_dir = tempfile.mkdtemp(prefix="auto_cutter_hybrid_")
        temp_files.append(seg_dir)

        seg_files: list[str] = []
        done = 0.0
        seg_ext = "ts"

        if not pack_on:
            video_bsf = self._ts_video_bsf(src_path)
            piece_cache: dict[str, str] = {}
            piece_cache_hits = 0

            def _piece_key(kind: str, payload: object) -> str | None:
                try:
                    q = 1000.0
                    base = {
                        "v": 9,
                        "kind": str(kind),
                        "src": self._source_signature(src_path),
                        "codec": str(self.codec),
                        "video_args_balanced": self._video_args("balanced"),
                        "cut_hq_policy": self._cut_hq_policy(),
                        "fps": round(float(force_fps or 0.0), 6),
                        "copy_seek_eps": round(float(self._copy_seek_epsilon(force_fps)), 6),
                        "copy_tail_eps": round(float(self._cut_boundary_epsilon(force_fps)), 6),
                        "audio_copy": bool(segment_audio_copy),
                        "audio_filters": str(audio_filters),
                        "container": str(seg_ext),
                        "ts_policy": "zero_based_v2",
                    }
                    if kind == "cluster":
                        ckeeps = payload  # type: ignore[assignment]
                        rows = []
                        for k in ckeeps:
                            rows.append([
                                int(round(float(k.start) * q)),
                                int(round(float(k.end) * q)),
                            ])
                        base["keeps"] = rows
                    else:
                        s, e = payload  # type: ignore[misc]
                        base["start_ms"] = int(round(float(s) * q))
                        base["end_ms"] = int(round(float(e) * q))
                        if str(kind) == "reencode":
                            d = max(0.0, float(e) - float(s))
                            preset = "cut_hq" if self._use_cut_hq_for_reencode(d) else "balanced"
                            base["video_preset"] = preset
                            base["video_args"] = self._video_args(preset)
                    raw = json.dumps(base, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
                    return hashlib.sha1(raw.encode("utf-8")).hexdigest()
                except Exception:
                    return None

            for idx, (kind, payload) in enumerate(pieces):
                piece_out_path = os.path.join(seg_dir, f"seg_{idx:04d}.{seg_ext}")
                cache_key = _piece_key(str(kind), payload)
                if cache_key:
                    hit = piece_cache.get(cache_key, "")
                    if hit and os.path.exists(hit):
                        seg_files.append(hit)
                        piece_cache_hits += 1
                        hit_dur = 0.0
                        try:
                            if kind == "cluster":
                                ckeeps = payload  # type: ignore[assignment]
                                hit_dur = sum(max(0.0, s.end - s.start) for s in ckeeps)
                                done += hit_dur
                            else:
                                s, e = payload  # type: ignore[misc]
                                hit_dur = max(0.0, float(e) - float(s))
                                done += hit_dur
                        except Exception:
                            pass
                        if hit_dur > 0.0:
                            self._record_reuse(1, hit_dur, category="hybrid_piece_pack_reuse")
                        self._log(
                            f"smart_hybrid_piece_cache_hit {idx+1}/{len(pieces)} "
                            f"mode={kind} path={hit}"
                        )
                        _notify_piece_progress(done)
                        pct = int(max(0.0, min(99.0, (done / total_kept) * 100.0)))
                        if emit_progress:
                            self.progress.emit(pct, f"Hybrid render {pct}%")
                        continue
                    disk_hit = self._chunk_cache_lookup(cache_key, seg_ext)
                    if disk_hit and os.path.exists(disk_hit):
                        piece_cache[cache_key] = disk_hit
                        seg_files.append(disk_hit)
                        piece_cache_hits += 1
                        hit_dur = 0.0
                        try:
                            if kind == "cluster":
                                ckeeps = payload  # type: ignore[assignment]
                                hit_dur = sum(max(0.0, s.end - s.start) for s in ckeeps)
                                done += hit_dur
                            else:
                                s, e = payload  # type: ignore[misc]
                                hit_dur = max(0.0, float(e) - float(s))
                                done += hit_dur
                        except Exception:
                            pass
                        if hit_dur > 0.0:
                            self._record_reuse(1, hit_dur, category="hybrid_piece_pack_reuse")
                        self._log(
                            f"smart_hybrid_piece_cache_hit {idx+1}/{len(pieces)} "
                            f"mode={kind} source=disk path={disk_hit}"
                        )
                        _notify_piece_progress(done)
                        pct = int(max(0.0, min(99.0, (done / total_kept) * 100.0)))
                        if emit_progress:
                            self.progress.emit(pct, f"Hybrid render {pct}%")
                        continue
                if kind == "cluster":
                    ckeeps = payload  # type: ignore[assignment]
                    cmd, _ = self._build_cmd_filter_concat_to(
                        ckeeps,
                        piece_out_path,
                        temp_files,
                        tmp_dir=seg_dir,
                        force_fps=force_fps,
                        src_path=src_path,
                        apply_audio_filters=apply_audio_filters_in_segments,
                        video_only=False,
                        container=seg_ext,
                    )
                    self._log(
                        f"smart_hybrid_cluster {idx+1}/{len(pieces)} keeps={len(ckeeps)} "
                        f"dur={sum(max(0.0, s.end - s.start) for s in ckeeps):.3f}s"
                    )
                    t_piece = time.time()
                    rc, _out, err = self._run_ffmpeg(cmd)
                    if rc != 0:
                        err = (err or "").strip()
                        raise RuntimeError(err or f"FFmpeg hybrid cluster {idx} failed")
                    piece_dur = sum(max(0.0, s.end - s.start) for s in ckeeps)
                    done += piece_dur
                    elapsed_piece = max(0.0, time.time() - t_piece)
                    self._record_render_work(piece_dur, elapsed_piece)
                    self._log(
                        f"smart_hybrid_piece_done {idx+1}/{len(pieces)} mode=cluster "
                        f"dur={piece_dur:.3f}s elapsed={elapsed_piece:.2f}s"
                    )
                    _notify_piece_progress(done)
                else:
                    s, e = payload  # type: ignore[misc]
                    cmd = self._build_cmd_smart_segment(
                        src_path,
                        piece_out_path,
                        float(s),
                        float(e),
                        str(kind),
                        audio_copy=segment_audio_copy,
                        audio_filters=audio_filters,
                        container=seg_ext,
                        video_bsf=video_bsf if str(kind) == "copy" else None,
                        video_only=False,
                        force_fps=force_fps,
                    )
                    self._log(f"smart_hybrid_segment {idx+1}/{len(pieces)} mode={kind} {s:.3f}-{e:.3f}")
                    t_piece = time.time()
                    rc, _out, err = self._run_ffmpeg(cmd)
                    if rc != 0:
                        err = (err or "").strip()
                        raise RuntimeError(err or f"FFmpeg hybrid segment {idx} failed")
                    piece_dur = max(0.0, float(e) - float(s))
                    done += piece_dur
                    elapsed_piece = max(0.0, time.time() - t_piece)
                    self._record_render_work(piece_dur, elapsed_piece)
                    self._log(
                        f"smart_hybrid_piece_done {idx+1}/{len(pieces)} mode={kind} "
                        f"dur={piece_dur:.3f}s elapsed={elapsed_piece:.2f}s"
                    )
                    _notify_piece_progress(done)

                final_piece_path = piece_out_path
                if cache_key:
                    final_piece_path = self._chunk_cache_store(cache_key, seg_ext, piece_out_path)
                    piece_cache[cache_key] = final_piece_path
                seg_files.append(final_piece_path)
                pct = int(max(0.0, min(99.0, (done / total_kept) * 100.0)))
                if emit_progress:
                    self.progress.emit(pct, f"Hybrid render {pct}%")
            if piece_cache_hits > 0:
                self._log(f"smart_hybrid_piece_cache_hits total={piece_cache_hits}")
        else:
            packs: list[tuple[str, list[tuple[str, object]]]]
            packs = self._pack_hybrid_pieces(
                pieces,
                pack_target_seconds,
                pack_max_pieces,
                reencode_max_source_span=reencode_pack_max_source_span,
                reencode_max_source_jump=reencode_pack_max_source_jump,
            )
            self._log(
                f"smart_hybrid_pack on packs={len(packs)} "
                f"pack_target={pack_target_seconds:.1f}s pack_max_pieces={pack_max_pieces} "
                f"segments_context={'yes' if segments_context else 'no'} "
                f"reencode_max_span={reencode_pack_max_source_span:.1f}s "
                f"reencode_max_jump={reencode_pack_max_source_jump:.1f}s"
            )
            try:
                _single_seg_env = str(_env("AUTO_CUTTER_SINGLE_SEGMENT_REENCODE", "0")).strip().lower()
                allow_single_segment_reencode = _single_seg_env in ("1", "true", "yes", "on")
            except Exception:
                allow_single_segment_reencode = False
            self._log(
                f"smart_hybrid_single_segment_reencode="
                f"{'on' if allow_single_segment_reencode else 'off'}"
            )
            pack_cache: dict[str, str] = {}
            pack_cache_hits = 0
            q = 1000.0
            video_bsf = self._ts_video_bsf(src_path)

            def _pack_key_copy(ranges: list[tuple[float, float]]) -> str | None:
                try:
                    payload = {
                        "v": 9,
                        "kind": "pack_copy",
                        "src": self._source_signature(src_path),
                        "codec": str(self.codec),
                        "video_args": self._video_args("balanced"),
                        "cut_hq_policy": self._cut_hq_policy(),
                        "copy_seek_eps": round(float(self._copy_seek_epsilon(force_fps)), 6),
                        "copy_tail_eps": round(float(self._cut_boundary_epsilon(force_fps)), 6),
                        "audio_copy": bool(segment_audio_copy),
                        "audio_filters": str(audio_filters),
                        "container": str(seg_ext),
                        "ts_policy": "zero_based_v2",
                        "ranges": [[int(round(float(s) * q)), int(round(float(e) * q))] for s, e in ranges],
                    }
                    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
                    return hashlib.sha1(raw.encode("utf-8")).hexdigest()
                except Exception:
                    return None

            def _pack_key_reencode(
                pack_keeps: list[Segment],
                video_preset: str = "balanced",
            ) -> str | None:
                try:
                    payload = {
                        "v": 9,
                        "kind": "pack_reencode",
                        "src": self._source_signature(src_path),
                        "codec": str(self.codec),
                        "video_preset": str(video_preset),
                        "video_args": self._video_args(video_preset),
                        "cut_hq_policy": self._cut_hq_policy(),
                        "fps": round(float(force_fps or 0.0), 6),
                        "apply_audio_filters": bool(apply_audio_filters_in_segments),
                        "audio_filters": str(audio_filters),
                        "container": str(seg_ext),
                        "ts_policy": "zero_based_v2",
                        "keeps": [
                            [int(round(float(s.start) * q)), int(round(float(s.end) * q))]
                            for s in pack_keeps
                        ],
                    }
                    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
                    return hashlib.sha1(raw.encode("utf-8")).hexdigest()
                except Exception:
                    return None

            for idx, (pack_kind, pack_pieces) in enumerate(packs):
                pack_out_path = os.path.join(seg_dir, f"pack_{idx:04d}.{seg_ext}")
                final_pack_path = pack_out_path
                if pack_kind == "copy":
                    ranges: list[tuple[float, float]] = []
                    for k, payload in pack_pieces:
                        if k != "copy":
                            continue
                        s, e = payload  # type: ignore[misc]
                        ranges.append((float(s), float(e)))
                    if not ranges:
                        continue
                    pack_dur = sum(max(0.0, e - s) for s, e in ranges)
                    cache_key = _pack_key_copy(ranges)
                    if cache_key:
                        hit = pack_cache.get(cache_key, "")
                        if hit and os.path.exists(hit):
                            seg_files.append(hit)
                            done += pack_dur
                            pack_cache_hits += 1
                            self._record_reuse(1, pack_dur, category="hybrid_piece_pack_reuse")
                            self._log(
                                f"smart_hybrid_pack_cache_hit {idx+1}/{len(packs)} "
                                f"kind=copy dur={pack_dur:.3f}s path={hit}"
                            )
                            _notify_piece_progress(done)
                            pct = int(max(0.0, min(99.0, (done / total_kept) * 100.0)))
                            if emit_progress:
                                self.progress.emit(pct, f"Hybrid render {pct}%")
                            continue
                        disk_hit = self._chunk_cache_lookup(cache_key, seg_ext)
                        if disk_hit and os.path.exists(disk_hit):
                            pack_cache[cache_key] = disk_hit
                            seg_files.append(disk_hit)
                            done += pack_dur
                            pack_cache_hits += 1
                            self._record_reuse(1, pack_dur, category="hybrid_piece_pack_reuse")
                            self._log(
                                f"smart_hybrid_pack_cache_hit {idx+1}/{len(packs)} "
                                f"kind=copy source=disk dur={pack_dur:.3f}s path={disk_hit}"
                            )
                            _notify_piece_progress(done)
                            pct = int(max(0.0, min(99.0, (done / total_kept) * 100.0)))
                            if emit_progress:
                                self.progress.emit(pct, f"Hybrid render {pct}%")
                            continue
                    copy_cmd_mode = "concat"
                    if len(ranges) == 1:
                        copy_cmd_mode = "single_segment"
                        rs, re = ranges[0]
                        cmd = self._build_cmd_smart_segment(
                            src_path=src_path,
                            out_path=pack_out_path,
                            start=float(rs),
                            end=float(re),
                            mode="copy",
                            audio_copy=segment_audio_copy,
                            audio_filters=audio_filters,
                            container=seg_ext,
                            video_bsf=video_bsf,
                            video_only=False,
                            force_fps=force_fps,
                        )
                        self._log(
                            f"smart_hybrid_pack_copy {idx+1}/{len(packs)} "
                            f"pieces={len(ranges)} dur={pack_dur:.3f}s mode={copy_cmd_mode}"
                        )
                        t_pack = time.time()
                        rc, _out, err = self._run_ffmpeg(cmd)
                        if rc != 0:
                            err = (err or "").strip()
                            raise RuntimeError(err or f"FFmpeg hybrid pack copy {idx} failed")
                    else:
                        # More robust than ffconcat inpoint/outpoint for H.264 boundaries:
                        # extract each range with -ss/-t copy and concatenate file-list only.
                        copy_cmd_mode = "split_concat"
                        part_files: list[str] = []
                        self._log(
                            f"smart_hybrid_pack_copy {idx+1}/{len(packs)} "
                            f"pieces={len(ranges)} dur={pack_dur:.3f}s mode={copy_cmd_mode}"
                        )
                        t_pack = time.time()
                        for ridx, (rs, re) in enumerate(ranges):
                            part_path = os.path.join(seg_dir, f"pack_{idx:04d}_part_{ridx:03d}.{seg_ext}")
                            temp_files.append(part_path)
                            part_cmd = self._build_cmd_smart_segment(
                                src_path=src_path,
                                out_path=part_path,
                                start=float(rs),
                                end=float(re),
                                mode="copy",
                                audio_copy=segment_audio_copy,
                                audio_filters=audio_filters,
                                container=seg_ext,
                                video_bsf=video_bsf,
                                video_only=False,
                                force_fps=force_fps,
                            )
                            rc, _out, err = self._run_ffmpeg(part_cmd)
                            if rc != 0:
                                err = (err or "").strip()
                                raise RuntimeError(
                                    err or f"FFmpeg hybrid pack copy part {idx}:{ridx} failed"
                                )
                            part_files.append(part_path)
                        if not part_files:
                            raise RuntimeError(f"FFmpeg hybrid pack copy {idx} produced no parts")
                        self._concat_ts_list(part_files, temp_files, pack_out_path)

                    done += pack_dur
                    elapsed_pack = max(0.0, time.time() - t_pack)
                    self._record_render_work(pack_dur, elapsed_pack)
                    self._log(
                        f"smart_hybrid_pack_done {idx+1}/{len(packs)} kind=copy "
                        f"dur={pack_dur:.3f}s elapsed={elapsed_pack:.2f}s"
                    )
                    _notify_piece_progress(done)
                    final_pack_path = pack_out_path
                    if cache_key:
                        final_pack_path = self._chunk_cache_store(cache_key, seg_ext, pack_out_path)
                        pack_cache[cache_key] = final_pack_path
                else:
                    pack_keeps: list[Segment] = []
                    for k, payload in pack_pieces:
                        if k == "cluster":
                            try:
                                ckeeps = payload  # type: ignore[assignment]
                                pack_keeps.extend(ckeeps)
                            except Exception:
                                pass
                        else:
                            try:
                                s, e = payload  # type: ignore[misc]
                                pack_keeps.append(Segment(float(s), float(e)))
                            except Exception:
                                pass
                    pack_keeps = [s for s in pack_keeps if s.end > s.start]
                    if not pack_keeps:
                        continue
                    pack_dur = sum(max(0.0, s.end - s.start) for s in pack_keeps)
                    single_piece_candidate = (
                        len(pack_pieces) == 1
                        and pack_pieces[0][0] != "cluster"
                        and len(pack_keeps) == 1
                    )
                    reencode_preset = "balanced"
                    if single_piece_candidate:
                        one = pack_keeps[0]
                        one_dur = max(0.0, float(one.end) - float(one.start))
                        reencode_preset = "cut_hq" if self._use_cut_hq_for_reencode(one_dur) else "balanced"
                    single_piece_reencode = bool(single_piece_candidate and allow_single_segment_reencode)
                    cache_key = _pack_key_reencode(pack_keeps, video_preset=reencode_preset)
                    if cache_key:
                        hit = pack_cache.get(cache_key, "")
                        if hit and os.path.exists(hit):
                            seg_files.append(hit)
                            done += pack_dur
                            pack_cache_hits += 1
                            self._record_reuse(1, pack_dur, category="hybrid_piece_pack_reuse")
                            self._log(
                                f"smart_hybrid_pack_cache_hit {idx+1}/{len(packs)} "
                                f"kind=reencode dur={pack_dur:.3f}s path={hit}"
                            )
                            _notify_piece_progress(done)
                            pct = int(max(0.0, min(99.0, (done / total_kept) * 100.0)))
                            if emit_progress:
                                self.progress.emit(pct, f"Hybrid render {pct}%")
                            continue
                        disk_hit = self._chunk_cache_lookup(cache_key, seg_ext)
                        if disk_hit and os.path.exists(disk_hit):
                            pack_cache[cache_key] = disk_hit
                            seg_files.append(disk_hit)
                            done += pack_dur
                            pack_cache_hits += 1
                            self._record_reuse(1, pack_dur, category="hybrid_piece_pack_reuse")
                            self._log(
                                f"smart_hybrid_pack_cache_hit {idx+1}/{len(packs)} "
                                f"kind=reencode source=disk dur={pack_dur:.3f}s path={disk_hit}"
                            )
                            _notify_piece_progress(done)
                            pct = int(max(0.0, min(99.0, (done / total_kept) * 100.0)))
                            if emit_progress:
                                self.progress.emit(pct, f"Hybrid render {pct}%")
                            continue
                    if single_piece_reencode:
                        one = pack_keeps[0]
                        cmd = self._build_cmd_smart_segment(
                            src_path,
                            pack_out_path,
                            float(one.start),
                            float(one.end),
                            "reencode",
                            audio_copy=segment_audio_copy,
                            audio_filters=audio_filters,
                            container=seg_ext,
                            video_bsf=None,
                            video_only=False,
                            force_fps=force_fps,
                        )
                    else:
                        cmd, _ = self._build_cmd_filter_concat_to(
                            pack_keeps,
                            pack_out_path,
                            temp_files,
                            tmp_dir=seg_dir,
                            force_fps=force_fps,
                            src_path=src_path,
                            apply_audio_filters=apply_audio_filters_in_segments,
                            video_only=False,
                            container=seg_ext,
                            video_preset=reencode_preset,
                        )
                    self._log(
                        f"smart_hybrid_pack_reencode {idx+1}/{len(packs)} "
                        f"keeps={len(pack_keeps)} dur={pack_dur:.3f}s "
                        f"mode={'single_segment' if single_piece_reencode else 'filter_concat'} "
                        f"preset={reencode_preset}"
                    )
                    t_pack = time.time()
                    rc, _out, err = self._run_ffmpeg(cmd)
                    if rc != 0:
                        err = (err or "").strip()
                        raise RuntimeError(err or f"FFmpeg hybrid pack reencode {idx} failed")
                    done += pack_dur
                    elapsed_pack = max(0.0, time.time() - t_pack)
                    self._record_render_work(pack_dur, elapsed_pack)
                    self._log(
                        f"smart_hybrid_pack_done {idx+1}/{len(packs)} kind=reencode "
                        f"dur={pack_dur:.3f}s elapsed={elapsed_pack:.2f}s"
                    )
                    _notify_piece_progress(done)
                    if cache_key:
                        final_pack_path = self._chunk_cache_store(cache_key, seg_ext, pack_out_path)
                        pack_cache[cache_key] = final_pack_path

                seg_files.append(final_pack_path)
                pct = int(max(0.0, min(99.0, (done / total_kept) * 100.0)))
                if emit_progress:
                    self.progress.emit(pct, f"Hybrid render {pct}%")
            if pack_cache_hits > 0:
                self._log(f"smart_hybrid_pack_cache_hits total={pack_cache_hits}")

        # concat lossless (TS) then remux
        group_size = 50
        if len(seg_files) > 1200:
            group_size = 100
        elif len(seg_files) > 400:
            group_size = 60
        concat_ts = self._tree_concat_ts(seg_files, temp_files, group_size, label="hybrid")

        target_out = out_path if out_path else self.output_path
        if target_out and str(target_out).lower().endswith(".ts"):
            t_copy = time.time()
            try:
                src_abs = os.path.abspath(concat_ts)
                dst_abs = os.path.abspath(target_out)
                if src_abs != dst_abs:
                    shutil.copyfile(src_abs, dst_abs)
            except Exception as e:
                raise RuntimeError(f"Hybrid TS finalize failed: {e}")
            self._log(f"hybrid_finalize_ts done elapsed={time.time() - t_copy:.2f}s")
            return

        t_remux = time.time()
        self._remux_mp4_from_ts(
            concat_ts,
            target_out,
            audio_mode,
            src_probe_path=src_path,
        )
        self._log(f"hybrid_remux done elapsed={time.time() - t_remux:.2f}s")

    # -----------------------------
    def _find_ffprobe(self) -> str | None:
        if not self.ffmpeg_path:
            return None
        ffmpeg_dir = os.path.dirname(self.ffmpeg_path)
        cand = os.path.join(ffmpeg_dir, "ffprobe.exe")
        if os.path.exists(cand):
            return cand
        # fallback to PATH
        return "ffprobe"

    def _probe_fps(self, path: str) -> float:
        try:
            ffprobe = self._find_ffprobe()
            if not ffprobe:
                return 0.0
            cmd = [
                ffprobe, "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=avg_frame_rate,r_frame_rate",
                "-of", "default=nk=1:nw=1",
                path,
            ]
            p = run_no_window(cmd, capture_output=True, text=True)
            if p.returncode != 0:
                return 0.0
            lines = [ln.strip() for ln in (p.stdout or "").splitlines() if ln.strip()]
            if not lines:
                return 0.0
            # prefer avg_frame_rate if available
            rate = lines[0]
            if len(lines) > 1 and rate in ("0/0", "0"):
                rate = lines[1]
            if "/" in rate:
                num, den = rate.split("/", 1)
                num = float(num)
                den = float(den)
                if den > 0:
                    return num / den
            return float(rate)
        except Exception:
            return 0.0

    def _snap_keeps_to_frames(self, keeps: list[Segment], fps: float) -> list[Segment]:
        if not keeps or fps <= 1e-3:
            return keeps
        frame = 1.0 / float(fps)
        eps = frame * 1e-3
        out: list[Segment] = []
        for s in keeps:
            start = float(s.start)
            end = float(s.end)
            # Conservative snapping: avoid pulling frames from outside the requested keep.
            a = math.ceil((start - eps) / frame) * frame
            b = math.floor((end + eps) / frame) * frame
            if b <= a:
                b = a + frame
            out.append(Segment(max(0.0, a), max(0.0, b)))
        return merge_overlaps(sorted(out, key=lambda k: k.start))

