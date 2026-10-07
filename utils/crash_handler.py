from __future__ import annotations

from datetime import datetime, timezone
from contextlib import redirect_stderr
import io
import os
from pathlib import Path
import sys
import threading
import traceback
from types import TracebackType
from typing import Any, Callable, Type

from utils.redaction import redact_secrets
from utils.runtime_paths import data_root


_hook_reporting_lock = threading.RLock()


def _report_hook_safely(hook: Callable[..., Any], *args: Any) -> None:
    """Keep the original traceback reporter while redacting its stderr output."""
    with _hook_reporting_lock:
        destination = sys.stderr
        captured = io.StringIO()
        try:
            with redirect_stderr(captured):
                hook(*args)
        finally:
            if destination is not None:
                try:
                    destination.write(redact_secrets(captured.getvalue()))
                    destination.flush()
                except (OSError, ValueError):
                    # A closed terminal must not replace the original hook error.
                    pass


def crash_logs_dir() -> Path:
    override = str(os.environ.get("AUTO_CUTTER_CRASH_DIR", "") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return data_root() / "crash_logs"


def write_crash_log(
    exception_type: Type[BaseException],
    exception: BaseException,
    tb: TracebackType | None,
) -> Path:
    directory = crash_logs_dir()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    output = directory / f"crash_{stamp}.log"
    content = "".join(traceback.format_exception(exception_type, exception, tb))
    output.write_text(redact_secrets(content), encoding="utf-8", errors="replace")
    return output


def install_global_exception_handler() -> None:
    previous_sys_hook = sys.excepthook

    def handle_sys(
        exception_type: Type[BaseException],
        exception: BaseException,
        tb: TracebackType | None,
    ) -> None:
        try:
            write_crash_log(exception_type, exception, tb)
        finally:
            _report_hook_safely(previous_sys_hook, exception_type, exception, tb)

    sys.excepthook = handle_sys

    if hasattr(threading, "excepthook"):
        previous_thread_hook = threading.excepthook

        def handle_thread(args: threading.ExceptHookArgs) -> None:
            try:
                if args.exc_value is not None:
                    write_crash_log(args.exc_type, args.exc_value, args.exc_traceback)
            finally:
                _report_hook_safely(previous_thread_hook, args)

        threading.excepthook = handle_thread
