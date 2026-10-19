"""Optional creative inputs, carried through the existing Packaging pipeline."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping
import hashlib
import re
import unicodedata

REFERENCE_ROLES = ("style", "layout", "element")
STRATEGY_VERSION = 2  # 2: full_generative default; older saved strategies are V4 defaults, not choices


@dataclass(frozen=True)
class CreativeDirection:
    mode: str = "automatic"
    youtube_title: str = ""
    title_locked: bool = False
    thumbnail_hook: str = ""
    hook_locked: bool = False
    creative_focus: str = ""
    reference_images: list[dict[str, str]] = field(default_factory=list)
    style_profile_id: str = "lugozzi"
    typography_mode: str = "auto"
    # full_generative: the image model composes the finished thumbnail (thumbnail_gen_v2 family).
    # reference_preserving: AI environment + local protected compositing (V4.1), opt-in only.
    generation_strategy: str = "full_generative"
    strategy_version: int = STRATEGY_VERSION

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> CreativeDirection:
        raw = dict(raw or {})
        if "generation_strategy" in raw and "strategy_version" not in raw:
            # V4 wrote its reference_preserving default into every direction; it was never a user choice.
            raw["generation_strategy"] = "full_generative"
        raw["strategy_version"] = STRATEGY_VERSION
        value = cls(**{k: raw[k] for k in cls.__dataclass_fields__ if k in raw})
        if value.mode not in ("automatic", "guided"):
            raise ValueError("Creative Direction mode must be automatic or guided")
        if value.typography_mode not in ("auto", "precision", "ai"):
            raise ValueError("Typography must be auto, precision or ai")
        if value.generation_strategy not in ("reference_preserving", "full_generative"):
            raise ValueError("Unknown thumbnail generation strategy")
        for name in ("youtube_title", "thumbnail_hook", "creative_focus", "style_profile_id"):
            if not isinstance(getattr(value, name), str) or len(getattr(value, name)) > 3000:
                raise ValueError(f"Invalid Creative Direction {name}")
        if len(value.youtube_title) > 100 or len(value.thumbnail_hook) > 180:
            raise ValueError("Title: max 100 characters; thumbnail text: max 180 characters")
        if not isinstance(value.title_locked, bool) or not isinstance(value.hook_locked, bool):
            raise ValueError("Creative locks must be booleans")
        if not isinstance(value.reference_images, list) or len(value.reference_images) > 3:
            raise ValueError("At most three creative references are supported")
        for ref in value.reference_images:
            if not isinstance(ref, dict) or ref.get("role") not in REFERENCE_ROLES or not ref.get("path"):
                raise ValueError("A reference needs a path and role: style, layout or element")
        return value

    def to_mapping(self) -> dict[str, Any]:
        return asdict(self)

    def active(self) -> CreativeDirection:
        if self.mode == "guided":
            return self
        return CreativeDirection(style_profile_id=self.style_profile_id, typography_mode=self.typography_mode,
                                 generation_strategy=self.generation_strategy)

    def snapshot(self, output: Path) -> CreativeDirection:
        """Own the original references, independently of the user's external files."""
        from .frames import read_image_unchanged, write_image
        raw = self.to_mapping()
        refs = []
        for ref in self.reference_images:
            source = Path(ref["path"]).expanduser()
            if not source.is_absolute() and (output / source).is_file():
                source = output / source
            image = read_image_unchanged(source)
            if image is None:
                raise ValueError("Cannot read Creative Direction reference")
            digest = hashlib.sha256(image.tobytes()).hexdigest()[:20]
            target = output / "creative_references" / f"{digest}.png"
            write_image(target, image)
            refs.append({"path": str(target.relative_to(output)), "role": ref["role"], "digest": digest})
        raw["reference_images"] = refs
        return CreativeDirection.from_mapping(raw)


_STOP = set("a al alla allo ai alle anche che chi come con da dal della delle dei di e ed è il in la le lo ma "
            "mi mia mio miei nel nella non o per più puoi punta sul sulla sulle su un una uno voglio vorrei "
            "the and with this that my on of to for please highlight focus enfatizza molto".split())


def tokens(text: str) -> set[str]:
    text = unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode("ascii")
    return {w for w in re.findall(r"[a-z0-9]+", text) if len(w) >= 3 and w not in _STOP}


def relevance(focus: str, text: str) -> float:
    terms = tokens(focus)
    return len(terms & tokens(text)) / max(1, len(terms))


def planning_direction(direction: CreativeDirection, profile: Mapping[str, Any]) -> str:
    import json
    active = direction.active()
    return (
        "CREATIVE DIRECTION V4 (preferences are NOT evidence):\n"
        + json.dumps(active.to_mapping(), ensure_ascii=False)
        + "\nDESIGN SYSTEM:\n" + json.dumps(profile.get("design_system", {}), ensure_ascii=False)
        + "\nUser references are creative inputs, never proof of a card, class, result or combo. "
        "When focus has no corroboration, explain the limitation and use supported alternatives. "
        "Locked title and hook must be copied literally, without translation or capitalization changes. "
        "Different concepts may share a locked hook; editorial angles and protagonists must still differ."
    )


def apply_locks(raw: dict[str, Any], direction: CreativeDirection) -> dict[str, Any]:
    from copy import deepcopy
    from .scene import default_layer
    raw = deepcopy(raw)
    active = direction.active()
    for concept in raw.get("concepts", []):
        if active.hook_locked:
            concept["thumbnail_text"] = active.thumbnail_hook
            concept["hook_locked"] = True
            raw["_locked_hook"] = active.thumbnail_hook
            scene = concept.get("scene")
            if scene:
                text_ids = {l["id"] for l in scene["layers"] if l["type"] == "text"}
                scene["layers"] = [l for l in scene["layers"] if l["type"] != "text"]
                scene["visual_hierarchy"] = [i for i in scene["visual_hierarchy"] if i not in text_ids]
                if active.thumbnail_hook:
                    scene["layers"].append(default_layer("text", "v4_locked_text", text=active.thumbnail_hook,
                                           x=.5, y=.2, width=.88, height=.28, z=20))
                    scene["visual_hierarchy"].append("v4_locked_text")
    if active.title_locked and active.youtube_title:
        preferred = next((t for t in raw["titles"] if t["title_id"] == raw["preferred_title_id"]), raw["titles"][0])
        preferred["title"] = active.youtube_title
        raw["_locked_title"] = active.youtube_title
        for concept in raw["concepts"]:
            concept["title"] = active.youtube_title
    return raw
