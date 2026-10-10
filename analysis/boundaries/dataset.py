"""Versioned JSONL training examples: BoundaryContext + chosen boundary. No training here.

One line per labelled boundary:
  {"format": "recut_boundary_example", "version": 1, "label": {...}, "context": <BoundaryContext mapping>}
label.timestamp is the editor's chosen cut point; label.candidate_index is the
matching candidate (nearest within tolerance) or null when none matches, which
is itself useful to measure candidate-generation recall. candidate_rows()
flattens an example into (feature values, mask, target, distance) rows for a
per-candidate scorer; a listwise model can group rows by example id.
"""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any

from .candidates import FEATURE_NAMES, feature_vector
from .models import BoundaryContext, BoundaryFormatError

EXAMPLE_FORMAT = "recut_boundary_example"
EXAMPLE_VERSION = 1


@dataclass
class BoundaryLabel:
    timestamp: float
    candidate_index: int | None
    labeler: str  # e.g. "manual", "agent:<model>", "exact-baseline"
    source: str = "manual"
    notes: str = ""

    def to_mapping(self) -> dict[str, Any]:
        return {"timestamp": self.timestamp, "candidate_index": self.candidate_index, "labeler": self.labeler,
                "source": self.source, "notes": self.notes}


def match_candidate(context: BoundaryContext, timestamp: float, tolerance_s: float = 0.25) -> int | None:
    best = min(range(len(context.candidates)), key=lambda i: abs(context.candidates[i].timestamp - timestamp),
               default=None)
    if best is None or abs(context.candidates[best].timestamp - timestamp) > tolerance_s:
        return None
    return best


def example_record(context: BoundaryContext, label: BoundaryLabel) -> dict[str, Any]:
    ctx = context.to_mapping()
    identity = json.dumps([ctx["video"].get("fingerprint") or ctx["video"].get("path"), ctx["game"]["index"],
                           ctx["boundary_type"], ctx["window"]], sort_keys=True)
    return {"format": EXAMPLE_FORMAT, "version": EXAMPLE_VERSION,
            "id": hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16],
            "label": label.to_mapping(), "context": ctx}


def append_example(path: str | Path, context: BoundaryContext, label: BoundaryLabel) -> dict[str, Any]:
    record = example_record(context, label)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
    return record


def iter_examples(path: str | Path) -> Iterator[tuple[str, BoundaryContext, Mapping[str, Any]]]:
    with Path(path).open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            raw = json.loads(line)
            if raw.get("format") != EXAMPLE_FORMAT or raw.get("version") != EXAMPLE_VERSION:
                raise BoundaryFormatError(f"line {number}: unsupported example format")
            yield str(raw.get("id", number)), BoundaryContext.from_mapping(raw["context"]), raw["label"]


def candidate_rows(context: BoundaryContext, label: Mapping[str, Any], names: list[str] | None = None
                   ) -> list[dict[str, Any]]:
    names = names or FEATURE_NAMES
    chosen = label.get("candidate_index")
    target_t = float(label["timestamp"])
    rows = []
    for index, candidate in enumerate(context.candidates):
        values, mask = feature_vector(candidate, names)
        rows.append({"candidate_index": index, "boundary_type": context.boundary_type.value,
                     "features": values, "mask": mask, "target": 1.0 if index == chosen else 0.0,
                     "distance_to_label_s": candidate.timestamp - target_t})
    return rows
