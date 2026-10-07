# Shared pipeline implementation notes

The local working tree is the implementation baseline. The remote repository is
not used to replace any local modules or changes.

## Existing architecture (before this change)

* `main.py` starts QApplication and imports MainWindow. GUI settings use
  QSettings; presets live in Qt AppDataLocation and migrate bundled presets.
* `PipelineJob` stores DISCOVERED -> WAITING_RANGE -> DOWNLOADING -> ANALYZING
  -> READY_EXPORT -> EXPORTING -> READY_UPLOAD -> UPLOADING -> DONE. FAILED
  remembers the interrupted stage; CANCELLED is terminal.
* `PipelineStore` serializes version 1 JSON using flush/fsync and same-directory
  atomic replacement. Its original lock covers only one Python instance.
* `PipelineManager` owns transitions, progress and artifact paths. The original
  read-modify-write operations and execution ownership are process-local.
* Download, analysis and export each have a synchronous service and a single
  background worker queue. The queue callbacks in TwitchIntegration connect
  these existing services. The queue deques themselves are not durable.
* Twitch uses a public-client device grant, encrypted Windows DPAPI token files,
  and an opt-in VOD watcher. The GUI chooses the range through the manager.
* The downloader resolves streams through yt-dlp, renders the selected interval
  through FFmpeg, validates a same-directory partial and promotes it.
* The analyzer uses the shared audio service and cut engine, writes a portable
  editor project atomically and can reuse a matching project. Its original
  balanced profile uses automatic threshold rather than the GUI preset threshold.
* The exporter reads that project and uses the editor's ExportWorker, codec
  selection and settings. It already validates a same-directory partial before
  replacing the final file. A signature sidecar identifies reusable exports.
* Startup marks active stages FAILED/interrupted. It neither reconstructs queued
  READY_EXPORT jobs nor automatically dispatches recovered stages.
* YouTube states and metadata methods exist; an API uploader does not.

## Incremental implementation sequence

1. Fix blocking pipe cancellation, invalid export cache handling and stale Twitch
   authorization persistence, with regressions.
2. Add short cross-process store transactions and per-job execution locks, then
   reconstruct dispatch from persisted states without changing the state enum.
3. Extract the actual GUI preset normalization and Classic cut calculation into
   shared pure services. Persist resolved preset snapshots in job metadata.
4. Dispatch terminal commands before GUI imports. The terminal orchestrator calls
   the existing download, analysis and export services; it does not drive widgets.
5. Add official YouTube desktop OAuth and private resumable upload, encrypted
   credentials/session checkpoints, bounded transient retries and durable video
   identity. Unknown final upload outcomes must never trigger a blind insert.
6. Test original and new functionality, real local FFmpeg processing, headless
   commands, GUI smoke and executable packaging.

Runtime configuration, credentials, jobs, caches and user output are writable
user data. `project_root` and `_MEIPASS` are resource locations only. Every final
export temporary is created beside its final destination, including when the
job store and destination use different drives.

## Current architecture

`main.py` dispatches arguments before importing GUI modules. `automation/cli.py`
parses requests and renders progress; `automation/runtime.py` coordinates the
existing synchronous stage services. `ui/twitch_integration.py` uses their worker
queues and adapts callbacks to Qt signals. Both frontends use the same manager,
JSON store, execution locks, downloader, audio/cut services, presets, project
writer, ExportWorker and validators. There is no alternate CLI renderer.

`core/presets.py` owns the actual GUI catalog and normalization;
`analysis/classic.py` owns the shared threshold/cut calculation. New job metadata
stores resolved preset/export snapshots, normalized float range, delivery options
and durable artifact signatures. Jobs created before snapshots preserve the
previous legacy Classic profile and version-1 serialization remains supported.

`automation/uploader.py` adds the fourth shared stage/queue. It validates an
export before calling the official YouTube integration. Credentials and resumable
checkpoints use DPAPI files outside job JSON. The remote video id is persisted
before thumbnail, progress and DONE; unknown completion outcomes block a blind
new insert. Manual confirmed-id reconciliation is available through CLI resume.

Store transactions hold a short process-safe lock. Heavy work holds only its
job lock, plus an output lock during export; different jobs can run concurrently.
Queue reconstruction skips live executors. Explicit cancel persists CANCELLED;
shutdown/crash interruptions retain recoverable stage ownership. Transient retry
backoff also has a durable marker, so closing during an automatic retry does not
strand that job as an ordinary permanent failure.
