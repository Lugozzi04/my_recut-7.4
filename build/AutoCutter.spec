# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules


try:
    # SPECPATH is provided by PyInstaller at spec runtime (directory of this .spec file).
    project_root = Path(SPECPATH).resolve().parent
except NameError:
    project_root = Path.cwd()
main_script = project_root / "main.py"
icon_path = project_root / "icons" / "logo" / "app.ico"

datas = []
binaries = []
hiddenimports = []

# Project assets
for folder in ("icons", "webui", "bin"):
    source = project_root / folder
    if source.exists():
        datas.append((str(source), folder))

for optional_folder in ("pretrained_models",):
    source = project_root / optional_folder
    if source.exists():
        datas.append((str(source), optional_folder))

presets_file = project_root / "presets.json"
if presets_file.exists():
    datas.append((str(presets_file), "."))

# Third-party runtime assets
datas += collect_data_files("av")
binaries += collect_dynamic_libs("av")
hiddenimports += collect_submodules("av")

try:
    datas += collect_data_files("winsdk")
    hiddenimports += collect_submodules("winsdk")
except Exception:
    pass

# Qt modules used dynamically
hiddenimports += [
    "PySide6.QtSvg",
    "PySide6.QtSvgWidgets",
    "PySide6.QtMultimedia",
    "PySide6.QtPrintSupport",
    "PySide6.QtWebChannel",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
]

# Keep installer lean: these large ML stacks are optional in dev workflows
# and are not required for the core desktop login/edit/export runtime.
excluded_packages = [
    "tensorflow",
    "tensorboard",
    "keras",
    "torch",
    "torchaudio",
    "torchvision",
    "onnx",
    "onnxruntime",
    "sklearn",
    "pandas",
    "grpc",
    "h5py",
    "sentencepiece",
]

a = Analysis(
    [str(main_script)],
    pathex=[str(project_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excluded_packages,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AutoCutter",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(icon_path) if icon_path.exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="AutoCutter",
)
