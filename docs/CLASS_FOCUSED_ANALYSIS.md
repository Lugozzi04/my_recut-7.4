# Class-focused analysis and export-local artifacts (2026-10-15)

## GUI workflow

- **Analysis → Automatic speech cuts** is on by default. Turn it off before importing
  to skip the Classic audio-analysis worker for the new source. On a loaded source,
  turning it off recomposes keeps from manual cuts and gameplay selection only;
  it does not destroy the intensity/threshold configuration. Turning it back on
  reuses RMS already in memory or starts the existing audio worker when necessary.
  This option is saved per source; old projects default to on.
- **Gameplay editing → Find class** defaults to All classes. A named class enables
  the experimental sparse class search for that source. It is independent of the
  later export checkboxes; targeted runs initially select their requested class.
- **All classes + Traditional** restores the ordinary full detector. Adaptive V2
  remains unchanged and available through its existing backend control for
  all-class runs. Class-focused runs use the frozen traditional detector locally.
- WIN-only, individual exclusions, manual class overrides and Intro assignments
  still govern the final export.

## Actual search architecture

producer → artifact → consumers:

- FrameSamplingService accurate seeks → 60 s scout plus 30 s midpoint/recovery
  samples, three distinct observations per point → existing bottom Hero Power
  recognizer and board gate.
- Existing HeroGroupingService reference folder (including DK) → class votes,
  similarity/margin/support → source-window preselection.
- Target/weak/missing evidence or a class change → traditional detection window,
  padded 60 s on both sides → existing VS/result matchers and refinement.
- Adjacent strong votes for the same non-target class → skipped interval.
- Each merged window → source-local sampler adapter → unchanged detector and
  assembler → events/games translated to original source time. Games are never
  paired across skipped gaps.
- Combined detections → Hero Groups once → target game IDs → Smart Boundaries
  only for requested-class complete WINs → final WIN keep-policy filter.
- The worker reuses that grouping instead of repeating it after boundaries.

No temporary VOD, render, cloud classification, new model or download is required.
Whisper/Exact semantics are unchanged. Source and asset identities, class, reference
bytes, ROI/settings and algorithm versions invalidate the persistent focused cache.
Manual grouping overrides are reapplied after a detection-cache hit.

### Important limitation

A 60 s scout with 30 s recovery **cannot guarantee recall of a game entirely
between samples**, even if both visible non-target Hero Powers agree. The fast
choice and report explicitly disclose this. Weak/covered/modified powers, missing
requested reference, menus and ambiguous evidence trigger local or full fallback.
A confident wrong Hero Power classification or an invisible brief match remains
a limitation; use All classes/Traditional when completeness is essential.
No global detector accuracy claim or real-VOD speedup is made here.

The classifier still analyzes the player's lower hero/power, not the opponent.
Existing skin descriptors, class thresholds, WIN/LOSS algorithms and frozen assets
were not tuned. The sparse stage classifies samples, not match boundaries.

## Export and storage

Every selected output has an isolated folder in the chosen destination:

- Separate class videos: existing destination/output-UUID folder.
- One combined or one-class video: <video_stem>_recut_<output-id-prefix>/ beside
  the chosen MP4.

The folder contains:

- project_before_export.autocutter — full parent project with selections,
  sources, manual edits and analysis metadata, saved before rendering.
- edit.autocutter / export_edit.autocutter — this output's immutable edit.
- timestamps.txt — ready to paste into a YouTube description: chapter starts
  on the final edited timeline, labelled Intro (when present), then Partita 1,
  Partita 2, etc. Games are numbered in output order, independently of source
  indexes. Internal cuts are removed from durations. No headers, end times,
  filenames or extra metadata are included.

  Example:
  ```text
  0:00 Intro
  2:15 Partita 1
  12:40 Partita 2
  1:02:03 Partita 3
  ```
- packaging/ — independent cover run, JSON, references and thumbnails, when generated.

Normal GUI export also saves a uniquely named sibling artifact folder with the
parent project and timings before starting its existing ExportWorker.
The renderer refreshes timings from its normalized/snapped keeps or segments
before rendering; no matching/cut/render decisions are changed. Container/timebase
rounding can still affect the last fraction of a frame.
The normal project's raw backup remains editable and includes all sources.

Projects and media are not embedded together: keep source video files available.
Old artifacts are preserved. When regenerating covers for a legacy export whose
project lives in cache, settings_for_output first copies its edit beside the video;
it does not delete or move the old run. Internal technical caches remain reusable,
but new final cover runs no longer depend on a cache-only output location.

## Thumbnail scope diagnosis and fix

The saved run under the user's Hunter destination actually selected **warlock**
and contained **six Warlock WINs** in its immutable export intervals. The pipeline
could not infer Hunter from the directory name, and it must not do so.

The fixes make the actual selection explicit:

- A combined output containing only one known class is named for that class,
  instead of anonymous “Selected games”; GUI suggests its real class filename.
- Packaging context derives allowed PLAYER classes from actual included export
  intervals, including older anonymous combined-output snapshots.
- Opponent classes and class names mentioned in Intro do not define the player.
- Only exported gameplay contexts/transcript excerpts supply the creative brief.
  Intro and parent-unselected games cannot supply class/card evidence.
- Frame filtering continues to enforce actual final keeps and this output's
  gameplay intervals. A deliberately selected Whole current project source still
  means a combined project cover; choose An exported class video for a finished edit.

## Profiling and verification

The existing profiler aggregates class_focus.scout frame requests/deliveries,
class_focus.local_detection, local window count, source seconds scanned/skipped
and focused cache hit/miss. The existing matcher/refinement profiling remains
active inside each window.

Synthetic tests cover sparse requests, source-relative timestamps and unchanged
6/60 FPS refinement arguments, target/unknown/class-change fallback windows,
independent window assembly, cache reuse and source/reference invalidation,
manual overrides, class-only boundary work and final keeps, no-target WINs,
export-local snapshots, legacy packaging-copy compatibility, normalized chapter
times, multiple sources/noncontiguous keeps, option persistence and backward
compatibility. No real VOD or AI provider is used by the new tests.

Measured results and final checks are recorded below after completion.

### Completed checks

- Broad related regression run: **428 passed, 2 real-VOD tests explicitly deselected**, 57.01 s.
- Packaging V2/V3/gen/CLI/output regression run: **76 passed**, 86.71 s.
- Final run after the last cache/identity/GUI/generator changes: **68 passed**, 25.74 s.
  This includes **19 new feature tests**. These runs overlap; counts are not summed as unique tests.
- Ruff passed across all production modules changed in this task and the new test module.
- Mypy --follow-imports=silent passed for 12 checked modules (automation exporter,
  TrackState, artifacts, class focus, gameplay editing, group projects/context,
  packaging outputs/pipeline/V3, gameplay worker and Hero Groups panel).
- ui/layout_main_page.py retains 20 pre-existing Qt enum typing diagnostics.
  A HEAD shadow-file check produced the same 20 diagnostics; none was introduced
  by this task. No claim is made that whole-repository mypy is clean.
- Isolated offscreen main.py --smoke-test exited 0, without loading media.
- git diff --check passed; frozen detector assets, state-aware detector, Adaptive V2,
  assembler and skin/class recognizer remain unchanged.
- No real VOD analysis/render, ASR inference, provider call, model download or commit.

Routing-only fixture (fake media/recognition/detector, **not a speed benchmark**):
3000 s source; 302 requested scout frames; local windows [810, 1260] and
[2910, 3000]; 540 s sent to local detection, **18% source coverage**. Real total
cost and event recall must be measured on representative VODs with the existing
GUI profiler. A high uncertainty rate can still cause near-total fallback.

Scout votes are cached independently of target class through the existing
GameplayCache. Window results are reusable across classes when source and bounds
are identical. Manual overrides do not invalidate either cache. Scout timing is
inclusive of the reused board/Hero Power routines; overlapping profiler phases
must not be added as independent wall times.

Files added: analysis/gameplay/hearthstone/class_focus.py, export/artifacts.py,
tests/test_class_focus_workflow.py and this guide. Existing GUI/source state,
gameplay service, group planner, packaging context/output/pipeline and renderer
were extended at their existing integration points. Rendering changes only add
the optional normalized-plan timing sidecar; existing export algorithms/options
remain in place.
