# Shared analysis audit and implementation

The local working tree is the source of truth. Historical cold 7h02 baseline: Detect Games 1081.55s, monitor 634.60s, boundaries 259.87s, hero grouping 67.40s; Classic separately 68.51s. These are historical measurements, not new benchmarks.

| Producer | Existing artifact | Consumers / sharing gap |
|---|---|---|
| Classic audio_service | float32 voice-filtered RMS, duration, hop, threshold (formerly RAM only) | Classic, silence engine; local boundary and Packaging extraction previously re-decoded identical requests |
| FasterWhisperEngine | boundaries/transcripts/<key>.json, original-source word timestamps | Boundary resolvers and Packaging already share engine; exact-window keys did not reuse covered overlapping requests |
| State-aware analyzer | gameplay/hearthstone/state-aware/<key>.json: events, games, board/result observations, ROI/window evidence | Gameplay, Packaging games_from_detector already uses this same cache; retain it |
| HeroGroupingService | hero_groups/<key>.json: samples/portrait previews/classes | GUI and group outputs; automatic cache remains separate from project manual overrides |
| FFmpegFrameSampler | actual source PTS and resized BGR frames, formerly transient | VS/board/results, boundaries, hero groups, Packaging |
| Boundary visual analyzer | small local visual-change samples, formerly transient | Exact/Claude/Codex evidence; persistent request-compatible reuse added |
| Packaging FrameExtractor | packaging/frames/<key>.npz JPEG bursts, source-based, before keep filtering | Thumbnail/webcam/cards; preserve this existing cache and quality contract |
| EditMap | project planner final_keeps/source pieces -> edited time | Packaging, per-group projects; this remains the authoritative TimelineMapper |
| Packaging transcript builder | per-edit filtered lines and word mapping | Titles/highlights/thumbnails; raw ASR remains source-based, edited views output-specific |

Compatibility matters: a whole-file voice-filtered RMS series is not numerically identical to a separately sought window (filter warm-up and block origin); Packaging also uses 50ms blocks whereas Classic uses 30ms. Share exact compatible extraction requests, expose native RMS evidence, and do not silently resample/relabel these series. Keep existing editorial algorithms unchanged.

Whisper remains enabled in Exact. New coverage reuse must retain source timestamps, distinguish untranscribed intervals from empty/silent transcripts, and transcribe only missing ranges. Old exact-window caches remain readable. No full PCM file is persisted.

## Implemented adapters

- `analysis/shared/audio.py`: exact native RMS artifacts in `cache_root()/audio_evidence`, keyed by bounded source identity, filter, sample rate, hop, seek range, schema/algorithm. Both full Classic and local boundary/Packaging requests pass through the existing audio service. PCM is never persisted. Different filter origins/hops deliberately do not share an incompatible series.
- `analysis/shared/transcript.py`: coverage references under the existing `boundaries/transcripts/coverage/<source+options>/`; text/words remain in the existing per-window JSON. Locking reuses `automation.locking.ProcessFileLock`. Exact old keys work unchanged and are registered lazily. Covered intervals (including silence) do not load Whisper; only the missing union is transcribed. Missing legacy metadata cannot be inverted from a hash: legacy overlapping windows become discoverable when their exact key is requested.
- `analysis/shared/visual.py`: adapter to existing GameplayCache; new compact boundary visual-change artifacts under `boundaries/visual`, keyed by source, interval, FPS/width and algorithm version. No global frame sequence is persisted. Board/result/ROI metadata stays in the original detector artifact.
- `analysis/shared/frames.py`: selective lossless source frame cache under `selected_frames`, keyed by source, requested timestamp, 100ms first-real-frame selection, width and algorithm/schema. Actual PTS is retained. Low-resolution evidence cannot answer a high-resolution request. Nearby sparse requests can use FFmpeg select batching, with the original single seek as fallback; cancellation never retries. Packaging continues to use its existing source-based JPEG burst cache, before keep filtering.
- `analysis/shared/timeline.py`: alias to the existing `EditMap`, used by Packaging frames/transcript. Export planner final_keeps remains authoritative, including manual gaps and per-group edits; there is no new mapping algorithm.

Source-native artifacts do not depend on manual cuts, group names/selections, Packaging prompt/provider or thumbnail text. Edit-dependent filtered transcript/energy views and output assets keep their existing per-output identities. Changing source/config invalidates only the corresponding source artifact.

## Profiling

New aggregate nodes `shared.audio`, `shared.visual`, `shared.transcript`, `shared.frames` record hits/misses, requested/cached/decoded frames, batch/single/fallback sessions and source interval coverage. `sampling.streams` continues counting actual FFmpeg sampling processes. Covered ASR runtime/extraction/inference is marked skipped, never a fictitious duration saved. Nested wall times are inclusive and must not be summed. No per-frame files/logs were added to the global detector.

## Deliberate limits

Whisper remains enabled for Exact; this change does not claim that existing boundary-only transcripts cover the entire kept game. Packaging transcribes the previously uncovered interiors when needed, then later outputs reuse them. ASR segmentation/context on a newly transcribed gap can differ from transcribing a large window in one call; source words are clipped with half-open midpoint ownership at coverage seams. Whole cached/exact segments preserve original text. No speech or content is invented.

No hardware decoding, matcher prefilter, FPS/threshold change, new neural model, provider call, or unconstrained concurrency was introduced. The seven-hour baseline has not been rerun.

GameplayEditing also prepares all publishable WIN boundary transcript windows in one native Whisper batch (cache enabled only), so the helper/model does not reload once per game. Window construction uses the same search_window settings/neighbors; per-game evidence then consumes cached source transcripts. Disabled/custom engines retain their previous contract. Failures preserve the original per-game fallback, cancellation propagates. This batches work, not threads.

## Measured checks (this iteration)

Benchmarks are sequential, short and offline. No full VOD analysis, ASR model execution, agent/provider call or model download. Reports retain unsuccessful optimization evidence as well as the final configuration.

| Case | Previous path | Final path | Warm shared cache |
|---|---:|---:|---:|
| Synthetic 16s 1080p60, six samples <=2.7s apart | 0.507s / 6 FFmpeg processes | 0.294s / 1 process (includes persistence) | 0.047s |
| Real 72s stream-copy clip from videoprova, ten samples ~6.7s apart | 1.117s / 10 processes | 1.409s / 10 processes (includes persistence) | 0.124s |
| Synthetic Classic RMS | 0.159s cold | same exact float32 series | 0.071s |
| Frozen matchers, six synthetic frames (median of 3) | 0.0520s | 0.0485s | n/a |

Pixel values and source timestamps matched independent seeks exactly, including nonzero-origin/VFR regression fixtures. Frozen matcher scores/events also matched. The real clip is 72 seconds copied from the VOD, not a full detector run or accuracy validation. Small timings are illustrative, not statistically strong or extrapolations to seven hours.

The initially evaluated 8s batch gap reduced ten processes to three but took 2.062s on the real clip. It was **not** adopted. Default max_gap_s is conservatively 3s, max_span_s 24s: sparse distant requests retain independent seeks. Cold Hero retrieval on the representative spacing is not yet faster; persistence adds measured overhead, while later requests avoid decoding. This limitation is intentionally visible.

Two separate Packaging edits read pre-populated source ASR with zero helper/model invocations. This demonstrates reuse and filtering, not real Whisper inference speed. A native-Whisper mocked integration verifies one helper for multiple WIN boundary windows and no helper during subsequent per-game evidence retrieval.

Final benchmark artifacts:

- [synthetic fixture](performance_reports/shared_fixture_final_80461e4f.json)
- [real short clip](performance_reports/shared_real_clip_final_be69d0e4.json)

Reproduce synthetic checks: `.venv310/Scripts/python.exe scripts/benchmark_shared_analysis.py --output docs/performance_reports/shared_fixture_new.json`. Add `--hero-spacing` for the 72s synthetic spacing experiment. Do not overwrite existing evidence files.

No benchmark of an entire cold/warm seven-hour Gameplay run has been performed; the historical baseline remains unchanged. Larger-scale global FFmpeg/OpenCV optimization and distant-seek Hero decoding remain open, rather than weakening sampling or detection.

## Files changed by this iteration

Existing: `analysis/audio_service.py`, `analysis/boundaries/{analyzer,transcript,visual}.py`, `analysis/gameplay_editing.py`, `analysis/gameplay/hearthstone/state_aware.py`, `analysis/gameplay/hearthstone/visual/{sampler,detector}.py`, `analysis/hero_groups/recognition.py`, `analysis/packaging/{frames,transcript}.py`. Earlier profiling changes in other files were preserved.

Created: `analysis/shared/{__init__,arrays,audio,frames,transcript,visual,timeline}.py`, `tests/test_shared_analysis.py`, `scripts/benchmark_shared_analysis.py`, this audit/report, and the explicitly named benchmark JSON files under `docs/performance_reports`.

## Final quality gate

- `python -m pytest -o addopts= -q`, with `AUTO_CUTTER_CACHE_DIR` set to an isolated writable temporary directory: **1159 passed, 0 failed, 1 skipped in 191.00s**. Includes 22 new shared-analysis tests.
- `python -m ruff check .`: passed.
- Project CI mypy scope plus shared modules, changed boundary/gameplay/hero/Packaging modules: **60 source files passed**.
- `python main.py --smoke-test`: exit 0.
- SHA-256 audit: all **32** files in state-aware iteration5_frozen and 86 iteration4_frozen unchanged. Frozen tests were not edited.
- Initial non-isolated suite: 1154 passed, 1 failed, 1 skipped. The existing smart-hybrid export test could not create AppData cache/splice_points (WinError 5); with a writable isolated cache the same test and the entire suite passed. No exporter/test-expectation workaround was introduced.
- No commits, user-cache deletion, provider calls or model downloads.
