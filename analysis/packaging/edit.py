"""Edit, revert and approve the thumbnails of a finished packaging run (CLI loop and GUI share this).

edit      ONE image-model call on ONE thumbnail: the current image is image 1, the original references of
          its package (webcam crop, card, board) follow so the person and the game data stay real across
          edits. Every version is kept (thumbnails/edits/C2_v0.png = the original, v1, v2, ...);
          thumbnails/concept_NN.png always shows the latest one.
revert    makes an earlier version current again (no call).
approve   copies the chosen thumbnail to thumbnails/thumbnail_final.png, writes selection.json and, when the
          run has a creator profile, stores the approval in the style memory.

PRIVACY: an edit sends the current thumbnail and its reference images to the provider of the codex CLI.
"""
from __future__ import annotations

import json
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Callable

EDIT_VERSION = "thumbnail_edit_v1"
EDITS_FILE = "thumbnail_edits.json"

EDIT_RULES = (
    "You are editing an existing YouTube thumbnail for a Hearthstone video. Image 1 is the CURRENT thumbnail.",
    "Apply ONLY the change requested by the creator. Keep everything else as it is: composition, the person, the "
    "cards, the board, the colours and the overall style, unless the request asks to change them.",
    "If the request changes the text, write EXACTLY the requested words (same language, correct spelling), still "
    "huge and readable at phone size, never in the bottom-right corner (YouTube duration badge).",
    "Keep the SAME real person: same face, glasses, hair and clothing (the original webcam frame, when attached, "
    "is the identity reference). Never invent or alter card names, card text or mana/attack/health numbers (the "
    "attached game frames are the real references).",
    "Output exactly ONE landscape 16:9 image with one image_gen call. Then answer with the JSON object only.",
)


class EditError(RuntimeError):
    pass


class ThumbnailEditor:
    def __init__(self, output: str | Path, *, generator: Any = None, timeout_s: float = 1500.0,
                 progress: Callable[[str], None] | None = None) -> None:
        self.out = Path(output).expanduser().resolve()
        self.thumbs = self.out / "thumbnails"
        self.edits_dir = self.thumbs / "edits"
        self._generator = generator
        self.timeout_s = timeout_s
        self.progress = progress or (lambda message: None)
        try:
            self._concepts = json.loads((self.out / "thumbnail_concepts.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise EditError(f"{self.out} is not a finished packaging run (thumbnail_concepts.json): {exc}") from exc

    # ---- reading ---------------------------------------------------------------------------------------
    def concepts(self) -> list[dict[str, Any]]:
        """[{number, concept_id, title, hook, path, versions, current_version, approved}] in display order."""
        history = self._history()
        approved = (self._selection() or {}).get("thumbnail", {}) or {}
        out = []
        for n, concept in enumerate(self._concepts.get("concepts", []), start=1):
            cid = str(concept.get("concept_id"))
            path = self._current_path(concept, n)
            edits = [e for e in history["edits"] if e.get("concept_id") == cid]
            out.append({"number": n, "concept_id": cid, "title": concept.get("title", ""),
                        "hook": concept.get("thumbnail_text", ""), "role": concept.get("role"),
                        "quality_warnings": (concept.get("render") or {}).get("quality", {}).get("warnings", []),
                        "typography_mode": (concept.get("design_pack") or {}).get("typography", {}).get("mode"),
                        "path": str(path), "exists": path.is_file(),
                        "versions": [str(p) for p in self._versions(cid)],
                        "current_version": history["current"].get(cid, 0), "edits": edits,
                        "approved": approved.get("concept_id") == cid})
        return out

    def resolve(self, ref: str | int) -> dict[str, Any]:
        """Accepts 2, "2", "C2", "concept_02"."""
        text = str(ref).strip().lower()
        items = self.concepts()
        for item in items:
            if text in (str(item["number"]), item["concept_id"].lower(), f"concept_{item['number']:02d}"):
                return item
        match = re.fullmatch(r"(?:c|concept_?)?0*(\d+)", text)
        if match:
            for item in items:
                if item["number"] == int(match.group(1)):
                    return item
        raise EditError(f"unknown thumbnail '{ref}' (choose 1-{len(items)})")

    def _current_path(self, concept: dict[str, Any], number: int) -> Path:
        rendered = (concept.get("render") or {}).get("path")
        return self.out / rendered if rendered else self.thumbs / f"concept_{number:02d}.png"

    def _versions(self, cid: str) -> list[Path]:
        def number(path: Path) -> int:
            found = re.search(r"_v(\d+)$", path.stem)
            return int(found.group(1)) if found else 0

        return sorted(self.edits_dir.glob(f"{cid}_v*.png"), key=number)

    def _history(self) -> dict[str, Any]:
        try:
            data = json.loads((self.out / EDITS_FILE).read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("edits"), list):
                data.setdefault("current", {})
                return data
        except (OSError, ValueError):
            pass
        return {"format": "recut_thumbnail_edits", "version": EDIT_VERSION, "edits": [], "current": {}}

    def _save_history(self, history: dict[str, Any]) -> None:
        tmp = self.out / (EDITS_FILE + ".tmp")
        tmp.write_text(json.dumps(history, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.out / EDITS_FILE)

    def _selection(self) -> dict[str, Any] | None:
        try:
            return json.loads((self.out / "selection.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _references(self, cid: str) -> list[tuple[Path, str]]:
        """Original package images of this concept (identity and game references), at most 3."""
        try:
            request = json.loads((self.out / "generation_request.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        package = next((p for p in request.get("packages", []) if p.get("concept_id") == cid), None)
        labels = {"face_main": "original webcam frame of the creator (identity reference)",
                  "face_alt": "alternative webcam frame", "board": "real game frame", "detail": "real card / detail"}
        out: list[tuple[Path, str]] = []
        for image in (package or {}).get("images", []):
            if image.get("kind") == "face_alt":
                continue
            path = self.out / str(image.get("path"))
            if path.is_file():
                out.append((path, labels.get(str(image.get("kind")), "reference")))
        return out[:3]

    # ---- actions ---------------------------------------------------------------------------------------
    def edit_prompt(self, item: dict[str, Any], request: str, references: list[tuple[Path, str]]) -> str:
        pack = self.design_pack(item["concept_id"])
        if pack and pack.get("composition", {}).get("strategy") == "reference_preserving":
            from .design import environment_prompt
            return ("POLISH/EDIT ENVIRONMENT ONLY. Image 1 is the current environment, NOT a flattened thumbnail. "
                    "Never draw a face, person, card or replacement for a protected subject. "
                    "ReCut preserves the original crops and masks and composites them afterwards.\n"
                    + environment_prompt(pack) + "\nUSER ENVIRONMENT REQUEST: " + request.strip()
                    + "\nORIGINAL DESIGN PACK: " + json.dumps(pack, ensure_ascii=False)
                    + "\nAdditional attachments: " + "; ".join(label for _, label in references)
                    + '\nReturn JSON variants with concept_id "' + item["concept_id"] + '" and generated path.')
        lines = [f"EDIT_VERSION: {EDIT_VERSION}", *EDIT_RULES, "",
                 f'CREATOR REQUEST for thumbnail {item["concept_id"]}: "{request.strip()}"']
        if item.get("hook"):
            lines.append(f'Current hook text on the thumbnail: "{item["hook"]}" (keep it unless the request changes it).')
        lines += ["", "ATTACHED IMAGES:", "- image 1: the CURRENT thumbnail to edit"]
        lines += [f"- image {n}: {label}" for n, (_, label) in enumerate(references, start=2)]
        lines += ["", 'Answer: {"variants": [{"concept_id": "' + item["concept_id"] + '", "path": "<generated file '
                  'path if known, else empty>"}]}']
        lines += ["", "LOCAL TECHNICAL WARNINGS: " + "; ".join(item.get("quality_warnings") or [])]
        pack = self.design_pack(item["concept_id"])
        if pack:
            lines += ["", "ORIGINAL DESIGN PACK (preserve its story, references and identity constraints):",
                      json.dumps(pack, ensure_ascii=False)]
            if pack["typography"]["mode"] == "precision":
                lines += ["Remove all headline text from the returned image; preserve the reserved text area. "
                          "ReCut reapplies exact local typography afterwards. Do not create letters/placeholders."]
        return "\n".join(lines)

    def design_pack(self, cid: str) -> dict[str, Any] | None:
        file = self.out / "design_packs" / f"{cid}.json"
        if not file.is_file():
            return None
        return json.loads(file.read_text(encoding="utf-8"))

    def edit(self, ref: str | int, request: str, *, reference_image: str | Path | None = None, polish: bool = False) -> dict[str, Any]:
        from .generate import CodexImageGenerator, GenerationError, normalize_generated

        if not request.strip():
            raise EditError("empty edit request")
        item = self.resolve(ref)
        cid = item["concept_id"]
        current = Path(item["path"])
        if not current.is_file():
            raise EditError(f"{current} does not exist")
        pack = self.design_pack(cid)
        protected = bool(pack and pack.get("composition", {}).get("strategy") == "reference_preserving")
        environment = self.thumbs / "environments" / f"{cid}.png"
        if protected and not environment.is_file():
            raise EditError("Protected environment is missing; cannot safely edit a flattened thumbnail")
        # Validate the optional attachment before changing history or calling the provider.
        attached = None
        if reference_image is not None:
            from .frames import read_image_unchanged, require_cv2

            cv2 = require_cv2()
            try:
                attached = read_image_unchanged(Path(reference_image).expanduser().resolve())
            except (OSError, ValueError, cv2.error) as exc:
                raise EditError("Cannot read the attached reference image.") from exc
            if attached is None:
                raise EditError("Cannot read the attached reference image.")
        self.edits_dir.mkdir(parents=True, exist_ok=True)
        versions = self._versions(cid)
        if not versions:  # keep the original as v0 before the first edit
            shutil.copyfile(current, self.edits_dir / f"{cid}_v0.png")
            versions = self._versions(cid)
        generator = self._generator or CodexImageGenerator(timeout_s=self.timeout_s)
        if not generator.available():
            raise EditError("codex CLI not found: thumbnail edits need the image model")
        references = [] if protected else self._references(cid)
        attachment = None
        if attached is not None:
            from .frames import write_image

            # Keep an independent, lossless copy (including transparency) inside this run.
            attachment = self.edits_dir / "references" / f"{cid}_{uuid.uuid4().hex}.png"
            write_image(attachment, attached)
            references.insert(0, (attachment, "creator-attached reference: use it as directed in the edit request; "
                                  "do not replace the current thumbnail with it"))
        prompt = self.edit_prompt(item, request, references)
        if polish:
            prompt = ("POLISH SELECTED: refine lighting, hierarchy and integration selectively; "
                      "preserve the established concept unless explicitly instructed.\n" + prompt)
        pack = self.design_pack(cid)
        if not versions:
            raise EditError("Original version not saved")
        self._save_design_version(cid, 0, pack, preserve=True)
        self.progress(f"editing {cid}: 1 image-model call (the thumbnail and {len(references)} reference images are "
                      "sent to the codex CLI provider)")
        started = time.perf_counter()
        from .cache import PackagingCache, stable_key
        import hashlib
        cache = PackagingCache()
        inputs = [environment if protected else current] + [p for p, _ in references]
        cache_key = stable_key("polish_v4" if polish else "edit_v4", prompt,
                               [hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs])
        reused = self.edits_dir / "raw" / f"{cid}_cached.png"
        cache_hit = cache.load_file("generated", cache_key, ".png", reused)
        try:
            raw = {cid: reused} if cache_hit else generator.generate(prompt, inputs, [cid], self.edits_dir / "raw")
        except GenerationError as error:
            raise EditError(f"image model failed ({error.code}): {error.detail}") from error
        if not cache_hit and cid in raw:
            cache.store_file("generated", cache_key, ".png", raw[cid])
        if cid not in raw:
            raise EditError("the image model returned no image")
        version = len(versions)
        target = self.edits_dir / f"{cid}_v{version}.png"
        if normalize_generated(raw[cid], target, size=(1920, 1080) if pack else None) is None:
            raise EditError("the generated image cannot be read")
        if pack:
            if protected:
                from .compositing import composite
                shutil.copyfile(target, environment)
                composite(environment, target, pack, self.out, cache=cache)
            base = self.edits_dir / "bases" / f"{cid}_v{version}.png"
            base.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(target, base)
            if pack["typography"]["mode"] == "precision":
                from .typography import compose
                compose(base, target, pack["typography"])
            saved_base = self.thumbs / "bases" / f"{cid}.png"
            saved_base.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(base, saved_base)
            self._save_design_version(cid, version, pack)
        shutil.copyfile(target, current)
        history = self._history()
        entry = {"concept_id": cid, "version": version, "request": request.strip(), "path": str(target.relative_to(self.out)),
                 "time": time.strftime("%Y-%m-%dT%H:%M:%S"), "runtime_s": round(time.perf_counter() - started, 1),
                 "tokens_used": (getattr(generator, "meta", None) or {}).get("tokens_used")}
        if attachment is not None:
            entry["reference_image"] = attachment.relative_to(self.out).as_posix()
        entry["operation"] = "polish" if polish else "image_edit"
        entry["cli_invocations"] = 0 if cache_hit else 1
        entry["cache_hit"] = cache_hit
        entry["image_generation_calls_observed"] = None
        history["edits"].append(entry)
        history["current"][cid] = version
        self._save_history(history)
        self.refresh_overview()
        return {**entry, "current": str(current)}

    def _store_pack(self, cid: str, pack: dict[str, Any]) -> None:
        from analysis.gameplay.cache import atomic_write_json
        atomic_write_json(self.out / "design_packs" / f"{cid}.json", pack)
        for c in self._concepts["concepts"]:
            if c["concept_id"] == cid:
                c["design_pack"] = pack
                c["thumbnail_text"] = pack["typography"]["text"]
        atomic_write_json(self.out / "thumbnail_concepts.json", self._concepts)

    def _save_design_version(self, cid: str, version: int, pack: dict[str, Any] | None,
                             *, preserve: bool = False) -> None:
        if not pack:
            return
        from analysis.gameplay.cache import atomic_write_json
        target = self.edits_dir / "designs" / f"{cid}_v{version}.json"
        base = self.thumbs / "bases" / f"{cid}.png"
        copy = self.edits_dir / "bases" / f"{cid}_v{version}.png"
        if not preserve or not target.exists():
            atomic_write_json(target, pack)
        if base.is_file() and (not preserve or not copy.exists()):
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(base, copy)
        environment = self.thumbs / "environments" / f"{cid}.png"
        saved_env = self.edits_dir / "environments" / f"{cid}_v{version}.png"
        if environment.is_file() and (not preserve or not saved_env.exists()):
            saved_env.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(environment, saved_env)

    def typography(self, ref: str | int, *, text: str, font: str | None = None,
                   color: str | None = None, max_px: float | None = None,
                   position: str | None = None, preset: str | None = None) -> dict[str, Any]:
        """No agent or image model: recompose from this version's clean image base."""
        import copy
        from .typography import compose, preview_and_check
        from .cache import PackagingCache
        item = self.resolve(ref)
        cid = item["concept_id"]
        original = self.design_pack(cid)
        if not original or original["typography"]["mode"] != "precision":
            raise EditError("Local text editing requires a V4 Precision Typography base")
        pack = copy.deepcopy(original)
        pack["typography"]["text"] = text
        if preset:
            from .typography import apply_preset
            pack["typography"] = apply_preset(pack["typography"], preset)
        if pack["typography"].get("locked"):
            pack["direction"]["thumbnail_hook"] = text
        if font:
            pack["typography"]["fonts"] = [font] + [f for f in pack["typography"]["fonts"] if f != font]
        if color:
            from PySide6.QtGui import QColor
            if not QColor(color).isValid():
                raise EditError("Invalid text colour")
            if color != pack["typography"].get("color"):
                pack["typography"].pop("gradient", None)  # explicit custom fill overrides the preset gradient
            pack["typography"]["color"] = color
        if max_px is not None:
            if not 24 <= max_px <= 400:
                raise EditError("Font size must be 24–400 pixels")
            pack["typography"]["max_px"] = float(max_px)
        if position is not None:
            from .design import TEXT_BOXES
            if position not in TEXT_BOXES:
                raise EditError("Unknown text position")
            pack["typography"].update(position=position, box=TEXT_BOXES[position])
        base = self.thumbs / "bases" / f"{cid}.png"
        if not base.is_file():
            raise EditError("Text-free image base is missing")
        self.edits_dir.mkdir(parents=True, exist_ok=True)
        versions = self._versions(cid)
        if not versions:
            shutil.copyfile(item["path"], self.edits_dir / f"{cid}_v0.png")
            versions = self._versions(cid)
        self._save_design_version(cid, 0, original, preserve=True)
        if position and pack.get("composition"):
            from .compositing import layout_layers, composite
            layout_layers(pack)
            composite(self.thumbs / "environments" / f"{cid}.png", base, pack, self.out)
        version = len(versions)
        target = self.edits_dir / f"{cid}_v{version}.png"
        started = time.perf_counter()
        meta = compose(base, target, pack["typography"], cache=PackagingCache())
        shutil.copyfile(target, item["path"])
        self._save_design_version(cid, version, pack)
        self._store_pack(cid, pack)
        preview_and_check(Path(item["path"]), self.thumbs / "previews" / f"{cid}_160.png")
        entry = {"concept_id": cid, "version": version, "request": "Local typography",
                 "operation": "typography", "path": str(target.relative_to(self.out)),
                 "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
                 "runtime_s": round(time.perf_counter() - started, 3),
                 "cli_invocations": 0, "image_generation_calls_observed": 0, "typography": meta}
        history = self._history()
        history["edits"].append(entry)
        history["current"][cid] = version
        self._save_history(history)
        self.refresh_overview()
        return {**entry, "current": item["path"]}

    def polish(self, ref: str | int, request: str, *, reference_image: str | Path | None = None) -> dict[str, Any]:
        return self.edit(ref, request, reference_image=reference_image, polish=True)

    def revert(self, ref: str | int, version: int) -> dict[str, Any]:
        item = self.resolve(ref)
        cid = item["concept_id"]
        source = self.edits_dir / f"{cid}_v{int(version)}.png"
        if not source.is_file():
            raise EditError(f"{cid} has no version {version}")
        shutil.copyfile(source, Path(item["path"]))
        saved_pack = self.edits_dir / "designs" / f"{cid}_v{int(version)}.json"
        saved_base = self.edits_dir / "bases" / f"{cid}_v{int(version)}.png"
        if saved_pack.is_file():
            self._store_pack(cid, json.loads(saved_pack.read_text(encoding="utf-8")))
        if saved_base.is_file():
            shutil.copyfile(saved_base, self.thumbs / "bases" / f"{cid}.png")
        saved_env = self.edits_dir / "environments" / f"{cid}_v{int(version)}.png"
        if saved_env.is_file():
            shutil.copyfile(saved_env, self.thumbs / "environments" / f"{cid}.png")
        history = self._history()
        history["current"][cid] = int(version)
        self._save_history(history)
        self.refresh_overview()
        return {"concept_id": cid, "version": int(version), "current": item["path"]}

    def approve(self, ref: str | int, *, creator: str | None = None) -> dict[str, Any]:
        item = self.resolve(ref)
        cid = item["concept_id"]
        final = self.thumbs / "thumbnail_final.png"
        shutil.copyfile(Path(item["path"]), final)
        selection = self._selection() or {"format": "recut_packaging_selection"}
        previous = self.out / "selection.json"
        if previous.exists():  # keep the previous choice
            history = self.out / "history" / time.strftime("%Y%m%d-%H%M%S")
            history.mkdir(parents=True, exist_ok=True)
            (history / "selection.json").write_bytes(previous.read_bytes())
        version = self._history()["current"].get(cid, 0)
        concept: dict[str, Any] = next((c for c in self._concepts.get("concepts", []) if c.get("concept_id") == cid),
                                       {})
        selection.update(selected_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
                         thumbnail={"concept_id": cid, "number": item["number"], "version": version,
                                    "path": str(final.relative_to(self.out)), "title": concept.get("title"),
                                    "hook": concept.get("thumbnail_text"), "role": concept.get("role")})
        tmp = self.out / "selection.json.tmp"
        tmp.write_text(json.dumps(selection, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(previous)
        stored = None
        creator = creator or self._creator()
        if creator:
            from .style import CreatorStyleStore

            edits = [e["request"] for e in self._history()["edits"] if e.get("concept_id") == cid]
            entry = {"action": "approve", "kind": "thumbnail", "id": cid, "run": self._run_created(),
                     "value": f"{concept.get('thumbnail_text', '')} ({concept.get('role') or concept.get('treatment')})",
                     "note": ("edited: " + " | ".join(edits)) if edits else None}
            try:
                stored = CreatorStyleStore().record_feedback(creator, [entry])["revision"]
            except (OSError, ValueError, KeyError):
                stored = None
        return {"concept_id": cid, "version": version, "final": str(final), "creator": creator,
                "profile_revision": stored}

    def _creator(self) -> str | None:
        try:
            report = json.loads((self.out / "packaging_report.json").read_text(encoding="utf-8"))
            return (report.get("settings") or {}).get("creator") or None
        except (OSError, ValueError):
            return None

    def _run_created(self) -> str | None:
        try:
            return json.loads((self.out / "packaging_report.json").read_text(encoding="utf-8")).get("created")
        except (OSError, ValueError):
            return None

    def refresh_overview(self) -> None:
        """overview.jpg / overview_320.jpg from the current thumbnails (labels show the version)."""
        import cv2
        import numpy as np

        from .frames import _ascii, read_image, write_image

        cells, small = [], []
        for item in self.concepts():
            image = read_image(Path(item["path"])) if item["exists"] else None
            if image is None:
                continue
            cell = cv2.resize(image, (640, 360), interpolation=cv2.INTER_AREA)
            band = np.zeros((40, 640, 3), np.uint8)
            label = f"{item['number']}: {item['title']}" + (f"  [v{item['current_version']}]" if item["current_version"] else "")
            cv2.putText(band, _ascii(label)[:70], (8, 27), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1,
                        cv2.LINE_AA)
            cells.append(np.vstack([cell, band]))
            small.append(cv2.resize(image, (320, 180), interpolation=cv2.INTER_AREA))
        if not cells:
            return
        rows = [np.hstack(cells[i:i + 2] + [np.zeros_like(cells[0])] * (2 - len(cells[i:i + 2])))
                for i in range(0, len(cells), 2)]
        write_image(self.thumbs / "overview.jpg", np.vstack(rows), quality=90)
        write_image(self.thumbs / "overview_320.jpg", np.hstack(small), quality=92)


def open_in_file_manager(path: str | Path) -> bool:
    """Open a folder/file with the system viewer (Windows Explorer, macOS open, xdg-open)."""
    import os
    import subprocess
    import sys

    target = str(Path(path))
    try:
        if sys.platform == "win32":
            os.startfile(target)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", target])
        else:
            subprocess.Popen(["xdg-open", target])
        return True
    except (OSError, AttributeError):
        return False
