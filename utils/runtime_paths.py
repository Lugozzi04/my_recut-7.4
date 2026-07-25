from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def project_root() -> Path:
    """
    Runtime-safe app root.

    - Source mode: project folder (parent of utils/)
    - PyInstaller onefile/onedir: extraction/bundle root
    """
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass).resolve()
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def resource_path(*parts: str) -> Path:
    return project_root().joinpath(*parts)


def cache_root() -> Path:
    """Return a writable, user-local cache directory."""
    custom = str(os.environ.get("AUTO_CUTTER_CACHE_DIR", "") or "").strip()
    if custom:
        return Path(custom).expanduser().resolve()

    if os.name == "nt":
        base = str(os.environ.get("LOCALAPPDATA", "") or "").strip()
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / "Auto Cutter" / "cache"

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "Auto Cutter"

    base = str(os.environ.get("XDG_CACHE_HOME", "") or "").strip()
    root = Path(base).expanduser() if base else Path.home() / ".cache"
    return root / "auto-cutter"
