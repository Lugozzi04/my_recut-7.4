# Thumbnail V4.1 — visual recovery and protected compositing

The later [Hearthstone composition correction](THUMBNAIL_HEARTHSTONE_COMPOSITION.md)
supersedes the environment-only default and raw-webcam fallback described below.
The remaining sections document the original V4.1 implementation and its audit.

V4.1 extends the existing Packaging pipeline. It does not change source selection,
EditMap, detection, Whisper, highlights, video export or provider integration.

## Audit: confirmed causes versus unverified hypotheses

Read-only inspection covered the existing builders, HEAD's earlier generation
builder, and real run artifacts dated 2026-10-13, 2026-10-14 and 2026-10-16.
The older and newer successful runs concern different content. They are not a
controlled artistic A/B comparison, and the model is not explicitly pinned in
the recent report (`model: null`). No provider/model-quality conclusion is justified.

Confirmed in the successful V4 run of 2026-10-16 01:02:07:

- All four final thumbnails report `engine=generated`. They are not local fallbacks.
- The C2 raw generation already redraws the supplied Déjà Vu card: the name/rules
  disappear and its artwork/frame become a new illustration. The original K01
  source crop still contains the real card. Final local typography is not causing
  that change. C3's face is also generated rather than copied from W028.
- Generated mode explicitly skipped local subject extraction. It delegated face
  isolation and reconstruction to the image model. The finish step replaced the
  local draft with the generated picture and never restored original card/face pixels.
- All four final headlines use Impact and yellow. Anton was unavailable. Planner
  requests included descriptive strings such as `Archivo Black, domanda su due
  righe` and `Blu scuro e bianco con accento giallo`; exact-string preset lookup
  failed, leaving the same default font order/colour.
- The earlier generation builder used rotating composition/text/grade recipes.
  V4 still recorded their IDs, but its Design Pack branch did not consume them.
  V4 Precision supplied solid fill, one outline and shadow, with little treatment variety.
- One reference card crop is an intentionally generous source window, designed
  for an image model to isolate the card. That is not an automatically reliable
  card alpha mask. Simply compositing it must retain its surrounding context or
  warn, rather than pretend the surrounding UI has been cleanly removed.
- Portfolio validation accepted narrative/treatment differences while the final
  typography could still collapse to one colour/font. Different IDs did not ensure
  different visual treatments.

Not proven: universal V3 superiority, which model made the difference, measured
CTR/clickability, automatic semantic correctness, or reduced need for polish.
The older pipeline was also full-generative and did not guarantee authenticity.

## Implemented path

Creative Direction has a persistent `generation_strategy` selector:

- `reference_preserving` (opt-in since gen v2.1): generate environment only, then composite
  authentic selected subjects using the existing SceneRenderer.
- `full_generative` (default again, see [gen v2.1](THUMBNAIL_GEN_V2_1.md)): the image model
  composes the finished thumbnail with the 14 Oct `thumbnail_gen_v2` prompt.

V3/V4 saved runs without a protected composition continue through their old
editing/finalization path. Reopening them does not silently insert new layers.
Regenerating with V4.1 can explicitly choose either strategy.

Sequence: existing evidence and planning → resolve card readings → selected webcam
segmentation → Design Packs → deterministic preflight/diversification → environment
generation → authentic layers → exact local typography where selected → previews/checks.
The Local render engine retains its existing authentic scene graph and quality fixes;
it does not acquire an unnecessary generated-environment composition path.

Only the selected webcam crops are segmented, with already-decoded burst frames
as background plates and the existing subject cache/GrabCut checks. No face
enhancer, ML download, new analysis or additional planning call is introduced.
If the existing mask is not reliable, the original webcam frame is retained with
an explicit warning. Missing/poor cards become real gameplay; absent usable
subjects stop the concept before image generation. No synthetic card/face fallback.

Protected originals and masks are content-hashed. Their source indexes, source IDs,
timestamps and game IDs remain attached. Card/face content is not colour-graded
or reconstructed. Aspect-preserving scale, external shadows and controlled rim
light are allowed; cover-cropping is forbidden. Upscaling is bounded at 2.5× and
does not create authentic detail. At most two factual visual subjects are composed.

Board/gameplay references are `guided`, but this implementation also composites
their selected real pixels locally. The generative layer is an environment, not
a new factual board. Protected photographs/crops are not attached to background
generation; creative style/layout references may still be supplied explicitly.

## Design Pack protection example

```json
{
  "generation_strategy": "reference_preserving",
  "composition": {
    "version": "protected_composite_v41",
    "strategy": "reference_preserving",
    "family": "card_dominance",
    "reserved_boxes": [[0.53, 0.08, 0.42, 0.84]],
    "layers": [{
      "asset_id": "K01",
      "original_asset_id": "K01",
      "protection": "locked",
      "path": "cards/K01_F021.png",
      "sha256": "<digest of original source asset>",
      "mask": null,
      "source_id": "<output's real source ID>",
      "source_time": 9501.783,
      "type": "image",
      "z": 10,
      "rotation": 0,
      "brightness": 0,
      "contrast": 1,
      "saturation": 1,
      "clip": "none"
    }]
  }
}
```

A webcam subject has `identity_protected`, an authenticated BGRA cutout and a
separate mask file/hash. An unreliable mask records `fallback: original_frame`.
No segmentation of a factual card is claimed when only its context window is available.

## Generation prompt example (same actual builder)

```text
Mission V4.1: generate ONLY a 16:9 1920x1080 environment layer for a YouTube gaming composition.
Protected content: authentic Hearthstone cards, gameplay and creator photographs
are composited locally AFTER this generation. Do not draw people, faces, cards,
heroes, minions, card-shaped placeholders or HUD.
Composition: card_dominance; reserved subject boxes [x,y,w,h]=[[0.53, 0.08, 0.42, 0.84]].
Keep these areas quiet and unobstructed, never fill them with substitute subjects.
Generated environment: dark directional spotlight and warm edge light;
one coherent subdued backdrop, controlled contrast and light, no decorative clutter.
Typography: NO TEXT, LETTERS OR PLACEHOLDERS. Keep the reserved text box clear.
```

Full-generative packs use the full-image prompt, not this background-only instruction.
The existing CLI batch still requests one image-generation operation per variant;
one CLI invocation is not reported as one observed generation operation.

## Portfolio and hooks

Planning now asks for short curiosity/decision/contrast/consequence hooks, normally
2–4 words, complementary to the YouTube title. It does not insert a fixed list
of recurring hooks. Empty hooks and literal locks remain supported.

`portfolio_preflight.json` records family/layout, dominant subject, face presence
and size, element count, hook/type, text position, typography and accent. Repeated
treatments and text positions are diversified deterministically before generation,
including corresponding subject reservations. Hooks/facts are never rewritten by
this correction. Repeated locked hooks are allowed with an explicit note.

Families: Card Dominance, Reaction Impact, Gameplay Clutch and Big Statement.
Insufficient material can require repeated families; position, hierarchy, treatment
and environment still vary. No claim of guaranteed aesthetic diversity is made.
Existing semantic portfolio validators and the existing corrective attempt remain.

## Typography

Local presets:

| Preset | Fill | Separation |
|---|---|---|
| White Impact | white/silver gradient | dark outlines, directional shadow, light depth |
| Gold Impact | cream/yellow/gold gradient | dark/gold outlines, moderate extrusion |
| Danger Red | orange/red gradient | dark red outline, depth/shadow |
| Energy Cyan | white/cyan/blue gradient | blue outlines, restrained coloured outer edge |

Small controlled tilt, multilevel outline, adaptive wrapping and margins are
rendered locally with the existing Qt glyph paths. No font download is performed.
Free-form old font requests are matched to known font names. Missing preferred
fonts use actual configured installed fallbacks, varying those when available;
the requested and actual font are recorded. A system with one usable family
cannot magically supply four different fonts.

Use the thumbnail dialog's Treatment selector and Apply local text — no AI to
change text/font/preset/colour/size/position. A custom fill can override the
preset gradient. Moving the text also repositions protected layers from the saved
environment, without a provider call. Locked Unicode text remains exact.

## Quality, cache, polish and history

- Final 1920×1080 and 160×90 previews remain.
- Protected file/mask hashes, layer order, actual drawn geometry, clipping and
  source provenance are checked. Reports expose the compositing metadata.
- These checks certify use of the recorded pixels, not the truth of a card name,
  facial recognition, generated environment contents, artistic merit or CTR.
- Background cache uses its actual prompt/reference inputs; original masks/crop
  bytes belong to the independent existing `render` namespace for compositing.
- Typography has its own render key. Changes do not invalidate Whisper/RMS/
  frames/detection. Existing cache services are reused, not replaced/migrated.
- Background-only Polish receives the saved environment, instructions and creative
  edit attachment. It does not receive the flattened authentic subjects for redrawing.
  The same hashed local layers are recomposited, then Precision is reapplied.
- Original/edited images, Design Packs, clean composite bases and environments are
  versioned. Revert restores the matching environment/base/design.
- Full-generative legacy editing remains available on full-generative/old packs.
- Optional creator identity photos are separately owned by CreatorStyleStore and
  used only for full-generative references. They are never gameplay proof and are
  not sent to background-only generation. GUI: Set creator identity photos…

## Limits and manual comparison

No real image-generation or paid-provider call was made for implementation/tests.
No new VOD analysis was run. This is verified technical preservation, not proof
of improved first-generation artistic quality.

Prompt instructions cannot certify that a generative environment contains no
stray subject/lettering. Withholding protected photos reduces that invitation;
the user must still inspect the environment/final. AI Typography remains semantic
model output. Precision guarantees its drawing input, not OCR validation of a base.

Small/context-heavy card crops remain small/context-heavy. A reliable universal
card alpha mask or official artwork lookup is not introduced. Failed webcam
segmentation deliberately retains the real original frame rather than inventing
face detail. These fallbacks can be less polished; warnings explain them.

To try: restart ReCut → Thumbnails → choose the exact export output/plan → expand
Creative Direction → Image strategy: Reference preserving. Generate normally.
Inspect the four previews, warnings, Design Packs and protected source layers.
Choose Full generative for rollback/comparison. Use the same output, segments,
direction/profile and provider configuration; base evidence cache is reused.
Pin the same provider model/configuration externally if a controlled comparison
requires it. Do not compare unrelated VODs as if they were an A/B test.

## Changed files

Production: `analysis/packaging/{assets,cards,compositing,design,direction,edit,generate,
pipeline,pipeline_v3,portfolio,style,typography}.py`,
`ui/thumbnail_direction.py`, `ui/thumbnails_page.py`.
`compositing.py` is the only new production module: a small adapter over existing
SceneRenderer/subject extraction/cache, not a second Packaging pipeline.

Tests: new `tests/test_thumbnail_v41.py`; frozen tests/artifacts are not changed.
Documentation/examples: this guide and `docs/thumbnail_v41_examples/`.

The card metadata also now retains the actual neighbour-frame timestamp when the
existing refinement selects a sharper crop. Its manifest edited time is mapped
from that source time. The neighbour-search policy itself is unchanged.

## Verification (2026-10-16)

- Scoped Packaging/V3/V4/card-fallback/output/class-workflow regression: **223 passed,
  zero failed/skipped, 238.632 s**. Providers, ASR and samplers use existing fakes.
- Final targeted checks after provenance/telemetry guards: **30 passed**, 28.674 s
  (overlap the regression suite; counts must not be added as distinct tests).
- Two last integration rechecks passed after restricting new segmentation to
  Generated and correcting actual-composition report metadata.
- Ruff passed on the touched production modules and new tests.
- Mypy `--follow-imports=silent`: **14 production files**, no issues.
- `main.py --smoke-test`: offscreen, isolated config/cache/data, exit 0.
- Additional offscreen GUI smoke: strategy roundtrip, four treatment choices and
  exact local text edit, zero providers, exit 0.
- Four synthetic local compositions were rendered at 1920×1080; the 160×90 grid
  was visually inspected. It verifies treatment/geometry/text, not AI artistic quality.
- Same-source/edit two-strategy fake comparison verifies transcript/frame cache hits
  and no second ASR invocation. This is not a real-model visual benchmark.
- Git whitespace check clean (existing line-ending warnings only).
- No real AI/provider call, VOD analysis, model/font download or commit.

Generation reports now identify `composition_source=design_packs` and the actual
family/layout. Unused old recipe IDs are labelled legacy hints, not claimed as
the composition used. Planning CLI invocations include corrective attempts in
their own count, while existing task counts remain for compatibility.

Examples: [preview](thumbnail_v41_examples/precision_preservation_preview.png),
[actual prompt builder output](thumbnail_v41_examples/generation_prompt_C1.txt),
[Design Pack](thumbnail_v41_examples/design_pack_C1.json).
