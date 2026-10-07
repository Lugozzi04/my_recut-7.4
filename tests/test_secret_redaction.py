from __future__ import annotations

import json
import io
from pathlib import Path
import sys
import threading
import traceback
from types import SimpleNamespace
import zipfile

import pytest

from utils.crash_handler import install_global_exception_handler, write_crash_log
from utils.diagnostics import create_support_bundle
from utils.redaction import redact_secrets


@pytest.mark.parametrize(
    "message,secret",
    [
        ('{"access_token": "fake-access-123"}', "fake-access-123"),
        ("{'refresh_token': 'fake-refresh-123'}", "fake-refresh-123"),
        ("device_code=fake-device-123", "fake-device-123"),
        ("user_code=fake-user-123", "fake-user-123"),
        ('client_secret="fake-client-123"', "fake-client-123"),
        ("Authorization: Bearer fake-bearer-123", "fake-bearer-123"),
        ("{'Authorization': 'Bearer fake-header-123'}", "fake-header-123"),
        ("Bearer fake-standalone-123", "fake-standalone-123"),
        ("Cookie: session=fake-cookie-123; other=fake-other-123", "fake-cookie-123"),
        ("{'cookies': 'session=fake-cookie-dict-123'}", "fake-cookie-dict-123"),
        ("Set-Cookie: login=fake-set-cookie-123; HttpOnly", "fake-set-cookie-123"),
        ("http://127.0.0.1:4567/?code=fake-oauth-code-123&state=fake-state", "fake-oauth-code-123"),
        ("https://vod.twitch.tv/file.m3u8?token=fake-signed-token-123&sig=fake-signature-123", "fake-signed-token-123"),
        ("https://upload.googleapis.com/upload/youtube/v3/videos?upload_id=fake-session-123", "fake-session-123"),
        ('{"id_token": "fake-id-token-123"}', "fake-id-token-123"),
    ],
)
def test_redacts_credentials_in_rendered_messages(message: str, secret: str) -> None:
    redacted = redact_secrets(message)
    assert secret not in redacted
    assert "[REDACTED]" in redacted
    assert redact_secrets(redacted) == redacted


def test_redacts_all_signed_twitch_and_oauth_parameters() -> None:
    result = redact_secrets(
        "https://example.invalid/?token=fake-token&sig=fake-sig"
        "&code=fake-code&state=fake-state&quality=best"
    )
    for secret in ("fake-token", "fake-sig", "fake-code", "fake-state"):
        assert secret not in result
    assert "quality=best" in result


def test_keeps_nonsecret_messages_and_json_readable() -> None:
    original = '{"stage": "analysis", "progress": 42, "access_token": "fake-json-token"}'
    result = json.loads(redact_secrets(original))
    assert result == {"stage": "analysis", "progress": 42, "access_token": "[REDACTED]"}
    assert redact_secrets("Export failed: codec not available") == "Export failed: codec not available"


def test_download_failure_never_persists_unquoted_credentials(tmp_path: Path) -> None:
    from automation.downloader import PipelineDownloadError, PipelineDownloadService
    from automation.manager import PipelineManager
    from automation.store import PipelineStore

    manager = PipelineManager(PipelineStore(tmp_path / "jobs.json"))
    job, _ = manager.discover_vod(vod_id="1", vod_url="https://www.twitch.tv/videos/1")
    manager.request_range(job.id)
    manager.select_range(job.id, 0, 10)

    class FailedDownloader:
        def download(self, request, **kwargs):
            raise RuntimeError("Authorization: Bearer fake-download-bearer refresh_token=fake-download-refresh")

    with pytest.raises(PipelineDownloadError):
        PipelineDownloadService(manager, downloader_factory=FailedDownloader).execute(job.id)
    content = manager.store.path.read_text(encoding="utf-8")
    assert "fake-download-bearer" not in content
    assert "fake-download-refresh" not in content
    assert "[REDACTED]" in content


@pytest.mark.parametrize("levels", [1, 2, 3])
def test_redacts_credentials_inside_json_escaped_messages(levels: int) -> None:
    original = 'response={"access_token":"fake-nested-access","refresh_token":"fake-nested-refresh"}'
    for _ in range(levels):
        original = json.dumps({"error": original})
    result = redact_secrets(original)
    assert "fake-nested-access" not in result
    assert "fake-nested-refresh" not in result
    assert "[REDACTED]" in result
    assert redact_secrets(result) == result
    decoded = result
    for _ in range(levels):
        decoded = json.loads(decoded)["error"]
    assert '"access_token":"[REDACTED]"' in decoded


def test_json_log_prefix_with_escaped_credentials_is_redacted() -> None:
    record = json.dumps({"error": 'response={"client_secret":"fake-log-nested-secret"}'})
    result = redact_secrets("2026-10-04 failure " + record)
    assert "fake-log-nested-secret" not in result
    json.loads(result.split("failure ", 1)[1])


def test_support_bundle_redacts_metadata_and_old_logs(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    crashes = tmp_path / "crashes"
    crashes.mkdir()
    (logs / "session.log").write_text("refresh_token=fake-log-refresh", encoding="utf-8")
    (crashes / "crash.log").write_text("Authorization: Bearer fake-crash-bearer", encoding="utf-8")
    bundle = create_support_bundle(
        tmp_path / "support.zip",
        session_logs_dir=logs,
        crash_logs_dir=crashes,
        consistency_errors=["failed https://localhost/?code=fake-error-code"],
    )
    with zipfile.ZipFile(bundle) as archive:
        content = "\n".join(archive.read(name).decode("utf-8") for name in archive.namelist())
        metadata = json.loads(archive.read("diagnostics.json"))
    for secret in ("fake-log-refresh", "fake-crash-bearer", "fake-error-code"):
        assert secret not in content
    assert "[REDACTED]" in metadata["project_consistency_errors"][0]


def test_crash_logs_redact_before_writing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTO_CUTTER_CRASH_DIR", str(tmp_path))
    error = RuntimeError("access_token=fake-crash-access upload_id=fake-crash-session")
    output = write_crash_log(type(error), error, None)
    content = output.read_text(encoding="utf-8")
    assert "fake-crash-access" not in content
    assert "fake-crash-session" not in content
    assert "RuntimeError" in content


def test_bundle_redacts_before_truncating_a_log_boundary(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    secret = "fake-boundary-secret-123"
    (logs / "session.log").write_text(
        f"access_token={secret}" + "\n" + "x" * 1_999_999,
        encoding="utf-8",
    )
    bundle = create_support_bundle(tmp_path / "support.zip", session_logs_dir=logs)
    with zipfile.ZipFile(bundle) as archive:
        content = archive.read("logs/session-1.log").decode("utf-8")
    assert "boundary-secret" not in content


def test_default_crash_directory_uses_writable_data_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AUTO_CUTTER_CRASH_DIR", raising=False)
    monkeypatch.setenv("AUTO_CUTTER_DATA_DIR", str(tmp_path))
    error = RuntimeError("example")
    assert write_crash_log(type(error), error, None).parent == tmp_path / "crash_logs"


def test_previous_exception_hook_reports_redacted_chained_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = io.StringIO()
    monkeypatch.setenv("AUTO_CUTTER_CRASH_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "stderr", destination)
    monkeypatch.setattr(sys, "excepthook", sys.__excepthook__)
    monkeypatch.setattr(threading, "excepthook", threading.__excepthook__)
    install_global_exception_handler()
    try:
        try:
            raise RuntimeError("upload_id=fake-hook-session")
        except RuntimeError as cause:
            raise ValueError("Authorization: Bearer fake-hook-bearer") from cause
    except ValueError as error:
        sys.excepthook(type(error), error, error.__traceback__)
    result = destination.getvalue()
    assert "fake-hook-session" not in result
    assert "fake-hook-bearer" not in result
    assert "RuntimeError" in result and "ValueError" in result
    assert "direct cause" in result


def test_thread_hook_redacts_stderr_and_preserves_original_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = io.StringIO()
    received = []
    error = RuntimeError("refresh_token=fake-thread-refresh")
    args = SimpleNamespace(exc_type=type(error), exc_value=error, exc_traceback=None)

    def original_hook(value) -> None:
        received.append(value)
        traceback.print_exception(value.exc_type, value.exc_value, value.exc_traceback)

    monkeypatch.setenv("AUTO_CUTTER_CRASH_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "stderr", destination)
    monkeypatch.setattr(sys, "excepthook", sys.__excepthook__)
    monkeypatch.setattr(threading, "excepthook", original_hook)
    install_global_exception_handler()
    threading.excepthook(args)
    assert received == [args]
    assert "fake-thread-refresh" not in destination.getvalue()
    assert "RuntimeError" in destination.getvalue()


def test_hook_error_still_emits_redacted_output_and_restores_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = io.StringIO()

    def failing_hook(*args) -> None:
        print("client_secret=fake-hook-client", file=sys.stderr)
        raise RuntimeError("fake reporter failed")

    monkeypatch.setenv("AUTO_CUTTER_CRASH_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "stderr", destination)
    monkeypatch.setattr(sys, "excepthook", failing_hook)
    monkeypatch.setattr(threading, "excepthook", threading.__excepthook__)
    install_global_exception_handler()
    error = ValueError("example")
    with pytest.raises(RuntimeError, match="fake reporter failed"):
        sys.excepthook(type(error), error, None)
    assert sys.stderr is destination
    assert "fake-hook-client" not in destination.getvalue()
    assert "[REDACTED]" in destination.getvalue()
