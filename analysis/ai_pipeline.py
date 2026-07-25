from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List
import os
import subprocess
import tempfile
import shutil
import math
import sys
import json
import importlib.util

import numpy as np

from analysis.cut_engine import Segment
from analysis.cancellation import AiCancellationToken
from utils.ffmpeg import ensure_ffmpeg, ffprobe_duration_seconds, _project_root
from utils.subprocess_utils import run_no_window


def _app_env(name: str) -> Optional[str]:
    val = os.environ.get(name)
    if val is not None:
        return val
    if name.startswith("AUTO_CUTTER_"):
        legacy = "RECUT_" + name[len("AUTO_CUTTER_") :]
        return os.environ.get(legacy)
    return None


def _candidate_ai_roots() -> list[Path]:
    """
    Candidate roots for external AI runtimes.

    In source mode the root is the project folder.
    In PyInstaller onedir, runtime root may be "_internal", while the external
    helper venv is usually placed next to the EXE (parent folder).
    """
    out: list[Path] = []
    seen: set[str] = set()

    def _add(p: Path | None) -> None:
        if p is None:
            return
        try:
            rp = p.resolve()
        except Exception:
            rp = Path(p)
        key = str(rp).lower()
        if key in seen:
            return
        seen.add(key)
        out.append(rp)

    try:
        _add(Path(_project_root()))
    except Exception:
        pass
    try:
        _add(Path(sys.executable).resolve().parent)
    except Exception:
        try:
            _add(Path(sys.executable).parent)
        except Exception:
            pass

    # In frozen onedir builds project_root() usually points to "_internal";
    # also probe parent folders where we install external AI helpers.
    for p in list(out):
        try:
            _add(p.parent)
        except Exception:
            pass

    return out


@dataclass
class AiPipelineConfig:
    spleeter_python: Optional[str] = None
    silero_python: Optional[str] = None
    spleeter_model: str = "spleeter:2stems"
    keep_temp: bool = False
    enable_diarization: bool = True
    max_speakers: int = 2
    vad_threshold: float = 0.5
    min_speech_s: float = 0.3
    merge_gap_s: float = 0.2
    expected_speakers: int = 0


@dataclass
class AiPipelineResult:
    duration: float
    speech: list[Segment]
    temp_dir: Optional[Path] = None
    speaker_ids: Optional[list[int]] = None


def find_spleeter_python() -> Optional[str]:
    env = (
        _app_env("AUTO_CUTTER_AI_PY")
        or _app_env("AUTO_CUTTER_SPLEETER_PY")
        or os.environ.get("SPLEETER_PY")
    )
    if env:
        p = Path(env)
        if p.exists() and _python_has_module(str(p), "spleeter"):
            return str(p)

    for root in _candidate_ai_roots():
        candidates = [
            root / "ai_runtime" / "Scripts" / "python.exe",
            root / "ai_runtime" / "bin" / "python",
            root / ".venv_spleeter" / "Scripts" / "python.exe",
            root / ".venv_spleeter" / "bin" / "python",
        ]
        for cand in candidates:
            if cand.exists() and _python_has_module(str(cand), "spleeter"):
                return str(cand)
    return None


def _module_available(module_name: str) -> bool:
    try:
        return importlib.util.find_spec(module_name) is not None
    except Exception:
        return False


def _python_has_module(python_exe: str, module_name: str, timeout_s: float = 6.0) -> bool:
    try:
        p = run_no_window(
            [
                str(python_exe),
                "-c",
                (
                    "import importlib.util,sys;"
                    "sys.exit(0 if importlib.util.find_spec(sys.argv[1]) else 1)"
                ),
                module_name,
            ],
            capture_output=True,
            text=True,
            timeout=max(1.0, float(timeout_s)),
        )
        return int(p.returncode) == 0
    except Exception:
        return False


def _python_can_import_modules(
    python_exe: str,
    module_names: list[str],
    timeout_s: float = 12.0,
) -> bool:
    mods = [str(m).strip() for m in (module_names or []) if str(m).strip()]
    if not mods:
        return True
    try:
        p = run_no_window(
            [
                str(python_exe),
                "-c",
                (
                    "import importlib,sys;"
                    "mods=sys.argv[1:];"
                    "[(importlib.import_module(m)) for m in mods];"
                    "sys.exit(0)"
                ),
                *mods,
            ],
            capture_output=True,
            text=True,
            timeout=max(1.0, float(timeout_s)),
        )
        return int(p.returncode) == 0
    except Exception:
        return False


def _inprocess_silero_runtime_ok() -> bool:
    # PyInstaller may include silero_vad without bundling torch.
    # Require both before using the app interpreter for VAD.
    return _module_available("silero_vad") and _module_available("torch")


def _python_has_silero_runtime(python_exe: str) -> bool:
    return _python_can_import_modules(str(python_exe), ["silero_vad", "torch"])


def _spleeter_model_path(spleeter_python: str) -> Path:
    configured = _app_env("AUTO_CUTTER_AI_MODELS")
    if configured:
        return Path(configured).expanduser().resolve()

    # Reuse source-tree models when developing, but keep downloaded models
    # beside the external AI runtime in installed builds.
    for root in _candidate_ai_roots():
        candidate = root / "pretrained_models"
        if candidate.is_dir():
            return candidate

    python_path = Path(spleeter_python).resolve()
    runtime_root = python_path.parent.parent if python_path.parent.name.lower() in {"scripts", "bin"} else python_path.parent
    return runtime_root / "pretrained_models"


def find_silero_python() -> Optional[str]:
    env = (
        _app_env("AUTO_CUTTER_AI_PY")
        or _app_env("AUTO_CUTTER_SILERO_PY")
        or os.environ.get("SILERO_PY")
    )
    if env:
        p = Path(env)
        if p.exists() and _python_has_silero_runtime(str(p)):
            return str(p)

    # current interpreter
    try:
        cur = str(Path(sys.executable).resolve())
    except Exception:
        cur = str(sys.executable)
    if _inprocess_silero_runtime_ok():
        return cur

    candidates: list[Path] = []
    for root in _candidate_ai_roots():
        candidates.extend(
            [
                root / ".venv310" / "Scripts" / "python.exe",
                root / ".venv" / "Scripts" / "python.exe",
                root / "ai_runtime" / "Scripts" / "python.exe",
                root / ".venv_spleeter" / "Scripts" / "python.exe",
                root / ".venv310" / "bin" / "python",
                root / ".venv" / "bin" / "python",
                root / "ai_runtime" / "bin" / "python",
                root / ".venv_spleeter" / "bin" / "python",
            ]
        )
    seen: set[str] = set()
    for cand in candidates:
        try:
            c = str(cand.resolve())
        except Exception:
            c = str(cand)
        if c in seen:
            continue
        seen.add(c)
        if cand.exists() and _python_has_silero_runtime(str(cand)):
            return str(cand)
    return None


def _extract_audio_wav(
    src: str,
    dst: Path,
    cancel_token: AiCancellationToken | None = None,
) -> None:
    ffmpeg, _ = ensure_ffmpeg()
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-v", "error",
        "-y",
        "-i", str(src),
        "-vn",
        "-ac", "2",
        "-ar", "48000",
        str(dst),
    ]
    runner = cancel_token.run if cancel_token is not None else run_no_window
    p = runner(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or "FFmpeg audio extraction failed")


def _run_spleeter(
    spleeter_py: str,
    wav_path: Path,
    out_dir: Path,
    model: str,
    cancel_token: AiCancellationToken | None = None,
) -> Path:
    env = os.environ.copy()
    model_path = _spleeter_model_path(spleeter_py)
    model_path.mkdir(parents=True, exist_ok=True)
    env["MODEL_PATH"] = str(model_path)
    try:
        ffmpeg, _ = ensure_ffmpeg()
        env["FFMPEG_BINARY"] = str(ffmpeg)
        env["PATH"] = str(Path(ffmpeg).parent) + os.pathsep + env.get("PATH", "")
    except Exception:
        pass
    cmd = [
        spleeter_py,
        "-m", "spleeter",
        "separate",
        "-p", str(model),
        "-o", str(out_dir),
        str(wav_path),
    ]
    runner = cancel_token.run if cancel_token is not None else run_no_window
    p = runner(cmd, capture_output=True, text=True, env=env)
    if p.returncode != 0:
        err = (p.stderr or "").strip()
        raise RuntimeError(err or "Spleeter failed")

    stem_dir = out_dir / wav_path.stem
    candidates = []
    if stem_dir.exists():
        candidates = list(stem_dir.glob("vocals.*"))
    if not candidates:
        candidates = list(out_dir.rglob("vocals.*"))
    if candidates:
        return candidates[0]

    # provide more context if the process reported anything
    err = (p.stderr or p.stdout or "").strip()
    details = f" Details: {err}" if err else ""
    files = []
    if stem_dir.exists():
        files = [p.name for p in stem_dir.glob("*")]
    info = f" Files: {', '.join(files[:6])}" if files else ""
    raise RuntimeError(f"Spleeter output not found (vocals.*).{details}{info}")


def _read_audio_fallback(path: Path) -> tuple[np.ndarray, int]:
    # Prefer soundfile if available; fallback to scipy.io.wavfile.
    data = None
    sr = None
    try:
        import soundfile as sf
        data, sr = sf.read(str(path), always_2d=False)
    except Exception:
        try:
            from scipy.io import wavfile
            sr, data = wavfile.read(str(path))
        except Exception as e:
            raise RuntimeError(f"Failed to read audio for VAD: {e}") from e

    if data is None or sr is None:
        raise RuntimeError("Failed to read audio for VAD.")

    if isinstance(data, np.ndarray) and data.ndim > 1:
        data = np.mean(data, axis=1)
    data = np.asarray(data)
    if data.dtype.kind in ("i", "u"):
        max_val = float(np.iinfo(data.dtype).max)
        if max_val > 0:
            data = data.astype(np.float32) / max_val
        else:
            data = data.astype(np.float32)
    else:
        data = data.astype(np.float32, copy=False)
    return data, int(sr)


def _resample_audio(data: np.ndarray, sr: int, target_sr: int) -> np.ndarray:
    if sr == target_sr:
        return data
    try:
        from scipy.signal import resample_poly
        g = math.gcd(sr, target_sr)
        up = target_sr // g
        down = sr // g
        return resample_poly(data, up, down).astype(np.float32, copy=False)
    except Exception:
        # Fallback: linear interpolation
        ratio = float(target_sr) / float(sr)
        x_old = np.arange(len(data), dtype=np.float32)
        x_new = np.arange(int(len(data) * ratio), dtype=np.float32) / ratio
        return np.interp(x_new, x_old, data).astype(np.float32, copy=False)


def _run_silero_vad_external(
    silero_python: str,
    vocals_path: Path,
    threshold: float,
    min_speech_s: float,
    merge_gap_s: float,
    cancel_token: AiCancellationToken | None = None,
) -> list[Segment]:
    script = r"""
import json
import math
import sys
import numpy as np
from silero_vad import load_silero_vad, get_speech_timestamps

def read_audio(path):
    data = None
    sr = None
    try:
        import soundfile as sf
        data, sr = sf.read(path, always_2d=False)
    except Exception:
        from scipy.io import wavfile
        sr, data = wavfile.read(path)
    if getattr(data, "ndim", 1) > 1:
        data = data.mean(axis=1)
    data = np.asarray(data)
    if data.dtype.kind in ("i", "u"):
        max_val = float(np.iinfo(data.dtype).max)
        data = data.astype(np.float32) / (max_val if max_val > 0 else 1.0)
    else:
        data = data.astype(np.float32, copy=False)
    return data, int(sr)

def resample_audio(data, sr, target_sr=16000):
    if sr == target_sr:
        return data
    try:
        from scipy.signal import resample_poly
        g = math.gcd(sr, target_sr)
        up = target_sr // g
        down = sr // g
        return resample_poly(data, up, down).astype(np.float32, copy=False)
    except Exception:
        ratio = float(target_sr) / float(sr)
        x_old = np.arange(len(data), dtype=np.float32)
        x_new = np.arange(int(len(data) * ratio), dtype=np.float32) / ratio
        return np.interp(x_new, x_old, data).astype(np.float32, copy=False)

path = sys.argv[1]
threshold = float(sys.argv[2])
min_speech_s = float(sys.argv[3])
merge_gap_s = float(sys.argv[4])

wav, sr = read_audio(path)
wav = resample_audio(wav, sr, 16000)
model = load_silero_vad()
ts = get_speech_timestamps(
    wav,
    model,
    sampling_rate=16000,
    return_seconds=True,
    threshold=threshold,
    min_speech_duration_ms=int(max(0.0, min_speech_s) * 1000.0),
    min_silence_duration_ms=int(max(0.0, merge_gap_s) * 1000.0),
)
print(json.dumps(ts), end="")
"""
    runner = cancel_token.run if cancel_token is not None else run_no_window
    p = runner(
        [
            str(silero_python),
            "-c",
            script,
            str(vocals_path),
            f"{float(threshold):.6f}",
            f"{float(min_speech_s):.6f}",
            f"{float(merge_gap_s):.6f}",
        ],
        capture_output=True,
        text=True,
    )
    if p.returncode != 0:
        err = (p.stderr or p.stdout or "").strip()
        raise RuntimeError(err or "External Silero VAD failed")

    out = (p.stdout or "").strip()
    if not out:
        return []
    try:
        raw = json.loads(out)
    except Exception as e:
        raise RuntimeError(f"External Silero VAD returned invalid JSON: {e}") from e

    segs: list[Segment] = []
    for t in raw if isinstance(raw, list) else []:
        try:
            segs.append(Segment(float(t["start"]), float(t["end"])))
        except Exception:
            continue
    return segs


def _run_silero_vad(
    vocals_path: Path,
    threshold: float,
    min_speech_s: float,
    merge_gap_s: float,
    silero_python: Optional[str] = None,
    cancel_token: AiCancellationToken | None = None,
) -> list[Segment]:
    if cancel_token is not None:
        cancel_token.check()
    if not _inprocess_silero_runtime_ok():
        if silero_python:
            return _run_silero_vad_external(
                silero_python,
                vocals_path,
                threshold,
                min_speech_s,
                merge_gap_s,
                cancel_token,
            )
        if _module_available("silero_vad") and not _module_available("torch"):
            raise RuntimeError(
                "silero_vad found but torch is missing in app runtime "
                "(set AUTO_CUTTER_SILERO_PY or use the bundled .venv_spleeter)."
            )
        raise RuntimeError("silero_vad not found (set AUTO_CUTTER_SILERO_PY or install silero-vad).")

    try:
        from silero_vad import load_silero_vad, get_speech_timestamps
    except Exception:
        if silero_python:
            return _run_silero_vad_external(
                silero_python,
                vocals_path,
                threshold,
                min_speech_s,
                merge_gap_s,
                cancel_token,
            )
        raise

    model = load_silero_vad()
    wav, sr = _read_audio_fallback(vocals_path)
    wav = _resample_audio(wav, sr, 16000)
    ts = get_speech_timestamps(
        wav,
        model,
        sampling_rate=16000,
        return_seconds=True,
        threshold=float(threshold),
        min_speech_duration_ms=int(max(0.0, float(min_speech_s)) * 1000.0),
        min_silence_duration_ms=int(max(0.0, float(merge_gap_s)) * 1000.0),
    )
    if cancel_token is not None:
        cancel_token.check()
    segs = []
    for t in ts:
        try:
            segs.append(Segment(float(t["start"]), float(t["end"])))
        except Exception:
            continue
    return segs


def _speech_stats(segs: list[Segment], duration: float) -> tuple[float, float]:
    if not segs or duration <= 0:
        return 0.0, 0.0
    total = 0.0
    last_end = 0.0
    for s in segs:
        try:
            total += float(s.end) - float(s.start)
            last_end = max(last_end, float(s.end))
        except Exception:
            continue
    coverage = total / float(duration) if duration > 0 else 0.0
    return coverage, last_end


def _needs_vad_fallback(segs: list[Segment], duration: float) -> bool:
    if not segs:
        return True
    coverage, last_end = _speech_stats(segs, duration)
    if coverage < 0.02:  # too little speech overall
        return True
    if duration > 0 and last_end < (duration * 0.6):  # speech ends too early
        return True
    return False


def _run_speechbrain_diarization(
    vocals_path: Path,
    speech: list[Segment],
    max_speakers: int,
    expected_speakers: int = 0,
    cancel_token: AiCancellationToken | None = None,
) -> Optional[list[int]]:
    if not speech:
        return None
    if cancel_token is not None:
        cancel_token.check()

    try:
        from speechbrain.pretrained import EncoderClassifier
        import torchaudio
        import torch
        from sklearn.cluster import AgglomerativeClustering
    except Exception:
        return None

    try:
        waveform, sr = torchaudio.load(str(vocals_path))
    except Exception:
        # torchaudio >= 2.10 may require torchcodec; if missing, skip diarization
        return None
    if waveform.ndim > 1 and waveform.size(0) > 1:
        waveform = torch.mean(waveform, dim=0, keepdim=True)

    target_sr = 16000
    if sr != target_sr:
        resampler = torchaudio.transforms.Resample(orig_freq=sr, new_freq=target_sr)
        waveform = resampler(waveform)
        sr = target_sr

    classifier = EncoderClassifier.from_hparams(source="speechbrain/spkrec-ecapa-voxceleb")
    if cancel_token is not None:
        cancel_token.check()

    embeddings = []
    for s in speech:
        if cancel_token is not None:
            cancel_token.check()
        start = max(0, int(float(s.start) * sr))
        end = max(start + 1, int(float(s.end) * sr))
        chunk = waveform[:, start:end]
        if chunk.numel() < sr * 0.3:
            # pad very short segments
            pad = int(sr * 0.3) - int(chunk.numel())
            chunk = torch.nn.functional.pad(chunk, (0, max(0, pad)))
        emb = classifier.encode_batch(chunk).squeeze().detach().cpu().numpy()
        embeddings.append(emb)

    if len(embeddings) <= 1:
        return [0] * len(embeddings)

    n_spk = max(1, min(int(max_speakers), len(embeddings)))
    if expected_speakers and int(expected_speakers) > 0:
        n_spk = max(1, min(int(expected_speakers), len(embeddings)))
    try:
        cluster = AgglomerativeClustering(n_clusters=n_spk, metric="cosine", linkage="average")
    except TypeError:
        cluster = AgglomerativeClustering(n_clusters=n_spk, affinity="cosine", linkage="average")
    labels = cluster.fit_predict(embeddings)
    return [int(x) for x in labels]


def run_ai_pipeline(
    path: str,
    cfg: Optional[AiPipelineConfig] = None,
    cancel_token: AiCancellationToken | None = None,
) -> AiPipelineResult:
    cfg = cfg or AiPipelineConfig()
    if cancel_token is not None:
        cancel_token.check()
    spleeter_py = cfg.spleeter_python or find_spleeter_python()
    if not spleeter_py:
        raise RuntimeError(
            "AI runtime not found. Run build/install-ai-runtime.ps1 or set AUTO_CUTTER_AI_PY."
        )
    silero_py = cfg.silero_python or find_silero_python()
    if not _inprocess_silero_runtime_ok() and not silero_py:
        raise RuntimeError("silero_vad not found (set AUTO_CUTTER_SILERO_PY or install silero-vad).")

    tmp_dir = Path(tempfile.mkdtemp(prefix="auto_cutter_ai_"))
    try:
        wav_path = tmp_dir / "input.wav"
        _extract_audio_wav(path, wav_path, cancel_token)

        out_dir = tmp_dir / "spleeter_out"
        out_dir.mkdir(parents=True, exist_ok=True)
        vocals_path = _run_spleeter(
            spleeter_py,
            wav_path,
            out_dir,
            cfg.spleeter_model,
            cancel_token,
        )

        if cancel_token is not None:
            cancel_token.check()
        duration = float(ffprobe_duration_seconds(path))

        speech = _run_silero_vad(
            vocals_path,
            cfg.vad_threshold,
            cfg.min_speech_s,
            cfg.merge_gap_s,
            silero_py,
            cancel_token,
        )
        if _needs_vad_fallback(speech, duration):
            # fallback to VAD on original audio if vocals look unreliable
            try:
                speech_alt = _run_silero_vad(
                    wav_path,
                    cfg.vad_threshold,
                    cfg.min_speech_s,
                    cfg.merge_gap_s,
                    silero_py,
                    cancel_token,
                )
                if speech_alt:
                    speech = speech_alt
            except Exception:
                pass
        speaker_ids = None
        if cfg.enable_diarization:
            speaker_ids = _run_speechbrain_diarization(
                vocals_path,
                speech,
                cfg.max_speakers,
                int(getattr(cfg, "expected_speakers", 0) or 0),
                cancel_token,
            )

        if cancel_token is not None:
            cancel_token.check()
        if cfg.keep_temp:
            return AiPipelineResult(duration=duration, speech=speech, temp_dir=tmp_dir, speaker_ids=speaker_ids)

        return AiPipelineResult(duration=duration, speech=speech, temp_dir=None, speaker_ids=speaker_ids)
    finally:
        if not cfg.keep_temp:
            shutil.rmtree(tmp_dir, ignore_errors=True)
