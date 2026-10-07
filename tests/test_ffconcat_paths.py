from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from analysis.cut_engine import Segment
from export.exporter import ExportWorker
from utils.ffconcat import quote_ffconcat_path
from utils.ffmpeg import ensure_ffmpeg, ffprobe_duration_seconds
from utils.subprocess_utils import run_no_window


@pytest.mark.parametrize("unsafe", ["line\nbreak.mp4", "line\rbreak.mp4", "nul\x00.mp4"])
def test_concat_rejects_control_characters(unsafe: str) -> None:
    with pytest.raises(ValueError):
        quote_ffconcat_path(unsafe)


def test_concat_uses_ffmpeg_apostrophe_escape() -> None:
    assert quote_ffconcat_path("C:\\Videos\\l'estate è 🎬.mp4") == "'C:/Videos/l'\\''estate è 🎬.mp4'"


def test_real_export_concat_accepts_spaces_unicode_and_apostrophes(tmp_path: Path) -> None:
    ffmpeg, _ffprobe = ensure_ffmpeg()
    directory = tmp_path / "l'estate è 🎬"
    directory.mkdir()
    source = directory / "video d'oggi.mp4"
    output = directory / "risultato finale.mp4"
    generated = run_no_window(
        [
            ffmpeg, "-hide_banner", "-v", "error", "-y", "-f", "lavfi", "-i",
            "testsrc2=size=160x90:rate=10:duration=2", "-f", "lavfi", "-i",
            "sine=frequency=440:sample_rate=48000:duration=2", "-c:v", "libx264", "-preset",
            "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source),
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8", timeout=30,
    )
    assert generated.returncode == 0, generated.stderr
    worker = ExportWorker(
        ffmpeg_path=ffmpeg, input_path=str(source), output_path=str(output),
        keeps=[Segment(0.0, 1.0)], codec="libx264", parallel_workers=1,
    )
    temporary_files: list[str] = []
    try:
        command = worker._build_cmd_concat_copy_to_ts(
            [(0.0, 0.8), (1.0, 1.8)], str(output), temporary_files, str(source), audio_copy=True,
        )
        exported = run_no_window(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8", timeout=30,
        )
        assert exported.returncode == 0, exported.stderr
        assert ffprobe_duration_seconds(str(output)) > 1.0
    finally:
        for temporary in temporary_files:
            Path(temporary).unlink(missing_ok=True)
