import subprocess
import threading
from typing import Optional

import numpy as np
from PySide6.QtCore import QObject, Signal, Slot

from utils.ffmpeg import ensure_ffmpeg, has_audio_stream, ffprobe_duration_seconds
from utils.subprocess_utils import popen_no_window


class AnalyzeWorker(QObject):
    """
    Estrae audio, calcola RMS su finestre hop_s, e propone una soglia automatica robusta.
    NOTA: lo smoothing/attack/release sono applicati in cut_engine (non qui),
    così l'RMS "raw" resta disponibile anche per meter e auto-threshold.
    """
    done = Signal(int, float, object, float, float)  # track_idx, duration, rms(np.ndarray), hop_s, auto_threshold
    error = Signal(int, str)

    def __init__(self, path: str, hop_s: float = 0.03, sr: int = 16000, track_idx: int = 0):
        super().__init__()
        self.path = path
        self.hop_s = float(hop_s)
        self.sr = int(sr)
        self.track_idx = int(track_idx)
        self._cancel_requested = False
        self._proc_lock = threading.Lock()
        self._proc: subprocess.Popen | None = None

    @Slot()
    def cancel(self) -> None:
        self._cancel_requested = True
        proc: subprocess.Popen | None = None
        with self._proc_lock:
            proc = self._proc
        if proc is None:
            return
        # Important for responsive reset: break a blocking stdout.read() in the worker
        # thread by closing the pipes from the GUI thread, then terminate/kill ffmpeg.
        try:
            if proc.stdout is not None:
                proc.stdout.close()
        except Exception:
            pass
        try:
            if proc.stderr is not None:
                proc.stderr.close()
        except Exception:
            pass
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:
            pass

    @Slot()
    def run(self):
        try:
            if self._cancel_requested:
                raise RuntimeError("__CANCELLED__")
            ffmpeg, _ = ensure_ffmpeg()

            # Se non c'è audio, fermati con un messaggio chiaro (così capisci subito perché non vedi la timeline)
            try:
                if not has_audio_stream(self.path):
                    raise RuntimeError("Nessuna traccia audio trovata nel file. Impossibile generare la timeline audio.")
            except Exception:
                # Se has_audio_stream fallisce, continuiamo comunque e lasciamo decidere a ffmpeg
                pass

            duration: Optional[float]
            try:
                duration = float(ffprobe_duration_seconds(self.path))
            except Exception:
                duration = None

            af_voice = (
                "highpass=f=150,"
                "lowpass=f=3400,"
                "acompressor=threshold=0.10:ratio=4:attack=5:release=50"
            )

            def run_ffmpeg_extract(apply_filter: bool) -> np.ndarray:
                if self._cancel_requested:
                    raise RuntimeError("__CANCELLED__")
                af = af_voice if apply_filter else "anull"

                cmd = [
                    ffmpeg,
                    "-nostdin",
                    "-hide_banner",
                    "-v", "error",
                    "-probesize", "50M",
                    "-analyzeduration", "100M",
                    "-fflags", "+genpts",
                    "-err_detect", "ignore_err",
                    "-i", self.path,

                    # IMPORTANT: scegliamo esplicitamente la prima traccia audio; niente "?".
                    # Se non esiste, ffmpeg fallisce e noi mostriamo stderr (utile per debug).
                    "-map", "0:a:0",

                    "-vn",
                    "-sn",
                    "-dn",

                    "-af", af,
                    "-ac", "1",
                    "-ar", str(self.sr),
                    "-f", "s16le",
                    "pipe:1",
                ]

                frame_len = int(self.sr * self.hop_s)
                frame_len = max(frame_len, 256)

                proc = popen_no_window(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,   # <-- non DEVNULL: ci serve l'errore vero
                )
                with self._proc_lock:
                    self._proc = proc
                assert proc.stdout is not None

                rms_list: list[float] = []
                leftover = np.zeros(0, dtype=np.int16)

                while True:
                    if self._cancel_requested:
                        try:
                            proc.terminate()
                        except Exception:
                            pass
                        raise RuntimeError("__CANCELLED__")
                    try:
                        b = proc.stdout.read(65536)
                    except Exception:
                        if self._cancel_requested:
                            raise RuntimeError("__CANCELLED__")
                        raise
                    if not b:
                        break

                    arr = np.frombuffer(b, dtype=np.int16)
                    if leftover.size:
                        arr = np.concatenate([leftover, arr])
                        leftover = np.zeros(0, dtype=np.int16)

                    n_frames = arr.size // frame_len
                    if n_frames <= 0:
                        leftover = arr
                        continue

                    frames = arr[: n_frames * frame_len].astype(np.float32).reshape(n_frames, frame_len)
                    ms = np.mean(frames * frames, axis=1)
                    rms = np.sqrt(ms) / 32768.0
                    rms_list.extend(rms.tolist())

                    leftover = arr[n_frames * frame_len:]

                try:
                    if self._cancel_requested:
                        rc = proc.wait(timeout=0.5)
                    else:
                        rc = proc.wait()
                except subprocess.TimeoutExpired:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    try:
                        rc = proc.wait(timeout=0.5)
                    except Exception:
                        rc = -1
                with self._proc_lock:
                    if self._proc is proc:
                        self._proc = None
                if self._cancel_requested:
                    raise RuntimeError("__CANCELLED__")
                if rc != 0:
                    err = ""
                    if proc.stderr:
                        try:
                            err = proc.stderr.read().decode("utf-8", errors="ignore").strip()
                        except Exception:
                            err = ""
                    raise RuntimeError(err or "FFmpeg audio extract failed")

                rms_np = np.array(rms_list, dtype=np.float32)
                return rms_np

            # 1) Primo tentativo: voice-focused
            try:
                rms_np = run_ffmpeg_extract(apply_filter=True)
            except Exception as e:
                # 2) Fallback: nessun filtro (molto più compatibile)
                try:
                    rms_np = run_ffmpeg_extract(apply_filter=False)
                except Exception as e2:
                    # Mostra entrambi gli errori (voice + fallback) per debug reale
                    raise RuntimeError(
                        "Analisi audio fallita.\n\n"
                        f"Con filtro voice-focused: {e}\n\n"
                        f"Senza filtro (fallback): {e2}"
                    )

            if rms_np.size < 10:
                raise RuntimeError("Audio troppo corto o non processabile.")

            # Auto-threshold robusto
            p20 = float(np.percentile(rms_np, 20))
            p90 = float(np.percentile(rms_np, 90))
            auto_thr = p20 + 0.12 * (p90 - p20)
            auto_thr = float(max(0.0008, min(0.06, auto_thr)))

            if duration is None or duration <= 0.0:
                duration = float(rms_np.size) * float(self.hop_s)

            if self._cancel_requested:
                raise RuntimeError("__CANCELLED__")
            self.done.emit(int(self.track_idx), float(duration), rms_np, float(self.hop_s), float(auto_thr))

        except Exception as e:
            self.error.emit(int(self.track_idx), str(e))
        finally:
            with self._proc_lock:
                self._proc = None
