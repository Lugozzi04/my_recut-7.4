"""Timestamped transcripts for local windows only (optional faster-whisper).

The repository has no ASR yet; faster-whisper (CTranslate2, no PyTorch) is the
optional engine. Like the existing Spleeter/Silero integration it runs in this
interpreter when importable, otherwise in an external runtime located through
AUTO_CUTTER_WHISPER_PY or the same candidate AI runtime folders. Without it the
transcript is reported as unavailable; nothing is fabricated. Several windows
are transcribed in ONE helper call, so the model loads once, and results are
cached per (video identity, window, engine settings).
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any, Protocol

from analysis.performance_profile import current_profile, measure, record
from analysis.audio_service import AnalysisCancellation, AnalysisCancelled
from .media import extract_wav
from .models import EvidenceStatus, TimeRange, TranscriptSegment, TranscriptWord

HELPER = r"""
import json, math, sys
from faster_whisper import WhisperModel
job = json.load(open(sys.argv[1], encoding="utf-8"))
options = job["options"]
try:
    model = WhisperModel(options["model"], device=options["device"], compute_type=options["compute_type"],
                         local_files_only=True)
except Exception:
    model = WhisperModel(options["model"], device=options["device"], compute_type=options["compute_type"])
results = []
for path in job["files"]:
    segments, info = model.transcribe(path, language=options["language"], beam_size=options["beam_size"],
                                      vad_filter=options["vad_filter"], word_timestamps=True,
                                      condition_on_previous_text=False)
    out = []
    for s in segments:
        words = [{"start": w.start, "end": w.end, "text": w.word.strip(), "probability": w.probability}
                 for w in (s.words or [])]
        out.append({"start": s.start, "end": s.end, "text": s.text.strip(),
                    "confidence": math.exp(s.avg_logprob) if s.avg_logprob is not None else None,
                    "no_speech_probability": s.no_speech_prob, "words": words})
    results.append({"language": info.language, "language_probability": info.language_probability,
                    "segments": out})
json.dump(results, open(sys.argv[2], "w", encoding="utf-8"))
"""


@dataclass(frozen=True)
class TranscriptSettings:
    enabled: bool = True
    model: str = "small"  # faster-whisper size or local CTranslate2 model folder
    language: str | None = None  # None = auto-detect per window
    device: str = "cpu"
    compute_type: str = "int8"
    beam_size: int = 5
    vad_filter: bool = True  # faster-whisper's own VAD: avoids hallucinated text in silences
    python: str | None = None  # explicit external interpreter
    use_cache: bool = True

    def engine_options(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if k in
                ("model", "language", "device", "compute_type", "beam_size", "vad_filter")}


class TranscriptionEngine(Protocol):
    def transcribe(self, video: str | Path, windows: Sequence[TimeRange], *,
                   cancellation: AnalysisCancellation | None = None
                   ) -> tuple[EvidenceStatus, list[list[TranscriptSegment]]]: ...


class NullTranscriptionEngine:
    def __init__(self, status: str = "disabled", detail: str = "") -> None:
        self.status, self.detail = status, detail

    def transcribe(self, video: str | Path, windows: Sequence[TimeRange], *,
                   cancellation: AnalysisCancellation | None = None
                   ) -> tuple[EvidenceStatus, list[list[TranscriptSegment]]]:
        return EvidenceStatus(self.status, "none", self.detail), [[] for _ in windows]


def _importable_here() -> bool:
    import importlib.util
    try:
        return importlib.util.find_spec("faster_whisper") is not None
    except (ImportError, ValueError):
        return False


def find_whisper_python(explicit: str | None = None) -> str | None:
    from analysis.ai_pipeline import _app_env, _candidate_ai_roots, _python_has_module

    configured = explicit or _app_env("AUTO_CUTTER_WHISPER_PY")
    if configured:
        return configured if Path(configured).exists() and _python_has_module(configured, "faster_whisper") else None
    if _importable_here():
        return sys.executable
    for root in _candidate_ai_roots():
        for name in (".venv310", ".venv", "ai_runtime", ".venv_spleeter"):
            for candidate in (root / name / "Scripts" / "python.exe", root / name / "bin" / "python"):
                if candidate.exists() and _python_has_module(str(candidate), "faster_whisper"):
                    return str(candidate)
    return _system_whisper_python()


_SYSTEM_WHISPER: list[str | None] = []  # memo: probing interpreters spawns processes


def _system_whisper_python() -> str | None:
    """faster-whisper installed in a SYSTEM Python (python.org installs, the py launcher, PATH): found without
    AUTO_CUTTER_WHISPER_PY, so a plain command line gets transcripts too. Probed once per process."""
    if _SYSTEM_WHISPER:
        return _SYSTEM_WHISPER[0]
    import os
    import shutil
    import subprocess

    from analysis.ai_pipeline import _python_has_module

    candidates: list[str] = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates += [str(p) for p in sorted((Path(local) / "Programs" / "Python").glob("Python3*/python.exe"),
                                              reverse=True)]
    for program_files in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if program_files:
            candidates += [str(p) for p in sorted(Path(program_files).glob("Python3*/python.exe"), reverse=True)]
    launcher = shutil.which("py")
    if launcher:
        try:
            listed = subprocess.run([launcher, "-0p"], capture_output=True, text=True, timeout=10,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            candidates += [line.split()[-1] for line in listed.splitlines() if line.strip().endswith(".exe")]
        except (OSError, subprocess.SubprocessError):
            pass
    for name in ("python", "python3"):
        found = shutil.which(name)
        if found and "WindowsApps" not in found:  # the Store alias opens the Store instead of running
            candidates.append(found)
    seen: set[str] = set()
    result = None
    for candidate in candidates:
        key = os.path.normcase(os.path.abspath(candidate))
        if key in seen or key == os.path.normcase(os.path.abspath(sys.executable)):
            continue
        seen.add(key)
        if Path(candidate).exists() and _python_has_module(candidate, "faster_whisper"):
            result = candidate
            break
    _SYSTEM_WHISPER.append(result)
    return result


def _shift(raw: dict[str, Any], window: TimeRange) -> TranscriptSegment | None:
    def clamp(t: float) -> float:
        return min(window.end, max(window.start, window.start + float(t)))

    try:
        words = tuple(TranscriptWord(clamp(w["start"]), clamp(w["end"]), str(w["text"]),
                                     None if w.get("probability") is None else float(w["probability"]))
                      for w in raw.get("words", []) if w.get("start") is not None and w.get("end") is not None)
        confidence = raw.get("confidence")
        no_speech = raw.get("no_speech_probability")
        return TranscriptSegment(clamp(raw["start"]), clamp(raw["end"]), str(raw.get("text", "")).strip(),
                                 None if confidence is None or not math.isfinite(confidence) else float(confidence),
                                 None if no_speech is None else float(no_speech), words)
    except (KeyError, TypeError, ValueError):
        return None


class FasterWhisperEngine:
    def __init__(self, settings: TranscriptSettings | None = None, *, cache_directory: str | Path | None = None) -> None:
        self.settings = settings or TranscriptSettings()
        if cache_directory is None:
            from utils.runtime_paths import cache_root
            cache_directory = cache_root() / "boundaries" / "transcripts"
        self.cache_directory = Path(cache_directory)

    def _key(self, identity: dict[str, Any], window: TimeRange) -> str:
        value = {"version": 1, "video": identity, "window": [round(window.start, 3), round(window.end, 3)],
                 "engine": "faster-whisper", "options": self.settings.engine_options()}
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()

    def _load(self, key: str) -> list[TranscriptSegment] | None:
        try:
            raw = json.loads((self.cache_directory / f"{key}.json").read_text(encoding="utf-8"))
            if raw.get("key") != key:
                return None
            return [TranscriptSegment.from_mapping(item) for item in raw["segments"]]
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def transcribe(self, video: str | Path, windows: Sequence[TimeRange], *,
                   cancellation: AnalysisCancellation | None = None
                   ) -> tuple[EvidenceStatus, list[list[TranscriptSegment]]]:
        if not self.settings.use_cache or not windows:
            return self._transcribe_exact(video, windows, cancellation=cancellation)
        from analysis.gameplay.cache import video_identity
        from analysis.shared.transcript import TranscriptCoverage, clip_segments

        token = cancellation or AnalysisCancellation()
        token.raise_if_cancelled()
        identity = video_identity(Path(video).expanduser().resolve())
        store = TranscriptCoverage(self.cache_directory, identity, self.settings.engine_options())
        with store.hold(token):
            for window in windows:
                key = self._key(identity, window)
                if self._load(key) is not None:
                    store.register(key, window)  # Register legacy exact-window hits lazily.
            if all(self._load(self._key(identity, window)) is not None for window in windows):
                record("shared.transcript", exact_window_hits=len(windows))
                profile = current_profile()
                if profile:
                    profile.cache("shared.transcript", "hit")
                return self._transcribe_exact(video, windows, cancellation=token)
            entries = store.entries(self._load)
            plans = [store.plan(window, entries) for window in windows]
            merged: list[TimeRange] = []
            gaps = sorted((gap for _, gaps in plans for gap in gaps), key=lambda span: (span.start, span.end))
            for gap in gaps:
                if merged and gap.start <= merged[-1].end:
                    merged[-1] = TimeRange(merged[-1].start, max(merged[-1].end, gap.end))
                else:
                    merged.append(gap)
            record("shared.transcript", windows_requested=len(windows), missing_intervals=len(merged),
                   covered_seconds=sum(span.duration for covered, _ in plans for span, _ in covered),
                   requested_seconds=sum(window.duration for window in windows))
            profile = current_profile()
            if profile:
                profile.cache("shared.transcript", "hit" if not merged else
                              "partial" if any(c for c, _ in plans) else "miss")
            if merged:
                status, new = self._transcribe_exact(video, merged, cancellation=token)
                if status.status in ("ok", "cached"):
                    entries += list(zip(merged, new))
            else:
                if profile:
                    profile.cache("boundaries.transcript", "hit")
                    for phase in ("audio_extraction", "runtime_discovery", "inference"):
                        profile.skip_unexecuted("boundaries.transcript." + phase, "source_coverage_hit")
                record("boundaries.transcript", windows_requested=len(windows), cached_windows=len(windows))
                status = EvidenceStatus("cached", "faster-whisper", "source interval coverage hit",
                                        self.settings.engine_options())
            entries.sort(key=lambda entry: (entry[0].start, -entry[0].end))
            output = []
            for window in windows:
                covered, _ = store.plan(window, entries)
                output.append([segment for span, segments in covered for segment in clip_segments(segments, span)])
            token.raise_if_cancelled()
            return status, output

    def _transcribe_exact(self, video: str | Path, windows: Sequence[TimeRange], *,
                   cancellation: AnalysisCancellation | None = None
                   ) -> tuple[EvidenceStatus, list[list[TranscriptSegment]]]:
        from analysis.gameplay.cache import atomic_write_json, video_identity

        token = cancellation or AnalysisCancellation()
        token.raise_if_cancelled()
        options = self.settings.engine_options()
        results: list[list[TranscriptSegment] | None] = [None] * len(windows)
        keys: list[str] = []
        with measure("boundaries.transcript.cache_lookup"):
            if self.settings.use_cache:
                identity = video_identity(Path(video).expanduser().resolve())
                keys = [self._key(identity, window) for window in windows]
                results = [self._load(key) for key in keys]
        missing = [i for i, value in enumerate(results) if value is None]
        profile = current_profile()
        if profile:
            profile.cache("boundaries.transcript", ("disabled" if not self.settings.use_cache else
                "hit" if not missing else "miss" if len(missing) == len(windows) else "partial"))
        record("boundaries.transcript", windows_requested=len(windows), cached_windows=len(windows) - len(missing),
               uncached_windows=len(missing))
        if not missing:
            return EvidenceStatus("cached", "faster-whisper", "", options), [r or [] for r in results]
        with measure("boundaries.transcript.runtime_discovery"):
            python = find_whisper_python(self.settings.python)
        if python is None:
            return (EvidenceStatus("unavailable", "faster-whisper",
                                   "faster-whisper not found (install it or set AUTO_CUTTER_WHISPER_PY)", options),
                    [r or [] for r in results])
        from utils.subprocess_utils import popen_no_window

        languages: list[str] = []
        with tempfile.TemporaryDirectory(prefix="recut-boundary-asr-") as folder:
            with measure("boundaries.transcript.audio_extraction"):
                files = [str(extract_wav(video, windows[i], Path(folder) / f"w{i}.wav", cancellation=token)) for i in missing]
            record("boundaries.transcript.audio_extraction", windows=len(missing))
            job, output = Path(folder) / "job.json", Path(folder) / "out.json"
            job.write_text(json.dumps({"options": options, "files": files}), encoding="utf-8")
            env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
            with measure("boundaries.transcript.inference"):
                process = popen_no_window([python, "-c", HELPER, str(job), str(output)], stdout=subprocess.DEVNULL,
                                          stderr=subprocess.PIPE, env=env)
                token.attach_process(process)
                try:
                    _, stderr = process.communicate()
                finally:
                    token.detach_process(process)
            record("boundaries.transcript.inference", windows=len(missing), helper_invocations=1)
            if token.is_cancelled:
                raise AnalysisCancelled()
            if process.returncode != 0 or not output.is_file():
                message = (stderr or b"").decode("utf-8", "ignore").strip().splitlines()[-1:] or ["unknown error"]
                return (EvidenceStatus("failed", "faster-whisper", message[0], options), [r or [] for r in results])
            raw = json.loads(output.read_text(encoding="utf-8"))
        if (not isinstance(raw, list) or len(raw) != len(missing)
                or any(not isinstance(item, dict) or not isinstance(item.get("segments"), list) for item in raw)):
            return EvidenceStatus("failed", "faster-whisper", "invalid helper output", options), [r or [] for r in results]
        for i, item in zip(missing, raw):
            segments = [s for s in (_shift(seg, windows[i]) for seg in item.get("segments", [])) if s is not None]
            results[i] = segments
            probability = item.get("language_probability")
            languages.append(f"{item.get('language')}({probability:.2f})" if isinstance(probability, float)
                             else str(item.get("language")))
            if self.settings.use_cache:
                try:
                    atomic_write_json(self.cache_directory / f"{keys[i]}.json", {
                        "format": "recut_boundary_transcript_cache", "key": keys[i], "language": item.get("language"),
                        "segments": [s.to_mapping() for s in segments]})
                    from analysis.shared.transcript import TranscriptCoverage
                    TranscriptCoverage(self.cache_directory, identity, options).register(keys[i], windows[i])
                except OSError:
                    pass
        detail = "languages per transcribed window: " + ", ".join(languages)
        return EvidenceStatus("ok", "faster-whisper", detail, {**options, "python": python}), [r or [] for r in results]
