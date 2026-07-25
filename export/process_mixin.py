from __future__ import annotations

import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable

from utils.subprocess_utils import popen_no_window

from .runtime_utils import ExportCancelled, env_value, parse_out_time_hms, safe_int


class ExportProcessMixin:
    """Manage FFmpeg subprocesses, cancellation, and progress parsing."""

    def _init_process_control(self) -> None:
        self._cancelled = False
        self._proc_lock = threading.Lock()
        self._active_procs: set[subprocess.Popen] = set()

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
            for proc in procs:
                self._terminate_proc(proc)
        except Exception:
            pass

    def _check_cancelled(self) -> None:
        if self._cancelled:
            raise ExportCancelled("Export cancelled.")

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

            thread = threading.Thread(target=_drain_stderr, daemon=True)
            thread.start()
            try:
                heartbeat_sec = float(
                    env_value("AUTO_CUTTER_FFMPEG_HEARTBEAT_SEC", "5").strip() or "5"
                )
            except Exception:
                heartbeat_sec = 5.0
            heartbeat_sec = max(0.0, heartbeat_sec)
            started_at = time.monotonic()
            last_heartbeat = started_at
            output_hint = str(cmd_run[-1]) if cmd_run else ""

            try:
                while True:
                    if self._cancelled:
                        self._terminate_proc(proc)
                        raise ExportCancelled("Export cancelled.")
                    if heartbeat_sec > 0.0:
                        now = time.monotonic()
                        if (now - last_heartbeat) >= heartbeat_sec:
                            self._log(
                                f"ffmpeg_wait elapsed={now - started_at:.1f}s "
                                f"target={output_hint}"
                            )
                            last_heartbeat = now
                    return_code = proc.poll()
                    if return_code is not None:
                        try:
                            thread.join(timeout=1.0)
                        except Exception:
                            pass
                        return int(return_code), "\n".join(err_lines)
                    time.sleep(0.1)
            finally:
                self._unregister_proc(proc)
                try:
                    if proc.stderr is not None:
                        proc.stderr.close()
                except Exception:
                    pass

        return_code, error_text = _run_once(cmd)
        if return_code != 0 and ("-hwaccel" in cmd or "-hwaccel_output_format" in cmd):
            cmd_no_hw = self._strip_hwaccel_args(cmd)
            self._log("hwaccel_failed -> retry cpu decode")
            self._disable_hwaccel_for_export("ffmpeg_run_failed")
            return_code, error_text = _run_once(cmd_no_hw)

        return int(return_code), "", (error_text or "")

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
            line_queue = deque()
            queue_lock = threading.Lock()
            reader_done = threading.Event()
            out_time = 0.0
            try:
                stall_warn_s = float(
                    env_value("AUTO_CUTTER_FFMPEG_PROGRESS_STALL_WARN_SEC", "90").strip()
                    or "90"
                )
            except Exception:
                stall_warn_s = 90.0
            try:
                stall_kill_s = float(
                    env_value("AUTO_CUTTER_FFMPEG_PROGRESS_STALL_KILL_SEC", "420").strip()
                    or "420"
                )
            except Exception:
                stall_kill_s = 420.0
            try:
                stall_eps = float(
                    env_value("AUTO_CUTTER_FFMPEG_PROGRESS_STALL_EPS", "0.20").strip()
                    or "0.20"
                )
            except Exception:
                stall_eps = 0.20
            stall_warn_s = max(0.0, float(stall_warn_s))
            stall_kill_s = max(0.0, float(stall_kill_s))
            stall_eps = max(0.001, float(stall_eps))
            try:
                heartbeat_sec = float(
                    env_value("AUTO_CUTTER_FFMPEG_PROGRESS_HEARTBEAT_SEC", "5").strip()
                    or "5"
                )
            except Exception:
                heartbeat_sec = 5.0
            heartbeat_sec = max(0.0, heartbeat_sec)
            started_at = time.monotonic()
            last_heartbeat = started_at
            last_activity = started_at
            last_progress = started_at
            last_reported_out = 0.0
            stall_warned = False
            output_hint = str(cmd_run[-1]) if cmd_run else ""

            def _drain_stderr() -> None:
                nonlocal last_activity
                try:
                    assert proc.stderr is not None
                    for raw in proc.stderr:
                        if raw is None:
                            continue
                        line = raw.strip()
                        if not line:
                            continue
                        with queue_lock:
                            line_queue.append(line)
                            err_lines.append(line)
                        last_activity = time.monotonic()
                except Exception:
                    pass
                finally:
                    reader_done.set()

            def _consume_progress() -> None:
                nonlocal out_time, last_activity, last_progress
                nonlocal last_reported_out, stall_warned
                while True:
                    with queue_lock:
                        if not line_queue:
                            break
                        line = line_queue.popleft()
                    parsed_time = None
                    if line.startswith("out_time_ms=") or line.startswith("out_time_us="):
                        parsed_value = safe_int(line.split("=", 1)[1])
                        if parsed_value is not None:
                            parsed_time = parsed_value / 1_000_000.0
                    elif line.startswith("out_time="):
                        parsed_time = parse_out_time_hms(line.split("=", 1)[1])

                    if parsed_time is None:
                        continue
                    out_time = parsed_time
                    if out_time >= (last_reported_out + stall_eps):
                        last_reported_out = out_time
                        last_progress = time.monotonic()
                        last_activity = last_progress
                        stall_warned = False
                    if on_progress:
                        on_progress(out_time)

            thread = threading.Thread(target=_drain_stderr, daemon=True)
            thread.start()
            try:
                while True:
                    if self._cancelled:
                        self._terminate_proc(proc)
                        raise ExportCancelled("Export cancelled.")
                    _consume_progress()
                    if heartbeat_sec > 0.0:
                        now = time.monotonic()
                        if (now - last_heartbeat) >= heartbeat_sec:
                            self._log(
                                f"ffmpeg_progress_wait elapsed={now - started_at:.1f}s "
                                f"out_time={out_time:.1f}s target={output_hint}"
                            )
                            last_heartbeat = now
                    now = time.monotonic()
                    idle_for = max(0.0, now - max(last_activity, last_progress))
                    if stall_warn_s > 0.0 and not stall_warned and idle_for >= stall_warn_s:
                        self._log(
                            f"ffmpeg_stall_warning idle={idle_for:.1f}s "
                            f"out_time={out_time:.1f}s target={output_hint}"
                        )
                        stall_warned = True
                    if stall_kill_s > 0.0 and idle_for >= stall_kill_s:
                        stall_message = (
                            f"ffmpeg_stall_detected idle={idle_for:.1f}s "
                            f"out_time={out_time:.1f}s target={output_hint}"
                        )
                        self._log(stall_message + " -> terminate")
                        try:
                            err_lines.append(stall_message)
                        except Exception:
                            pass
                        self._terminate_proc(proc)
                    return_code = proc.poll()
                    if return_code is not None:
                        try:
                            thread.join(timeout=1.0)
                        except Exception:
                            pass
                        _consume_progress()
                        return int(return_code), "\n".join(err_lines)
                    if reader_done.is_set():
                        # The process can still be running after closing stderr.
                        pass
                    time.sleep(0.1)
            finally:
                self._unregister_proc(proc)
                try:
                    if proc.stderr is not None:
                        proc.stderr.close()
                except Exception:
                    pass

        return_code, error_text = _run_once(cmd)
        if return_code != 0 and ("-hwaccel" in cmd or "-hwaccel_output_format" in cmd):
            cmd_no_hw = self._strip_hwaccel_args(cmd)
            self._log("hwaccel_failed -> retry cpu decode")
            self._disable_hwaccel_for_export("ffmpeg_progress_failed")
            return_code, error_text = _run_once(cmd_no_hw)

        return int(return_code), error_text
