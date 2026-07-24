from __future__ import annotations

import os
import subprocess
from typing import Any


def _with_no_window_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    merged = dict(kwargs)
    if os.name != "nt":
        return merged

    # Prevent console popups when running ffmpeg/ffprobe from a GUI app on Windows.
    create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if create_no_window:
        merged["creationflags"] = int(merged.get("creationflags", 0)) | int(create_no_window)

    startupinfo = merged.get("startupinfo")
    if startupinfo is None and hasattr(subprocess, "STARTUPINFO"):
        si = subprocess.STARTUPINFO()
        si.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
        si.wShowWindow = 0
        merged["startupinfo"] = si

    return merged


def run_no_window(*popenargs: Any, **kwargs: Any) -> subprocess.CompletedProcess:
    return subprocess.run(*popenargs, **_with_no_window_kwargs(kwargs))


def popen_no_window(*popenargs: Any, **kwargs: Any) -> subprocess.Popen:
    return subprocess.Popen(*popenargs, **_with_no_window_kwargs(kwargs))
