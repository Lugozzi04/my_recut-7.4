"""Opt-in, aggregate diagnostics. No media reads, Qt, providers or algorithm changes."""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timezone
from functools import wraps
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable, Iterator, ParamSpec, TypeVar
import uuid

VERSION = "gameplay-profiler-v1"
_ACTIVE: ContextVar[GameplayProfiler | None] = ContextVar("gameplay_profiler", default=None)
_RECENT_AUDIO: dict[tuple[str, int, int], dict[str, Any]] = {}
_AUDIO_LOCK = threading.Lock()
P = ParamSpec("P")
R = TypeVar("R")


def current_profile() -> GameplayProfiler | None:
    return _ACTIVE.get()


def measure(name: str) -> Any:
    profile = current_profile()
    return profile.measure(name) if profile else nullcontext()


def submeasure(name: str) -> Any:
    profile = current_profile()
    return profile.measure(profile.child(name)) if profile else nullcontext()


def record(name: str, seconds: float = 0.0, **counts: float) -> None:
    profile = current_profile()
    if profile:
        profile.record(name, seconds, **counts)


def profiled(name: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    def decorate(fn: Callable[P, R]) -> Callable[P, R]:
        @wraps(fn)
        def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            with measure(name):
                return fn(*args, **kwargs)
        return wrapped
    return decorate


def _audio_key(source: str) -> tuple[str, int, int] | None:
    try:
        file = Path(source).resolve()
        stat = file.stat()
        return str(file), stat.st_size, stat.st_mtime_ns
    except OSError:
        return None


def remember_audio(source: str, summary: dict[str, Any]) -> None:
    key = _audio_key(source)
    if key is not None:
        with _AUDIO_LOCK:
            if len(_RECENT_AUDIO) >= 16:
                _RECENT_AUDIO.pop(next(iter(_RECENT_AUDIO)))
            _RECENT_AUDIO[key] = summary


def report_directory() -> Path:
    from utils.runtime_paths import data_root, is_frozen
    # Source checkout, independent of CWD; never write into a PyInstaller bundle.
    if is_frozen():
        return data_root() / "performance_reports"
    return Path(__file__).resolve().parents[1] / "docs" / "performance_reports"


def _physical_memory() -> int | None:
    if os.name != "nt":
        return None
    try:
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                        *[(name, ctypes.c_ulonglong) for name in ("physical", "available", "page",
                          "available_page", "virtual", "available_virtual", "extended")]]

        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        return int(status.physical) if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)) else None
    except (AttributeError, OSError):
        return None


class GameplayProfiler:
    def __init__(self, source: str, modes: dict[str, Any] | None = None, *, save: bool = True) -> None:
        self.started = time.perf_counter()
        self.created = datetime.now(timezone.utc)
        self.run_id = uuid.uuid4().hex
        self.save = save
        self.path = report_directory() / (
            f"gameplay_profile_{self.created.strftime('%Y%m%d_%H%M%S')}_{self.run_id}.json")
        self.status = "running"
        self.finished: str | None = None
        self.elapsed: float | None = None
        self.error: dict[str, str] | None = None
        self.interruption_phase: str | None = None
        self._last_exception: BaseException | None = None
        self.stack: list[tuple[str, float]] = []
        self.phases: dict[str, dict[str, Any]] = {}
        self.video: dict[str, Any] = {"name": Path(source).name, "duration_s": None,
                                      "width": None, "height": None, "fps": None, "codec": None}
        try:
            self.video["size_bytes"] = Path(source).stat().st_size
        except OSError:
            self.video["size_bytes"] = None
        self.modes = modes or {}
        self.system: dict[str, Any] = {"logical_cpu_cores": os.cpu_count(), "physical_memory_bytes": _physical_memory()}
        self.decoder: dict[str, Any] = {"backend": "FFmpeg", "hardware_acceleration_requested": False,
                                        "decoder_implementation": "FFmpeg default; not independently observed"}
        self.last_write = self.started
        self.write_error: str | None = None
        self.checkpoint_write_seconds = 0.0
        self.audio: dict[str, Any] = {"status": "not_observed", "included_in_total": False,
            "reason": "Classic runs separately; it may predate this session or have been restored."}
        key = _audio_key(source)
        with _AUDIO_LOCK:
            previous = _RECENT_AUDIO.get(key) if key else None
            if previous:
                self.audio = {"status": "observed_separate_run", "included_in_total": False,
                              "measurement": deepcopy(previous)}
        for name in ("detection.setup", "detection", "monitor", "vs_refinement", "result_windows",
                     "event_consolidation", "game_assembly", "boundaries", "hero_groups",
                     "boundaries.transcript.inference", "boundaries.transcript.audio_extraction",
                     "boundaries.selection.exact"):
            self.skip(name, "not_reached")

    @contextmanager
    def activate(self) -> Iterator[GameplayProfiler]:
        marker = _ACTIVE.set(self)
        try:
            self.checkpoint(force=True)
            yield self
        finally:
            _ACTIVE.reset(marker)

    def child(self, name: str) -> str:
        return f"{self.stack[-1][0]}.{name}" if self.stack else name

    def node(self, name: str) -> dict[str, Any]:
        return self.phases.setdefault(name, {"status": "pending", "wall_seconds": 0.0,
                                             "calls": 0, "counters": {}})

    def skip(self, name: str, reason: str) -> None:
        self.node(name).update(status="skipped", reason=reason)

    def skip_unexecuted(self, name: str, reason: str) -> None:
        node = self.node(name)
        if node["calls"] == 0 and node["wall_seconds"] == 0:
            self.skip(name, reason)

    def cache(self, name: str, state: str) -> None:
        node = self.node(name)
        previous = node.get("cache")
        node["cache"] = ("partial" if state == "partial" or previous == "partial"
                         or {previous, state} == {"hit", "miss"} else state)
        lookups = node.setdefault("cache_observations", {})
        lookups[state] = lookups.get(state, 0) + 1

    def record(self, name: str, seconds: float = 0.0, **counts: float) -> None:
        node = self.node(name)
        if node["status"] in ("pending", "skipped"):
            node["status"] = "completed"
            node.pop("reason", None)
        node["wall_seconds"] += seconds
        for key, value in counts.items():
            node["counters"][key] = node["counters"].get(key, 0) + value

    @contextmanager
    def measure(self, name: str) -> Iterator[None]:
        node = self.node(name)
        node.update(status="running")
        node.pop("reason", None)
        node["calls"] += 1
        started = time.perf_counter()
        self.stack.append((name, started))
        try:
            yield
        except BaseException as exc:
            if not isinstance(exc, (GeneratorExit, StopIteration)):
                node["status"] = "cancelled" if "Cancel" in type(exc).__name__ else "failed"
                node["error_type"] = type(exc).__name__
                node["counters"]["failures"] = node["counters"].get("failures", 0) + 1
                if self._last_exception is not exc:
                    self._last_exception = exc
                    self.interruption_phase = name
            raise
        finally:
            node["wall_seconds"] += time.perf_counter() - started
            if node["status"] == "running":
                node["status"] = "completed"
            self.stack.pop()

    def checkpoint(self, *, force: bool = False) -> None:
        now = time.perf_counter()
        if not self.save or (not force and now - self.last_write < 30.0):
            return
        self.last_write = now
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.snapshot(), ensure_ascii=False, indent=2,
                                           allow_nan=False), encoding="utf-8")
            os.replace(temporary, self.path)
        except Exception as exc:
            # Diagnostic I/O must never fail or cancel a successful analysis.
            self.write_error = type(exc).__name__
        finally:
            self.checkpoint_write_seconds += time.perf_counter() - now

    def finish(self, status: str, error: BaseException | None = None) -> None:
        self.status = status
        self.elapsed = time.perf_counter() - self.started
        self.finished = datetime.now(timezone.utc).isoformat()
        if error is not None:
            # Exception messages can contain full paths, tokens or transcript text.
            self.error = {"type": type(error).__name__}
            code = getattr(error, "code", None)
            if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", code):
                self.error["code"] = code
        self.checkpoint(force=True)

    def snapshot(self) -> dict[str, Any]:
        phases = deepcopy(self.phases)
        now = time.perf_counter()
        for name, started in self.stack:
            phases[name]["wall_seconds"] += now - started
        for name, node in phases.items():
            wall = node["wall_seconds"]
            children = sum(child["wall_seconds"] for key, child in phases.items()
                           if key.startswith(name + ".") and "." not in key[len(name) + 1:])
            node["other_wall_seconds"] = max(0.0, wall - children)
            counts = node["counters"]
            node["throughput_frames_per_second"] = counts["frames"] / wall if wall > 0 and "frames" in counts else None
            node["mean_seconds_per_window"] = wall / counts["windows"] if wall > 0 and counts.get("windows") else None
            node["throughput_media_seconds_per_second"] = counts["media_seconds"] / wall if wall > 0 and "media_seconds" in counts else None
        return {"format": "recut_gameplay_performance", "profiler_version": VERSION,
                "run_id": self.run_id, "started_at_utc": self.created.isoformat(),
                "finished_at_utc": self.finished, "status": self.status,
                "total_wall_seconds": self.elapsed if self.elapsed is not None else now - self.started,
                "active_phase": self.stack[-1][0] if self.stack else None,
                "interruption_phase": self.interruption_phase if self.status != "completed" else None,
                "error": self.error, "video": self.video, "modes": self.modes, "system": self.system,
                "decoder": self.decoder, "classic_audio": self.audio, "phases": phases,
                "report_write_error": self.write_error,
                "prior_checkpoint_write_seconds": self.checkpoint_write_seconds,
                "timing_notes": ["Phase/subphase wall times are inclusive; do not sum overlapping parents and children.",
                    "frame_delivery_wait includes producer/pipe/queue/PTS wait; it is not isolated decoder CPU time.",
                    "FFmpeg decoding overlaps matching; sampled frames are not all internally decoded source frames.",
                    "Cache-hit reports contain current lookup time only, never historical processing timings.",
                    "Periodic running snapshots are best effort; forced termination can leave the latest snapshot.",
                    "Total covers Detect Games worker including prior checkpoints; excludes earlier Classic, final report write and Qt delivery."]}
