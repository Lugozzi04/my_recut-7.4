# Covers for separate class exports

In **Export → Videos by class**, select the desired classes and optionally check **Generate a cover for each video** (off by default). The batch uses the provider, language and thumbnail engine selected in **Thumbnails**. A single consent dialog explains which transcript/frame data is sent; recognition of the classes remains entirely local.

The shared renderer exports the selected classes first. Each successful output is registered immediately, even if a later video fails or the batch is cancelled. Then the existing PackagingPipeline runs sequentially once per exported edit, with its own source project and output folder. A failed cover does not delete/invalidate the video or prevent the next cover. Cancel interrupts local work and prevents subsequent video runs. Packaging checks cancellation between stages. A provider CLI call already in progress may finish or reach its existing timeout before cancellation returns; the window waits for worker completion.

## Exact input, isolated results

Each class output already has a UUID-owned directory and filtered child project. After a successful export we save an additional **export_edit.autocutter** snapshot. Packaging uses this immutable snapshot, not the full parent project and not a newly detected rendered MP4. Thus it inherits the actual kept games, current Smart Boundaries and Classic/manual cuts, in their exported order. Later changes to edit.autocutter do not silently change the cover's input.

Directory layout:

~~~text
group_hunter_<uuid>/
  edit.autocutter
  export_edit.autocutter
  group_hunter.mp4                 # container follows export settings
  packaging_request.json          # output association, options, status; atomic JSON
  packaging/
    content_analysis.json
    game_contexts.json
    transcript.json
    frames/
    webcam/
    contact_sheets/
    thumbnail_concepts.json
    title_candidates.json
    thumbnails/
      concept_01.png ...
      thumbnail_final.png         # chosen in this output's UI
    selection.json
~~~

Paladin/Hunter/etc. have separate folders, frame IDs, concepts, titles, approvals and final covers. A frame ID such as F001 is local to its own run. No title/class is inferred from another output.

The Packaging frame extractor now applies this edit's **half-open final keeps** on every decode/cache read. It never falls back to removed frames when a local window has no kept sample. Saving a frame outside the edit is rejected. The shared source-frame cache stores raw physical frames; filtering happens **after** reading it, so reuse cannot import another class's evidence. This also protects nearby card crops and background plates. Transcript words already use the edit-map keep filter.

## Generate later / review each video

If the checkbox is off, no Packaging/provider calls are made by the export batch. In **Thumbnails → Source → An exported class video**, choose the video and click **Generate titles and thumbnails** when desired. Finished runs appear there for individual editing and confirmation. Changing the chosen video clears the previous results before displaying the next run.

The existing engine generates a portfolio of thumbnail alternatives **per video**; confirm one independently for each output. This change does not replace that portfolio with a single unreviewable image. The association list is saved with the parent .autocutter project (save the project to retain it on reopening). The child snapshot and packaging_request.json also remain beside the video, so the run can be opened independently with **Open an existing run**.

## Reuse and recovery

No game detection or boundary agent runs are needed for a class output: games mode is project. Existing content-addressed Packaging caches are retained for transcript/audio/visual/frame evidence. Previously cached identical ranges/settings are reused; a range without cached ASR evidence may still need its first transcription. Decisions and output artifacts remain per edit. An explicit new Generate archives the previous run and may call the selected provider again; editing/confirmation stays output-specific.

An interrupted/failed cover can be regenerated for that video without exporting the video again. The request manifest records running/done/failed/cancelled. Finished video files survive all Packaging failures. Worker references remain alive until QThread.finished; closing waits for cancellation before destroying the window.

The existing **--only render** mode was also corrected to reuse that run's saved generated images locally, even if the shared cache is disabled. Missing images fall back to local renders; render-only makes no provider calls.

## Verification

- Focused tests: output settings/input separation; raw frame-cache reuse with per-edit filtering; no removed-frame fallback; no duplicate cached ASR/frame decoding; retained games only; independent cover generation/approval; failure continuation; cancellation; successful-export notifications; immutable snapshot; optional batch flag; per-output GUI selection and project round-trip; worker completion/cancellation; render-only without shared cache / missing images.
- Local videoprova.mp4 check: two bounded frame requests (Hunter game 2 / Paladin game 4), each selected within its own export keeps; no VOD detector, ASR, provider calls or full video rendering. Evidence: [per_output_frames/verification.json](hero_group_validation/per_output_frames/verification.json) and the two per-class contact sheets.
- Full suite, lint, typing and GUI smoke results are recorded in the delivery report.

No new dependencies, gameplay-detector tuning, export algorithm changes or commits.

## Final results (2026-10-12)

- New focused suite: python -m pytest -o addopts= -q tests/test_packaging_outputs.py — **13 passed**.
- Full suite: python -m pytest -o addopts= -q — **1137 passed, 0 failed, 1 skipped**, 166.13 s.
- python -m ruff check . — passed.
- Existing mypy gate plus outputs.py — **76 files passed**; targeted --follow-imports=silent check including thumbnails_page.py — **7 files passed**.
- python main.py --smoke-test — passed.
- GUI preview: Export checkbox and two-output Thumbnails selector; saved project payload contains both associations.
- Real bounded videoprova frame check: Hunter/Paladin, one representative kept frame each, two decode requests, **0.79 s**. No cloud requests or full-VOD analyses/renders.
- Frozen analysis/gameplay and resources/gameplay trees unchanged.

Modified for this feature: analysis/hero_groups/projects.py; analysis/packaging/frames.py; analysis/packaging/pipeline.py; core/project.py; ui/hero_groups.py; ui/thumbnails_page.py; ui/main_window.py; docs/HERO_GROUPS.md; docs/PACKAGING.md. New: analysis/packaging/outputs.py; tests/test_packaging_outputs.py; this document; per-output GUI/frame verification artifacts under docs/hero_group_validation. Other pre-existing working-tree changes were retained.
