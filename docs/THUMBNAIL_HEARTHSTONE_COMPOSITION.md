# Hearthstone composition recovery

This is a correction inside V4/V4.1, not a second pipeline. No real provider call,
image generation, ASR or VOD analysis was used to implement or validate it.

## Confirmed technical causes

The V4.1 `environment_prompt` explicitly requested an environment with no heroes,
minions or HUD. `generation_prompt_v4` withheld all gameplay references along with
the protected cards and photographs. Four generic environment recipes were then
used as the entire canvas. `layout_layers` contained each board screenshot inside
a foreground box, made even smaller when another subject was present. Large text
reservations displaced gameplay. An unreliable webcam mask fell back to a raw
rectangle. Diversity corrected colours and text positions without ensuring
different dominant subject types.

Those paths explain how the reported failure modes were possible. They do not
prove that every historical image took the same path or that a particular model
became worse. No controlled artistic before/after generation was performed.

## Changes in the existing path

1. Planning requests concrete `thumbnail_archetype`, dominant subject kind,
   background strategy, card/face usage, hook category, clutter budget and banned
   patterns. Existing Creative Direction still precedes ranking/planning.
2. The default `background_strategy` is `real_gameplay_derived`. The selected
   package supplies a real frame (or the parent of its board region), with its hash,
   source ID, source time and game ID. No asset from an unrelated class/output is
   borrowed to complete the portfolio.
3. The frame fills the canvas; board-driven concepts need no inset foreground
   screenshot at all. Known webcam-overlay regions use the existing masking helper.
   The final background always derives locally from these real pixels.
   Board-led backgrounds stay sharp; card/face backdrops are modestly darkened
   (0.82x) and blurred (1.2px, 2px for the explicit blurred-depth strategy).
4. The generator receives the masked real backdrop as a gameplay reference, but
   no foreground face/card photograph. Its image contributes only a spatially
   smoothed luminance field, capped at approximately +/-6% brightness. Generated
   objects, faces, cards, HUD or abstract colours cannot replace the factual base.
   This is deliberately conservative; it is not a semantic verification of AI output.
5. Cards marked primary and verified/legible must also meet crop size and sharpness
   checks. One card fills approximately 88% of canvas height in card-led layouts;
   in face+card layouts both subjects are large and separated. Aspect ratio and
   original content remain protected. Scaling is capped at 2.5x and reported.
6. Only a usable isolated creator crop may become a face foreground. Transparent
   padding is removed without changing visible pixels, and original hash/framing
   provenance is recorded. Failed segmentation means omission or a board-led
   alternative, never a raw webcam rectangle or a fabricated face.
7. Typography occupies compact safe areas rather than half the canvas. White
   Impact, Gold Impact, Danger Red and Energy Cyan retain gradients, outlines,
   depth, exact text and installed-font fallbacks. Duplicate treatments/positions
   are diversified. Locked text is never rewritten. New gameplay-anchored
   compositions use Precision Typography; AI Typography remains available in
   Full generative and existing saved runs.
8. A deterministic geometric preflight rejects absent dominant subjects, tiny
   card/face foregrounds, inset gameplay layers, clutter and abstract background
   substitution. Suitable failures fall back to a full board. Scores/penalties
   and bonuses are saved, with no claim to measure beauty, emotion, CTR or OCR.
9. The four concepts use different supported archetypes where selected assets
   permit: `card_led`, `board_led`, `face_board`, `face_card`, `result_payoff`.
   Maximum two use a face; a clean board/result option is retained. A payoff is
   never fabricated merely to fill a category. Insufficient cards/cutouts produce
   an explicit diversity warning instead of invented evidence.

## Design Pack example (excerpt)

```json
{
  "thumbnail_archetype": "card_led",
  "dominant_subject": "card",
  "secondary_subject_kind": "gameplay_board",
  "optional_subject": "",
  "background_strategy": "real_gameplay_derived",
  "gameplay_frame_role": "full_canvas_authentic_backdrop",
  "card_usage": "hero_card_big",
  "face_usage": "none",
  "board_visibility": "recognizable_backdrop",
  "text_need": "optional",
  "hook_style": "question",
  "composition_template": "large_card_right_real_board",
  "clutter_budget": 2,
  "banned_patterns": ["tiny_center_screenshot", "generic_abstract_background", "raw_webcam_box", "too_many_small_elements", "weak_subject_hierarchy", "low_hearthstone_identity"]
}
```

`main_subject`/`secondary_subject` retain their existing meaning of asset IDs;
`dominant_subject` and `secondary_subject_kind` describe their editorial roles.
Actual background/layer provenance, dimensions, `subject_balance`,
`clutter_score`, `gameplay_visibility_score`, `card_legibility_score`,
`face_readability_score` and penalties are persisted after layout. Readability
scores are geometry heuristics, not certified recognition of card text or faces.

Prompt excerpt built from a pack:

> Enhance the attached REAL Hearthstone gameplay backdrop. The actual frame
> remains the full-canvas local base. Preserve recognizable board structure,
> do not shrink it into a screenshot sticker. Protected cards and creator
> cutouts are composited AFTER generation. Do not draw replacement subjects.

## Compatibility, cache and manual check

Use **Thumbnails -> Creative Direction -> Reference preserving**, the default,
then generate from the usual selected export plan or previously exported video.
No new configuration is required. Inspect the four 160x90 previews and warnings.
`design_packs/*.json`, `portfolio_preflight.json`, `generation_request.json` and
the final compositing checks document the actual layout and source provenance.

`Full generative` remains the explicit alternative. Its prompt also requests a
real gameplay-derived composition, but it cannot provide the local fidelity
guarantees of the reference-preserving path. Saved V3/V4/V4.1 packs without the
new `composition.background` field keep their previous render/edit behavior.
Polish and local text edits reuse the saved original background/crops/masks;
the compositor reanchors any model edit to real gameplay before final output.

Only planning/generation/render dependencies change. Transcript, RMS, frame,
EditMap, detector and export caches are unchanged. Planning remains one fast
request with the existing optional correction mechanism. Four image variants
are still requested; this change does not promise fewer provider generations.

## Limits

- Subject isolation and crop quality remain limited by real input resolution.
  No new segmentation, face model, artwork database or download was added.
  An authentic card window can retain surrounding UI; no perfect card mask or
  semantic recognition of every crop is claimed.
- Global webcam ROI is the existing detector's estimate; inaccurate masks need
  manual review. A full source frame may retain other stream overlays.
- Non-16:9 gameplay uses the renderer's existing aspect-fitting behavior.
- Local composition protects the selected factual pixels, not the truth of an
  unverified planner interpretation. Card names, result and story still need review.
- Four distinct archetypes are impossible without suitable selected assets.
- Generative influence is intentionally restricted to mild lighting. Stronger
  full-image reinterpretation remains an explicit alternative.
- Visual appeal and clickability have not been established by real generations
  or audience testing. Only offline geometric, provenance and regression checks
  are claimed.

## Validation (2026-10-16)

- Packaging regression suite: 224 tests, initially 223 passed. Its JPEG attachment
  compatibility check exposed the new PNG backdrop reference. Fixed by retaining
  JPEG attachments and preserving the existing face-first package order; source
  originals and protected final pixels remain lossless.
- Final targeted rerun: 69/69 passed, including that integration regression,
  V4 Creative Direction, protected rendering, editing, cache reuse and legacy runs.
- Final backdrop grading/geometry rerun: 33/33 passed. Across these runs all 226
  distinct tests have a passing latest result; no remaining failure or skip.
- Ruff passed for the five changed implementation modules and two test files.
  Mypy passed for the five implementation modules. Offscreen GUI smoke
  (`main.py --smoke-test`) exited successfully. Synthetic output was inspected.
- Provider/planner/generator/ASR operations in tests are fakes. Zero real AI calls,
  zero real image generations, no full VOD analysis, no commits. Existing frozen
  gameplay artifacts were not modified. The whole repository suite was not run;
  validation was scoped to Packaging and its affected integrations.

Files changed for this correction: `analysis/packaging/design.py`,
`compositing.py`, `generate.py`, `pipeline_v3.py`, `portfolio.py`;
`tests/test_thumbnail_hearthstone_composition.py`, `test_thumbnail_v41.py`;
this report and a supersession note in `docs/THUMBNAIL_V4_1.md`.

## Planning hierarchy recovery

`visual_hierarchy` is advisory asset ordering, not another asset catalogue.
If a planner puts a descriptive label or nonexistent ID in that list, the
existing validator previously rejected the entire run with a generic message.
The schema/prompt now explicitly require selected package IDs. Before strict
validation, an adapter rebuilds only malformed *references in the ordering*
using the valid main subject and the concept's already selected real assets.
No missing asset is invented or borrowed from another output. Invalid required,
optional, forbidden or factual evidence references still fail strict validation;
malformed list structures also remain errors.

Repairs are reported in `design_repairs.json`, run warnings and each affected
Design Pack's `planning_repairs`, including original/rejected values. Errors
which remain now identify the concept, field and offending IDs. This avoids a
provider repair call for a recoverable layout mistake while preserving all
factual checks, analysis caches and existing user overrides.
