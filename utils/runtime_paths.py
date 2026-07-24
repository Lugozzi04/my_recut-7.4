from __future__ import annotations

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

