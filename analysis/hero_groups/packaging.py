"""Verified group metadata for the existing Packaging prompts; no agent calls."""

from __future__ import annotations
import json
from typing import Any


def group_context(payload: dict[str, Any] | None) -> list[str]:
    from .labels import class_id, class_label

    selection = (payload or {}).get("export_selection") or {}
    classes = sorted({name for r in selection.get("intervals", [])
                      if r.get("role") == "gameplay" and r.get("included")
                      and (name := class_id(r.get("output_group") or r.get("group"))) is not None})
    if classes:
        return ["EXPORTED PLAYER CLASSES: " + json.dumps([class_label(c) for c in classes]),
                "These are the PLAYER's classes in this exact exported edit. Only its kept games may supply "
                "thumbnail/story evidence. Never replace the player's class with an opponent's class or "
                "a class mentioned in the Intro. A class does not identify a deck archetype."]
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
