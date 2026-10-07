from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from automation.twitch_auth import TwitchTokenStore
from utils.credential_files import migrate_credential_file


def test_encrypted_migration_moves_opaque_bytes_into_destination_directory(tmp_path: Path) -> None:
    legacy = tmp_path / "old" / "twitch-token.bin"
    target = tmp_path / "new drive" / "credenziali è 🎬" / "twitch-token.bin"
    legacy.parent.mkdir()
    legacy.write_bytes(b"opaque-encrypted-credential")

    assert migrate_credential_file(legacy, target)

    assert target.read_bytes() == b"opaque-encrypted-credential"
    assert not legacy.exists()
    assert not list(target.parent.glob("*.tmp"))


def test_migration_never_overwrites_a_newer_credential(tmp_path: Path) -> None:
    legacy = tmp_path / "old.bin"
    target = tmp_path / "new.bin"
    legacy.write_bytes(b"old")
    target.write_bytes(b"new")

    assert not migrate_credential_file(legacy, target)
    assert target.read_bytes() == b"new"
    assert legacy.read_bytes() == b"old"


def test_concurrent_credential_publication_wins_without_deleting_legacy(tmp_path: Path) -> None:
    legacy = tmp_path / "old.bin"
    target = tmp_path / "new.bin"
    legacy.write_bytes(b"old")

    def publish_newer(source: Path, destination: Path) -> None:
        assert source.parent == destination.parent
        destination.write_bytes(b"new")
        raise FileExistsError("Another login published first")

    with patch("utils.credential_files.os.link", side_effect=publish_newer):
        assert not migrate_credential_file(legacy, target)

    assert target.read_bytes() == b"new"
    assert legacy.exists()
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.skipif(os.name != "nt", reason="Windows rename refuses replacement")
def test_migration_handles_destination_without_hardlinks(tmp_path: Path) -> None:
    legacy = tmp_path / "old.bin"
    target = tmp_path / "new" / "token.bin"
    legacy.write_bytes(b"old")

    with patch("utils.credential_files.os.link", side_effect=OSError("hardlinks unavailable")):
        assert migrate_credential_file(legacy, target)

    assert target.read_bytes() == b"old"
    assert not legacy.exists()


def test_twitch_logout_removes_conflicting_legacy_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    legacy = tmp_path / "pipeline" / "twitch-token.bin"
    target = tmp_path / "credentials" / "twitch-token.bin"
    legacy.parent.mkdir()
    target.parent.mkdir()
    legacy.write_bytes(b"legacy")
    target.write_bytes(b"current")
    monkeypatch.delenv("AUTO_CUTTER_TWITCH_TOKEN_FILE", raising=False)

    with patch("automation.twitch_auth.default_twitch_token_path", return_value=target), patch(
        "automation.twitch_auth.default_pipeline_store_path", return_value=legacy.with_name("jobs.json")
    ):
        store = TwitchTokenStore()
        store.delete()
        restarted = TwitchTokenStore()

    assert not target.exists()
    assert not legacy.exists()
    assert restarted.load() is None
