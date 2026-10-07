from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import zipfile

from utils.app_version import app_version
from utils.redaction import redact_secrets
from utils.subprocess_utils import run_no_window


def _redact(text: str) -> str:
    output = redact_secrets(text)
    home = str(Path.home())
    if home:
        output = output.replace(home, "%USERPROFILE%")
        output = output.replace(home.replace("\\", "/"), "%USERPROFILE%")
    username = str(os.environ.get("USERNAME", "") or "").strip()
    if username:
        output = output.replace(username, "%USERNAME%")
    return output


def _ffmpeg_version(ffmpeg_path: str | None) -> str:
    if not ffmpeg_path or not Path(ffmpeg_path).is_file():
        return "unavailable"
    try:
        result = run_no_window(
            [str(ffmpeg_path), "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
        )
        lines = str(result.stdout or "").splitlines()
        return lines[0].strip() if lines else "unknown"
    except Exception as exc:
        return f"probe failed: {type(exc).__name__}"


def create_support_bundle(
    output_path: Path,
    *,
    session_logs_dir: Path | None = None,
    crash_logs_dir: Path | None = None,
    ffmpeg_path: str | None = None,
    consistency_errors: list[str] | None = None,
) -> Path:
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.suffix.lower() != ".zip":
        destination = destination.with_suffix(".zip")

    details = {
        "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "app_version": _redact(app_version()),
        "python": platform.python_version(),
        "platform": _redact(platform.platform()),
        "architecture": platform.machine(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "ffmpeg": _redact(_ffmpeg_version(ffmpeg_path)),
        "project_consistency_errors": [_redact(str(item)) for item in (consistency_errors or [])],
    }
    privacy = (
        "This bundle contains runtime metadata and redacted application logs. "
        "Review its contents before sharing it. Media files and project files are not included."
    )

    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("diagnostics.json", json.dumps(details, indent=2, ensure_ascii=True))
        archive.writestr("PRIVACY.txt", privacy)
        if session_logs_dir is not None and Path(session_logs_dir).is_dir():
            logs = sorted(
                Path(session_logs_dir).glob("*.log"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )[:5]
            for index, log_path in enumerate(logs, start=1):
                try:
                    raw = log_path.read_text(encoding="utf-8", errors="replace")
                    archive.writestr(f"logs/session-{index}.log", _redact(raw)[-2_000_000:])
                except OSError:
                    continue
        if crash_logs_dir is not None and Path(crash_logs_dir).is_dir():
            crashes = sorted(
                Path(crash_logs_dir).glob("*.log"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )[:3]
            for index, log_path in enumerate(crashes, start=1):
                try:
                    raw = log_path.read_text(encoding="utf-8", errors="replace")
                    archive.writestr(f"crashes/crash-{index}.log", _redact(raw)[-2_000_000:])
                except OSError:
                    continue
    return destination
