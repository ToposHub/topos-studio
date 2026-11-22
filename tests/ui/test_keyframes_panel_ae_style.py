"""AE 风格 Keyframes 面板交互测试。

覆盖四项优化：
  1. 滚轮缩放时间线刻度（以光标为锚点，方便逐帧操作）
  2. 鼠标中键拖拽平移时间线
  3. 范围播放条（work area）+ I/O 键设置播放范围
  4. 素材图层条：每个节点都显示图层条，拖拽调节长度控制播放范围
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from PyQt6.QtCore import QPoint, QPointF, Qt, QEvent
from PyQt6.QtGui import QContextMenuEvent, QKeyEvent, QMouseEvent, QWheelEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QMenu

from src.app.controllers.page_controller import _KeyframesTimelineTransport
from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.core.layer_stack_controller import LayerStackController
from src.features.composite.nodes.output_node import OutputNode
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.ui.composite_page import CompositePage
from src.features.composite.ui.keyframes_panel import KeyframesPanel
from src.features.composite.ui.keyframes_model import KEYFRAMES_ROW_H
from src.shared.keyframe import Keyframe, KeyframeTrack


CLIP_FRAMES = 100
CLIP_FPS = 24.0
CLIP_START = 1000  # comp clip 在时间线上的起始帧（全局坐标）


@pytest.fixture
def graph():
    g = NodeGraph()
    g.add_node(TransformNode())
    return g


@pytest.fixture
def panel(qapp, graph):
    p = KeyframesPanel()
    p.resize(1000, 300)
    p.set_node_graph(graph)
    p.set_frame_range(0, CLIP_FRAMES - 1, fps=CLIP_FPS)
    return p


def _wheel_at(view, x, y, delta=120, mods=Qt.KeyboardModifier.NoModifier):
    ev = QWheelEvent(QPointF(x, y), QPointF(x, y),
                     QPoint(0, 0), QPoint(0, delta),
                     Qt.MouseButton.NoButton, mods,
                     Qt.ScrollPhase.NoScrollPhase, False)
    view.wheelEvent(ev)


def _mouse(view, etype, x, y, button, buttons=Qt.MouseButton.NoButton):
    ev = QMouseEvent(etype, QPointF(x, y), button, buttons,
                     Qt.KeyboardModifier.NoModifier)
    view.mousePressEvent(ev) if etype == QEvent.Type.MouseButtonPress \
        else view.mouseMoveEvent(ev) if etype == QEvent.Type.MouseMove \
        else view.mouseReleaseEvent(ev)


def test_layer_group_values_and_keyframes_use_the_live_axis_tracks(qapp, monkeypatch):
    graph = NodeGraph()
    graph.add_node(OutputNode(node_id="out"))
    controller = LayerStackController(graph, layer_stack_version=2)
    layer_id = controller.create_generated_layer("solid", "Card", {})
    node_id = graph.get_layer(layer_id).root_node_id
    node = graph.get_node(node_id)
    panel = KeyframesPanel()
    panel.resize(1200, 360)
    panel.set_layer_stack_controller(controller)
    panel.set_layer_view_mode("layers")
    panel.set_node_graph(graph, selected_layer_id=layer_id)
    panel.set_value_provider(lambda _node_id, param: node.parameters.get(param))
    panel.show()
    try:
        qapp.processEvents()
        tree = panel._tree_view
        model = panel._model
        cols = tree._column_geometry()
        layer_y = tree._header_height + KEYFRAMES_ROW_H // 2
        QTest.mouseClick(tree, Qt.MouseButton.LeftButton,
                         pos=QPoint(cols["name"][0] + 8, layer_y))
        pair = next(row for row in model.visible_rows()
                    if row.component_params == ("scale_x", "scale_y"))
        assert not pair.expanded
        x_control = tree._value_controls[(node_id, "scale_x")]
        y_control = tree._value_controls[(node_id, "scale_y")]
        assert x_control.value() == y_control.value() == 100.0
        assert x_control.geometry().right() < y_control.geometry().left()
        assert x_control.y() == y_control.y()
        assert {row.component_params for row in model.visible_rows() if row.component_params} >= {
            ("pivot_x", "pivot_y"), ("translate_x", "translate_y"), ("scale_x", "scale_y"),
        }
        lock = tree._scale_buttons[node_id]
        assert lock.isChecked()
        assert lock.geometry().right() < x_control.geometry().left()
        lock.click()
        assert not lock.isChecked()
        panel.parameter_changed.connect(lambda _nid, name, value: node.set_parameter(name, value))
        x_control.setValue(125.0)
        assert node.parameters["scale_x"] == 1.25

        calls = []
        panel.track_changed.connect(lambda _nid, name, keyframes: calls.append((name, keyframes)))
        pair_y = layer_y + model.visible_rows().index(pair) * KEYFRAMES_ROW_H
        pair_cols = tree._row_geometry(pair, cols)
        assert pair_cols["keyframe"][1] == pair_cols["name"][0] < cols["name"][0]
        QTest.mouseClick(tree, Qt.MouseButton.LeftButton,
                         pos=QPoint(pair_cols["keyframe"][0] + 8, pair_y))
        assert [(name, keyframes[0]["value"]) for name, keyframes in calls] == [
            ("scale_x", 1.25), ("scale_y", 1.0)]

        pair_pos = QPoint(pair_cols["name"][0] + 8, pair_y)
        QTest.mouseClick(tree, Qt.MouseButton.LeftButton, pos=pair_pos)
        assert not pair.expanded

        def toggle_from_context_menu(pos):
            tree.contextMenuEvent(QContextMenuEvent(
                QContextMenuEvent.Reason.Mouse, pos, tree.mapToGlobal(pos)))

        def select_first_action(menu, _pos):
            action = menu.actions()[0]
            action.trigger()
            return action

        monkeypatch.setattr(QMenu, "exec", select_first_action)
        before_split = graph.to_dict()
        toggle_from_context_menu(pair_pos)
        assert graph.to_dict() == before_split
        assert [row.param_name for row in model.visible_rows() if row.parent_row_id == pair.row_id] == [
            "scale_x", "scale_y"]
        assert not any(row.row_id == pair.row_id for row in model.visible_rows())

        scale_track = KeyframeTrack(param_name="scale_x", min_val=0.01, max_val=100.0)
        scale_track.add_keyframe(Keyframe(0, 1.25))
        scale_track.add_keyframe(Keyframe(10, 1.50))
        node.set_keyframe_track("scale_x", scale_track)
        panel.set_node_graph(graph, selected_layer_id=layer_id)
        panel.set_current_frame(5)
        tree._value_controls[(node_id, "scale_x")].setValue(140.0)
        assert node.parameters["scale_x"] == 1.25
        assert calls[-1][0] == "scale_x"
        assert [keyframe["frame"] for keyframe in calls[-1][1]] == [0, 5, 10]

        x_row = next(row for row in model.visible_rows() if row.param_name == "scale_x")
        x_y = layer_y + model.visible_rows().index(x_row) * KEYFRAMES_ROW_H
        before_combine = graph.to_dict()
        toggle_from_context_menu(QPoint(tree._row_geometry(x_row, cols)["name"][0] + 8, x_y))
        assert graph.to_dict() == before_combine
        pair = next(row for row in model.visible_rows() if row.row_id == pair.row_id)
        rail = panel._timeline_view
        pair_y = layer_y + model.visible_rows().index(pair) * KEYFRAMES_ROW_H
        marker_x = round(panel._frame_mapper.frame_to_x(10))
        seeks = []
        panel.current_frame_changed.connect(seeks.append)
        QTest.mouseClick(rail, Qt.MouseButton.LeftButton, pos=QPoint(marker_x, pair_y))
        assert not pair.expanded
        assert seeks == [10]
    finally:
        panel.close()


def test_recursive_disclosure_expands_only_the_clicked_layer_branch(qapp):
    graph = NodeGraph()
    graph.add_node(OutputNode(node_id="out"))
    controller = LayerStackController(graph, layer_stack_version=2)
    sibling_id = controller.create_generated_layer("solid", "Other layer", {})
    layer_id = controller.create_generated_layer("solid", "Card", {})
    panel = KeyframesPanel()
    panel.resize(1200, 400)
    panel.set_layer_stack_controller(controller)
    panel.set_layer_view_mode("layers")
    panel.set_node_graph(graph, selected_layer_id=layer_id)
    panel.show()
    try:
        qapp.processEvents()
        tree = panel._tree_view
        model = panel._model
        name_x, last_name_x = tree._column_geometry()["name"]
        heading_y = tree._header_height + KEYFRAMES_ROW_H // 2
        root = next(row for row in model.visible_rows() if row.layer_id == layer_id)
        sibling = next(row for row in model.visible_rows() if row.layer_id == sibling_id)
        disclosures = []
        tree.layer_disclosure_requested.connect(disclosures.append)

        name_click = QPoint(min(last_name_x - 2, name_x + 55), heading_y)
        QTest.mouseClick(tree, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.ControlModifier, name_click)
        assert not root.expanded and not disclosures
        disclosure_click = QPoint(name_x + 8, heading_y)
        tree.mouseMoveEvent(QMouseEvent(
            QEvent.Type.MouseMove, QPointF(disclosure_click),
            Qt.MouseButton.NoButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))
        assert "Cmd" in tree.toolTip() or "Ctrl" in tree.toolTip()
        QTest.mouseClick(tree, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.ControlModifier, disclosure_click)
        assert root.expanded and not sibling.expanded
        assert disclosures == [layer_id]
        assert all(row.expanded for row in model.visible_rows()
                   if row.layer_id == layer_id and row.row_type == "group" and not row.component_params)
        assert any(row.component_params == ("scale_x", "scale_y") and not row.expanded
                   for row in model.visible_rows() if row.layer_id == layer_id)
        assert not any(row.param_name == "scale_x" for row in model.visible_rows())

        QTest.mouseClick(tree, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.ControlModifier, disclosure_click)
        assert not root.expanded
        QTest.mouseClick(tree, Qt.MouseButton.LeftButton,
                         pos=QPoint(name_x + 8, heading_y))
        assert root.expanded
        assert not any(row.param_name == "scale_x" for row in model.visible_rows())
    finally:
        panel.close()


def test_inline_layer_rename_commits_only_the_current_row(qapp):
    graph = NodeGraph()
    graph.add_node(OutputNode(node_id="out"))
    controller = LayerStackController(graph, layer_stack_version=2)
    other_id = controller.create_generated_layer("solid", "Other", {})
    layer_id = controller.create_generated_layer("solid", "Card", {})
    panel = KeyframesPanel()
    panel.resize(1200, 360)
    panel.set_layer_stack_controller(controller)
    panel.set_layer_view_mode("layers")
    panel.set_node_graph(graph, selected_layer_id=layer_id)
    panel.show()
    try:
        qapp.processEvents()
        tree = panel._tree_view
        edits = []
        tree.layer_name_edited.connect(lambda ident, name: edits.append((ident, name)))
        assert not tree.open_layer_name_editor(other_id)
        assert tree.open_layer_name_editor(layer_id)
        editor = tree._layer_name_editor
        assert editor.hasFocus() and editor.selectedText() == "Card"
        QTest.keyClicks(editor, "  New title  ")
        QTest.keyClick(editor, Qt.Key.Key_Return)
        assert edits == [(layer_id, "New title")]
        assert tree._layer_name_editor is None

        assert tree.open_layer_name_editor(layer_id)
        editor = tree._layer_name_editor
        editor.setText("Cancelled")
        QTest.keyClick(editor, Qt.Key.Key_Escape)
        assert tree._layer_name_editor is None

        assert tree.open_layer_name_editor(layer_id)
        tree._layer_name_editor.setText("Focus changed")
        panel._timeline_view.setFocus()
        qapp.processEvents()
        assert tree._layer_name_editor is None

        assert tree.open_layer_name_editor(layer_id)
        tree._layer_name_editor.setText("Selection moved")
        tree.set_selected_layer_id(other_id)
        assert tree._layer_name_editor is None

        tree.set_selected_layer_id(layer_id)
        assert tree.open_layer_name_editor(layer_id)
        tree._layer_name_editor.setText("Row rebuilt")
        panel.set_node_graph(graph, selected_layer_id=layer_id)
        assert tree._layer_name_editor is None
        assert edits == [(layer_id, "New title")]
    finally:
        panel.close()


@pytest.fixture
def layer_shortcuts_panel(qapp):
    graph = NodeGraph()
    graph.add_node(OutputNode(node_id="out"))
    controller = LayerStackController(graph, layer_stack_version=2)
    other = controller.create_generated_layer("solid", "Other", {})
    layer_id = controller.create_generated_layer("solid", "Card", {})
    root = graph.get_node(graph.get_layer(layer_id).root_node_id)
    assert controller.add_layer_effect(layer_id, "blur")
    root.set_parameter("scale_x", 1.4)
    root.set_expression("translate_y", "1")
    rotation = KeyframeTrack(param_name="rotation", min_val=-360.0, max_val=360.0)
    rotation.add_keyframe(Keyframe(10, 0.0))
    rotation.add_keyframe(Keyframe(30, 20.0))
    root.set_keyframe_track("rotation", rotation)
    panel = KeyframesPanel()
    panel.resize(1200, 400)
    panel.set_layer_stack_controller(controller)
    panel.set_layer_view_mode("layers")
    panel.set_node_graph(graph, selected_layer_id=layer_id)
    panel.set_frame_range(0, 99, fps=25.0)
    panel.show()
    qapp.setActiveWindow(panel)
    panel._tree_view.setFocus()
    qapp.processEvents()
    yield panel, graph, layer_id, other, root
    panel.close()


def test_layer_property_shortcuts_isolate_add_and_restore(layer_shortcuts_panel):
    panel, _, layer_id, other, _ = layer_shortcuts_panel
    tree = panel._tree_view
    model = panel._model

    def shown_params():
        return {row.param_name for row in model.rows if row.layer_id == layer_id and row.param_name}

    QTest.keyClick(tree, Qt.Key.Key_P)
    assert shown_params() == {"translate_x", "translate_y"}
    assert any(row.component_params == ("translate_x", "translate_y") for row in model.visible_rows())
    assert not any(row.row_id.endswith(":group:transform") for row in model.visible_rows())
    assert any(row.layer_id == other for row in model.visible_rows())
    QTest.keyClick(tree, Qt.Key.Key_S, Qt.KeyboardModifier.ShiftModifier)
    assert shown_params() == {"translate_x", "translate_y", "scale_x", "scale_y"}
    QTest.keyClick(tree, Qt.Key.Key_P, Qt.KeyboardModifier.ShiftModifier)
    assert shown_params() == {"scale_x", "scale_y"}

    QTest.keyClick(tree, Qt.Key.Key_U)
    assert shown_params() == {"rotation"}
    QTest.keyClick(tree, Qt.Key.Key_U)
    assert {"rotation", "scale_x", "translate_y"} <= shown_params()
    QTest.keyClick(tree, Qt.Key.Key_E)
    assert any(row.row_type == "node" and row.layer_id == layer_id for row in model.visible_rows())
    QTest.keyClick(tree, Qt.Key.Key_E)
    assert shown_params() == {"translate_y"}

    layer_y = tree._header_height + KEYFRAMES_ROW_H // 2
    disclosure = QPoint(tree._column_geometry()["name"][0] + 8, layer_y)
    QTest.mouseClick(tree, Qt.MouseButton.LeftButton, pos=disclosure)
    QTest.mouseClick(tree, Qt.MouseButton.LeftButton, pos=disclosure)
    assert any(row.row_id.endswith(":group:transform") for row in model.visible_rows())
    assert {"rotation", "translate_y", "scale_x"} <= shown_params()


def test_layer_navigation_keys_use_visible_or_selected_tracks(layer_shortcuts_panel):
    panel, _, _, _, _ = layer_shortcuts_panel
    tree = panel._tree_view
    panel.set_play_range(2, 90)
    panel.set_current_frame(5)
    QTest.keyClick(tree, Qt.Key.Key_K)
    assert panel._state.current_frame == 90
    panel.set_current_frame(5)
    QTest.keyClick(tree, Qt.Key.Key_P)
    QTest.keyClick(tree, Qt.Key.Key_K, Qt.KeyboardModifier.ShiftModifier)
    assert panel._state.current_frame == 10
    QTest.keyClick(tree, Qt.Key.Key_R)
    QTest.keyClick(tree, Qt.Key.Key_K)
    assert panel._state.current_frame == 30
    QTest.keyClick(tree, Qt.Key.Key_PageUp)
    assert panel._state.current_frame == 29
    QTest.keyClick(tree, Qt.Key.Key_PageDown, Qt.KeyboardModifier.ShiftModifier)
    assert panel._state.current_frame == 39


def test_layer_keyboard_editing_respects_focus_and_edit_target(layer_shortcuts_panel):
    panel, graph, layer_id, _, root = layer_shortcuts_panel
    tree = panel._tree_view
    QTest.keyClick(tree, Qt.Key.Key_Return)
    editor = tree._layer_name_editor
    assert editor is not None and editor.selectedText() == "Card"
    QTest.keyClicks(editor, "New layer")
    QTest.keyClick(editor, Qt.Key.Key_Return)
    assert graph.get_layer(layer_id).name == "New layer"

    changes = []
    panel.track_changed.connect(lambda nid, name, keys: changes.append((nid, name, keys)))
    panel.set_current_frame(12)
    QTest.keyClick(tree, Qt.Key.Key_P, Qt.KeyboardModifier.AltModifier)
    assert [(nid, name, keys[0]["frame"]) for nid, name, keys in changes] == [
        (root.id, "translate_x", 12), (root.id, "translate_y", 12)]

    panel._search_edit.setFocus()
    QTest.keyClicks(panel._search_edit, "press uuee")
    assert panel._search_edit.text() == "press uuee"
    assert panel._state.current_frame == 12
    panel._search_edit.clear()
    tree.setFocus()

    QTest.keyClick(tree, Qt.Key.Key_R)
    rail = panel._timeline_view
    row_id = f"param:{root.id}:rotation"
    rail._select_keyframe(row_id, 10, additive=False)
    QTest.keyClick(tree, Qt.Key.Key_F9)
    assert changes[-1][2][0]["interpolation"] == "ease_in_out"
    deleted_layers = []
    panel.layer_delete_requested.connect(deleted_layers.append)
    QTest.keyClick(tree, Qt.Key.Key_Backspace)
    assert changes[-1][1] == "rotation"
    assert len(changes[-1][2]) == 1 and not deleted_layers
    rail.clear_keyframe_selection()
    QTest.keyClick(tree, Qt.Key.Key_Backspace)
    assert deleted_layers == [layer_id]


def test_layer_time_ruler_adapts_to_zoom(layer_shortcuts_panel):
    panel, _, _, _, _ = layer_shortcuts_panel
    view = panel._timeline_view
    mapper = panel._frame_mapper
    assert view._display_mode() == "time"
    mapper.set_scroll(0)
    mapper.set_zoom(1.5)
    labels = [(x, text) for x, text in view._ruler_ticks(200) if text]
    assert labels == [(0, "00s"), (75, "02s")]

    mapper.set_zoom(48)
    labels = [(x, text) for x, text in view._ruler_ticks(140) if text]
    assert labels == [(0, "00f"), (48, "01f"), (96, "02f")]


def test_layer_time_ruler_uses_actual_fps_and_supports_negative_frames(layer_shortcuts_panel):
    panel, _, _, _, _ = layer_shortcuts_panel
    view = panel._timeline_view
    mapper = panel._frame_mapper
    fps = 24000 / 1001
    view.set_display("time", fps)
    view.set_composition_range(59000, 92000)
    mapper.set_zoom(0.06)
    mapper.set_scroll(-59000 * mapper.pixels_per_frame)
    labels = [(x, text) for x, text in view._ruler_ticks(300) if text]
    assert labels[0][1] == "42:00"
    assert labels[0][0] == pytest.approx(mapper.frame_to_x(42 * 60 * fps))

    view.set_composition_range(-2, 10)
    mapper.set_zoom(48)
    mapper.set_scroll(96)
    labels = [(x, text) for x, text in view._ruler_ticks(120) if text]
    assert labels == [(0, "−02f"), (48, "−01f"), (96, "00f")]


def test_option_shortcut_keys_the_text_layer_transform_through_filters(layer_shortcuts_panel):
    panel, graph, _, _, _ = layer_shortcuts_panel
    controller = panel._layer_stack_controller
    layer_id = controller.create_generated_layer("text", "Title", {"text": "Title"})
    root_id = graph.get_layer(layer_id).root_node_id
    panel.set_node_graph(graph, selected_layer_id=layer_id)
    panel._set_filter_mode("animated")
    edits = []
    panel.track_changed.connect(lambda nid, name, keys: edits.append((nid, name, keys)))
    panel.set_current_frame(12)

    event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_S, Qt.KeyboardModifier.AltModifier, "ß")
    assert panel.eventFilter(panel._tree_view, event)
    assert [(node_id, name, keys[0]["value"]) for node_id, name, keys in edits] == [
        (root_id, "scale_x", 1.0), (root_id, "scale_y", 1.0)]


def test_property_projection_preserves_visible_keyframe_selection(layer_shortcuts_panel):
    panel, _, layer_id, _, root = layer_shortcuts_panel
    tree = panel._tree_view
    rail = panel._timeline_view
    QTest.keyClick(tree, Qt.Key.Key_R)
    row_id = f"param:{root.id}:rotation"
    rail._select_keyframe(row_id, 10)
    QTest.keyClick(tree, Qt.Key.Key_S, Qt.KeyboardModifier.ShiftModifier)
    assert rail.selected_keyframes() == [(row_id, 10)]

    layer_deletes = []
    panel.layer_delete_requested.connect(layer_deletes.append)
    QTest.keyClick(tree, Qt.Key.Key_P)
    assert rail.selected_keyframes() == []
    QTest.keyClick(tree, Qt.Key.Key_Backspace)
    assert layer_deletes == []
    panel._on_layer_selected(layer_id)
    QTest.keyClick(tree, Qt.Key.Key_Backspace)
    assert layer_deletes == [layer_id]


def test_layer_parent_menu_is_available_directly_on_the_row(layer_shortcuts_panel, monkeypatch):
    panel, graph, layer_id, other, _ = layer_shortcuts_panel
    tree = panel._tree_view
    y = tree._header_height + KEYFRAMES_ROW_H // 2
    x = tree._column_geometry()["value"][0] + 16
    QTest.mouseClick(tree, Qt.MouseButton.LeftButton, pos=QPoint(x, y))
    menu = panel.findChild(QMenu, "KeyframesLayerParentMenu")
    assert menu is not None
    next(action for action in menu.actions() if action.text() == "Other").trigger()
    assert graph.get_layer(layer_id).parent_layer_id == other
    row = next(row for row in panel._model.rows if row.layer_id == layer_id and row.row_type == "layer")
    assert row.parent_layer_name == "Other"
    menu.close()
    painted = []
    monkeypatch.setattr(tree, "_draw_layer_parent", lambda _painter, _span, _y, row: painted.append(row))
    assert not tree.grab().isNull()
    assert row in painted


# ── 1. 滚轮缩放 ─────────────────────────────────────────────────────────

def test_plain_wheel_zooms_timeline_view(panel):
    view = panel._timeline_view
    before = panel._frame_mapper.pixels_per_frame
    _wheel_at(view, 200, 100, delta=120)
    assert panel._frame_mapper.pixels_per_frame > before, "滚轮应放大刻度"
    _wheel_at(view, 200, 100, delta=-120)
    _wheel_at(view, 200, 100, delta=-120)
    assert panel._frame_mapper.pixels_per_frame < before, "反向滚轮应缩小"


def test_wheel_zoom_anchored_at_cursor(panel):
    """缩放时鼠标下的帧保持不动（逐帧精调的关键）。"""
    view = panel._timeline_view
    fm = panel._frame_mapper
    x = 250.0
    anchor_frame = fm.x_to_frame(x)
    _wheel_at(view, x, 100, delta=120)
    assert fm.x_to_frame(x) == pytest.approx(anchor_frame, abs=0.01)


def test_wheel_zoom_clamped_for_frame_precision(panel):
    """可放大到足够逐帧操作的刻度（每帧几十像素）。"""
    view = panel._timeline_view
    for _ in range(80):
        _wheel_at(view, 100, 100, delta=120)
    assert panel._frame_mapper.pixels_per_frame >= 32.0
    assert panel._frame_mapper.pixels_per_frame <= 256.0


def test_shift_wheel_pans_horizontally(panel):
    view = panel._timeline_view
    before = panel._frame_mapper.scroll_offset
    _wheel_at(view, 100, 100, delta=120,
              mods=Qt.KeyboardModifier.ShiftModifier)
    assert panel._frame_mapper.scroll_offset != before


def test_alt_wheel_bubbles_for_vertical_scroll(panel):
    """Alt+滚轮不在轨道侧消费 → 冒泡给面板做垂直滚动。"""
    view = panel._timeline_view
    ev = QWheelEvent(QPointF(100, 100), QPointF(100, 100),
                     QPoint(0, 0), QPoint(0, -120),
                     Qt.MouseButton.NoButton, Qt.KeyboardModifier.AltModifier,
                     Qt.ScrollPhase.NoScrollPhase, False)
    view.wheelEvent(ev)
    assert not ev.isAccepted()


# ── 2. 中键平移 ─────────────────────────────────────────────────────────

def test_middle_button_drag_pans(panel):
    view = panel._timeline_view
    before = panel._frame_mapper.scroll_offset
    _mouse(view, QEvent.Type.MouseButtonPress, 300, 100,
           Qt.MouseButton.MiddleButton, Qt.MouseButton.MiddleButton)
    _mouse(view, QEvent.Type.MouseMove, 380, 100,
           Qt.MouseButton.NoButton, Qt.MouseButton.MiddleButton)
    _mouse(view, QEvent.Type.MouseButtonRelease, 380, 100,
           Qt.MouseButton.MiddleButton)
    delta = panel._frame_mapper.scroll_offset - before
    assert delta == pytest.approx(80.0), "中键拖拽应 1:1 平移刻度"


def test_middle_pan_does_not_move_playhead(panel):
    frames: list[int] = []
    panel.current_frame_changed.connect(frames.append)
    view = panel._timeline_view
    _mouse(view, QEvent.Type.MouseButtonPress, 300, 100,
           Qt.MouseButton.MiddleButton, Qt.MouseButton.MiddleButton)
    _mouse(view, QEvent.Type.MouseMove, 400, 100,
           Qt.MouseButton.NoButton, Qt.MouseButton.MiddleButton)
    _mouse(view, QEvent.Type.MouseButtonRelease, 400, 100,
           Qt.MouseButton.MiddleButton)
    assert frames == [], "中键平移绝不能触发 seek"


# ── 3. 范围播放条 + I/O 键 ──────────────────────────────────────────────

def test_io_keys_set_play_range_at_playhead(panel):
    ranges: list[tuple[int, int]] = []
    panel.play_range_changed.connect(lambda i, o: ranges.append((i, o)))

    panel.set_current_frame(20)
    panel.set_play_in_at_playhead()      # I
    panel.set_current_frame(60)
    panel.set_play_out_at_playhead()     # O

    assert panel.effective_play_range() == (20, 60)
    assert (20, 60) in ranges
    # 视图上的工作区条同步
    assert panel._timeline_view.play_range() == (20, 60)
    # 底部标签显示 In/Out
    assert "20-60" in panel._range_label.text()


def test_shortcut_i_o_registered(panel):
    """I/O 快捷键确实注册在面板上（WidgetWithChildrenShortcut）。"""
    assert panel._shortcut_set_in.key().toString() == "I"
    assert panel._shortcut_set_out.key().toString() == "O"


def test_composite_shift_z_fits_only_active_composition(qapp):
    """Composite 的 Shift+Z 只在本页有打开合成时调用 Fit。"""
    page = CompositePage()
    calls: list[bool] = []
    page._keyframes_panel = SimpleNamespace(
        fit_time_range=lambda: calls.append(True),
    )

    page._is_active = True
    page.set_composition_active(True)

    assert page._fit_composition_shortcut.key().toString() == "Shift+Z"
    assert page._fit_composition_shortcut.isEnabled()
    page._fit_keyframes_to_composition()
    assert calls == [True]

    page._is_active = False
    page._sync_fit_composition_shortcut()
    assert not page._fit_composition_shortcut.isEnabled()


def test_internal_playback_loops_inside_work_area(panel):
    frames: list[int] = []
    panel.current_frame_changed.connect(frames.append)

    panel.set_play_range(10, 13)
    panel.set_current_frame(10)
    panel._internal_set_playing(True)
    for _ in range(6):
        panel._on_internal_play_tick()
    panel._internal_set_playing(False)

    # 10 → 11,12,13,10,11,12 —— 只在 In/Out 内循环
    assert frames == [11, 12, 13, 10, 11, 12]


def test_workarea_band_drag_updates_range(panel):
    """拖拽标尺上的工作区 Out 边缘 → 范围实时更新。"""
    view = panel._timeline_view
    fm = panel._frame_mapper
    panel.set_play_range(20, 60)

    ranges: list[tuple[int, int]] = []
    panel.play_range_changed.connect(lambda i, o: ranges.append((i, o)))

    x_out = fm.frame_to_x(61)  # end+1 边缘
    _mouse(view, QEvent.Type.MouseButtonPress, x_out, 2,
           Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
    _mouse(view, QEvent.Type.MouseMove, fm.frame_to_x(71), 2,
           Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
    _mouse(view, QEvent.Type.MouseButtonRelease, fm.frame_to_x(71), 2,
           Qt.MouseButton.LeftButton)

    assert panel.effective_play_range() == (20, 71)
    assert ranges[-1] == (20, 71)


def test_clear_play_range_restores_full_clip(panel):
    ranges: list[tuple[int, int]] = []
    panel.play_range_changed.connect(lambda i, o: ranges.append((i, o)))
    panel.set_play_range(20, 60)
    panel.clear_play_range()
    assert panel._timeline_view.play_range() is None
    assert panel.effective_play_range() == (0, CLIP_FRAMES - 1)
    assert ranges[-1] == (0, CLIP_FRAMES - 1)


def test_page_forwards_play_range_to_transport(qapp):
    page = CompositePage()
    page._composite_duration_frames = CLIP_FRAMES
    panel = KeyframesPanel()
    panel.set_frame_range(0, CLIP_FRAMES - 1, fps=CLIP_FPS)
    page.set_keyframes_panel(panel)

    class FakeController:
        is_playing = False
        playback_range = None

        def play(self):
            pass

        def pause(self):
            pass

        def set_playback_range(self, s, e):
            self.playback_range = (s, e)

    ctrl = FakeController()
    page.set_keyframes_transport(
        _KeyframesTimelineTransport(ctrl, CLIP_START, CLIP_FRAMES))

    panel.set_play_range(10, 40)

    # clip 本地帧 → 时间线全局帧
    assert ctrl.playback_range == (CLIP_START + 10, CLIP_START + 40)
    # play() 使用工作区范围而不是整个 clip
    page._keyframes_transport.play()
    assert ctrl.playback_range == (CLIP_START + 10, CLIP_START + 40)


def test_no_transport_play_range_falls_back_to_comp_timer(qapp):
    page = CompositePage()
    page._composite_duration_frames = CLIP_FRAMES
    panel = KeyframesPanel()
    panel.set_frame_range(0, CLIP_FRAMES - 1, fps=CLIP_FPS)
    page.set_keyframes_panel(panel)
    page.set_keyframes_transport(None)

    panel.set_play_range(5, 25)
    assert page._comp_in_frame == 5
    assert page._comp_out_frame == 25


# ── 4. 素材图层条 ───────────────────────────────────────────────────────

def test_every_node_shows_layer_bar(panel, graph):
    """无 time_range 的节点也显示横跨整个合成的图层条（AE 风格）。"""
    node_id = panel._model.rows[0].node_id
    track = panel._model.track_for(f"node:{node_id}")
    assert track is not None and track.range_bar is not None
    assert track.range_bar.default is True
    assert track.range_bar.start_frame == 0
    assert track.range_bar.end_frame == CLIP_FRAMES - 1


def test_trim_default_layer_bar_writes_time_range(panel, graph):
    """拖拽默认图层条边缘 → 节点获得显式 time_range（控制播放范围）。"""
    node_id = panel._model.rows[0].node_id
    node = graph.get_node(node_id)
    assert node.time_range is None

    emitted: list[tuple[str, dict]] = []
    panel.range_changed.connect(lambda nid, tr: emitted.append((nid, tr)))

    # 模拟视图侧的提交（拖拽 Out 边缘到 70）
    track = panel._model.track_for(f"node:{node_id}")
    track.range_bar.start_frame = 10
    track.range_bar.end_frame = 70
    view = panel._timeline_view
    view._drag_mode = "range_edge"
    view._drag_row_id = f"node:{node_id}"
    view._commit_drag_edit()

    assert emitted == [(node_id, {
        "comp_start": 10, "comp_end": 70, "enabled": True})]


def test_page_merges_time_range_preserving_source_keys(qapp, graph):
    """CompositePage 合并写入：trim 不丢 source_in/source_out。"""
    page = CompositePage()
    page._node_graph = graph
    node = graph.nodes[next(iter(graph.nodes))]
    node.time_range = {"source_in": 5, "source_out": 200, "offset": 0}

    page._on_keyframes_panel_time_range_changed(
        node.id, {"comp_start": 10, "comp_end": 70, "enabled": True})

    assert node.time_range["comp_start"] == 10
    assert node.time_range["comp_end"] == 70
    assert node.time_range["enabled"] is True
    assert node.time_range["source_in"] == 5, "原有 source_in 必须保留"
    assert node.time_range["source_out"] == 200


def test_explicit_time_range_bar_not_default(qapp):
    """已有 time_range 的节点：图层条反映显式范围且 default=False。"""
    g = NodeGraph()
    t = TransformNode()
    t.time_range = {"comp_start": 3, "comp_end": 40, "enabled": True}
    g.add_node(t)
    p = KeyframesPanel()
    p.resize(800, 300)
    p.set_frame_range(0, CLIP_FRAMES - 1, fps=CLIP_FPS)
    p.set_node_graph(g)

    track = p._model.track_for(f"node:{t.id}")
    assert track.range_bar.default is False
    assert track.range_bar.start_frame == 3
    assert track.range_bar.end_frame == 40


# ── 6. 刻度条（自适应刻度 + 帧数/时间码双模式） ─────────────────────

def test_tick_steps_adapt_to_zoom(panel):
    """任意缩放级别下，主刻度间距足够宽，标签永不重叠。"""
    view = panel._timeline_view
    fm = panel._frame_mapper
    for ppf in (0.05, 0.2, 1.0, 2.0, 8.0, 32.0, 256.0):
        fm.set_zoom(ppf)
        minor, major = view._tick_steps()
        assert 1 <= minor <= major, f"ppf={ppf}: 步长非法"
        if major > 1:
            assert major % minor == 0, f"ppf={ppf}: 次刻度需整除主刻度"
        # 主刻度(带标签)像素间距必须 >= 最小间隔
        assert major * ppf >= 50.0 - 1e-6, \
            f"ppf={ppf}: 主刻度间距 {major * ppf:.1f}px 过密"


def test_tick_steps_follow_125_series(panel):
    """主刻度步长应为 1/2/5×10^k（剪辑页风格的整齐刻度）。"""
    view = panel._timeline_view
    fm = panel._frame_mapper
    for ppf in (0.1, 0.7, 3.0, 20.0):
        fm.set_zoom(ppf)
        _minor, major = view._tick_steps()
        # 剥离 10^k 后尾数只能是 1/2/5
        m = major
        while m % 10 == 0:
            m //= 10
        assert m in (1, 2, 5), f"ppf={ppf}: major={major} 不在 1-2-5 系列"


def test_ruler_labels_frames_and_timecode(panel):
    """刻度标签支持帧数 / 时间码两种显示方式。"""
    view = panel._timeline_view
    view.set_display("frame", CLIP_FPS)
    assert view._ruler_label(96) == "96"

    view.set_display("timecode", CLIP_FPS)
    assert view._ruler_label(0) == "00:00:00:00"
    assert view._ruler_label(24) == "00:00:01:00"      # 24fps → 1 秒
    assert view._ruler_label(24 * 60 + 12) == "00:01:00:12"


def test_timecode_mode_widens_tick_spacing(panel):
    """时间码标签更宽，主刻度间距要求也更大，避免重叠。"""
    view = panel._timeline_view
    fm = panel._frame_mapper
    fm.set_zoom(2.0)
    view.set_display("frame", CLIP_FPS)
    _m1, major_frame = view._tick_steps()
    view.set_display("timecode", CLIP_FPS)
    _m2, major_tc = view._tick_steps()
    assert major_tc >= major_frame


def test_context_menu_switch_syncs_footer_combo(panel):
    """时间轴右键切换刻度模式后，底部单位下拉框同步更新。"""
    view = panel._timeline_view
    view._set_display_mode("timecode")
    assert panel._state.display_mode == "timecode"
    assert panel._time_unit_combo.currentIndex() == 3
    view._set_display_mode("frame")
    assert panel._time_unit_combo.currentIndex() == 1


def test_footer_combo_switches_ruler_mode(panel):
    """底部下拉框选 Timecode 后，刻度标签跟随切换。"""
    panel._time_unit_combo.setCurrentIndex(3)  # Timecode
    view = panel._timeline_view
    assert view._display_mode() == "timecode"
    assert view._ruler_label(48) == "00:00:02:00"
    panel._time_unit_combo.setCurrentIndex(1)  # Frame
    assert view._display_mode() == "frame"
    assert view._ruler_label(48) == "48"


def test_layer_ruler_ticks_do_not_continue_into_the_tracks(layer_shortcuts_panel):
    panel, _, _, _, _ = layer_shortcuts_panel
    view = panel._timeline_view
    mapper = panel._frame_mapper
    mapper.set_zoom(view.width() / 100)
    mapper.set_scroll(0)
    tick_x = next(round(x) for x, label in view._ruler_ticks(view.width()) if label and x > 12)
    image = view.grab().toImage()
    ratio = image.devicePixelRatio()
    y = view.height() - 12
    assert image.pixelColor(round(tick_x * ratio), round(y * ratio)) == image.pixelColor(
        round((tick_x + 4) * ratio), round(y * ratio))
