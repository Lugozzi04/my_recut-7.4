from __future__ import annotations

from typing import Any

from utils.runtime_paths import initialize_qt_settings


def get_setting(key: str, default: Any = None) -> Any:
    from PySide6.QtCore import QSettings

    initialize_qt_settings()
    return QSettings("Auto Cutter", "Auto Cutter").value(key, default)


def set_setting(key: str, value: Any) -> None:
    from PySide6.QtCore import QSettings

    initialize_qt_settings()
    settings = QSettings("Auto Cutter", "Auto Cutter")
    settings.setValue(key, value)
    settings.sync()
