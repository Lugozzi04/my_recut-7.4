"""Regression: exported cuts must show every kept frame once, in order, with audio in sync.

Root cause fixed (videoprova, smart_hybrid via method=auto): the splice-point scanner sniffed MP4 packets for an
Annex B start code; a small B-frame whose 4-byte NAL length is 00 00 01 xx was read as an IDR. A stream-copy piece
started on that "keyframe" really started at the previous one, so 0-2 s already shown were played again.
The smart pieces also overran by a few frames (B-frame reorder, encoder padding) and carried per-piece AAC that
drifted against the picture at every splice.

The end-to-end test renders a short synthetic source (frame index painted as 16 binary blocks, a 1 kHz blip on
every whole second) with every export method and checks the decoded sequence frame by frame.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

W, H, FPS, BITS = 160, 96, 60, 16
CELL = W // BITS
HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


# ---------- splice-point scanner (no ffmpeg needed) ----------

class _Packet:
    def __init__(self, pts: int, data: bytes, keyframe: bool) -> None:
        self.pts, self.dts, self.time_base = pts, pts, 0.001
        self.is_keyframe = keyframe
        self._data = data

    def __bytes__(self) -> bytes:
        return self._data


def _avcc(nal: bytes) -> bytes:
    return len(nal).to_bytes(4, "big") + nal


def test_scanner_rejects_b_frames_whose_length_prefix_looks_like_a_start_code(monkeypatch):
    from utils import ffmpeg

    idr = _avcc(b"\x65" + b"\x88" * 1000)  # NAL type 5, a real IDR on a sync sample
    b_frame = _avcc(b"\x25" + b"\x01" * 292)  # 293 bytes -> prefix 00 00 01 25; NAL ref_idc 1, type 5 when misread
    assert b_frame[:4] == b"\x00\x00\x01\x25"
    b_frame = b_frame[:4] + b"\x41" + b_frame[5:]  # the real NAL header: non-IDR slice
    packets = [_Packet(0, idr, True), _Packet(1083, b_frame, False), _Packet(2000, idr, True)]
    stream = SimpleNamespace(type="video", codec_context=SimpleNamespace(
        name="h264", extradata=bytes([1, 0x64, 0, 0x28, 0xFF, 0xE1])))  # avcC: 4-byte NAL lengths
    container = SimpleNamespace(streams=[stream], demux=lambda s: iter(packets), close=lambda: None)
    monkeypatch.setattr(ffmpeg, "av", SimpleNamespace(open=lambda path: container))
    assert ffmpeg._scan_splice_safe_pts_pyav("x.mp4") == [0.0, 2.0]


def test_scanner_requires_a_container_sync_sample(monkeypatch):
    from utils import ffmpeg

    idr = _avcc(b"\x65" + b"\x88" * 600)
    packets = [_Packet(0, idr, True), _Packet(500, idr, False)]  # an IDR NAL the container does not index
    stream = SimpleNamespace(type="video", codec_context=SimpleNamespace(name="h264", extradata=bytes([1, 0, 0, 0, 0xFF])))
    container = SimpleNamespace(streams=[stream], demux=lambda s: iter(packets), close=lambda: None)
    monkeypatch.setattr(ffmpeg, "av", SimpleNamespace(open=lambda path: container))
    assert ffmpeg._scan_splice_safe_pts_pyav("x.mp4") == [0.0]


def test_splice_cache_v1_is_not_reused(tmp_path, monkeypatch):
    import json

    from utils import ffmpeg

    media = tmp_path / "v.mp4"
    media.write_bytes(b"x" * 10)
    monkeypatch.setattr(ffmpeg, "_splice_cache_root", lambda: tmp_path)
    mtime, size = ffmpeg._keyframe_cache_meta(str(media))
    ffmpeg._splice_cache_path(str(media)).write_text(json.dumps(
        {"mtime": mtime, "size": size, "points": [1.0, 1.083], "version": 1}), encoding="utf-8")
    assert ffmpeg._load_splice_cache(str(media)) is None
    ffmpeg._save_splice_cache(str(media), [1.0, 3.0])
    assert ffmpeg._load_splice_cache(str(media)) == [1.0, 3.0]


def test_piece_frame_count():
    from export.exporter import ExportWorker

    assert ExportWorker._piece_frame_count(4.0, 38.0, 60.0) == 2040
    assert ExportWorker._piece_frame_count(1.13, 2.0, 60.0) == 52  # frames 68..119
    assert ExportWorker._piece_frame_count(1.0, 2.0, 0.0) == 0  # unknown fps: no frame limit


# ---------- end-to-end (synthetic source, every export method) ----------

def _frame(i: int) -> np.ndarray:
    img = np.full((H, W), 40, np.uint8)
    for b in range(BITS):
        if (i >> b) & 1:
            img[10:50, b * CELL + 1:(b + 1) * CELL - 1] = 230
    x = (i * 3) % (W - 20)
    img[60:90, x:x + 20] = 140  # motion, so encoders produce real B-frames
    return np.dstack([img, img, img])


def _make_source(path: Path, seconds: int) -> None:
    sr = 48000
    audio = np.zeros(seconds * sr, np.float32)
    blip = 0.8 * np.sin(2 * np.pi * 1000 * np.arange(int(0.02 * sr)) / sr)
    for k in range(seconds):
        audio[k * sr:k * sr + len(blip)] = blip
    wav = path.with_suffix(".wav")
    with wave.open(str(wav), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sr)
        handle.writeframes((audio * 32767).astype(np.int16).tobytes())
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
           "-i", "-", "-i", str(wav), "-c:v", "libx264", "-preset", "veryfast", "-g", str(2 * FPS),
           "-keyint_min", str(2 * FPS), "-sc_threshold", "0", "-bf", "3", "-pix_fmt", "yuv420p", "-c:a", "aac",
           "-b:a", "128k", "-video_track_timescale", "1000", "-shortest", str(path)]
    frames = b"".join(_frame(i).tobytes() for i in range(seconds * FPS))
    subprocess.run(cmd, input=frames, check=True)


def _indices(path: Path) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-an", "-vf", f"scale={W}:{H},format=gray",
                          "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
    frames = np.frombuffer(raw, np.uint8).reshape(-1, H, W)
    cells = np.stack([frames[:, 15:45, b * CELL + 2:(b + 1) * CELL - 2].mean(axis=(1, 2)) for b in range(BITS)], 1)
    return ((cells > 135).astype(np.int64) * (1 << np.arange(BITS))).sum(1)


def _blips(path: Path) -> list[float]:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "48000", "-f", "s16le",
                          "-"], capture_output=True, check=True).stdout
    env = np.convolve(np.abs(np.frombuffer(raw, np.int16).astype(np.float32)), np.ones(96) / 96, mode="same")
    above = env > 3000
    return [o / 48000 for o in np.where(above[1:] & ~above[:-1])[0] + 1]


KEEPS = [(0.53, 17.4), (18.2, 33.1), (34.0, 45.6), (46.0, 59.5)]  # cuts on and off the 2 s GOP grid


@pytest.fixture(scope="module")
def source(tmp_path_factory: Any) -> Path:
    if not HAVE_FFMPEG:
        pytest.skip("ffmpeg not installed")
    path = tmp_path_factory.mktemp("junctions") / "src.mp4"
    _make_source(path, 60)
    return path


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")
@pytest.mark.parametrize("method", ["filter_concat", "chunked_parallel", "smart_render", "smart_hybrid"])
def test_every_kept_frame_once_in_order_with_audio_in_sync(source, tmp_path, monkeypatch, method):
    from analysis.cut_engine import Segment
    from export.exporter import ExportWorker

    for name in ("CACHE", "CHUNK_CACHE", "CONFIG", "DATA"):
        monkeypatch.setenv(f"AUTO_CUTTER_{name}_DIR", str(tmp_path / name.lower()))
    out = tmp_path / f"{method}.mp4"
    worker = ExportWorker("ffmpeg", str(source), str(out), [Segment(a, b) for a, b in KEEPS], codec="libx264",
                          export_method=method, parallel_workers=2)
    log: list[str] = []
    worker._log = log.append  # type: ignore[method-assign,assignment]
    errors: list[str] = []
    worker.error.connect(errors.append)
    worker.run()
    assert not errors and out.is_file(), errors
    if method == "smart_hybrid":
        assert not any("smart_hybrid_fallback" in line for line in log), "the hybrid path must really run"

    idx = _indices(out).tolist()
    expected = [i for a, b in KEEPS for i in range(int(np.ceil(a * FPS - 1e-6)), int(np.ceil(b * FPS - 1e-6)))]
    allowed = set(expected)
    first_keep = set(range(expected[0], expected[0] + 3))
    seen: set[int] = set()
    previous = -1
    for pos, value in enumerate(idx):
        assert value in allowed, f"frame {value} at {pos} is outside the keeps"
        assert value >= previous, f"backward jump at output frame {pos}: {previous} -> {value}"
        assert value not in seen or value in first_keep, f"frame {value} shown twice (output frame {pos})"
        seen.add(value)
        previous = value
    missing = allowed - seen
    assert len(missing) <= len(KEEPS), f"{len(missing)} kept frames missing"  # <= 1 boundary frame per keep
    assert abs(len(idx) - len(expected)) <= len(KEEPS) + 2

    keep_total = sum(b - a for a, b in KEEPS)
    duration = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                     str(out)], capture_output=True, text=True).stdout.strip())
    assert abs(duration - keep_total) <= 0.1, (duration, keep_total)

    offsets = []
    for t in _blips(out):  # each blip must play while its own second is on screen
        s = idx[min(int(t * FPS), len(idx) - 1)] / FPS
        k = round(s)
        if not any(abs(k - a) < 0.25 or abs(k - b) < 0.25 for a, b in KEEPS):  # a blip cut by a junction
            offsets.append(s - k)
    assert len(offsets) >= 45
    assert max(abs(o) for o in offsets) <= 2.5 / FPS, sorted(set(round(o, 3) for o in offsets))
