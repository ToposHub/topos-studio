"""Clip effect settings open the supplied panel and use project edit signals."""
from __future__ import annotations

from importlib import import_module

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QSignalSpy
from PyQt6.QtWidgets import QDialogButtonBox, QScrollArea, QScrollBar
from pytestqt.qtbot import QtBot

from src.features.edit.core.effect_registry import AUDIO_MIXER_EFFECT_TYPES
from src.features.edit.core.timeline_v2 import ReverbParams, _ensure_effect_param_registry
from src.features.edit.ui.clip_audio_effect_dialog_adapter import ClipAudioEffectDialogManager
from src.features.mixer.effects.catalog import get_effect
from src.features.mixer.effects.production_dialog import PRODUCTION_BINDINGS, ProductionEffectDialog
from src.shared.ui.widgets.chevron_combo import ChevronComboBox

pytestmark = pytest.mark.edit_core


def test_all_registered_clip_effects_have_supplied_panels() -> None:
    assert set(PRODUCTION_BINDINGS) - {"spatial_panner"} == AUDIO_MIXER_EFFECT_TYPES
    for effect_type in AUDIO_MIXER_EFFECT_TYPES:
        panel_id, fields = PRODUCTION_BINDINGS[effect_type]
        descriptor = get_effect(panel_id)
        assert getattr(import_module(descriptor.panel_module), descriptor.panel_class)
        assert fields


@pytest.mark.parametrize("effect_type", sorted(PRODUCTION_BINDINGS))
def test_production_effect_window_shows_native_panel_at_its_minimum(
    qtbot: QtBot, effect_type: str,
) -> None:
    registry = _ensure_effect_param_registry()
    params = registry[effect_type].default().to_dict() if effect_type in registry else {}
    dialog = ProductionEffectDialog(effect_type, "很长的录音素材名称 · Long Dialogue Take 01", params)
    dialog.show()
    try:
        dialog.resize(1, 1)
        assert dialog.panel.parentWidget() is dialog
        assert not dialog.findChildren(QScrollArea)
        assert all(not bar.isVisible() for bar in dialog.findChildren(QScrollBar))
        assert dialog.rect().contains(dialog.panel.geometry())
        assert dialog.panel.width() >= dialog.panel.layout().minimumSize().width()
        assert dialog.panel.height() >= dialog.panel.layout().minimumSize().height()
        assert dialog.findChild(QDialogButtonBox).parentWidget().objectName() == "EffectWindowToolbar"
        assert dialog.current_params() == params
    finally:
        dialog.close()


def test_open_shows_the_supplied_reverb_panel(qtbot: QtBot) -> None:
    from src.features.mixer.effects.panels.reverb import StudioAcousticReverbWidget

    manager = ClipAudioEffectDialogManager()
    dialog = manager.open_dialog("reverb", "clip_1", "fx_1", ReverbParams.default().to_dict(), "My Clip")

    assert dialog is not None
    assert isinstance(dialog.panel, StudioAcousticReverbWidget)
    assert dialog.isVisible()
    assert "My Clip" in dialog.windowTitle()
    dialog.close()


def test_open_reuses_same_clip_effect_window(qtbot: QtBot) -> None:
    manager = ClipAudioEffectDialogManager()
    params = ReverbParams.default().to_dict()
    first = manager.open_dialog("reverb", "clip_2", "fx_2", params)
    second = manager.open_dialog("reverb", "clip_2", "fx_2", params)

    assert first is not None
    assert first is second
    manager.close_all()


@pytest.mark.parametrize("effect_type", ["blur", "spatial_panner"])
def test_non_clip_effect_returns_none(qtbot: QtBot, effect_type: str) -> None:
    manager = ClipAudioEffectDialogManager()
    assert manager.open_dialog(effect_type, "clip_3", "fx_3", {}) is None


def test_reverb_applies_two_knob_edits_together(qtbot: QtBot) -> None:
    manager = ClipAudioEffectDialogManager()
    commits = QSignalSpy(manager.clip_effect_params_committed)
    params = ReverbParams(dry_wet=50.0).to_dict()
    dialog = manager.open_dialog("reverb", "clip_4", "fx_4", params)

    assert dialog is not None
    dialog.panel._controls["dry_wet"].setValue(80.0, emit=True)
    dialog.panel._controls["reverb_time"].setValue(1500.0, emit=True)
    assert len(commits) == 0

    buttons = dialog.findChild(QDialogButtonBox)
    apply = buttons.button(QDialogButtonBox.StandardButton.Apply)
    assert apply.isEnabled()
    qtbot.mouseClick(apply, Qt.MouseButton.LeftButton)

    expected = {**params, "dry_wet": 80.0, "reverb_time": 1500.0}
    assert list(commits) == [["clip_4", "fx_4", expected, None]]
    assert dialog.isVisible()
    assert not apply.isEnabled()
    dialog.close()


def test_equalizer_filter_selector_uses_shared_dropdown_and_updates_filter(qtbot: QtBot) -> None:
    manager = ClipAudioEffectDialogManager()
    params = _ensure_effect_param_registry()["strip_eq"].default().to_dict()
    dialog = manager.open_dialog("strip_eq", "clip_eq", "fx_eq", params)
    assert dialog is not None
    try:
        selector = dialog.panel.band_cards[0].shape_selector
        assert isinstance(selector, ChevronComboBox)
        assert selector.currentData() == params["bands"][0]["filter_type"]
        with qtbot.waitSignal(manager.clip_effect_params_committed) as commit:
            selector.setCurrentIndex(selector.findData("Bell"))
            dialog.apply()
        assert commit.args[2]["bands"][0]["filter_type"] == "Bell"
    finally:
        dialog.close()


def test_noop_ok_does_not_commit(qtbot: QtBot) -> None:
    manager = ClipAudioEffectDialogManager()
    commits = QSignalSpy(manager.clip_effect_params_committed)
    dialog = manager.open_dialog("reverb", "clip_5", "fx_5", ReverbParams.default().to_dict())

    assert dialog is not None
    buttons = dialog.findChild(QDialogButtonBox)
    qtbot.mouseClick(buttons.button(QDialogButtonBox.StandardButton.Ok), Qt.MouseButton.LeftButton)

    assert len(commits) == 0


def test_partial_reverb_parameters_render_with_defaults(qtbot: QtBot) -> None:
    manager = ClipAudioEffectDialogManager()
    dialog = manager.open_dialog("reverb", "clip_6", "fx_6", {"dry_wet": 60.0})

    assert dialog is not None
    assert dialog.panel._controls["dry_wet"].value() == 60.0
    assert dialog.panel._controls["reverb_time"].value() == ReverbParams.default().reverb_time
    dialog.close()
