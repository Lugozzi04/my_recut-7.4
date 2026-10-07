# Changelog

## 1.0.0

- Added terminal presets/jobs/process/vod/resume/auth commands over the shared
  GUI pipeline, with preset snapshots, dry-run, JSON results and stable exit codes.
- Fixed blocking audio cancellation, partial export reuse, filename safety,
  output collisions and stale Twitch authorization after logout.
- Added process-safe job/store/output locking and artifact-aware startup queue
  recovery, including interrupted transient retry backoff.
- Added official desktop YouTube OAuth and encrypted resumable PRIVATE uploads,
  durable video-id checkpoints, optional thumbnails and conservative duplicate
  prevention when the remote outcome is unknown.
- Added isolated source GUI/CLI media checks, Google packaging hooks, console
  dispatch and dependency checks before release target cleanup.
- Added automatic hardware codec probing with software fallback.
- Added portable versioned projects, offline media preservation, and relink.
- Added cancellable optional AI runtime with pinned dependencies.
- Added deterministic build, installer validation, and signing verification.
- Added ProjectSession domain ownership and project consistency checks.
- Added Windows CI, lint, type checks, and real media export tests.
- Added FFmpeg source/license packaging and third-party notices.
- Added onboarding, diagnostics, crash logs, accessibility metadata, and
  English/Italian core UI strings.
- Removed account, purchase, licensing, and Microsoft Store integrations.
- Added optional Twitch VOD discovery with encrypted Device Flow credentials.
- Added cancellable, retryable range downloads using yt-dlp stream resolution
  and FFmpeg stream copy without video re-encoding.
- Added cancellable automatic audio analysis after Twitch downloads, including
  portable project generation, retry-safe reuse, and explicit opening in the editor.
- Added cancellable automatic background export after analysis, using the editor
  render engine, persisted delivery settings, validated output, retry-safe cache,
  detailed render logs, and UI actions for retry/cancel/open.
