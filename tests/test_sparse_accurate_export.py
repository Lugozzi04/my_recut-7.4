"""Accurate single-VOD exports seek across discarded regions; no real VOD/AI."""
from __future__ import annotations

from dataclasses import replace
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from analysis.hero_groups.projects import prepare_selected_outputs, export_outputs
from automation.manager import PipelineManager
from automation.store import PipelineStore
from export.exporter import ExportWorker
from export.settings import ExportSettings
from utils.ffmpeg import ensure_ffmpeg, ffprobe_duration_seconds
from utils.subprocess_utils import run_no_window

APP = QApplication.instance() or QApplication([])


@pytest.mark.parametrize("audio_settings", ["source", "web"])
def test_single_vod_accurate_export_seeks_large_gaps_preserves_audio_and_joins(tmp_path, monkeypatch, audio_settings):
    ffmpeg, ffprobe = ensure_ffmpeg()
    source = tmp_path / "sparse source è.mp4"
    generated = run_no_window([ffmpeg, "-v", "error", "-f", "lavfi", "-i",
        "color=blue:s=64x48:r=30:d=244,drawbox=color=green:t=fill:enable='gte(t,120)'",
        "-f", "lavfi", "-i", r"aevalsrc=0.1*sin(2*PI*if(lt(t\,120)\,440\,880)*t):s=44100:d=244",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-ac", "1", "-y", str(source)],
        capture_output=True, timeout=30)
    assert generated.returncode == 0, generated.stderr
    settings = replace(ExportSettings.defaults(), codec="libx264", quality="high", method="auto",
                       parallel_workers=1, hwaccel_decode=False, cut_quality="higher")
    if audio_settings == "web":
        settings = replace(settings, audio_codec="aac", audio_bitrate_kbps=256, sample_rate="48000",
                           channels="stereo", pixel_depth="8", color_mode="rec709")
    games = [{"id": str(i), "index": i, "result": "WIN", "start": a, "end": b}
             for i, (a, b) in enumerate(((1., 2.), (242., 243.)), 1)]
    keeps = [{"start": g["start"], "end": g["end"]} for g in games]
    payload = {"format": "autocutter_project", "version": 2, "global": {"export_settings": settings.to_mapping()},
        "tracks": [{"path": str(source), "source_id": "vod", "source_role": "gameplay", "duration": 244,
        "cuts_enabled": True, "keeps": keeps, "gameplay_mode": "keep", "gameplay_keeps": keeps,
        "gameplay_report": {"games": games, "keep_islands": [dict(k, game=g) for k, g in zip(keeps, games)]},
        "gameplay_grouping": {"games": [{"key": g["id"], "game": g, "group": "mage"} for g in games],
                             "groups": {"mage": {"class_name": "mage"}}, "selected": ["mage"]}}]}
    commands = []
    original = ExportWorker._build_cmd_filter_segments
    def inspect(self, segments, *args, **kwargs):
        command, duration = original(self, segments, *args, **kwargs)
        commands.append(command)
        return command, duration
    monkeypatch.setattr(ExportWorker, "_build_cmd_filter_segments", inspect)
    plans, _ = prepare_selected_outputs(payload, tmp_path / "outputs")
    details = []
    output = export_outputs(plans, manager=PipelineManager(PipelineStore(tmp_path / "jobs.json")),
                            on_detail=details.append)[0]["output"]
    assert any("run_bounded_accurate_keeps" in line for line in details)
    assert any("max_removed_gap=240.000s" in line for line in details)
    assert len(commands) == 2
    assert all(float(c[c.index("-t")+1]) < 10 for c in commands)  # never decode discarded 240 seconds
    assert any(float(c[c.index("-ss")+1]) >= 240 for c in commands if "-ss" in c)
    assert all(c[c.index("-crf")+1] == "18" for c in commands)  # user quality retained
    assert abs(ffprobe_duration_seconds(output)-2) < .06

    metadata = run_no_window([ffprobe, "-v", "error", "-show_streams", "-of", "json", output],
        capture_output=True, text=True, timeout=10)
    streams = json.loads(metadata.stdout)["streams"]
    audio = next(s for s in streams if s["codec_type"] == "audio")
    assert (audio["sample_rate"], audio["channels"]) == (("44100", 1) if audio_settings == "source" else ("48000", 2))
    for timestamp, channel in ((.5, 2), (1.5, 1)):
        raw = run_no_window([ffmpeg, "-v", "error", "-ss", str(timestamp), "-i", output,
            "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"],
            capture_output=True, timeout=10)
        assert np.frombuffer(raw.stdout, np.uint8).reshape(48, 64, 3).mean(axis=(0, 1)).argmax() == channel
    frame_times = run_no_window([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
        "frame=best_effort_timestamp_time", "-of", "json", output], capture_output=True, text=True, timeout=10)
    times = [float(f["best_effort_timestamp_time"]) for f in json.loads(frame_times.stdout)["frames"]]
    assert np.all(np.diff(times) > 0) and max(np.diff(times)) < .04
    decoded = run_no_window([ffmpeg, "-v", "error", "-i", output, "-vn", "-ac", "1", "-ar", "48000",
        "-f", "f32le", "pipe:1"], capture_output=True, timeout=10)
    samples = np.frombuffer(decoded.stdout, np.float32)
    for start, frequency in ((.25, 440), (1.25, 880)):
        window = samples[int(start*48000):int((start+.25)*48000)]
        peak = np.argmax(abs(np.fft.rfft(window*np.hanning(len(window)))))*48000/len(window)
        assert abs(peak-frequency) < 10
    assert np.sqrt(np.mean(samples[int(.98*48000):int(1.02*48000)]**2)) > .02
