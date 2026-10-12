"""V3 stages of PackagingPipeline (engines gen and v3): assets -> opportunities -> portfolio -> scenes.

Mixed into PackagingPipeline; uses its helpers (_timed, _write, _frames, _style_profile, ...).

  _prepare_assets   VisualAssetManifest (frames, webcam crops + cut-outs, board regions) and the
                    Creative Opportunity Matrix; local only, cached per asset.
  _fast_v3          ONE agent call: understanding + PortfolioStrategy + concepts with scene graphs
                    (+ the optional single patch call). Also used by --mode deep (with the story).
  _render_scenes    scene graphs -> PNG through SceneRenderer, V3 quality gate with local fixes
                    (always re-derived from the agent's scene), render cache, portfolio diversity
                    on the final images, PortfolioQualityReport.
  _generate_finals  engine gen: asset packages -> ONE image-model call (codex image_gen) composes the
                    final thumbnails; the local renders are kept as drafts / fallback (generate.py).
"""
from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

from .assets import (VisualAsset, VisualAssetManifest, card_assets, frame_assets, hash_image,
                     region_assets, subject_assets, summary_lines)
from .frames import contact_sheet, read_image, require_cv2, write_image
from .models import FrameRecord

ENGINE_FILES = ("visual_assets.json", "opportunities.json", "portfolio_quality_report.json")
_CODE_DIGEST: str | None = None


def renderer_code_digest() -> str:
    """Hash of the renderer and scene-model source: any drawing change invalidates cached renders."""
    global _CODE_DIGEST
    if _CODE_DIGEST is None:
        import hashlib

        from . import render, scene

        digest = hashlib.sha256()
        for module in (render, scene):
            digest.update(Path(str(module.__file__)).read_bytes())
        _CODE_DIGEST = digest.hexdigest()[:16]
    return _CODE_DIGEST


class V3Stages:
    # attributes provided by PackagingPipeline
    settings: Any
    out: Path
    cache: Any
    warnings: list[str]
    progress: Any
    _bursts: dict[str, list[tuple[float, np.ndarray]]]

    manifest: VisualAssetManifest | None = None
    opportunities: list[Any] = []
    portfolio_report: dict[str, Any] | None = None
    generation_report: dict[str, Any] | None = None
    _prefetched_generation: Any = None  # (finals, ids, report) produced by plan() in the background
    _generator: Any = None

    # ---- assets + opportunities ----------------------------------------------------------------------
    def _prepare_assets(self, evidence: Any) -> None:
        from .opportunities import build_opportunities
        from .subjects import background_plate

        frames = self._frames()  # type: ignore[attr-defined]
        manifest = VisualAssetManifest()
        for asset in frame_assets(frames, evidence.highlights, evidence.games):
            manifest.add(asset)
        plates: dict[str | None, np.ndarray | None] = {}
        # engine gen: the image model cuts the creator out of the ORIGINAL webcam crop (better than GrabCut):
        # no local segmentation; the local fallback render shows the webcam as a framed bubble instead
        if evidence.roi.rect is not None and self.settings.thumbnail_engine != "gen":
            rect = evidence.roi.rect
            by_game: dict[str | None, list[np.ndarray]] = {}
            for h in evidence.highlights:  # plates from frames already decoded for the bursts: no extra decode
                for t, image in getattr(self, "_bursts", {}).get(h.id, []):
                    if evidence.edit_map.to_edited(t, h.source_index) is not None:
                        by_game.setdefault(h.game_id, []).append(rect.crop(image))
            for game_id, crops in by_game.items():
                if crops:
                    h0, w0 = crops[0].shape[:2]
                    cv2 = require_cv2()
                    plates[game_id] = background_plate([cv2.resize(c, (w0, h0)) for c in crops])
            for asset in self._timed("subject_extraction", lambda: subject_assets(  # type: ignore[attr-defined]
                    frames, plates, self.out, evidence.highlights, cache=self.cache)):
                manifest.add(asset)
        exclude = [evidence.roi.rect.to_list()] if evidence.roi.rect is not None else []
        for asset in region_assets(frames, evidence.highlights, self.out, exclude=exclude):
            manifest.add(asset)
        self.manifest = manifest
        self.opportunities = build_opportunities(evidence.highlights, evidence.games, evidence.lines, manifest)
        self._write("visual_assets.json", manifest.to_mapping())  # type: ignore[attr-defined]
        self._write("opportunities.json", {"format": "recut_creative_opportunities",  # type: ignore[attr-defined]
                                           "opportunities": [o.to_mapping() for o in self.opportunities]})
        self._asset_sheets(manifest)
        isolated = len([a for a in manifest.of_type("webcam_subject") if a.quality.get("isolated")])
        self.progress("assets", f"{len(manifest.assets)} assets ({isolated} isolated cut-outs), "
                                f"{len(self.opportunities)} opportunities")

    def _upgrade_assets(self, evidence: Any) -> None:
        """Manifest for a run made before V3: plates from the saved gameplay frames (same game)."""
        from .subjects import background_plate

        frames = self._frames()  # type: ignore[attr-defined]
        manifest = VisualAssetManifest(frame_assets(frames, evidence.highlights, evidence.games))
        if evidence.roi.rect is not None:
            rect = evidence.roi.rect
            game_of = {h.id: h.game_id for h in evidence.highlights}
            by_game: dict[str | None, list[np.ndarray]] = {}
            for record in frames.records.values():
                if record.kind == "gameplay" and record.id in frames.images:
                    by_game.setdefault(game_of.get(record.highlight_id or ""), []).append(
                        rect.crop(frames.images[record.id]))
            cv2 = require_cv2()
            plates: dict[str | None, np.ndarray | None] = {}
            for game_id, crops in by_game.items():
                h0, w0 = crops[0].shape[:2]
                plates[game_id] = background_plate([cv2.resize(c, (w0, h0)) for c in crops])
            for asset in subject_assets(frames, plates, self.out, evidence.highlights, cache=self.cache):
                manifest.add(asset)
        exclude = [evidence.roi.rect.to_list()] if evidence.roi.rect is not None else []
        for asset in region_assets(frames, evidence.highlights, self.out, exclude=exclude):
            manifest.add(asset)
        cards_file = self.out / "cards.json"
        if cards_file.is_file():
            for asset in card_assets(json.loads(cards_file.read_text(encoding="utf-8")).get("cards", []), frames,
                                     evidence.highlights):
                manifest.add(asset)
        self.manifest = manifest
        self._write("visual_assets.json", manifest.to_mapping())  # type: ignore[attr-defined]

    def _asset_sheets(self, manifest: VisualAssetManifest) -> None:
        """Cut-outs (on a neutral backdrop, labelled) and board regions, for the agent and the report."""
        cv2 = require_cv2()
        frames = self._frames()  # type: ignore[attr-defined]
        subjects = manifest.of_type("webcam_subject")
        if subjects:
            images = {}
            records = []
            for a in subjects[:16]:
                rgba = frames.images.get(a.id)
                if rgba is None:
                    continue
                backdrop = np.zeros(rgba.shape[:2] + (3,), np.uint8)
                backdrop[:] = (70, 45, 55)
                alpha = rgba[..., 3:4].astype(np.float32) / 255.0
                images[a.id] = (rgba[..., :3] * alpha + backdrop * (1 - alpha)).astype(np.uint8)
                label = "" if a.quality.get("isolated") else " NOT isolated"
                records.append(FrameRecord(a.id, "webcam", a.source_index, a.source_time, a.edited_time, a.path,
                                           a.width, a.height, notes=label))
            if records:
                contact_sheet(records, images, self.out / "contact_sheets" / "assets_subjects.jpg", columns=4,
                              cell_width=300, title="creator cut-outs (S..) on a neutral backdrop")
        regions = manifest.of_type("board_region")
        if regions:
            records = [FrameRecord(a.id, "region", a.source_index, a.source_time, a.edited_time, a.path, a.width,
                                   a.height) for a in regions[:16] if a.id in frames.images]
            contact_sheet(records, frames.images, self.out / "contact_sheets" / "assets_regions.jpg", columns=4,
                          cell_width=360, title="board regions (B..)")
        del cv2

    # ---- agent call -----------------------------------------------------------------------------------
    def _v3_context(self, evidence: Any) -> dict[str, Any]:
        from .opportunities import STORY_TYPES

        assert self.manifest is not None
        games = {g.game_id: g.to_mapping() for g in evidence.games}
        supported = {str(len([g for g in evidence.games if g.index is not None])),
                     str(len([g for g in evidence.games if g.result == "WIN"]))}
        supported |= {str(g.index) for g in evidence.games if g.index is not None}
        story_types = {o.story_type for o in self.opportunities} | {t for o in self.opportunities
                                                                   for t in o.story_type_alternatives}
        treatments = {t for o in self.opportunities for t in o.possible_treatments} - {"text_led"}
        return dict(n_titles=self.settings.titles, n_concepts=self.settings.thumbnails,
                    highlight_ids={h.id for h in evidence.highlights}, assets=self.manifest.lookup(),
                    transcript={line.id: line.text for line in evidence.lines}, games=games,
                    highlight_games={h.id: h.game_id for h in evidence.highlights}, supported_numbers=supported,
                    edited_duration=evidence.edit_map.duration,
                    opportunities={o.opportunity_id: o.to_mapping() for o in self.opportunities},
                    available_story_types=len(story_types & set(STORY_TYPES)),
                    available_treatments=len(treatments))

    def _v3_images(self, brief: Any, folder: Path, story_types: list[str], *,
                   cutouts: bool = True) -> list[dict[str, Any]]:
        """<= 7 images: selected-frame sheets, cut-outs (not for gen), board regions, one card-reading frame,
        2 style refs."""
        from .style import select_references

        images = [i for i in brief.images if not i.get("grid")][:3]
        sheets = [("assets_subjects.jpg", "creator cut-outs S.. (labels say if NOT isolated)")] if cutouts else []
        for name, description in sheets + [("assets_regions.jpg", "board regions B.. (ids)")]:
            path = self.out / "contact_sheets" / name
            if path.is_file():
                images.append({"path": path, "description": description})
        images += [i for i in brief.images if i.get("grid")][:1]
        profile = self._style_profile()  # type: ignore[attr-defined]
        for ref in select_references(profile, story_types, k=2):
            from .style import CreatorStyleStore

            path = CreatorStyleStore(self.settings.style_dir).reference_path(str(profile["creator_id"]), ref)
            if path.is_file() and path.suffix.lower() in (".png", ".jpg", ".jpeg"):
                copy_path = folder / f"style_{ref['id']}{path.suffix.lower()}"
                cv2 = require_cv2()
                image = read_image(path)
                if image is not None:
                    write_image(copy_path.with_suffix(".jpg"), cv2.resize(image, (640, 360)), quality=85)
                    polarity = ref.get("polarity", "positive")
                    images.append({"path": copy_path.with_suffix(".jpg"),
                                   "description": f"creator {'REFERENCE (inspiration, do not copy)' if polarity == 'positive' else 'NEGATIVE example (avoid this)'} "
                                                  f"{ref['id']} categories {', '.join(ref.get('categories') or []) or '-'}"})
        return images[:7]

    def _fast_v3(self, agent: Any, evidence: Any, *, story: dict[str, Any] | None = None,
                 task: str = "packaging_fast_v3") -> tuple[dict[str, Any], dict[str, Any]]:
        from . import fast
        from .brief import build_brief
        from .opportunities import opportunity_lines
        from .style import prompt_summary

        s = self.settings
        assert self.manifest is not None
        frames = self._frames()  # type: ignore[attr-defined]
        context = self._v3_context(evidence)
        valid = {"highlights": sorted(context["highlight_ids"]), "assets": sorted(context["assets"]),
                 "opportunities": sorted(context["opportunities"]), "games": sorted(context["games"])}
        gen = s.thumbnail_engine == "gen"  # roles + asset packages instead of scene graphs; never a patch call
        limit = 0 if story is not None or gen else s.deepen_limit

        def validator(raw: Any) -> dict[str, Any]:
            if gen:
                return fast.validate_gen(raw, **context)
            return fast.validate_v3(raw, max_deepen=limit, **context)

        def repair(previous: Any, error: str) -> str:
            return fast.repair_prompt(previous, error, valid=valid, n_titles=s.titles, n_concepts=s.thumbnails)

        with tempfile.TemporaryDirectory(prefix="recut-pack-inputs-") as folder:
            brief = self._timed("brief", lambda: build_brief(  # type: ignore[attr-defined]
                games=evidence.games, highlights=evidence.highlights, lines=evidence.lines, frames=frames,
                sheets_dir=self.out / "contact_sheets", work_dir=Path(folder)))
            self.brief_stats = brief.stats()
            story_types = [o.story_type for o in self.opportunities]
            images = self._v3_images(brief, Path(folder), story_types, cutouts=not gen)
            from analysis.hero_groups.packaging import group_context
            style_lines = prompt_summary(self._style_profile(), max_examples=6) + group_context(evidence.payload)  # type: ignore[attr-defined]
            from .generate import manifest_lines

            common = dict(language=s.language, brief=brief,
                          manifest_lines=manifest_lines(self.manifest) if gen else summary_lines(self.manifest),
                          opportunity_lines=opportunity_lines(self.opportunities), n_titles=s.titles,
                          n_concepts=s.thumbnails, style_lines=style_lines, images=images, story=story)
            if gen:
                prompt = fast.build_prompt_gen(**common)
                schema, version, task = fast.RESPONSE_SCHEMA_GEN, fast.PROMPT_VERSION_GEN, task.replace("_v3", "_gen")
            else:
                prompt = fast.build_prompt_v3(max_deepen=limit, **common)
                schema, version = fast.RESPONSE_SCHEMA_V3, fast.PROMPT_VERSION_V3
            result = self._timed("agent_portfolio" if story else "agent_fast", lambda: agent.run(  # type: ignore[attr-defined]
                task, prompt=prompt, schema=schema, system_prompt=fast.SYSTEM_PROMPT,
                prompt_version=version, images=[Path(i["path"]) for i in images],
                validate=validator, repair=repair))
            raw = dict(result.answer)
            requests = raw.get("deepen_requests") or []
            if requests and limit > 0:
                raw = self._fast_patch_v3(agent, evidence, raw, requests[0], Path(folder), context)
        self._resolve_cards_v3(raw, evidence)
        video_story = {**(story or {}), **fast.to_story_v3(raw), "mode": s.mode, "engine": s.thumbnail_engine,
                       "deepened": raw.get("deepened") or (story or {}).get("deepened")}
        for game in evidence.games:
            game.interpretation = next((g for g in video_story.get("games") or [] if g.get("game_id") == game.game_id), {})
        self._write("video_story.json", {"format": "recut_video_story",  # type: ignore[attr-defined]
                                         "prompt_version": fast.PROMPT_VERSION_V3, "provider": s.provider,
                                         "mode": s.mode, "story": video_story})
        self._write("portfolio_strategy.json", {"format": "recut_portfolio_strategy",  # type: ignore[attr-defined]
                                                **(raw.get("portfolio_strategy") or {})})
        self._write("game_contexts.json", {"games": [g.to_mapping() for g in evidence.games]})  # type: ignore[attr-defined]
        analysis = json.loads((self.out / "content_analysis.json").read_text(encoding="utf-8"))
        self._write("content_analysis.json", {**analysis, "frames": [r.to_mapping() for r in frames.records.values()],  # type: ignore[attr-defined]
                                              "brief": self.brief_stats})
        proposals = {k: raw[k] for k in ("concepts", "titles", "preferred_title_id", "preferred_concept_id")}
        proposals["portfolio_strategy"] = raw.get("portfolio_strategy")
        self._write("title_candidates.json", {  # type: ignore[attr-defined]
            "format": "recut_title_candidates", "prompt_version": fast.PROMPT_VERSION_V3, "provider": s.provider,
            "language": s.language, "preferred_title_id": proposals["preferred_title_id"],
            "titles": proposals["titles"]})
        return video_story, proposals

    def _fast_patch_v3(self, agent: Any, evidence: Any, raw: dict[str, Any], request: dict[str, Any], folder: Path,
                       context: dict[str, Any]) -> dict[str, Any]:
        from analysis.boundaries.agents import AgentCallError

        from . import fast

        concept = next(c for c in raw["concepts"] if c["concept_id"] == request["concept_id"])
        images = self._timed("deepen_extraction", lambda: self._deepen(  # type: ignore[attr-defined]
            evidence, [{**request, "reason": request.get("question", "")}], folder))
        frames = self._frames()  # type: ignore[attr-defined]
        assert self.manifest is not None
        for record in frames.records.values():  # new deepen frames become assets
            if record.role == "deepen" and record.id not in self.manifest.by_id():
                kind = "gameplay_frame" if record.kind == "gameplay" else "webcam_original"
                self.manifest.add(VisualAsset(record.id, kind, record.path, record.source_index, record.source_time,
                                              record.edited_time, None, None, record.width, record.height,
                                              quality=dict(record.quality), provenance={"method": "deepen_request"},
                                              uses=["background", "primary"] if kind == "gameplay_frame" else ["secondary"]))
        context = {**context, "assets": self.manifest.lookup()}
        valid = {"assets": sorted(context["assets"]), "highlights": sorted(context["highlight_ids"])}

        def merged(patch: Any) -> dict[str, Any]:
            if not isinstance(patch, dict) or not isinstance(patch.get("concept"), dict):
                raise ValueError("patch must contain a concept object")
            if patch["concept"].get("concept_id") != concept["concept_id"]:
                raise ValueError("patch must keep the same concept_id")
            out = json.loads(json.dumps(raw))
            out["concepts"] = [patch["concept"] if c["concept_id"] == concept["concept_id"] else c for c in out["concepts"]]
            out["card_observations"] = list(out.get("card_observations") or []) + list(patch.get("card_observations") or [])
            out["deepen_requests"] = []
            return fast.validate_v3(out, max_deepen=0, **context)

        prompt = fast.patch_prompt(language=self.settings.language, request=request, concept=concept, images=images,
                                   valid=valid)
        try:
            result = self._timed("agent_patch", lambda: agent.run(  # type: ignore[attr-defined]
                "packaging_fast_patch_v3", prompt=prompt, schema=fast.PATCH_SCHEMA_V3, system_prompt=fast.SYSTEM_PROMPT,
                prompt_version=fast.PATCH_VERSION_V3, images=[Path(i["path"]) for i in images],
                validate=lambda answer: (merged(answer), answer)[1]))
        except AgentCallError as error:
            self.warnings.append(f"deepen patch failed ({error.code}); the original concept is kept")
            return {**raw, "deepen_requests": [], "deepened": {"request": request, "status": f"failed: {error.code}"}}
        out = merged(result.answer)
        out["deepened"] = {"request": request, "answer": result.answer.get("answer"), "status": "applied"}
        return out

    def _resolve_cards_v3(self, raw: dict[str, Any], evidence: Any) -> None:
        """Card readings -> real crops (K..); a reading whose crop is unusable becomes a board_crop of its frame."""
        from .cards import extract_cards

        frames = self._frames()  # type: ignore[attr-defined]
        assert self.manifest is not None
        observations = list(raw.get("card_observations") or [])
        # Readings made on a board REGION (B..) are mapped onto its parent frame: the crop then comes from the
        # full-resolution frame (and its neighbours). Without this they were silently dropped.
        lookup = self.manifest.by_id()
        for item in observations:
            region = lookup.get(str(item.get("frame_id")))
            if region is None or region.type != "board_region" or not region.parent_id or not region.bbox:
                continue
            rx, ry, rw, rh = region.bbox
            x, y, bw, bh = (float(v) for v in item.get("box") or (0, 0, 0, 0))
            item["read_on"] = region.id
            item["frame_id"] = region.parent_id
            item["box"] = [rx + x * rw, ry + y * rh, bw * rw, bh * rh]
        cards = self._timed("cards", lambda: extract_cards(observations, frames, evidence.lines))  # type: ignore[attr-defined]
        self._write("cards.json", {"cards": [c.to_mapping() for c in cards]})  # type: ignore[attr-defined]
        for asset in card_assets([c.to_mapping() for c in cards], frames, evidence.highlights):
            self.manifest.add(asset)
        by_obs = {str(c.provenance.get("obs_id")): c for c in cards}
        obs = {str(o.get("obs_id")): o for o in observations}
        for concept in raw["concepts"]:
            scene = concept["scene"]
            for layer in scene.get("layers", []):
                aid = str(layer.get("asset_id"))
                if layer.get("type") != "card" or aid not in obs:
                    continue
                if aid in by_obs:
                    layer["asset_id"] = by_obs[aid].id
                else:  # no usable crop: show the real board around the reading instead of inventing a card
                    box = obs[aid]["box"]
                    pad = 0.04
                    layer.update(type="board_crop", asset_id=obs[aid]["frame_id"],
                                 crop=[max(0.0, box[0] - pad), max(0.0, box[1] - pad), min(1.0, box[2] + 2 * pad),
                                       min(1.0, box[3] + 2 * pad)])
                    self.warnings.append(f"{concept['concept_id']}: card {aid} had no usable crop; board shown instead")
            package = concept.get("package") or {}
            if package.get("detail"):  # gen engine: the package follows the resolved card (or its frame)
                package["detail"] = [by_obs[d].id if d in by_obs else obs[d]["frame_id"] if d in obs else d
                                     for d in package["detail"]]
            if concept.get("primary_subject") in by_obs:
                concept["primary_subject"] = by_obs[concept["primary_subject"]].id
            elif concept.get("primary_subject") in obs:
                concept["primary_subject"] = obs[concept["primary_subject"]]["frame_id"]
        self._write("visual_assets.json", self.manifest.to_mapping())  # type: ignore[attr-defined]

    # ---- rendering -----------------------------------------------------------------------------------
    def _render_scenes(self, proposals: dict[str, Any], evidence: Any) -> list[dict[str, Any]]:
        from . import quality
        from .cache import stable_key
        from .portfolio import PortfolioQualityReport, asset_reuse, evaluate_portfolio
        from .render import RENDERER_VERSION, SCENE_RENDERER_VERSION, SceneRenderer
        from .scene import normalize_scene, scene_assets, scene_from_v2, validate_scene

        frames = self._frames()  # type: ignore[attr-defined]
        if self.manifest is None:  # an older (V2) run: build cut-outs and regions from its saved frames
            self._timed("asset_upgrade", lambda: self._upgrade_assets(evidence))  # type: ignore[attr-defined]
        assert self.manifest is not None
        assets = self.manifest.lookup()
        subject_for = {a.parent_id: a.id for a in self.manifest.of_type("webcam_subject")
                       if a.parent_id and a.quality.get("isolated")}
        profile = self._style_profile()  # type: ignore[attr-defined]
        brand = (profile or {}).get("brand", {}) if profile and profile.get("enabled", True) else {}
        fonts = [str(f) for f in brand.get("fonts", []) or []]
        renderer = SceneRenderer(extra_fonts=[f for f in fonts if Path(f).suffix.lower() in (".ttf", ".otf")])
        mask = [evidence.roi.rect.to_list()] if evidence.roi.rect is not None else []
        sharp = [float(r.quality.get("sharpness", 0.0)) for r in frames.records.values() if r.kind == "gameplay"]
        reference = float(np.median(sharp)) if sharp else 0.0
        results: list[dict[str, Any]] = []
        for concept in proposals["concepts"]:
            if "scene" not in concept:  # a V2 concept: convert its composition plan
                concept["scene"] = scene_from_v2(concept, subject_for)
                concept.setdefault("treatment", "converted_v2")
                concept.setdefault("story_type", "converted_v2")

        digests: dict[str, str | None] = {}

        def file_digest(asset_id: str) -> str | None:
            """Identity of an asset's saved file (stable across runs, unlike re-decoded pixels)."""
            if asset_id not in digests:
                import hashlib

                path = self.out / str((assets.get(asset_id) or {}).get("path") or "")
                digests[asset_id] = hashlib.sha256(path.read_bytes()).hexdigest()[:24] if path.is_file() else                     hash_image(frames.images.get(asset_id))
            return digests[asset_id]

        def alternatives(concept: dict[str, Any], current: str) -> list[str]:
            ids = [r.id for r in frames.records.values() if r.kind == "gameplay" and
                   r.highlight_id in (concept.get("highlight_ids") or []) and r.id != current]
            return sorted(ids, key=lambda f: -float(frames.records[f].quality.get("selection",
                                                                                  frames.records[f].quality.get("sharpness", 0.0))))

        def render_one(n: int, concept: dict[str, Any]) -> dict[str, Any]:
            target = self.out / "thumbnails" / f"concept_{n:02d}.{self.settings.image_format}"
            problems = validate_scene(concept["scene"], assets, where=str(concept["concept_id"]))
            if problems:
                raise ValueError("; ".join(problems[:4]))
            working = copy.deepcopy(concept["scene"])  # fixes always start from the agent's scene
            fixes: list[str] = []
            issues: list[quality.GateIssue] = []
            result: Any = None
            for _ in range(3):
                scene = normalize_scene(working)
                used = scene_assets(scene)
                key = stable_key("render", RENDERER_VERSION, SCENE_RENDERER_VERSION, renderer_code_digest(), scene,
                                 self.settings.image_format, [file_digest(a) for a in used], mask)
                cached = self.cache.load_json("render", key)
                if isinstance(cached, dict) and self.cache.load_file("render", key, Path(target).suffix, target):
                    from .render import SceneResult

                    result = SceneResult(**{**cached, "path": str(target)})
                else:
                    result = renderer.render_scene(scene, frames.images, target, fmt=self.settings.image_format,
                                                   mask_boxes=mask, fonts=[Path(f).stem for f in fonts])
                    self.cache.store_json("render", key, {k: v for k, v in result.to_mapping().items() if k != "path"})
                    self.cache.store_file("render", key, Path(target).suffix, target)
                issues = quality.inspect_scene(result, scene, assets, reference_sharpness=reference)
                fixable = [i for i in issues if i.fix]
                if not fixable:
                    break
                issue = fixable[0]
                options = alternatives(concept, str(working["background"]["asset_id"])) \
                    if issue.fix == "swap_background" else []
                if not quality.apply_scene_fix(working, issue.fix or "", alternatives=options):
                    break
                fixes.append(f"{issue.code}: {issue.fix}")
            assert result is not None
            mapping = result.to_mapping()
            mapping["path"] = str(target.relative_to(self.out))
            mapping["quality"] = {"passed": not issues, "issues": [i.to_mapping() for i in issues], "fixes": fixes}
            concept["scene_rendered"] = normalize_scene(working) if fixes else None
            return mapping

        def render() -> None:
            for n, concept in enumerate(proposals["concepts"], start=1):
                try:
                    results.append({"concept_id": concept["concept_id"], **render_one(n, concept)})
                except (KeyError, ValueError) as exc:
                    results.append({"concept_id": concept["concept_id"], "error": f"{type(exc).__name__}: {exc}"})
                    self.warnings.append(f"{concept['concept_id']} not rendered: {exc}")
        self._timed("thumbnail_render", render)  # type: ignore[attr-defined]
        rendered = [self.out / r["path"] for r in results if "path" in r]
        from .opportunities import STORY_TYPES

        story_types = len({o.story_type for o in self.opportunities} & set(STORY_TYPES)) if self.opportunities else 0
        treatments = len({t for o in self.opportunities for t in o.possible_treatments} - {"text_led"})
        diversity = evaluate_portfolio(proposals["concepts"], assets, available_story_types=story_types or 1,
                                       available_treatments=treatments or 1,
                                       available_opportunities=len(self.opportunities) or 1, rendered=rendered)
        notes = []
        for finding in list(diversity.blocking):
            if finding.code == "near_identical_render":  # visual duplicate: try a local change of background
                j = next(i for i, c in enumerate(proposals["concepts"]) if c["concept_id"] == finding.concepts[1])
                concept = proposals["concepts"][j]
                options = alternatives(concept, str(concept["scene"]["background"]["asset_id"]))
                if options:
                    concept["scene"]["background"]["asset_id"], concept["scene"]["background"]["crop"] = options[0], None
                    results[j] = {"concept_id": concept["concept_id"], **render_one(j + 1, concept)}
                    notes.append(f"{concept['concept_id']}: background changed to {options[0]} (near-identical render)")
        if notes:
            rendered = [self.out / r["path"] for r in results if "path" in r]
            diversity = evaluate_portfolio(proposals["concepts"], assets, available_story_types=story_types or 1,
                                           available_treatments=treatments or 1,
                                           available_opportunities=len(self.opportunities) or 1, rendered=rendered)
        narrative = [f for f in diversity.blocking if f.code != "near_identical_render"]
        if narrative:
            notes.append("the portfolio repeats an idea: regenerate it (packaging --only thumbnails) rather than "
                         "recolouring; no automatic agent call was made")
        report = PortfolioQualityReport(
            [{"concept_id": r["concept_id"], **({"quality": r.get("quality")} if "quality" in r else {"error": r.get("error")})}
             for r in results], diversity, asset_reuse(proposals["concepts"], assets), bool(narrative), notes)
        self.portfolio_report = report.to_mapping()
        self._write("portfolio_quality_report.json", self.portfolio_report)  # type: ignore[attr-defined]
        overview = []
        for concept, item in zip(proposals["concepts"], results):
            image = read_image(self.out / item["path"]) if "path" in item else None
            if image is not None:
                overview.append((f"{concept['concept_id']}: {concept.get('title', '')}", image))
        if overview:
            self._overview(overview)  # type: ignore[attr-defined]
            cv2 = require_cv2()
            small = np.hstack([cv2.resize(image, (320, 180), interpolation=cv2.INTER_AREA) for _, image in overview])
            write_image(self.out / "thumbnails" / "overview_320.jpg", small, quality=92)
        concepts = [{**concept, "render": item} for concept, item in zip(proposals["concepts"], results)]
        from .creative import PROMPT_VERSION_V3

        self._write("thumbnail_concepts.json", {  # type: ignore[attr-defined]
            "format": "recut_thumbnail_concepts", "engine": "v3", "prompt_version": PROMPT_VERSION_V3,
            "thumbnail_engine": self.settings.thumbnail_engine,
            "renderer": SCENE_RENDERER_VERSION, "provider": self.settings.provider,
            "preferred_concept_id": proposals.get("preferred_concept_id"),
            "portfolio_strategy": proposals.get("portfolio_strategy"), "concepts": concepts})
        return results

    # ---- final generation (thumbnail engine "gen") --------------------------------------------------------
    def _style_note(self) -> str:
        profile = self._style_profile()  # type: ignore[attr-defined]
        if not profile or not profile.get("enabled", True):
            return ""
        brand = profile.get("brand", {}) or {}
        parts = []
        if brand.get("colors"):
            parts.append("brand colours " + ", ".join(map(str, brand["colors"][:4])))
        if brand.get("typography"):
            parts.append(f"typography {brand['typography']}")
        if brand.get("webcam_size"):
            parts.append(f"face size {brand['webcam_size']}")
        notes = (profile.get("thumbnail_style") or {}).get("notes") or []
        parts += [str(n) for n in notes[-2:]]
        return "; ".join(parts)[:400]

    def _generate_images(self, proposals: dict[str, Any], evidence: Any
                         ) -> tuple[dict[str, Path], list[str], dict[str, Any]]:
        """The image-model step alone (packages, prompt, cache, ONE generator call): no Qt, safe to run in a
        background thread while the video export renders. Returns (concept -> normalized PNG, ids, report)."""
        from .generate import (HISTORY_FILE, CodexImageGenerator, GenerationError, asset_package,
                               choose_compositions, file_digest, generation_key, generation_prompt,
                               normalize_generated, remember_compositions)

        s = self.settings
        concepts = [c for c in proposals["concepts"] if c.get("package")]
        report: dict[str, Any] = {"engine": "gen", "status": "skipped", "calls_made": 0, "cache_hit": False}
        if not concepts:
            report["detail"] = "the concepts have no asset package (older run): local renders kept"
            return {}, [], report
        assert self.manifest is not None
        frames = self._frames()  # type: ignore[attr-defined]
        mask = [evidence.roi.rect.to_list()] if evidence.roi.rect is not None else []
        thumbs = self.out / "thumbnails"
        packages = asset_package(concepts, self.manifest.lookup(), frames.images, thumbs / "packages", mask_boxes=mask)
        video_key = evidence.edit_map.digest()
        history = Path(self.cache.root) / HISTORY_FILE if getattr(self.cache, "root", None) else None
        compositions = choose_compositions(packages, video_key=video_key, history_path=history)
        prompt, images = generation_prompt(packages, language=s.language, style_note=self._style_note(),
                                           compositions=compositions)
        ids = [p.concept_id for p in packages]
        self._write("generation_request.json", {  # type: ignore[attr-defined]
            "format": "recut_generation_request", "generator": "codex_image_gen", "prompt": prompt,
            "images": [str(p.relative_to(self.out)) for p in images],
            "packages": [p.to_mapping(self.out) for p in packages], "compositions": compositions})
        report.update(images_sent=len(images), image_bytes=sum(p.stat().st_size for p in images),
                      prompt_chars=len(prompt))
        lookup = self.manifest.lookup()
        sources = sorted({(i.asset_id, file_digest(self.out / str((lookup.get(i.asset_id) or {}).get("path") or "")))
                          for package in packages for i in package.images})
        key = generation_key(prompt, [f"{a}:{d}" for a, d in sources])
        generated = thumbs / "generated"
        generated.mkdir(parents=True, exist_ok=True)
        finals: dict[str, Path] = {}
        cached = self.cache.load_json("generated", key) if s.use_cache else None
        if isinstance(cached, dict):
            for cid in cached.get("concepts", []):
                target = generated / f"{cid}.png"
                if cid in ids and self.cache.load_file("generated", f"{key}-{cid}", ".png", target):
                    finals[cid] = target
            if finals:
                report.update(status="done" if len(finals) == len(ids) else "partial", cache_hit=True,
                              **{k: v for k, v in (cached.get("meta") or {}).items() if k in ("runtime_s", "tokens_used")})
        if not finals:
            generator = self._generator or CodexImageGenerator(timeout_s=s.generator_timeout)
            if not generator.available():
                name = getattr(generator, "name", "generator")
                report.update(status="unavailable", detail=f"{name} unavailable (codex CLI not found?): local renders kept")
                self.warnings.append(f"thumbnail generation unavailable ({name}): local renders kept")
            else:
                self.progress("generation", f"1 image-model call for {len(ids)} thumbnails ({len(images)} images); "
                                            "the package images are sent to the codex CLI provider")
                raw: dict[str, Path] = {}
                try:
                    raw = self._timed("thumbnail_generation", lambda: generator.generate(  # type: ignore[attr-defined]
                        prompt, images, ids, generated / "raw"))
                    report["calls_made"] = 1
                except GenerationError as error:
                    report.update(status="failed", calls_made=1, error_code=error.code, detail=error.detail[:300])
                    self.warnings.append(f"thumbnail generation failed ({error.code}): local renders kept")
                report.update({k: v for k, v in (getattr(generator, "meta", None) or {}).items()
                               if k in ("runtime_s", "tokens_used")})
                for cid, path in raw.items():
                    target = generated / f"{cid}.png"
                    size = normalize_generated(path, target)
                    if size is not None:
                        finals[cid] = target
                        report.setdefault("source_sizes", {})[cid] = list(size)
                if finals:
                    report["status"] = "done" if len(finals) == len(ids) else "partial"
                    if s.use_cache:
                        for cid, path in finals.items():
                            self.cache.store_file("generated", f"{key}-{cid}", ".png", path)
                        self.cache.store_json("generated", key, {"concepts": sorted(finals), "meta": {
                            k: report.get(k) for k in ("runtime_s", "tokens_used")}})
        report["compositions"] = {cid: c["recipe"] for cid, c in compositions.items()}
        if finals and history is not None:  # the next video avoids these layouts
            remember_compositions(history, video_key, compositions)
        return finals, ids, report

    def _generate_finals(self, proposals: dict[str, Any], results: list[dict[str, Any]],
                         evidence: Any) -> list[dict[str, Any]]:
        """ONE image-model call composes every thumbnail from its asset package; local renders become drafts
        (thumbnails/local/) and stay the final output for any concept the generator could not produce."""
        import shutil

        prefetched = getattr(self, "_prefetched_generation", None)
        if prefetched is not None:  # already produced in the background during the export
            finals, ids, report = prefetched
            self._prefetched_generation = None
        else:
            finals, ids, report = self._generate_images(proposals, evidence)
        self.generation_report = report
        if not ids:
            return results
        thumbs = self.out / "thumbnails"
        missing = [cid for cid in ids if cid not in finals]
        if finals and missing:
            self.warnings.append(f"no generated image for {', '.join(missing)}: local render kept")
        local_dir = thumbs / "local"
        for concept, item in zip(proposals["concepts"], results):
            cid = str(concept["concept_id"])
            if "path" not in item:
                continue
            image = read_image(finals[cid]) if cid in finals else None
            if image is None:
                item["engine"] = "local"
                continue
            current = self.out / item["path"]
            local_dir.mkdir(parents=True, exist_ok=True)
            draft = local_dir / current.name
            shutil.move(str(current), draft)
            write_image(current, image, quality=95)
            item.update(engine="generated", local_draft=str(draft.relative_to(self.out)),
                        generated=str(finals[cid].relative_to(self.out)), width=1280, height=720)
            item["quality"] = {"passed": True, "issues": [], "fixes": [],
                               "note": "composed by the image model; the local gate checked the draft only"}
        report["generated"] = sorted(finals)
        self._finish_generated(proposals, results)
        return results

    def _finish_generated(self, proposals: dict[str, Any], results: list[dict[str, Any]]) -> None:
        """Overview sheets, near-duplicate check and concept file for the final thumbnails."""
        from .quality import dhash, hamming

        hashes = []
        for concept, item in zip(proposals["concepts"], results):
            image = read_image(self.out / item["path"]) if "path" in item else None
            if image is not None:
                hashes.append((str(concept["concept_id"]), dhash(image)))
        notes = [f"{a} and {b} look nearly identical" for i, (a, ha) in enumerate(hashes)
                 for b, hb in hashes[i + 1:] if hamming(ha, hb) <= 10]
        self.warnings += notes
        if self.portfolio_report is not None:
            self.portfolio_report["final_notes"] = notes
            self.portfolio_report["thumbnails"] = [
                {"concept_id": r["concept_id"], "engine": r.get("engine", "local"),
                 **({"quality": r.get("quality")} if "quality" in r else {"error": r.get("error")})} for r in results]
            self._write("portfolio_quality_report.json", self.portfolio_report)  # type: ignore[attr-defined]
        overview = []
        for concept, item in zip(proposals["concepts"], results):
            image = read_image(self.out / item["path"]) if "path" in item else None
            if image is not None:
                overview.append((f"{concept['concept_id']} [{item.get('engine')}]: {concept.get('title', '')}", image))
        if overview:
            self._overview(overview)  # type: ignore[attr-defined]
            cv2 = require_cv2()
            small = np.hstack([cv2.resize(image, (320, 180), interpolation=cv2.INTER_AREA) for _, image in overview])
            write_image(self.out / "thumbnails" / "overview_320.jpg", small, quality=92)
        concepts_file = self.out / "thumbnail_concepts.json"
        if concepts_file.is_file():
            data = json.loads(concepts_file.read_text(encoding="utf-8"))
            by_id = {r["concept_id"]: r for r in results}
            for concept in data.get("concepts", []):
                if concept.get("concept_id") in by_id:
                    concept["render"] = by_id[concept["concept_id"]]
            data["generation"] = getattr(self, "generation_report", None)
            self._write("thumbnail_concepts.json", data)  # type: ignore[attr-defined]
