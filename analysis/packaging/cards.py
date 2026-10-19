"""Card assets: crops of cards the agent READ in real frames, cross-checked with the transcript.

No card/meta/combo database. A card is named only when the agent read it in a
listed frame; the name is then compared with what the creator said around that
moment. Status:
  verified   legible name AND mentioned in the transcript near the frame
  legible    legible name, not mentioned (crop usable; name shown only with care)
  uncertain  partial/unreadable/no name: crop kept as a visual only, never named
Unreadable crops are retried on neighbouring real frames (sharpest crop wins).
Official high-quality artwork is pluggable through CardAssetProvider and only
used when a provider can state the licence; V1 ships no provider (nothing is
downloaded, no artwork is invented).
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, Sequence

from .frames import FrameExtractor, frame_quality, write_image
from .models import TranscriptLine

MARGIN = 0.08


@dataclass
class CardAsset:
    id: str
    name: str | None
    status: str  # verified | legible | uncertain
    frame_id: str
    box: list[float]
    path: str
    source_time: float
    confidence: float
    legibility: str
    transcript_ids: list[str] = field(default_factory=list)
    transcript_match: str | None = None
    alternatives_tried: int = 0
    provenance: dict[str, Any] = field(default_factory=dict)
    thumbnail_use: str = "secondary"

    def to_mapping(self) -> dict[str, Any]:
        return asdict(self)


class CardAssetProvider(Protocol):
    """Optional official artwork source; must return provenance with licence/attribution or None."""

    def lookup(self, name: str) -> tuple[str, dict[str, Any]] | None: ...


class NoOfficialAssets:
    def lookup(self, name: str) -> tuple[str, dict[str, Any]] | None:
        return None


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9 ]+", " ", text).strip()


def transcript_mention(name: str, texts: Sequence[str], *, threshold: float = 0.8) -> str | None:
    """Fuzzy match of the card name against word windows of the transcript (ASR spells names loosely)."""
    target = _norm(name)
    if len(target) < 3:
        return None
    size = len(target.split())
    for text in texts:
        words = _norm(text).split()
        for n in {max(1, size - 1), size, size + 1}:
            for i in range(0, max(1, len(words) - n + 1)):
                window = " ".join(words[i:i + n])
                if window and difflib.SequenceMatcher(None, target, window).ratio() >= threshold:
                    return window
    return None


CARD_ASPECT = 0.70  # width / height of a full Hearthstone card (frame, gem, name banner, text box)
REFINED_MARGIN = 0.0


def refine_card_box(image: Any, box: Sequence[float]) -> tuple[list[float], dict[str, Any]]:
    """Generous, predictable card window around the agent's reading box.

    Agent readings are approximate and, on real videoprova frames, consistently too HIGH: they start above the
    card and stop inside its text box, so the bottom (text, race, attack/health) was cut. Outline detection was
    tried and is unreliable (golden glow, speech bubbles, adjacent cards). Instead the window grows a little
    upwards and a lot downwards and is widened to the card aspect: the whole card is always inside, with some
    context that the final image model crops away. A square-ish reading is a minion portrait, kept as read."""
    h, w = image.shape[:2]
    x, y, bw, bh = (float(v) for v in box)
    if bw <= 0 or bh <= 0:
        return [x, y, bw, bh], {"method": "agent_box"}
    if (bw * w) / max(1e-6, bh * h) >= 0.85:  # a minion on the board, not a card
        return [x, y, bw, bh], {"method": "agent_box_minion"}
    top = y - 0.06 * bh
    height = 1.36 * bh  # 6 % above, 30 % below
    width = max(bw * 1.06, CARD_ASPECT * height * h / w)
    left = x + bw / 2 - width / 2
    left, top = max(0.0, left), max(0.0, top)
    width, height = min(width, 1.0 - left), min(height, 1.0 - top)
    return [left, top, width, height], {"method": "card_window", "agent_box": [x, y, bw, bh]}


def _crop(image: Any, box: Sequence[float], margin: float = MARGIN) -> Any:
    h, w = image.shape[:2]
    x, y, bw, bh = box
    left = max(0, int((x - bw * margin) * w))
    top = max(0, int((y - bh * margin) * h))
    right = min(w, int((x + bw * (1 + margin)) * w))
    bottom = min(h, int((y + bh * (1 + margin)) * h))
    return image[top:bottom, left:right]


def extract_cards(observations: Sequence[dict[str, Any]], frames: FrameExtractor, lines: Sequence[TranscriptLine], *,
                  provider: CardAssetProvider | None = None) -> list[CardAsset]:
    provider = provider or NoOfficialAssets()
    assets: list[CardAsset] = []
    by_id = {line.id: line for line in lines}
    for n, item in enumerate(observations, start=1):
        record = frames.records.get(str(item.get("frame_id")))
        image = frames.images.get(str(item.get("frame_id")))
        if record is None or image is None:
            continue
        box, refined = refine_card_box(image, [float(v) for v in item["box"]])
        margin = REFINED_MARGIN if refined["method"] == "card_outline" else MARGIN
        crop = _crop(image, box, margin)
        if crop.size == 0 or min(crop.shape[:2]) < 12:
            continue
        name = (item.get("name") or "").strip() or None
        legibility = str(item.get("legibility", "unreadable"))
        tried = 0
        sampled_time = record.source_time
        if legibility != "clear":  # reuse the existing bounded neighbour search, without inventing detail
            burst = frames.burst(record.source_index, max(0.0, record.source_time - 1.5), record.source_time + 1.5, fps=4.0)
            best_score = frame_quality(crop)["score"]
            for candidate_time, other in burst:
                tried += 1
                candidate = _crop(other, box, margin)
                if candidate.size and frame_quality(candidate)["score"] > best_score + 0.05:
                    crop, best_score = candidate, frame_quality(candidate)["score"]
                    sampled_time = float(candidate_time)
        cited = [by_id[i].text for i in item.get("transcript_ids", []) if i in by_id]
        nearby = [line.text for line in lines if record.edited_time is not None
                  and abs(line.edited_start - record.edited_time) <= 60.0]
        match = transcript_mention(name, cited + nearby) if name else None
        if name and legibility == "clear" and match:
            status = "verified"
        elif name and legibility == "clear":
            status = "legible"
        else:
            status, name = "uncertain", (name if legibility == "partial" else None)
        card_id = f"K{n:02d}"
        relative = f"cards/{card_id}_{record.id}.png"
        write_image(frames.output_dir / relative, crop)
        provenance: dict[str, Any] = {"type": "video_crop", "obs_id": item.get("obs_id"), "frame_id": record.id,
                                      "source_time": round(sampled_time, 3), "reading_source_time": record.source_time,
                                      "box": box, "agent_box": [float(v) for v in item["box"]],
                                      "refine": refined, "note": "crop of the creator's own video frame"}
        official = provider.lookup(name) if name and status == "verified" else None
        if official is not None:
            provenance["official_asset"] = {"path": official[0], **official[1]}
        assets.append(CardAsset(card_id, name, status, record.id, box, relative, sampled_time,
                                float(item.get("confidence", 0.0)), legibility, list(item.get("transcript_ids", [])),
                                match, tried, provenance))
        quality = frame_quality(crop)
        assets[-1].thumbnail_use = ("primary" if status in ("verified", "legible") and min(crop.shape[:2]) >= 100
                                    and quality["sharpness"] >= 80 else "secondary" if status != "uncertain" else "avoid")
        assets[-1].provenance["thumbnail_use"] = assets[-1].thumbnail_use
        frames.images[card_id] = crop
    return assets


def prepare_card_fallbacks(raw: dict[str, Any], assets: dict[str, Any]) -> dict[str, Any]:
    """Resolve optional unusable card references BEFORE creative validation.

    A real, catalogued frame may replace an uncertain/tiny card crop. Unknown IDs,
    non-finite/out-of-frame geometry and unsupported textual claims still fail
    validation. Nothing is widened, recognised or promoted to verified.
    """
    from copy import deepcopy
    import math

    out = deepcopy(raw)
    replacements: dict[str, str] = {}
    diagnostics = list(out.get("_card_fallbacks") or [])
    observations = []
    for item in out.get("card_observations") or []:
        frame = str(item.get("frame_id"))
        known_frame = assets.get(frame) or {}
        valid_source = known_frame.get("type") in ("gameplay_frame", "board_region", "background")
        box = item.get("box")
        finite_inside = False
        try:
            if isinstance(box, list) and len(box) == 4:
                x, y, w, h = [float(v) for v in box]
                finite_inside = (all(math.isfinite(v) for v in (x, y, w, h))
                                 and 0 <= x < 1 and 0 <= y < 1 and w > 0 and h > 0
                                 and x + w <= 1.001 and y + h <= 1.001)
        except (TypeError, ValueError):
            pass
        too_small = finite_inside and (float(box[2]) < .05 or float(box[3]) < .05)
        uncertain = item.get("legibility") != "clear" or not item.get("name")
        # Never hide an invalid source reference or malformed/out-of-frame geometry.
        if valid_source and finite_inside and (too_small or uncertain):
            oid = str(item.get("obs_id"))
            replacements[oid] = frame
            diagnostics.append({"obs_id": oid, "frame_id": frame, "box": deepcopy(box),
                                "name": item.get("name"), "legibility": item.get("legibility"),
                                "reason": "crop_too_small" if too_small else "reading_uncertain",
                                "replacement": frame, "verified": False})
            if too_small:
                continue  # no usable card window; preserve the original only in diagnostics
        observations.append(item)
    out["card_observations"] = observations

    for aid, asset in assets.items():
        if asset.get("type") not in ("card_crop", "card_artwork"):
            continue
        provenance = asset.get("provenance") or {}
        if provenance.get("status") != "uncertain" and provenance.get("thumbnail_use") != "avoid":
            continue
        parent = asset.get("parent_id")
        if parent in assets and assets[parent].get("type") in ("gameplay_frame", "board_region", "background"):
            replacements[aid] = parent
            diagnostics.append({"obs_id": aid, "frame_id": parent, "name": provenance.get("name"),
                                "reason": "catalog_card_unsuitable", "replacement": parent, "verified": False})

    def replace_id(aid: Any) -> Any:
        return replacements.get(str(aid), aid)

    def remap_design(design: dict[str, Any]) -> None:
        for key in ("main_subject", "secondary_subject"):
            if key in design:
                design[key] = replace_id(design[key])
        for key in ("must_use_assets", "optional_assets", "visual_hierarchy", "evidence_ids"):
            if isinstance(design.get(key), list):
                design[key] = list(dict.fromkeys(replace_id(a) for a in design[key]))
        if isinstance(design.get("must_not_use_assets"), list):
            # Avoiding an unreadable crop does not forbid its entire context frame.
            design["must_not_use_assets"] = [a for a in design["must_not_use_assets"] if str(a) not in replacements]

    for concept in out.get("concepts") or []:
        used: set[str] = set()
        package = concept.get("package") or {}
        for key, ids in package.items():
            if isinstance(ids, list):
                used.update(str(a) for a in ids if str(a) in replacements)
                package[key] = list(dict.fromkeys(replace_id(a) for a in ids))
        scene = concept.get("scene")
        if isinstance(scene, dict):
            background = scene.get("background") or {}
            background["asset_id"] = replace_id(background.get("asset_id"))
            for layer in scene.get("layers") or []:
                aid = str(layer.get("asset_id"))
                if aid in replacements:
                    used.add(aid)
                    layer["asset_id"] = replacements[aid]
                    if layer.get("type") == "card":
                        layer.update(type="board_crop", crop=None, width=layer.get("width") or .6)
        if str(concept.get("primary_subject")) in replacements:
            used.add(str(concept["primary_subject"]))
            concept["primary_subject"] = replace_id(concept["primary_subject"])
        if isinstance(concept.get("supporting"), list):
            concept["supporting"] = [replace_id(a) for a in concept["supporting"]]
        for evidence in concept.get("evidence") or []:
            if isinstance(evidence.get("asset_ids"), list):
                evidence["asset_ids"] = [replace_id(a) for a in evidence["asset_ids"]]
        if isinstance(concept.get("design"), dict):
            remap_design(concept["design"])
        if used:
            if concept.get("design"):
                concept["design"]["story_confidence"] = "uncertain"
            if concept.get("role") == "card-led":
                concept["role"] = "board-led"
                if concept.get("design"):
                    concept["design"]["layout_archetype"] = "clutch_moment"
            concept["risk_notes"] = (str(concept.get("risk_notes") or "") +
                " Card fallback: " + ", ".join(sorted(used)) +
                " replaced by real frame context; card identity/details are not verified.").strip()
    for entry in (out.get("portfolio_strategy") or {}).get("plan") or []:
        if "primary_subject" in entry:
            entry["primary_subject"] = replace_id(entry["primary_subject"])
    if diagnostics:
        out["_card_fallbacks"] = diagnostics
    return out
