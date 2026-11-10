"""Suite-wide guards."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_card_art_network(monkeypatch):
    """Official card images are on by default; tests never download them (test_card_art passes its own fetch)."""
    def offline(url: str) -> bytes:
        raise OSError(f"network disabled in tests: {url}")

    monkeypatch.setattr("analysis.packaging.card_art._fetch", offline)
