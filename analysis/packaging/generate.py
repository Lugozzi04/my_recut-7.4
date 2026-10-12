"""Final thumbnail generation (thumbnail engine "gen"): the image model composes, ReCut prepares.

The planning call (FAST: the single agent call) chooses, per concept, a ROLE and an ASSET PACKAGE
(best webcam crop + alternatives, 1-2 strong board frames, an optional card/detail crop) plus a short
moment description, hook and art direction. ReCut then:

  asset_package()       resolves the ids to real files (original webcam crops, not cut-outs; the stream's
                        webcam overlay inpainted out of board frames) and writes them to thumbnails/packages/
  generation_prompt()   ONE prompt for all variants, images referenced by index
  CodexImageGenerator   ONE `codex exec` call with the built-in image_gen tool (ChatGPT login, no API key);
                        the generated files are collected from $CODEX_HOME/generated_images
  normalize_generated() centre-crop to 16:9, 1280x720

draft_scene() keeps a small local scene per concept: it is used for validation, for diversity checks and
as the local fallback render when generation is unavailable or fails. It is never sent to the image model.

PRIVACY: the package images (webcam crops, gameplay frames) and the prompt are sent to the provider of the
codex CLI. Generated images are cached by prompt + image bytes: a re-run never calls the generator again.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

import numpy as np

GENERATOR_VERSION = "thumbnail_gen_v2"  # v2: per-variant composition recipes, punchier rules
ROLES = ("reaction-led", "card-led", "board-led", "narrative-led")
ROLE_TREATMENT = {"reaction-led": "reaction_cutout", "card-led": "card_hero", "board-led": "board_zoom",
                  "narrative-led": "cinematic_moment"}
FACE_TYPES = ("webcam_original", "webcam_subject")
BOARD_TYPES = ("gameplay_frame", "board_region", "background")
DETAIL_TYPES = ("card_crop", "card_artwork", "board_region", "gameplay_frame")
MAX_GENERATION_IMAGES = 12
OUTPUT_SIZE = (1280, 720)


# ---- planning schema (the concept fields that replace the scene graph) ---------------------------------
def package_schema() -> dict[str, Any]:
    ids = {"type": "array", "items": {"type": "string"}}
    return {"type": "object", "additionalProperties": False, "required": ["face", "board", "detail"],
            "properties": {"face": {**ids, "description": "0-3 webcam ids (W.. or S..), best first"},
                           "board": {**ids, "description": "1-2 frame/region ids (F.. or B..), strongest first"},
                           "detail": {**ids, "description": "0-1 card (O.. observation, K..) or region id"}}}


def planning_section(n_concepts: int) -> str:
    roles = ", ".join(ROLES)
    return (f"D. ASSET PACKAGE per concept (the final thumbnail is COMPOSED BY AN IMAGE MODEL from these assets; do "
            f"not design pixel layouts). role: one of {roles}"
            + (f"; with {n_concepts} concepts use each role at least once" if n_concepts >= len(ROLES) else
               "; every concept a different role") +
            ". Role guidance: board-led = the most spectacular board state (lethal, huge swing, full board, big "
            "effect), never an ordinary board; narrative-led = the story of the video in ONE image (decisive moment + "
            "the creator's emotion, or the result), never a before/after comparison of two ordinary boards. "
            "Each concept should show a DIFFERENT moment (another highlight or game) when the material allows. "
            "package.face: 0-3 webcam ids, the sharpest (highest sharp=, never motion-blurred) and most expressive "
            "first (required for reaction-led; "
            "otherwise optional, and at least one concept should work without the face). package.board: 1-2 ids "
            "of the most readable frames/regions of the moment. package.detail: 0-1 card observation (O..), card "
            "(K..) or region (B..) - required for card-led. moment: <= 160 characters, what happens on screen. "
            "art_direction: <= 300 characters for the image model: protagonist, how the face is used (if at all), "
            "background treatment, mood and the punch (zoom, light, effects). Do NOT forbid text or effects and do "
            "NOT fix exact positions: placement, text style and colour are assigned per variant later. Every concept "
            "needs a hook text except at most one; at most 2 hooks may be questions; 2-3 strong elements, never a "
            "collage.")


def manifest_lines(manifest: Any, *, limit: int = 90) -> list[str]:
    """Compact manifest for the planning call: no cut-out entries (the image model gets the original webcam crop;
    'cutout' only says a clean local cut-out exists), no layout hints."""
    isolated = {a.parent_id for a in manifest.of_type("webcam_subject") if a.quality.get("isolated")}
    labels = {"gameplay_frame": "frame", "webcam_original": "webcam", "board_region": "region",
              "background": "frame", "card_crop": "card", "card_artwork": "card"}
    lines = []
    for a in [a for a in manifest.assets if a.type != "webcam_subject"][:limit]:
        t = "--:--" if a.edited_time is None else f"{int(a.edited_time) // 60:02d}:{int(a.edited_time) % 60:02d}"
        extra = []
        if a.type == "webcam_original":
            extra.append(f"{a.width}x{a.height}" + (" cutout" if a.id in isolated else "")
                         + (f" sharp={a.quality['sharpness']:.0f}" if "sharpness" in a.quality else ""))
        elif "sharpness" in a.quality:
            extra.append(f"sharp={a.quality['sharpness']:.0f}")
        if a.type == "board_region" and a.parent_id:
            extra.append(f"of {a.parent_id}")
        if a.limitations:
            extra.append("LIMITS: " + "; ".join(a.limitations))
        lines.append(f"- {a.id} {labels.get(a.type, a.type)} {a.highlight_id or '-'} {t} {' '.join(extra)}".rstrip())
    return lines


# ---- validation helpers ----------------------------------------------------------------------------------
def _isolated_subject(face_id: str, assets: Mapping[str, Mapping[str, Any]]) -> str | None:
    info = assets.get(face_id) or {}
    if info.get("type") == "webcam_subject":
        return face_id if (info.get("quality") or {}).get("isolated") else None
    for aid, other in assets.items():
        if other.get("type") == "webcam_subject" and other.get("parent_id") == face_id and \
                (other.get("quality") or {}).get("isolated"):
            return aid
    return None


def _webcam_original(face_id: str, assets: Mapping[str, Mapping[str, Any]]) -> str:
    info = assets.get(face_id) or {}
    if info.get("type") == "webcam_subject" and info.get("parent_id"):
        return str(info["parent_id"])
    return face_id


def check_package(concept: Mapping[str, Any], assets: Mapping[str, Mapping[str, Any]],
                  observations: Mapping[str, Any]) -> list[str]:
    cid = concept.get("concept_id")
    problems: list[str] = []
    role = concept.get("role")
    if role not in ROLES:
        problems.append(f"{cid}: role must be one of {', '.join(ROLES)}")
    package = concept.get("package") or {}
    face, board, detail = (list(package.get(k) or []) for k in ("face", "board", "detail"))
    for aid in face:
        if (assets.get(aid) or {}).get("type") not in FACE_TYPES:  # W.. (or S.., accepted for compatibility)
            problems.append(f"{cid}: package.face {aid} is not a webcam asset")
    if not 1 <= len(board) <= 2:
        problems.append(f"{cid}: package.board needs 1-2 frame/region ids")
    for aid in board:
        if (assets.get(aid) or {}).get("type") not in BOARD_TYPES:
            problems.append(f"{cid}: package.board {aid} is not a frame/region asset")
    if len(detail) > 1:
        problems.append(f"{cid}: package.detail takes at most one id")
    for aid in detail:
        if aid not in observations and (assets.get(aid) or {}).get("type") not in DETAIL_TYPES:
            problems.append(f"{cid}: package.detail {aid} is not a card/region asset")
    if len(face) > 3:
        problems.append(f"{cid}: package.face takes at most 3 ids")
    if role == "reaction-led" and not face:
        problems.append(f"{cid}: reaction-led needs package.face")
    if role == "card-led" and not detail:
        problems.append(f"{cid}: card-led needs a card in package.detail")
    return problems


def check_roles(concepts: Sequence[Mapping[str, Any]]) -> list[str]:
    roles = [c.get("role") for c in concepts]
    problems = []
    if len(concepts) >= len(ROLES):
        missing = [r for r in ROLES if r not in roles]
        if missing:
            problems.append(f"portfolio: roles {missing} are missing (each role at least once)")
    elif len(set(roles)) != len(roles):
        problems.append("portfolio: every concept needs a different role")
    silent = [c.get("concept_id") for c in concepts if not str(c.get("thumbnail_text") or "").strip()]
    if len(concepts) >= 3 and len(silent) > 1:
        problems.append(f"portfolio: {len(silent)} concepts without thumbnail text ({', '.join(map(str, silent))}); "
                        "at most one")
    questions = [c.get("concept_id") for c in concepts if str(c.get("thumbnail_text") or "").strip().endswith("?")]
    if len(questions) > 2:
        problems.append(f"portfolio: {len(questions)} thumbnail texts are questions ({', '.join(map(str, questions))}); "
                        "at most 2 - vary the hook style")
    return problems


# ---- local draft (validation, diversity, fallback render) ------------------------------------------------
def draft_scene(concept: Mapping[str, Any], assets: Mapping[str, Mapping[str, Any]]) -> tuple[dict[str, Any], str]:
    """A small local scene for the concept's role; returns (scene, protagonist asset id)."""
    from .scene import preset_board, preset_board_band, preset_reaction, preset_story

    package = concept.get("package") or {}
    face, board, detail = (list(package.get(k) or []) for k in ("face", "board", "detail"))
    text = str(concept.get("thumbnail_text") or "").strip() or None
    background = board[0]
    subject = _isolated_subject(face[0], assets) if face else None
    bubble = _webcam_original(face[0], assets) if face and not subject else None
    second = detail[0] if detail else (board[1] if len(board) > 1 else None)
    role = concept.get("role")
    is_card = bool(detail) and (detail[0] not in assets or  # an O.. observation is a (virtual) card
                                (assets.get(detail[0]) or {}).get("type") in ("card_crop", "card_artwork"))
    if role == "reaction-led":
        if subject:
            scene = preset_reaction(background=background, subject=subject, text=text,
                                    element=detail[0] if detail else None,
                                    element_type="card" if is_card else "board_crop")
            return scene, subject
        scene = preset_story(background=background, text=text, bubble=bubble)
        return scene, str(bubble)
    if role == "card-led":
        if is_card:
            scene = preset_board(background=background, text=text, cards=detail[:1], subject=subject)
        else:
            scene = preset_board(background=background, text=text, region=detail[0], subject=subject)
        return scene, detail[0]
    if role == "board-led":
        if (assets.get(background) or {}).get("type") == "board_region":
            scene = preset_board(background=board[1] if len(board) > 1 else str(
                (assets.get(background) or {}).get("parent_id") or background), text=text, region=background)
            return scene, background
        inset = second if second and (assets.get(second) or {}).get("type") in BOARD_TYPES else None
        scene = preset_board_band(background=background, background_crop=None, text=text, inset=inset)
        return scene, background
    inset = second if second and (assets.get(second) or {}).get("type") in BOARD_TYPES else None
    scene = preset_story(background=background, text=text, inset=inset, subject=subject, bubble=bubble)
    return scene, background


def attach_drafts(raw: dict[str, Any], assets: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Adds scene, treatment and a consistent primary_subject to every concept; returns package problems."""
    observations = {str(o.get("obs_id")): o for o in raw.get("card_observations") or []}
    problems: list[str] = []
    for concept in raw.get("concepts") or []:
        found = check_package(concept, assets, observations)
        problems += found
        if found:
            continue
        scene, primary = draft_scene(concept, assets)
        concept["scene"] = scene
        concept["treatment"] = ROLE_TREATMENT[concept["role"]]
        concept["primary_subject"] = primary
    problems += check_roles(raw.get("concepts") or [])
    return problems


# ---- package files for the image model -------------------------------------------------------------------
@dataclass
class PackageImage:
    asset_id: str
    kind: str  # face_main | face_alt | board | detail
    path: Path
    note: str = ""


@dataclass
class AssetPackage:
    concept_id: str
    role: str
    hook: str
    moment: str
    art_direction: str
    message: str
    title: str
    images: list[PackageImage] = field(default_factory=list)

    def to_mapping(self, root: Path | None = None) -> dict[str, Any]:
        def rel(p: Path) -> str:
            try:
                return str(p.relative_to(root)) if root else str(p)
            except ValueError:
                return str(p)
        return {"concept_id": self.concept_id, "role": self.role, "hook": self.hook, "moment": self.moment,
                "art_direction": self.art_direction, "message": self.message, "title": self.title,
                "images": [{"asset_id": i.asset_id, "kind": i.kind, "path": rel(i.path), "note": i.note}
                           for i in self.images]}


def _concept_assets(concept: Mapping[str, Any]) -> list[tuple[str, str]]:
    package = concept.get("package") or {}
    face = list(package.get("face") or [])
    alternatives = 2 if concept.get("role") == "reaction-led" else 0
    items = [(face[0], "face_main")] if face else []
    items += [(aid, "face_alt") for aid in face[1:1 + alternatives]]
    items += [(aid, "board") for aid in list(package.get("board") or [])[:2]]
    items += [(aid, "detail") for aid in list(package.get("detail") or [])[:1]]
    return items


def asset_package(concepts: Sequence[Mapping[str, Any]], assets: Mapping[str, Mapping[str, Any]],
                  images: Mapping[str, np.ndarray], folder: Path, *,
                  mask_boxes: Sequence[Sequence[float]] = ()) -> list[AssetPackage]:
    """Writes each needed asset once (JPEG, <= 1280 px) and returns one package per concept."""
    from .frames import write_image
    from .render import mask_regions

    folder.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    packages = []
    for concept in concepts:
        package = AssetPackage(str(concept.get("concept_id")), str(concept.get("role") or ""),
                               str(concept.get("thumbnail_text") or ""), str(concept.get("moment") or ""),
                               str(concept.get("art_direction") or ""), str(concept.get("message") or ""),
                               str(concept.get("title") or ""))
        for aid, kind in _concept_assets(concept):
            source_id = _webcam_original(aid, assets) if kind.startswith("face") else aid
            image = images.get(source_id)
            if image is None:
                continue
            if source_id not in written:
                if image.ndim == 3 and image.shape[2] == 4:
                    image = np.ascontiguousarray(image[..., :3])
                info = assets.get(source_id) or {}
                if info.get("type") == "gameplay_frame" and mask_boxes:
                    image = mask_regions(image, mask_boxes)  # the stream's own webcam overlay is not a subject
                h, w = image.shape[:2]
                if max(h, w) > 1280:
                    import cv2

                    scale = 1280 / max(h, w)
                    image = cv2.resize(image, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
                path = folder / f"{source_id}.jpg"
                write_image(path, image, quality=92)
                written[source_id] = path
            info = assets.get(source_id) or {}
            note = ", ".join(str(x) for x in info.get("limitations") or [])
            name = (info.get("provenance") or {}).get("name")
            if name:
                note = f"card '{name}'" + (f"; {note}" if note else "")
            package.images.append(PackageImage(source_id, kind, written[source_id], note))
        packages.append(package)
    return packages


# ---- the request to the image model ----------------------------------------------------------------------
GENERATION_RULES = (
    "You are a senior YouTube thumbnail designer. Create finished, professional, clickable YouTube thumbnails for a "
    "Hearthstone (Standard/Ranked ladder) video, built from the REAL reference images attached.",
    "Each thumbnail: landscape 16:9 (1280x720 or larger), ONE clear idea readable in one second at phone size, a "
    "strong visual hierarchy with at most 2-3 strong elements, clean depth (subject in front, simplified background), "
    "high contrast, saturated but natural colour, crisp edges. No chaos, no collage, no 'screenshot with stickers' "
    "look, no picture-in-picture boxes, no emoji, logos, watermarks or fake game UI.",
    "Give every thumbnail PUNCH: never just upscale a screenshot. Re-stage the moment: zoom into the key area, add "
    "depth, dramatic light, glow, light rays, energy or motion effects around the decisive element; one simple "
    "circle or arrow is allowed when it makes the key element obvious.",
    "The creator's webcam crop is a SUBJECT, not a live-stream box: cut the person out, enhance sharpness, lighting "
    "and skin tones, upscale as needed, enlarge the face so it reads at small size, and blend it into the scene "
    "with matching light, rim light and shadow. Keep the SAME real person: same face, glasses, hair, beard and "
    "clothing, natural expression of the reference (you may intensify it slightly, never change identity, age or "
    "features). Use the alternative webcam crops only to pick the best expression or to reconstruct detail.",
    "Board and cards come from the real game: keep them recognisable and readable. Never invent or alter card names, "
    "card text, mana/attack/health numbers, hero portraits or the game result. You may crop, zoom, relight, sharpen, "
    "blur or darken the background and isolate the key card/board area. In gameplay frames the stream's own webcam "
    "overlay was removed and may leave a smeared patch: never reproduce that patch or the stream overlays (score "
    "boxes, deck trackers, player names).",
    "Text: use EXACTLY the given hook text (same words, same language, correct spelling), 1-4 words, huge and "
    "readable at phone size, at most two lines, in the given text style, placed where the variant's composition "
    "says, away from the face and the key card, never in the bottom-right corner (YouTube duration badge). If the "
    "hook is empty, use no text.",
    "Each variant has a COMPOSITION, a TEXT STYLE and a COLOUR GRADE: follow them exactly - they override any "
    "placement mentioned in the art direction. Variants must look like four different thumbnails.",
)


def generation_prompt(packages: Sequence[AssetPackage], *, language: str, style_note: str = "",
                      compositions: Mapping[str, Mapping[str, str]] | None = None) -> tuple[str, list[Path]]:
    """One prompt for all variants; returns (prompt, ordered image list referenced by index)."""
    order: list[Path] = []
    index: dict[Path, int] = {}
    for package in packages:
        for image in package.images:
            if image.path not in index and len(order) < MAX_GENERATION_IMAGES:
                index[image.path] = len(order) + 1
                order.append(image.path)
    out = [f"PROMPT_VERSION: {GENERATOR_VERSION}", *GENERATION_RULES]
    if style_note:
        out.append(f"Creator style: {style_note}")
    out += ["", f"Generate {len(packages)} thumbnails, one image_gen call per variant, in this order: "
            + ", ".join(p.concept_id for p in packages) + ". Do not generate extra images.", ""]
    labels = {"face_main": "main webcam crop (the creator)", "face_alt": "alternative webcam crop",
              "board": "gameplay frame/board", "detail": "card/detail crop"}
    for package in packages:
        out.append(f"VARIANT {package.concept_id} - role {package.role}")
        out.append(f"- hook text ({language}): \"{package.hook}\"" if package.hook else "- hook text: none (no text)")
        if package.message:
            out.append(f"- message: {package.message}")
        if package.moment:
            out.append(f"- moment: {package.moment}")
        if package.art_direction:
            out.append(f"- art direction: {package.art_direction}")
        recipe = (compositions or {}).get(package.concept_id)
        if recipe:
            out.append(f"- composition: {recipe['layout']}")
            out.append(f"- text style: {recipe['text_style']}")
            out.append(f"- colour grade: {recipe['grade']}")
        for image in package.images:
            if image.path in index:
                note = f" ({image.note})" if image.note else ""
                out.append(f"- image {index[image.path]}: {labels.get(image.kind, image.kind)}{note}")
        out.append("")
    out.append("After generating, answer with the JSON object only: for each variant its concept_id and the path of "
               "the generated file if you know it (else an empty string), in generation order.")
    return "\n".join(out), order


GENERATION_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["variants"], "properties": {
    "variants": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                            "required": ["concept_id", "path"],
                                            "properties": {"concept_id": {"type": "string"},
                                                           "path": {"type": "string"}}}}}}


# ---- generator -------------------------------------------------------------------------------------------
class GenerationError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class ThumbnailGenerator(Protocol):
    name: str

    def available(self) -> bool: ...

    def generate(self, prompt: str, images: Sequence[Path], concept_ids: Sequence[str],
                 out_dir: Path) -> dict[str, Path]: ...


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def _snapshot(folder: Path) -> dict[Path, float]:
    if not folder.is_dir():
        return {}
    return {p: p.stat().st_mtime for p in folder.rglob("*") if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")}


class CodexImageGenerator:
    """ONE `codex exec` call with the built-in image_gen tool; files are taken from $CODEX_HOME/generated_images."""

    name = "codex_image_gen"

    def __init__(self, *, executable: str | None = None, model: str | None = None, timeout_s: float = 1500.0) -> None:
        from analysis.boundaries.agents import CodexBoundaryResolver
        from analysis.boundaries.resolvers import ResolverOptions

        self._locator = CodexBoundaryResolver(ResolverOptions(executable=executable, model=model))
        self.model = model
        self.timeout_s = timeout_s
        self.meta: dict[str, Any] = {}

    def available(self) -> bool:
        return self._locator.locate() is not None

    def command(self, executable: str, workdir: Path, schema_path: Path, output_path: Path,
                images: Sequence[Path]) -> list[str]:
        cmd = [executable, "exec", "--enable", "image_generation", "--sandbox", "read-only", "--skip-git-repo-check",
               "--ignore-rules", "--color", "never", "-C", str(workdir), "--output-schema", str(schema_path),
               "-o", str(output_path)]
        if self.model:
            cmd += ["-m", self.model]
        cmd += [f"--image={image}" for image in images]
        return cmd + ["--", "-"]

    def generate(self, prompt: str, images: Sequence[Path], concept_ids: Sequence[str],
                 out_dir: Path) -> dict[str, Path]:
        from analysis.boundaries.agents import AgentCallError, run_agent_process

        from .agent import codex_tokens

        executable = self._locator.locate()
        if executable is None:
            raise GenerationError("cli_missing", "codex CLI not found")
        generated_root = codex_home() / "generated_images"
        before = _snapshot(generated_root)
        started = time.time()
        with tempfile.TemporaryDirectory(prefix="recut-pack-gen-") as folder:
            root = Path(folder)
            workdir = root / "empty"
            workdir.mkdir()
            inputs = root / "inputs"
            inputs.mkdir()
            copies = []
            for n, image in enumerate(images, start=1):
                copy = inputs / f"image_{n:02d}{image.suffix.lower()}"
                shutil.copyfile(image, copy)
                copies.append(copy)
            schema_path, output_path = root / "schema.json", root / "answer.json"
            schema_path.write_text(json.dumps(GENERATION_SCHEMA), encoding="utf-8")
            try:
                _, stderr = run_agent_process(self.command(executable, workdir, schema_path, output_path, copies),
                                              prompt, workdir, self.timeout_s)
            except AgentCallError as error:
                raise GenerationError(error.code, error.detail) from error
            answer: dict[str, Any] = {}
            try:
                answer = json.loads(output_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                answer = {}
        self.meta = {"runtime_s": round(time.time() - started, 3), "tokens_used": codex_tokens(stderr),
                     "images_sent": len(images), "prompt_chars": len(prompt)}
        new = sorted((p for p, m in _snapshot(generated_root).items() if p not in before or before[p] != m),
                     key=lambda p: p.stat().st_mtime)
        mapping = map_generated(answer, new, concept_ids)
        if not mapping:
            raise GenerationError("no_images", f"codex produced {len(new)} new images; none could be mapped")
        out_dir.mkdir(parents=True, exist_ok=True)
        result = {}
        for cid, path in mapping.items():
            target = out_dir / f"{cid}{path.suffix.lower()}"
            shutil.copyfile(path, target)  # copy: the originals stay in the user's codex folder
            result[cid] = target
        return result


def map_generated(answer: Mapping[str, Any], new_files: Sequence[Path], concept_ids: Sequence[str]) -> dict[str, Path]:
    """Generated files -> concept ids: paths reported by the model first, then generation order."""
    mapping: dict[str, Path] = {}
    resolved = {p.resolve(): p for p in new_files}
    for item in answer.get("variants") or []:
        cid, path = str(item.get("concept_id") or ""), str(item.get("path") or "")
        if cid in concept_ids and path:
            candidate = Path(path).expanduser()
            if candidate.resolve() in resolved:
                mapping[cid] = resolved[candidate.resolve()]
    remaining = [p for p in new_files if p not in mapping.values()]
    reported = [str(i.get("concept_id")) for i in answer.get("variants") or [] if i.get("concept_id") in concept_ids]
    order = [c for c in reported if c not in mapping] or [c for c in concept_ids if c not in mapping]
    if len(remaining) == len(order):
        mapping.update(dict(zip(order, remaining)))
    elif remaining and len(remaining) > len(order) and order:
        mapping.update(dict(zip(order, remaining[-len(order):])))  # retries inside the session: keep the last ones
    return mapping


def normalize_generated(path: Path, target: Path) -> tuple[int, int] | None:
    """Centre-crop to 16:9 and resize to 1280x720; returns the source size (None if unreadable)."""
    import cv2

    from .frames import read_image, write_image

    image = read_image(path)
    if image is None:
        return None
    h, w = image.shape[:2]
    want = 16 / 9
    if w / h > want:
        cw = int(h * want)
        image = image[:, (w - cw) // 2:(w - cw) // 2 + cw]
    else:
        ch = int(w / want)
        image = image[(h - ch) // 2:(h - ch) // 2 + ch]
    interpolation = cv2.INTER_AREA if image.shape[1] >= OUTPUT_SIZE[0] else cv2.INTER_CUBIC
    write_image(target, cv2.resize(image, OUTPUT_SIZE, interpolation=interpolation), quality=95)
    return w, h


def generation_key(prompt: str, sources: Sequence[str]) -> str:
    """Prompt + identity of the SOURCE asset files (stable across runs, unlike re-encoded package JPEGs)."""
    from .cache import stable_key

    return stable_key("generated", GENERATOR_VERSION, hashlib.sha256(prompt.encode("utf-8")).hexdigest(), list(sources))


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:24] if path.is_file() else "missing"


# ---- originality: composition recipes that rotate across videos -------------------------------------------
# The concepts (WHAT each thumbnail says) come from the agent; HOW it is laid out rotates here, so two videos
# never get the same placements. The choice is deterministic per video (same video -> same prompt ->
# generation cache hit, so --only render stays free) and avoids what the previous videos used (a small
# history file in the packaging cache).
RECIPES: dict[str, list[dict[str, str]]] = {
    "reaction-led": [
        {"id": "R1", "face": "left", "layout": "the creator cut out large on the LEFT third, looking toward the game "
                                               "element on the right; hook in the upper right"},
        {"id": "R2", "face": "right", "layout": "the creator large on the RIGHT, the game element on the left; hook "
                                                "across the bottom left on a bold tilted banner"},
        {"id": "R3", "face": "center", "layout": "the creator centred at the bottom, breaking out of the frame edge, "
                                                 "the moment large behind; hook across the top"},
        {"id": "R4", "face": "diagonal", "layout": "an extreme close-up of the face filling half the frame on a "
                                                   "diagonal, the game element in the opposite corner; hook stacked "
                                                   "vertically beside the face"},
        {"id": "R5", "face": "corner", "layout": "the game moment full-bleed, the creator as a large cut-out popping "
                                                 "in from the lower corner; hook in the opposite upper corner"},
    ],
    "card-led": [
        {"id": "C1", "face": "none", "layout": "the card huge and slightly rotated on the LEFT, hook on the right"},
        {"id": "C2", "face": "none", "layout": "the card huge on the RIGHT with a light burst behind it, hook on the "
                                               "left"},
        {"id": "C3", "face": "small-left", "layout": "the card centred and tilted toward the viewer, the creator small "
                                                     "in the lower left reacting to it; hook across the top"},
        {"id": "C4", "face": "none", "layout": "the card in the centre at extreme size, its art spilling out of the "
                                               "frame; hook split above and below"},
        {"id": "C5", "face": "small-right", "layout": "the card on the left, the creator small on the right looking "
                                                      "at it; hook at the bottom"},
    ],
    "board-led": [
        {"id": "B1", "face": "none", "layout": "a low dramatic zoom on the key board area with depth of field; hook "
                                               "across the top"},
        {"id": "B2", "face": "none", "layout": "the decisive minions or hero pushed to the foreground with motion and "
                                               "light streaks, the rest of the board darkened; hook at the bottom"},
        {"id": "B3", "face": "small-corner", "layout": "the key board area tilted in perspective, the creator small "
                                                       "in a corner; hook on the opposite side"},
        {"id": "B4", "face": "none", "layout": "split light: the key area lit, everything else in shadow, a "
                                               "spotlight on the decisive element; hook on the left"},
    ],
    "narrative-led": [
        {"id": "N1", "face": "right", "layout": "the decisive moment large on the left, the creator reacting on the "
                                                "right; hook at the top centre"},
        {"id": "N2", "face": "left", "layout": "the creator on the left, the result or key moment glowing on the "
                                               "right; hook at the bottom"},
        {"id": "N3", "face": "none", "layout": "one cinematic frame of the key moment with strong colour grading and "
                                               "vignette; hook big in the centre-left"},
        {"id": "N4", "face": "center", "layout": "the creator centred and small in the middle ground, the moment huge "
                                                 "behind like a poster; hook across the top"},
    ],
}
TEXT_STYLES = [
    "white extra-bold sans with a thick black outline",
    "yellow bold with a dark outline and a slight 3D extrusion",
    "white on a solid tilted colour banner",
    "two-tone: the key word in the accent colour, the rest white",
    "condensed all-caps with a soft glow",
    "heavy rounded sans with a drop shadow and gradient fill",
]
GRADES = ["warm orange / teal", "purple / gold", "cold blue with a red accent", "vivid neon on dark",
          "dark cinematic with rim light", "bright clean daylight"]
HISTORY_FILE = "composition_history.json"
HISTORY_KEEP = 6  # videos remembered


def _load_history(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and isinstance(data.get("runs"), list) else {"runs": []}
    except (OSError, ValueError):
        return {"runs": []}


def choose_compositions(packages: Sequence[AssetPackage], *, video_key: str,
                        history_path: Path | None) -> dict[str, dict[str, str]]:
    """concept_id -> {recipe, layout, face, text_style, grade}. Same video -> same choice (reruns stay cached);
    a new video avoids the recipes, text styles and grades of the previous videos where it can."""
    import random

    history = _load_history(history_path) if history_path else {"runs": []}
    for run in history["runs"]:
        if run.get("video") == video_key and isinstance(run.get("choice"), dict):
            return dict(run["choice"])
    used_recipes = {r for run in history["runs"] for r in (run.get("recipes") or [])}
    used_styles = {s for run in history["runs"][-2:] for s in (run.get("styles") or [])}
    used_grades = {g for run in history["runs"][-2:] for g in (run.get("grades") or [])}
    rng = random.Random(int(hashlib.sha256(video_key.encode("utf-8")).hexdigest()[:12], 16))
    styles = [s for s in TEXT_STYLES if s not in used_styles] or list(TEXT_STYLES)
    grades = [g for g in GRADES if g not in used_grades] or list(GRADES)
    rng.shuffle(styles)
    rng.shuffle(grades)
    choice: dict[str, dict[str, str]] = {}
    faces: set[str] = set()
    for n, package in enumerate(packages):
        pool = list(RECIPES.get(package.role) or RECIPES["narrative-led"])
        rng.shuffle(pool)
        has_face = any(i.kind.startswith("face") for i in package.images)

        def rank(recipe: dict[str, str], has_face: bool = has_face, role: str = package.role
                 ) -> tuple[int, int, int, int]:
            side = recipe["face"]
            needs_missing_face = int(not has_face and side != "none")  # no webcam in this package
            same_side = int(has_face and side != "none" and side in faces)  # face on the same side twice
            # the face already leads another variant: other roles prefer a faceless or small-face layout
            face_again = int(role != "reaction-led" and bool(faces) and side not in ("none",)
                             and not side.startswith("small"))
            return (needs_missing_face, same_side, face_again, int(recipe["id"] in used_recipes))

        recipe = min(pool, key=rank)
        if has_face and recipe["face"] != "none":
            faces.add(recipe["face"])
        choice[package.concept_id] = {"recipe": recipe["id"], "layout": recipe["layout"], "face": recipe["face"],
                                      "text_style": styles[n % len(styles)], "grade": grades[n % len(grades)]}
    return choice


def remember_compositions(history_path: Path, video_key: str, choice: dict[str, dict[str, str]]) -> None:
    history = _load_history(history_path)
    runs = [r for r in history["runs"] if r.get("video") != video_key]
    runs.append({"video": video_key, "time": time.strftime("%Y-%m-%dT%H:%M:%S"), "choice": choice,
                 "recipes": sorted({c["recipe"] for c in choice.values()}),
                 "styles": sorted({c["text_style"] for c in choice.values()}),
                 "grades": sorted({c["grade"] for c in choice.values()})})
    try:
        history_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = history_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"runs": runs[-HISTORY_KEEP:]}, indent=1), encoding="utf-8")
        os.replace(tmp, history_path)
    except OSError:
        pass
