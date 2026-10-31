"""Timeline graphics backend preference persistence."""

from __future__ import annotations

import pytest

from src.app.ui.preferences_dialog import PreferencesDialog
from src.shared.config.config_manager import ConfigManager


class _ConfigStub:
    def __init__(self, initial=None):
        self.DEFAULT_CONFIG = ConfigManager.DEFAULT_CONFIG
        self.config = {**self.DEFAULT_CONFIG, **(initial or {})}

    def get(self, key):
        return self.config.get(key)

    def set(self, key, value, auto_save=True):
        self.config[key] = value

    def save_config(self):
        pass


@pytest.fixture
def make_dialog(qapp):
    dialogs = []

    def create(initial=None):
        dialog = PreferencesDialog(_ConfigStub(initial), module_pages=[])
        dialogs.append(dialog)
        return dialog

    yield create
    for dialog in dialogs:
        dialog.deleteLater()


def test_graphics_backend_preference_loads_and_persists_without_dropping_other_dimensions(
    make_dialog,
):
    initial_backend = {
        "graphics": "metal",
        "compute": "metal_compute",
        "decode": "videotoolbox",
        "interop": "cvmetal",
    }
    dialog = make_dialog({"backend": initial_backend})
    combo = dialog._widgets["timeline_graphics_backend"]
    assert combo.currentData() == "metal"

    combo.setCurrentIndex(combo.findData("opengl"))
    dialog._apply_settings()
    assert dialog._config_manager.get("backend") == {
        **initial_backend,
        "graphics": "opengl",
    }


def test_graphics_backend_defaults_to_auto(make_dialog):
    dialog = make_dialog()
    assert dialog._widgets["timeline_graphics_backend"].currentData() == "auto"
