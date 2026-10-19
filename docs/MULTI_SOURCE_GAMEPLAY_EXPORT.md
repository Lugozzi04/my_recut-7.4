# Selected gameplay exports and multi-video sources

## Audit and minimal changes

The repository already had linked Media/Clip tracks, sequential multi-file playback,
source-local keeps, source switching/reordering, ProjectExportPlanner, FFmpeg
filter-concat, a job-backed group exporter, EditMap, shared evidence caches and
the CLI concat command. None of those are replaced.

Two real selection bugs were found:

1. Class checkboxes wrote gameplay_grouping.selected, but normal Export video
   never consulted that field. Only the separate group button did.
2. GUI flattening applied global keeps only when the list was nonempty. An
   empty keep plan could leave the full base clips in the rendering plan.

The main Export action now dispatches filtered gameplay requests to the existing
group worker/job exporter. selected_project is an editorial projection in the
existing hero_groups/projects module:

    source group selections
      intersect current game islands / individual overrides
      intersect final Classic/manual keeps
      filter Complete WINs Only, when enabled
      prepend applicable intros
      existing ProjectExportPlanner -> ExportWorker -> validation/promotion

Empty selection is an error. It never falls back to a whole VOD or an intro-only
video. Explicitly unfiltered editing remains available for the old workflow.

## GUI

1. In Analysis -> Sources, use Add video and choose Gameplay or Intro. The existing
   top-level Import/Add Video action remains available and adds Gameplay sources.
2. Gameplay sources use existing Classic analysis and Detect games, including the
   existing Adaptive V2 test selector. Detect games processes Gameplay sources
   only. Existing evidence caches are reused unchanged.
3. Click a source row to activate its player/track, seek to its start and focus the
   timeline on its interval. Hero Groups shows that source's games and class
   checkboxes. Their selections are independent for each VOD.
4. Individual Include checkboxes and timeline toggles remain separate from the
   class filter. Deselecting a class does not destroy per-game choices; its boxes
   become grey with “class deselected”. Rechecking the class restores that filter
   layer without changing individual exclusions.
5. For an Intro, choose Role / intro and check one or more class/group targets,
   or All selected outputs. It does not start audio/gameplay/Whisper analysis.
   Replace intro, Remove source and reorder arrows reuse existing import,
   removal confirmation and track-order mechanisms.
6. In Export -> Gameplay export selection, choose:
   - Single video — selected classes: one combined video.
   - Videos by class — selected classes: an independent video/project per output.
   - Single video — current edit (unfiltered): the explicit legacy edit, without
     filtering by class checkboxes.
7. Leave Complete WINs only checked for the requested WIN-only workflow. Turning
   it off permits explicitly included manual/LOSS/UNKNOWN intervals under the
   existing editing model; detected results are never rewritten.
8. Read the actual games/WIN counts and output lengths. Show export plan lists
   each output, source interval and resulting output interval before rendering.
   Use Export video or Export with this selection; both follow the chosen mode.

Filtered gameplay requires Single video in the ordinary delivery Output setting.
Per-clip/audio-only/video-only/range delivery modes are rejected clearly in this
workflow instead of silently ignoring settings. They remain available through
the explicitly unfiltered editor.

Pending Gameplay detection is an explicit error, not silently omitted content.

## Ordering and group identity

Separate outputs combine recognized Hearthstone classes across VODs. Anonymous
portrait IDs remain scoped to their source: unrelated “A” groups are never
assumed to be the same skin. A combined single output can include both; manually
assigning a recognized class uses the existing Hero Groups correction controls.

In a combined output all unique applicable intros form one prefix, in project
order. Gameplay sources follow in project source order, with chronological games
within each source. Split fragments of a source are ordered by source-in time.
Moving source rows changes this order. An intro shared by two classes occurs
once in a combined output and once in each separate class output.

## Persistence, mapping and caches

Existing TrackState gains source_id, source_role and intro_targets. Existing
Project gains gameplay_export (mode and wins_only). They are saved in the normal
.autocutter payload, including manual overrides. Project version remains 2;
legacy tracks default to Gameplay and a stable path-derived source ID. Saved IDs
survive relinking. No new database, source registry or cache hierarchy is added.

Each selected output has a new project/job/output identity. Its existing
job-backed exporter retains cancellation, temporary outputs, validation and
atomic promotion. Existing requested single filenames are refused; separate
outputs use unique directories and trusted class/generic filenames.

Every selected interval retains:
source_id, source START/END, group, output_group, game_id, result, included, role.

The planner preserves those fields and generates output-time segments with their
own v_idx/a_idx and source-local coordinates. ExportPlan also retains
selection_intervals when a single-source fast path is used. EditMap reads the
same planner, so identical timestamps in two VODs remain distinct by source
index. Output project snapshots are available to future Packaging consumers.

Changing class selection, intro targets or source order performs projection only.
It does not run detection, ASR, Hero Groups recognition or boundary analysis.
Shared evidence cache keys/locations and their algorithms are unchanged.

## Verified normalization and limits

Selected multi-source videos reuse the existing FFmpeg filter-concat path with
the SAR/scale/pad normalization shared with CLI concat. The first retained
Gameplay source supplies source-resolution/FPS defaults, rather than a
low-resolution Intro. Explicit output resolution/FPS settings still apply.

Input images are fitted without stretching; letterboxing can be intentional.
Missing audio becomes silence; mixed audio is normalized to 48 kHz stereo
before the existing output audio settings. MP4 edit lists preserve encoder
priming/reordering. HDR compositions fail explicitly; automatic tone mapping
has not been added.

Synthetic rendering exposed offsets introduced by the legacy conservative TS
block-join path (AAC priming/B-frame reorder). Annotated selected compositions
therefore use the existing one-pass filter-concat path, with edit lists, rather
than that TS join. Other export paths retain their old behavior.

Large selected compositions may create large filtergraphs and have not been
benchmarked on long VODs. A bounded, timestamp-safe block strategy would require
separate validation; this change makes no speed or memory-performance claim.
No GPU-specific equivalence claim is made.

Packaging prompts/providers/image generation are unchanged. Its future source
lookup can use the saved selected project, source IDs and existing EditMap.

## Verification

New tests cover the actual planner inputs received by the render workflow,
class and individual selection, WIN-only, empty plans, two VODs with identical
game IDs/timestamps, source order, shared intro deduplication, per-output identity,
existing-file protection, source-role persistence and legacy projects, GUI
source switching/class selection, intro assignment without analysis, and
source-aware EditMap.

Two real FFmpeg tests use only tiny generated clips:
different resolution/FPS/audio sample rates, Intro with and without audio, and a
manual cut inside a WIN. They verify output duration, source ordering by decoded
frame colors, no black center frames at joins, distinct audio tones/silence,
monotonic frame timestamps, and gameplay-derived output geometry.

No complete VOD, real AI request, model download or commit is involved.

### Final local results (2026-10-13)

- Pertinent regression suite: **739 passed, 0 failed, 0 skipped**, 45 test files (160.48 s). This is the relevant suite, not every repository test.
- New multi-source export coverage: **17 tests**, including two real FFmpeg synthetic renders.
- Ruff: passed on changed Python modules and new tests.
- Mypy: passed on nine core/planner/source-panel/group-panel modules; no claim of repository-wide mypy cleanliness.
- Application smoke: `python main.py --smoke-test`, passed.
- Additional offscreen GUI smoke with two generated one-second files: passed; Intro skips analysis, source switching works, and selected preview contains one WIN plus its Intro (1.6 s).
- `git diff --check`: passed. Frozen detector artifacts/tests were not modified.

## Files involved in this extension

- `analysis/hero_groups/projects.py`: selected editorial projections and isolated output projects.
- `analysis/gameplay/selection.py`: exclude Intro sources from gameplay selection operations.
- `core/project.py`, `core/track_state.py`, `core/project_file.py`: source roles/IDs/targets and saved export options, legacy defaults.
- `automation/exporter.py`: source-aware selected intervals and planner mapping.
- `export/exporter.py`, `export/concat.py`: reuse filter-concat and shared SDR normalization for selected multi-source outputs.
- New `ui/project_sources.py`: source list, roles, Intro assignment, order, replacement/removal.
- `ui/hero_groups.py`: modes, exact preview, selection dispatch to existing export jobs.
- `ui/main_window.py`, `ui/layout_main_page.py`, `ui/layout_export_page.py`: connect existing GUI, save/load and analysis guards.
- `widgets/timeline.py`: show class/WIN export filtering separately from individual inclusion.
- New `tests/test_multi_source_export.py`: targeted regressions and synthetic rendering verification.
- This document and the selected-export clarification in `docs/GAMEPLAY_TIMELINE_EDITING.md`.

Pre-existing CLI concat and timeline/manual-editing work in the working tree is retained; it is not a new implementation of this task.

## Follow-up export audit (2026-10-14)

The one-pass selected-composition renderer described above has been superseded by bounded MP4/lossless-audio chunks. Export presets and per-output thumbnail planning were also added. See [Export workflow audit](EXPORT_WORKFLOW_AUDIT.md) for the current behavior and verification.
