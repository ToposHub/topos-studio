"""Registered rack effects open and reconcile their actual production panels."""
from __future__ import annotations

import pytest
from PyQt6 import sip
from PyQt6.QtWidgets import QMenu
from pytestqt.qtbot import QtBot

from src.features.mixer.effects.production_dialog import ProductionEffectDialog
from src.features.mixer.effects.bindings_core import set_path
from src.shared.ui.audio_mixer.track_mixer_widget import TrackMixerWidget
from tests.mixer.test_project_workspace import ProjectHost, project_host as project_host
from tests.mixer.test_real_media_effects import CONTROLS


_INSERTS = sorted(TrackMixerWidget._INSERT_EFFECT_TYPES - {"dynamics"})


@pytest.mark.parametrize("kind", [*_INSERTS, "noise_gate"])
def test_rack_row_opens_production_panel_and_removal_closes_it(qtbot: QtBot, kind: str) -> None:
    strip = TrackMixerWidget("audio", "Real media QA", "A1")
    qtbot.addWidget(strip)
    strip._effect_add(kind)
    instance = strip._effect_chain.effects[0]
    row = strip._effect_list.item(0)
    strip._on_effect_item_clicked(row)
    dialogs = [
        dialog
        for attribute in strip._EFFECT_DIALOG_ATTRS
        for dialog in getattr(strip, attribute, {}).values()
    ]
    assert len(dialogs) == 1
    dialog = dialogs[0]
    assert isinstance(dialog, ProductionEffectDialog)
    assert dialog.isVisible()
    assert dialog.fx_effect_type == kind
    assert dialog._instance_id == instance.instance_id
    assert dialog.current_params() == instance.params

    # Re-entering the same rack row must reuse this instance's window.
    strip._on_effect_item_clicked(row)
    assert sum(len(getattr(strip, name, {})) for name in strip._EFFECT_DIALOG_ATTRS) == 1
    strip._effect_bypass(instance)
    assert dialog.panel.get_state()["enabled"] is False
    strip._effect_remove(strip._effect_chain.effects[0])
    qtbot.waitUntil(lambda: sip.isdeleted(dialog))
    assert strip._effect_chain is None
    assert all(not getattr(strip, name, {}) for name in strip._EFFECT_DIALOG_ATTRS)


@pytest.mark.parametrize("kind", ["exciter", "subharmonic"])
def test_new_tone_processors_have_consistent_catalog_and_rack_names(qtbot: QtBot, kind: str) -> None:
    strip = TrackMixerWidget("audio", "Track", "A1")
    qtbot.addWidget(strip)
    menu = QMenu(strip)
    strip._populate_add_effect_menu(menu)
    action = next(
        action
        for category in menu.actions()
        for action in category.menu().actions()
        if action.data() == kind
    )
    action.trigger()
    row = strip._effect_list.item(0)
    assert row.text() == action.text()
    assert row.text() != kind
    assert row.toolTip().startswith(action.text())


def test_master_does_not_offer_panner_edits_without_a_project_or_audio_path(
    project_host: ProjectHost,
) -> None:
    host = project_host
    master = host.audio_mixer_panel._master_mixer
    before = host.timeline_panel.timeline_view.get_timeline()
    emitted: list[object] = []
    master.panner_committed.connect(lambda *args: emitted.append(args))
    assert not master._panner_expand_btn.isEnabled()
    assert not master._balance_fader.isEnabled()
    assert not master._balance_spin.isEnabled()
    menu = QMenu(master)
    master._populate_add_effect_menu(menu)
    action = next(action for category in menu.actions() for action in category.menu().actions()
                  if action.data() == "spatial_panner")
    assert not action.isEnabled()
    master._add_spatial_panner()
    master._open_panner_dialog()
    assert master._panner_dialog is None
    assert not emitted
    assert host.timeline_panel.timeline_view.get_timeline() is before


def _open_panel(strip: TrackMixerWidget) -> ProductionEffectDialog:
    strip._on_effect_item_clicked(strip._effect_list.item(0))
    return next(
        dialog
        for name in strip._EFFECT_DIALOG_ATTRS
        for dialog in getattr(strip, name, {}).values()
    )


def _edit_panel(dialog: ProductionEffectDialog, kind: str) -> None:
    state = dialog.panel.get_state()
    for path, value in CONTROLS[kind].items():
        set_path(state, path, value)
    dialog.panel.set_state(state)
    dialog.panel.stateChanged.emit(dialog.panel.get_state())


@pytest.mark.parametrize("kind", [*_INSERTS, "noise_gate"])
def test_panel_cancel_apply_and_history_use_project_commands(
    project_host: ProjectHost, qtbot: QtBot, kind: str,
) -> None:
    host = project_host
    host.show_mixer()
    view = host.timeline_panel.timeline_view
    strip = host.audio_mixer_panel._track_mixers["audio-track"]
    strip._effect_add(kind)
    original = view.get_timeline().audio_tracks[0].effect_chain.effects[0]
    dialog = _open_panel(strip)
    _edit_panel(dialog, kind)
    qtbot.waitUntil(lambda: bool(strip._effect_previews))
    assert view.get_timeline().audio_tracks[0].effect_chain.effects[0] == original
    dialog.reject()
    assert not strip._effect_previews
    assert view.get_timeline().audio_tracks[0].effect_chain.effects[0] == original

    dialog = _open_panel(strip)
    _edit_panel(dialog, kind)
    expected = dialog.current_params()
    assert expected != original.params
    dialog.apply()
    applied = view.get_timeline().audio_tracks[0].effect_chain.effects[0]
    assert applied.params == expected
    assert applied.instance_id == original.instance_id
    view.undo()
    assert view.get_timeline().audio_tracks[0].effect_chain.effects[0] == original
    assert dialog.current_params() == original.params
    view.redo()
    assert view.get_timeline().audio_tracks[0].effect_chain.effects[0] == applied
    assert dialog.current_params() == expected
    strip._effect_bypass(applied)
    bypassed = view.get_timeline().audio_tracks[0].effect_chain.effects[0]
    assert not (bypassed.enabled and bypassed.params.get("enabled", True))
    view.undo()
    restored = view.get_timeline().audio_tracks[0].effect_chain.effects[0]
    assert restored.enabled and restored.params.get("enabled", True)
    dialog.reject()
