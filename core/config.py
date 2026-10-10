from __future__ import annotations

from typing import Any

from utils.runtime_paths import app_settings, initialize_qt_settings


def get_setting(key: str, default: Any = None) -> Any:
    initialize_qt_settings()
    return app_settings().value(key, default)


def set_setting(key: str, value: Any) -> None:
    initialize_qt_settings()
    settings = app_settings()
    settings.setValue(key, value)
    settings.sync()
