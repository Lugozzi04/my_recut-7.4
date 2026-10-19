"""Portfolio planning model and the deterministic diversity evaluator (no LLM call).

A PortfolioStrategy is the agent's plan for the WHOLE set of thumbnails: which opportunity,
story, treatment and primary subject each concept takes and how it differs from the others.
evaluate_portfolio() checks that plan against the evidence and the scene graphs:

  blocking     the set is not a portfolio (same story told the same way, same message,
               duplicated subject + composition) although the material allowed better
  warning      worth a look (shared highlights, shared assets, similar layout)
  compromise   a repetition the material justifies (few opportunities or treatments available)

Assets derived from the same source are one "family" (S010 cut-out of W010, B003 region of
F021, K02 card in F005): reusing a family counts as reusing the frame.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .scene import composition_signature, primary_layer, scene_assets

PORTFOLIO_VERSION = "portfolio_v1"
STOPWORDS = {"il", "lo", "la", "i", "gli", "le", "un", "uno", "una", "di", "da", "a", "in", "con", "su", "per", "tra",
             "fra", "e", "o", "ma", "che", "non", "mi", "ti", "si", "ci", "è", "e'", "del", "della", "dei", "al",
             "alla", "nel", "nella", "questo", "questa", "quello", "quella", "the", "a", "an", "of", "to", "and", "or",
             "is", "my", "this", "that", "it", "on", "in", "for", "with", "how", "come", "cosa", "qui", "ora"}
_TOKEN = re.compile(r"[\wàèéìòóù']+", re.UNICODE)


def _obj(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


STRATEGY_SCHEMA = _obj({
    "rationale": {"type": "string", "description": "why these directions together cover the video best"},
    "plan": {"type": "array", "items": _obj({
        "concept_id": {"type": "string"},
        "opportunity_id": {"type": "string"},
        "story_type": {"type": "string"},
        "message": {"type": "string", "description": "the one idea the viewer gets in a second"},
        "treatment": {"type": "string"},
        "primary_subject": {"type": "string", "description": "asset id of the protagonist"},
        "differs_by": {"type": "string", "description": "what makes it different from the other concepts"},
    })},
    "tradeoffs": {"type": "string", "description": "repetitions forced by limited material, or empty"},
})


@dataclass
class DiversityFinding:
    severity: str  # blocking | warning | compromise
    code: str
    concepts: list[str]
    detail: str

    def to_mapping(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PortfolioDiversity:
    findings: list[DiversityFinding] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def blocking(self) -> list[DiversityFinding]:
        return [f for f in self.findings if f.severity == "blocking"]

    def to_mapping(self) -> dict[str, Any]:
        return {"version": PORTFOLIO_VERSION, "passed": not self.blocking, "metrics": self.metrics,
                "findings": [f.to_mapping() for f in self.findings]}


def family(asset_id: str | None, assets: Mapping[str, Mapping[str, Any]]) -> str | None:
    """Root source of an asset (cut-out -> webcam crop, region/card -> frame)."""
    seen = 0
    current = asset_id
    while current and current in assets and assets[current].get("parent_id") and seen < 4:
        current = str(assets[current]["parent_id"])
        seen += 1
    return current


def content_words(text: str) -> set[str]:
    words = {t.lower() for t in _TOKEN.findall(text or "")}
    return {w[:5] for w in words - STOPWORDS if len(w) > 2}


def _jaccard(a: set[Any], b: set[Any]) -> float:
    return len(a & b) / len(a | b) if a or b else 0.0


def evaluate_portfolio(concepts: Sequence[Mapping[str, Any]], assets: Mapping[str, Mapping[str, Any]], *,
                       available_story_types: int, available_treatments: int, available_opportunities: int,
                       rendered: Sequence[Path] | None = None) -> PortfolioDiversity:
    report = PortfolioDiversity()
    n = len(concepts)
    ids = [str(c.get("concept_id")) for c in concepts]
    narratives = [(str(c.get("story_type")), frozenset(c.get("highlight_ids") or [])) for c in concepts]
    treatments = [str(c.get("treatment")) for c in concepts]
    primaries = []
    signatures: list[tuple[Any, ...]] = []
    for c in concepts:
        pack = c.get("design_pack") or {}
        comp = pack.get("composition") or {}
        if comp.get("background") and not comp.get("layers"):
            primaries.append(family(str(comp["background"]["asset_id"]), assets))
            signatures.append((pack["thumbnail_archetype"], "full_canvas_gameplay", pack["typography"]["position"]))
            continue
        if comp.get("layers"):
            main = comp["layers"][0]
            primaries.append(family(str(main["original_asset_id"]), assets))
            signatures.append((comp["family"], round(main["x"], 2), round(main["height"], 2),
                               pack["typography"]["position"]))
            continue
        scene = c.get("scene") or {}
        layer = primary_layer(scene)
        asset = (layer or {}).get("asset_id") or (scene.get("background") or {}).get("asset_id")
        primaries.append(family(str(c.get("primary_subject") or asset), assets) or str(asset))
        signatures.append(composition_signature(scene))
    texts = [content_words(str(c.get("thumbnail_text") or "")) for c in concepts]
    messages = [content_words(str(c.get("thumbnail_text") or "") + " " + str(c.get("narrative_angle") or ""))
                for c in concepts]
    distinct_narratives = len({story for story, _ in narratives}) if n else 0
    distinct_stories = len(set(narratives))
    want_angles = min(3, n, max(1, available_story_types), max(1, available_opportunities))
    report.metrics = {"concepts": n, "distinct_story_types": distinct_narratives, "distinct_narratives": distinct_stories,
                      "distinct_treatments": len(set(treatments)), "distinct_primary_sources": len(set(primaries)),
                      "distinct_compositions": len(set(signatures)),
                      "available": {"story_types": available_story_types, "treatments": available_treatments,
                                    "opportunities": available_opportunities}}

    def add(severity: str, code: str, members: Sequence[str], detail: str) -> None:
        report.findings.append(DiversityFinding(severity, code, list(members), detail))

    if distinct_narratives < want_angles:
        limited = available_story_types < 3 or available_opportunities < 3
        add("compromise" if limited else "blocking", "too_few_angles", ids,
            f"{distinct_narratives} distinct story types for {n} concepts (wanted {want_angles}; "
            f"material offers {available_story_types} story types / {available_opportunities} opportunities)")
    want_treatments = min(3, n, max(1, available_treatments))
    if len(set(treatments)) < want_treatments:
        add("compromise" if available_treatments < 3 else "blocking", "too_few_treatments", ids,
            f"{len(set(treatments))} treatments for {n} concepts (wanted {want_treatments})")
    for i in range(n):
        for j in range(i + 1, n):
            pair = [ids[i], ids[j]]
            if narratives[i][0] == narratives[j][0] and primaries[i] == primaries[j] and signatures[i] == signatures[j]:
                add("blocking", "duplicate_concept", pair,
                    f"same story ({narratives[i][0]}), same primary source ({primaries[i]}) and same composition")
            elif primaries[i] == primaries[j]:
                if treatments[i] != treatments[j] and narratives[i] != narratives[j]:
                    add("compromise", "shared_primary_source", pair,
                        f"both built on {primaries[i]} but with a different story and treatment")
                else:
                    add("warning", "shared_primary_source", pair, f"both built on {primaries[i]}")
            text_overlap = _jaccard(texts[i], texts[j])
            message_overlap = _jaccard(messages[i], messages[j])
            if texts[i] and texts[j] and text_overlap >= 0.5 and not (concepts[i].get("hook_locked") and concepts[j].get("hook_locked")):
                add("blocking", "same_message", pair, f"thumbnail texts share {text_overlap:.0%} of their content words")
            elif message_overlap >= 0.6:
                add("warning", "similar_message", pair, f"angle + text overlap {message_overlap:.0%}")
            highlight_overlap = _jaccard(set(narratives[i][1]), set(narratives[j][1]))
            if highlight_overlap >= 0.99 and narratives[i][0] == narratives[j][0]:
                add("warning", "same_moment_same_story", pair, "same highlights told as the same story type")
            if signatures[i] == signatures[j]:
                add("warning", "same_composition", pair, f"same composition signature {signatures[i]}")
    background_families = [family((c.get("scene") or {}).get("background", {}).get("asset_id"), assets)
                            for c in concepts]
    for i in range(n):
        for j in range(i + 1, n):
            if background_families[i] and background_families[i] == background_families[j] and primaries[i] != \
                    background_families[i]:
                add("warning", "shared_background", [ids[i], ids[j]], f"same background frame {background_families[i]}")
    if rendered:
        from .quality import dhash, hamming
        from .frames import read_image

        hashes = []
        for path in rendered:
            image = read_image(Path(path))
            hashes.append(dhash(image) if image is not None else None)
        for i in range(len(hashes)):
            for j in range(i + 1, len(hashes)):
                a, b = hashes[i], hashes[j]
                if a is not None and b is not None and hamming(a, b) <= 10:
                    add("blocking", "near_identical_render", [ids[i], ids[j]], f"perceptual hash distance {hamming(a, b)}")
    report.metrics["blocking"] = len(report.blocking)
    return report


def asset_reuse(concepts: Sequence[Mapping[str, Any]], assets: Mapping[str, Mapping[str, Any]]) -> dict[str, list[str]]:
    """source family -> concepts using it (for the report)."""
    usage: dict[str, list[str]] = {}
    for c in concepts:
        for aid in set(scene_assets(c.get("scene") or {})):
            root = family(aid, assets) or aid
            usage.setdefault(root, []).append(str(c.get("concept_id")))
    return {k: v for k, v in usage.items() if len(v) > 1}


@dataclass
class PortfolioQualityReport:
    """Technical checks per thumbnail + portfolio diversity. Not a CTR or quality score."""

    thumbnails: list[dict[str, Any]]
    diversity: PortfolioDiversity
    reuse: dict[str, list[str]]
    regenerate_recommended: bool
    notes: list[str] = field(default_factory=list)

    def to_mapping(self) -> dict[str, Any]:
        return {"format": "recut_portfolio_quality_report", "version": PORTFOLIO_VERSION,
                "thumbnails": self.thumbnails, "diversity": self.diversity.to_mapping(), "asset_reuse": self.reuse,
                "regenerate_recommended": self.regenerate_recommended, "notes": self.notes,
                "disclaimer": "technical constraints and visual hints only; no click-through prediction"}
