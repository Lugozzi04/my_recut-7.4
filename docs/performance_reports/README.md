# Gameplay performance profiling

Profiling starts automatically with GUI Detect Games, on its background worker. It does not change algorithms,
FPS, thresholds, caches, boundaries, grouping, rendering or packaging.

## Two manual runs

1. Restart ReCut to load the instrumentation, then import the approximately one-hour VOD normally.
2. Let Classic finish. Run Analysis / Detect games, Complete wins only, Exact, Balanced. No export is needed.
3. Preserve the JSON and repeat with the seven-hour VOD, identical options and similar background workload.
4. Compare reports only after checking cache states. The profiler does not invalidate caches or force a cold run.

Reports: gameplay_profile_YYYYMMDD_HHMMSS_<run-id>.json (UTC timestamp and unique UUID).
Source runs write to this checkout's docs/performance_reports/, independent of CWD.
Packaged builds write to writable app data/performance_reports/, never the PyInstaller extraction directory.
No actual analysis/report was generated during implementation.

## Schema and interpretation

- run_id, profiler_version, UTC dates, status: running, completed, failed or cancelled.
- video: filename only, file size, duration/resolution, FPS/codec from the existing FFprobe invocation.
- modes: GUI choices, sampling rates, detector/profile/pack identifiers and evidence options when reached.
- system: logical CPU cores and Windows physical RAM when its inexpensive API is available.
- decoder: configured FFmpeg backend/hardware request; actual decoder implementation is not independently observed.
- phases: inclusive wall_seconds, calls, aggregate counters, frames/second, mean seconds per attempted window,
  and other_wall_seconds after directly named child timers. Parent/child times overlap: do not add them together.
  Residual includes orchestration and timer overhead; it is not an independently isolated operation.
- classic_audio: latest observed AnalyzeWorker measurement for the unchanged source in this app session.
  Classic is separate and excluded from Detect Games total. If not observed, this is explicit; no duration/cache
  hit is invented. Import after restarting to capture it. RMS blocks are audio blocks, not video frames.
- error: exception type and safe code only, never exception messages. interruption_phase identifies the deepest
  measured failure in a failed/cancelled run. A completed run can retain a failed subphase handled by fallback.

## Cache and skipped work

Detection/hero_groups cache fields distinguish hit, miss, disabled and pending lookup. Cache-hit detection marks
monitor/refinement/assembly skipped; cached_games/events are explicitly labeled, without copying historical
processing times or frame counts. Hero cache hits skip recognition/sampling but still measure lookup and edit
reconciliation. Transcript cache records cached/uncached windows, lookup observations and an aggregated
hit/miss/partial state across all boundary calls. Inference is marked skipped for disabled/unavailable/cached
calls when no inference ran. Phases without candidates/WINs or not reached have explicit skip reasons.
A cached total must not be compared as equivalent work to a fully recalculated run. Keep cached reports labeled
as such; a controlled cold-run procedure is a separate step, not a cache policy change made by this profiler.

## Measurements

- Classic: total, RMS extraction, audio pipe-read wait, PCM samples/blocks and threshold selection.
- Detector setup, identity/probe/cache I/O.
- Global monitor: frames, delivery/queue/PTS wait, normalization, VS, board and result matching, image preparation,
  visual comparison counters and candidate observations. Internal preparation is included in matching wrappers.
- VS/result refinement: attempted/completed windows, frames, comparisons, candidates and mean window time.
- Window planning, event consolidation/deduplication, game assembly, complete games and WIN/LOSS/UNKNOWN counts.
- Boundaries: evidence total; audio preparation; visual frame extraction/difference; transcript cache lookup,
  runtime discovery, WAV extraction and ASR helper wall time; candidate generation and Exact selection.
  Existing evidence work is measured even when Exact chooses the anchors; none is removed.
- Hero groups: reference/identity/cache work, sampling/readability, signatures, Hero Power comparisons,
  classification/grouping/cache write, frames, games and comparison counts.

## Timing limits and lifecycle

Clocks use perf_counter(). Frame delivery wait includes startup/seeking, FFmpeg decode/filter work, pipe/queue
and timestamp availability. It is not decoder-only CPU time. FFmpeg decoding overlaps OpenCV matching.
Sample counts do not count internally decoded dependency frames. The ASR helper timer combines startup,
imports/model load and inference. Concurrent remote resolver internals are not timed inside provider threads;
existing agent/cache counters and enclosing boundary wall time remain available. Profiling invokes no provider.

Small aggregate snapshots are atomically replaced approximately every 30 seconds at worker progress points.
No per-frame logging, scores, screenshots, transcript, absolute paths, video contents, full-video hashing or
extra media processing is added. Writes happen on the worker, not the GUI thread. A final report is saved on
normal completion, error or cancellation. Forced termination/power loss can leave only the latest running
snapshot. During long blocking helper calls that snapshot can be older than 30 seconds; normal cancellation
still unwinds and saves final measurements. Diagnostic write failures do not abort analysis; unwritable/disk-full
storage can leave a missing or stale report.

No speedup or measured instrumentation overhead is claimed. No tests, benchmarks, smoke tests or VOD analyses
were run during implementation. Only source review, AST parsing, targeted lint and static typing were performed.
