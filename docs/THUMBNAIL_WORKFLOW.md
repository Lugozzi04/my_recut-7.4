# Thumbnail runs: reopening, image attachments and reuse

## Reopen without generating again

In Thumbnails, choose **Open an existing run…** and select the directory containing
thumbnail_concepts.json. This is the run root, not the MP4, a PNG, or the parent
containing several runs. Its **thumbnails/** subdirectory is also accepted and
resolved to the run root. Reopening only reads saved metadata and images; it does
not call ASR, gameplay detection, export or a provider.

For a per-output run, the directory is **packaging/** beside that output's saved
.autocutter snapshot. The exact path appears in Thumbnails Results and the export
completion message. Outputs registered in the saved parent project can also be
selected via **An exported class video**. Save the parent project after creating
covers to retain this output list. Preserve the entire run folder, including its
JSON files and reference assets, for later editing; a lone PNG can be viewed with
an image viewer but is not an editable ReCut run.

Generated candidates are thumbnails/concept_NN.png; confirming one creates
thumbnails/thumbnail_final.png and selection.json. Earlier edit versions remain
in thumbnails/edits/.

## Attach an image to an edit

Click a thumbnail, write the requested change, and use **Attach reference image…**.
The dialog previews the attachment and allows removal before **Apply change**.
A cancelled picker leaves the existing choice intact. A failed edit retains it
for retry; a successful edit clears the pending choice.

One existing image-generator request receives, in order:

1. The current thumbnail.
2. The optional creator attachment, explicitly labelled and governed by the edit request.
3. Up to three original identity/game references for that concept.

The attachment is validated before the provider is invoked and copied losslessly
(including alpha) to thumbnails/edits/references/<concept>_<uuid>.png.
thumbnail_edits.json stores its relative path in edits[].reference_image.
Moving the original attachment does not break the recorded version. Existing runs
and text-only edits retain their previous behavior. Applying an edit uses the
existing image-model integration; reopening, reverting and confirming are local.
No provider calls were made during this implementation's tests.

## Pipeline audit (2026-10-14)

| Operation | Existing reuse / isolation |
| --- | --- |
| Per-output scope | Immutable child project and final keeps, independent run directory and output association. |
| Detection | settings_for_output uses games=project; no new gameplay detection. |
| Transcript | Original-source faster-whisper coverage cache, keyed by source and compatible engine settings; clipped and remapped into kept output segments. Missing coverage still requires ASR. |
| Audio | Packaging RMS-span cache plus shared native RMS store; compatibility includes filter, hop, sample rate and window origin. Whole-source Classic RMS is not blindly sliced into incompatible filtered windows. |
| Visual scan | Cached keyframe-only 160×90 evidence over kept spans, not a full-resolution VOD scan. |
| Thumbnail frames | Short selected full-resolution bursts, shared source-bundle cache, filtering by this output's keeps and gameplay intervals after loading. |
| Generation | The usual fast path plans candidates in one request and generates the set in one request on a cold cache; optional deepening/retries can add calls. Prompt/asset generation cache retained. |
| Editing | Current thumbnail and saved references only; no evidence analysis or video rendering. |
| Export plus covers | GUI GroupWorker finishes video exports, then packages outputs serially; no added simultaneous FFmpeg/render workload. |

### Targeted change

FrameExtractor.burst now checks the intersection of requested time, kept source
pieces and optional class-specific gameplay intervals **before** loading a cache
bundle or asking FFmpeg for frames. Intro/excluded/removed-only requests return an
empty result immediately. Requests partly intersecting an allowed interval retain
the original sampling grid and post-load filtering; cache keys and recognition
algorithms are unchanged. Cancellation is checked even for skipped work.

### Limits

This is a code audit and offline regression verification, not a real-VOD speed
benchmark. A new output still needs its own highlight selection, crop/ranking and
creative planning. Cold caches require decoding of selected bursts; overlapping
but non-identical burst requests are not automatically combined. Source frame
bundles are shared, while output-specific images and metadata remain separate.
Changing source identity or ASR options can invalidate reuse. Large runs retain
full-resolution selected images in memory and archiving a prior run copies its
assets. Provider latency remains external and is not optimized here.

No detector, export, grouping, provider choice or creative-generation algorithm
was changed. The new attachment label only affects edit requests with attachments.

## Verification

Focused offline tests cover attachment forwarding, transparent/unicode images,
invalid inputs before any model call, text-only compatibility, re-opening,
version history/revert/approval, dialog controls, excluded/Intro frame requests,
disjoint filters, cancellation, per-output isolation and existing export workflow.
Additional existing Packaging V2/V3/generated-engine regressions use fake media,
ASR, agents and generators. Ruff and mypy target the changed production modules.
Completed checks: 34 focused thumbnail/output/export workflow tests passed
(36.10 s), followed by 59 Packaging V2/V3/generated-engine regressions passed
(86.81 s): **93 passed, zero failures**. Ruff passed for the three changed
production modules and the new test module. Mypy with --follow-imports=silent
passed for analysis/packaging/edit.py, analysis/packaging/frames.py and
ui/thumbnails_page.py. git diff --check passed. GUI dialog/controller behavior
was exercised offscreen; no full interactive GUI smoke or whole repository
suite was run for this change. No real VOD or cloud model was processed.

## Update 2026-10-15

New output runs and legacy-export regeneration now save their project/packaging
artifacts beside the destination video, not only in cache. Exported player classes
are derived from the actual selected intervals and sent to both planning and final
image generation. See [Class-focused analysis and output artifacts](CLASS_FOCUSED_ANALYSIS.md).


## Thumbnail Pipeline V4

See [implementation, examples and verification](THUMBNAIL_V4.md) for Creative Direction, Competitive Gaming profiles, Design Packs, Hybrid Typography and Polish. Legacy runs remain supported.
# Creare copertine per un video già esportato

In **Thumbnails**, usa **Create thumbnails for an exported video…** e seleziona
il video finale. Non occorre una run precedente né riesportare il video.

Se sono disponibili l'associazione all'export, lo snapshot esatto e le sorgenti,
ReCut recupera l'output nel selettore e riusa partite, final keeps ed evidenze
compatibili. Cerca soltanto nella cartella del video e nelle sue cartelle artifact
con nome corrispondente; non usa il backup dell'intero progetto come se fosse
il montaggio esportato. Snapshot ambigui non vengono scelti automaticamente.

Se quei dati non sono disponibili, seleziona **Previously exported video — choose
file** e lavora direttamente sul video finale. Non esegue detection Hearthstone;
il normale Packaging ricava transcript, momenti e frame soltanto da quel file.
Senza metadati non conosce i risultati originali delle partite o i confini dell'Intro.
Le copertine vengono salvate accanto al video in `<nome_video>_packaging/`.

Dopo la scelta, imposta la Creative Direction se vuoi e clicca **Generate titles
and thumbnails**. Il progetto di editing corrente non viene sostituito.

**Open an existing run…** resta invece il comando per riaprire e modificare
copertine già generate, scegliendo la cartella con `thumbnail_concepts.json`.

