from __future__ import annotations

import os
from typing import Any


def env_value(name: str, default: Any = None) -> Any:
    """Read current environment names, with support for legacy RECUT_* keys."""
    value = os.environ.get(name)
    if value is not None:
        return value
    if name.startswith("AUTO_CUTTER_"):
        legacy_name = "RECUT_" + name[len("AUTO_CUTTER_"):]
        legacy_value = os.environ.get(legacy_name)
        if legacy_value is not None:
            return legacy_value
    return default


def safe_int(value: str) -> int | None:
    value = value.strip()
    if not value or value.upper() == "N/A":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def parse_out_time_hms(value: str) -> float | None:
    """Parse an FFmpeg progress timestamp in HH:MM:SS.micro format."""
    value = value.strip()
    if not value or value.upper() == "N/A":
        return None
    try:
        hours, minutes, seconds = value.split(":")
        return int(hours) * 3600.0 + int(minutes) * 60.0 + float(seconds)
    except (TypeError, ValueError):
        return None


class ExportCancelled(Exception):
    pass


class SmartHybridFallback(Exception):
    pass
