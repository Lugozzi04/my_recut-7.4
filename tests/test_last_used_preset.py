"""The cut preset proposed for a new video is the last one chosen or used for an export."""
from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def owner(presets):
    from ui.main_window import MainWindow
    fake = SimpleNamespace(presets=presets, _default_preset_name=None)
    fake._remember_last_preset = lambda name: MainWindow._remember_last_preset(fake, name)
    fake.pick = lambda: MainWindow._pick_default_preset(fake)
    return fake


def test_last_chosen_preset_beats_the_balanced_default(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_CUTTER_CONFIG_DIR", str(tmp_path / "config"))
    presets = {"Balanced (Default)": {}, "HS": {}, "Natural Speech": {}}
    window = owner(presets)
    window.pick()
    assert window._default_preset_name == "Balanced (Default)"  # nothing remembered yet
    window._remember_last_preset("HS")
    window._remember_last_preset("Manual")  # tweaked values never replace the remembered preset
    window.pick()
    assert window._default_preset_name == "HS"
    fresh = owner(presets)
    fresh.pick()  # survives a restart: stored in the app settings
    assert fresh._default_preset_name == "HS"
    deleted = owner({"Balanced (Default)": {}})
    deleted.pick()  # a deleted preset falls back to the default
    assert deleted._default_preset_name == "Balanced (Default)"
