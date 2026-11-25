"""Base Grade dock navigation and persisted parameter editing."""

from __future__ import annotations

import math

import numpy as np
import pytest
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtWidgets import QLabel, QTabBar

from src.features.color.core.base_grade import BASE_GRADE_DEFAULTS
from src.features.color.ui.color_wheel import GradingPanel, KnurledJogWheel
from src.shared.ui.style_sheet import DARK_THEME


def _show_base_grade(panel: GradingPanel, qtbot) -> None:
    base = panel.base_grade_panel
    qtbot.addWidget(base)
    base.setStyleSheet(DARK_THEME)
    base.resize(496, 333)
    base.show()


def _arc_point(wheel, side: int, dy: int = 0) -> QPoint:
    center, radius = wheel.canvas._geometry()
    return center.toPoint() + QPoint(round(radius + wheel.canvas._ARC_GAP) * side, dy)


def test_base_grade_is_separate_from_primary_modes_and_has_header_icons(qtbot) -> None:
    panel = GradingPanel()
    qtbot.addWidget(panel)
    _show_base_grade(panel, qtbot)
    base = panel.base_grade_panel
    assert base.width() == 496
    assert panel.mode_stack.indexOf(base) == -1
    assert not panel.findChildren(QTabBar)
    assert not base.findChildren(QTabBar)
    assert not base.findChildren(KnurledJogWheel)
    dim_light, dark_bright = (base.zone_buttons.button(index) for index in range(2))
    for button in (dim_light, dark_bright):
        assert button.parentWidget().objectName() == "colorToolHeader"
        assert button.mapTo(base, QPoint()).y() < base._wheel_row.geometry().top()
        assert button.accessibleName() == button.toolTip()
    assert dim_light.isChecked()
    for wheel in (base._wheels["dim"], base._wheels["balance"], base._wheels["light"]):
        assert wheel.isVisible()
        assert base.rect().contains(wheel.canvas.mapTo(base, wheel.canvas.rect().bottomRight()))
        fields = [wheel._control(key) for key in ("saturation", "a", "b", "exposure")]
        assert all(field.mapTo(wheel, QPoint()).y() > wheel.canvas.geometry().bottom()
                   for field in fields)
        positions = [field.mapTo(wheel, QPoint()) for field in fields]
        assert positions == sorted(positions, key=lambda point: (point.y(), point.x()))
        if wheel is not base._wheels["balance"]:
            shaping = [wheel._control(key) for key in ("pivot", "falloff")]
            labels = shaping[0].parentWidget().findChildren(
                QLabel, options=Qt.FindChildOption.FindDirectChildrenOnly,
            )
            assert len(labels) == 2
            for label, field in zip(labels, shaping):
                assert label.geometry().right() < field.geometry().left()
                assert label.geometry().center().y() == field.geometry().center().y()
                assert field.mapTo(wheel, QPoint()).y() > positions[-1].y()
    assert not base._wheels["dark"].isVisible()
    dark_bright.click()
    assert dark_bright.isChecked() and not dim_light.isChecked()
    assert base._wheels["dark"].isVisible()
    assert base._wheels["bright"].isVisible()
    assert base._wheels["balance"].isVisible()

    panel._mode_buttons["log"].click()
    dim_light.click()
    assert panel.mode_stack.currentWidget() is panel._log_page


def test_response_curve_hides_at_narrow_width_and_returns_when_wider(qtbot) -> None:
    panel = GradingPanel()
    qtbot.addWidget(panel)
    _show_base_grade(panel, qtbot)
    base = panel.base_grade_panel
    assert base.tone_curve.isHidden()
    base.zone_buttons.button(1).click()
    base.resize(760, base.height())
    qtbot.waitUntil(base.tone_curve.isVisible)
    assert base.tone_curve.width() >= base.tone_curve.minimumWidth()
    assert base._wheels["dark"].isVisible() and base._wheels["bright"].isVisible()
    base.resize(416, base.height())
    qtbot.waitUntil(base.tone_curve.isHidden)
    assert base.width() == 416
    assert base._wheels["balance"].isVisible()
    assert base.zone_buttons.button(1).isChecked()
    balance = base._wheels["balance"]
    ev, sat = (balance._control(key) for key in ("exposure", "saturation"))
    qtbot.waitUntil(lambda: ev.mapTo(balance, QPoint()).y() > sat.mapTo(balance, QPoint()).y())
    for zone in ("dark", "balance", "bright"):
        wheel = base._wheels[zone]
        for control in ("exposure", "a", "b", "saturation"):
            field = wheel._control(control)
            assert base.rect().contains(field.mapTo(base, field.rect().bottomRight()))


@pytest.mark.parametrize(("zone", "side", "key", "dy"), (
    ("balance", -1, "base_grade_saturation", -30),
    ("dim", -1, "base_grade_dim_saturation", 30),
    ("bright", 1, "base_grade_bright_exposure", -30),
))
def test_side_arcs_write_only_the_paired_numeric_value(zone, side, key, dy, qtbot) -> None:
    panel = GradingPanel()
    qtbot.addWidget(panel)
    _show_base_grade(panel, qtbot)
    base = panel.base_grade_panel
    if zone == "bright":
        base.zone_buttons.button(1).click()
    wheel = base._wheels[zone]
    canvas = wheel.canvas
    start, end = _arc_point(wheel, side), _arc_point(wheel, side, dy)
    events: list[tuple[str, dict[str, float]]] = []
    panel.interaction_started.connect(lambda: events.append(("start", {})))
    panel.grading_extended_changed.connect(lambda patch: events.append(("change", patch)))
    panel.interaction_ended.connect(lambda: events.append(("end", {})))

    qtbot.mousePress(canvas, Qt.MouseButton.LeftButton, pos=start)
    assert base.get_params()[key] == BASE_GRADE_DEFAULTS[key]
    qtbot.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=end)
    value = wheel.controls[key].value()
    assert events == [("start", {}), ("change", {key: value}), ("end", {})]
    assert (value - BASE_GRADE_DEFAULTS[key]) * dy < 0
    assert panel.get_current_params()[key] == value

    qtbot.mouseDClick(canvas, Qt.MouseButton.LeftButton, pos=start)
    assert wheel.controls[key].value() == BASE_GRADE_DEFAULTS[key]
    assert events[-3:] == [("start", {}), ("change", {key: BASE_GRADE_DEFAULTS[key]}), ("end", {})]


def test_arc_thumb_positions_follow_hydration_and_numeric_edits(qtbot) -> None:
    panel = GradingPanel()
    qtbot.addWidget(panel)
    _show_base_grade(panel, qtbot)
    base = panel.base_grade_panel
    balance = base._wheels["balance"]
    emitted: list[dict[str, float]] = []
    panel.grading_extended_changed.connect(emitted.append)
    panel.load_extended_params({
        "base_grade_saturation": 2.0,
        "base_grade_balance_exposure": -4.0,
        "base_grade_dim_saturation": 0.5,
    })
    assert not emitted
    assert balance._arc_position("exposure") == -0.5
    assert balance._arc_position("saturation") == 0.5
    assert base._wheels["dim"]._arc_position("saturation") == -0.5
    base.zone_buttons.button(1).click()
    field = balance.controls["base_grade_saturation"]
    qtbot.mouseDClick(field, Qt.MouseButton.LeftButton, pos=field.rect().center())
    qtbot.keyClicks(field._edit, "3.0")
    qtbot.keyClick(field._edit, Qt.Key.Key_Return)
    assert balance._arc_position("saturation") == 1.0
    assert emitted == [{"base_grade_saturation": 3.0}]
    assert set(base.get_params()) == set(BASE_GRADE_DEFAULTS)


@pytest.mark.parametrize(("key", "side"), (("saturation", -1), ("exposure", 1)))
def test_side_arc_accepts_drag_next_to_the_larger_thumb(key, side, qtbot) -> None:
    panel = GradingPanel()
    qtbot.addWidget(panel)
    _show_base_grade(panel, qtbot)
    wheel = panel.base_grade_panel._wheels["balance"]
    start = _arc_point(wheel, side) - QPoint(7 * side, 0)
    qtbot.mousePress(wheel.canvas, Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseRelease(wheel.canvas, Qt.MouseButton.LeftButton, pos=start - QPoint(0, 12))
    assert wheel._control(key).value() > wheel._control(key).defaultValue()


def test_exposure_arc_shift_adjusts_only_the_next_movement(qtbot) -> None:
    panel = GradingPanel()
    qtbot.addWidget(panel)
    _show_base_grade(panel, qtbot)
    wheel = panel.base_grade_panel._wheels["balance"]
    canvas = wheel.canvas
    start, end = _arc_point(wheel, 1), _arc_point(wheel, 1, -30)
    field = wheel._control("exposure")

    qtbot.mousePress(canvas, Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=end)
    normal_value = field.value()
    assert normal_value > 0

    qtbot.mouseDClick(canvas, Qt.MouseButton.LeftButton, pos=start)
    qtbot.mousePress(canvas, Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseRelease(canvas, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier, pos=end)
    assert field.value() == pytest.approx(normal_value * 0.2, abs=0.01)


@pytest.mark.parametrize("key, side", (("exposure", 1), ("saturation", -1)))
def test_horizontal_motion_towards_disk_does_not_change_side_arc(key, side, qtbot) -> None:
    panel = GradingPanel()
    qtbot.addWidget(panel)
    _show_base_grade(panel, qtbot)
    wheel = panel.base_grade_panel._wheels["balance"]
    canvas = wheel.canvas
    start, end = _arc_point(wheel, side), _arc_point(wheel, -side)
    field = wheel._control(key)
    events: list[dict[str, float]] = []
    panel.grading_extended_changed.connect(events.append)
    qtbot.mousePress(canvas, Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(canvas, QPoint((start.x() + end.x()) // 2, start.y()))
    qtbot.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=end)
    assert field.value() == field.defaultValue()
    assert not events


def test_base_grade_tint_drag_commits_both_axes_before_ending(qtbot) -> None:
    panel = GradingPanel()
    qtbot.addWidget(panel)
    _show_base_grade(panel, qtbot)
    wheel = panel.base_grade_panel._wheels["balance"]
    events = []
    panel.interaction_started.connect(lambda: events.append(("start", {})))
    panel.grading_extended_changed.connect(lambda patch: events.append(("change", patch)))
    panel.interaction_ended.connect(lambda: events.append(("end", {})))

    center = wheel.canvas.rect().center()
    end = center + QPoint(18, 10)
    qtbot.mousePress(wheel.canvas, Qt.MouseButton.LeftButton, pos=center)
    qtbot.mouseMove(wheel.canvas, end)
    qtbot.mouseRelease(wheel.canvas, Qt.MouseButton.LeftButton, pos=end)
    assert [event for event, _ in events] == ["start", "change", "end"]
    changed = events[1][1]
    assert set(changed) == {"base_grade_balance_a", "base_grade_balance_b"}
    assert all(value != 0.0 for value in changed.values())
    final = panel.get_current_params()
    assert {key: final[key] for key in changed} == changed


def test_numeric_zone_edit_is_independent_of_other_zone_pair(qtbot) -> None:
    panel = GradingPanel()
    qtbot.addWidget(panel)
    _show_base_grade(panel, qtbot)
    base = panel.base_grade_panel
    base.zone_buttons.button(1).click()
    field = base._wheels["bright"].controls["base_grade_bright_falloff"]
    events: list[dict[str, float]] = []
    panel.grading_extended_changed.connect(events.append)
    qtbot.mouseDClick(field, Qt.MouseButton.LeftButton, pos=field.rect().center())
    assert field._edit.isVisible()
    qtbot.keyClicks(field._edit, "0.32")
    qtbot.keyClick(field._edit, Qt.Key.Key_Return)
    base.zone_buttons.button(0).click()
    assert events == [{"base_grade_bright_falloff": 0.32}]
    params = panel.get_current_params()
    assert params["base_grade_bright_falloff"] == 0.32
    assert params["base_grade_light_falloff"] == BASE_GRADE_DEFAULTS["base_grade_light_falloff"]


def test_base_grade_hydration_and_local_reset(qtbot) -> None:
    panel = GradingPanel()
    qtbot.addWidget(panel)
    _show_base_grade(panel, qtbot)
    emitted: list[dict[str, float]] = []
    panel.grading_extended_changed.connect(emitted.append)

    panel.set_extended_params({
        "pivot": 0.31,
        "base_grade_balance_exposure": 1.0,
        "base_grade_bright_falloff": 0.34,
    })
    assert emitted == []
    params = panel.get_current_params()
    assert params["base_grade_balance_exposure"] == 1.0
    assert params["base_grade_bright_falloff"] == 0.34
    display = panel.base_grade_panel.tone_curve._output_ev
    assert display[len(display) // 2] == pytest.approx(1.0)

    panel.base_grade_panel.reset_btn.click()
    params = panel.get_current_params()
    assert params["pivot"] == pytest.approx(0.31)
    assert all(params[key] == value for key, value in BASE_GRADE_DEFAULTS.items())
    assert set(emitted.pop()) == {"base_grade_balance_exposure", "base_grade_bright_falloff"}

    panel.set_extended_params({"base_grade_dark_a": 0.1})
    panel.load_extended_params({})
    np.testing.assert_allclose(panel.base_grade_panel.tone_curve._output_ev,
                               np.linspace(-8.0, 8.0, 129), atol=1e-6)
    assert panel.get_current_params()["base_grade_dark_a"] == 0.0
    assert emitted == []


def test_curve_pivots_project_onto_input_axis_after_contrast_balance_and_contrast_pivot(qtbot) -> None:
    panel = GradingPanel()
    qtbot.addWidget(panel)
    _show_base_grade(panel, qtbot)
    base = panel.base_grade_panel
    curve = base.tone_curve
    base.resize(810, base.height())
    qtbot.waitUntil(curve.isVisible)
    old_dim_x = curve._pivot_point("dim", curve._plot_rect()).x()
    emitted: list[dict[str, float]] = []
    panel.grading_extended_changed.connect(emitted.append)

    # The global stage is y = 2x + 0.5, so y=1 and y=-1 project to x=0.25 and x=-0.75.
    panel.set_extended_params({
        "base_grade_contrast": 2.0,
        "base_grade_contrast_pivot": 0.25,
        "base_grade_balance_exposure": 0.5,
    })
    assert curve._global_ev[len(curve._global_ev) // 2] == pytest.approx(0.5)
    assert curve._input_for_pivot("dim") == pytest.approx(0.25, abs=1e-5)
    assert curve._input_for_pivot("light") == pytest.approx(-0.75, abs=1e-5)
    assert curve._pivot_point("dim", curve._plot_rect()).x() < old_dim_x
    assert base._wheels["dim"]._control("pivot").value() == 1.0
    assert base._wheels["light"]._control("pivot").value() == -1.0
    assert not emitted


@pytest.mark.parametrize("zone", ("dim", "light"))
def test_overlapping_curve_pivots_can_be_dragged_independently_under_contrast(zone, qtbot) -> None:
    panel = GradingPanel()
    qtbot.addWidget(panel)
    _show_base_grade(panel, qtbot)
    base = panel.base_grade_panel
    curve = base.tone_curve
    base.resize(810, base.height())
    qtbot.waitUntil(curve.isVisible)
    panel.load_extended_params({
        "base_grade_contrast": 2.0,
        "base_grade_dim_pivot": 0.0,
        "base_grade_light_pivot": 0.0,
        f"base_grade_{zone}_exposure": 1.0,
    })
    plot = curve._plot_rect()
    assert curve._pivot_point("dim", plot).x() == pytest.approx(curve._pivot_point("light", plot).x())
    before = curve._output_ev.copy()
    start = curve._pivot_point(zone, plot).toPoint() + QPoint(6, 0)
    events: list[tuple[str, dict[str, float]]] = []
    panel.interaction_started.connect(lambda: events.append(("start", {})))
    panel.grading_extended_changed.connect(lambda patch: events.append(("change", patch)))
    panel.interaction_ended.connect(lambda: events.append(("end", {})))

    qtbot.mousePress(curve, Qt.MouseButton.LeftButton, pos=start)
    assert base._wheels[zone]._control("pivot").value() == 0.0
    qtbot.mouseRelease(curve, Qt.MouseButton.LeftButton, pos=start + QPoint(24, 0))
    actual = base._wheels[zone]._control("pivot").value()
    assert actual == pytest.approx(24 * 16.0 / plot.width() * 2.0, abs=1e-5)
    assert curve._pivot_point(zone, plot).x() == pytest.approx(start.x() - 6 + 24, abs=0.5)
    assert events == [("start", {}), ("change", {f"base_grade_{zone}_pivot": actual}), ("end", {})]
    assert not np.allclose(before, curve._output_ev)


@pytest.mark.parametrize(("params", "edge", "inward"), (
    ({"base_grade_balance_exposure": 8.0, "base_grade_flare": 0.1}, "left", 1),
    ({"base_grade_contrast": 0.2, "base_grade_dim_pivot": 4.0}, "right", -1),
))
def test_curve_pivot_can_be_dragged_back_from_outside_the_display(params, edge, inward, qtbot) -> None:
    panel = GradingPanel()
    qtbot.addWidget(panel)
    _show_base_grade(panel, qtbot)
    base = panel.base_grade_panel
    curve = base.tone_curve
    base.resize(810, base.height())
    qtbot.waitUntil(curve.isVisible)
    panel.load_extended_params(params)
    plot = curve._plot_rect()
    point = curve._pivot_point("dim", plot)
    assert point.x() == (plot.left() if edge == "left" else plot.right())
    field = base._wheels["dim"]._control("pivot")
    value = field.value()
    start = point.toPoint()
    qtbot.mousePress(curve, Qt.MouseButton.LeftButton, pos=start)
    assert field.value() == value

    distance = math.ceil(plot.width() * 0.25)
    qtbot.mouseRelease(curve, Qt.MouseButton.LeftButton, pos=start + QPoint(inward * distance, 0))
    moved = curve._pivot_point("dim", plot)
    assert plot.left() < moved.x() < plot.right()
    assert (field.value() - value) * inward > 0


@pytest.mark.parametrize("transition", ("hide", "switch", "hydrate"))
def test_curve_drag_stops_on_resize_zone_switch_or_hydration(transition, qtbot) -> None:
    panel = GradingPanel()
    qtbot.addWidget(panel)
    _show_base_grade(panel, qtbot)
    base = panel.base_grade_panel
    curve = base.tone_curve
    base.resize(810, base.height())
    qtbot.waitUntil(curve.isVisible)
    start = curve._pivot_point("dim", curve._plot_rect()).toPoint()
    events: list[str] = []
    panel.interaction_started.connect(lambda: events.append("start"))
    panel.grading_extended_changed.connect(lambda _: events.append("change"))
    panel.interaction_ended.connect(lambda: events.append("end"))
    qtbot.mousePress(curve, Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(curve, start + QPoint(12, 0))
    assert events == ["start"]
    edited = base._wheels["dim"]._control("pivot").value()
    assert edited > 1.0

    if transition == "hide":
        base.resize(496, base.height())
        qtbot.waitUntil(curve.isHidden)
    elif transition == "switch":
        base.zone_buttons.button(1).click()
    else:
        panel.load_extended_params({"base_grade_dim_pivot": -3.0})
    qtbot.mouseRelease(curve, Qt.MouseButton.LeftButton, pos=start + QPoint(36, 0))
    panel.flush_pending_changes()
    if transition == "hydrate":
        assert events == ["start", "end"]
        assert base._wheels["dim"]._control("pivot").value() == -3.0
    else:
        assert events == ["start", "change", "end"]
        assert base._wheels["dim"]._control("pivot").value() == edited
    assert base._wheels["dark"]._control("pivot").value() == BASE_GRADE_DEFAULTS["base_grade_dark_pivot"]


@pytest.mark.parametrize("control", ("tint", "ring", "exposure_arc", "sat_arc", "value"))
def test_hydration_closes_pointer_gesture_and_ignores_release(control, qtbot) -> None:
    panel = GradingPanel()
    qtbot.addWidget(panel)
    _show_base_grade(panel, qtbot)
    wheel = panel.base_grade_panel._wheels["balance"]
    if control == "value":
        widget = wheel.controls["base_grade_balance_exposure"]
    else:
        widget = wheel.canvas
    start = widget.rect().center()
    if control == "ring":
        center, radius = wheel.canvas._geometry()
        start = center.toPoint() + QPoint(round(radius - 1.0), 0)
    elif control in ("exposure_arc", "sat_arc"):
        start = _arc_point(wheel, -1 if control == "sat_arc" else 1)
    move = start + QPoint(12, 12)
    release = move + QPoint(25, 19)

    events: list[str] = []
    panel.interaction_started.connect(lambda: events.append("start"))
    panel.grading_extended_changed.connect(lambda _: events.append("change"))
    panel.interaction_ended.connect(lambda: events.append("end"))
    qtbot.mousePress(widget, Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(widget, move)
    assert events == ["start"]

    hydrated_params = {"base_grade_balance_exposure": 0.75, "base_grade_balance_a": 0.04}
    panel.load_extended_params(hydrated_params)
    qtbot.mouseMove(widget, release)
    qtbot.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=release)
    panel.flush_pending_changes()

    assert events == ["start", "end"]
    current = panel.get_current_params()
    assert {key: current[key] for key in hydrated_params} == hydrated_params
    assert current["base_grade_balance_b"] == BASE_GRADE_DEFAULTS["base_grade_balance_b"]
    assert current["base_grade_saturation"] == BASE_GRADE_DEFAULTS["base_grade_saturation"]


@pytest.mark.parametrize("action", ("zone_button", "hide_dock", "stack", "reset"))
def test_context_transition_finishes_active_base_grade_edit(action, qtbot) -> None:
    panel = GradingPanel()
    qtbot.addWidget(panel)
    _show_base_grade(panel, qtbot)
    wheel = panel.base_grade_panel._wheels["dim"]
    widget = wheel.canvas
    start = _arc_point(wheel, 1)
    move = start + QPoint(0, -12)
    release = move + QPoint(0, -24)
    events: list[str] = []
    panel.interaction_started.connect(lambda: events.append("start"))
    panel.grading_extended_changed.connect(lambda _: events.append("change"))
    panel.interaction_ended.connect(lambda: events.append("end"))

    qtbot.mousePress(widget, Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(widget, move)
    assert events == ["start"]
    dragged_value = wheel.controls["base_grade_dim_exposure"].value()
    assert dragged_value > 0.0
    if action == "zone_button":
        panel.base_grade_panel.zone_buttons.button(1).click()
    elif action == "hide_dock":
        panel.base_grade_panel.hide()
    elif action == "stack":
        panel.current_stack_id = "next-stack"
    else:
        panel.base_grade_panel.reset_btn.click()
    qtbot.mouseMove(widget, release)
    qtbot.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=release)
    panel.flush_pending_changes()

    if action == "stack":
        assert events == ["start", "end"]
    elif action == "reset":
        assert events == ["start", "change", "end", "start", "change", "end"]
    else:
        assert events == ["start", "change", "end"]
    expected = dragged_value if action in ("zone_button", "hide_dock") else 0.0
    assert panel.get_current_params()["base_grade_dim_exposure"] == pytest.approx(expected)
