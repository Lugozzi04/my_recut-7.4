# ReCut GUI redesign — plan and old → new map

Status: implemented and validated (see §13–§16). The cut engine, gameplay detector, exporter and
project format are untouched. Outside the presentation layer only GUI-orchestration bugs found during
validation were fixed (threading/ownership, settings isolation — §14).

## 1. Layout

```
┌ Top bar (48) ─ logo · ReCut / project name · status ─────── Import  Save  [Export]  ⋯ ┐
├──────────────────────────────────────────────┬───────────────────────────────────────┤
│ Viewer (video, letterbox, overlays)          │ Inspector  [ Edit | Analysis | Export ] │
│                                              │  contextual, compact property rows     │
├ scrub bar ───────────────────────────────────┤                                        │
├ Transport (44): tc · ⏮cut ◀ ▶ ▶ cut⏭ · skip · vol│                                       │
├══════════════════ splitter ══════════════════╧═══════════════════════════════════════┤
│ Timeline toolbar (36): Select/Cut/Split · Undo/Redo · Snap ······ legend · − zoom + Fit │
│ Timeline: track headers | ruler · CUTS lane · VIDEO · AUDIO (waveform) · GAMEPLAY lane  │
├ Status bar (26): messages / progress ·········· 3:00 → 2:10 · 11 cuts · 28% removed ─────┤
```

* The viewer and the inspector share the upper area (horizontal splitter, inspector 340–520 px,
  default 384 px at 1920 px). The timeline spans the full window width below (vertical splitter),
  which is how NLEs give the timeline room.
* The "inspector left/right" layout switch is kept, but moved from a visible button into
  ⋯ → View → "Inspector on the left". It is rarely changed and does not deserve permanent space.

## 2. Top bar

Native Qt (`ui/widgets/top_bar.py`) instead of `webui/topbar.html`:

* Left: logo mark, "ReCut", project/file name, unsaved dot, status pill
  (`No video` · `Importing…` · `Analyzing audio… 42%` · `Analyzed` · `AI processing…` ·
  `Exporting… 37%` · `Export complete` · `Error`), with a thin progress line under the bar.
* Right: `Import` (secondary), `Save` (secondary, Ctrl+S), `Export` (primary → opens the Export
  inspector and focuses the CTA), `⋯` app menu.
* ⋯ menu: File (Open video, Open project, Save, Save as, Export EDL, Reset workspace), View
  (Inspector on the left, Status stats), existing Preferences (theme, recovery, recent files, remember
  filters/cuts, language), Twitch automation, Help (Keyboard shortcuts, Quick start, Diagnostics,
  Logs folder, Third-party notices, About), Reset app to defaults.
* Removed from the bar: Reset, Project Save/Load buttons, auto-skip toggle (moved to transport),
  VIDEO/AUDIO/PRESET/CODEC chips (status pill + inspector already show them), step pills
  "1 Import 2 Analyze 3 Review 4 Export", the full temp path line above the player.

## 3. Inspector

Segmented tabs `Edit | Analysis | Export` (`ui/widgets/inspector.py` provides section/row
primitives). Rows are label-left/control-right, 32 px controls, no nested cards.

* **Edit**: *Cuts* summary doubling as legend (automatic / manual / gameplay / restored counts) and
  selection details (start, end, duration, kind, Restore / Jump buttons); *Preset* (combo + `⋯`:
  Save changes, Save as new, Manage, Delete; state line "Balanced • Modified"); *Detection*
  (Cut intensity slider + value, Voice threshold slider + value + live level meter); *Advanced*
  (collapsed by default: Padding, Detection timing, Smoothing, Merging, Audio — each with Reset).
* **Analysis**: segmented `Classic | AI` with a one-line description; classic status line
  (auto-runs on import); AI tuning + `Run AI analysis` primary button + `Re-run`; AI stats; a
  read-only *Gameplay* section (shows gameplay keep islands imported from CLI projects; detection is
  run from the CLI pipeline today — no new GUI logic).
* **Export**: summary (output duration, cuts, codec), *Output* rows (Profile, Format, Resolution,
  Frame rate, Quality, Output mode, Output folder), primary `Export video` CTA with inline progress
  and status, secondary `Export EDL…`; collapsed *Advanced* (codec, aspect, FPS mode, depth, color,
  rate control, audio, method, cut border quality, range), *Performance* (HW decode, workers,
  chunks, benchmark advisor, clear cache), *Log* (details, copy, open folder).

## 4. Timeline hierarchy

Rewritten painting in `widgets/timeline.py` (same public API, same signals + new ones):

* Left header column (88 px) with lane names and icons: CUTS, VIDEO, AUDIO, GAMEPLAY (only when
  gameplay data exists).
* Ruler with adaptive ticks/labels; playhead with head on the ruler.
* CUTS lane: one block per effective cut, coloured by provenance — automatic (classic/AI, red),
  manual (amber), gameplay (violet); restored (suppressed) ranges drawn as green outlined
  hatched blocks.
* VIDEO lane: clips with name chips; removed ranges dimmed + hatched.
* AUDIO lane: mirrored waveform (filled, gradient); removed ranges desaturated with a category
  tint so you can see what is cut.
* GAMEPLAY lane: keep islands (green) vs gameplay-removed (violet).
* Hover highlight + tooltip (kind, start–end, duration); selection outline across all lanes.
* Interactions: click = move playhead; click a cut = select (Shift = add); drag on empty area =
  marquee selection; drag a cut edge = resize (applied via manual/suppressed ranges, i.e. the same
  model the editor already used); double-click a cut = jump to it; Delete = restore selected; right
  click = compact context menu. Clip drag on the VIDEO lane keeps the existing swap behaviour.
* Empty state text when nothing is loaded.

Provenance classification is a pure function (`classify_cuts`) fed by data the editor already
holds (`manual_cuts`, enabled `gameplay_cuts`, `suppressed_cuts`) — no new cut logic.

## 5. Transport

Native (`ui/widgets/transport.py`): timecode (current / total, tabular figures) on the left;
centre group previous cut · back 5 s · play (36 px round) · forward 5 s · next cut; right:
"Skip cuts" toggle (preview with cuts removed) and a volume button with a popup slider. All icons
come from one painter-drawn glyph set (`ui/widgets/glyphs.py`) so stroke weight and size are
consistent. Tooltips include shortcuts.

## 6. Stats / status

* Stats live in the status bar (permanent widget): `3:00 → 2:10 · 11 cuts · 28% removed`, updated on
  every recompute (the old HTML strip could stay stale at "0 cuts").
* Processing state has one owner: the top bar pill + progress line; transient messages go to the
  status bar; blocking overlays on the viewer only for AI processing.

## 7. Export panel — see §3 Export.

The old giant "Esporta MP4" / "Esporta EDL" equal-weight buttons, the always-visible 0 % bar and the
benchmark in the main path are replaced by one CTA, a secondary link-button and a collapsed
Performance section. The progress bar only appears while exporting.

## 8. Advanced placement

* Detection advanced → Edit › Advanced (collapsed, persists open state for the session).
* Export advanced → Export › Advanced (collapsed). Performance / benchmark → Export › Performance
  (collapsed). Logs → Export › Log (collapsed).
* App-level toggles (mini stats, layout switch, advanced enable, recent, remember…) stay in ⋯.

## 9. Old → new mapping

| Old | New |
| --- | --- |
| HTML top bar Import | Top bar `Import` (Ctrl+O) |
| HTML Reset | ⋯ › File › Reset workspace… |
| Project Save / Project Load | Top bar `Save` (Ctrl+S), ⋯ › Save as… (Ctrl+Shift+S), ⋯ › Open project… (Ctrl+Shift+O) |
| Auto-skip preview toggle | Transport "Skip cuts" toggle |
| Status chips VIDEO/AUDIO/PRESET/CODEC | Status pill (top bar), preset state (Edit), codec (Export summary) |
| Step pills 1–4 | Status pill + empty states |
| File path label | Project name in top bar (full path in tooltip) |
| HTML transport (prev/next cut, play, zoom, undo, cut/split, snap, volume) | Native transport (playback) + timeline toolbar (tools, undo/redo, snap, zoom, fit) |
| `Layout` button | ⋯ › View › Inspector on the left |
| HTML stats strip | Status bar stats |
| Tabs Montaggio / Esporta | Inspector tabs Edit / Analysis / Export |
| Preset save / save as / manage icons | Preset `⋯` menu |
| Classic/AI toggle switch | Analysis › segmented Classic | AI |
| Process AI / Reprocess | Analysis › Run AI analysis / Re-run |
| Advanced lock header | Edit › Advanced (collapsible) |
| Esporta MP4 / Esporta EDL / progress | Export › Export video CTA + Export EDL… + inline progress |
| Performance + Benchmark | Export › Performance (collapsed) |
| Show logs / Copy logs / Open logs folder | Export › Log (collapsed) and ⋯ › Help › Logs folder |
| "Remove this cut?" confirmations | Select + Delete / context menu (undo available) |

## 10. New widgets

`ui/widgets/`: `glyphs.py` (icon painter), `top_bar.py`, `transport.py`, `editor_status.py`
(status-bar stats), `inspector.py` (tabs, sections, rows, segmented control, collapsible),
`timeline_toolbar.py`. Design tokens in `ui/design_tokens.py`, consumed by `ui/theme.py` and the
custom-painted widgets.

## 11. Files modified

New: `ui/design_tokens.py`, `ui/widgets/{glyphs,inspector,top_bar,transport,timeline_toolbar,editor_status}.py`.
Rewritten: `ui/theme.py`, `ui/layout_left.py`, `ui/layout_right.py`, `ui/layout_main_page.py`,
`ui/layout_export_page.py`, `widgets/timeline.py`, empty state/overlay of `ui/frame_video_widget.py`.
Modified: `ui/main_window.py` (native chrome, state routing, selection/resize/keyboard, menus, export
folder, dirty state, threading fixes), `ui/main_window_components.py` (`GuiInvoker`,
`UiMessageRelay`; removed `WebUiBridge`, `GlobalWheelBlocker`), `utils/runtime_paths.py`
(`app_settings()`), `core/config.py`, `main.py`, `tests/test_ui_product_basics.py` (asserts that no
QtWebEngine views are created and the native chrome exists).

## 12. Regression risks and mitigations

* State pushes that went through `_web_js` now call native setters directly — every call site was
  converted mechanically and covered by the GUI smoke run.
* Timeline x-mapping now has a header offset: `_t_to_x/_x_to_t` are the only mapping functions, all
  hit-testing goes through them.
* Cut-edge resize writes manual/suppressed ranges exactly like add-cut/remove-cut; undo snapshot is
  pushed first.
* Removing confirmation dialogs makes edits faster; every edit stays undoable (Ctrl+Z).
* QtWebEngine is no longer initialised at startup (faster start); Twitch/other features do not use it.


## 13. Implementation notes (deviations from the plan)

* **Export CTA is a sticky footer** of the Export tab (always visible under the scrolling settings);
  progress bar and status appear only while exporting.
* **Threshold preview**: changing *Voice threshold* draws a dashed "Voice threshold" level on the
  AUDIO lane for ~2 s (same scale as the waveform), so the effect is visible before the cuts update.
* **Cut-edge resize** writes the grown part as a manual cut and the shrunk part as a suppressed
  (restored) range — the editor's existing model, undoable, no re-analysis.
* **Language**: the redesigned editor chrome is English-only. The old i18n hook translated only 6
  button labels, which produced mixed IT/EN screens ("Montaggio", "Esporta MP4"); menus/dialogs still
  go through `utils.i18n`. Full Italian localisation of the new UI is not done (limit).
* **First launch** shows a non-blocking status-bar hint instead of the modal Quick start (still in
  ⋯ › Help).
* **Unsaved changes**: a dot next to the project name after any edit; cleared on save/load/reset.
  Closing does not prompt because the session is already autosaved/restored by crash recovery.
* `webui/*.html` and `ui/inspector.html` remain on disk but are no longer loaded (QtWebEngine is not
  initialised by the editor any more).

## 14. Bugs found during validation and fixed

All pre-existing (reproduced on the untouched `HEAD` code where applicable), all in GUI orchestration:

1. **UI code ran on background threads** (random access violations / hangs, typically a few seconds
   after an analysis, much more often after *Open project* or during *Export* after a reload).
   Worker/thread signals were connected to bare `lambda`s with `Qt.QueuedConnection`; PySide delivers
   those in the *sender's* thread, so `_on_analysis_done`, progress, cleanup and export-log updates
   touched widgets from worker threads. Fixed with `GuiInvoker` (a receiver living on the GUI
   thread). Proven by a minimal test (bare lambda: off-GUI-thread; invoker: GUI thread) and by the
   reload→export reproducer: crashed in most runs before, 4/4 and 5/5 clean after.
2. **Double deletion of worker objects**: Python-owned workers also had `worker.deleteLater()`, racing
   with Python's own deletion when the references were dropped. Removed; export workers are retired
   until their thread has actually finished.
3. **Application-wide Python event filter** (`GlobalWheelBlocker`) ran for every event in the app;
   replaced by the per-spin-box filter that already existed (same behaviour for the user).
4. **Warm-cache logger** called `QTimer.singleShot` from a plain Python thread; now a queued signal.
5. **Settings isolation**: `QSettings("Auto Cutter", "Auto Cutter")` always uses the native store
   (Windows registry) and ignored `AUTO_CUTTER_CONFIG_DIR`, so `tests/test_main_entrypoint.py` (which
   sets that override) wrote `window_maximized` to the real registry. All 34 call sites now use
   `utils.runtime_paths.app_settings()` — identical behaviour without overrides, an INI file inside
   the override directory with it. Debug/crash-recovery/export logs use `config_root()` (same folder
   as Qt's AppDataLocation, but honours the override).
6. Status pill could stay on "Analyzing audio…" after analysis; stats strip could stay at "0 cuts".
   Status is now derived from real state and refreshed when analysis jobs are cleaned up.
7. Inspector pages had `setStyleSheet("background: transparent")`, which cascades to every child and
   overrode themed buttons/segmented controls; removed.
8. Splitter ratios were applied inside `resizeEvent` (stale sizes): the inspector fell to its minimum
   at 2560 px. Now applied after layout (384 px at 1920, 512 px at 2560).

## 15. Validation

Harness (`scratchpad/gui/gui_flows.py`, not part of the repo) drives the real `MainWindow` with **full
isolation**: QSettings → temp INI, AppData/config/data/cache/chunk-cache/crash/credentials/output/
pipeline → temp dirs (`AUTO_CUTTER_*` overrides), file dialogs → temp paths, message boxes
auto-answered and logged. A snapshot of the real registry key and of every file under
`%APPDATA%\Auto Cutter` and `%LOCALAPPDATA%\Auto Cutter` (with mtimes) is compared before/after.

23 functional checks at 1920×1080 and 2560×1440, all passing: status states; analysis produces cuts;
select → inspector; restore without dialogs; suppressed range recorded; dirty flag; undo restores the
exact cut list; redo; I/O manual cut; manual cut effective; edge resize shrink/grow (other cuts
untouched); threshold guide; zoom slider sync; EDL written; MP4 export finished, file valid
(`post_export_validator status=ok`); project save; dirty cleared; reload restores identical cuts.

Screens: `docs/gui_redesign/screens/` (before at 1920×1040, after at 1920×1080 and 2560×1440).

## 16. Known limits

* Rare (≈1 in 8 automated runs) native **hang inside Qt Multimedia during media teardown** (workspace
  reset / reopen); Windows logs it as AppHang. Seen before the threading fixes too; not reproduced
  deterministically, cause not identified.
* Gameplay detection is not runnable from the GUI yet (Analysis › Gameplay is read-only and shows the
  CLI command); the GAMEPLAY lane displays results of projects produced by the pipeline.
* Gameplay-cut ranges are not re-sliced when a video is split in the GUI (pre-existing).
* mypy on `ui/main_window.py` still reports legacy errors (190, was 213 at `HEAD`); the new modules
  have no errors besides PySide6 stub gaps for short enum names (`Qt.NoPen`, …) used project-wide.
