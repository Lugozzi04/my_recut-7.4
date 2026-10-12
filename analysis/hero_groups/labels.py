"""Local Hearthstone class labels/colors and optional bundled Hero Power references."""

from __future__ import annotations
from pathlib import Path
import sys
from typing import Any
from utils.runtime_paths import is_frozen, resource_path

CLASS_COLORS = {
    "death_knight": "#C41E3A",
    "demon_hunter": "#A330C9",
    "druid": "#FF7C0A",
    "hunter": "#AAD372",
    "mage": "#3FC7EB",
    "paladin": "#F48CBA",
    "priest": "#FFFFFF",
    "rogue": "#FFF468",
    "shaman": "#0070DD",
    "warlock": "#8788EE",
    "warrior": "#C69B6D",
}
ALIASES = {"dk": "death_knight", "dh": "demon_hunter", "deathknight": "death_knight", "demonhunter": "demon_hunter"}


def class_id(value: Any) -> str | None:
    text = str(value or "").strip().lower().replace(" ", "_").replace("-", "_")
    text = ALIASES.get(text, text)
    return text if text in CLASS_COLORS else None


def class_label(value: Any) -> str:
    key = class_id(value)
    return key.replace("_", " ").title() if key else "Da assegnare"


def default_class_refs() -> Path:
    if is_frozen():
        external = Path(sys.executable).resolve().parent / "class_refs"
        if external.is_dir():
            return external
    return resource_path("class_refs")


def display_group(group: dict[str, Any]) -> str:
    return str(group.get("name") or class_label(group.get("class_name")))


def timeline_islands(metadata: list[dict[str, Any]], grouping: dict[str, Any]) -> list[dict[str, Any]]:
    """Decorate a copy for display only: no edit/boundary mutation."""
    rows = grouping.get("games", [])
    by_id = {str(r.get("key")): r for r in rows}
    by_index = {r["game"]["index"]: r for r in rows}
    result = []
    for original in metadata:
        item = dict(original)
        row = by_id.get(str(item.get("game_id"))) or by_index.get(item.get("game_index"))
        if row:
            group = grouping.get("groups", {}).get(row.get("group"), {})
            hero_class = class_id(group.get("class_name")) or class_id(group.get("name"))
            if hero_class:
                item["hero_class"] = hero_class
        result.append(item)
    return result
