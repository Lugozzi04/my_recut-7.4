# Gameplay Timeline editing

The existing Gameplay Editing fields and exporter are reused. Detection reports,
WIN/LOSS, Smart Boundaries, class recognition and Adaptive V2 are unchanged.

## Interaction

- In the selection tool, click the interior of a game box on the **VIDEO lane**
  to include/exclude it. An excluded game remains visible in grey with a dashed
  outline and an `excluded` label; click again to restore it.
- Drag an edge to adjust START/END. This does not toggle inclusion.
- The **Include** checkbox at the right of each Hero Groups row performs the
  same operation. Clicking the other row columns still controls the yellow
  view-only highlight. The yellow band is never saved as an export choice.
- Right-click a box for include/exclude, assignment to an existing group,
  assignment lock, and a precise START/END dialog.
- Assignment uses the existing group IDs. Portrait groups A/B can remain
  distinct even if both have Hunter as class evidence. The menu shows both
  identities where applicable. Choosing an assignment locks it; unchecking
  the lock lets a later automatic recalculation assign it again.
- Right-click a free area **inside a source clip** for **Create manual game /
  interval**. Enter START and END as source-video seconds (three decimals) and
  choose an existing group. The result is UNKNOWN, inclusion is explicit.
- A manual interval can be edited with the same dialog or edge handles,
  excluded without deleting it, or deleted with confirmation. Detected games
  cannot be deleted by these actions.
- Existing cut, split, clip and ruler actions remain available. On the AUDIO
  and CUTS lanes a left click continues to edit/select cuts.

## Policy and validation

Complete WINs Only supplies the automatic default. Explicit inclusion is a
manual editorial override, including for a LOSS or a manual UNKNOWN; it never
changes the detected result. Incomplete detected games cannot be included until
both boundaries have been set. Estimated display bounds are not export bounds.

START/END must be finite and satisfy `0 <= START < END <= source duration`.
New/edited intervals overlapping another complete effective game are rejected,
including overlaps with excluded games. Adjacent intervals are allowed.
An automatic partial interval with an unknown boundary does not claim its
estimated display span as an authoritative boundary.

Selected intervals are mapped into each source clip's local coordinates.
Existing silence/manual cuts remain inside included games. Explicit exclusions
also outrank Restore Cut and remain exclusions when the automatic keep policy
is turned off. Re-enable the game checkbox to undo an exclusion.

## Persistence and exports

Overrides live in the already persisted `gameplay_grouping` dictionary:

```json
{
  "overrides": {"detected-game-id": "hunter"},
  "editor": {
    "version": 1,
    "included": {"detected-game-id": false, "manual-id": true},
    "boundaries": {"detected-game-id": [123.25, 456.75]},
    "manual_games": []
  }
}
```

All boundary values use original source seconds. `manual_games` contains
UNKNOWN game records with stable UUID IDs and their chosen groups.
The detected `gameplay_report`, original results, and detected keeps are
retained. Existing project serialization and undo snapshots already copy these
fields; no project format version bump is needed.

The editor derives `gameplay_keeps`, `gameplay_islands` and their complementary
cuts from these overrides, and reuses the current recomputation/export path.
Group exports intersect the same current islands with final keeps. Excluded
games cannot reappear in a class output. An empty group is omitted. Outputs
keep their existing independent project/output identities and chronological
ordering; no rendering pipeline is added.

Automatic GUI grouping and the project-based grouping command preserve the
editor section and manual rows. Locked assignments use the existing
`overrides` map. Overrides for detected games are keyed by the detector's
stable game ID: if a future detector produces different IDs, old overrides are
retained but are not heuristically transferred to a different game.

Reset boundaries resets boundary overrides only; it does not clear explicit
inclusion or assignment decisions. Manual interval boundaries reset to their
creation values. Older projects use their current edited keeps when first
adopted, so previously removed games are not resurrected.

## Verification

`tests/test_gameplay_timeline.py` exercises real offscreen Qt mouse events,
checkbox synchronization, yellow-highlight independence, source-time edits,
precise dialogs, manual deletion confirmation, save/restore GUI helpers,
manual-row preservation through recalculation, split clips, explicit policy
overrides, Restore Cut protection, group export and child-project isolation.

No VOD analysis, AI request or long render is needed for these checks.

Final checks for this change:

- Pertinent regression suite (Gameplay, Adaptive/V2, groups, exports, packaging
  outputs, timeline, projects and GUI components): **605 passed, 0 failed,
  0 skipped**, including **20 new gameplay timeline tests**.
- Ruff on all changed Python modules and the new tests: passed.
- Mypy on selection, group-project adapter, timeline controller and Hero Groups
  panel: passed (4 files).
- Extended mypy on the existing timeline widget still reports 64 errors across
  the widget and its glyph dependency. Checking the unmodified HEAD version
  reports the same 64 errors; this broader legacy typing check is not clean.
- `python main.py --smoke-test`: passed.
- `git diff --check`: passed.
- Frozen detector artifacts and tests were not edited. No complete VOD
  analysis, long render, paid service call or commit was performed.

## Selected export policy

The filtered Gameplay export has an additional **Complete WINs only** option. When enabled, it excludes LOSS/UNKNOWN even if manually included on the timeline. Turn it off to export those explicit inclusions. Class selection remains an independent filter; it never clears individual inclusion overrides. See [multi-source gameplay export](MULTI_SOURCE_GAMEPLAY_EXPORT.md).
