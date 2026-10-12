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
        lines = [f"EDIT_VERSION: {EDIT_VERSION}", *EDIT_RULES, "",
                 f'CREATOR REQUEST for thumbnail {item["concept_id"]}: "{request.strip()}"']
        if item.get("hook"):
            lines.append(f'Current hook text on the thumbnail: "{item["hook"]}" (keep it unless the request changes it).')
        lines += ["", "ATTACHED IMAGES:", "- image 1: the CURRENT thumbnail to edit"]
        lines += [f"- image {n}: {label}" for n, (_, label) in enumerate(references, start=2)]
        lines += ["", 'Answer: {"variants": [{"concept_id": "' + item["concept_id"] + '", "path": "<generated file '
                  'path if known, else empty>"}]}']
        return "\n".join(lines)

    def edit(self, ref: str | int, request: str) -> dict[str, Any]:
        from .generate import CodexImageGenerator, GenerationError, normalize_generated

        if not request.strip():
            raise EditError("empty edit request")
        item = self.resolve(ref)
        cid = item["concept_id"]
        current = Path(item["path"])
        if not current.is_file():
            raise EditError(f"{current} does not exist")
        self.edits_dir.mkdir(parents=True, exist_ok=True)
        versions = self._versions(cid)
        if not versions:  # keep the original as v0 before the first edit
            shutil.copyfile(current, self.edits_dir / f"{cid}_v0.png")
            versions = self._versions(cid)
        generator = self._generator or CodexImageGenerator(timeout_s=self.timeout_s)
        if not generator.available():
            raise EditError("codex CLI not found: thumbnail edits need the image model")
        references = self._references(cid)
        prompt = self.edit_prompt(item, request, references)
        self.progress(f"editing {cid}: 1 image-model call (the thumbnail and {len(references)} reference images are "
                      "sent to the codex CLI provider)")
        started = time.perf_counter()
        try:
            raw = generator.generate(prompt, [current] + [p for p, _ in references], [cid], self.edits_dir / "raw")
        except GenerationError as error:
            raise EditError(f"image model failed ({error.code}): {error.detail}") from error
        if cid not in raw:
            raise EditError("the image model returned no image")
        version = len(versions)
        target = self.edits_dir / f"{cid}_v{version}.png"
        if normalize_generated(raw[cid], target) is None:
            raise EditError("the generated image cannot be read")
        shutil.copyfile(target, current)
        history = self._history()
        entry = {"concept_id": cid, "version": version, "request": request.strip(), "path": str(target.relative_to(self.out)),
                 "time": time.strftime("%Y-%m-%dT%H:%M:%S"), "runtime_s": round(time.perf_counter() - started, 1),
                 "tokens_used": (getattr(generator, "meta", None) or {}).get("tokens_used")}
        history["edits"].append(entry)
        history["current"][cid] = version
        self._save_history(history)
        self.refresh_overview()
        return {**entry, "current": str(current)}

    def revert(self, ref: str | int, version: int) -> dict[str, Any]:
        item = self.resolve(ref)
        cid = item["concept_id"]
        source = self.edits_dir / f"{cid}_v{int(version)}.png"
        if not source.is_file():
            raise EditError(f"{cid} has no version {version}")
        shutil.copyfile(source, Path(item["path"]))
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
