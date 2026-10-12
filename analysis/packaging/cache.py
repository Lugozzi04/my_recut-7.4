"""Separate, content-addressed caches for each packaging stage.

Namespaces: transcript (kept-region lines per ASR chunk), evidence (audio/visual
features per span), frames (extracted images, keyed by source identity + time),
agent (validated agent answers keyed by task, provider, model, prompt version and
input digest), render (thumbnail files keyed by the composition plan and assets).
Keys always include the input identity and the evidence version, so a change in
the edit only invalidates the artifacts that depend on the modified ranges.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Mapping

from .models import EVIDENCE_VERSION

NAMESPACES = ("transcript", "evidence", "frames", "agent", "render", "generated")


def stable_key(*parts: Any) -> str:
    value = json.dumps([EVIDENCE_VERSION, *parts], sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class PackagingCache:
    def __init__(self, root: str | Path | None = None, *, enabled: bool = True) -> None:
        if root is None:
            from utils.runtime_paths import cache_root
            root = cache_root() / "packaging"
        self.root = Path(root)
        self.enabled = enabled
        self.stats: dict[str, dict[str, int]] = {ns: {"hit": 0, "miss": 0} for ns in NAMESPACES}

    def directory(self, namespace: str) -> Path:
        if namespace not in NAMESPACES:
            raise ValueError(f"unknown cache namespace {namespace}")
        return self.root / namespace

    def load_json(self, namespace: str, key: str) -> Any | None:
        if not self.enabled:
            self.stats[namespace]["miss"] += 1
            return None
        try:
            raw = json.loads((self.directory(namespace) / f"{key}.json").read_text(encoding="utf-8"))
            if raw.get("key") != key or raw.get("version") != EVIDENCE_VERSION:
                raise ValueError("stale")
        except (OSError, ValueError, AttributeError):
            self.stats[namespace]["miss"] += 1
            return None
        self.stats[namespace]["hit"] += 1
        return raw.get("value")

    def store_json(self, namespace: str, key: str, value: Any) -> None:
        if not self.enabled:
            return
        from analysis.gameplay.cache import atomic_write_json

        try:
            atomic_write_json(self.directory(namespace) / f"{key}.json",
                              {"format": "recut_packaging_cache", "namespace": namespace, "key": key,
                               "version": EVIDENCE_VERSION, "value": value})
        except (OSError, TypeError, ValueError):
            pass

    def file_path(self, namespace: str, key: str, suffix: str) -> Path:
        return self.directory(namespace) / f"{key}{suffix}"

    def load_file(self, namespace: str, key: str, suffix: str, target: Path) -> bool:
        """Copy a cached binary artifact to target; False on a miss."""
        source = self.file_path(namespace, key, suffix)
        if not self.enabled or not source.is_file():
            self.stats[namespace]["miss"] += 1
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        self.stats[namespace]["hit"] += 1
        return True

    def store_file(self, namespace: str, key: str, suffix: str, source: Path) -> None:
        if not self.enabled:
            return
        target = self.file_path(namespace, key, suffix)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.tmp")
            shutil.copyfile(source, temporary)
            temporary.replace(target)
        except OSError:
            pass

    def summary(self) -> Mapping[str, Any]:
        return {"root": str(self.root), "enabled": self.enabled, "stats": self.stats}
