from __future__ import annotations

from pathlib import Path
import sys

import pytest

from automation.manager import PipelineManager
from automation.models import PipelineJob
from automation.paths import requested_output, reserve_output, safe_output_name
from automation.store import PipelineStore
from export.output_safety import OutputSourceCollisionError, atomic_promote_output, choose_output_path
from utils import runtime_paths


@pytest.mark.parametrize(
    "title",
    [
        "CON", "con.mp4", "PRN", "AUX", "NUL", "COM1", "com9.txt", "LPT1", "lpt9.mp4",
        "title.", "title ", 'illegal<>:"/\\|?*', "../../escape", "..\\..\\escape",
        "C:\\Users\\other\\escape", ".", "..", "", "   ", "città 日本語 🎬",
    ],
)
def test_remote_title_is_one_safe_windows_component(title: str) -> None:
    name = safe_output_name(title)
    assert name
    assert name == name.rstrip(". ")
    assert not any(character in name for character in '<>:"/\\|?*')
    assert name not in {".", ".."}
    assert Path(name).name == name
    assert name.split(".", 1)[0].upper() not in {
        "CON", "PRN", "AUX", "NUL", *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }


def test_sanitizing_preserves_unicode_and_limits_windows_length() -> None:
    assert safe_output_name("città 日本語 🎬") == "città 日本語 🎬"
    name = safe_output_name("🎬" * 300)
    assert len(name.encode("utf-16-le")) <= 320


def _job(manager: PipelineManager, vod_id: str, destination: Path, *, title: str = "video") -> PipelineJob:
    job, created = manager.discover_vod(
        vod_id=vod_id,
        vod_url=f"https://example.invalid/{vod_id}",
        source_title=title,
        metadata={"delivery": {"output_dir": str(destination)}},
    )
    assert created
    return job


def test_existing_output_and_companion_are_never_replaced_implicitly(tmp_path: Path) -> None:
    output = tmp_path / "video.mp4"
    output.write_bytes(b"existing-user-video")
    companion = tmp_path / "video_2.mp4.automation.json"
    companion.write_text("existing-user-manifest", encoding="utf-8")
    assert choose_output_path(output) == tmp_path / "video_3.mp4"
    assert output.read_bytes() == b"existing-user-video"
    assert companion.read_text(encoding="utf-8") == "existing-user-manifest"
    assert choose_output_path(output, force=True) == output


def test_output_reservation_is_durable_and_separates_jobs_in_another_directory(tmp_path: Path) -> None:
    state = tmp_path / "state drive" / "jobs.json"
    output = tmp_path / "other drive" / "città 🎬"
    manager = PipelineManager(PipelineStore(state))
    first = _job(manager, "first", output)
    second = _job(manager, "second", output)
    reserved = reserve_output(manager, first)
    assert reserved == output / "video.mp4"
    assert reserve_output(manager, second) == output / "video_2.mp4"
    restarted = PipelineManager(PipelineStore(state))
    snapshot = restarted.get(first.id)
    assert snapshot.metadata["delivery"]["resolved_output_path"] == str(reserved)
    assert reserve_output(restarted, snapshot) == reserved


def test_remote_metadata_cannot_escape_output_folder(tmp_path: Path) -> None:
    manager = PipelineManager(PipelineStore(tmp_path / "jobs.json"))
    output = tmp_path / "exports"
    job = _job(manager, "traversal", output, title="../../C:\\escape.mp4")
    assert requested_output(job).parent == output
    assert reserve_output(manager, job).parent == output


def test_force_cannot_overwrite_media_or_project(tmp_path: Path) -> None:
    manager = PipelineManager(PipelineStore(tmp_path / "jobs.json"))
    source = tmp_path / "my input è 🎬.mkv"
    source.write_bytes(b"input")
    job = _job(manager, "source-protected", tmp_path)
    job.local_source_path = str(source)
    manager.store.upsert(job)
    job = manager.update_metadata(job.id, {"delivery": {"output_path": str(source), "force": True}})
    with pytest.raises(OutputSourceCollisionError):
        reserve_output(manager, job)
    assert source.read_bytes() == b"input"


def test_force_can_replace_a_reserved_destination_but_execution_stays_exclusive(tmp_path: Path) -> None:
    from automation.locking import FileLockBusyError
    from automation.paths import output_execution_lock

    manager = PipelineManager(PipelineStore(tmp_path / "jobs.json"))
    first = _job(manager, "first", tmp_path / "exports")
    reserved = reserve_output(manager, first)
    second = _job(manager, "second", tmp_path / "exports")
    second = manager.update_metadata(second.id, {"delivery": {"output_path": str(reserved), "force": True}})
    assert reserve_output(manager, second) == reserved
    with output_execution_lock(reserved).hold():
        with pytest.raises(FileLockBusyError):
            with output_execution_lock(reserved).hold(blocking=False, reentrant=False):
                raise AssertionError("A second executor acquired an active destination")


def test_promotion_uses_output_directory_independent_of_job_directory(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    destination = tmp_path / "other volume output è 🎬"
    destination.mkdir()
    temporary = destination / ".video.uuid.partial.mp4"
    final = destination / "video.mp4"
    temporary.write_bytes(b"validated-video")
    atomic_promote_output(temporary, final, overwrite=False)
    assert final.read_bytes() == b"validated-video"
    assert not temporary.exists()
    assert not tuple(state.iterdir())


def test_writable_paths_ignore_cwd_and_pyinstaller_resource_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "readonly bundle"
    unrelated = tmp_path / "different cwd"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    overrides = {
        "AUTO_CUTTER_CONFIG_DIR": tmp_path / "config è 🎬",
        "AUTO_CUTTER_DATA_DIR": tmp_path / "state è 🎬",
        "AUTO_CUTTER_CREDENTIALS_DIR": tmp_path / "credentials è 🎬",
        "AUTO_CUTTER_CACHE_DIR": tmp_path / "cache è 🎬",
        "AUTO_CUTTER_OUTPUT_DIR": tmp_path / "exports è 🎬",
    }
    for variable, path in overrides.items():
        monkeypatch.setenv(variable, str(path))
    paths = [
        runtime_paths.config_root(), runtime_paths.data_root(), runtime_paths.credentials_root(),
        runtime_paths.cache_root(), runtime_paths.exports_root(), runtime_paths.logs_root(),
    ]
    assert runtime_paths.project_root() == bundle
    assert runtime_paths.resource_path("icon.ico") == bundle / "icon.ico"
    assert paths[:5] == list(overrides.values())
    assert paths[5] == overrides["AUTO_CUTTER_CONFIG_DIR"] / "session_logs"
    assert all(bundle not in path.parents and unrelated not in path.parents for path in paths)
