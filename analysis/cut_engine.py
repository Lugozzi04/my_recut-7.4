from dataclasses import dataclass
from typing import List
import numpy as np


@dataclass
class Segment:
    start: float
    end: float

    @property
    def dur(self) -> float:
        return max(0.0, self.end - self.start)


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def merge_overlaps(segs: List[Segment]) -> List[Segment]:
    if not segs:
        return []
    segs = sorted(segs, key=lambda s: s.start)
    out = [Segment(segs[0].start, segs[0].end)]
    for s in segs[1:]:
        last = out[-1]
        if s.start <= last.end:
            last.end = max(last.end, s.end)
        else:
            out.append(Segment(s.start, s.end))
    return out


def runs_to_segments(mask: np.ndarray, hop_s: float) -> List[Segment]:
    if mask.size == 0:
        return []
    segs: List[Segment] = []
    in_run = bool(mask[0])
    run_start = 0
    for i in range(1, mask.size):
        if bool(mask[i]) != in_run:
            if in_run:
                segs.append(Segment(run_start * hop_s, i * hop_s))
            in_run = bool(mask[i])
            run_start = i
    if in_run:
        segs.append(Segment(run_start * hop_s, mask.size * hop_s))
    return segs


def _moving_average_same(x: np.ndarray, win: int) -> np.ndarray:
    """
    Media mobile con output same-length.
    win<=1 => x
    """
    if win <= 1 or x.size == 0:
        return x
    win = int(win)
    if win % 2 == 0:
        win += 1
    kernel = np.ones(win, dtype=np.float32) / float(win)
    return np.convolve(x.astype(np.float32), kernel, mode="same").astype(np.float32)


def _apply_attack_release_to_silent_mask(
    silent: np.ndarray,
    hop_s: float,
    attack_ms: float,
    release_ms: float,
) -> np.ndarray:
    """
    Debounce temporale sulle transizioni:
    - Attack: richiede N frame consecutivi "voice" per uscire da silent.
    - Release: richiede N frame consecutivi "silent" per entrare in silent.

    silent[i] = True => silenzio, False => voce
    """
    if silent.size == 0:
        return silent

    atk_frames = int(round(max(0.0, float(attack_ms)) / 1000.0 / float(hop_s)))
    rel_frames = int(round(max(0.0, float(release_ms)) / 1000.0 / float(hop_s)))

    if atk_frames <= 0 and rel_frames <= 0:
        return silent

    out = np.empty_like(silent, dtype=bool)

    # Stato corrente
    cur_silent = bool(silent[0])
    out[0] = cur_silent

    run_len = 1  # lunghezza run del "nuovo stato osservato" (diverso da cur_silent)

    for i in range(1, silent.size):
        obs_silent = bool(silent[i])

        if obs_silent == cur_silent:
            # nessuna transizione in corso
            run_len = 1
            out[i] = cur_silent
            continue

        # transizione in corso: contiamo quanto dura il nuovo stato osservato
        run_len += 1

        if cur_silent and (not obs_silent):
            # silent -> voice: serve attack
            if run_len >= max(1, atk_frames):
                cur_silent = False
                run_len = 1
        else:
            # voice -> silent: serve release
            if run_len >= max(1, rel_frames):
                cur_silent = True
                run_len = 1

        out[i] = cur_silent

    return out


def compute_cuts_from_rms(
    rms: np.ndarray,
    duration: float,
    hop_s: float,
    threshold: float,
    min_silence: float,
    edge_keep: float,
    pre_roll: float = 0.0,
    min_cut: float = 0.0,
    aggressiveness: int = 70,
    max_extra_delay: float = 1.0,
    # NEW (all optional / backward-compatible):
    detection_smoothing_ms: float = 0.0,
    attack_ms: float = 0.0,
    release_ms: float = 0.0,
    merge_short_pauses_ms: float = 0.0,
) -> List[Segment]:
    """
    Genera i segmenti di CUT (silenzio da rimuovere) a partire da RMS.

    NEW:
      - detection_smoothing_ms: media mobile su RMS prima del threshold
      - attack_ms / release_ms: debounce temporale sulle transizioni voice/silence
      - merge_short_pauses_ms: rafforza min_silence (non taglia pause più brevi)
    """
    if rms.size == 0 or duration <= 0:
        return []

    # 1) optional smoothing
    if detection_smoothing_ms and detection_smoothing_ms > 0.0:
        win = int(round((float(detection_smoothing_ms) / 1000.0) / float(hop_s)))
        win = max(1, win)
        rms_used = _moving_average_same(rms, win)
    else:
        rms_used = rms

    # 2) base silent mask
    silent = (rms_used < float(threshold))

    # 3) optional attack/release debounce
    if (attack_ms and attack_ms > 0.0) or (release_ms and release_ms > 0.0):
        silent = _apply_attack_release_to_silent_mask(silent, hop_s, attack_ms, release_ms)

    # 4) Merge short pauses (UX): implementato come minimo vincolo su min_silence
    if merge_short_pauses_ms and merge_short_pauses_ms > 0.0:
        merge_s = float(merge_short_pauses_ms) / 1000.0
        min_silence = max(float(min_silence), merge_s)

    silence_segs = runs_to_segments(silent, hop_s)

    # aggressiveness: 100 = taglio subito, 0 = lascia più silenzio prima del taglio
    aggr = int(clamp(float(aggressiveness), 0.0, 100.0))
    extra_delay = float(max_extra_delay) * (1.0 - aggr / 100.0)

    cuts: List[Segment] = []
    for s in silence_segs:
        if s.dur < float(min_silence):
            continue

        cs = s.start + float(edge_keep) + float(extra_delay)
        ce = s.end - float(edge_keep) - max(0.0, float(pre_roll))

        # anti micro-cuts: se dopo i padding il taglio è troppo corto, ignoralo
        if (ce - cs) < float(min_cut):
            continue

        if ce > cs:
            cuts.append(Segment(clamp(cs, 0.0, duration), clamp(ce, 0.0, duration)))

    return merge_overlaps(cuts)


def invert_to_keeps(duration: float, cuts: List[Segment], min_keep: float) -> List[Segment]:
    if duration <= 0:
        return []
    if not cuts:
        return [Segment(0.0, duration)]

    keeps: List[Segment] = []
    cur = 0.0
    for c in cuts:
        if c.start > cur and (c.start - cur) >= float(min_keep):
            keeps.append(Segment(cur, c.start))
        cur = max(cur, c.end)
    if duration > cur and (duration - cur) >= float(min_keep):
        keeps.append(Segment(cur, duration))
    return keeps
