"""Suite-wide guards."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_user_state(monkeypatch, tmp_path_factory):
    """Never read or write the user's real settings, data or caches.

    A MainWindow closed without AUTO_CUTTER_CONFIG_DIR saves its QSettings into the real registry
    (it reset the user's filter preset and window state); a cached fake result also made tests
    order-dependent. Tests that need specific dirs still override these with monkeypatch.
    """
    base = tmp_path_factory.mktemp("user_state")
    for name in ("CONFIG", "DATA", "CREDENTIALS", "OUTPUT", "CACHE", "CHUNK_CACHE", "CRASH", "DOWNLOAD"):
        monkeypatch.setenv(f"AUTO_CUTTER_{name}_DIR", str(base / name.lower()))


@pytest.fixture(autouse=True)
def _no_card_art_network(monkeypatch):
    """Official card images are on by default; tests never download them (test_card_art passes its own fetch)."""
    def offline(url: str) -> bytes:
        raise OSError(f"network disabled in tests: {url}")

    monkeypatch.setattr("analysis.packaging.card_art._fetch", offline)
