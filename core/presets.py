from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import json
import os
from pathlib import Path
from typing import Any
import uuid

from utils.runtime_paths import config_root, resource_path


PRESET_VERSION = 1

_NORMALIZATION_DEFAULTS: dict[str, Any] = {
    "intensity": 45,
    "threshold_pct": 45,
    "pre_pad_s": 0.25,
    "post_pad_s": 0.25,
    "min_cut_s": 0.10,
    "gain_db": 0.0,
    "gain_affects_detection": False,
    "attack_ms": 120,
    "release_ms": 250,
    "smoothing_mode": "Medium",
    "merge_pauses_ms": 300,
    "normalize_lufs": False,
    "lufs_target": -14.0,
    "limiter": True,
}


def normalize_preset_cfg(
    cfg: Mapping[str, Any] | None,
    defaults: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize editor settings while retaining unknown fields and old aliases."""
    fallback = {**_NORMALIZATION_DEFAULTS, **dict(defaults or {})}
    out = deepcopy(dict(cfg or {}))
    for name in ("intensity", "threshold_pct"):
        out[name] = int(out.get(name, fallback[name]))
    if "pre_pad_s" not in out:
        out["pre_pad_s"] = out.get("preroll_s", fallback["pre_pad_s"])
    if "post_pad_s" not in out:
        if "aggressiveness" in out:
            aggressiveness = max(0.0, min(100.0, float(out["aggressiveness"])))
            out["post_pad_s"] = round(0.05 + (aggressiveness / 100.0) * 0.95, 2)
        else:
            out["post_pad_s"] = fallback["post_pad_s"]
    for name in ("pre_pad_s", "post_pad_s", "min_cut_s", "gain_db", "lufs_target"):
        out[name] = float(out.get(name, fallback[name]))
    for name in ("attack_ms", "release_ms", "merge_pauses_ms"):
        out[name] = int(out.get(name, fallback[name]))
    for name in ("gain_affects_detection", "normalize_lufs", "limiter"):
        out[name] = bool(out.get(name, fallback[name]))
    out["smoothing_mode"] = str(out.get("smoothing_mode", fallback["smoothing_mode"]))
    return out


def default_presets_catalog() -> dict[str, dict[str, Any]]:
    common = {
        "gain_db": 0.0,
        "gain_affects_detection": False,
        "attack_ms": 120,
        "release_ms": 250,
        "smoothing_mode": "Medium",
        "merge_pauses_ms": 300,
        "normalize_lufs": False,
        "lufs_target": -14.0,
        "limiter": True,
    }
    return {
        "Balanced (Default)": {
            "intensity": 50, "threshold_pct": 6,
            "pre_pad_s": 0.25, "post_pad_s": 0.62, "min_cut_s": 0.10, **common,
        },
        "Natural Speech": {
            "intensity": 34, "threshold_pct": 3,
            "pre_pad_s": 0.28, "post_pad_s": 0.84, "min_cut_s": 0.10, **common,
        },
        "Aggressive Cleanup": {
            "intensity": 72, "threshold_pct": 10,
            "pre_pad_s": 0.18, "post_pad_s": 0.42, "min_cut_s": 0.08, **common,
        },
    }


def default_preset_name(catalog: Mapping[str, Any]) -> str | None:
    """Select the editor's preferred default from a user catalog."""
    if not catalog:
        return None
    for preferred in ("Balanced (Default)", "Gameplay (Default)", "Default"):
        for name in catalog:
            if name.lower() == preferred.lower():
                return name
    for name in catalog:
        if "(default)" in name.lower():
            return name
    return sorted(catalog, key=lambda name: name.lower())[0]


class PresetRepository:
    """Read and save the same user catalog used by the desktop editor."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path).expanduser() if path is not None else config_root() / "presets.json"
        self._use_legacy = path is None

    @staticmethod
    def _normalize_catalog(raw: object) -> dict[str, dict[str, Any]]:
        if not isinstance(raw, Mapping):
            raise ValueError("Preset catalog must be an object.")
        catalog = {}
        for name, cfg in raw.items():
            if not isinstance(name, str) or not isinstance(cfg, Mapping):
                continue
            if not name.strip() or name.strip().lower() == "manual":
                continue
            catalog[name] = normalize_preset_cfg(cfg)
        return catalog

    def load(self) -> dict[str, dict[str, Any]]:
        source = self.path
        if not source.is_file() and self._use_legacy:
            source = resource_path("presets.json")
        if source.is_file():
            loaded = self._normalize_catalog(json.loads(source.read_text(encoding="utf-8")))
            if loaded:
                return loaded
        return default_presets_catalog()

    def save(self, catalog: Mapping[str, Mapping[str, Any]]) -> None:
        normalized = self._normalize_catalog(catalog)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def resolve(self, name: str) -> dict[str, Any]:
        catalog = self.load()
        if name not in catalog:
            raise KeyError(f"Unknown preset: {name}")
        return deepcopy(catalog[name])
