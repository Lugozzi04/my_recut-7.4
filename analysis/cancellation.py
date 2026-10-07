from __future__ import annotations

import os
import subprocess
import threading
from typing import Any

from utils.subprocess_utils import popen_no_window, run_no_window


class AiPipelineCancelled(RuntimeError):
    def __init__(self) -> None:
        super().__init__("__CANCELLED__")


class AiCancellationToken:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self.cancelled:
            raise AiPipelineCancelled()

    def cancel(self) -> None:
        self._event.set()
        with self._lock:
            process = self._process
        if process is not None:
            self._terminate_process(process)

    def run(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        self.check()
        popen_kwargs = dict(kwargs)
        capture_output = bool(popen_kwargs.pop("capture_output", False))
        timeout = popen_kwargs.pop("timeout", None)
        if capture_output:
            popen_kwargs.setdefault("stdout", subprocess.PIPE)
            popen_kwargs.setdefault("stderr", subprocess.PIPE)

        process = popen_no_window(args, **popen_kwargs)
        with self._lock:
            self._process = process
            cancel_now = self.cancelled
        if cancel_now:
            self._terminate_process(process)

        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._terminate_process(process)
            stdout, stderr = process.communicate()
            raise
        finally:
            with self._lock:
                if self._process is process:
                    self._process = None

        self.check()
        return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)

    @staticmethod
    def _terminate_process(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            try:
                result = run_no_window(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=1.5,
                )
                if result.returncode == 0:
                    try:
                        process.wait(timeout=0.75)
                    except subprocess.TimeoutExpired:
                        pass
                    if process.poll() is not None:
                        return
            except Exception:
                pass
        try:
            process.terminate()
            process.wait(timeout=0.75)
            return
        except Exception:
            pass
        try:
            process.kill()
            process.wait(timeout=0.75)
        except Exception:
            pass
