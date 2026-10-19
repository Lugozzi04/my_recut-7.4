# Export / presets / per-output thumbnails — 2026-10-14

## Existing-run evidence

Two saved selected-output projects included one Intro and seven WIN games:
351 intervals (350 gameplay keeps) and 313 intervals (312 gameplay keeps).
The actual delivery files are marked DONE and report about 42:33 and 52:31.
Six sampled frames (5, 250 and 1500 seconds in each existing output) show
Intro and Gameplay; they are not Intro-only files. This was a metadata/seek
inspection, not a full re-analysis or a complete visual audit of every cut.

The previous multi-source implementation forced one filter-concat graph with
hundreds of trim branches. This permits large future-frame queues while concat
waits for earlier streams. The group job also did not forward renderer details
to the normal Export footer/log. Both explain a costly, opaque path; no claim
is made that the exact wall time of the user's run was reproduced.

## Rendering correction

Selected compositions with more than one source now reuse the existing chunk
planner/scheduler/cache, source seek calculation, filter builder and validator:
- at most 40 trim branches and a 120-second source-span budget per chunk;
- split on source changes; only referenced inputs are opened in each process;
- preserve codec, CRF/QP, resolution, FPS, colors, cut policy and delivery options;
- intermediate MP4 with constant 48 kHz stereo 24-bit ALAC, to avoid repeated
  AAC encoder priming at each block join;
- explicit requested chunk durations in the concat manifest, edit lists, and
  trimming the FPS-converted video tail to avoid duplicate boundary frames;
- final video stream copy, one delivery-audio encode with requested audio
  settings and the existing global gain/LUFS/limiter processing;
- cache signature version distinguishes this representation and includes the
  Gameplay normalization reference, preventing Intro cache collisions across
  differently sized source projects.

Workers/chunk preferences still feed the existing planner; the safety bounds may
require additional chunks. Accurate remains accurate re-encoding, not stream
copy of the original source. Existing single-source/legacy export routes remain.
No expensive option or built-in profile was removed. Mixed HDR remains explicitly
unsupported; GPU encoder-specific equivalence and full-VOD throughput are not
claimed. Intermediate lossless audio consumes additional cache/disk space.

This supersedes the one-pass limitation documented in the earlier multi-source
report. The legacy TS join is still avoided for selected compositions.

## GUI workflow

There is one render CTA in the Export footer, also invoked by the existing top-bar
action / Ctrl+E. The redundant in-panel render button is hidden; all selection,
preview, cover and delivery options remain. The CTA describes combined selected
classes or separate class videos and becomes Cancel export while the group
renderer runs. Renderer details, progress and errors appear in the standard
Export log/footer. Settings are locked only during rendering.

Delivery is labelled separately from Gameplay export selection. Single selected
classes deliberately combines those classes into one video; Videos by class
creates separate files. The summary shows Intro count per output. Class
checkboxes apply to the active Gameplay source; the plan includes all source
selections. Show export plan retains source/output timestamps.

## User export presets

Output -> Saved presets supports Save as, Load and Delete. Built-in Profile
starting points and every advanced control remain available. Saved entries
contain normalized ExportSettings (codec, quality/rate control, audio, transforms,
method, performance and range options) plus combined/per-class mode, WIN-only
and the optional per-output cover setting. Source identities, actual class
checkboxes and game inclusion remain project choices, not global preset data.

Storage: config_root()/export_presets.json; normally
%APPDATA%/Auto Cutter/Auto Cutter/export_presets.json on Windows. The existing
AUTO_CUTTER_CONFIG_DIR override applies. Writes use the existing atomic writer.
Replacing a name requires confirmation; corrupt preset data is not silently
discarded. No migration of analysis presets or previous projects is performed.

## Thumbnail scope

Thumbnails defaults to Selected export plan — covers per output:
- follows Export mode and selections;
- snapshots each child project with its own identity and packaging directory;
- can prepare covers before rendering; those entries are marked planned/not
  rendered and do not create an MP4;
- uses the existing serial/cancellable packaging pipeline and provider options,
  with the existing explicit consent;
- retains Whole current project — combined cover as an explicit option and
  An exported class video / A video file.

For selected exports, GameContexts come from export_intervals, not unfiltered
parent reports. IDs are source-scoped and Windows-safe even when VODs reuse game
IDs. Frame extraction filters cached/source frames against this output's final
keeps and selected Gameplay game intervals; shared Intro frames are excluded as
game/card evidence. Original source clocks and edited timestamps remain intact.
Class metadata is passed to Packaging only when known; anonymous groups never
invent a class or deck. No creative prompt/provider/model was changed.

## Verification

See final local test results below. Tests use synthetic clips and provider/ASR
fakes only; no full VOD run, paid service call, model download or commit.

New tests cover persisted full settings, duplicate/corrupt preset protection,
GUI preset loading and action labels/cancellation, per-source input remapping,
350 keeps plus Intro with bounded graphs and monotonic video PTS, per-class
thumbnail plans without rendering, source-scoped GameContexts, and class-only
frame evidence excluding Intro. Existing mixed-size/FPS/audio render tests are
retained unchanged.

### Final local results

- Extended relevant regression suite: **872 passed, 0 failed, 0 skipped**,
  51 test files, 216.10 s. This is not the entire repository pytest suite.
- After final cache/scope guards: **41 passed, 0 failed, 0 skipped**, 31.61 s
  (export workflow, multi-source export, per-output Packaging).
- **11 new regression tests** in test_export_workflow.py. The 350-fragment real
  synthetic render also verifies audible tone through successive block joins,
  final gameplay frames and strictly increasing frame timestamps.
- Ruff passed on changed Python files/new tests.
- Targeted mypy passed on eight modules with --follow-imports=silent. An initial
  unrestricted import traversal also reported existing Qt typing errors in
  unrelated GUI modules; no repository-wide mypy-clean claim is made.
- Isolated offscreen GUI source smoke (main.py --smoke-test): passed.
- git diff --check: passed. Detector/frozen artifact files were not changed.

No before/after long-VOD benchmark was run. Retained high-quality software
encoding can still be slower than real time. The verified change bounds graph
construction and source access; it is not a guarantee of a particular hardware
throughput.

### Files changed in this follow-up

- export/exporter.py: bounded selected compositions, exact joins, lossless
  intermediate audio, active-input remapping and normalization-aware cache keys.
- analysis/hero_groups/projects.py: forward renderer details and verified class metadata.
- ui/hero_groups.py, ui/main_window.py, ui/layout_export_page.py: one render CTA,
  accurate scope/Intro summary, progress/logs/error/cancellation and preset row.
- New export/presets.py and ui/export_presets.py: persistent named export presets.
- ui/thumbnails_page.py: per-output plan worker and explicit scope/preview isolation.
- analysis/packaging/games.py, frames.py, pipeline.py: selected GameContexts and
  Gameplay-only frame eligibility in source/output timelines.
- New tests/test_export_workflow.py; this report and the multi-source report follow-up.

### Intro-active class selector fix

The export class list was incorrectly reading the active Intro's empty grouping.
It now retains the last selected Gameplay source (or the first detected Gameplay
source when opening with Intro active). The game list/player still follow the
actual active source. Class checkbox edits and group names target the displayed
Gameplay source, preserving independent selections across VODs. The source is
named explicitly below the checkboxes. Individual game exclusions remain intact.

Validation: 12 lightweight selection/UI tests passed in 0.84 s, including two
new Intro regressions; Ruff and targeted mypy passed. No render, detector,
ASR, full-VOD analysis or running application/export was started/stopped.

### Intro cut preservation fix

The Intro role previously reset keeps/cuts_enabled on role conversion. Manual
cut editing subsequently rebuilt visible keeps but did not enable cuts for a
fresh Intro, so export could ignore exactly the cuts shown on the timeline.

Role conversion now preserves the existing edit. Manual Intro edits enable cuts
and use exact manual keeps without the Classic minimum-keep heuristic. Old
projects with visible Intro cuts and a disabled flag are repaired on restore;
selected-output projection also handles those older payloads defensively.
A fully removed Intro stays removed, and source-in offsets are preserved.
Gameplay detection, audio/AI worker guards and Intro assignment semantics are
unchanged; no re-analysis is needed.

Verification: 12 focused tests passed in 0.64 s (six new Intro cases plus existing
role, assignment, source-selection and shared-Intro regressions). The tests check
actual ExportPlan source intervals and save/restore; no render/FFmpeg, ASR or VOD
analysis was executed. Ruff and targeted mypy (two modules) passed.

### User-requested removal of saved export presets

The personal Saved presets row (Load / Save as / Delete) and its helper modules were removed. Built-in Profile choices and all manual export controls remain. Existing user export_presets.json files are retained and are no longer read by the GUI. The earlier preset implementation/results above describe historical work, not an available feature.

### Per-class duration display

Export class rows now include Duration (rounded h:mm:ss / m:ss): the available
Gameplay duration in the displayed source after final cuts, per-game exclusions
and the active WIN-only policy. Unchecked classes keep showing their potential
duration. Computation reuses selected_project on a lightweight metadata snapshot;
it does not run media probes, analysis or rendering and does not mutate selections.

The output summary separately lists Gameplay + edited Intro = final duration
for each selected output, including compositions from multiple VODs. Validation:
14 focused GUI/selection tests passed; Ruff and targeted mypy passed.

### Single-source Accurate stall: 2026-10-14

Evidence from the supplied log and the saved Death Knight output project:
- 7h02m30s source; 233 raw keeps -> 207 normalized keeps.
- About 40m14.7s of retained content.
- Output reached 786.4s at wall time 241.9s, then stayed there through 1394.2s.
- Reconstructing the existing normalization/frame snapping from the saved JSON
  places the large source gap at output 786.4833s, matching the observed pause.
- Source before the gap: 8837.3333s (2:27:17); after: 17203.1333s
  (4:46:43). Removed interval: 8365.8s (~2h19m26s).
- The old command read a continuous 14738.533s input window from seek 7907.750s,
  with 207 trim branches. It did not seek directly across the removed hours.
  The log alone cannot distinguish all decoder work from a subprocess/driver
  stall, but the unbounded source-gap path is confirmed.
- h264_amf is active. hw_decode=off is the tuner-selected decode path,
  not fallback to x264. Initial output throughput was ~3.25x real time.

The earlier bounded fix covered annotated multi-source compositions only. A
single-source plan flattened to keeps and bypassed it. Accurate single-video
exports with >40 keeps or a removed gap >30s now adapt those existing keeps to
the same bounded segment renderer, before the conservative one-pass branch.
Other Smart/Hybrid fast paths and non-single delivery modes are unchanged.
Chunk source windows remain bounded; full re-encoding still honors the selected
quality/transforms. No inter-hour source scan or new detector/ASR work is needed.
Re-encoded bounded chunks do not request unnecessary keyframe scans.

Single-source intermediate lossless audio now preserves the requested/source
sample rate and channel layout instead of forcing the multi-source stereo/48k
normalization. Frame-tail trims/edit lists also apply to bounded single-source
chunks. Cache representation version is bumped.

Static partitioning of the actual raw JSON (without opening the VOD) produced
26 bounded windows, at most 15 branches and 118.93s source span. This is a
planning check, not a measured long-VOD render; runtime tuning may partition
differently within the same bounds.

Progress heartbeat now includes no_output_progress_for. The warning checks
output advancement even if FFmpeg keeps sending the same timestamp; the
existing inactivity termination policy is unchanged.

Verification:
- 107 relevant export/concat/recovery/Intro tests passed (49.17s).
- Four subprocess/progress/cancellation tests passed (1.13s), including the new
  repeated-timestamp warning regression.
- Two new real synthetic single-source renders skip a 240-second discarded
  gap using two short input windows and preserve source-mono/44.1k or requested
  stereo/48k audio, codec quality, content order and monotonic video timestamps.
- Ruff and git diff --check pass. Targeted mypy on process_mixin still reports
  15 existing mixin-interface/annotation errors, also present in HEAD.
- No live export was stopped, no full VOD was rendered, no commit was made.
