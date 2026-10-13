"""Conservative local multi-frame grouping of the BOTTOM player's portrait.

Scores are similarities, not calibrated probabilities. Connected components
must be cliques: an ambiguous A--X--B bridge never merges two different skins.
The existing board gate is reused read-only; detector outputs are never changed.
"""

from __future__ import annotations

import base64
from collections import Counter
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Callable

import numpy as np

from analysis.performance_profile import current_profile, measure, profiled, record
from analysis.audio_service import AnalysisCancellation
from analysis.gameplay.hearthstone.visual.templates import require_opencv
from core.project_file import media_fingerprint
from utils.runtime_paths import cache_root

VERSION = 8


@dataclass(frozen=True)
class PortraitSettings:
    # Inner artwork, excluding health, armor, attack, weapon and most hero frame.
    portrait_roi: tuple[float, float, float, float] = (0.466, 0.707, 0.068, 0.128)
    power_roi: tuple[float, float, float, float] = (0.565625, 0.692592593, 0.08125, 0.144444444)
    samples: int = 10
    delay_s: float = 60.0
    horizon_s: float = 120.0
    min_samples: int = 3
    similarity: float = 0.88
    color_similarity: float = 0.80
    consensus: float = 0.60
    board_min: float = 0.38  # existing profile's conservative board_high
    class_similarity: float = 0.60  # Hero Power artwork-in-card vs in-game icon; margin/support also required
    class_margin: float = 0.08

    def __post_init__(self) -> None:
        if not 5 <= self.samples <= 10 or not 2 <= self.min_samples <= self.samples:
            raise ValueError("Use 5–10 samples and at least two agreeing samples.")
        if (
            not all(
                math.isfinite(v) and 0 < v <= 1
                for v in (
                    self.similarity,
                    self.color_similarity,
                    self.consensus,
                    self.board_min,
                    self.class_similarity,
                    self.class_margin,
                )
            )
            or not 0 <= self.delay_s < self.horizon_s <= 120
        ):
            raise ValueError("Invalid portrait settings.")
        for x, y, w, h in (self.portrait_roi, self.power_roi):
            if (
                not all(math.isfinite(v) for v in (x, y, w, h))
                or min(x, y) < 0
                or min(w, h) <= 0
                or x + w > 1
                or y + h > 1
            ):
                raise ValueError("ROI must be normalized and inside the video.")


from analysis.gameplay.cache import atomic_write_json as atomic_json


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def game_key(game: dict[str, Any]) -> str:
    return str(game.get("id") or digest([game.get("index"), game.get("start"), game.get("end")])[:24])


def crop(frame: np.ndarray, roi: tuple[float, float, float, float]) -> np.ndarray:
    h, w = frame.shape[:2]
    x, y, rw, rh = roi
    return frame[round(y * h) : round((y + rh) * h), round(x * w) : round((x + rw) * w)]


def thumbnail(image: np.ndarray) -> str:
    cv = require_opencv()
    ok, encoded = cv.imencode(".png", cv.resize(image, (64, 80), interpolation=cv.INTER_AREA))
    return base64.b64encode(encoded.tobytes()).decode("ascii") if ok else ""


def features(image: np.ndarray) -> dict[str, np.ndarray] | None:
    cv = require_opencv()
    if image.size == 0 or min(image.shape[:2]) < 16:
        return None
    from .quality import confirmation_visible

    if confirmation_visible(image):
        return None
    small = cv.resize(image, (48, 56), interpolation=cv.INTER_AREA)
    gray = cv.cvtColor(small, cv.COLOR_BGR2GRAY).astype(np.float32)
    # Flat/dark/saturated blocks and very blurred crops cannot identify a skin.
    if float(gray.std()) < 10 or float(cv.Laplacian(gray, cv.CV_32F).var()) < 12:
        return None
    if float(np.mean((gray < 5) | (gray > 250))) > 0.65:
        return None
    norm = (gray - gray.mean()) / max(float(gray.std()), 1.0)
    gx = cv.Sobel(norm, cv.CV_32F, 1, 0)
    gy = cv.Sobel(norm, cv.CV_32F, 0, 1)
    edges = cv.resize(cv.magnitude(gx, gy), (24, 28), interpolation=cv.INTER_AREA)
    hsv = cv.cvtColor(small, cv.COLOR_BGR2HSV)
    hist = cv.calcHist([hsv], [0, 1], None, [12, 8], [0, 180, 0, 256]).flatten()
    hist /= max(float(np.linalg.norm(hist)), 1e-9)
    # SIFT is a bounded local descriptor, not a trained model. Interior mask
    # excludes shared hero-frame geometry; it handles small skin animations.
    sift = cv.SIFT_create(nfeatures=80, contrastThreshold=0.025)
    large = cv.resize(small, (96, 112), interpolation=cv.INTER_CUBIC)
    mask = np.zeros((112, 96), np.uint8)
    mask[8:100, 8:88] = 255
    keypoints, descriptors = sift.detectAndCompute(cv.cvtColor(large, cv.COLOR_BGR2GRAY), mask)
    points = np.array([k.pt for k in keypoints], np.float32).reshape(-1, 2)
    return {
        "gray": norm,
        "edges": edges,
        "color": hist,
        "points": points,
        "descriptors": descriptors if descriptors is not None else np.empty((0, 128), np.float32),
    }


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    return max(
        0.0, min(1.0, float(np.dot(a.ravel(), b.ravel()) / max(float(np.linalg.norm(a) * np.linalg.norm(b)), 1e-9)))
    )


def registered_similarity(a: dict[str, np.ndarray], b: dict[str, np.ndarray]) -> float:
    cv = require_opencv()
    if min(len(a.get("points", [])), len(b.get("points", []))) < 8:
        return 0.0
    matcher = cv.BFMatcher(cv.NORM_L2)
    matches = matcher.knnMatch(a["descriptors"], b["descriptors"], k=2)
    good = [m[0] for m in matches if len(m) == 2 and m[0].distance < 0.70 * m[1].distance]
    # Require unique correspondence, enough interior coverage and a small,
    # physically plausible transform, not shared decorative corners.
    if (
        len(good) < 8
        or len(good) / min(len(a["points"]), len(b["points"])) < 0.35
        or len({m.trainIdx for m in good}) != len(good)
    ):
        return 0.0
    left = np.array([a["points"][m.queryIdx] for m in good], np.float32)
    right = np.array([b["points"][m.trainIdx] for m in good], np.float32)
    transform, mask = cv.estimateAffinePartial2D(left, right, method=cv.LMEDS)
    if transform is None or mask is None:
        return 0.0
    kept = mask.ravel().astype(bool)
    if int(kept.sum()) < 8:
        return 0.0
    scale = float(np.hypot(transform[0, 0], transform[1, 0]))
    angle = abs(float(np.degrees(np.arctan2(transform[1, 0], transform[0, 0]))))
    if not 0.85 <= scale <= 1.15 or angle > 15 or float(np.linalg.norm(transform[:, 2])) > 12:
        return 0.0
    coverage = min(float(cv.contourArea(cv.convexHull(p[kept]))) for p in (left, right)) / (96 * 112)
    if coverage < 0.15:
        return 0.0
    return float(np.mean(kept))


def similarity(a: dict[str, np.ndarray], b: dict[str, np.ndarray]) -> tuple[float, float]:
    record("hero_groups.comparisons", similarity_pairs=1, template_comparisons=2)
    cv = require_opencv()
    # Small translations tolerate capture jitter without matching unrelated art.
    gray = max(
        float(cv.matchTemplate(a["gray"], b["gray"][2:-2, 2:-2], cv.TM_CCOEFF_NORMED).max()),
        float(cv.matchTemplate(b["gray"], a["gray"][2:-2, 2:-2], cv.TM_CCOEFF_NORMED).max()),
    )
    edge = cosine(a["edges"], b["edges"])
    structural = max(0.0, min(gray, edge))
    if structural < 0.88:
        structural = max(structural, registered_similarity(a, b), registered_similarity(b, a))
    return structural, cosine(a["color"], b["color"])


def agrees(a: dict[str, np.ndarray], b: dict[str, np.ndarray], settings: PortraitSettings) -> bool:
    structural, color = similarity(a, b)
    return structural >= settings.similarity and color >= settings.color_similarity


@profiled("hero_groups.signature")
def signature(
    images: list[np.ndarray], settings: PortraitSettings
) -> tuple[dict[str, np.ndarray] | None, int, float, int]:
    valid = [(i, f) for i, image in enumerate(images) if (f := features(image)) is not None]
    if len(valid) < settings.min_samples:
        return None, 0, 0.0, -1
    # At most ten samples: exhaustive clique selection costs <=1024 subsets.
    # Every accepted pair must agree, without medoid bridge chaining.
    compatible = [[agrees(a[1], b[1], settings) for b in valid] for a in valid]
    cliques = []
    for bits in range(1, 1 << len(valid)):
        chosen = [i for i in range(len(valid)) if bits & (1 << i)]
        if len(chosen) < settings.min_samples or len(chosen) / len(valid) < settings.consensus:
            continue
        if all(compatible[i][j] for i in chosen for j in chosen if i < j):
            cliques.append(chosen)
    if not cliques:
        return None, 0, 0.0, -1
    chosen = max(cliques, key=lambda indices: (len(indices), tuple(-i for i in indices)))
    if any(len(other) >= settings.min_samples and not set(other) & set(chosen) for other in cliques):
        return None, 0, 0.0, -1
    best = max(chosen, key=lambda i: (sum(similarity(valid[i][1], valid[j][1])[0] for j in chosen), -i))
    score = min(similarity(valid[best][1], valid[i][1])[0] for i in chosen)
    return valid[best][1], len(chosen), score, valid[best][0]


def label(index: int) -> str:
    result = ""
    while index >= 0:
        result = chr(65 + index % 26) + result
        index = index // 26 - 1
    return result


def reserved_ids(previous: dict[str, Any]) -> set[str]:
    values = set(previous.get("allocated_ids", []))
    values.update(previous.get("groups", {}))
    values.update(previous.get("names", {}))
    values.update(g for g in previous.get("overrides", {}).values() if g)
    values.update(r.get("automatic_group") for r in previous.get("games", []) if r.get("automatic_group"))
    return values


def group_signatures(
    records: list[dict[str, Any]], settings: PortraitSettings, previous: dict[str, Any] | None = None
) -> dict[str, Any]:
    records = sorted(
        records, key=lambda r: (r["game"].get("start") is None, r["game"].get("start") or 0, game_key(r["game"]))
    )
    adjacency = {i: {i} for i, r in enumerate(records) if r.get("_signature") is not None}
    for i in adjacency:
        for j in adjacency:
            if agrees(records[i]["_signature"], records[j]["_signature"], settings):
                adjacency[i].add(j)
    components: list[list[int]] = []
    remaining = set(adjacency)
    while remaining:
        todo, component = [min(remaining)], set()
        while todo:
            i = todo.pop()
            if i in component:
                continue
            component.add(i)
            todo.extend(adjacency[i] - component)
        remaining -= component
        if all(component <= adjacency[i] for i in component):
            components.append(sorted(component))
        else:
            for i in component:
                records[i]["reason"] = "ambiguous similarity bridge"
    previous = previous or {}
    old = {r["key"]: r.get("automatic_group", r.get("group")) for r in previous.get("games", [])}
    used: set[str] = set()
    reserved = reserved_ids(previous)
    for members in components:
        votes = Counter(old.get(game_key(records[i]["game"])) for i in members)
        candidates = sorted(((-n, str(g)) for g, n in votes.items() if g and g not in used))
        ordinal = 0
        while label(ordinal) in used or label(ordinal) in reserved:
            ordinal += 1
        name = candidates[0][1] if candidates else label(ordinal)
        used.add(name)
        for i in members:
            records[i]["group"] = name
    output: dict[str, Any] = {
        "version": VERSION,
        "settings": asdict(settings),
        "games": [],
        "overrides": dict(previous.get("overrides", {})),
        "names": dict(previous.get("names", {})),
        "selected": previous.get("selected"),
        "groups": {},
        "allocated_ids": sorted(reserved | used),
    }
    for record in records:
        row = {k: v for k, v in record.items() if not k.startswith("_")}
        row["key"] = game_key(row["game"])
        row["automatic_group"] = row.get("group")
        row["group"] = output["overrides"].get(row["key"], row.get("group"))
        output["games"].append(row)
    refresh_groups(output)
    return output


def refresh_groups(result: dict[str, Any]) -> None:
    groups: dict[str, Any] = {}
    for row in result.get("games", []):
        group = row.get("group")
        if not group:
            continue
        entry = groups.setdefault(
            group, {"games": [], "wins": 0, "portrait": row.get("portrait", ""), "class_name": None}
        )
        entry["games"].append(row["key"])
        entry["wins"] += int(str(row["game"].get("result", "")).upper() == "WIN")
    for name, entry in groups.items():
        votes = [r["class_evidence"] for r in result["games"] if r.get("group") == name and r.get("class_evidence")]
        counts = Counter(votes)
        from .labels import class_id

        # A game already carries multi-frame evidence; more games reinforce it.
        if class_id(name):
            entry["class_name"] = class_id(name)
        elif len(counts) == 1 and votes:
            entry["class_name"] = votes[0]
        from .labels import class_label

        entry["name"] = result.get("names", {}).get(name) or (
            class_label(entry["class_name"]) if entry["class_name"] else ""
        )
    result["groups"] = groups
    result["allocated_ids"] = sorted(reserved_ids(result) | set(groups))
    if result.get("selected") is None:
        result["selected"] = sorted(groups)


def sample_times(game: dict[str, Any], duration: float, settings: PortraitSettings,
                 *, after: float | None = None) -> list[float]:
    """Portrait sample times inside the game's gameplay.

    Without an observed START (stream joined mid-game, VS not broadcast), the
    portrait is still visible before the END: when the caller gives the
    previous boundary ``after``, sample the same span (horizon_s - delay_s)
    ending just before the result, never before ``after``. Board validation
    rejects any frame that is not gameplay.
    """
    if game.get("start") is None:
        if after is None or game.get("end") is None:
            return []
        end = min(float(game["end"]), duration)
        lo = max(float(after), end - (settings.horizon_s - settings.delay_s))
        hi = end - 2.0
        if hi - lo < 8:
            return []
        return [round(float(t), 3) for t in np.linspace(lo, hi, settings.samples)]
    start = float(game["start"])
    end = float(game.get("end") or duration)
    length = min(end, duration) - start
    if length < 8:
        return []
    lo = start + min(settings.delay_s, length * 0.25)
    hi = min(start + settings.horizon_s, end - max(2.0, length * 0.05))
    return [round(float(t), 3) for t in np.linspace(lo, hi, settings.samples)] if hi > lo else []


def previous_boundaries(games: list[dict[str, Any]]) -> list[float]:
    """Latest observed START/END of any other game before each game's first anchor.

    Chronological, independent of list order (games without START may be listed last).
    """
    anchors = [[float(g[k]) for k in ("start", "end") if g.get(k) is not None] for g in games]
    out = []
    for index, own in enumerate(anchors):
        first = min(own) if own else float("inf")
        earlier = [t for other, values in enumerate(anchors) if other != index for t in values if t < first]
        out.append(max(earlier, default=0.0))
    return out


class HeroGroupingService:
    def __init__(
        self,
        settings: PortraitSettings | None = None,
        *,
        sampler: Any = None,
        board_gate: Callable[[np.ndarray], float] | None = None,
        cache_dir: Path | None = None,
        class_refs: Path | None = None,
    ) -> None:
        self.settings = settings or PortraitSettings()
        from analysis.shared.frames import FrameSamplingService

        self.sampler = sampler or FrameSamplingService()
        self.cache_dir = cache_dir or cache_root() / "hero_groups"
        from .labels import default_class_refs

        self.class_refs = class_refs if class_refs is not None else default_class_refs()
        self.profile_fingerprint = ""
        if board_gate is None:
            from analysis.gameplay_editing import default_detector_assets
            from analysis.gameplay.hearthstone.visual.board import BoardPresenceDetector
            from analysis.gameplay.hearthstone.visual.state_profile import StateAwareProfile

            profile = StateAwareProfile(default_detector_assets("hearthstone")["state_profile"])
            board = BoardPresenceDetector(profile)
            cv = require_opencv()
            board_gate = lambda frame: board.score(cv.resize(frame, profile.reference_size))[0]
            self.profile_fingerprint = profile.fingerprint
        self.board_gate = board_gate

    def _references(self) -> dict[str, np.ndarray]:
        cv = require_opencv()
        from .labels import class_id

        refs = {}
        if self.class_refs and self.class_refs.is_dir():
            for file in sorted(self.class_refs.iterdir()):
                if file.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"} and class_id(file.stem):
                    image = cv.imdecode(np.frombuffer(file.read_bytes(), np.uint8), cv.IMREAD_COLOR)
                    if image is not None and features(image) is not None:
                        # Accept either an icon crop or the standard tall Hero Power card.
                        # Only the circular artwork is relevant; card text/language is excluded.
                        if 1.2 <= image.shape[0] / image.shape[1] <= 1.8:
                            side = round(image.shape[1] * 0.31)
                            cx, cy = round(image.shape[1] * 0.5), round(image.shape[0] * 0.315)
                            image = image[cy - side // 2 : cy + (side + 1) // 2, cx - side // 2 : cx + (side + 1) // 2]
                        if features(image) is not None:
                            refs[class_id(file.stem) or file.stem] = image
        return refs

    def _power_score(
        self,
        image: np.ndarray,
        reference: np.ndarray,
        *,
        image_features: dict[str, np.ndarray] | None = None,
        reference_features: dict[str, np.ndarray] | None = None,
    ) -> float:
        cv = require_opencv()
        feat = image_features if image_features is not None else features(image)
        ref = reference_features if reference_features is not None else features(reference)
        if feat is None or ref is None:
            return 0.0
        best = similarity(feat, ref)[0]
        # The structural board anchor provides a search box, not the exact icon
        # centre: allow local position/scale variation without reading card text.
        query = cv.cvtColor(cv.resize(image, (96, 96)), cv.COLOR_BGR2GRAY)
        for side in (32, 36, 40, 44, 48, 52, 56, 60, 64, 80, 96):
            template = cv.cvtColor(cv.resize(reference, (side, side)), cv.COLOR_BGR2GRAY)
            best = max(best, float(cv.matchTemplate(query, template, cv.TM_CCOEFF_NORMED).max()))
            record("hero_groups.comparisons", template_comparisons=1)
        return best

    @profiled("hero_groups.hero_power")
    def _power_evidence(self, powers: list[np.ndarray], refs: dict[str, np.ndarray]) -> dict[str, Any]:
        from .labels import class_id

        prepared = [(name, ref, feat) for name, ref in refs.items() if (feat := features(ref)) is not None]
        frames = []
        for image in powers:
            feat = features(image)
            if feat is None:
                continue
            values = {
                name: self._power_score(image, ref, image_features=feat, reference_features=ref_feat)
                for name, ref, ref_feat in prepared
            }
            if values:
                frames.append(values)
        if not frames:
            return {"class": None, "scores": {}, "support": 0, "status": "no_samples"}
        # Top five observations tolerate brief tooltip/used-power overlays, while
        # requiring repeated votes to call an assignment reliable.
        scores = {name: float(np.mean(sorted((f[name] for f in frames), reverse=True)[:5])) for name, _, _ in prepared}
        ranked = sorted(scores, key=lambda name: (-scores[name], name))
        best = ranked[0]
        margin = scores[best] - (scores[ranked[1]] if len(ranked) > 1 else 0.0)
        support = sum(max(frame, key=lambda name: frame[name]) == best for frame in frames)
        reliable = (
            support >= self.settings.min_samples
            and support / len(frames) >= self.settings.consensus
            and scores[best] >= self.settings.class_similarity
            and margin >= self.settings.class_margin
        )
        return {
            "class": class_id(best) or best,
            "scores": scores,
            "support": support,
            "frames": len(frames),
            "margin": margin,
            "best_template": best,
            "status": "matched" if reliable else "estimated",
        }

    def _classify(self, powers: list[np.ndarray], refs: dict[str, np.ndarray]) -> str | None:
        evidence = self._power_evidence(powers, refs)
        return evidence["class"] if evidence["status"] == "matched" else None

    @profiled("hero_groups")
    def analyze(
        self,
        source: Path,
        games: list[dict[str, Any]],
        *,
        duration: float,
        previous: dict[str, Any] | None = None,
        cancellation: AnalysisCancellation | None = None,
        on_progress: Callable[[int], None] | None = None,
    ) -> dict[str, Any]:
        token = cancellation or AnalysisCancellation()
        token.raise_if_cancelled()
        source = source.expanduser().resolve()
        profile = current_profile()
        if profile:
            profile.node("hero_groups")["cache"] = "pending"
        with measure("hero_groups.references"):
            refs = self._references()
        record("hero_groups", games_requested=len(games), references=len(refs))
        with measure("hero_groups.identity"):
            identity = {
                "source": str(source),
                "size": source.stat().st_size,
                "mtime": source.stat().st_mtime_ns,
                "fingerprint": media_fingerprint(source),
                "version": VERSION,
                "settings": asdict(self.settings),
                "board": self.profile_fingerprint,
                "quality_asset": hashlib.sha256(
                    (Path(__file__).parent / "assets" / "confirmation.png").read_bytes()
                ).hexdigest(),
                "refs": {k: hashlib.sha256(v.tobytes()).hexdigest() for k, v in refs.items()},
                "games": [{k: g.get(k) for k in ("id", "index", "start", "end", "result")} for g in games],
                "samples": [(game_key(g), sample_times(g, duration, self.settings, after=after))
                            for g, after in zip(games, previous_boundaries(games))],
            }
        key = digest(identity)
        cache = self.cache_dir / (key + ".json")
        try:
            with measure("hero_groups.cache_lookup"):
                cached = json.loads(cache.read_text(encoding="utf-8"))
            if cached.get("cache_key") == key and cached.get("version") == VERSION:
                if profile:
                    profile.cache("hero_groups", "hit")
                    for stage in ("sampling", "signature", "hero_power", "classification"):
                        profile.skip("hero_groups." + stage, "hero_group_cache_hit")
                record("hero_groups", cached_games=len(cached.get("games", [])))
                # Cached automatic result; edits remain project-owned and override it.
                if cached.get("grouping_mode") == "class":
                    from .classes import class_groups

                    result = class_groups(cached["games"], self.settings, previous)
                    result.update({k: cached[k] for k in ("cache_key", "sample_fingerprint", "source_identity")})
                    result["cache_hit"] = True
                    token.raise_if_cancelled()
                    return result
                result = cached
                old = {r["key"]: r.get("automatic_group", r.get("group")) for r in (previous or {}).get("games", [])}
                remap: dict[str, str] = {}
                used: set[str] = set()
                reserved = reserved_ids(previous or {})
                for auto in sorted({r["automatic_group"] for r in result["games"] if r.get("automatic_group")}):
                    votes = Counter(old.get(r["key"]) for r in result["games"] if r.get("automatic_group") == auto)
                    choices = sorted((-n, str(g)) for g, n in votes.items() if g and g not in used)
                    new_name = choices[0][1] if choices else auto
                    if new_name in used or (not choices and new_name in reserved):
                        ordinal = 0
                        while label(ordinal) in used or label(ordinal) in reserved:
                            ordinal += 1
                        new_name = label(ordinal)
                    remap[auto] = new_name
                    used.add(new_name)
                for row in result["games"]:
                    row["automatic_group"] = remap.get(row.get("automatic_group"), row.get("automatic_group"))
                for field in ("overrides", "names", "selected", "allocated_ids"):
                    if previous and field in previous:
                        result[field] = previous[field]
                for row in result["games"]:
                    row["group"] = result["overrides"].get(row["key"], row["automatic_group"])
                refresh_groups(result)
                result["cache_hit"] = True
                token.raise_if_cancelled()
                return result
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            pass
        if profile:
            profile.cache("hero_groups", "miss")
        records = []
        boundaries = previous_boundaries(games)
        for index, game in enumerate(games):
            token.raise_if_cancelled()
            images, powers, samples = [], [], []
            record("hero_groups", games_started=1)
            times = sample_times(game, duration, self.settings, after=boundaries[index])
            batched = None
            if callable(getattr(self.sampler, "get_many", None)):
                with measure("hero_groups.sampling"):
                    batched = self.sampler.get_many(source, times, max_width=960, cancellation=token)
            for timestamp in times:
                record("hero_groups", samples_requested=1)
                token.raise_if_cancelled()
                if batched is not None:
                    frame = batched.get(timestamp)
                else:
                    frames = self.sampler.sample(
                        source,
                        fps=1.0,
                        start_s=timestamp,
                        end_s=min(timestamp + 0.10, duration),
                        max_width=960,
                        cancellation=token,
                    )
                    with measure("hero_groups.sampling"):
                        try:
                            frame = next(frames, None)
                        finally:
                            close = getattr(frames, "close", None)
                            if close:
                                close()
                if frame is None:
                    continue
                record("hero_groups", frames=1)
                with measure("hero_groups.board_gate"):
                    board_score = self.board_gate(frame.image)
                sample = {"requested": timestamp, "timestamp": frame.timestamp, "board": board_score}
                samples.append(sample)
                if board_score < self.settings.board_min:
                    record("hero_groups", rejected_board_samples=1)
                    sample["rejected"] = "board not readable"
                    continue
                record("hero_groups", accepted_board_samples=1)
                portrait = crop(frame.image, self.settings.portrait_roi)
                images.append(portrait)
                powers.append(crop(frame.image, self.settings.power_roi))
                sample["portrait"] = thumbnail(portrait)
                sample["sha256"] = hashlib.sha256(portrait.tobytes()).hexdigest()
            feat, support, score, representative = signature(images, self.settings)
            record("hero_groups.signature", games=1, consistent_samples=support)
            evidence = self._power_evidence(powers, refs)
            records.append(
                {
                    "game": game,
                    "_signature": feat,
                    "_sample_features": [f for image in images if (f := features(image)) is not None],
                    "support": support,
                    "similarity": score,
                    "samples": samples,
                    "portrait": thumbnail(images[representative if representative >= 0 else 0]) if images else "",
                    "class_evidence": evidence["class"],
                    "power_evidence": evidence,
                    "reason": "" if feat is not None else "insufficient consistent readable portrait samples",
                }
            )
            if on_progress:
                on_progress(round(100 * (index + 1) / max(1, len(games))))
        from .classes import class_groups, classify_records

        if refs:
            with measure("hero_groups.classification"):
                classify_records(records, self.settings)
        elif profile:
            profile.skip("hero_groups.classification", "no_references")
        grouping = class_groups if refs else group_signatures
        with measure("hero_groups.grouping"):
            automatic = grouping(records, self.settings)
        record("hero_groups", games_processed=len(records),
               games_classified=sum(bool(r.get("class_evidence")) for r in records),
               groups=len(automatic.get("groups", {})))
        automatic.update(
            {
                "cache_key": key,
                "sample_fingerprint": digest([r["samples"] for r in records]),
                "source_identity": identity,
                "cache_hit": False,
            }
        )
        token.raise_if_cancelled()
        with measure("hero_groups.cache_write"):
            atomic_json(cache, automatic)
        # Preserve stable project IDs as well as manual assignments on recalculation.
        result = grouping(records, self.settings, previous)
        result.update({k: automatic[k] for k in ("cache_key", "sample_fingerprint", "source_identity", "cache_hit")})
        return result
