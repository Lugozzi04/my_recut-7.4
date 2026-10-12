"""Multi-frame class assignment from Hero Power and known skins in this VOD.

Best-match assignments are explicitly marked estimated/conflict when evidence is weak.
No global skin memory, training, cloud calls or changes to game results.
"""

from __future__ import annotations
from collections import Counter
from copy import deepcopy
from typing import Any
import numpy as np
from .labels import class_id


def classify_records(records: list[dict[str, Any]], settings: Any) -> None:
    from .recognition import similarity

    # Only strong direct Hero Power evidence can label a skin exemplar.
    anchors = [
        r for r in records if r.get("_signature") is not None and r.get("power_evidence", {}).get("status") == "matched"
    ]
    for record in records:
        power = record.get("power_evidence", {})
        skin_scores: dict[str, float] = {}
        skin_support: dict[str, int] = {}
        for anchor in anchors:
            if anchor is record:
                continue
            values = []
            for feat in record.get("_sample_features", []):
                structural, color = similarity(feat, anchor["_signature"])
                if color >= settings.color_similarity:
                    values.append(structural)
            values.sort(reverse=True)
            if len(values) < settings.min_samples:
                continue
            value = float(np.mean(values[: settings.min_samples]))
            support = sum(s >= settings.similarity for s in values)
            if support < settings.min_samples:
                continue
            key = anchor["power_evidence"]["class"]
            if value > skin_scores.get(key, 0):
                skin_scores[key], skin_support[key] = value, support
        combined = dict(power.get("scores", {}))
        for key, value in skin_scores.items():
            combined[key] = max(combined.get(key, 0.0), value)
        ranked = sorted(combined, key=lambda key: (-combined[key], key))
        if not ranked:
            record["class_evidence"] = None
            record["class_status"] = "no_samples"
            continue
        winner = ranked[0]
        via_skin = skin_scores.get(winner, 0.0) > power.get("scores", {}).get(winner, 0.0)
        conflict = bool(
            skin_scores
            and power.get("status") == "matched"
            and max(skin_scores, key=lambda name: skin_scores[name]) != power.get("class")
        )
        record.update(
            {
                "class_evidence": winner,
                "class_scores": combined,
                "skin_scores": skin_scores,
                "class_similarity": combined[winner],
                "class_margin": combined[winner] - (combined[ranked[1]] if len(ranked) > 1 else 0.0),
                "class_support": skin_support.get(winner, 0) if via_skin else power.get("support", 0),
                "class_source": "portrait" if via_skin else "hero_power",
                "class_status": "conflict"
                if conflict
                else ("matched" if via_skin or power.get("status") == "matched" else "estimated"),
            }
        )


def class_groups(
    records: list[dict[str, Any]], settings: Any, previous: dict[str, Any] | None = None
) -> dict[str, Any]:
    from .recognition import VERSION, game_key, refresh_groups
    from dataclasses import asdict

    previous = deepcopy(previous or {})
    current = {game_key(r["game"]): r for r in records}
    # Migrate A/B/C projects and all edits to named groups without losing corrections.
    old_to_class: dict[str, str] = {}
    for key, group in previous.get("groups", {}).items():
        found = class_id(key) or class_id(group.get("name")) or class_id(group.get("class_name"))
        if not found:
            votes = Counter(current[k].get("class_evidence") for k in group.get("games", []) if k in current)
            votes.pop(None, None)
            if votes:
                found = sorted(votes, key=lambda c: (-votes[c], c))[0]
        old_to_class[key] = found or key
    output: dict[str, Any] = {
        "version": VERSION,
        "settings": asdict(settings),
        "grouping_mode": "class",
        "games": [],
        "overrides": {k: old_to_class.get(v, v) for k, v in previous.get("overrides", {}).items()},
        "names": {old_to_class.get(k, k): v for k, v in previous.get("names", {}).items()},
        "selected": [old_to_class.get(g, g) for g in previous["selected"]]
        if previous.get("selected") is not None
        else None,
        "groups": {},
        "allocated_ids": [],
    }
    for record in sorted(
        records, key=lambda r: (r["game"].get("start") is None, r["game"].get("start") or 0, game_key(r["game"]))
    ):
        row = {k: v for k, v in record.items() if not k.startswith("_")}
        row["key"] = game_key(row["game"])
        row["automatic_group"] = row.get("class_evidence")
        row["group"] = output["overrides"].get(row["key"], row["automatic_group"])
        output["games"].append(row)
    refresh_groups(output)
    return output
