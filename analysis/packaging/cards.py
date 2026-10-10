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


def _crop(image: Any, box: Sequence[float]) -> Any:
    h, w = image.shape[:2]
    x, y, bw, bh = box
    left = max(0, int((x - bw * MARGIN) * w))
    top = max(0, int((y - bh * MARGIN) * h))
    right = min(w, int((x + bw * (1 + MARGIN)) * w))
    bottom = min(h, int((y + bh * (1 + MARGIN)) * h))
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
        box = [float(v) for v in item["box"]]
        crop = _crop(image, box)
        if crop.size == 0 or min(crop.shape[:2]) < 12:
            continue
        name = (item.get("name") or "").strip() or None
        legibility = str(item.get("legibility", "unreadable"))
        tried = 0
        if legibility != "clear":  # look for a sharper real frame of the same region
            burst = frames.burst(record.source_index, max(0.0, record.source_time - 1.5), record.source_time + 1.5, fps=4.0)
            best_score = frame_quality(crop)["score"]
            for _, other in burst:
                tried += 1
                candidate = _crop(other, box)
                if candidate.size and frame_quality(candidate)["score"] > best_score + 0.05:
                    crop, best_score = candidate, frame_quality(candidate)["score"]
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
                                      "source_time": round(record.source_time, 3),
                                      "box": box, "note": "crop of the creator's own video frame"}
        official = provider.lookup(name) if name and status == "verified" else None
        if official is not None:
            provenance["official_asset"] = {"path": official[0], **official[1]}
        assets.append(CardAsset(card_id, name, status, record.id, box, relative, record.source_time,
                                float(item.get("confidence", 0.0)), legibility, list(item.get("transcript_ids", [])),
                                match, tried, provenance))
        frames.images[card_id] = crop
    return assets
