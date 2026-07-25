from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import threading
import traceback
from types import TracebackType
from typing import Type


def crash_logs_dir() -> Path:
    override = str(os.environ.get("AUTO_CUTTER_CRASH_DIR", "") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    base = str(os.environ.get("LOCALAPPDATA", "") or "").strip()
    root = Path(base) if base else Path.home() / "AppData" / "Local"
    return root / "Auto Cutter" / "crash_logs"


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
    output.write_text(content, encoding="utf-8", errors="replace")
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
            previous_sys_hook(exception_type, exception, tb)

    sys.excepthook = handle_sys

    if hasattr(threading, "excepthook"):
        previous_thread_hook = threading.excepthook

        def handle_thread(args: threading.ExceptHookArgs) -> None:
            try:
                write_crash_log(args.exc_type, args.exc_value, args.exc_traceback)
            finally:
                previous_thread_hook(args)

        threading.excepthook = handle_thread
