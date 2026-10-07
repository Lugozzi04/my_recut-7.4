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


def _override(name: str) -> Path | None:
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser().resolve() if value else None


def config_root() -> Path:
    """Writable configuration, preserving the GUI's historical preset location."""
    custom = _override("AUTO_CUTTER_CONFIG_DIR")
    if custom is not None:
        return custom
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return base / "Auto Cutter" / "Auto Cutter"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Auto Cutter" / "Auto Cutter"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "Auto Cutter" / "Auto Cutter"


def data_root() -> Path:
    custom = _override("AUTO_CUTTER_DATA_DIR")
    if custom is not None:
        return custom
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Auto Cutter"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Auto Cutter"
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "auto-cutter"


def credentials_root() -> Path:
    return _override("AUTO_CUTTER_CREDENTIALS_DIR") or data_root() / "credentials"


def logs_root() -> Path:
    return config_root() / "session_logs"


def exports_root() -> Path:
    return _override("AUTO_CUTTER_OUTPUT_DIR") or Path.home() / "Videos" / "Auto Cutter" / "Exports"


def initialize_qt_settings() -> None:
    """Configure QtCore only; neither a GUI nor an application instance is needed."""
    from PySide6.QtCore import QCoreApplication, QSettings

    QCoreApplication.setOrganizationName("Auto Cutter")
    QCoreApplication.setApplicationName("Auto Cutter")
    custom = _override("AUTO_CUTTER_CONFIG_DIR")
    if custom is not None:
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(custom))


def cache_root() -> Path:
    """Return a writable, user-local cache directory."""
    custom = str(os.environ.get("AUTO_CUTTER_CACHE_DIR", "") or "").strip()
    if custom:
        return Path(custom).expanduser().resolve()
    if _override("AUTO_CUTTER_DATA_DIR") is not None:
        return data_root() / "cache"

    if os.name == "nt":
        base = str(os.environ.get("LOCALAPPDATA", "") or "").strip()
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / "Auto Cutter" / "cache"

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "Auto Cutter"

    base = str(os.environ.get("XDG_CACHE_HOME", "") or "").strip()
    root = Path(base).expanduser() if base else Path.home() / ".cache"
    return root / "auto-cutter"
