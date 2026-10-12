"""Short generated media only. No VOD rendering, detection, ASR or agent calls."""

from pathlib import Path
import pytest

pytest.importorskip("cv2")
from analysis.hero_groups.recognition import PortraitSettings, group_signatures
from analysis.hero_groups.projects import prepare_outputs, export_outputs
from automation.manager import PipelineManager
from automation.store import PipelineStore
from test_hero_groups import art, record


@pytest.mark.parametrize("named_classes", [False, True])
@pytest.mark.parametrize("remove_first_win", [False, True])
def test_two_group_videos_use_existing_renderer_and_validation(tmp_path, named_classes, remove_first_win):
    from utils.ffmpeg import ensure_ffmpeg, ffprobe_duration_seconds, has_audio_stream, has_video_stream
    from utils.subprocess_utils import run_no_window

    try:
        ffmpeg, _ = ensure_ffmpeg()
    except Exception as exc:
        pytest.skip(str(exc))
    source = tmp_path / "source è 🎮.mp4"
    created = run_no_window(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=24:duration=3",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=3",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            "-shortest",
            "-y",
            str(source),
        ],
        capture_output=True,
        timeout=20,
    )
    assert created.returncode == 0
    rows = [record(1, art()), record(2, art(2)), record(3, art())]
    for i, r in enumerate(rows):
        r["game"].update(start=i + 0.1, end=i + 0.9)
    if named_classes:
        from analysis.hero_groups.classes import class_groups

        for row, name in zip(rows, ("hunter", "paladin", "hunter")):
            row["class_evidence"] = name
        grouping = class_groups(rows, PortraitSettings())
    else:
        grouping = group_signatures(rows, PortraitSettings())
    games = [r["game"] for r in grouping["games"]]
    islands = [{"start": g["start"], "end": g["end"], "game": g} for g in games]
    keeps = [{"start": g["start"], "end": g["end"]} for g in games]
    payload = {
        "format": "autocutter_project",
        "version": 2,
        "global": {
            "export_settings": {
                "codec": "libx264",
                "method": "filter_concat",
                "parallel_workers": 1,
                "hwaccel_decode": False,
            }
        },
        "tracks": [
            {
                "path": str(source),
                "duration": 3.0,
                "segment_source_in": 0.0,
                "segment_source_out": 3.0,
                "cuts_enabled": True,
                "keeps": keeps,
                "gameplay_mode": "keep",
                "gameplay_keeps": keeps,
                "gameplay_report": {"games": games, "keep_islands": islands},
                "gameplay_grouping": grouping,
            }
        ],
    }
    if remove_first_win:
        from analysis.cut_engine import Segment
        from core.track_state import TrackState
        from ui.gameplay_editing_ui import island_metadata, remove_gameplay_island

        data = payload["tracks"][0]
        state = TrackState(path=str(source), duration=3.0, segment_source_out=3.0,
                           gameplay_keeps=[Segment(**k) for k in keeps],
                           gameplay_islands=island_metadata(data["gameplay_report"], 0, 3))
        remove_gameplay_island([state], 0, 0)
        data["gameplay_keeps"] = [{"start": k.start, "end": k.end} for k in state.gameplay_keeps]
        data["keeps"] = data["gameplay_keeps"]
        data["gameplay_islands"] = state.gameplay_islands
    plans, warnings = prepare_outputs(payload, tmp_path / "other directory")
    assert not warnings
    manager = PipelineManager(PipelineStore(tmp_path / "jobs.json"))
    outputs = export_outputs(plans, manager=manager)
    assert len(outputs) == 2
    for output, expected in zip(outputs, (0.8 if remove_first_win else 1.6, 0.8)):
        path = Path(output["output"])
        assert path.is_file() and has_video_stream(str(path)) and has_audio_stream(str(path))
        assert abs(ffprobe_duration_seconds(str(path)) - expected) < 0.15
        assert path.with_suffix(".mp4.automation.json").is_file()
