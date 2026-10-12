"""Coverage index over the existing Whisper JSON artifacts (no second transcript cache)."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Iterator

from analysis.audio_service import AnalysisCancellation
from analysis.boundaries.models import TimeRange, TranscriptSegment
from analysis.gameplay.cache import atomic_write_json
from automation.locking import FileLockBusyError, FileLockError, ProcessFileLock


def clip_segments(segments: list[TranscriptSegment], window: TimeRange) -> list[TranscriptSegment]:
    out = []
    for segment in segments:
        if segment.end <= window.start or segment.start >= window.end:
            continue
        if segment.start >= window.start and segment.end <= window.end:
            out.append(segment)
            continue
        words = tuple(replace(w, start=max(w.start, window.start), end=min(w.end, window.end))
                      for w in segment.words if window.start <= (w.start + w.end) / 2 < window.end)
        if words:
            out.append(replace(segment, start=words[0].start, end=words[-1].end,
                               text=" ".join(w.text for w in words), words=words))
        elif not segment.words and window.start <= (segment.start + segment.end) / 2 < window.end:
            out.append(replace(segment, start=max(segment.start, window.start), end=min(segment.end, window.end)))
    return out


class TranscriptCoverage:
    """Small references indexed by source/config; transcript text stays in old JSON files."""
    def __init__(self, directory: Path, identity: dict[str, Any], options: dict[str, Any]) -> None:
        self.descriptor = {"schema": 1, "algorithm": "faster-whisper-v1", "source": identity, "options": options}
        key = hashlib.sha256(json.dumps(self.descriptor, sort_keys=True).encode()).hexdigest()
        self.directory = directory / "coverage" / key

    @contextmanager
    def hold(self, token: AnalysisCancellation) -> Iterator[None]:
        lock = ProcessFileLock(self.directory / ".lock")
        while True:
            token.raise_if_cancelled()
            try:
                held = lock.hold(blocking=False)
                held.__enter__()
            except FileLockBusyError:
                import time
                time.sleep(0.05)
                continue
            except FileLockError:
                # Read-only/unavailable cache: preserve the existing uncached path.
                yield
                return
            break
        try:
            token.raise_if_cancelled()
            yield
        finally:
            held.__exit__(None, None, None)

    def register(self, key: str, window: TimeRange) -> None:
        try:
            atomic_write_json(self.directory / (key + ".json"),
                              {**self.descriptor, "key": key, "window": window.to_mapping()})
        except OSError:
            pass

    def entries(self, load: Callable[[str], list[TranscriptSegment] | None]
                ) -> list[tuple[TimeRange, list[TranscriptSegment]]]:
        out = []
        try:
            paths = list(self.directory.glob("*.json"))
        except OSError:
            return []
        for path in paths:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if any(raw.get(k) != v for k, v in self.descriptor.items()):
                    continue
                key = raw["key"]
                if not isinstance(key, str) or len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
                    continue
                segments = load(key)
                if segments is not None:  # Empty but successfully transcribed windows are covered too.
                    out.append((TimeRange.from_mapping(raw["window"]), segments))
            except (OSError, ValueError, TypeError, KeyError):
                continue
        return sorted(out, key=lambda entry: (entry[0].start, -entry[0].end))

    @staticmethod
    def plan(window: TimeRange, entries: list[tuple[TimeRange, list[TranscriptSegment]]]
             ) -> tuple[list[tuple[TimeRange, list[TranscriptSegment]]], list[TimeRange]]:
        covered = []
        missing = []
        cursor = window.start
        for span, segments in entries:
            a, b = max(cursor, span.start), min(window.end, span.end)
            if b <= a:
                continue
            if a > cursor:
                missing.append(TimeRange(cursor, a))
            covered.append((TimeRange(a, b), segments))
            cursor = b
        if cursor < window.end:
            missing.append(TimeRange(cursor, window.end))
        return covered, missing
