"""Unified curve navigation retains the existing HSL editing contract."""
import pytest
from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QMouseEvent
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


@pytest.mark.parametrize("mode", MODE_ORDER)
def test_calibrated_hsl_pointer_commits_release_position(qapp, mode: str) -> None:
    panel = CurvesPanel(CurvesWidget(), use_measured_curves=lambda: True)
    panel.set_layer_state({})
    panel.set_curve_mode(mode)
    canvas = panel.hsl_panel.hsl_curves_widget
    canvas.resize(711, 370)
    started, changed, ended = (QSignalSpy(signal) for signal in
                               (canvas.interaction_started, canvas.changed, canvas.interaction_ended))

    def event(kind: QEvent.Type, x: float, y: float) -> QMouseEvent:
        point = QPointF(*canvas.to_widget(x, y))
        return QMouseEvent(kind, point, point, Qt.MouseButton.LeftButton,
                           Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)

    press = event(QEvent.Type.MouseButtonPress, 85.0, 95.0)
    release = event(QEvent.Type.MouseButtonRelease, 112.0, 105.0)
    canvas.mousePressEvent(press)
    canvas.mouseReleaseEvent(release)
    points = canvas.get_curves_data()[mode]
    assert len(points) == (1 if mode.startswith("Hue") else 3)
    assert points[len(points) // 2] == pytest.approx((112.0, 105.0))
    assert (len(started), len(changed), len(ended)) == (1, 1, 1)
    assert changed[0][0].shape == (6, 1024)

    canvas.mousePressEvent(event(QEvent.Type.MouseButtonPress, 112.0, 105.0))
    canvas.mouseReleaseEvent(release)
    assert (len(started), len(changed), len(ended)) == (1, 1, 1)


def test_calibrated_presets_and_hue_readback_wrap(qapp) -> None:
    panel = CurvesPanel(CurvesWidget(), use_measured_curves=lambda: True)
    panel.set_layer_state({})
    panel.set_curve_mode("HueSat")
    canvas = panel.hsl_panel.hsl_curves_widget
    assert canvas.selected_values() == (256.0, 1.0)
    canvas.add_preset_anchor(0.0)
    points = canvas.get_curves_data()["HueSat"]
    assert [p[0] for p in points] == pytest.approx([0.0, 42.5, 212.5])
    assert canvas.selected_values() == (256.0, 1.0)
    canvas.set_selected_value("x", 255.0)
    assert canvas.selected_values() == pytest.approx((255.0, 1.0))
    assert canvas.get_curves_data()["HueSat"][-1][0] == pytest.approx(359.0 * 255.0 / 360.0)
    canvas.set_selected_value("x", 90.0)
    assert canvas.selected_values() == pytest.approx((255.0, 1.0))
    canvas.set_selected_value("x", 281.0)
    assert canvas.selected_values() == (281.0, 1.0)
    assert canvas.get_curves_data()["HueSat"][0][0] == pytest.approx(25.0 * 255.0 / 360.0)
    panel.set_curve_mode("LumSat")
    canvas.add_preset_anchor(1.0)
    assert canvas.selected_values() == (0.75, 1.0)


def test_calibrated_hue_seam_is_a_single_editable_point(qapp) -> None:
    panel = CurvesPanel(CurvesWidget(), use_measured_curves=lambda: True)
    panel.set_layer_state({})
    panel.set_curve_mode("HueSat")
    canvas = panel.hsl_panel.hsl_curves_widget
    canvas.set_curves_data({"HueSat": [(0.0, 110.0), None, (110.0, 130.0), (255.0, 100.0)]})
    assert canvas.get_curves_data()["HueSat"] == [(0.0, 105.0), (110.0, 130.0)]
    canvas.add_anchor(1.0)
    assert len(canvas.get_curves_data()["HueSat"]) == 2
    assert canvas.selected_values() == pytest.approx((256.0, 150.0 / 127.5))
