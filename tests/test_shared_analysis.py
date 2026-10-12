"""Shared source evidence regression tests; no ASR model or external provider is invoked."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from analysis.audio_service import AnalysisCancellation, AnalysisCancelled, AnalysisRequest, _ProgressReporter, _extract_rms
from analysis.boundaries.models import EvidenceStatus, TimeRange, TranscriptSegment, TranscriptWord
from analysis.boundaries.transcript import FasterWhisperEngine, TranscriptSettings
from analysis.gameplay.cache import atomic_write_json, video_identity
from analysis.gameplay.hearthstone.visual.sampler import FFmpegFrameSampler, SampledFrame
from analysis.packaging.cache import PackagingCache
from analysis.packaging.transcript import build_transcript
from analysis.shared.audio import AudioEvidenceStore
from analysis.shared.frames import FrameSamplingService
from analysis.shared.timeline import KeepPiece, TimelineMapper
from analysis.shared.transcript import TranscriptCoverage, clip_segments
from analysis.shared.visual import VisualEvidenceStore


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "video è 🎮.mp4"
    path.write_bytes(b"fixture source")
    return path


def publish(engine, source, window, segments):
    identity = video_identity(source)
    key = engine._key(identity, window)
    atomic_write_json(engine.cache_directory / (key + ".json"), {"key": key,
                     "segments": [s.to_mapping() for s in segments]})
    TranscriptCoverage(engine.cache_directory, identity, engine.settings.engine_options()).register(key, window)


def test_audio_compatible_requests_share_native_rms_and_invalidate(source, tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_CUTTER_CACHE_DIR", str(tmp_path / "cache"))
    values = np.arange(20, dtype=np.float32) / 100
    args = dict(ffmpeg="unused", request=AnalysisRequest(str(source)), duration=2.0, audio_filter="filter",
                cancellation=AnalysisCancellation(), progress=_ProgressReporter(None))
    with patch("analysis.audio_service._extract_rms_uncached", return_value=values) as decode:
        np.testing.assert_array_equal(_extract_rms(**args), values)
        np.testing.assert_array_equal(_extract_rms(**args), values)
        assert decode.call_count == 1
        _extract_rms(**{**args, "start_s": .01, "duration_s": 1.0})
        _extract_rms(**{**args, "request": replace(args["request"], hop_s=.05)})
        _extract_rms(**{**args, "audio_filter": "anull"})
        source.write_bytes(b"changed media source")
        _extract_rms(**args)
        assert decode.call_count == 5


def test_audio_corruption_and_cancellation(source, tmp_path):
    store = AudioEvidenceStore(tmp_path)
    d = store.descriptor(source, hop_s=.03, sample_rate=16000, audio_filter="voice", start_s=None, duration_s=None)
    store.save(d, np.ones(20, np.float32))
    store._path(d).write_bytes(b"partial")
    assert store.load(d) is None
    token = AnalysisCancellation()
    token.cancel()
    with patch("analysis.audio_service._extract_rms_uncached") as decode, pytest.raises(AnalysisCancelled):
        _extract_rms(ffmpeg="unused", request=AnalysisRequest(str(source)), duration=None,
                     audio_filter="voice", cancellation=token, progress=_ProgressReporter(None))
    decode.assert_not_called()


def test_covered_transcript_reused_without_runtime_discovery(source, tmp_path):
    engine = FasterWhisperEngine(cache_directory=tmp_path / "asr")
    words = tuple(TranscriptWord(t, t+.3, str(t)) for t in (1., 3., 5., 7.))
    publish(engine, source, TimeRange(0, 10), [TranscriptSegment(1, 7.3, "1 3 5 7", words=words)])
    with patch("analysis.boundaries.transcript.find_whisper_python", side_effect=AssertionError("duplicate ASR")):
        status, segments = engine.transcribe(source, [TimeRange(2, 6)])
    assert status.status == "cached"
    assert [w.text for s in segments[0] for w in s.words] == ["3.0", "5.0"]
    assert segments[0][0].start == 3


def test_partial_transcript_only_requests_missing_intervals(source, tmp_path):
    engine = FasterWhisperEngine(cache_directory=tmp_path / "asr")
    publish(engine, source, TimeRange(2, 5), [TranscriptSegment(3, 4, "cached")])
    requested = []
    def fake(video, windows, **kwargs):
        requested.extend(windows)
        result = [[TranscriptSegment(w.start, w.end, "new")] for w in windows]
        for w, segs in zip(windows, result):
            publish(engine, source, w, segs)
        return EvidenceStatus("ok", "fake"), result
    with patch.object(engine, "_transcribe_exact", side_effect=fake):
        status, results = engine.transcribe(source, [TimeRange(0, 8), TimeRange(4, 10)])
    assert requested == [TimeRange(0, 2), TimeRange(5, 10)]
    assert status.status == "ok" and len(results) == 2
    assert any(s.text == "cached" for s in results[0])
    with patch("analysis.boundaries.transcript.find_whisper_python", side_effect=AssertionError("duplicate ASR")):
        assert engine.transcribe(source, [TimeRange(1, 9)])[0].status == "cached"


def test_empty_transcript_is_covered_and_different_settings_are_not(source, tmp_path):
    engine = FasterWhisperEngine(cache_directory=tmp_path / "asr")
    publish(engine, source, TimeRange(0, 10), [])
    with patch("analysis.boundaries.transcript.find_whisper_python", return_value=None) as discovery:
        assert engine.transcribe(source, [TimeRange(2, 6)])[0].status == "cached"
        discovery.assert_not_called()
        other = FasterWhisperEngine(TranscriptSettings(language="en"), cache_directory=engine.cache_directory)
        assert other.transcribe(source, [TimeRange(2, 6)])[0].status == "unavailable"
        assert discovery.call_count == 1


def test_legacy_exact_transcript_is_discovered_without_rewriting(source, tmp_path):
    engine = FasterWhisperEngine(cache_directory=tmp_path)
    window = TimeRange(0, 10)
    key = engine._key(video_identity(source), window)
    target = tmp_path / (key + ".json")
    atomic_write_json(target, {"key": key, "segments": [TranscriptSegment(2, 4, "old").to_mapping()]})
    before = target.read_bytes()
    with patch("analysis.boundaries.transcript.find_whisper_python", side_effect=AssertionError("ASR")):
        assert engine.transcribe(source, [window])[0].status == "cached"
        assert engine.transcribe(source, [TimeRange(1, 5)])[1][0][0].text == "old"
    assert target.read_bytes() == before


def test_failed_asr_does_not_publish_false_coverage(source, tmp_path):
    engine = FasterWhisperEngine(cache_directory=tmp_path)
    with patch.object(engine, "_transcribe_exact", return_value=(EvidenceStatus("failed", "fake"), [[]])):
        assert engine.transcribe(source, [TimeRange(0, 10)])[0].status == "failed"
    store = TranscriptCoverage(tmp_path, video_identity(source), engine.settings.engine_options())
    assert store.entries(engine._load) == []


def test_packaging_groups_reuse_source_transcript_and_keep_only_their_words(source, tmp_path):
    engine = FasterWhisperEngine(cache_directory=tmp_path / "asr")
    words = tuple(TranscriptWord(t, t+.5, str(t)) for t in (1., 5., 9., 15., 21., 25.))
    publish(engine, source, TimeRange(0, 30), [TranscriptSegment(1, 25.5, "all", words=words)])
    identity = [video_identity(source)]
    a = TimelineMapper([str(source)], [KeepPiece(0, 0, 6, 0), KeepPiece(0, 20, 26, 6)], origin="project", identities=identity)
    b = TimelineMapper([str(source)], [KeepPiece(0, 8, 16, 0)], origin="project", identities=identity)
    cache = PackagingCache(tmp_path / "packaging")
    with patch("analysis.boundaries.transcript.find_whisper_python", side_effect=AssertionError("duplicate ASR")):
        _, la = build_transcript(a, engine.settings, cache=cache, engine=engine)
        _, lb = build_transcript(b, engine.settings, cache=cache, engine=engine)
        status, warm = build_transcript(a, engine.settings, cache=cache, engine=engine)
    assert status["packaging_cache"] == "hit" and warm == la
    assert [w["text"] for line in la for w in line.words] == ["1.0", "5.0", "21.0", "25.0"]
    assert [w["text"] for line in lb for w in line.words] == ["9.0", "15.0"]
    assert la[-1].edited_start == 7.0 and lb[0].edited_start == 1.0
    assert a.digest() != b.digest()


def test_timeline_mapper_is_existing_implementation_and_manual_gaps():
    from analysis.packaging.timeline import EditMap
    assert TimelineMapper is EditMap
    mapping = TimelineMapper(["source"], [KeepPiece(0, 10, 20, 0), KeepPiece(0, 30, 40, 10)], origin="project")
    assert mapping.to_edited(15) == 5 and mapping.to_edited(25) is None
    assert mapping.to_source(12) == (0, 32)


class FakeSampler:
    def __init__(self, fail=False):
        self.calls = 0
        self.batch_calls = 0
        self.fail = fail
    def sample(self, source, **kwargs):
        self.calls += 1
        yield SampledFrame(kwargs["start_s"] + .01, np.full((16, 24, 3), 35, np.uint8))
    def sample_at(self, source, timestamps, **kwargs):
        self.batch_calls += 1
        if self.fail:
            raise RuntimeError("batch unsupported")
        for t in timestamps:
            yield SampledFrame(t + .01, np.full((16, 24, 3), 35, np.uint8))


def test_batch_frames_order_cache_resolution_and_source_invalidation(source, tmp_path):
    sampler = FakeSampler()
    service = FrameSamplingService(sampler, directory=tmp_path / "frames")
    first = service.get_many(source, [3., 1., 2., 1.])
    assert list(first) == [1., 2., 3.] and sampler.batch_calls == 1 and sampler.calls == 0
    second = service.get_many(source, [1., 3.])
    assert sampler.batch_calls == 1
    np.testing.assert_array_equal(first[1.].image, second[1.].image)
    service.get_many(source, [1., 2.], max_width=1920)
    assert sampler.batch_calls == 2  # Low resolution cannot serve a thumbnail request.
    source.write_bytes(b"other source")
    service.get_many(source, [1., 2.])
    assert sampler.batch_calls == 3


def test_batch_failure_falls_back_but_cancel_never_does(source, tmp_path):
    sampler = FakeSampler(fail=True)
    service = FrameSamplingService(sampler, directory=tmp_path)
    assert len(service.get_many(source, [1., 2.])) == 2 and sampler.calls == 2
    token = AnalysisCancellation()
    token.cancel()
    with pytest.raises(AnalysisCancelled):
        service.get_many(source, [1., 2.], cancellation=token)
    assert sampler.calls == 2
    with patch.object(sampler, "sample_at", side_effect=AnalysisCancelled()), pytest.raises(AnalysisCancelled):
        service.get_many(source, [3., 4.])
    assert sampler.calls == 2


def test_visual_window_cache_is_compact_and_compatible(source, tmp_path):
    from analysis.boundaries.visual import VisualSettings, analyze_visual_window
    from analysis.shared import visual
    sampler = FakeSampler()
    with patch.object(visual, "cache_root", return_value=tmp_path), patch("analysis.shared.frames.FFmpegFrameSampler", return_value=sampler):
        first = analyze_visual_window(source, TimeRange(0, 3), VisualSettings())
        second = analyze_visual_window(source, TimeRange(0, 3), VisualSettings())
    assert first[0].status == "ok" and second[0].status == "cached" and sampler.calls == 1
    store = VisualEvidenceStore(tmp_path / "other")
    descriptor = store.descriptor(source, 0, 3, {"fps": 2})
    store.save(descriptor, [])
    assert store.load(descriptor) == []
    assert store.load({**descriptor, "options": {"fps": 3}}) is None


def test_clip_words_has_single_owner_at_source_seam():
    seg = TranscriptSegment(0, 4, "left right", words=(TranscriptWord(1, 2, "left"), TranscriptWord(2, 3, "right")))
    left = clip_segments([seg], TimeRange(0, 2))
    right = clip_segments([seg], TimeRange(2, 4))
    assert [w.text for s in left+right for w in s.words] == ["left", "right"]


@pytest.mark.parametrize("vfr", [False, True])
def test_real_batch_frames_equal_independent_seeks(tmp_path, vfr):
    from contextlib import closing
    from utils.ffmpeg import ensure_ffmpeg
    from utils.subprocess_utils import run_no_window
    ffmpeg, _ = ensure_ffmpeg()
    source = tmp_path / "source è 🎮.mkv"
    vf = "select='not(between(n,10,14))',setpts=PTS+5/TB" if vfr else "null"
    created = run_no_window([ffmpeg, "-hide_banner", "-v", "error", "-f", "lavfi", "-i",
                            "testsrc2=size=320x180:rate=60:duration=3", "-vf", vf, "-fps_mode", "vfr",
                            "-c:v", "ffv1", "-y", str(source)], capture_output=True, timeout=30)
    assert created.returncode == 0, created.stderr
    times = [.03, .21, .32, .85, 1.4, 2.6]
    sampler = FFmpegFrameSampler()
    reference = {}
    for t in times:
        with closing(sampler.sample(source, fps=1., start_s=t, end_s=t+.1, max_width=240)) as frames:
            reference[t] = next(frames, None)
    service = FrameSamplingService(sampler, directory=tmp_path / "selected")
    batch = service.get_many(source, times, max_width=240)
    for t in times:
        if reference[t] is None:
            assert batch[t] is None
        else:
            assert batch[t] is not None and batch[t].timestamp == reference[t].timestamp
            np.testing.assert_array_equal(batch[t].image, reference[t].image)
    cached = service.get_many(source, times, max_width=240)
    for t in times:
        if batch[t] is not None:
            np.testing.assert_array_equal(batch[t].image, cached[t].image)


def test_frozen_matcher_prepared_gray_equals_legacy_full_frame():
    import cv2
    from analysis.gameplay.hearthstone.state_aware import StateAwareHearthstoneAnalyzer
    from analysis.gameplay.hearthstone.visual.detector import normalize_frame
    from analysis.gameplay.models import GameEventType
    from analysis.gameplay_editing import default_detector_assets
    assets = default_detector_assets("hearthstone")
    analyzer = StateAwareHearthstoneAnalyzer(**assets, use_cache=False)
    rng = np.random.default_rng(77)
    for _ in range(3):
        frame = rng.integers(0, 256, (540, 960, 3), dtype=np.uint8)
        normalized = normalize_frame(frame, analyzer.profile.reference_size, analyzer.profile.viewport)
        gray = cv2.cvtColor(normalized, cv2.COLOR_BGR2GRAY)
        assert analyzer.board.score(gray) == analyzer.board.score(normalized)
        left, top, right, bottom = analyzer.profile.result_roi.pixel_bounds(*analyzer.profile.reference_size)
        assert analyzer.text.score_roi(gray[top:bottom, left:right]) == analyzer.text.score_roi(normalized[top:bottom, left:right])
        kinds = (GameEventType.VS_SCREEN,)
        legacy = analyzer.vs.detect_candidates(frame, 3., minimum_similarity=.01, event_types=kinds)
        prepared = analyzer.vs.detect_reference_candidates(gray, 3., minimum_similarity=.01, event_types=kinds)
        assert [e.to_mapping() for e in legacy] == [e.to_mapping() for e in prepared]


def test_profile_records_actual_reuse_and_skips_asr(source, tmp_path):
    from analysis.performance_profile import GameplayProfiler
    engine = FasterWhisperEngine(cache_directory=tmp_path / "asr")
    publish(engine, source, TimeRange(0, 10), [])
    profiler = GameplayProfiler(source, {}, save=False)
    with profiler.activate():
        engine.transcribe(source, [TimeRange(2, 5)])
    snapshot = profiler.snapshot()
    phases = snapshot["phases"]
    assert phases["shared.transcript"]["cache"] == "hit"
    assert phases["boundaries.transcript.inference"]["status"] == "skipped"


def test_transcript_cache_lock_unavailable_keeps_uncached_fallback(source, tmp_path):
    from automation.locking import FileLockError
    engine = FasterWhisperEngine(cache_directory=tmp_path / "asr")
    with patch("analysis.shared.transcript.ProcessFileLock.hold", side_effect=FileLockError("read only")), \
            patch("analysis.boundaries.transcript.find_whisper_python", return_value=None):
        assert engine.transcribe(source, [TimeRange(0, 10)])[0].status == "unavailable"



def test_win_boundary_prefetch_uses_one_helper_then_cached_evidence(source, tmp_path):
    from analysis.boundaries.analyzer import BoundaryAnalyzer, BoundarySettings
    from analysis.boundaries.audio import AudioEvidence
    from analysis.gameplay.models import GameplayAnalysisResult, GameResult, GameSegment
    engine = FasterWhisperEngine(cache_directory=tmp_path / "asr")
    analyzer = BoundaryAnalyzer(BoundarySettings(keep_policy="wins"), transcription=engine)
    analysis = GameplayAnalysisResult(str(source), 1000, [], [
        GameSegment("g1", 1, 100, 300, GameResult.WIN), GameSegment("g2", 2, 400, 700, GameResult.WIN)], "fixture")
    helpers = []
    def spawn(command, **kwargs):
        helpers.append(command)
        job = json.loads(Path(command[-2]).read_text(encoding="utf-8"))
        output = Path(command[-1])
        output.write_text(json.dumps([{"segments": [{"start": .1, "end": .5, "text": "word"}]} for _ in job["files"]]), encoding="utf-8")
        return SimpleNamespace(returncode=0, communicate=lambda: (b"", b""))
    def wav(video, window, output, **kwargs):
        return output  # No audio is decoded and no ASR engine is launched in this test.
    with patch("analysis.boundaries.transcript.find_whisper_python", return_value="fake"), \
            patch("analysis.boundaries.transcript.extract_wav", side_effect=wav), \
            patch("utils.subprocess_utils.popen_no_window", side_effect=spawn), \
            patch("analysis.boundaries.analyzer.analyze_audio_window", return_value=AudioEvidence(EvidenceStatus("unavailable", "fake"), [], [])), \
            patch("analysis.boundaries.analyzer.analyze_visual_window", return_value=(EvidenceStatus("disabled", "fake"), [])):
        analyzer.prefetch_kept_transcripts(source, analysis, [1, 2], cancellation=AnalysisCancellation())
        assert len(helpers) == 1
        for index in (1, 2):
            boundary = analyzer.analyze_game(source, analysis, index, require_removable=False)
            assert all(context.transcript_status.status == "cached" for context in boundary.contexts.values())
        assert len(helpers) == 1


def test_concurrent_consumers_do_not_duplicate_source_asr(source, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import time
    engines = [FasterWhisperEngine(cache_directory=tmp_path / "asr") for _ in range(2)]
    calls = []
    start = threading.Barrier(2)
    def exact(engine, video, windows, **kwargs):
        if all(engine._load(engine._key(video_identity(source), w)) is not None for w in windows):
            return EvidenceStatus("cached", "fake"), [[] for _ in windows]
        calls.append(windows)
        time.sleep(.03)
        for w in windows:
            publish(engine, source, w, [])
        return EvidenceStatus("ok", "fake"), [[] for _ in windows]
    def run(index):
        start.wait()
        return engines[index].transcribe(source, [TimeRange(0, 10)])
    with patch.object(engines[0], "_transcribe_exact", side_effect=lambda *a, **k: exact(engines[0], *a, **k)), \
            patch.object(engines[1], "_transcribe_exact", side_effect=lambda *a, **k: exact(engines[1], *a, **k)), \
            ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, [0, 1]))
    assert len(calls) == 1 and {status.status for status, _ in results} == {"ok", "cached"}



def test_frame_cache_corruption_is_a_miss_not_a_detection_failure(source, tmp_path):
    sampler = FakeSampler()
    service = FrameSamplingService(sampler, directory=tmp_path / "frames")
    service.get_many(source, [1., 2.])
    for cached in service.directory.glob("*.npz"):
        cached.write_bytes(b"PK truncated archive")
    service.get_many(source, [1., 2.])
    assert sampler.batch_calls == 2


def test_cancel_while_waiting_for_transcript_owner_is_prompt(source, tmp_path):
    import threading
    from automation.locking import ProcessFileLock
    engine = FasterWhisperEngine(cache_directory=tmp_path / "asr")
    coverage = TranscriptCoverage(engine.cache_directory, video_identity(source), engine.settings.engine_options())
    token = AnalysisCancellation()
    errors = []
    def waiting():
        try:
            engine.transcribe(source, [TimeRange(0, 10)], cancellation=token)
        except AnalysisCancelled:
            errors.append("cancelled")
    with ProcessFileLock(coverage.directory / ".lock").hold():
        thread = threading.Thread(target=waiting)
        thread.start()
        token.cancel()
        thread.join(timeout=1)
        assert not thread.is_alive()
    assert errors == ["cancelled"]
