"""Pointer/UI contracts for the Resolve-shaped palette (no GPU or decoder)."""
from __future__ import annotations

import json
from dataclasses import replace

import numpy as np
import pytest
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QToolButton
from pytestqt.qtbot import QtBot

from src.features.color.adapters.warper_command_adapter import WarperCommandAdapter
from src.features.color.core.layer import Layer
from src.features.color.core.warper_commands import WarperCommandService
from src.features.color.core.warper_color import RESOLVE_HS709_SPACE, RESOLVE_HS709_V2_SPACE
from src.features.color.core.warper_state import (
    RESOLVE_HS_ALGORITHM, RESOLVE_HS_ALGORITHM_V2, WARPER_SCHEMA_VERSION, WarperGrid, WarperPin, WarperStateV2,
)
from src.features.color.ui.warper_panel import WarperPanel
from src.shared.ui.widgets.warper_canvas import CALIBRATED_MAX_RADIUS, CALIBRATED_SAT_SCALE, WarperCanvas


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


def test_calibrated_rectangular_grid_numeric_edits_and_hydration(panel: WarperPanel) -> None:
    layer = Layer("warper")
    history = []
    service = WarperCommandService(history_push=lambda target: history.append(target.id))
    adapter = WarperCommandAdapter(service, lambda: layer)
    results = []
    def apply(command: dict) -> None:
        result = adapter.submit(command)
        results.append(result)
        if result["dirty"] and command["payload"]["phase"] != "update":
            panel.confirm_mesh_edit(layer.warper_state)

    panel.command_requested.connect(apply)
    legacy = []
    panel.warper_changed.connect(legacy.append)
    panel._use_measured_warper = lambda: True
    panel.load_layer_state(layer)
    assert layer.warper_state is None
    assert not panel.space_combo.isEnabled()

    panel.link_button.click()
    panel.grid_combo.setCurrentText("8")
    canvas = panel.warper
    click_point(canvas, (2, 0))
    assert canvas.selected_values() == pytest.approx({"hue": 0.0, "sat": 1 / np.sqrt(3), "luma": 0.5})

    panel.hs_tools.values["hue"].setValue(0.075)
    assert layer.warper_state.algorithm_version == RESOLVE_HS_ALGORITHM_V2
    panel.hs_tools.values["sat"].setValue(0.74)
    value = panel.hs_tools.values["luma"]
    value.interaction_started.emit()
    baseline = service.revision(layer)
    value.setValue(0.57)
    value.setValue(0.63)
    value.interaction_ended.emit()
    assert service.revision(layer) == baseline + 1 == len(history)
    grid = layer.warper_state.grids["hs"]
    assert grid.offsets.shape == (6, 8, 3)
    assert grid.offsets[2, 0] == pytest.approx([-0.075, 0.74 * np.sqrt(3) / 2 - 0.5, 0.13])
    panel.hs_tools.component_buttons["hue"][-1].click()
    assert layer.warper_state.grids["hs"].offsets[2, 0, 0] == pytest.approx(0.0)
    baseline = service.revision(layer)
    panel.hs_tools.action_buttons["convert_pin"].click()
    assert layer.warper_state.grids["hs"].pins[0].point_id == "hs:2:0"
    assert service.revision(layer) == baseline + 1
    assert all(result["ok"] for result in results)
    assert not legacy

    saved = layer.warper_state.copy()
    count = len(results)
    panel.set_params(None)
    assert panel.set_warper_state(saved)
    assert len(results) == count
    assert panel.warper.calibrated_hs
    assert panel.warper.pins == {(2, 0)}
    assert np.allclose(panel.warper.calibrated_offsets(), saved.grids["hs"].offsets, atol=1e-7)


def test_calibrated_pointer_preview_can_cancel_without_revision(panel: WarperPanel, qtbot: QtBot) -> None:
    layer = Layer("warper")
    service = WarperCommandService()
    adapter = WarperCommandAdapter(service, lambda: layer)
    panel.command_requested.connect(adapter.submit)
    panel._use_measured_warper = lambda: True
    panel.load_layer_state(layer)
    before = layer.warper_state
    revision = service.revision(layer)
    canvas = panel.warper
    start = canvas.point_screen((2, 0)).toPoint()
    QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=start)
    end = start + QPoint(14, -9)
    QTest.mouseMove(canvas, end)
    qtbot.waitUntil(lambda: layer.warper_state is not None and layer.warper_state.grids["hs"].offsets.any())
    QTest.keyClick(canvas, Qt.Key.Key_Escape)
    QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=end)
    assert layer.warper_state is before is None
    assert not canvas.calibrated_offsets().any()
    assert service.revision(layer) == revision


def test_calibrated_pointer_limit_preserves_untouched_outer_points(panel: WarperPanel) -> None:
    panel._use_measured_warper = lambda: True
    panel.set_params(None)
    canvas = panel.warper
    pinned, unselected, dragged = (5, 0), (5, 3), (5, 1)
    canvas.selected = {pinned, unselected}
    canvas.edit_component("sat", 1.55 * CALIBRATED_SAT_SCALE)
    canvas.pins = {pinned}
    canvas.selected.clear()
    before = canvas.points.copy()
    start = canvas.point_screen(dragged).toPoint()
    center = canvas.grid_rect().center().toPoint()
    end = start + (start - center)
    QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(canvas, end)
    QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=end)
    assert np.array_equal(canvas.points[pinned], before[pinned])
    assert np.array_equal(canvas.points[unselected], before[unselected])
    assert np.linalg.norm(canvas.points[dragged]) == pytest.approx(CALIBRATED_MAX_RADIUS)


def test_blank_mesh_follows_project_without_changes_and_legacy_is_preserved(panel: WarperPanel) -> None:
    measured = False
    panel._use_measured_warper = lambda: measured
    changes = []
    panel.command_requested.connect(changes.append)
    panel.set_params(None)
    assert panel.space_combo.currentText() == "HSV"
    assert not any(button.text() == "709" for button in panel.findChildren(QToolButton))
    measured = True
    panel.refresh_automatic_calibration()
    assert panel.warper.calibrated_hs
    assert panel.space_combo.currentText() == "HSP"
    click_point(panel.warper, (2, 0))
    panel.on_reset()
    measured = False
    panel.refresh_automatic_calibration()
    assert not panel.warper.calibrated_hs
    assert changes == []
    grid = np.zeros((6, 6, 2), dtype=np.float32)
    grid[2, 0] = [0.1, 0.2]
    measured = True
    panel.set_params(grid)
    assert not panel.warper.calibrated_hs
    np.testing.assert_allclose(panel.warper._compute_deltas(), grid, atol=1e-7)


def test_calibrated_v1_edit_and_reset_preserve_version(panel: WarperPanel) -> None:
    grid = WarperGrid.identity("hs", 6, 6, ("dh", "ds", "dl"))
    offsets = grid.mutable_offsets()
    offsets[2, 0, 1] = 0.1
    state = WarperStateV2(
        schema_version=WARPER_SCHEMA_VERSION, algorithm_version=RESOLVE_HS_ALGORITHM,
        mode="hue_saturation", color_space="hsp", axis_angle=0.0,
        grids={"hs": grid.with_offsets(offsets)},
    )
    layer = Layer.from_dict({"warper_state": state.to_dict()})
    adapter = WarperCommandAdapter(WarperCommandService(), lambda: layer)
    panel.command_requested.connect(adapter.submit)
    assert panel.set_warper_state(layer.warper_state)
    click_point(panel.warper, (2, 0))
    panel.hs_tools.values["luma"].setValue(0.6)
    assert layer.warper_state.grids["hs"].offsets[2, 0, 2] == pytest.approx(0.1)
    assert layer.warper_state.algorithm_version == RESOLVE_HS_ALGORITHM
    assert layer.warper_space == RESOLVE_HS709_SPACE

    panel.on_reset()
    assert layer.warper_state.algorithm_version == RESOLVE_HS_ALGORITHM
    panel.refresh_automatic_calibration()
    assert panel.warper.calibrated_hs
    assert layer.warper_state.algorithm_version == RESOLVE_HS_ALGORITHM


def test_calibrated_v1_upgrade_preserves_exact_mesh_and_can_undo(panel: WarperPanel) -> None:
    grid = WarperGrid.identity("hs", 6, 8, ("dh", "ds", "dl"))
    offsets = grid.mutable_offsets()
    offsets[2, 0] = (-0.0754321, 0.1234567, 0.05000001)
    grid = replace(grid.with_offsets(offsets), pins=(WarperPin("hs:2:0"),))
    state = WarperStateV2(
        schema_version=WARPER_SCHEMA_VERSION, algorithm_version=RESOLVE_HS_ALGORITHM,
        mode="hue_saturation", color_space="hsp", axis_angle=0.0, grids={"hs": grid},
    )
    saved = state.to_dict()
    layer = Layer.from_dict({"warper_state": saved})
    history = []
    service = WarperCommandService(history_push=lambda target: history.append(target.warper_state.to_dict()))
    adapter = WarperCommandAdapter(service, lambda: layer)
    panel.command_requested.connect(adapter.submit)
    assert panel.set_warper_state(layer.warper_state, target_id=layer.id)
    assert not history

    action = panel.upgrade_calibration_action
    assert action.isVisible()
    panel.set_mode("chroma_luma")
    assert not action.isVisible()
    panel.set_mode("hue_saturation")
    assert action.isVisible()
    action.trigger()

    assert layer.warper_state.to_dict() == {**saved, "algorithm_version": RESOLVE_HS_ALGORITHM_V2}
    np.testing.assert_array_equal(layer.warper_params, offsets)
    assert layer.warper_space == RESOLVE_HS709_V2_SPACE
    assert history == [saved]
    assert service.revision(layer) == 1
    assert not action.isVisible()

    layer.apply_grade_patch({"warper_state": history[0]}, categories=("warper",))
    assert panel.set_warper_state(layer.warper_state, target_id=layer.id)
    assert layer.warper_space == RESOLVE_HS709_SPACE
    assert action.isVisible()


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
