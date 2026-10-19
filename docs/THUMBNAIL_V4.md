# Thumbnail Pipeline V4 — implementation and verification

ReCut's existing PackagingPipeline now carries optional Creative Direction from
selection through planning, Design Packs, generation and subsequent editing.
This is an extension of the existing pipeline, not another detector/export path.

## Implemented workflow

1. Thumbnails → Creative Direction: Automatic (default) or Guided; optional title,
   exact-title lock, hook/exact-hook lock, focus, reference role, creator preset
   and Hybrid Auto / Precision / AI typography.
2. Load the existing output EditMap and games. For class export snapshots, intersect
   evidence ranges with exported gameplay BEFORE ASR/RMS/keyframe scans.
   Source indexes and original edited positions survive; Intro does not become
   gameplay evidence or require transcription in that path.
3. Reuse the existing source transcript coverage/cache, RMS and keyframe evidence.
4. Add lexical guided-focus signals to existing multi-signal highlight ranking.
   Coverage and an automatic alternative remain. No separate LLM interprets focus.
   No match → warning and automatic selection. Keyword matches are not combo proof.
5. Existing frame bursts and Haar webcam detector are reused. Up to three distinct
   webcam crops per highlight are labelled primary/alt_1/alt_2. Face size,
   sharpness, exposure, crop resolution and activity remain interpretable proxies,
   never emotion certification. Existing gameplay ranking publishes separate
   reference_quality and thumbnail_quality scores. Card crops retain their
   verified/legible/uncertain identity and gain primary/secondary/avoid graphic use.
   Weak card protagonists fall back to a real source frame.
6. Six planning attachments are chosen by editorial relevance: one summary, up to
   two ranked webcam crops, main gameplay, detail/region and alternate/reference,
   with unused slots redistributed. Early games cannot occupy every image slot.
7. One fast planning invocation (Claude/Codex), producing six title candidates and
   four concepts. No automatic deepen call in fast mode. Existing invalid-output
   repair remains available; it can add a corrective invocation.
8. Validate concept assets/evidence; apply literal user locks; resolve observed cards
   through the existing extraction stage. Save one design_packs/<concept_id>.json.
9. Existing Codex image_generation integration gets a per-concept specification
   inside its usual set request. SceneRenderer still makes local drafts/fallbacks.
   Local render also receives direction and V4 finalization, without image generation.
10. Preserve originals; adapt to 1920×1080 using contain/padding, not destructive
    centre cropping. Request native 16:9 from the generator, but the CLI/tool may
    return another size. Upscaling is reported as adaptation, not authentic detail.
11. Precision overlays exact Unicode text locally on a clean saved base. AI mode
    gets no second local overlay; no-hook concepts get no headline.
12. Save 160×90 previews, technical/text metadata and diversity checks on the FINAL
    thumbnails. Identity/card correctness remain explicitly unverified.
13. Optional Polish Selected uses current image, Design Pack, original references,
    technical warnings and the user's instruction. No ASR/detection/replanning.
    Local text edits use the saved base, with zero provider calls.

## Structures and files

New production files:
- analysis/packaging/direction.py — typed validated CreativeDirection, owned reference
  snapshots, lexical relevance and shared literal-lock adapter.
- analysis/packaging/design.py — V4 planning schema, asset/evidence checks, serializable
  Design Packs, layout/text geometry and per-concept generation prompt.
- analysis/packaging/selection.py — six-attachment editorial selection.
- analysis/packaging/typography.py — existing Qt font/wrap primitives, exact local
  typography, contain normalization, preview and technical checks.
- ui/thumbnail_direction.py — collapsible controls and explicit Design System JSON editor.

Existing production files extended:
- analysis/packaging/pipeline.py and pipeline_v3.py — orchestration, early evidence
  filters, direction/planning/design/finalization, cache/report/history.
- analysis/packaging/generate.py — V4 prompt branch in the existing generator,
  reference allocation, legacy-compatible image normalization.
- analysis/packaging/style.py — default Competitive Gaming profile in CreatorStyleStore.
- analysis/packaging/highlights.py and opportunities.py — guided lexical relevance,
  automatic alternatives, original-webcam availability in Generated opportunities.
- analysis/packaging/cards.py and assets.py — graphic-use metadata and provenance.
- analysis/packaging/creative.py and portfolio.py — explicit locks override only
  stylistic constraints; gameplay evidence validation remains.
- analysis/packaging/render.py — installed Archivo Black/Barlow Condensed font lookup.
- analysis/packaging/edit.py — Design Pack-aware polish, local text edits, separate
  clean-base/design versions and selective revision cache.
- analysis/packaging/outputs.py — direction passed/persisted with each output.
- ui/thumbnails_page.py — direction propagation, per-output persistence, preview,
  local text/font/colour/size/position controls and Polish Selected.
- core/project.py and ui/main_window.py — optional creative_direction project field.

Tests:
- New tests/test_thumbnail_v4.py.
- Existing test_youtube_packaging_gen.py and test_youtube_packaging_v3.py update
  fresh-run resolution/role-policy expectations. Legacy 720p helper tests remain.
- No frozen gameplay tests, templates, detector, recognition, Whisper engine or video
  export algorithm modified by this implementation.

## Design System

Default: Lugozzi — Competitive Gaming. Persisted in the existing store:

config_root()/packaging/creators/lugozzi/profile.json

On Windows without configuration overrides:

%APPDATA%/Auto Cutter/Auto Cutter/packaging/creators/lugozzi/profile.json

Yellow #FFD436, White #FFFFFF, Dark #111420; Red #FF514A and Cyan #27C9FF accents.
Maximum two dominant saturated colours is a generation direction, not a semantic
image analyser. Effects: rim/depth 4, selective glow 3, rays/particles 2, distortion 0.
Layout archetypes: reaction_impact, card_power, clutch_moment, big_statement.
The planner may repeat a role when material warrants it; narrative/composition
diversity checks still apply. Colour alone does not count as diversity.

Edit Design System… exposes its JSON using the existing versioned CreatorStyleStore.
Old explicit creator profiles gain V4 defaults in memory without a surprise revision;
saving explicit changes persists them. Additional profiles can use the existing
packaging-style CLI and appear in the selector on reopening the app.

Fonts: Anton → Archivo Black → Barlow Condensed Black → installed fallback.
No font is downloaded or bundled. Local rendering records the actual resolved
family and warns on fallback. On this machine the synthetic verification resolved
Impact because Anton was unavailable.

Primary licence sources (SIL OFL 1.1):
- https://github.com/googlefonts/AntonFont/blob/main/OFL.txt
- https://github.com/google/fonts/blob/main/ofl/archivoblack/OFL.txt
- https://github.com/google/fonts/blob/main/ofl/barlowcondensed/OFL.txt

Exact family is controllable only for an installed/configured local font. AI
lettering receives a style preference, never an exact-font guarantee.

## Hybrid typography

- Hybrid Auto: ordinary non-empty text defaults to Precision.
- A planner can explicitly choose AI for artistic lettering in Auto.
- Locked hook always forces Precision, even if AI was selected.
- Empty hook means no text.
- Precision: generator asked for no letters/placeholders and a reserved box.
  ReCut fits exact text, preserves case/accents/punctuation, wraps at word boundaries,
  draws outline/shadow and records actual geometry/font.
- AI: generator draws the text; ReCut never adds a second headline.
- Local render uses Precision when AI lettering would otherwise require a generator.
- Click a result → edit local text/font/colour/max size/position → Apply local text —
  no AI. This starts from thumbnails/bases/<id>.png, not the flattened old headline.
- Polish is optional. V4 Precision edits reapply the current local hook after the
  image edit. Clean bases and Design Packs are versioned with the image; revert
  restores all three.

## Real builder examples (synthetic input)

These are actual outputs of the V4 prompt/pack builders, produced with fake
planning/generation and synthetic images. They are NOT evidence of AI image quality.

- [Planning prompt](thumbnail_v4_examples/planning_prompt.txt)
- [Generation prompt](thumbnail_v4_examples/generation_prompt.txt)
- [Design Pack C1](thumbnail_v4_examples/design_pack_C1.json)
- [160×90 typography comparison](thumbnail_v4_examples/precision_preview.png)

Planning retains the existing games/transcript/asset manifest and adds:
CREATIVE DIRECTION V4, DESIGN SYSTEM, literal-lock rules, explicit story confidence,
asset requirements/prohibitions, hierarchy, layout, text position, palette,
lighting/effects and why the concept communicates its story.

Generation uses sections Mission, Story, Asset Roles, Composition, Identity,
Authenticity, Visual Style, Typography, Visual Quality and Negative Constraints.
Original webcam/gameplay and user style/layout/element references have different
roles; a creative attachment never verifies gameplay.

## Persistence and cache

Existing PackagingCache namespaces are reused; no second cache service.
- Base evidence keys do not contain creative focus, font or palette.
- Guided ranking is inexpensive and persisted in the existing highlights/analysis
  artifacts; it is recomputed from cached base evidence.
- Planning keys include actual prompt/schema/image bytes and model/provider/effort.
  Direction and profile enter the planning prompt, not the ASR identity.
- Generation keys contain the V4 generation prompt and source asset digests.
  Precision headline/font/colour are excluded from the text-free-image instructions;
  local recomposition has its own key in the existing render namespace.
- Typography cache depends on clean-base bytes, exact text/style, canvas and available fonts.
- Polish cache depends on current-image/reference bytes and instruction/design.
- Report distinguishes cli_invocations, requested generation variants and
  image_generation_calls_observed. The last is null when the CLI does not expose
  trustworthy tool-call telemetry. One CLI process is not reported as one actual
  image-generation operation.
- Run files, Design Packs, creative references, raw generations and local drafts
  are archived through the existing history mechanism.
- .autocutter saves project direction and each registered output's direction.
  Reopening a run restores creative_direction.json; legacy runs without it still open.

## Limits and compromises

- No real AI generation was run. First-generation artistic quality and reduced
  revision/cost rates are goals to validate manually, not measured outcomes.
- Lexical focus matching cannot understand all combos, card aliases or emotions.
- Six planning images / twelve generation reference images remain bounded;
  the generator is told which requested references were omitted by its budget.
- GUI attaches one creative reference at a time; the structured model supports three.
- Design System editing is JSON, not a new graphical editor.
- Local readability/contrast and image hashes are proxies. They do not certify
  creator identity, card details, semantic truth, AI spelling or an uncluttered scene.
- Exact local text is guaranteed by the drawing input, but there is no OCR proof
  that an image model obeyed the instruction to leave its base completely text-free.
  Review the base/final if stray AI lettering appears.
- Existing Packaging RMS cache rounds dB to two decimals. In a synthetic cold/warm
  comparison two plateau peaks moved 0.05–0.3s and four new burst requests were
  needed; ASR/audio/visual evidence and identical frame requests were still reused.
  This pre-existing audio behavior was not changed.
- Native generator resolution is requested, not guaranteed. Padding avoids clipping;
  lower-resolution output is explicitly labelled as upscaled. Raw originals are kept.
- Invalid structured planning may still trigger the existing corrective attempt.
  There is no new compulsory deepen/polish call.
- Video export, detection, class recognition and the Whisper engine are unchanged.

## Manual GUI trial

1. Restart ReCut; choose an exported class video (or Selected export plan).
2. Expand Creative Direction. Leave Automatic for a fully automatic run, or choose
   Guided and enter focus/title/hook. Use exact-text lock to force Precision.
3. Keep Lugozzi Competitive Gaming or edit/select a saved profile. Optional reference:
   select its role, then attach it.
4. Generate titles and thumbnails with your configured CLI.
5. Compare the four results and the 160×90 preview; read warnings.
6. Click a candidate: local controls change exact text/font/colour/size/position with
   no AI. Use Polish Selected only when a visual refinement is wanted.
7. Confirm the chosen thumbnail. Save the parent project to retain per-output settings.
8. Reopen the packaging/ run folder using Open an existing run…; old V3 folders work too.

## Verification

Results are recorded after the consolidated offline regression run. No paid providers,
real Whisper, real VOD decoding or long exports are involved. Whole-repository pytest
is outside this scoped verification; gameplay frozen files were checked unchanged.

Completed verification (2026-10-15):

- Consolidated scoped regression suite: **194 passed, 0 failed**, 176.53s.
- Includes V4, Generated/V3/legacy Packaging, edit references, per-output registry, CLI and class-focused workflow regressions.
- Ruff: packaging modules, modified GUI/model modules and changed/new tests passed.
- Mypy --follow-imports=silent: **20 production modules passed**; this is not a whole-repository mypy claim.
- main.py --smoke-test: isolated configuration, offscreen, no media; exit 0.
- Synthetic V4 prompt/pack/preview example generation: 4 outputs, zero real providers/ASR/video decoding.
- Visual inspection of precision_preview.png: exact accented hook remains readable; installed fallback was Impact. This says nothing about artistic quality from a real image model.
- Frozen detector/Adaptive V2/assembler/Hero recognition/Whisper/template paths: no diff.
- No commit, model/font download, real AI generation or long-VOD analysis.

## Fix 2026-10-16 — optional card readings before validation

The saved Priest run failed after two planning attempts: a contained card box
[0.91, 0.471, 0.085, 0.022] was below the existing minimum crop size, while C1/C4
used uncertain observations as protagonists. The later extraction fallback was
unreachable because creative validation ran first.

prepare_card_fallbacks now adapts optional readings before V4 validation: tiny
contained crops are recorded rather than enlarged; uncertain card references use
their catalogued real frame. Package, design, evidence and scene references stay
consistent. Fallback stories remain uncertain. Unsupported card names in titles
remain invalid; unknown IDs, non-finite/out-of-frame boxes are not hidden.
card_fallbacks.json records discarded readings, reasons and replacements. No
recognition threshold, detector, VOD analysis or real provider call changed.

Initial offline fix regression: 27 passed (new reported-case tests + Generated
regressions), 41.71s. Ruff passed; mypy passed on the five touched production modules.
The failed live answer itself is not retained by the existing adapter; its report
and the reproduced geometry/error shape were inspected, not a full live-answer replay.
Restart ReCut and retry generation from Thumbnails. Compatible local evidence is
reused, but a new planning request is needed because rejected answers are not cached.

Final recheck after retaining story_confidence=uncertain: 14 passed, 12.41s
(reported-case tests plus Local render lock regression; overlaps the earlier 27).
