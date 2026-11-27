"""The newly exposed production controls reach persisted DSP parameters."""
from __future__ import annotations

from pathlib import Path

import pytest
from PyQt6 import sip
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtWidgets import QInputDialog, QMenu, QPushButton
from pytestqt.qtbot import QtBot

from src.features.edit.core.timeline_v2 import _ensure_effect_param_registry
from src.features.edit.core import effect_preset
from src.features.mixer.effects.bindings_core import get_path, set_path
from src.features.mixer.effects.production_dialog import ProductionEffectDialog
from src.shared.ui.audio_mixer.track_mixer_widget import TrackMixerWidget


@pytest.mark.parametrize('kind,values', [
    ('de_esser', {'filter_shape': 'High Shelf', 'lookahead': 5., 'output_gain': -3.}),
    ('bus_comp', {'threshold': -25., 'ratio': 4., 'attack': 5., 'release': 90., 'makeup': 2.}),
    ('tremolo_auto_pan', {'waveform': 'Square', 'sync': True, 'bpm': 120., 'sync_div': '1/4', 'shape': 25.}),
])
def test_native_controls_roundtrip_and_revert(qtbot, kind: str, values: dict) -> None:
    defaults = _ensure_effect_param_registry()[kind].default().to_dict()
    dialog = ProductionEffectDialog(kind, 'Fixture', defaults)
    qtbot.addWidget(dialog)
    assert dialog.current_params() == defaults
    state = dialog.panel.get_state()
    state.update(values)
    dialog.panel.set_state(state)
    changed = dialog.current_params()
    assert changed != defaults
    restored = _ensure_effect_param_registry()[kind].from_dict(changed).to_dict()
    if kind == 'de_esser':
        assert restored['detector_shape'] == 'high_shelf'
        assert restored['lookahead_ms'] == 5
    elif kind == 'bus_comp':
        assert restored['threshold_db'] == -25
    else:
        assert restored['waveform'] == 'square'
        assert restored['duty_cycle_pct'] == 25
        assert restored['sync'] is True
    dialog.set_current_params(defaults)
    assert dialog.current_params() == defaults


def test_dynamic_eq_graph_edits_saved_bands_and_uses_stable_sidechain_id(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch,
) -> None:
    params = {
        "bands": [
            {"enabled": True, "frequency_hz": 600.0, "max_cut_db": 7.125},
            {"enabled": False, "frequency_hz": 2250.0, "note": "preserve"},
        ],
    }
    dialog = ProductionEffectDialog(
        "dynamic_eq", "Fixture", params,
        sidechain_sources=(("track-1", "Dialogue"), ("track-3", "Dialogue")),
    )
    qtbot.addWidget(dialog)
    dialog.show()
    assert dialog.current_params() == params
    assert dialog.panel.band_cards[0].isVisible()

    graph = dialog.panel.eq_graph
    plot = graph._plot_rect()
    node = graph._node_pos(dialog.panel.get_state()["bands"][0], plot).toPoint()
    qtbot.mouseDClick(graph, Qt.MouseButton.LeftButton, pos=node)
    assert dialog.current_params()["bands"][0] == params["bands"][0] | {"enabled": False}

    def enable_second_band(menu: QMenu, _pos: QPoint) -> None:
        assert len(menu.actions()) == 3
        menu.actions()[1].trigger()

    monkeypatch.setattr(QMenu, "exec", enable_second_band)
    qtbot.mouseClick(graph, Qt.MouseButton.RightButton, pos=plot.center().toPoint() + QPoint(0, 30))
    assert dialog.panel.band_cards[1].isVisible()
    assert dialog.current_params()["bands"][1] == params["bands"][1] | {"enabled": True}

    combo = dialog.panel.sc_source_combo
    combo.setCurrentIndex(combo.findData("track-3"))
    dialog.set_sidechain_sources((("track-3", "Renamed"), ("track-4", "Ambience")))
    assert combo.currentData() == "track-3"
    assert combo.currentText() == "Renamed"
    with qtbot.waitSignal(dialog.committed) as commit:
        dialog.apply()
    assert commit.args[0]["sidechain_source_id"] == "track-3"
    assert len(commit.args[0]["bands"]) == 2


def test_dynamic_eq_adds_band_from_graph_and_keeps_original_parameters(qtbot: QtBot) -> None:
    params = {"frequency": 4300.0, "q": 2.3, "gain_db": -3.5, "max_cut_db": 7.125}
    dialog = ProductionEffectDialog("dynamic_eq", "Fixture", params)
    qtbot.addWidget(dialog)
    dialog.show()
    graph = dialog.panel.eq_graph
    plot = graph._plot_rect()
    qtbot.mouseDClick(graph, Qt.MouseButton.LeftButton,
                    pos=plot.center().toPoint() + QPoint(-30, 30))
    staged = dialog.current_params()
    first, second = staged["bands"]
    assert first["frequency_hz"] == 4300.0
    assert first["q"] == 2.3
    assert first["static_gain_db"] == -3.5
    assert first["max_cut_db"] == 7.125
    assert first["legacy_automation"]
    assert second["enabled"]
    assert dialog.panel.band_cards[1].isVisible()
    with qtbot.waitSignal(dialog.committed) as commit:
        dialog.apply()
    assert commit.args[0] == staged
    dialog.set_current_params(params)
    assert dialog.current_params() == params
    assert dialog.panel.band_cards[3].isVisible()


@pytest.mark.parametrize("kind,key,advanced", [
    ("dynamic_eq", "sidechain_source_id", "dialogue"),
    ("de_esser", "lookahead_ms", 5.0),
])
def test_cleanup_presets_reopen_with_the_saved_processing_model(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    kind: str, key: str, advanced: str | float,
) -> None:
    monkeypatch.setattr(effect_preset, "_store_path", lambda: str(tmp_path / "fx.json"))
    monkeypatch.setattr(QInputDialog, "getText", lambda *_args, **_kwargs: ("My cleanup", True))
    sources = (("dialogue", "Dialogue"),)
    dialog = ProductionEffectDialog(kind, "Original", {key: advanced}, sidechain_sources=sources)
    qtbot.addWidget(dialog)
    save = next(button for button in dialog.panel.findChildren(QPushButton, "ToolBtn")
                if button.text() == "+")
    save.click()
    assert dialog.current_params() == {key: advanced}
    dialog.close()

    reopened = ProductionEffectDialog(kind, "New track", {}, sidechain_sources=sources)
    qtbot.addWidget(reopened)
    combo = reopened.panel.preset_combo
    combo.setCurrentIndex(combo.findText("My cleanup"))
    assert reopened.current_params()[key] == advanced


@pytest.mark.parametrize("kind", ["strip_eq", "dynamic_eq", "de_esser"])
def test_effect_graph_uses_its_own_spectrum_and_clears_when_stopped(qtbot: QtBot, kind: str) -> None:
    strip = TrackMixerWidget("track", "Track")
    qtbot.addWidget(strip)
    strip.show()
    strip._effect_add(kind)
    instance = strip._effect_chain.effects[0]
    strip._open_insert_effect(instance)
    _, iid, dialog = next(strip._iter_insert_dialogs())
    graph = dialog.panel.graph if kind == "de_esser" else dialog.panel.eq_graph
    try:
        strip.update_track_telemetry({
            "spectrum": {"freqs": [100.0, 200.0], "db": [-6.0, -12.0]},
            "taps": {iid: {
                "in_db": -12.0, "out_db": -18.0,
                "spectrum": {"freqs": [1500.0, 3000.0], "input_db": [-18.0, -24.0],
                             "output_db": [-20.0, -30.0]},
                "analysis": {"detector_dbfs": -27.0, "gain_reduction_db": 4.0,
                             "bands": [{"dynamic_gain_db": -1.0}, {"dynamic_gain_db": -3.0}]},
            }},
        })
        assert graph._spec_freqs == [1500.0, 3000.0]
        assert graph._spec_peak == [-20.0, -30.0]
        if kind == "dynamic_eq":
            assert graph._band_dyn_offsets == [-1.0, -3.0, 0.0, 0.0, 0.0, 0.0]
        if kind == "de_esser":
            assert graph._spec_in == [-18.0, -24.0]
            assert graph._current_sc_db == -27.0
            assert graph._current_gr_db == -4.0
        strip.update_track_telemetry({"silent": True})
        assert graph._spec_freqs == []
        assert graph._spec_peak == []
    finally:
        strip.close_all_dialogs()
    qtbot.waitUntil(lambda: sip.isdeleted(dialog))


@pytest.mark.parametrize(("kind", "params", "path", "value", "stored_path", "expected"), [
    ("dynamic_eq", {"frequency": 2400.0}, "bands.3.freq", 850.0, "frequency", 850.0),
    ("saturation", {"drive": 11.0}, "drive", 30.0, "drive", 30.0),
    ("saturation", {"model_profile": "multiband", "mb_bands": [{"enabled": True}]},
     "bands.0.drive", 2.0, "mb_bands.0.drive_db", 6.0),
    ("transient_designer", {"attack_db": 3.0}, "transient.attack", 50.0, "attack_db", 12.0),
    ("transient_designer", {"attack_pct": 25.0}, "transient.attack", 50.0, "attack_pct", 50.0),
])
def test_native_controls_edit_the_saved_processing_model(
    qtbot: QtBot, kind: str, params: dict, path: str, value: float, stored_path: str, expected: float,
) -> None:
    dialog = ProductionEffectDialog(kind, "Fixture", params)
    qtbot.addWidget(dialog)
    assert dialog.current_params() == params
    state = dialog.panel.get_state()
    set_path(state, path, value)
    dialog.panel.set_state(state)
    result = dialog.current_params()
    assert get_path(result, stored_path) == expected
    root = stored_path.split(".", 1)[0]
    assert result == params | {root: result[root]}
