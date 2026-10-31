"""Qualifier palette: model units, channel controls and range drag regressions."""

import pytest
from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtTest import QTest

from src.features.color.ui.qualifier_panel import QualifierPanel
from src.shared.ui.widgets.qualifier_range_bar import QualifierRangeBar


@pytest.fixture
def panel(qapp):
    widget = QualifierPanel()
    yield widget
    widget.close()
    widget.deleteLater()


def test_percentage_edit_submits_normalized_value_once(panel):
    submitted = []
    panel.params_changed.connect(lambda params: submitted.append(dict(params)))
    panel.hue_center_ctrl.spin.setValue(25.5)
    assert panel.hue_center_ctrl.value() == pytest.approx(0.255)
    assert panel.params['hue_center'] == pytest.approx(0.255)
    assert panel.hue_bar.center == pytest.approx(0.255)
    assert len(submitted) == 1


def test_model_sync_keeps_percentage_controls_silent(panel):
    submitted = []
    panel.params_changed.connect(lambda params: submitted.append(dict(params)))
    panel.set_params({'hue_center': 0.12345, 'sat_low': 0.23456, 'enabled': False})
    assert panel.hue_center_ctrl.spin.value() == pytest.approx(12.345)
    assert panel.sat_low_ctrl.value() == pytest.approx(0.23456)
    assert len(submitted) == 1
    assert submitted[0]['enabled'] is False


@pytest.mark.parametrize('channel', ['hue', 'sat', 'lum'])
def test_channel_toggle_submits_and_disables_its_controls(panel, channel):
    submitted = []
    panel.params_changed.connect(lambda params: submitted.append(dict(params)))
    QTest.mouseClick(panel._channel_checks[channel], Qt.MouseButton.LeftButton)
    assert panel.params[f'{channel}_enabled'] is False
    assert panel.params['enabled'] is True
    assert not panel._channel_bars[channel].isEnabled()
    assert all(not row.isEnabled() for row in panel._channel_controls[channel])
    assert len(submitted) == 1


def test_reset_channel_preserves_other_ranges_and_matte(panel):
    panel.set_params({'hue_center': .75, 'sat_low': .25, 'sat_enabled': False,
                      'clean_black': 8, 'denoise': 30})
    submitted = []
    panel.params_changed.connect(lambda params: submitted.append(dict(params)))
    panel._channel_resets['sat'].click()
    assert panel.params['sat_enabled'] is True
    assert panel.params['sat_low'] == 0.0
    assert panel.params['hue_center'] == .75
    assert panel.params['clean_black'] == 8
    assert panel.params['denoise'] == 30
    assert panel.sat_bar.isEnabled()
    assert len(submitted) == 1


def test_restore_disabled_channels_does_not_activate_qualifier(panel):
    submitted = []
    panel.params_changed.connect(lambda params: submitted.append(dict(params)))
    panel.set_params({'hue_enabled': False, 'sat_enabled': False, 'lum_enabled': False,
                      'enabled': False, 'invert': True})
    assert not panel.params['enabled']
    assert not any(check.isChecked() for check in panel._channel_checks.values())
    assert len(submitted) == 1
    submitted.clear()
    panel.reset_all()
    assert all(check.isChecked() for check in panel._channel_checks.values())
    assert all(bar.isEnabled() for bar in panel._channel_bars.values())
    assert panel.params['enabled'] is False
    assert len(submitted) == 1


@pytest.mark.parametrize('key', ['blur', 'clean_black', 'clean_white', 'denoise'])
def test_matte_user_edit_submits_once_and_survives_model_sync(panel, key):
    submitted = []
    panel.params_changed.connect(lambda params: submitted.append(dict(params)))
    row = panel._matte_controls[key]
    row.spin.setValue(8.0)
    assert panel.params[key] == 8.0
    assert len(submitted) == 1
    panel.set_params({key: 3.0, 'enabled': False})
    assert row.value() == 3.0
    assert not panel.params['enabled']


def test_palette_keeps_finesse_narrow_and_sliders_visible(panel, qapp):
    panel.resize(980, 424)
    panel.show()
    qapp.processEvents()
    assert panel.refine_group.width() < panel.ranges_widget.width() / 3
    assert panel.refine_group.x() >= panel.ranges_widget.geometry().right()
    assert all(row.slider.isVisible() for row in panel._matte_controls.values())
    assert not panel.hue_center_ctrl.slider.isVisible()
    assert panel.controls_scroll.horizontalScrollBar().maximum() == 0
    assert panel.controls_scroll.verticalScrollBar().maximum() == 0


@pytest.mark.parametrize('center', [.2, .5, .8])
@pytest.mark.parametrize('side', ['start', 'end'])
def test_full_hue_endpoint_click_does_not_collapse_range(qapp, center, side):
    bar = QualifierRangeBar('hue')
    bar.resize(500, 28)
    bar.set_hue_params(center, 1.0, .1)
    bar.show()
    qapp.processEvents()
    rect = bar._bar_rect()
    point = QPoint(round(rect.left() if side == 'start' else rect.right()), 10)
    QTest.mouseClick(bar, Qt.MouseButton.LeftButton, pos=point)
    assert bar.hue_width == 1.0
    assert bar.center == center
    # The default full range really has two separately draggable endpoints.
    assert bar._get_handle_at(QPointF(point)) == side
    bar.close()
    bar.deleteLater()


def test_saturation_range_drag_preserves_click_offset_and_width(qapp):
    bar = QualifierRangeBar('sat')
    bar.resize(500, 28)
    bar.set_range(.2, .8)
    bar.show()
    qapp.processEvents()
    rect = bar._bar_rect()
    point = QPoint(round(rect.left() + rect.width() * .4), 10)
    QTest.mousePress(bar, Qt.MouseButton.LeftButton, pos=point)
    assert bar.low == pytest.approx(.2)
    QTest.mouseMove(bar, point + QPoint(49, 0))
    QTest.mouseRelease(bar, Qt.MouseButton.LeftButton, pos=point + QPoint(49, 0))
    assert bar.high - bar.low == pytest.approx(.6)
    assert bar.low == pytest.approx(.3, abs=.003)
    bar.close()
    bar.deleteLater()


def test_hue_softness_handle_and_overlay_use_shader_softness_units(qapp):
    bar = QualifierRangeBar('hue')
    bar.resize(500, 28)
    bar.set_hue_params(.5, .2, .1)
    # Keyer falloff extends by soft, rather than soft/2, beyond the hard edge.
    assert bar._handle_positions()['soft_start'] == pytest.approx(.05)
    assert bar._handle_positions()['soft_end'] == pytest.approx(.45)
    bar.close()
    bar.deleteLater()


def test_hue_display_rotation_preserves_picked_red_model_coordinate(panel, qapp):
    panel.process_picked_color(0.0, .7, .5, reset_ranges=True)
    assert panel.params['hue_center'] == .5
    panel.resize(980, 424)
    panel.show()
    qapp.processEvents()
    bar = panel.hue_bar
    # Red appears at one quarter of the violet-to-violet spectrum, but is
    # still stored at 0.5, matching existing projects and the GPU keyer.
    center = bar._handle_positions()['center']
    assert center == .25
    rect = bar._bar_rect()
    point = QPointF(rect.left() + center * rect.width(), rect.center().y())
    assert bar._position_value(point) == pytest.approx(.5)


@pytest.mark.parametrize('center,soft,handle', [(1.0, .1, 'center'),
                                               (.5, .8, 'soft_start'),
                                               (.5, .8, 'soft_end')])
def test_hue_handle_click_preserves_large_softness_and_center_endpoint(qapp, center, soft, handle):
    bar = QualifierRangeBar('hue')
    bar.resize(1006, 28)
    bar.set_hue_params(center, .2, soft)
    bar.show()
    qapp.processEvents()
    rect = bar._bar_rect()
    position = bar._handle_positions()[handle]
    point = QPoint(round(rect.left() + position * rect.width()),
                   round(rect.bottom()) if handle.startswith('soft') else 10)
    QTest.mouseClick(bar, Qt.MouseButton.LeftButton, pos=point)
    assert bar.center == center
    assert bar.soft == soft
    bar.close()
    bar.deleteLater()


def test_narrowing_full_hue_keeps_visible_edge_under_pointer(qapp):
    bar = QualifierRangeBar('hue')
    bar.resize(1006, 28)
    bar.set_hue_params(.2, 1.0, .1)
    bar.show()
    qapp.processEvents()
    rect = bar._bar_rect()
    point = QPoint(round(rect.left()), 10)
    QTest.mousePress(bar, Qt.MouseButton.LeftButton, pos=point)
    QTest.mouseMove(bar, point + QPoint(40, 0))
    QTest.mouseRelease(bar, Qt.MouseButton.LeftButton, pos=point + QPoint(40, 0))
    assert bar.hue_width == pytest.approx(.92)
    assert bar._handle_positions()['start'] == pytest.approx(.04)
    assert bar._handle_positions()['end'] == pytest.approx(.96)
    bar.close()
    bar.deleteLater()
