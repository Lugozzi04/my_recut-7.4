from __future__ import annotations

from functools import lru_cache

from utils.runtime_paths import resource_path


@lru_cache(maxsize=1)
def app_version() -> str:
    try:
        value = resource_path("VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        value = ""
    return value or "0.0.0-dev"
