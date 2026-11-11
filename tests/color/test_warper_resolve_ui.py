"""Pointer/UI contracts for the Resolve-shaped palette (no GPU or decoder)."""
from __future__ import annotations

import json

import numpy as np
import pytest
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtTest import QTest
from pytestqt.qtbot import QtBot

from src.features.color.ui.warper_panel import WarperPanel
from src.shared.ui.widgets.warper_canvas import WarperCanvas


@pytest.fixture
def panel(qtbot: QtBot) -> WarperPanel:
    widget = WarperPanel()
    qtbot.addWidget(widget)
    widget.resize(877, 425)
    widget.show()
    return widget


def click_point(canvas: WarperCanvas, key: tuple[int, int],
                modifier: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier) -> None:
    QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, modifier, canvas.point_screen(key).toPoint())


def test_mode_navigation_retains_mesh_and_never_submits_new_modes(panel: WarperPanel) -> None:
    legacy = []
    panel.warper_changed.connect(legacy.append)
    panel.warper.selected = {(2, 1)}
    panel.warper.edit_component("hue", 0.25)
    saved = panel.warper.points.copy()
    assert legacy == []  # HSP is a presentation-only space until backend work.
    for space in ("HSL", "HSP"):
        panel.space_combo.setCurrentText(space)
        panel.warper.edit_component("sat", 0.6)
        panel.grid_combo.setCurrentText("8")
        panel.on_reset()
    assert legacy == []
    panel.warper.points = saved
    for mode in ("chroma_luma", "chroma_warp"):
        panel.mode_buttons[mode].click()
        assert panel.canvas_stack.currentWidget() is panel.canvases[mode]
        assert panel.tools_stack.currentWidget() is panel.tools[mode]
        panel.on_reset()
    assert legacy == []
    panel.set_mode("hue_saturation")
    assert np.array_equal(panel.warper.points, saved)
    # Existing projects hydrate their original HSV interpretation.
    panel.set_params(np.zeros((6, 6, 2), dtype=np.float32))
    assert panel.space_combo.currentText() == "HSV"
    panel.warper.selected = {(1, 1)}
    panel.warper.edit_component("hue", 0.2)
    assert len(legacy) == 1 and legacy[0].shape == (6, 6, 2)


def test_linked_and_independent_resolution_contract(panel: WarperPanel) -> None:
    legacy = []
    panel.set_params(np.zeros((6, 6, 2), dtype=np.float32))
    panel.warper_changed.connect(legacy.append)
    panel.grid_combo.setCurrentText("8")
    assert panel.saturation_combo.currentText() == "8"
    assert panel.warper.points.shape == (8, 8, 2)
    assert legacy == [None]
    panel.link_button.click()
    panel.saturation_combo.setCurrentText("12")
    assert panel.warper.points.shape == (12, 8, 2)
    panel.warper.selected = {(2, 1)}
    panel.warper.edit_component("hue", 0.4)
    assert legacy == [None]
    panel.set_mode("chroma_luma")
    assert panel.grid_combo.currentText() == "6"
    panel.set_mode("hue_saturation")
    assert (panel.grid_combo.currentText(), panel.saturation_combo.currentText()) == ("8", "12")


def test_selection_toggle_multiselect_pin_and_group_drag(panel: WarperPanel) -> None:
    canvas = panel.warper
    click_point(canvas, (1, 1))
    assert canvas.selected == {(1, 1)}
    assert panel.hs_tools.values["hue"].isEnabled()
    click_point(canvas, (1, 1))
    assert not canvas.selected
    click_point(canvas, (1, 1))
    click_point(canvas, (2, 2), Qt.KeyboardModifier.ShiftModifier)
    assert canvas.selected == {(1, 1), (2, 2)}
    canvas.pins.add((2, 2))
    original = canvas.points.copy()
    start = canvas.point_screen((1, 1)).toPoint()
    QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(canvas, start + QPoint(15, 8))
    QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=start + QPoint(15, 8))
    assert not np.allclose(canvas.points[1, 1], original[1, 1])
    assert np.array_equal(canvas.points[2, 2], original[2, 2])
    panel.hs_tools.action_buttons["column"].click()
    assert all((r, 1) in canvas.selected for r in range(6))
    panel.hs_tools.action_buttons["convert_pin"].click()
    assert not canvas.selected and (1, 1) in canvas.pins
    panel.hs_tools.action_buttons["reset_selection"].click()
    assert not canvas.pins and not canvas.selected


def test_lasso_range_and_auto_lock(panel: WarperPanel) -> None:
    canvas = panel.warper
    panel.hs_tools.tool_buttons["draw"].click()
    point = canvas.point_screen((3, 2)).toPoint()
    QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=point - QPoint(9, 9))
    for offset in (QPoint(9, -9), QPoint(9, 9), QPoint(-9, 9)):
        QTest.mouseMove(canvas, point + offset)
    QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=point - QPoint(9, 9))
    assert (3, 2) in canvas.selected
    panel.hs_tools.tool_buttons["select"].click()
    panel.hs_tools.auto_lock.setChecked(True)
    panel.hs_tools.auto_lock.toggled.emit(True)
    assert panel.hs_tools.lock_combo.isEnabled()
    click_point(canvas, (1, 1))
    assert canvas.pins
    panel.hs_tools.range_bar.range_changed.emit(0.0, 0.2)
    assert canvas.selected
    assert all(0 <= canvas._hue_from_pos(canvas._initial_position(*key)) <= 0.2 for key in canvas.selected)


def test_smoothing_changes_only_requested_component(panel: WarperPanel) -> None:
    canvas = panel.warper
    canvas.selected = {(2, 1)}
    before = canvas.points[2, 1].copy()
    canvas.edit_component("hue", smooth=True)
    assert np.linalg.norm(canvas.points[2, 1]) == pytest.approx(np.linalg.norm(before))
    hue = canvas._hue_from_pos(canvas.points[2, 1])
    canvas.edit_component("sat", smooth=True)
    assert canvas._hue_from_pos(canvas.points[2, 1]) == pytest.approx(hue)
    panel.set_mode("chroma_luma")
    canvas = panel.chroma_luma
    canvas.selected = {(2, 1)}
    canvas.points[2, 1] += [0.2, 0.1]
    y = float(canvas.points[2, 1, 1])
    canvas.edit_component("chroma", smooth=True)
    assert canvas.points[2, 1, 1] == y


def test_luma_ui_value_has_no_legacy_edit(panel: WarperPanel) -> None:
    panel.set_params(np.zeros((6, 6, 2), dtype=np.float32))
    emitted = []
    panel.warper_changed.connect(emitted.append)
    panel.warper.selected = {(1, 1)}
    panel.warper.edit_component("luma", 0.7)
    assert panel.warper.selected_values()["luma"] == 0.7
    assert emitted == []
    panel.warper.edit_component("luma")
    assert panel.warper.selected_values()["luma"] == 0.5


def test_luma_numeric_drag_never_opens_legacy_history(panel: WarperPanel) -> None:
    panel.set_params(np.zeros((6, 6, 2), dtype=np.float32))
    panel.warper.selected = {(1, 1)}
    panel._sync_tools()
    events = []
    panel.interaction_started.connect(lambda: events.append("start"))
    panel.interaction_ended.connect(lambda: events.append("end"))
    value = panel.hs_tools.values["luma"]
    value.interaction_started.emit()
    value.setValue(0.7)
    value.interaction_ended.emit()
    assert not events


def test_global_reset_clears_legacy_even_from_other_modes(panel: WarperPanel) -> None:
    delta = np.zeros((6, 6, 2), dtype=np.float32)
    delta[1, 1] = [0.05, 0.1]
    panel.set_params(delta)
    panel.set_mode("chroma_luma")
    panel.chroma_luma.points[2, 1] += 0.1
    emitted = []
    panel.warper_changed.connect(emitted.append)
    panel.reset_all()
    assert emitted == [None]
    assert np.allclose(panel.warper.points[1, 1], panel.warper._initial_position(1, 1))
    assert np.allclose(panel.chroma_luma.points[2, 1], panel.chroma_luma._initial_position(2, 1))


def test_numeric_drag_is_one_legacy_interaction(panel: WarperPanel) -> None:
    panel.set_params(np.zeros((6, 6, 2), dtype=np.float32))
    panel.warper.selected = {(1, 1)}
    panel._sync_tools()
    events = []
    panel.interaction_started.connect(lambda: events.append("start"))
    panel.interaction_ended.connect(lambda: events.append("end"))
    value = panel.hs_tools.values["hue"]
    value.interaction_started.emit()
    for v in (0.1, 0.2, 0.3):
        value.setValue(v)
    value.interaction_ended.emit()
    assert events == ["start", "end"]


def test_dual_grids_preserve_independent_points_and_pins(panel: WarperPanel) -> None:
    panel.set_mode("chroma_luma")
    canvas = panel.chroma_luma
    canvas.selected = {(2, 1)}
    canvas.edit_component("chroma", 0.2)
    canvas.pins = {(0, 0)}
    first = canvas.points.copy()
    panel.cl_tools.grid_buttons[1].click()
    assert canvas.active_grid == 1 and not canvas.pins
    assert not np.array_equal(canvas.points, first)
    canvas.selected = {(1, 2)}
    canvas.edit_component("luma", 0.25)
    second = canvas.points.copy()
    panel.cl_tools.grid_buttons[0].click()
    assert np.array_equal(canvas.points, first) and canvas.pins == {(0, 0)}
    panel.cl_tools.grid_buttons[1].click()
    assert np.array_equal(canvas.points, second)


def test_chroma_warp_strokes_pins_parameters_and_escape(panel: WarperPanel) -> None:
    panel.set_mode("chroma_warp")
    canvas = panel.chroma_warp
    canvas.tool = "point_stroke"
    QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=canvas._screen([0.3, 0.3]).toPoint())
    QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=canvas._screen([0.6, 0.5]).toPoint())
    assert len(canvas.strokes) == 1
    assert canvas.strokes[0]["parameters"]["stroke_mode"] == "point_to_point"
    assert panel.stroke_tools.mode_buttons["point_to_point"].isChecked()
    canvas.tool = "add_pin"
    for p in ([0.2, 0.6], [0.7, 0.6]):
        QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=canvas._screen(p).toPoint())
    canvas.selected = ("pin", 0)
    canvas.set_parameter("exposure", 2.0)
    canvas.selected = ("pin", 1)
    panel._sync_tools()
    assert panel.stroke_tools.values["exposure"].value() == 0.0
    canvas.tool = "select"
    start = canvas._screen(canvas.pins[0]["position"]).toPoint()
    before = list(canvas.pins[0]["position"])
    QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(canvas, start + QPoint(20, 20))
    QTest.keyClick(canvas, Qt.Key.Key_Escape)
    QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=start + QPoint(20, 20))
    assert canvas.pins[0]["position"] == before
    canvas.selected = ("stroke", 0)
    canvas.delete_selection()
    assert not canvas.strokes
    # The contract is ordinary serializable presentation data.
    json.dumps(panel.ui_state())


def test_hydration_none_cancels_pending_and_clears_selection(panel: WarperPanel, qtbot: QtBot) -> None:
    canvas = panel.warper
    canvas.selected = {(2, 1)}
    canvas.points[2, 1] += 0.1
    canvas._pending_deltas = np.ones((6, 6, 2), dtype=np.float32)
    canvas._changed_timer.start()
    legacy = []
    panel.warper_changed.connect(legacy.append)
    panel.set_params(None)
    qtbot.wait(30)
    assert legacy == [] and not canvas.selected
    assert np.allclose(canvas.points[2, 1], canvas._initial_position(2, 1))


def test_expand_restore_and_narrow_footer(panel: WarperPanel, qtbot: QtBot) -> None:
    panel.set_mode("chroma_luma")
    panel.resize(640, 425)
    qtbot.wait(10)
    assert panel.footer.height() == 70
    panel.toggle_expanded()
    qtbot.wait(10)
    assert panel.surface.parent() is panel._dialog
    assert panel.footer.height() == 47
    panel.toggle_expanded()
    assert panel.surface.parent() is panel
    assert panel._dialog is None


def test_large_canvas_background_uses_same_aspect_ratio_as_mesh(qtbot: QtBot) -> None:
    canvas = WarperCanvas()
    qtbot.addWidget(canvas)
    canvas.resize(2000, 500)
    image = canvas._background_image()
    assert image.width() / image.height() == pytest.approx(4, rel=0.005)
    assert canvas._get_point_at(canvas.point_screen((5, 4))) == (5, 4)


@pytest.mark.parametrize("dimensions", [(624, 343), (556, 270), (900, 350), (400, 600)])
def test_chroma_luma_grid_is_square_and_hit_geometry_tracks_resize(
        panel: WarperPanel, dimensions: tuple[int, int]) -> None:
    canvas = panel.chroma_luma
    canvas.resize(*dimensions)
    for index in (0, 1):
        rect = canvas.grid_rect(index)
        assert rect.width() == pytest.approx(rect.height())
        assert rect.center().y() == pytest.approx(canvas.height() / 2)
        canvas.activate_grid(index)
        point = canvas.point_screen((2, 3))
        assert canvas._get_point_at(point) == (2, 3)
        assert np.allclose(canvas._normalized(point), canvas.points[2, 3])


def test_chroma_warp_has_colored_square_outside_gamut_and_unchanged_coordinates(panel: WarperPanel) -> None:
    panel.set_mode("chroma_warp")
    canvas = panel.chroma_warp
    canvas.show_grid = False
    image = canvas.grab().toImage()
    a, b = canvas._screen([0.02, 0.15]), canvas._screen([0.98, 0.85])
    outside_red = image.pixelColor(round(a.x()), round(a.y()))
    outside_blue = image.pixelColor(round(b.x()), round(b.y()))
    assert outside_red != outside_blue  # Both used to be a flat panel grey.
    assert outside_red.red() > outside_red.blue()
    assert outside_blue.blue() > outside_blue.red()
    assert max(outside_red.getRgb()[:3]) < 60
    assert np.allclose(canvas._normalized(canvas._screen([0.3, 0.7])), [0.3, 0.7])


def test_chroma_luma_neutral_axis_and_monotone_luma_without_hue_bends(panel: WarperPanel) -> None:
    canvas = panel.chroma_luma
    canvas.resize(624, 343)
    image = canvas._background_image()
    for index in (0, 1):
        field = canvas.field_rect(index)
        x = round(field.center().x())
        levels = [image.pixelColor(x, round(field.top() + field.height() * t)).getRgb()[:3]
                  for t in (0.05, 0.35, 0.65, 0.95)]
        assert all(max(rgb) - min(rgb) <= 1 for rgb in levels)
        assert all(levels[i][0] > levels[i + 1][0] for i in range(3))
    warm = image.pixelColor(8, 60)
    cool = image.pixelColor(304, 60)
    assert warm.red() > warm.blue() and warm.green() > warm.blue()
    assert cool.blue() > cool.red() and cool.blue() > cool.green()


def test_axis_angle_recolors_field_without_changing_grid_or_publishing_legacy(panel: WarperPanel) -> None:
    panel.set_mode("chroma_luma")
    canvas = panel.chroma_luma
    before = canvas._background_image().copy()
    points = canvas.points.copy()
    emitted = []
    panel.warper_changed.connect(emitted.append)
    panel.axis_angle.setValue(90)
    after = canvas._background_image()
    assert before != after
    assert np.array_equal(points, canvas.points)
    assert not emitted


def test_chroma_luma_footer_stays_inline_at_user_screenshot_width(panel: WarperPanel, qtbot: QtBot) -> None:
    panel.resize(825, 380)
    panel.set_mode("chroma_luma")
    qtbot.wait(10)
    assert panel.footer.height() == 47
    assert panel._axis_group.parent() is panel._footer_row
    assert panel._axis_group.geometry().right() < panel.space_combo.geometry().left()
