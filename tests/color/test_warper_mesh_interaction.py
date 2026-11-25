"""Mesh gesture regressions across the palette, authored state and render projection."""
from __future__ import annotations

import numpy as np
import pytest
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QGraphicsScene, QGraphicsView
from pytestqt.qtbot import QtBot

from src.features.color.adapters.warper_command_adapter import WarperCommandAdapter
from src.features.color.core.layer import Layer
from src.features.color.core.warper_color import apply_resolve_hs709
from src.features.color.core.warper_commands import WarperCommandService
from src.features.color.ui.warper_panel import WarperPanel
from src.shared.ui.widgets.warper_canvas import WHITE_POINT, WarperCanvas


@pytest.fixture
def hosted(qtbot: QtBot) -> tuple[WarperPanel, Layer, list]:
    panel = WarperPanel(use_measured_warper=lambda: True)
    qtbot.addWidget(panel)
    panel.resize(940, 480)
    panel.show()
    layer = Layer("Warper")
    snapshots = []
    service = WarperCommandService(history_push=lambda current: snapshots.append(current.warper_state),
                                   history_discard=lambda: snapshots.pop())
    adapter = WarperCommandAdapter(service, lambda: layer)

    def submit(command: dict) -> None:
        result = adapter.submit(command)
        assert result["ok"], result
        if result["dirty"] and command["payload"]["phase"] != "update":
            panel.confirm_mesh_edit(layer.warper_state)

    panel.command_requested.connect(submit)
    panel.load_layer_state(layer)
    return panel, layer, snapshots


def drag(canvas: WarperCanvas, key: tuple[int, int], offset: QPoint) -> None:
    start = canvas.point_screen(key).toPoint()
    QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=start + offset)


def test_outer_spoke_stretches_to_white_and_stops_at_authored_lock(hosted: tuple) -> None:
    panel, layer, history = hosted
    canvas = panel.warper
    neutral = canvas.display_points().copy()
    start = canvas.point_screen((2, 0)).toPoint()
    QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier, start)
    drag(canvas, (5, 0), QPoint(29, -12))
    delta = canvas.display_points() - neutral
    np.testing.assert_allclose(delta[:3, 0], 0, atol=1e-7)
    np.testing.assert_allclose(delta[3, 0], delta[-1, 0] / 3, atol=1e-7)
    np.testing.assert_allclose(delta[4, 0], delta[-1, 0] * 2 / 3, atol=1e-7)
    np.testing.assert_allclose(delta[:, 1:], 0, atol=1e-7)
    np.testing.assert_array_equal(np.argwhere(layer.warper_state.grids["hs"].offsets.any(axis=2)), [[5, 0]])
    assert len(history) == 2
    # Hit-testing follows the moved neighbor, and promoting it starts at its
    # visible position even after the original drag has been committed.
    before = canvas.point_screen((3, 0))
    drag(canvas, (3, 0), QPoint(11, 5))
    after = canvas.point_screen((3, 0))
    assert (after - before).x() == pytest.approx(11, abs=1e-4)
    assert (after - before).y() == pytest.approx(5, abs=1e-4)
    assert len(history) == 3


def test_rectangular_mesh_edges_and_second_grid_keep_independent_anchors(hosted: tuple) -> None:
    panel, layer, history = hosted
    panel.mode_buttons["chroma_luma"].click()
    canvas = panel.chroma_luma
    base = canvas.display_points().copy()
    drag(canvas, (0, 0), QPoint(26, 0))
    delta = canvas.display_points() - base
    np.testing.assert_allclose(delta[3, 0], delta[0, 0] * 0.5, atol=1e-7)
    np.testing.assert_allclose(delta[3, 3], delta[0, 0] * 0.25, atol=1e-7)
    np.testing.assert_allclose(delta[-1, :], 0, atol=1e-7)
    np.testing.assert_array_equal(canvas.display_points(1), base)
    first = canvas.display_points(0).copy()
    canvas.activate_grid(1)
    drag(canvas, (3, 3), QPoint(-20, 15))
    np.testing.assert_array_equal(canvas.display_points(0), first)
    np.testing.assert_array_equal(np.argwhere(layer.warper_state.grids["grid1"].offsets.any(axis=2)), [[6, 0]])
    np.testing.assert_array_equal(np.argwhere(layer.warper_state.grids["grid2"].offsets.any(axis=2)), [[3, 3]])
    assert len(history) == 2


def test_white_point_tint_is_rendered_reloaded_and_cancellable(hosted: tuple, qtbot: QtBot) -> None:
    panel, layer, history = hosted
    canvas = panel.warper
    base = canvas.display_points().copy()
    drag(canvas, WHITE_POINT, QPoint(20, -8))
    shift = canvas.display_points() - base
    assert np.linalg.norm(shift[0], axis=1).min() > 0
    np.testing.assert_allclose(shift[-1], 0, atol=1e-7)
    assert layer.warper_state.white_point == pytest.approx((*canvas.white_point, 0.0))
    assert not layer.warper_state.grids["hs"].offsets.any()
    restored = Layer.from_dict(layer.to_dict())
    np.testing.assert_array_equal(restored.warper_params, layer.warper_params)
    neutral = np.array([[0.5, 0.5, 0.5]], np.float32)
    rgb = apply_resolve_hs709(neutral, restored.warper_params, restored.warper_space)
    assert np.ptp(rgb) > 0.04
    base_state = layer.warper_state.to_dict()
    p = canvas.point_screen(WHITE_POINT).toPoint()
    QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=p)
    QTest.mouseMove(canvas, p + QPoint(-11, -8))
    qtbot.waitUntil(lambda: layer.warper_state.to_dict() != base_state)
    QTest.keyClick(canvas, Qt.Key.Key_Escape)
    QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=p)
    assert layer.warper_state.to_dict() == base_state
    assert len(history) == 1
    panel.load_layer_state(restored)
    assert canvas.white_point == pytest.approx(base_state["white_point"][:2])
    QTest.mouseClick(canvas, Qt.MouseButton.RightButton, pos=canvas.point_screen(WHITE_POINT).toPoint())
    assert layer.warper_state.is_identity()


def test_selection_expands_cross_and_range_translates_without_grade(hosted: tuple) -> None:
    panel, layer, history = hosted
    canvas = panel.warper
    center = canvas.point_screen((5, 0)).toPoint()
    QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=center)
    QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=center)
    assert canvas.selected == {(5, 0)}
    panel.hs_tools.action_buttons["falloff_add"].click()
    assert canvas.selected == {(5, 0), (4, 0), (5, 1), (5, 5)}
    panel.hs_tools.action_buttons["falloff_sub"].click()
    assert canvas.selected == {(5, 0)}
    bar = panel.hs_tools.range_bar
    width = bar.width()
    middle = bar.height() // 2
    QTest.mousePress(bar, Qt.MouseButton.LeftButton, pos=QPoint(width // 4, middle))
    QTest.mouseRelease(bar, Qt.MouseButton.LeftButton, pos=QPoint(width // 2, middle))
    interval = bar.high - bar.low
    QTest.mousePress(bar, Qt.MouseButton.LeftButton, pos=QPoint(width * 3 // 8, middle))
    QTest.mouseRelease(bar, Qt.MouseButton.LeftButton, pos=QPoint(width // 2, middle))
    assert bar.low == pytest.approx(0.375, abs=0.02)
    assert bar.high - bar.low == pytest.approx(interval)
    assert layer.warper_state is None and not history


def test_option_isolation_is_transient_and_input_is_retried(hosted: tuple, qtbot: QtBot) -> None:
    panel, layer, history = hosted
    view = QGraphicsView()
    qtbot.addWidget(view)
    # Production Canvas shadows the Qt scene accessor with an instance field.
    view.scene = QGraphicsScene(view)
    view.setScene(view.scene)
    transitions = []
    panel.bind_viewer(view, lambda: None, overlay_changed=lambda: transitions.append(True))
    context = ("node", "clip")
    key = panel.request_viewer_input("node", context)
    assert key == panel.request_viewer_input("node", context)
    rgb = np.array([[[0.8, 0.16, 0.16], [0.16, 0.8, 0.16]]], dtype=np.float32)
    panel.accept_viewer_input((key, (0, 0, 2, 1), True, rgb))
    assert panel.request_viewer_input("node", context) is None
    canvas = panel.warper
    point = canvas.point_screen((5, 0)).toPoint()
    QTest.mousePress(canvas, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.AltModifier, point)
    overlay = panel._isolation_item
    assert overlay is not None and overlay.isVisible()
    assert overlay.pixmap().toImage().pixelColor(0, 0).alpha() == 0
    assert overlay.pixmap().toImage().pixelColor(1, 0).alpha() == 255
    QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.AltModifier, point)
    assert not overlay.isVisible()
    assert len(transitions) == 2 and not history and layer.warper_state is None


def test_density_keeps_last_frame_while_same_source_refreshes(hosted: tuple) -> None:
    panel, _, _ = hosted
    source = ("node", "clip", 14)
    crop = (0, 0, 2, 1)
    red = np.array([[[0.9, 0.16, 0.16]]], dtype=np.float32)
    green = np.array([[[0.16, 0.9, 0.16]]], dtype=np.float32)
    first = panel.request_viewer_input("node", source, revision=(1, crop))
    panel.accept_viewer_input((first, crop, True, red))
    old = panel.warper.density_overlays
    assert old
    second = panel.request_viewer_input("node", source, revision=(2, crop))
    assert panel.warper.density_overlays is old
    assert second == panel.request_viewer_input("node", source, revision=(2, crop))
    panel.accept_viewer_input((first, crop, True, green))
    assert panel.warper.density_overlays is old
    panel.accept_viewer_input((second, crop, True, green))
    assert panel.warper.density_overlays is not old and panel.warper.density_overlays
    assert panel.request_viewer_input("node", source, revision=(2, crop)) is None
    third = panel.request_viewer_input("node", ("node", "clip", 15), revision=(2, crop))
    assert third != second and not panel.warper.density_overlays
    assert panel._viewer_input is None


def test_pull_push_apply_on_click_and_only_in_the_touched_region(hosted: tuple) -> None:
    panel, layer, history = hosted
    canvas = panel.warper
    original = canvas.display_points().copy()
    panel.hs_tools.tool_buttons["push"].click()
    press = ((canvas.point_screen((2, 0)) + canvas.point_screen((3, 0))) / 2).toPoint()
    QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=press)
    affected = canvas.display_points() - original
    assert np.linalg.norm(affected[:-1, 0]) > 0.07
    np.testing.assert_allclose(affected[-1], 0, atol=1e-7)
    np.testing.assert_allclose(affected[:, 1:], 0, atol=1e-7)
    panel.hs_tools.tool_buttons["pull"].click()
    QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=press)
    assert np.linalg.norm(canvas.display_points() - original) < np.linalg.norm(affected)
    assert len(history) == 2 and layer.warper_state is not None


def test_pin_range_reset_and_backtracked_numeric_edit_are_local(hosted: tuple) -> None:
    panel, layer, history = hosted
    panel.mode_buttons["chroma_warp"].click()
    canvas = panel.chroma_warp
    point = canvas._screen([0.3, 0.3]).toPoint()
    QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=point)
    tonal = panel.stroke_tools.values["tonal_low"]
    tonal.setValue(0.55)
    exposure = panel.stroke_tools.values["exposure"]
    base = layer.warper_state.to_dict()
    before = len(history)
    exposure.interaction_started.emit()
    exposure.setValue(1.0)
    exposure.setValue(0.0)
    exposure.interaction_ended.emit()
    assert layer.warper_state.to_dict() == base and len(history) == before

    panel.stroke_tools.tool_buttons["add_pin"].click()
    QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=canvas._screen([0.44, 0.44]).toPoint())
    assert not tonal.isEnabled() and not exposure.isEnabled()
    panel.stroke_tools.values["chroma_range"].setValue(0.15)
    panel.stroke_tools.pin_reset.click()
    assert layer.warper_state.pin_points[0].params["chroma_range"] == pytest.approx(0.04)
    assert layer.warper_state.strokes[0].params["tonal_low"] == pytest.approx(0.55)
