# Auto Cutter

Auto Cutter is a Windows desktop editor for automatic voice cuts, timeline
review, MP4 rendering, and EDL export. The UI is built with PySide6 and media
processing uses separate FFmpeg command-line programs.

## Requirements

- Windows 10 or later, 64-bit
- Python 3.10 for source development
- About 500 MB for the core development environment
- Optional AI runtime: more than 2 GB depending on installed model packages

## First setup

```powershell
py -3.10 -m venv .venv310
.\.venv310\Scripts\Activate.ps1
python -m pip install --requirement requirements.txt
python main.py
```

In VS Code, select `.venv310\Scripts\python.exe`. Running with another
interpreter is the usual cause of `ModuleNotFoundError: PySide6`.

## Workflow

1. Add or drag one or more videos.
2. Choose Classic analysis, or install and select the optional AI mode.
3. Review generated cuts on the timeline.
4. Save the `.autocutter` project.
5. Export MP4 with `Codec: Auto`, or export EDL.

Projects use relative media paths where possible. Missing media is retained as
offline state and can be searched in another folder when the project is opened.

## Shared automation pipeline

The headless automation engine is available in `automation/`. It stores a
versioned job queue atomically and validates every transition from VOD discovery
through download, analysis, export, and upload. Startup reconciles source,
project and export artifacts, reconstructs queues (including READY_EXPORT) and
resumes interrupted work. Explicitly cancelled and permanent failed jobs stay
stopped until requested. Per-job operating-system locks prevent two GUI/CLI
executors from processing the same job.

The default queue is stored in the user-local application data directory.
`AUTO_CUTTER_PIPELINE_STORE` can select another JSON file for development or
portable deployments.

The Twitch connector uses the public-client Device Code Flow and never embeds a
client secret. Access and refresh tokens are encrypted for the current Windows
user with DPAPI; `AUTO_CUTTER_TWITCH_TOKEN_FILE` can move the encrypted token
file. The VOD watcher is opt-in and performs no network activity until started
explicitly, so the original manual editor workflow remains unchanged.

To enable Twitch discovery from the desktop UI:

1. Register a public Twitch application that supports Device Code Flow and copy
   its Client ID. A client secret is not required or stored by Auto Cutter.
2. Open the top-right three-dot menu, select `Twitch automation`, then
   `Set Client ID...`.
3. Select `Connect Twitch account...`; the browser opens the activation page and
   Auto Cutter copies the displayed code to the clipboard.
4. Enable `Watch for new VODs`, or use `Check for a new VOD now` for a one-off
   check. The watcher preference is restored on the next normal launch.
5. When a VOD is found, enter start and end times. Cancelled selections remain
   available under `Configure pending VOD...`.

After the range is confirmed, Auto Cutter uses yt-dlp to resolve the best Twitch
stream and FFmpeg to copy only the selected interval without re-encoding. The
three-dot menu can change or open the download folder, cancel the current
download, and retry failed downloads. The folder defaults to
`Videos\Auto Cutter\Automation`; `AUTO_CUTTER_DOWNLOAD_DIR` can override it.

Completed downloads are validated and automatically passed to the same classic
audio-analysis engine used by the editor. Auto Cutter applies the balanced
GUI-selected preset and stores the generated cuts and keeps in a portable `.autocutter`
project next to the downloaded video. A valid project from a previous attempt
is reused when its source file has not changed.

The generated project is then exported in the background with the same render
engine and saved export settings used by the editor. Automatic delivery always
creates one video and validates its video/audio streams. New GUI jobs retain
the download-folder destination; CLI jobs can choose a different output drive.
Jobs without YouTube delivery finish at DONE; opted-in jobs advance through
READY_UPLOAD and UPLOADING to DONE. Existing legacy READY_UPLOAD jobs remain
compatible. A
signature sidecar allows a completed matching export to be reused safely after
a retry. Render configuration and FFmpeg details are recorded in application
logs.

The `Twitch automation` menu can cancel or retry analysis and export, open a
project that is waiting for export, and open the latest exported video. The
current manual workspace is never replaced automatically. Manual import,
analysis, editing, project save/load, and export continue to work independently
as before.

The same services also run without a display or GUI widgets:

```powershell
python main.py --help
python main.py presets
python main.py jobs
python main.py process "C:\Videos\vod.mp4" --preset "Balanced (Default)"
python main.py vod "https://www.twitch.tv/videos/1234567890" `
    --start 00:42:00 --end 03:15:00 `
    --preset "Balanced (Default)" --output-dir "D:\Videos\ReCut" --youtube
python main.py resume JOB_ID
```

Authenticate once using `python main.py auth twitch --client-id YOUR_CLIENT_ID`
and `python main.py auth youtube --client-config "C:\Private\desktop-client.json"`.
YouTube uses official desktop OAuth, encrypted refresh tokens, resumable chunks
and PRIVATE uploads only. Publish manually in YouTube Studio. Saved video ids
prevent a new insert on resume; an uncertain expired final upload requires manual
reconciliation instead of risking a duplicate.

The packaged `AutoCutter.exe` accepts the same commands. No arguments open the
editor. See [CLI commands, paths and exit codes](docs/CLI.md) and
[YouTube setup and crash recovery](docs/YOUTUBE.md). `--dry-run` plans a request
without processing; `--json` keeps progress on stderr and returns one JSON result
on stdout. Preset configuration and export settings are snapshotted per job.

Local implementation details, verification results and remaining account/build
checks are recorded in [the implementation report](docs/IMPLEMENTATION_REPORT.md).

## Optional AI runtime

The AI runtime is not copied from a developer virtual environment and is not
part of the core installer. Create the pinned local runtime with:

```powershell
powershell -ExecutionPolicy Bypass -File .\build\install-ai-runtime.ps1
```

This creates the ignored `ai_runtime/` directory from `requirements-ai.txt`.
`AUTO_CUTTER_AI_PY` can point to another verified Python interpreter.
Spleeter models are kept in `ai_runtime/pretrained_models/` by default and are
downloaded on first use; `AUTO_CUTTER_AI_MODELS` can select another location.

## Development checks

```powershell
python -m pip install --requirement requirements-dev.txt
ruff check .
mypy automation analysis/audio_service.py analysis/classic.py analysis/cancellation.py core/project_file.py core/project_session.py core/config.py core/presets.py integrations utils/codec_detection.py utils/runtime_paths.py utils/console.py utils/credential_files.py utils/ffconcat.py utils/redaction.py utils/ffmpeg.py
pytest
```

The Windows GitHub Actions workflow runs compilation, lint, type checks, unit
tests, cancellation tests, and a real FFmpeg export integration test.

## Build

```powershell
powershell -ExecutionPolicy Bypass -File .\build\build.ps1
powershell -ExecutionPolicy Bypass -File .\build\build-installer.ps1
```

The build:

- removes stale target artifacts;
- checks pinned runtime/build dependencies before removing an existing bundle;
- verifies every native command exit code;
- reads the version from `VERSION`;
- prepares and verifies FFmpeg source/license assets;
- creates `dist\AutoCutter\AutoCutter.exe`;
- runs isolated packaged GUI and CLI smoke tests, including redirected JSON stdout;
- creates `installer\output\AutoCutterSetup.exe`.

For a signed release, pass `-PfxPath` and `-PfxPassword` to
`build-installer.ps1`. Both the application executable and installer are signed
and verified.

## Diagnostics

Use `Settings > Help > Create diagnostics bundle` to create a ZIP containing
runtime metadata and redacted logs. Media and project files are not included.
Review the ZIP before sharing it.

## Repository hygiene

Virtual environments, models, caches, certificates, generated builds, exports,
and third-party release assets are ignored. `bin/*.exe` is configured for Git
LFS. Existing large blobs in old Git history require a separate history
migration if reducing clone size is necessary.

## Third-party software

See `THIRD_PARTY_NOTICES.md` and `docs/FFMPEG_DISTRIBUTION.md`. The included
Gyan FFmpeg 8.0.1 essentials binaries report GPLv3-or-later. Release builds
include the corresponding FFmpeg source archive and GPL text. Obtain legal
review before public distribution, especially for statically linked external
libraries.

## Hearthstone gameplay analysis (video-only prototype)

An optional headless prototype detects VS / VICTORY / DEFEAT and reconstructs games.
It produces diagnostic JSON without changing editor cuts. Real templates and VOD
calibration are required; see [setup, capture and validation](docs/GAMEPLAY.md).

```powershell
python -m pip install -r requirements-gameplay.txt
python -m analysis.gameplay.hearthstone.debug_analyze "video.mp4" --output-dir "gameplay-debug"
```
