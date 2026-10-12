"""Verified group metadata for the existing Packaging prompts; no agent calls."""

from __future__ import annotations
import json
from typing import Any


def group_context(payload: dict[str, Any] | None) -> list[str]:
    group = (payload or {}).get("hero_group_output") or {}
    if not group:
        return []
    return [
        "PLAYER PORTRAIT GROUP: "
        + json.dumps({"group": group.get("group"), "creator_label": group.get("name")}, ensure_ascii=True),
        "Groups identify player SKINS only; no deck/archetype was inferred.",
        (
            "The group is anonymous. Its class is UNKNOWN. Do not invent a class name or archetype for titles, "
            "thumbnails, captions or story claims."
            if not group.get("name")
            else "Use the supplied creator label only; do not infer a deck archetype from it."
        ),
    ]
