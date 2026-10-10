# Iteration4 frozen calibration candidate

Frozen before the single held-out73 validation run on2026-10-08. See `freeze_manifest.json` and `blind_invocation.json`; do not modify templates, gameplay code or settings after inspecting the result.

Base: previous73 calibration-only pack(its first3 games, never held-out games4–14). Accepted additions: `external74_defeat_stable.png` and `external86_defeat_late.png`; no other86 patch. Total:2VS,1VICTORY,5DEFEAT.

Threshold0.92, ambiguity margin0.04, grayscale/TM_CCOEFF_NORMED, ROI and scale grid unchanged. Single-frame VICTORY/DEFEAT OFF. Existing default app pack is not installed/replaced.

`templates.json` is the runnable pack; `single_frame_policy.json` explicitly keeps both rules disabled. Provenance snapshots: `../../74/capture_source_annotations.json` for74 and `../capture_source_annotations.json` for86.

Calibration results and final held-out outcome: `../README.md` and `../../../GAMEPLAY_VALIDATION_73.md`. No gameplay cuts or PyTorch.
