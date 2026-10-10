# State-aware Hearthstone candidate

This is an opt-in experiment. The established global 20 FPS result scan remains the default. Neither strategy creates cuts, edits projects, or initializes the GUI. VS detection, actual-source-frame FFmpeg sampling, cancellation, analysis cache, GameEvent/GameSegment, evaluation and GameAssembler are reused.

## Run

From the source checkout, using its selected Python environment:

~~~powershell
python -m analysis.gameplay.hearthstone.debug_analyze VIDEO.mp4 --strategy state-aware --state-profile docs/gameplay_validation/state_aware/iteration5_frozen/profile.json --template-pack docs/gameplay_validation/86/iteration4_frozen/templates.json --output-dir REPORT_DIRECTORY --no-cache --debug-frames
~~~

Add --ground-truth ANNOTATIONS.json for independent evaluation, or --json for machine-readable stdout. Progress goes to stderr. Ctrl+C uses the existing cancellation token and closes the shared sampler's FFmpeg generators. Use --window-fps 30 only for controlled comparisons; changing a frozen configuration invalidates its validation claim.

The global strategy remains available with --strategy global (or no strategy option). State-aware requires an explicit profile. Global sampling overrides are rejected for state-aware rather than silently ignored. No new runtime package is required beyond requirements-gameplay.txt.

## Candidate architecture

1. A single full-video 1 FPS pass supplies unchanged weak VS proposals, two small board anchors, and weak text evidence.
2. VS candidates receive the established +/-2 s, 6 FPS refinement, at least three observations and 0.25 s support. The VS templates and its 0.92 acceptance threshold are unchanged.
3. The chronological monitor arms board transitions after VS. Two observations above 0.38 establish board presence. A fall to 0.20 or less opens a result search. The board must actually return before that trigger rearms.
4. A separate rising-edge text candidate at similarity >=0.65 and class margin >=0.04 opens a search even if the board trigger or VS is missing. Weak evidence never directly creates a result.
5. Searches cover trigger-2 s through trigger+5 s, clamped to the source. Overlapping windows merge with all trigger evidence retained. There is no periodic global high-rate result scan.
6. FFmpeg crops the result search ROI before scaling/output. Local 60 FPS sampling emits real source frames only; low-FPS sources are not duplicated into artificial support.
7. *(Iteration 5, superseded by step 7b in v2.)* Four text patches, six scales, grayscale TM_CCOEFF_NORMED, absolute score >=0.85 and class margin >=0.04 produce observations. Exact ties remain UNKNOWN. A result still requires two distinct source timestamps spanning at least 1/120 s. Single-frame acceptance remains disabled.
7b. *(Iteration 6, `hearthstone-state-aware-v2`.)* Same patches and scores, but the decision is two-stage (`result_decision.py`): frames with max(VICTORY, DEFEAT) >=0.65 form class-agnostic clusters (gap <=0.5 s); a cluster with >=2 distinct source timestamps confirms a RESULT_SCREEN; WIN/LOSS is the argmax of the cluster's peak frame. A peak margin below 0.04, or opposite-class support of >=2 frames, gives UNKNOWN (no event). Single-frame acceptance remains disabled. Rationale and blind result: [result_decision/README.md](gameplay_validation/state_aware/result_decision/README.md).
8. Same-result animation clusters consolidate into one event before the unchanged assembler. VS and observed board returns are hard barriers; contradictory VICTORY/DEFEAT events are retained. A 45 s maximum gap bounds merging when state evidence is absent.

Outcome confidence is raw normalized template correlation, not a probability. Board confidence is the minimum of two masked structural correlation scores, also not a probability. No weighted learned decision or inferred boundary is used.

## Geometry and provenance

Reference geometry is 960x540, with normalized coordinates in the profile. The two board regions are the end-turn rim (81x43 reference pixels) and hero-power frame (78x78). Their changing center content is masked out. Each has three calibration references from 74.mp4; detection uses Canny edges and a +/-8 reference-pixel positional search.

The result search region is [0.425,0.5,0.15,0.1611111111], 144x87 reference pixels. Its vertical tolerance accommodates the moving banner. Templates themselves are tightly cropped to the white/outlined glyphs, not the hero portrait, rank, reward number or overall color. Contextual banner crops were compared experimentally but add no event coverage and give a less favorable DEFEAT/negative score separation; no context gate is enabled.

Only 74.mp4 supplied quantitative text/board templates. 86.mp4 supplied independent calibration checks, negatives, FPS/trigger/representation comparisons. The supplied templates/ screenshots informed geometry only because their VOD provenance was not verified. No frame from the eleven frozen validation games of 73.mp4 supplied a template, ROI selection or parameter choice.

## Calibration experiments

The existing explicit role=calibration and source-fingerprint annotations are reused. Visible intervals are positive frames; unreviewed animation phases are not silently labeled positive or negative. An annotated result does not make every frame in its surrounding window a true positive.

~~~powershell
python -m analysis.gameplay.hearthstone.result_experiment NEW_CALIBRATION_VOD.mp4 --annotations CALIBRATION_ANNOTATIONS.json --state-profile docs/gameplay_validation/state_aware/iteration5_frozen/profile.json --output-dir CALIBRATION_REPORT --robustness --ocr-manifest
~~~

This samples each local window once, compares grayscale/CLAHE/Sobel magnitude on identical frames, and optionally applies fifteen mild perturbations to reviewed peak examples. Perturbations are evaluation cases, never extra templates. The report does not automatically select thresholds. The tool rejects validation-role annotations and mismatched source fingerprints.

OCR exports use the same 144x87 micro-ROI, upscaled 4x with cubic interpolation (576x348) for legibility. No extra context is added.

Optional Windows OCR experiment:

~~~powershell
python -m tools.gameplay.ocr_experiment CALIBRATION_REPORT/ocr_manifest.json --output CALIBRATION_REPORT/ocr_report.json
~~~

This helper uses Windows.Media.Ocr, an installed OCR language and local Windows SDK/.NET reference assemblies. It installs nothing and compiles its small C# helper in a temporary directory. It is never imported by the production detector, GUI or automation. Text normalization removes punctuation and checks the exact Italian words; it does not fuzzily force an uncertain spelling to LOSS. OCR was not selected for the candidate because the calibrated template matcher was substantially more robust to animation/rendering perturbations. Official API: https://learn.microsoft.com/en-us/uwp/api/windows.media.ocr.ocrengine.recognizeasync?view=winrt-26100

## Diagnostics and limits

Reports include source/profile fingerprints, all monitor evidence, trigger windows, board reacquisitions, accepted/rejected events, deduplication counts, frame counts, actual text/board comparison counts and sampling-wait/matching timings. VS comparison counts can be derived separately from the unchanged pack; they are not included in the explicit text/board counters. Timing is wall time, not isolated CPU time: FFmpeg decode overlaps matching, and calibration benchmarks ran with other jobs active.

Frame counts mean sampler-emitted/analyzed frames. FFmpeg still decodes interdependent source frames during its low-rate pass; these counts must not be presented as the decoder's total internal frame count. Local seeks also have GOP/startup costs.

The strategy depends on the calibrated Hearthstone layout and Italian result glyphs. A layout change, occlusion or board never being established can suppress the transition trigger; weak result monitoring is a backup, not a guarantee for an arbitrarily short screen. Unsupported languages/layouts require independent calibration. Close repeated outcomes with both VS and board-return evidence missing can remain ambiguous. A small perfect test set cannot establish production safety.

The old strategy is preserved. No gameplay-cut, timeline, TrackState, project-format, preset, exporter or PyTorch integration is included.

## Reference provenance clarification — 2026-10-09

The user subsequently confirmed that the six screenshots in templates/ come from another VOD which has not yet been downloaded. This is user-attested external provenance; the source URL/ID, frame timestamps and video fingerprint remain unavailable. They are candidates for a future independent calibration cycle. At freeze time they informed general geometry only. No frozen asset, parameter, published metric or blind validation was changed. See [reference inventory and provenance](gameplay_validation/state_aware/reference_images_provenance.json).
