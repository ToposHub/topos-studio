"""Selective Grade publishes one editing gesture into the corrector model."""
from __future__ import annotations

import numpy as np
import pytest
from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QGraphicsView, QLabel, QStyle, QStyleFactory, QStyleOptionSlider, QToolButton
from pytestqt.qtbot import QtBot

from src.features.color.adapters.warper_command_adapter import WarperCommandAdapter
from src.features.color.core.layer import Layer
from src.features.color.core.warper_chroma709 import _CW_VERTICES
from src.features.color.core.warper_commands import WarperCommandService
from src.features.color.core.warper_state import (
    CURRENT_ALGORITHM, HS_GRID_NAME, WarperGrid, WarperPinPoint, WarperStroke, XGradeState, legacy_from_ndarray,
)
from src.features.color.ui.x_grade_panel import _REFERENCE_SCOPE_PIXELS, XGradeCanvas, XGradePanel
from src.features.color.ui.warper_panel import WarperPanel
from src.shared.i18n.i18n import I18N


@pytest.mark.parametrize(("locale", "title", "reset", "show"), (
    ("en_US", "Selective Grade", "Reset Selective Grade", "Show Selective Grade"),
    ("zh_CN", "选色调节", "重置选色调节", "显示选色调节"),
))
def test_selective_grade_uses_its_localized_name_in_both_presentations(
    qtbot: QtBot, locale: str, title: str, reset: str, show: str,
) -> None:
    previous_locale = I18N.get_current_locale()
    try:
        assert I18N.load_locale(locale)
        panel = XGradePanel()
        qtbot.addWidget(panel)
        assert panel._standalone_header.findChild(QLabel).text() == title
        assert panel.viewer_controls.findChild(QToolButton, "warper_reset").toolTip() == reset
        assert panel.expanded_placeholder.text() == show
        assert title in panel._working_notice.text()
        panel.toggle_expanded()
        assert panel._dialog.windowTitle() == title
        panel.collapse_expanded()

        warper = WarperPanel()
        qtbot.addWidget(warper)
        warper.add_x_grade_mode(panel)
        mode = warper.mode_buttons["x_grade"]
        assert mode.toolTip() == mode.accessibleName() == title
        mode.click()
        assert warper.title.text().endswith(title)
        notices: list[str] = []
        panel.notice_requested.connect(notices.append)
        panel.chroma_warp.limit_reached.emit()
        assert title in notices[0]
    finally:
        I18N.load_locale(previous_locale)


def test_palette_strokes_pins_and_reset_use_independent_saved_state(qtbot: QtBot) -> None:
    layer = Layer("Corrector")
    original_mesh = np.full((6, 6, 2), 0.02, dtype=np.float32)
    layer.warper_state = legacy_from_ndarray(original_mesh)
    layer.warper_params = original_mesh
    history: list[Layer] = []
    service = WarperCommandService(history_push=history.append)
    adapter = WarperCommandAdapter(service, lambda: layer)
    panel = XGradePanel()
    qtbot.addWidget(panel)
    panel.resize(570, 270)
    panel.show()
    panel.load_layer_state(layer)
    results: list[dict] = []
    panel.command_requested.connect(lambda request: results.append(adapter.submit(request)))
    canvas = panel.chroma_warp

    start = canvas._screen([0.3, 0.5]).toPoint()
    end = canvas._screen([0.42, 0.58]).toPoint()
    QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(canvas, end)
    QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=end)
    assert all(result["ok"] for result in results)
    assert len(history) == 1
    assert canvas.selected == ("stroke", 0)
    panel._values["exposure"].setValue(0.75)
    panel._zone.setCurrentIndex(panel._zone.findData("light"))
    control = Qt.KeyboardModifier.ControlModifier
    QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, control, pos=canvas._screen([0.6, 0.45]).toPoint())
    assert all(result["ok"] for result in results)

    restored = Layer.from_dict(layer.to_dict())
    grade = restored.warper_state.x_grade
    assert len(grade.strokes) == len(grade.pin_points) == 1
    assert grade.strokes[0].params["exposure"] == pytest.approx(0.75)
    assert grade.strokes[0].params["x_zone"] == "light"
    np.testing.assert_array_equal(restored.warper_state.grids["hs"].offsets, original_mesh)

    panel.load_layer_state(restored)
    assert canvas.selected == ("pin", 0)
    assert len(canvas.pins) == 1

    panel.on_reset()
    assert layer.warper_state.x_grade is None
    np.testing.assert_array_equal(layer.warper_params, original_mesh)


def test_viewer_exposure_drag_cancels_without_leaving_a_local_edit(qtbot: QtBot) -> None:
    panel = XGradePanel()
    qtbot.addWidget(panel)
    canvas = panel.chroma_warp
    canvas.resize(360, 240)
    phases: list[dict] = []
    panel.command_requested.connect(phases.append)
    # Qt maps the physical Control key to Meta on macOS.
    import sys

    canvas.viewer_modifiers = (Qt.KeyboardModifier.MetaModifier if sys.platform == "darwin"
                               else Qt.KeyboardModifier.ControlModifier)
    origin = canvas.begin_viewer_drag(np.array((0.35, 0.50)))
    assert origin is not None
    canvas.move_viewer_drag(origin - QPointF(0, 25))
    canvas._publish_preview()
    state = phases[-1]["payload"]["state"]
    assert state["strokes"][0]["params"]["exposure"] > 0.0
    canvas.cancel_gesture()
    assert [update["payload"]["phase"] for update in phases] == ["update", "cancel"]
    assert not canvas.strokes


def test_unsupported_working_space_allows_inspection_and_reset(qtbot: QtBot) -> None:
    layer = Layer("Corrector")
    panel = XGradePanel()
    qtbot.addWidget(panel)
    panel.resize(570, 270)
    panel.show()
    adapter = WarperCommandAdapter(WarperCommandService(), lambda: layer)
    point, rgb = (0.35, 0.45), (0.5, 0.3, 0.2)
    grade = XGradeState(strokes=(WarperStroke("warm", "normal", rgb, rgb, {"exposure": 0.6}, (point, point)),))
    assert adapter.submit({"action": "x_grade_edit", "mode": "x_grade",
                           "payload": {"phase": "atomic", "state": grade.to_dict()}})["ok"]
    panel.load_layer_state(layer)
    panel.chroma_warp.selected = ("stroke", 0)
    panel._sync_tools()
    phases: list[str] = []

    def submit(request: dict) -> None:
        phases.append(request["payload"]["phase"])
        assert adapter.submit(request)["ok"]

    panel.command_requested.connect(submit)
    panel._begin_palette_value()
    panel._values["exposure"].setValue(0.75)
    panel.set_working_supported(False)
    assert phases == ["update", "cancel"]
    assert layer.warper_state.x_grade.strokes[0].params["exposure"] == pytest.approx(0.6)
    assert panel._working_notice.isVisible()
    assert not panel.picker_button.isEnabled()
    assert panel.request_viewer_input("x", ("x",)) is None
    commands: list[dict] = []
    panel.command_requested.connect(commands.append)
    canvas = panel.chroma_warp
    QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=canvas._screen(list(point)).toPoint())
    assert canvas.selected == ("stroke", 0)
    assert panel._values["exposure"].value() == pytest.approx(0.6)
    assert not panel._values["exposure"].isEnabled()
    QTest.keyClick(canvas, Qt.Key.Key_Delete)
    assert len(canvas.strokes) == 1 and not commands
    panel.on_reset()
    assert commands[-1]["payload"]["reset"]
    assert phases[-1] == "atomic" and layer.warper_state is None


def test_hide_cancels_a_numeric_preview(qtbot: QtBot) -> None:
    panel = XGradePanel()
    qtbot.addWidget(panel)
    panel.show()
    layer = Layer("Corrector")
    adapter = WarperCommandAdapter(WarperCommandService(), lambda: layer)
    point, rgb = (0.35, 0.45), (0.5, 0.3, 0.2)
    grade = XGradeState(strokes=(WarperStroke("warm", "normal", rgb, rgb, {}, (point, point)),))
    assert adapter.submit({"action": "x_grade_edit", "mode": "x_grade",
                           "payload": {"phase": "atomic", "state": grade.to_dict()}})["ok"]
    saved = layer.warper_state.x_grade.to_dict()
    panel.load_layer_state(layer)
    panel.chroma_warp.selected = ("stroke", 0)
    panel._sync_tools()
    phases: list[str] = []

    def submit(request: dict) -> None:
        phases.append(request["payload"]["phase"])
        assert adapter.submit(request)["ok"]

    panel.command_requested.connect(submit)
    panel._begin_palette_value()
    panel._values["exposure"].setValue(0.75)
    panel.hide()
    assert phases == ["update", "cancel"]
    assert panel._values["exposure"].value() == 0.0
    assert layer.warper_state.x_grade.to_dict() == saved


def test_pin_only_is_identity_and_reset_keeps_authored_warper_controls() -> None:
    from dataclasses import replace

    layer = Layer("Corrector")
    adapter = WarperCommandAdapter(WarperCommandService(), lambda: layer)

    def set_grade(grade: XGradeState) -> None:
        assert adapter.submit({"action": "x_grade_edit", "mode": "x_grade",
                               "payload": {"phase": "atomic", "state": grade.to_dict()}})["ok"]

    pin = XGradeState(pin_points=(WarperPinPoint("skin", (0.3, 0.4)),))
    set_grade(pin)
    assert layer.warper_params is None
    assert layer.warper_state.x_grade == pin
    set_grade(XGradeState())
    assert layer.warper_state is None
    base = legacy_from_ndarray(np.zeros((6, 6, 2), dtype=np.float32))
    base = replace(base, algorithm_version=CURRENT_ALGORITHM, color_space="hsl",
                   grids={HS_GRID_NAME: WarperGrid.identity(HS_GRID_NAME, 8, 12, ("dh", "ds"))})
    layer.warper_state = base
    set_grade(pin)
    set_grade(XGradeState())
    assert layer.warper_state.to_dict() == base.to_dict()


def test_exposure_and_width_slider_follow_measured_drag_distances(qtbot: QtBot) -> None:
    panel = XGradePanel()
    qtbot.addWidget(panel)
    panel.resize(570, 340)
    panel.show()
    canvas = panel.chroma_warp
    start, end = (canvas._screen(point).toPoint() for point in ([0.3, 0.5], [0.42, 0.58]))
    QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(canvas, end)
    QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=end)
    style = QStyleFactory.create("Fusion")
    assert style is not None
    style.setParent(panel)

    for key, default, limit, cases in (
        ("exposure", 0.0, 0.09, ((20, 1.16), (-20, -1.16), (40, 2.31))),
        ("width", 0.20, 0.02, ((20, 0.383), (-12, 0.090), (40, 0.566))),
    ):
        slider, control = panel._sliders[key], panel._values[key]
        slider.setStyle(style)
        assert slider.isEnabled()
        for pixels, native in cases:
            control.setValue(default)
            option = QStyleOptionSlider()
            slider.initStyleOption(option)
            handle = style.subControlRect(QStyle.ComplexControl.CC_Slider, option,
                                          QStyle.SubControl.SC_SliderHandle, slider).center()
            end = handle + QPoint(pixels, 0)
            QTest.mousePress(slider, Qt.MouseButton.LeftButton, pos=handle)
            QTest.mouseMove(slider, end)
            QTest.mouseRelease(slider, Qt.MouseButton.LeftButton, pos=end)
            assert control.value() == pytest.approx(native, abs=limit)


def test_picker_tracks_still_grade_revisions_and_ignores_raw_warper_samples(qtbot: QtBot) -> None:
    panel = XGradePanel()
    qtbot.addWidget(panel)
    panel.show()
    rect = (0, 0, 2, 2)
    rgb = np.zeros((2, 2, 3), dtype=np.float32)
    source = ("still", "layer")
    before = panel.request_viewer_input("grade", source, revision=(1, rect, (4, 10)))
    assert before is not None
    panel.accept_viewer_input((before, rect, True, rgb))
    assert panel._viewer_input is None
    panel.accept_viewer_input(((*before, "x_grade"), rect, True, rgb))
    assert panel._viewer_input[-1] is rgb
    assert panel.request_viewer_input("grade", source, revision=(1, rect, (4, 10))) is None

    after = panel.request_viewer_input("grade", source, revision=(1, rect, (4, 11)))
    assert after is not None
    changed = np.ones_like(rgb)
    panel.accept_viewer_input(((*after, "x_grade"), rect, True, changed))
    panel.accept_viewer_input(((*before, "x_grade"), rect, True, rgb))
    assert panel._viewer_input[-1] is changed


def test_embedded_x_grade_owns_the_viewer_picker_only_while_visible(qtbot: QtBot) -> None:
    warper, panel, view = WarperPanel(), XGradePanel(), QGraphicsView()
    warper.add_x_grade_mode(panel)
    qtbot.addWidget(warper)
    qtbot.addWidget(view)
    warper.resize(633, 289)
    warper.show()
    view.resize(320, 160)
    view.setSceneRect(0, 0, 200, 100)
    view.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
    view.show()
    for tool in (warper, panel):
        tool.bind_viewer(view, lambda: None)

    rect = (0, 0, 200, 100)
    rgb = np.array([[[0.70, 0.28, 0.13], [0.14, 0.58, 0.22]]], dtype=np.float32)
    raw = warper.request_viewer_input("node", ("clip",))
    assert raw is not None
    warper.accept_viewer_input((raw, rect, True, rgb))
    warper.picker_button.setChecked(True)
    warper.mode_buttons["x_grade"].click()
    assert panel.isVisible() and panel.picker_button.isChecked()
    assert not warper.picker_button.isChecked()
    assert warper.request_viewer_input("node", ("clip",)) is None
    target = panel.request_viewer_input("node", ("clip",))
    assert target is not None
    viewport = view.viewport()
    green, warm = view.mapFromScene(150, 50), view.mapFromScene(50, 50)

    QTest.mouseMove(viewport, green)
    panel.accept_viewer_input(((*target, "x_grade"), rect, True, rgb))
    np.testing.assert_allclose(panel.chroma_warp.viewer_sample, panel._plot_positions(rgb[0], True)[1, 0])
    assert viewport.cursor().shape() == Qt.CursorShape.BitmapCursor
    assert panel.chroma_warp.plot_rect().width() > panel.chroma_warp.plot_rect().height() * 1.5
    outline = panel.chroma_warp._scope_outline(panel.chroma_warp.plot_rect())
    assert all(outline.contains(panel.chroma_warp._screen(vertex.tolist())) for vertex in _CW_VERTICES)
    assert not panel.chroma_warp.strokes
    # The viewer picker selects a color even when the graph's select tool is active.
    panel._tool_changed("select")
    QTest.mouseClick(viewport, Qt.MouseButton.LeftButton, pos=warm)
    assert panel.chroma_warp.selected == ("stroke", 0)
    assert panel._values["exposure"].isEnabled()
    start, end = panel.chroma_warp.strokes[0]["path"]
    assert start == end
    np.testing.assert_allclose(start, panel._plot_positions(rgb[0], True)[0, 0], atol=1e-7)
    assert not warper.warper.selected

    # Qt can keep a dock's widget logically visible while another dock tab covers it.
    warper.set_dock_tool_visible(False)
    assert panel.isVisible() and not panel.picker_button.isChecked()
    assert viewport.cursor().shape() == Qt.CursorShape.OpenHandCursor
    assert panel.chroma_warp.viewer_sample is None
    warper.picker_button.setChecked(True)
    panel.picker_button.setChecked(True)
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, pos=green)
    QTest.mouseMove(viewport, green - QPoint(15, 10))
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=green - QPoint(15, 10))
    assert len(panel.chroma_warp.strokes) == 1 and not warper.warper.selected

    warper.set_dock_tool_visible(True)
    warper.mode_buttons["chroma_luma"].click()
    assert not panel.isVisible() and not panel.picker_button.isChecked()
    assert warper.mode == "chroma_luma"


def test_all_x_grade_numeric_fields_drag_and_commit_to_the_selected_color(qtbot: QtBot) -> None:
    layer, panel = Layer("Corrector"), XGradePanel()
    qtbot.addWidget(panel)
    panel.resize(633, 350)
    panel.show()
    panel.load_layer_state(layer)
    history: list[Layer] = []
    adapter = WarperCommandAdapter(WarperCommandService(history_push=history.append), lambda: layer)

    def submit(request: dict) -> None:
        outcome = adapter.submit(request)
        assert outcome["ok"], outcome
        if request["payload"]["phase"] != "update":
            panel.load_layer_state(layer)

    panel.command_requested.connect(submit)
    canvas = panel.chroma_warp
    QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=canvas._screen([0.3, 0.5]).toPoint())
    panel._zone.setCurrentIndex(panel._zone.findData("dim"))
    assert layer.warper_state.x_grade.strokes[0].params["x_zone"] == "dim"
    for field, key, multiplier, expected in (
        ("exposure", "exposure", 1.0, 0.16),
        ("width", "chroma_range", 5.0, 0.2099),
        ("tonal_pivot", "pivot", 1.0, 0.51),
        ("falloff", "x_falloff", 1.0, 0.51),
    ):
        control = panel._values[field]
        assert control.isEnabled() and control.isVisible()
        start = QPoint(18, control.height() // 2)
        end = start + QPoint(20, 0)
        count = len(history)
        QTest.mousePress(control, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(control, end)
        QTest.mouseRelease(control, Qt.MouseButton.LeftButton, pos=end)
        assert len(history) == count + 1
        params = layer.warper_state.x_grade.strokes[0].params
        assert params[key] * multiplier == pytest.approx(expected)


def test_viewer_split_handle_remains_draggable_with_x_grade_armed(qtbot: QtBot) -> None:
    from types import SimpleNamespace
    from src.features.color.ui.canvas import Canvas

    warper, panel, view = WarperPanel(), XGradePanel(), Canvas()
    warper.add_x_grade_mode(panel)
    qtbot.addWidget(warper)
    qtbot.addWidget(view)
    view.resize(320, 160)
    view.setSceneRect(0, 0, 260, 140)
    view.show()
    warper.show()
    panel.bind_viewer(view, lambda: None, available=view.accepts_warper_picker,
                      position_available=view.accepts_warper_picker_at)
    warper.mode_buttons["x_grade"].click()
    request = panel.request_viewer_input("node", ("split",))
    assert request is not None
    rgb = np.full((2, 2, 3), (0.66, 0.37, 0.21), dtype=np.float32)
    panel.accept_viewer_input(((*request, "x_grade"), (0, 0, 260, 140), True, rgb))

    viewport = view.viewport()
    center = QPoint(viewport.width() // 2, viewport.height() // 2)
    view.engine = SimpleNamespace(split_view_active=True, split_position=0.5)
    try:
        QTest.mousePress(viewport, Qt.MouseButton.LeftButton, pos=center)
        assert view.is_dragging_split
        QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=center)
        assert not view.is_dragging_split
        assert not panel.chroma_warp.strokes and panel._viewer_drag is None
    finally:
        view.engine = None


def test_x_grade_white_range_grows_in_both_dimensions_during_drag(qtbot: QtBot) -> None:
    canvas = XGradeCanvas()
    qtbot.addWidget(canvas)
    canvas.resize(680, 340)
    start = [0.31, 0.37]
    origin = canvas.begin_viewer_drag(np.array(start))
    assert origin is not None
    bounds = []
    for pixels in (0, 55, 90, 140):
        offset = pixels * canvas.plot_rect().height() / _REFERENCE_SCOPE_PIXELS
        canvas.move_viewer_drag(origin - QPointF(0.0, offset))
        bounds.append(canvas._range_outline(canvas._drawing[0], 0.04, end=canvas._drawing[-1]).boundingRect())
    for smaller, larger in zip(bounds, bounds[1:]):
        assert larger.width() > smaller.width()
        assert larger.height() > smaller.height()
        assert larger.center().y() < smaller.center().y()
    canvas.cancel_gesture()
