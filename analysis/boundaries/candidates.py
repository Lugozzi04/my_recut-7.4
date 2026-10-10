"""Candidate generation, deduplication and per-candidate features. No decision here.

Candidates come from gameplay anchors/markers, silence edges/midpoints,
transcript segment and sentence edges and (optionally) strong visual changes.
Near-duplicates are merged keeping every source. Raw evidence (texts, regions)
is stored apart from the derived numeric features; a feature is None when its
modality was not analysed, never a fabricated zero.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .models import (BoundaryType, CandidateBoundary, CandidateSource, EvidenceStatus, GameplayMarker, TimeRange,
                     TranscriptSegment, VisualSample)


@dataclass(frozen=True)
class CandidateSettings:
    dedup_tolerance_s: float = 0.25  # ~ 8 RMS hops; finer than any silence the editor would cut
    silence_midpoint_min_s: float = 1.0  # midpoint only for pauses long enough to cut inside
    context_s: float = 5.0  # speech density / word counts look this far on each side
    edge_tolerance_s: float = 0.1  # "at a silence/transcript edge" tolerance
    visual_radius_s: float = 0.75  # visual_change_score = max change within +-radius
    visual_candidate_min_change: float | None = 0.35  # None disables VISUAL_CHANGE candidates


# Earlier entries win the merged timestamp: exact anchors, then speech structure, then audio, then hints.
PRIORITY = [CandidateSource.GAME_START_ANCHOR, CandidateSource.RESULT_ANCHOR, CandidateSource.END_OF_FILE,
            CandidateSource.START_OF_FILE, CandidateSource.NEXT_GAME_START, CandidateSource.PREVIOUS_GAME_END,
            CandidateSource.SENTENCE_END, CandidateSource.SENTENCE_START, CandidateSource.TRANSCRIPT_SEGMENT_END,
            CandidateSource.TRANSCRIPT_SEGMENT_START, CandidateSource.SILENCE_START, CandidateSource.SILENCE_END,
            CandidateSource.BOARD_DISAPPEARED, CandidateSource.BOARD_REACQUIRED, CandidateSource.VISUAL_CHANGE,
            CandidateSource.SILENCE_MID]

FEATURE_NAMES: list[str] = [
    "is_end_boundary", "offset_from_anchor_s", "distance_from_game_start_s", "distance_from_game_end_s",
    "distance_to_previous_game_end_s", "distance_to_next_game_start_s", "relative_window_position",
    "distance_to_window_start_s", "distance_to_window_end_s",
    "silence_before_s", "silence_after_s", "speech_before_s", "speech_after_s",
    "speech_density_before", "speech_density_after", "is_inside_speech", "is_inside_silence",
    "rms_db_before", "rms_db_after",
    "transcript_gap_before_s", "transcript_gap_after_s", "is_inside_transcript_segment",
    "words_before", "words_after", "is_sentence_boundary", "is_transcript_segment_boundary",
    "visual_change_score", "nearest_gameplay_marker_s", "source_count",
    *[f"source_{source.value.lower()}" for source in CandidateSource],
]


@dataclass
class Evidence:
    """Everything features are derived from; statuses decide None vs value."""

    boundary_type: BoundaryType
    window: TimeRange
    anchor: float
    anchors: dict[str, float | None]
    audio_status: EvidenceStatus
    silences: Sequence[TimeRange]
    speech: Sequence[TimeRange]
    transcript_status: EvidenceStatus
    transcript: Sequence[TranscriptSegment]
    visual_status: EvidenceStatus
    visual: Sequence[VisualSample]
    markers: Sequence[GameplayMarker]
    rms_db: Any = None  # callable(start, end) -> float | None


def raw_candidates(ev: Evidence, settings: CandidateSettings) -> list[tuple[float, CandidateSource, dict[str, Any]]]:
    w, eps = ev.window, 1e-6
    out: list[tuple[float, CandidateSource, dict[str, Any]]] = []

    def add(t: float | None, source: CandidateSource, **detail: Any) -> None:
        if t is not None and w.start - eps <= t <= w.end + eps:
            out.append((min(w.end, max(w.start, t)), source, detail))

    a = ev.anchors
    if ev.boundary_type == BoundaryType.START:
        add(a.get("game_start"), CandidateSource.GAME_START_ANCHOR)
        add(a.get("previous_game_end"), CandidateSource.PREVIOUS_GAME_END)
        if w.start <= eps:
            add(0.0, CandidateSource.START_OF_FILE)
    else:
        add(a.get("game_end"), CandidateSource.RESULT_ANCHOR)
        add(a.get("next_game_start"), CandidateSource.NEXT_GAME_START)
        eof = a.get("end_of_file")
        if eof is not None and w.end >= eof - eps:
            add(eof, CandidateSource.END_OF_FILE)
    for marker in ev.markers:
        try:
            add(marker.timestamp, CandidateSource(marker.kind))
        except ValueError:
            continue
    for region in ev.silences:
        if region.start > w.start + eps:
            add(region.start, CandidateSource.SILENCE_START, duration_s=region.duration)
        if region.end < w.end - eps:
            add(region.end, CandidateSource.SILENCE_END, duration_s=region.duration)
        if region.duration >= settings.silence_midpoint_min_s:
            add((region.start + region.end) / 2, CandidateSource.SILENCE_MID, duration_s=region.duration)
    for segment in ev.transcript:
        add(segment.start, CandidateSource.TRANSCRIPT_SEGMENT_START)
        add(segment.end, CandidateSource.TRANSCRIPT_SEGMENT_END, ends_sentence=segment.ends_sentence)
        for end, following in segment.sentence_boundaries():
            add(end, CandidateSource.SENTENCE_END)
            add(following, CandidateSource.SENTENCE_START)
    if settings.visual_candidate_min_change is not None:
        samples = list(ev.visual)
        for i, sample in enumerate(samples):
            neighbours = [s.change for s in samples[max(0, i - 1):i + 2] if s is not sample]
            if sample.change >= settings.visual_candidate_min_change and all(sample.change >= n for n in neighbours):
                add(sample.timestamp, CandidateSource.VISUAL_CHANGE, change=sample.change)
    return out


def deduplicate(raw: list[tuple[float, CandidateSource, dict[str, Any]]], tolerance: float
                ) -> list[tuple[float, tuple[CandidateSource, ...], dict[str, Any]]]:
    clusters: list[list[tuple[float, CandidateSource, dict[str, Any]]]] = []
    for item in sorted(raw, key=lambda x: (x[0], PRIORITY.index(x[1]))):
        if clusters and item[0] - clusters[-1][0][0] <= tolerance + 1e-9:
            clusters[-1].append(item)
        else:
            clusters.append([item])
    merged = []
    for cluster in clusters:
        best = min(cluster, key=lambda x: (PRIORITY.index(x[1]), x[0]))
        sources = tuple(sorted({x[1] for x in cluster}, key=PRIORITY.index))
        detail: dict[str, Any] = {}
        if len(cluster) > 1:
            detail["merged_timestamps"] = sorted({round(x[0], 3) for x in cluster})
        for _, source, extra in cluster:
            if extra:
                detail[source.value.lower()] = extra
        merged.append((best[0], sources, detail))
    return merged


def _adjacent(regions: Sequence[TimeRange], t: float, tol: float) -> tuple[float, float, TimeRange | None]:
    """Contiguous duration of `regions` just before and just after t (0 when t is not touching one)."""
    before = after = 0.0
    touching = None
    for r in regions:
        if r.start - tol <= t <= r.end + tol:
            touching = r
            before = max(before, min(t, r.end) - r.start) if t >= r.start - tol else before
            after = max(after, r.end - max(t, r.start)) if t <= r.end + tol else after
    return max(0.0, before), max(0.0, after), touching


def _coverage(regions: Sequence[TimeRange], start: float, end: float) -> float | None:
    if end <= start:
        return None
    covered = sum(max(0.0, min(end, r.end) - max(start, r.start)) for r in regions)
    return min(1.0, covered / (end - start))


def _segment_brief(segment: TranscriptSegment | None) -> dict[str, Any] | None:
    if segment is None:
        return None
    return {"start": round(segment.start, 3), "end": round(segment.end, 3), "text": segment.text,
            "confidence": segment.confidence}


def features(t: float, sources: Sequence[CandidateSource], ev: Evidence, settings: CandidateSettings
             ) -> tuple[dict[str, float | None], dict[str, Any]]:
    f: dict[str, float | None] = dict.fromkeys(FEATURE_NAMES)
    evidence: dict[str, Any] = {}
    w, a, tol, ctx = ev.window, ev.anchors, settings.edge_tolerance_s, settings.context_s

    def rel(value: float | None) -> float | None:
        return None if value is None else t - value

    f["is_end_boundary"] = 1.0 if ev.boundary_type == BoundaryType.END else 0.0
    f["offset_from_anchor_s"] = t - ev.anchor
    f["distance_from_game_start_s"] = rel(a.get("game_start"))
    f["distance_from_game_end_s"] = rel(a.get("game_end"))
    f["distance_to_previous_game_end_s"] = rel(a.get("previous_game_end"))
    nxt = a.get("next_game_start")
    f["distance_to_next_game_start_s"] = None if nxt is None else nxt - t
    f["distance_to_window_start_s"], f["distance_to_window_end_s"] = t - w.start, w.end - t
    f["relative_window_position"] = (t - w.start) / w.duration if w.duration > 0 else 0.0

    if ev.audio_status.available:
        sb, sa, silence = _adjacent(ev.silences, t, tol)
        pb, pa, speech = _adjacent(ev.speech, t, tol)
        inside_silence = any(r.start + tol < t < r.end - tol for r in ev.silences)
        inside_speech = any(r.start + tol < t < r.end - tol for r in ev.speech)
        f.update({"silence_before_s": sb, "silence_after_s": sa, "speech_before_s": pb, "speech_after_s": pa,
                  "speech_density_before": _coverage(ev.speech, max(w.start, t - ctx), t),
                  "speech_density_after": _coverage(ev.speech, t, min(w.end, t + ctx)),
                  "is_inside_speech": float(inside_speech), "is_inside_silence": float(inside_silence)})
        if ev.rms_db is not None:
            f["rms_db_before"], f["rms_db_after"] = ev.rms_db(max(w.start, t - 1.0), t), ev.rms_db(t, min(w.end, t + 1.0))
        evidence["silence_region"] = silence.to_mapping() if silence else None
        evidence["speech_region"] = speech.to_mapping() if speech else None

    if ev.transcript_status.available:
        segs = sorted(ev.transcript, key=lambda s: s.start)
        before = [s for s in segs if s.end <= t + tol]
        after = [s for s in segs if s.start >= t - tol]
        inside = [s for s in segs if s.start + tol < t < s.end - tol]
        # A segment touching t within the tolerance counts as gap 0, never a negative gap.
        f["transcript_gap_before_s"] = 0.0 if inside else (max(0.0, t - before[-1].end) if before else None)
        f["transcript_gap_after_s"] = 0.0 if inside else (max(0.0, after[0].start - t) if after else None)
        f["is_inside_transcript_segment"] = float(bool(inside))
        words = [wd for s in segs for wd in s.words]
        f["words_before"] = float(sum(1 for wd in words if t - ctx <= wd.start < t))
        f["words_after"] = float(sum(1 for wd in words if t <= wd.start < t + ctx))
        sentence_edges = [x for s in segs for pair in s.sentence_boundaries() for x in pair]
        sentence_edges += [s.end for s in segs if s.ends_sentence]
        f["is_sentence_boundary"] = float(any(abs(t - x) <= tol for x in sentence_edges))
        f["is_transcript_segment_boundary"] = float(any(abs(t - s.start) <= tol or abs(t - s.end) <= tol for s in segs))
        evidence["transcript_segment_before"] = _segment_brief(before[-1] if before else None)
        evidence["transcript_segment_after"] = _segment_brief(after[0] if after else None)
        evidence["transcript_segment_containing"] = _segment_brief(inside[0] if inside else None)

    if ev.visual_status.available:
        near = [s.change for s in ev.visual if abs(s.timestamp - t) <= settings.visual_radius_s]
        f["visual_change_score"] = max(near) if near else 0.0

    points = [m.timestamp for m in ev.markers] + [v for v in a.values() if v is not None]
    f["nearest_gameplay_marker_s"] = min(abs(t - p) for p in points) if points else None
    f["source_count"] = float(len(sources))
    for source in CandidateSource:
        f[f"source_{source.value.lower()}"] = float(source in sources)
    return f, evidence


def generate_candidates(ev: Evidence, settings: CandidateSettings) -> list[CandidateBoundary]:
    out = []
    for t, sources, detail in deduplicate(raw_candidates(ev, settings), settings.dedup_tolerance_s):
        values, evidence = features(t, sources, ev, settings)
        out.append(CandidateBoundary(round(t, 3), ev.boundary_type, sources, {**detail, **evidence}, values))
    return out


def feature_vector(candidate: CandidateBoundary, names: Sequence[str] = FEATURE_NAMES
                   ) -> tuple[list[float], list[float]]:
    """(values, mask) for a model: None -> 0.0 with mask 0.0, so missing is explicit."""
    values, mask = [], []
    for name in names:
        value = candidate.features.get(name)
        values.append(0.0 if value is None else float(value))
        mask.append(0.0 if value is None else 1.0)
    return values, mask
