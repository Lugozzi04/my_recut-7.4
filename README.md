# Auto-Cutter

Auto-Cutter is a Windows desktop app for video cutting, timeline editing, and MP4/EDL export. It uses PySide6 for the UI and FFmpeg for media processing. Optional AI-assisted analysis lives under `analysis/`.

## Project layout

- `main.py` application entry point
- `ui/` main window and custom Qt widgets
- `audio/`, `video/`, `analysis/`, `export/` processing pipeline
- `core/` project data model
- `utils/` shared helpers
- `widgets/` reusable timeline controls
- `bin/` bundled FFmpeg binaries used at runtime
- `pretrained_models/` optional model assets for AI workflows, kept local by default
- `build/` packaging scripts
- `installer/` Inno Setup script
- `msix/` MSIX packaging templates and notes
- `store.env.example` runtime licensing variable template

## What should stay out of Git

The first commit should not include local environments or generated outputs. These are safe to leave untracked:

- `.venv310/` and `.venv_spleeter/`
- `__pycache__/`, `*.pyc`, `*.pyd`
- `build/work/`
- `dist/`
- `installer/output/`
- `msix/staging/`
- `msix/output/`
- `pretrained_models/`
- local certificates such as `*.pfx`
- scratch exports like `auto_cutter.edl`, `ff.edl`, `fix_export.patch`
- editor/workspace folders like `.cursor/` and `.vscode/`

`pretrained_models/` is optional. It is bundled by the packaging scripts if present, but it is not required for the core editor/runtime path.
By default it is not tracked in Git, so you can keep the repo lighter and add the assets only on machines that need the AI path.

## First-time setup

Recommended: Python 3.10 on Windows.

```powershell
py -3.10 -m venv .venv310
.\.venv310\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

If you use VS Code, select `.venv310\Scripts\python.exe` as the interpreter. If you run the app with the wrong Python, you will usually get `ModuleNotFoundError: PySide6`.

Run the app:

```powershell
python main.py
```

## First run

On startup the app checks the Microsoft Store entitlement/session. If no active license/session is available, it shows a message and exits. Runtime licensing variables are documented in `store.env.example`.

Typical first-run flow:

1. Launch the app with the project venv active.
2. Add or drop a video into the workspace.
3. Let the app analyze or manually mark cuts.
4. Adjust the timeline if needed.
5. Export MP4 or EDL.

## Daily use

After the first setup, the normal workflow is short:

1. Activate `.venv310`.
2. Run `python main.py`.
3. Open your project/video.
4. Edit cuts, save presets if needed, and export.

Presets and window state are remembered between runs, so you usually only need to reopen the app and continue.

## Building

Build scripts live in `build/`.

```powershell
powershell -ExecutionPolicy Bypass -File .\build\build.ps1
powershell -ExecutionPolicy Bypass -File .\build\build-installer.ps1
powershell -ExecutionPolicy Bypass -File .\build\build-msix.ps1
```

`requirements-build.txt` adds the packaging dependencies needed by the build scripts.

## Notes

- `bin/` is kept in the repo because the app expects bundled FFmpeg binaries.
- `store.env.example` is only a template; copy or export the variables you need in your local environment.
- If you want the AI path offline, keep the separate runtime setup used by `analysis/ai_pipeline.py` and add the optional model assets locally before building.

