"""The newly exposed production controls reach persisted DSP parameters."""
from __future__ import annotations

import pytest

from src.features.edit.core.timeline_v2 import _ensure_effect_param_registry
from src.features.mixer.effects.production_dialog import ProductionEffectDialog


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


def test_dynamic_eq_model_switch_and_sidechain_uses_stable_id(qtbot) -> None:
    defaults = _ensure_effect_param_registry()['dynamic_eq'].default().to_dict()
    dialog = ProductionEffectDialog('dynamic_eq', 'Fixture', defaults,
                                    sidechain_sources=(('track-3', 'Dialogue'),))
    qtbot.addWidget(dialog)
    dialog.model_selector.setCurrentIndex(1)
    assert len(dialog.current_params()['bands']) == 6
    dialog.panel.sc_source_combo.setCurrentIndex(dialog.panel.sc_source_combo.findData('track-3'))
    assert dialog.current_params()['sidechain_source_id'] == 'track-3'


def test_saturation_explicit_model_switch_preserves_legacy_before_apply(qtbot) -> None:
    defaults = _ensure_effect_param_registry()['saturation'].default().to_dict()
    dialog = ProductionEffectDialog('saturation', 'Fixture', defaults)
    qtbot.addWidget(dialog)
    assert dialog.current_params() == defaults
    dialog.model_selector.setCurrentIndex(1)
    new = dialog.current_params()
    assert new['model_profile'] == 'multiband'
    assert len(new['mb_bands']) == 3
    dialog.set_current_params(defaults)
    assert dialog.current_params() == defaults
