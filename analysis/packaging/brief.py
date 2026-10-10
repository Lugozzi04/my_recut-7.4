"""PackagingBrief: the compact, verifiable evidence package of the FAST path.

The agent gets enough to understand the story, not a raw dump:
  * per game: id, result, edited span, markers, and a budgeted transcript selection
    (every line near a highlight first, then an even time sample of the rest, so
    strategy talk and quiet but important passages survive without audio peaks);
  * highlight candidates with their signals and the frame/webcam ids ALREADY selected
    locally (best settled frame + one different alternative);
  * images: one compact contact sheet per game (selected frames only), one webcam
    sheet (distinct expressions only), at most two full frames with a reference grid
    for reading cards; nothing is sent twice;
  * creator style preferences (compact) and the content/layout constraints.
Every item keeps its original id, so all claims stay checkable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .frames import FrameExtractor, contact_sheet, require_cv2, write_image
from .models import GameContext, HighlightCandidate, TranscriptLine

DEFAULT_TRANSCRIPT_CHARS = 14000
MIN_GAME_CHARS = 1200
NEAR_HIGHLIGHT_SHARE = 0.6  # at most this share of a game's budget goes to highlight context


@dataclass
class PackagingBrief:
    games: list[dict[str, Any]]
    highlights: list[dict[str, Any]]
    transcript: dict[str, list[str]]  # game_id -> rendered lines "T0001 [mm:ss] text"
    selected_line_ids: set[str]
    total_lines: int
    images: list[dict[str, Any]] = field(default_factory=list)
    frame_ids: set[str] = field(default_factory=set)
    webcam_ids: set[str] = field(default_factory=set)

    def stats(self) -> dict[str, Any]:
        return {"games": len(self.games), "highlights": len(self.highlights),
                "transcript_lines_sent": len(self.selected_line_ids), "transcript_lines_total": self.total_lines,
                "images": len(self.images), "frames_listed": len(self.frame_ids), "webcam_listed": len(self.webcam_ids)}


def _clock(t: float | None) -> str:
    if t is None:
        return "--:--"
    return f"{int(t) // 60:02d}:{int(t) % 60:02d}"


def select_transcript(game: GameContext, lines: Sequence[TranscriptLine], highlights: Sequence[HighlightCandidate],
                      budget: int) -> list[TranscriptLine]:
    """Diverse, chronological selection within `budget` characters for one game."""
    if game.edited_start is None or game.edited_end is None:
        return []
    mine = [line for line in lines if game.edited_start - 1e-3 <= line.edited_start <= game.edited_end + 1e-3]
    if sum(len(line.text) + 16 for line in mine) <= budget:
        return mine
    near = {i for h in highlights if h.game_id == game.game_id for i in h.transcript_ids}
    chosen: dict[str, TranscriptLine] = {}
    used = 0
    for line in mine:  # highlight context first (bounded)
        if line.id in near and used + len(line.text) + 16 <= budget * NEAR_HIGHLIGHT_SHARE:
            chosen[line.id] = line
            used += len(line.text) + 16
    rest = [line for line in mine if line.id not in chosen]
    if rest and used < budget:
        # even coverage over time: one line per bin, the most informative (longest, confident) of the bin
        bins = max(1, int((budget - used) / 110))
        span = (game.edited_end - game.edited_start) / bins
        for b in range(bins):
            lo, hi = game.edited_start + b * span, game.edited_start + (b + 1) * span
            candidates = [line for line in rest if lo <= line.edited_start < hi]
            if not candidates:
                continue
            best = max(candidates, key=lambda line: len(line.text) * (line.confidence or 0.6))
            if used + len(best.text) + 16 > budget:
                break
            chosen[best.id] = best
            used += len(best.text) + 16
    return sorted(chosen.values(), key=lambda line: line.edited_start)


def build_brief(*, games: Sequence[GameContext], highlights: Sequence[HighlightCandidate],
                lines: Sequence[TranscriptLine], frames: FrameExtractor, sheets_dir: Path, work_dir: Path,
                transcript_chars: int = DEFAULT_TRANSCRIPT_CHARS, max_images: int = 6,
                grid_frames: int = 2) -> PackagingBrief:
    total_kept = sum(max(1.0, g.kept_seconds) for g in games) or 1.0
    transcript: dict[str, list[str]] = {}
    selected: set[str] = set()
    for game in games:
        budget = max(MIN_GAME_CHARS, int(transcript_chars * max(1.0, game.kept_seconds) / total_kept))
        chosen = select_transcript(game, lines, highlights, budget)
        selected |= {line.id for line in chosen}
        transcript[game.game_id] = [f"{line.id} [{_clock(line.edited_start)}] {line.text}" for line in chosen]
    game_rows = []
    for game in games:
        markers = {k: (v or {}).get("edited") for k, v in game.markers.items()}
        game_rows.append({"game_id": game.game_id, "index": game.index, "result": game.result,
                          "edited": [_clock(game.edited_start), _clock(game.edited_end)],
                          "kept_s": round(game.kept_seconds), "markers": {k: _clock(v) for k, v in markers.items()
                                                                          if v is not None},
                          "highlights": game.highlight_ids})
    highlight_rows, frame_ids, webcam_ids = [], set(), set()
    for h in highlights:
        selected_frames = [f for f in h.frame_ids if frames.records.get(f) and frames.records[f].role in ("peak", "alt")]
        context_frames = [f for f in h.frame_ids if f not in selected_frames]
        webcam = h.webcam_frame_ids[:2]
        frame_ids |= set(selected_frames) | set(context_frames)
        webcam_ids |= set(webcam)
        highlight_rows.append({
            "id": h.id, "game_id": h.game_id, "edited": [_clock(h.edited_start), _clock(h.edited_end)],
            "signals": h.evidence_sources, "reason": h.reason, "coverage": h.coverage,
            "frames": selected_frames, "context_frames": context_frames, "webcam": webcam,
            "transcript_ids": [i for i in h.transcript_ids if i in selected][:6]})
    images = _images(games, highlights, frames, sheets_dir, work_dir, max_images=max_images, grid_frames=grid_frames)
    return PackagingBrief(game_rows, highlight_rows, transcript, selected, len(lines), images, frame_ids, webcam_ids)


def _images(games: Sequence[GameContext], highlights: Sequence[HighlightCandidate], frames: FrameExtractor,
            sheets_dir: Path, work_dir: Path, *, max_images: int, grid_frames: int) -> list[dict[str, Any]]:
    cv2 = require_cv2()
    images: list[dict[str, Any]] = []
    for game in games:  # selected frames only: best + alternative per highlight
        ids = [f for h in highlights if h.game_id == game.game_id for f in h.frame_ids
               if frames.records.get(f) and frames.records[f].role in ("peak", "alt")][:12]
        if not ids:
            continue
        path = sheets_dir / f"brief_{game.game_id}.jpg"
        contact_sheet([frames.records[f] for f in ids], frames.images, path, columns=4, cell_width=480,
                      title=f"{game.game_id} {game.result or ''} selected frames")
        images.append({"path": path, "description": f"{game.game_id}: frames {', '.join(ids)} (id + edited time)"})
    webcam = [frames.records[w] for h in highlights for w in h.webcam_frame_ids[:1] if w in frames.records]
    if webcam:
        path = sheets_dir / "brief_webcam.jpg"
        contact_sheet(webcam[:16], frames.images, path, columns=4, cell_width=240, title="webcam (distinct)")
        images.append({"path": path, "description": f"webcam crops {', '.join(r.id for r in webcam[:16])}"})
    ranked = sorted(highlights, key=lambda h: h.local_score, reverse=True)
    games_done: set[str | None] = set()
    for h in ranked:  # full frames with a grid, for reading cards, from different games first
        if len([i for i in images if i.get("grid")]) >= grid_frames or len(images) >= max_images:
            break
        peak = next((f for f in h.frame_ids if frames.records.get(f) and frames.records[f].role == "peak"), None)
        if peak is None or (h.game_id in games_done and len(games_done) < len({g.game_id for g in games})):
            continue
        games_done.add(h.game_id)
        image = cv2.resize(frames.images[peak], (1280, 720), interpolation=cv2.INTER_AREA)
        for k in range(1, 10):
            cv2.line(image, (128 * k, 0), (128 * k, 719), (0, 255, 255), 1)
            cv2.line(image, (0, 72 * k), (1279, 72 * k), (0, 255, 255), 1)
            cv2.putText(image, f"{k / 10:.1f}", (128 * k + 3, 14), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 255), 1)
            cv2.putText(image, f"{k / 10:.1f}", (3, 72 * k - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 255), 1)
        path = work_dir / f"grid_{peak}.jpg"
        write_image(path, image, quality=88)
        images.append({"path": path, "grid": True,
                       "description": f"frame {peak} ({h.id}) full size with a 0.1 grid, for reading cards"})
    return images[:max_images]
