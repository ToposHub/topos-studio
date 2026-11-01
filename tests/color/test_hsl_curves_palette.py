"""Unified curve navigation retains the existing HSL editing contract."""
import pytest
from PyQt6.QtCore import QPointF
from PyQt6.QtTest import QSignalSpy

from src.features.color.ui.curves_panel import CurvesPanel, CURVE_PALETTE_ORDER
from src.features.color.ui.hsl_curves_panel import HSLCurvesPanel
from src.features.color.core.workspace_presets import ColorWorkspacePreset, sanitize_preset
from src.shared.ui.widgets.curves_widget import CurvesWidget
from src.shared.ui.widgets.hsl_curves_widget import MODE_ORDER


def test_mode_strip_reuses_original_hsl_panel_without_rendering(qapp):
    original = HSLCurvesPanel()
    canvas = CurvesWidget()
    panel = CurvesPanel(canvas, hsl_panel=original)
    custom_changed = QSignalSpy(canvas.changed)
    hsl_changed = QSignalSpy(original.hsl_curves_widget.changed)
    original.hsl_curves_widget.curves['HueSat'].insert(1, QPointF(128, 100))
    saved = original.get_curves_data()
    for mode in CURVE_PALETTE_ORDER[1:]:
        panel.mode_buttons[mode].click()
        assert panel.curve_mode == mode
        assert panel.pages.currentWidget() is original
        assert original.hsl_curves_widget.current_mode == mode
        assert original.get_curves_data() == saved
    panel.set_curve_mode('Custom')
    assert panel.pages.currentWidget() is panel.custom_page
    assert len(custom_changed) == len(hsl_changed) == 0
    # Palette order must never change the shader/serialization LUT row order.
    assert MODE_ORDER == ['LumSat', 'HueHue', 'HueSat', 'HueLum', 'SatSat', 'SatLum']


@pytest.mark.parametrize(('mode', 'presets', 'input_name', 'output_name'), [
    ('HueHue', 6, 'Input Hue', 'Hue Rotate'),
    ('HueSat', 6, 'Input Hue', 'Saturation'),
    ('HueLum', 6, 'Input Hue', 'Lum'),
    ('LumSat', 2, 'Input Lum', 'Saturation'),
    ('SatSat', 0, 'Input Sat', 'Saturation'),
    ('SatLum', 0, 'Input Sat', 'Lum'),
])
def test_hsl_full_width_footer_matches_mode(qapp, mode, presets, input_name, output_name):
    panel = CurvesPanel(CurvesWidget())
    panel.set_curve_mode(mode)
    footer = panel.hsl_panel
    assert footer._preset_row.count() == presets + 1  # Original eyedropper.
    assert footer.input_label.text() == input_name
    assert footer.output_label.text() == output_name
    assert footer.hsl_curves_widget.btn_container.isHidden()
    assert panel.reset_curves_btn.isHidden()
    assert not panel.reset_hsl_btn.isHidden()
    assert footer.output_value.defaultValue() == (0.0 if mode == 'HueHue' else 1.0)


def test_hsl_numeric_fields_update_the_original_lut_and_gesture(qapp):
    panel = CurvesPanel(CurvesWidget())
    panel.set_curve_mode('HueSat')
    hsl = panel.hsl_panel.hsl_curves_widget
    hsl.add_anchor(0.5)
    changed, started, ended = QSignalSpy(hsl.changed), QSignalSpy(hsl.interaction_started), QSignalSpy(hsl.interaction_ended)
    field = panel.hsl_panel.output_value
    field.interaction_started.emit()
    field.setValue(1.4)
    field.interaction_ended.emit()
    assert hsl.selected_values()[1] == pytest.approx(1.4)
    assert len(changed) == len(started) == len(ended) == 1
    assert changed[0][0].shape == (6, 256)
    assert changed[0][0][MODE_ORDER.index('HueSat'), 128] == pytest.approx(1.4, abs=0.001)


def test_switching_editor_deactivates_hidden_pickers(qapp):
    panel = CurvesPanel(CurvesWidget())
    panel.picker_btn.setChecked(True)
    panel.set_curve_mode('HueHue')
    assert not panel.picker_btn.isChecked()
    panel.hsl_panel.hsl_curves_widget.picker_btn.setChecked(True)
    panel.set_curve_mode('Custom')
    assert not panel.hsl_panel.is_picker_active()


def test_reentering_same_hsl_mode_drops_aliased_custom_texture(qapp):
    panel = CurvesPanel(CurvesWidget())
    panel.set_curve_mode('HueHue')
    hsl = panel.hsl_panel.hsl_curves_widget
    hsl._has_histogram = True
    panel.set_curve_mode('Custom')
    assert not hsl._has_histogram
    hsl._has_histogram = True
    panel.set_curve_mode('HueHue')
    assert not hsl._has_histogram


def test_legacy_hsl_workspace_opens_curves_instead_of_color_mapping():
    preset = ColorWorkspacePreset('old.hsl', 'Old HSL', frozenset({'color.hsl_curves'}))
    resolved = sanitize_preset(preset, ['color.curves', 'color.mapping'])
    assert 'color.curves' in resolved
    assert 'color.mapping' not in resolved
