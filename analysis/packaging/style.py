"""Creator Style Memory: a persistent, versioned, editable profile per creator.

Stored under config_root()/packaging/creators/<slug>/profile.json; every change
bumps `revision` and keeps the previous file in history/ (nothing is deleted).
Preferences are only DERIVED from evidence (imported examples, approved and
rejected suggestions, explicit feedback); an empty profile asserts nothing.
Examples inform style; the agent is told never to copy them. No local model is
trained. A profile can be disabled without being deleted.
"""
from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np

PROFILE_FORMAT = "recut_creator_style"
SCHEMA_VERSION = 1
_SLUG = re.compile(r"[^a-z0-9_-]+")
_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿]")


def slugify(name: str) -> str:
    slug = _SLUG.sub("-", name.strip().lower()).strip("-")
    if not slug:
        raise ValueError("creator name must contain letters or digits")
    return slug[:48]


def default_root() -> Path:
    from utils.runtime_paths import config_root
    return config_root() / "packaging" / "creators"


def empty_profile(name: str, *, channel: str = "", language: str = "") -> dict[str, Any]:
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    return {"format": PROFILE_FORMAT, "schema": SCHEMA_VERSION, "revision": 1, "created": now, "updated": now,
            "creator_id": slugify(name), "display_name": name, "channel_name": channel, "language": language,
            "enabled": True,
            "brand": {"colors": [], "fonts": [], "webcam_roi": None, "webcam_size": None, "cards": None,
                      "text": None, "typography": None, "layouts": []},
            "title_style": {"observed": {}, "notes": []},
            "thumbnail_style": {"observed": {}, "notes": []},
            "references": [], "approved": [], "rejected": [], "feedback": []}


class CreatorStyleStore:
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root is not None else default_root()

    def path(self, name: str) -> Path:
        return self.root / slugify(name) / "profile.json"

    def list(self) -> list[dict[str, Any]]:
        out = []
        for path in sorted(self.root.glob("*/profile.json")) if self.root.is_dir() else []:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            out.append({"creator_id": data.get("creator_id"), "display_name": data.get("display_name"),
                        "revision": data.get("revision"), "enabled": data.get("enabled")})
        return out

    def load(self, name: str) -> dict[str, Any]:
        path = self.path(name)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise KeyError(f"creator profile '{name}' not found (create it with packaging-style create)") from exc
        if data.get("format") != PROFILE_FORMAT:
            raise ValueError(f"{path} is not a creator style profile")
        return data

    def create(self, name: str, *, channel: str = "", language: str = "") -> dict[str, Any]:
        if self.path(name).exists():
            raise FileExistsError(f"creator profile '{name}' already exists")
        profile = empty_profile(name, channel=channel, language=language)
        self._write(profile, previous=None)
        return profile

    def save(self, profile: dict[str, Any]) -> dict[str, Any]:
        """Persist a modified profile as a new revision; the previous revision moves to history/."""
        current = self.load(profile["creator_id"])
        if int(current.get("revision", 0)) != int(profile.get("revision", 0)):
            raise RuntimeError("the profile changed on disk since it was loaded; reload and retry")
        profile = dict(profile, revision=int(profile["revision"]) + 1, updated=time.strftime("%Y-%m-%dT%H:%M:%S"))
        self._write(profile, previous=current)
        return profile

    def _write(self, profile: dict[str, Any], *, previous: dict[str, Any] | None) -> None:
        from analysis.gameplay.cache import atomic_write_json

        path = self.path(profile["creator_id"])
        if previous is not None:
            history = path.parent / "history" / f"profile.r{int(previous.get('revision', 0)):04d}.json"
            atomic_write_json(history, previous)
        atomic_write_json(path, profile)

    # ---- edits -----------------------------------------------------------------------------------
    def update(self, name: str, **changes: Any) -> dict[str, Any]:
        profile = self.load(name)
        for key, value in changes.items():
            if value is None:
                continue
            if key in ("colors", "fonts", "webcam_roi", "webcam_size", "cards", "text", "typography", "layouts"):
                profile["brand"][key] = value
            elif key in ("channel_name", "language", "display_name", "enabled", "design_system"):
                profile[key] = value
            elif key in ("title_note", "thumbnail_note"):
                profile["title_style" if key == "title_note" else "thumbnail_style"]["notes"].append(str(value))
            else:
                raise KeyError(f"unknown profile field {key}")
        return self.save(profile)

    def set_identity_references(self, name: str, images: Sequence[Path]) -> dict[str, Any]:
        """Explicit identity photos, distinct from style examples; never gameplay evidence."""
        import hashlib
        from .frames import read_image_unchanged, write_image
        profile = self.load(name)
        owned = []
        for source in list(images)[:3]:
            image = read_image_unchanged(Path(source))
            if image is None:
                raise ValueError("Cannot read identity reference")
            digest = hashlib.sha256(image.tobytes()).hexdigest()[:20]
            target = self.path(name).parent / "identity" / f"{digest}.png"
            write_image(target, image)
            owned.append({"path": str(target), "digest": digest, "role": "identity"})
        profile["identity_references"] = owned
        return self.save(profile)

    def import_examples(self, name: str, *, titles: Sequence[str] = (), thumbnails: Sequence[Path] = (),
                        source: str = "local import", categories: Sequence[str] = (),
                        negative: bool = False) -> dict[str, Any]:
        """Import example titles / thumbnails. categories: story types or treatments the thumbnails
        illustrate (e.g. unexpected_reaction, card_hero); negative=True marks examples to avoid."""
        profile = self.load(name)
        folder = self.path(name).parent / "references"
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        known = {(r["kind"], r.get("text") or r.get("original")) for r in profile["references"]}
        for title in titles:
            title = title.strip()
            if title and ("title", title) not in known:
                profile["references"].append({"id": f"R{len(profile['references']) + 1:03d}", "kind": "title",
                                              "text": title, "source": source, "added": stamp})
        for image in thumbnails:
            image = Path(image)
            if not image.is_file() or ("thumbnail", str(image)) in known:
                continue
            ref_id = f"R{len(profile['references']) + 1:03d}"
            folder.mkdir(parents=True, exist_ok=True)
            copy = folder / f"{ref_id}{image.suffix.lower()}"
            shutil.copyfile(image, copy)  # the profile owns a copy; the original is never modified
            profile["references"].append({"id": ref_id, "kind": "thumbnail", "path": str(copy.relative_to(folder.parent)),
                                          "original": str(image), "source": source, "added": stamp,
                                          "polarity": "negative" if negative else "positive",
                                          "categories": [str(c) for c in categories]})
        profile["title_style"]["observed"] = analyze_titles(
            [r["text"] for r in profile["references"] if r["kind"] == "title"] +
            [a["value"] for a in profile["approved"] if a.get("kind") == "title"])
        profile["thumbnail_style"]["observed"] = analyze_thumbnails(
            [folder.parent / r["path"] for r in profile["references"]
             if r["kind"] == "thumbnail" and r.get("polarity", "positive") == "positive"])
        return self.save(profile)

    def reference_path(self, name: str, reference: dict[str, Any]) -> Path:
        return self.path(name).parent / str(reference["path"])

    def record_feedback(self, name: str, entries: Iterable[dict[str, Any]]) -> dict[str, Any]:
        """entries: {"action": approve|reject|note, "kind": title|thumbnail|concept, "value", "id", "run", "note"}."""
        profile = self.load(name)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        for entry in entries:
            item = {**entry, "at": stamp}
            profile["feedback"].append(item)
            if entry.get("action") == "approve":
                profile["approved"].append(item)
            elif entry.get("action") == "reject":
                profile["rejected"].append(item)
        profile["title_style"]["observed"] = analyze_titles(
            [r["text"] for r in profile["references"] if r["kind"] == "title"] +
            [a["value"] for a in profile["approved"] if a.get("kind") == "title" and a.get("value")])
        return self.save(profile)


def analyze_titles(titles: Sequence[str]) -> dict[str, Any]:
    titles = [t for t in titles if t.strip()]
    if not titles:
        return {}
    lengths = [len(t) for t in titles]
    words = [len(t.split()) for t in titles]
    letters = [c for t in titles for c in t if c.isalpha()]
    return {"examples": len(titles), "avg_chars": round(float(np.mean(lengths)), 1),
            "max_chars": int(max(lengths)), "avg_words": round(float(np.mean(words)), 1),
            "uppercase_ratio": round(sum(c.isupper() for c in letters) / max(1, len(letters)), 3),
            "with_emoji": round(sum(bool(_EMOJI.search(t)) for t in titles) / len(titles), 3),
            "with_question": round(sum("?" in t for t in titles) / len(titles), 3),
            "with_exclamation": round(sum("!" in t for t in titles) / len(titles), 3),
            "with_number": round(sum(bool(re.search(r"\d", t)) for t in titles) / len(titles), 3),
            "with_separator": round(sum(bool(re.search(r"[|\-–:]", t)) for t in titles) / len(titles), 3)}


def analyze_thumbnails(paths: Sequence[Path]) -> dict[str, Any]:
    from .frames import read_image, require_cv2
    from .webcam import detect_faces

    cv2 = require_cv2()
    images = [img for img in (read_image(Path(p)) for p in paths) if img is not None]
    if not images:
        return {}
    pixels, saturation, brightness, faces = [], [], [], 0
    for image in images:
        small = cv2.resize(image, (160, 90), interpolation=cv2.INTER_AREA)
        hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
        saturation.append(float(hsv[..., 1].mean()) / 255.0)
        brightness.append(float(hsv[..., 2].mean()) / 255.0)
        pixels.append(small.reshape(-1, 3).astype(np.float32))
        faces += 1 if detect_faces(image, min_rel=0.08) else 0
    data = np.vstack(pixels)
    k = min(5, len(data))
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 1.0)
    _, labels, centers = cv2.kmeans(data, k, None, criteria, 2, cv2.KMEANS_PP_CENTERS)
    counts = np.bincount(labels.ravel(), minlength=k)
    palette = [f"#{int(c[2]):02x}{int(c[1]):02x}{int(c[0]):02x}" for c in centers[np.argsort(counts)[::-1]]]
    return {"examples": len(images), "dominant_colors": palette, "mean_saturation": round(float(np.mean(saturation)), 3),
            "mean_brightness": round(float(np.mean(brightness)), 3), "face_ratio": round(faces / len(images), 3)}


def prompt_summary(profile: dict[str, Any] | None, *, max_examples: int = 12) -> list[str]:
    """Evidence-based style lines for Phase B; empty when the profile is missing or disabled."""
    if not profile or not profile.get("enabled", True):
        return []
    lines = [f"CREATOR STYLE MEMORY (profile '{profile.get('display_name')}', revision {profile.get('revision')}). "
             "Use it as INSPIRATION only: never copy or paraphrase example titles; follow only preferences supported "
             "by the evidence below."]
    if profile.get("channel_name"):
        lines.append(f"- channel: {profile['channel_name']}")
    brand = profile.get("brand", {})
    if brand.get("colors"):
        lines.append(f"- brand colors: {', '.join(brand['colors'])}")
    if brand.get("fonts"):
        lines.append(f"- brand fonts (renderer choices): {', '.join(brand['fonts'])}")
    preferences = {"webcam size": brand.get("webcam_size"), "cards": brand.get("cards"),
                   "thumbnail text": brand.get("text"), "typography": brand.get("typography"),
                   "preferred archetypes": ", ".join(brand.get("layouts") or []) or None}
    stated = [f"{k}: {v}" for k, v in preferences.items() if v]
    if stated:
        lines.append("- creator preferences: " + "; ".join(stated) +
                     " (cards prefer = use real card crops when legible; avoid = only when essential; "
                     "text minimal = 1-3 words)")
    for key, label in (("title_style", "title statistics"), ("thumbnail_style", "thumbnail statistics")):
        observed = profile.get(key, {}).get("observed") or {}
        if observed:
            lines.append(f"- {label} from {observed.get('examples')} examples: " +
                         ", ".join(f"{k}={v}" for k, v in observed.items() if k != "examples"))
        for note in profile.get(key, {}).get("notes", [])[-6:]:
            lines.append(f"- creator note ({key.split('_')[0]}): {note}")
    titles = [r["text"] for r in profile.get("references", []) if r.get("kind") == "title"][-max_examples:]
    if titles:
        lines.append("- example titles (style reference only, DO NOT copy): " + " || ".join(titles))
    approved = [a.get("value") for a in profile.get("approved", []) if a.get("kind") == "title" and a.get("value")]
    if approved:
        lines.append("- previously APPROVED titles: " + " || ".join(approved[-max_examples:]))
    rejected = [a.get("value") or a.get("id") for a in profile.get("rejected", [])][-max_examples:]
    if rejected:
        lines.append("- previously REJECTED suggestions (avoid this style): " + " || ".join(str(r) for r in rejected))
    notes = [f.get("note") for f in profile.get("feedback", []) if f.get("note")][-6:]
    if notes:
        lines.append("- creator feedback notes: " + " || ".join(notes))
    if len(lines) == 1:
        lines.append("- the profile has no evidence yet: do not assume any preference")
    return lines


def select_references(profile: dict[str, Any] | None, story_types: Sequence[str], *, k: int = 3) -> list[dict[str, Any]]:
    """2-3 real thumbnails of the creator that fit this video's opportunities (no embeddings needed).

    Positive examples first, ranked by category overlap with the story types, then approved ones
    and recency; at most one negative example (shown as "avoid"). Disabled profiles return nothing.
    """
    if not profile or not profile.get("enabled", True):
        return []
    approved_ids = {a.get("id") for a in profile.get("approved", [])}
    wanted = set(story_types)
    thumbs = [r for r in profile.get("references", []) if r.get("kind") == "thumbnail" and r.get("path")]

    def rank(ref: dict[str, Any]) -> tuple[int, int, str]:
        return (len(wanted & set(ref.get("categories") or [])), 1 if ref.get("id") in approved_ids else 0,
                str(ref.get("added", "")))

    positive = sorted((r for r in thumbs if r.get("polarity", "positive") == "positive"), key=rank, reverse=True)
    negative = sorted((r for r in thumbs if r.get("polarity") == "negative"), key=rank, reverse=True)
    chosen = positive[: max(0, k - (1 if negative else 0))] + negative[:1]
    return chosen[:k]


def style_sections(profile: dict[str, Any] | None) -> dict[str, Any]:
    """The profile split into what should stay stable and what only guides one video."""
    if not profile:
        return {}
    brand = profile.get("brand", {})
    return {"identity": {"colors": brand.get("colors", []), "fonts": brand.get("fonts", []),
                         "typography": brand.get("typography")},
            "composition": {"webcam_size": brand.get("webcam_size"), "cards": brand.get("cards"),
                            "text": brand.get("text"), "layouts": brand.get("layouts", [])},
            "titles": (profile.get("title_style") or {}).get("observed", {}),
            "positive_examples": len([r for r in profile.get("references", [])
                                      if r.get("kind") == "thumbnail" and r.get("polarity", "positive") == "positive"]),
            "negative_examples": len([r for r in profile.get("references", []) if r.get("polarity") == "negative"]),
            "recent_feedback": [f.get("note") or f.get("value") for f in profile.get("feedback", [])[-5:]]}


# Explicit creator preferences, not inferred viewing/CTR performance.
COMPETITIVE_DESIGN = {
    "name": "Lugozzi — Competitive Gaming",
    "mood": "Competitive gaming; dynamic but ordered; immediate hierarchy and controlled contrast",
    "palette": {"primary": ["#FFD436", "#FFFFFF", "#111420"], "accent": ["#FF514A", "#27C9FF"],
                "max_dominant_saturated": 2},
    "fonts": ["Anton", "Archivo Black", "Barlow Condensed Black", "Impact", "Arial Black", "DejaVu Sans"],
    "text": {"color": "#FFD436", "outline": "#111420", "outline_px": 12, "shadow_px": 8,
             "max_lines": 2, "uppercase_preferred": True, "safe_margin": .05},
    "layout_archetypes": ["reaction_impact", "card_power", "clutch_moment", "big_statement"],
    "webcam": {"positions": ["left", "right"], "reaction_width": [.30, .45], "required": False},
    "effects": {"rim_lighting": 4, "depth_shadows": 4, "selective_glow": 3,
                "particles_light_rays": 2, "chromatic_aberration": 0},
    "avoid": ["rectangular webcam", "random collage", "unnecessary HUD", "invented cards",
              "indiscriminate glow", "four recolours of one screenshot"],
    "typography": "auto",
    "output_size": [1920, 1080],
    "font_licenses": {
        "Anton": "https://github.com/googlefonts/AntonFont/blob/main/OFL.txt",
        "Archivo Black": "https://github.com/google/fonts/blob/main/ofl/archivoblack/OFL.txt",
        "Barlow Condensed Black": "https://github.com/google/fonts/blob/main/ofl/barlowcondensed/OFL.txt"},
}


def competitive_profile(store: CreatorStyleStore, name: str = "lugozzi") -> dict[str, Any]:
    """Reuse the existing versioned store; never replace a creator's custom profile."""
    from copy import deepcopy
    created = False
    try:
        profile = store.load(name)
    except KeyError:
        if name != "lugozzi":
            raise
        profile = store.create("lugozzi", channel="Lugozzi", language="it")
        created = True
    if "design_system" not in profile:
        profile["design_system"] = deepcopy(COMPETITIVE_DESIGN)
        # Older explicit creator profiles are enriched in memory only until edited.
        if created:
            profile = store.save(profile)
    return profile
