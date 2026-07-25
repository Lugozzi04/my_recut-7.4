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
mypy core/project_file.py core/project_session.py utils/codec_detection.py analysis/cancellation.py
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
- verifies every native command exit code;
- reads the version from `VERSION`;
- prepares and verifies FFmpeg source/license assets;
- creates `dist\AutoCutter\AutoCutter.exe`;
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
