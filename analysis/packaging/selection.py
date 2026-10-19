"""Editorial attachment selection within the existing six-image planning budget."""
from __future__ import annotations
from pathlib import Path
from typing import Any

from .frames import contact_sheet, write_image
from .direction import CreativeDirection


def planning_images(frames: Any, highlights: list[Any], manifest: Any, folder: Path,
                    output: Path, direction: CreativeDirection) -> list[dict[str, Any]]:
    ranked = sorted(highlights, key=lambda h: (h.local_score, h.visual.get("guided_relevance", 0)), reverse=True)
    gameplay = list(dict.fromkeys(f for h in ranked for f in h.frame_ids
                    if f in frames.records and frames.records[f].role in ("peak", "alt")))
    webcams = list(dict.fromkeys(f for h in ranked for f in h.webcam_frame_ids if f in frames.records))
    webcams.sort(key=lambda f: -(frames.records[f].quality.get("score", 0)
                  + .15 * next((h.visual.get("guided_relevance", 0) for h in highlights if f in h.webcam_frame_ids), 0)))
    images: list[dict[str, Any]] = []
    if gameplay:
        target = output / "contact_sheets" / "v4_editorial.jpg"
        # Cover multiple moments in one attachment instead of spending all slots on early games.
        contact_sheet([frames.records[f] for f in gameplay[:12]], frames.images, target, columns=4,
                      title="V4 selected evidence: asset id + edited time")
        images.append({"path": target, "description": "Summary of ranked real gameplay evidence: " + ", ".join(gameplay[:12])})
    def add(aid: str, role: str) -> None:
        if aid not in frames.images:
            return
        target = folder / f"planning_{aid}.jpg"
        write_image(target, frames.images[aid], quality=92)
        asset = manifest.by_id().get(aid)
        images.append({"path": target, "description": f"{role}: {aid}; real exported source "
                       f"{asset.source_index if asset else '?'} at {asset.source_time if asset else '?'}",
                       "asset_id": aid})
    for aid in webcams[:2]:
        add(aid, "Creator webcam primary" if aid == webcams[0] else "Creator webcam alternative")
    if gameplay:
        add(gameplay[0], "Main gameplay reference; preserve player/opponent distinction")
    detail = next((a.id for a in manifest.assets if a.type == "card_crop" and "primary" in a.uses), None)
    if detail is None:
        detail = next((a.id for h in ranked for a in manifest.for_highlight(h.id) if a.type == "board_region"), None)
    if detail:
        add(detail, "Real detail reference")
    refs = direction.active().reference_images
    if refs:
        ref = refs[0]
        images = images[:5]
        images.append({"path": output / ref["path"],
                       "description": f"USER CREATIVE REFERENCE ({ref['role']}); NOT gameplay evidence"})
    elif len(gameplay) > 1:
        add(gameplay[1], "Alternative gameplay moment")
    return images[:6]
