"""VisualAssetManifest: every visual element a thumbnail may use, with provenance.

Asset types (ids):
  gameplay_frame  F..  real full-resolution frames selected per highlight (frames.py)
  background      F..  same frames, flagged for background use (coverage frames)
  board_region    B..  salient 16:9 windows inside highlight frames (the board, a played card, the
                       scoreboard), found locally from edge energy with the webcam overlay excluded
  webcam_original W..  webcam ROI crops (webcam.py)
  webcam_subject  S..  RGBA cut-outs of W.. (subjects.py); a failed mask is a feathered rectangle and
                       says so in `limitations`
  card_crop       K..  crops of cards the agent read in a listed frame (cards.py), added after the call
  card_artwork    A..  official artwork through CardAssetProvider only (none shipped)

Nothing here is generated: every asset is a crop of the edited video, with source and edited
timestamps, parent frame, normalized bbox in the parent, technical quality and allowed uses.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from .frames import FrameExtractor, frame_quality, require_cv2, write_image
from .models import FrameRecord, GameContext, HighlightCandidate

MANIFEST_VERSION = "visual_asset_manifest_v1"
ASSET_TYPES = ("webcam_original", "webcam_subject", "gameplay_frame", "board_region", "card_crop", "card_artwork",
               "background")


@dataclass
class VisualAsset:
    id: str
    type: str
    path: str  # relative to the output folder
    source_index: int
    source_time: float
    edited_time: float | None
    highlight_id: str | None
    game_id: str | None
    width: int
    height: int
    bbox: list[float] | None = None  # normalized [x, y, w, h] inside parent (None for whole frames)
    parent_id: str | None = None
    quality: dict[str, float] = field(default_factory=dict)
    has_alpha: bool = False
    provenance: dict[str, Any] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)
    uses: list[str] = field(default_factory=list)  # background | primary | secondary | foreground

    def to_mapping(self) -> dict[str, Any]:
        value = asdict(self)
        value["source_time"] = round(self.source_time, 3)
        value["edited_time"] = None if self.edited_time is None else round(self.edited_time, 3)
        value["quality"] = {k: round(float(v), 4) for k, v in self.quality.items()}
        return value

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> VisualAsset:
        names = cls.__dataclass_fields__.keys()
        return cls(**{k: raw[k] for k in names if k in raw})


@dataclass
class VisualAssetManifest:
    assets: list[VisualAsset] = field(default_factory=list)
    version: str = MANIFEST_VERSION

    def by_id(self) -> dict[str, VisualAsset]:
        return {a.id: a for a in self.assets}

    def of_type(self, *types: str) -> list[VisualAsset]:
        return [a for a in self.assets if a.type in types]

    def for_highlight(self, highlight_id: str) -> list[VisualAsset]:
        return [a for a in self.assets if a.highlight_id == highlight_id]

    def add(self, asset: VisualAsset) -> None:
        if asset.type not in ASSET_TYPES:
            raise ValueError(f"unknown asset type {asset.type}")
        if asset.id in self.by_id():
            raise ValueError(f"duplicate asset id {asset.id}")
        self.assets.append(asset)

    def lookup(self) -> dict[str, dict[str, Any]]:
        """asset_id -> manifest entry (the shape scene validation expects)."""
        return {a.id: a.to_mapping() for a in self.assets}

    def to_mapping(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for a in self.assets:
            counts[a.type] = counts.get(a.type, 0) + 1
        return {"format": "recut_visual_asset_manifest", "version": self.version, "counts": counts,
                "assets": [a.to_mapping() for a in self.assets]}

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> VisualAssetManifest:
        return cls([VisualAsset.from_mapping(a) for a in raw.get("assets", [])], str(raw.get("version", MANIFEST_VERSION)))


def _game_of(highlight_id: str | None, highlights: Sequence[HighlightCandidate]) -> str | None:
    return next((h.game_id for h in highlights if h.id == highlight_id), None)


def frame_assets(frames: FrameExtractor, highlights: Sequence[HighlightCandidate],
                 games: Sequence[GameContext]) -> list[VisualAsset]:
    """gameplay_frame / background / webcam_original entries for the frames already extracted."""
    coverage_game = {f: g.game_id for g in games for f in g.frame_ids}
    out = []
    for record in frames.records.values():
        if record.kind not in ("gameplay", "webcam"):
            continue
        game_id = _game_of(record.highlight_id, highlights) or coverage_game.get(record.id)
        common: dict[str, Any] = dict(path=record.path, source_index=record.source_index, source_time=record.source_time,
                      edited_time=record.edited_time, highlight_id=record.highlight_id, game_id=game_id,
                      width=record.width, height=record.height, quality=dict(record.quality))
        if record.kind == "webcam":
            limitations = [] if record.quality.get("face") else ["no face detected"]
            out.append(VisualAsset(record.id, "webcam_original", **common, has_alpha=False,
                                   provenance={"method": "webcam_roi_crop", "role": record.role},
                                   limitations=limitations, uses=["secondary"]))
        else:
            kind = "background" if record.role == "coverage" else "gameplay_frame"
            uses = ["background"] if kind == "background" else ["background", "primary"]
            out.append(VisualAsset(record.id, kind, **common, provenance={"method": "ffmpeg_frame", "role": record.role,
                                                                          "selection": record.quality.get("selection")},
                                   uses=uses))
    return out


def board_regions(image: np.ndarray, *, exclude: Sequence[Sequence[float]] = (), count: int = 2,
                  sizes: Sequence[float] = (0.46, 0.6, 0.74)) -> list[tuple[list[float], float]]:
    """Salient 16:9 windows (normalized boxes) ranked by edge energy; webcam overlay and edges excluded.

    A window whose borders run through strong contours would cut a card or a minion in half: the mean
    gradient ON its four border lines is subtracted from the score, so borders settle in calm areas and the
    objects inside stay whole."""
    cv2 = require_cv2()
    h, w = image.shape[:2]
    small_w = 384
    small_h = max(1, round(h * small_w / w))
    gray = cv2.cvtColor(cv2.resize(image, (small_w, small_h), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
    energy = np.abs(cv2.Sobel(gray, cv2.CV_32F, 1, 0)) + np.abs(cv2.Sobel(gray, cv2.CV_32F, 0, 1))
    saturation = cv2.cvtColor(cv2.resize(image, (small_w, small_h), interpolation=cv2.INTER_AREA),
                              cv2.COLOR_BGR2HSV)[..., 1].astype(np.float32) / 255.0
    energy *= 0.5 + saturation  # coloured game art over grey HUD text
    raw = energy.copy()  # border-cut penalty: measured on the real contours, before the layout priors
    row_cum = np.concatenate([np.zeros((small_h, 1), np.float32), np.cumsum(raw, axis=1)], axis=1)
    col_cum = np.concatenate([np.zeros((1, small_w), np.float32), np.cumsum(raw, axis=0)], axis=0)
    frame_mean = float(raw.mean()) + 1e-6
    for x, y, bw, bh in exclude:
        energy[int(y * small_h):int((y + bh) * small_h), int(x * small_w):int((x + bw) * small_w)] = 0
    # HUD columns, the hand / hero row at the bottom and the top bar are rarely the story: the board is
    energy[:, : int(0.12 * small_w)] *= 0.3
    energy[:, int(0.88 * small_w):] *= 0.3
    energy[int(0.78 * small_h):, :] *= 0.35
    energy[: int(0.08 * small_h), :] *= 0.5
    integral = cv2.integral(energy)
    found: list[tuple[list[float], float]] = []
    for size in sizes:
        bw = int(size * small_w)
        bh = int(bw * 9 / 16)
        if bh >= small_h:
            continue
        best: tuple[float, int, int] | None = None
        for y in range(0, small_h - bh, 4):
            for x in range(0, small_w - bw, 4):
                total = integral[y + bh, x + bw] - integral[y, x + bw] - integral[y + bh, x] + integral[y, x]
                cx, cy = (x + bw / 2) / small_w, (y + bh / 2) / small_h
                prior = 1.0 - 0.5 * abs(cx - 0.5) - 1.1 * abs(cy - 0.47)  # board band, rarely a corner
                y1, x1 = min(y + bh, small_h - 1), min(x + bw, small_w - 1)
                border = (row_cum[y, x + bw] - row_cum[y, x] + row_cum[y1, x + bw] - row_cum[y1, x]
                          + col_cum[y + bh, x] - col_cum[y, x] + col_cum[y + bh, x1] - col_cum[y, x1])
                border_mean = float(border) / (2 * (bw + bh))
                score = total / (bw * bh) * prior - 0.45 * border_mean * min(1.0, border_mean / frame_mean)
                if best is None or score > best[0]:
                    best = (score, x, y)
        if best is not None:
            found.append(([best[1] / small_w, best[2] / small_h, bw / small_w, bh / small_h], float(best[0])))
    found.sort(key=lambda item: item[1], reverse=True)
    return found[:count]


def region_assets(frames: FrameExtractor, highlights: Sequence[HighlightCandidate], out_dir: Path, *,
                  exclude: Sequence[Sequence[float]] = ()) -> list[VisualAsset]:
    """board_region assets (B..) inside each highlight's best frame."""
    out = []
    n = 0
    for h in highlights:
        peak = next((f for f in h.frame_ids if frames.records.get(f) and frames.records[f].role == "peak"), None)
        if peak is None or peak not in frames.images:
            continue
        image = frames.images[peak]
        record = frames.records[peak]
        for box, score in board_regions(image, exclude=exclude):
            n += 1
            ih, iw = image.shape[:2]
            x0, y0 = int(box[0] * iw), int(box[1] * ih)
            crop = image[y0:y0 + int(box[3] * ih), x0:x0 + int(box[2] * iw)]
            rid = f"B{n:03d}"
            relative = f"regions/{rid}_{peak}.jpg"
            write_image(out_dir / relative, crop)
            frames.images[rid] = crop
            quality = frame_quality(crop)
            limitations = ["small source region: soft when enlarged"] if crop.shape[0] < 360 else []
            out.append(VisualAsset(rid, "board_region", relative, record.source_index, record.source_time,
                                   record.edited_time, h.id, h.game_id, int(crop.shape[1]), int(crop.shape[0]),
                                   [round(v, 4) for v in box], peak, {**quality, "salience": score}, False,
                                   {"method": "edge_energy_window", "parent": peak}, limitations,
                                   ["background", "primary", "secondary"]))
    return out


def subject_assets(frames: FrameExtractor, plates: Mapping[str | None, np.ndarray | None], out_dir: Path,
                   highlights: Sequence[HighlightCandidate], cache: Any = None) -> list[VisualAsset]:
    """webcam_subject assets (S..) for every webcam crop: plate-assisted cut-out or a flagged fallback."""
    from .cache import stable_key
    from .subjects import SEGMENTER_VERSION, segment_person

    cv2 = require_cv2()
    out = []
    for record in [r for r in frames.records.values() if r.kind == "webcam"]:
        crop = frames.images.get(record.id)
        if crop is None:
            continue
        game_id = _game_of(record.highlight_id, highlights)
        plate = plates.get(game_id)
        if plate is not None and plate.shape[:2] != crop.shape[:2]:
            plate = cv2.resize(plate, (crop.shape[1], crop.shape[0]), interpolation=cv2.INTER_AREA)
        sid = "S" + record.id[1:]
        relative = f"subjects/{sid}.png"
        key = stable_key("subject", SEGMENTER_VERSION, hash_image(crop), hash_image(plate) if plate is not None else None)
        cached = cache.load_json("evidence", key) if cache is not None else None
        target = out_dir / relative
        if isinstance(cached, dict) and cache.load_file("frames", key, ".png", target):
            from .frames import read_image_unchanged

            rgba = read_image_unchanged(target)
            info = cached
        else:
            cut = segment_person(crop, plate=plate)
            rgba = cut.rgba
            info = {"method": cut.method, "quality": cut.quality, "face": cut.face, "ok": cut.ok,
                    "limitations": cut.limitations}
            write_image(target, rgba)
            if cache is not None:
                cache.store_json("evidence", key, info)
                cache.store_file("frames", key, ".png", target)
        if rgba is None:
            continue
        frames.images[sid] = rgba
        limitations = list(info["limitations"])
        if not info["ok"]:
            limitations.append("not isolated: feathered rectangle of the webcam (keep it small or omit)")
        if crop.shape[0] < 240:
            limitations.append(f"low webcam resolution ({crop.shape[0]} px): soft when enlarged")
        out.append(VisualAsset(sid, "webcam_subject", relative, record.source_index, record.source_time,
                               record.edited_time, record.highlight_id, game_id, int(rgba.shape[1]),
                               int(rgba.shape[0]), None, record.id,
                               {**{k: float(v) for k, v in (info["quality"] or {}).items()},
                                "isolated": 1.0 if info["ok"] else 0.0,
                                "face_score": float(record.quality.get("score", 0.0))},
                               True, {"method": info["method"], "parent": record.id,
                                      "segmenter": SEGMENTER_VERSION, "face": info.get("face")},
                               limitations, ["primary", "foreground"] if info["ok"] else ["secondary"]))
    return out


def hash_image(image: np.ndarray | None) -> str | None:
    import hashlib

    if image is None:
        return None
    return hashlib.sha256(np.ascontiguousarray(image).tobytes()).hexdigest()[:24]


def card_assets(cards: Iterable[Mapping[str, Any]], frames: FrameExtractor,
                highlights: Sequence[HighlightCandidate]) -> list[VisualAsset]:
    """card_crop assets (K..) from cards.py results; names only travel with verified/legible readings."""
    out = []
    for card in cards:
        parent: FrameRecord | None = frames.records.get(card["frame_id"])
        image = frames.images.get(card["id"])
        if parent is None or image is None:
            continue
        limitations = []
        if card["status"] == "uncertain":
            limitations.append("identity uncertain: never name it")
        if image.shape[0] < 160:
            limitations.append("small crop: keep it small")
        out.append(VisualAsset(card["id"], "card_crop", card["path"], parent.source_index, parent.source_time,
                               parent.edited_time, parent.highlight_id, _game_of(parent.highlight_id, highlights),
                               int(image.shape[1]), int(image.shape[0]), list(card["box"]), parent.id,
                               frame_quality(image), False,
                               {"method": "agent_reading_crop", "status": card["status"],
                                "name": card["name"] if card["status"] != "uncertain" else None,
                                "obs_id": (card.get("provenance") or {}).get("obs_id")},
                               limitations, ["primary", "secondary"] if card["status"] != "uncertain" else ["secondary"]))
    return out


def summary_lines(manifest: VisualAssetManifest, *, limit: int = 80) -> list[str]:
    """Compact manifest for the agent prompt (one line per asset)."""
    lines = []
    for a in manifest.assets[:limit]:
        q = a.quality
        quality = f"sharp={q.get('sharpness', 0):.0f}" if "sharpness" in q else ""
        if a.type == "webcam_subject":
            quality = "isolated" if q.get("isolated") else "NOT isolated"
        lines.append(f"- {a.id} {a.type} {a.highlight_id or '-'} {a.game_id or '-'} "
                     f"t={_clock(a.edited_time)} {a.width}x{a.height} {quality} uses={'/'.join(a.uses)}"
                     f"{' parent=' + a.parent_id if a.parent_id else ''}"
                     f"{' LIMITS: ' + '; '.join(a.limitations) if a.limitations else ''}")
    if len(manifest.assets) > limit:
        lines.append(f"- ... {len(manifest.assets) - limit} more assets omitted")
    return lines


def _clock(t: float | None) -> str:
    if t is None:
        return "--:--"
    return f"{int(t) // 60:02d}:{int(t) % 60:02d}"
