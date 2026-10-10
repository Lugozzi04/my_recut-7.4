# Experimental candidates from86 — not the accepted pack

This directory preserves the previous base pack plus three DEFEAT patches captured from calibration86, game5. `external86_defeat_early` and `external86_defeat_mid` were rejected: no independent ≥0.92 coverage gain in74. `external86_defeat_late` was accepted after cross-VOD phase coverage increased on74 without strong negative candidates.

Do not use this experimental pack for frozen validation or editor cuts. Use `../iteration4_frozen/templates.json` for the accepted configuration; that pack also includes the independently verified stable74 variant.

Patch companion files record role, source fingerprint, exact source PTS, normalized capture ROI, annotation snapshot SHA256 and image SHA256. Snapshot: `../capture_source_annotations.json`. No73 held-out image was used. Full analysis: `../README.md`.
