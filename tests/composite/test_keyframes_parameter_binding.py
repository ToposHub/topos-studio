"""AE-style Keyframes property controls and graph binding contracts."""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QMouseEvent

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.blur_node import BlurNode
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.ui.keyframes_panel import KeyframesPanel
from src.shared.keyframe import Keyframe, KeyframeTrack


def _graph() -> tuple[NodeGraph, BlurNode, TransformNode]:
    graph = NodeGraph()
    blur = BlurNode(node_id="blur0001")
    transform = TransformNode(node_id="trans001")
    graph.add_node(blur)
    graph.add_node(transform)
    return graph, blur, transform


def test_parameter_binding_is_evaluated_and_serialized() -> None:
    graph, blur, transform = _graph()

    ok, error = graph.set_parameter_binding(
        transform.id, "translate_x", blur.id, "radius")
    assert (ok, error) == (True, None)
    assert graph.get_parameter_binding(transform.id, "translate_x") == {
        "node_id": blur.id,
        "param_name": "radius",
    }
    assert graph.resolve_all_expressions()[transform.id]["translate_x"] == \
        blur.parameters["radius"]

    restored = NodeGraph.from_dict(graph.to_dict())
    assert restored.get_parameter_binding(transform.id, "translate_x") == {
        "node_id": blur.id,
        "param_name": "radius",
    }


def test_parameter_binding_rejects_dependency_cycles() -> None:
    graph, blur, transform = _graph()
    assert graph.set_parameter_binding(
        transform.id, "translate_x", blur.id, "radius")[0]
    assert graph.set_parameter_binding(
        blur.id, "radius", transform.id, "translate_x") == (
            False, "Circular parameter binding detected")


def test_parent_binding_rejects_hierarchy_cycles() -> None:
    graph, blur, transform = _graph()
    assert graph.set_node_parent(transform.id, blur.id) == (True, None)
    assert graph.set_node_parent(blur.id, transform.id) == (
        False, "Parent cycle detected")


@pytest.mark.usefixtures("qapp")
def test_parent_pick_whip_is_limited_to_transform_capable_nodes(qapp) -> None:
    graph, blur, transform = _graph()
    panel = KeyframesPanel()
    panel.resize(800, 300)
    panel.set_node_graph(graph)
    panel.show()
    qapp.processEvents()

    assert panel._parent_binding_supported(transform.id) is True
    assert panel._parent_binding_supported(blur.id) is False

    tree = panel._tree_view
    rows = tree._model.visible_rows()
    transform_index = next(i for i, row in enumerate(rows)
                           if row.node_id == transform.id
                           and row.row_type == "node")
    blur_index = next(i for i, row in enumerate(rows)
                      if row.node_id == blur.id and row.row_type == "node")
    bind_x = sum(tree._column_geometry()["bind"]) / 2
    transform_y = 30 + transform_index * 24 + 12
    blur_y = 30 + blur_index * 24 + 12

    # A non-transform node has no parent pick-whip source at all.
    _mouse(tree, QMouseEvent.Type.MouseButtonPress, bind_x, blur_y,
           Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
    assert tree._binding_drag is None

    # It also cannot become a highlighted parent target while dragging from a
    # supported child. The line may remain visible, but the target tint must
    # be absent for this row.
    received = []
    tree.parent_binding_dragged.connect(
        lambda child, parent: received.append((child, parent)))
    _mouse(tree, QMouseEvent.Type.MouseButtonPress, bind_x, transform_y,
           Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
    _mouse(tree, QMouseEvent.Type.MouseMove, bind_x, blur_y,
           Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
    assert tree._binding_target_supported(rows[blur_index]) is False
    _mouse(tree, QMouseEvent.Type.MouseButtonRelease, bind_x, blur_y,
           Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)
    assert received == []


@pytest.mark.usefixtures("qapp")
def test_keyframes_panel_property_columns_toggle_keyframe_and_edit_value(
        qapp) -> None:
    graph, _blur, transform = _graph()
    panel = KeyframesPanel()
    panel.resize(800, 300)
    panel.set_node_graph(graph)
    panel.set_value_provider(
        lambda _node_id, _param_name: 12.0)

    panel._tree_view.keyframe_toggle_requested.emit(
        transform.id, "translate_x")
    visual = panel._model.track_for(
        f"param:{transform.id}:translate_x")
    assert visual is not None
    assert [kf.frame for kf in visual.keyframes] == [0]

    changed = []
    panel.parameter_changed.connect(
        lambda node_id, param_name, value:
        changed.append((node_id, param_name, value)))
    panel._on_property_value_changed(transform.id, "translate_y", "7.5")
    assert changed == [(transform.id, "translate_y", 7.5)]


def _mouse(tree, event_type, x, y, button, buttons):
    event = QMouseEvent(
        event_type, QPointF(float(x), float(y)), button, buttons,
        Qt.KeyboardModifier.NoModifier)
    if event_type == QMouseEvent.Type.MouseButtonPress:
        tree.mousePressEvent(event)
    elif event_type == QMouseEvent.Type.MouseMove:
        tree.mouseMoveEvent(event)
    else:
        tree.mouseReleaseEvent(event)


@pytest.mark.usefixtures("qapp")
def test_value_column_horizontal_drag_emits_step_adjusted_value(qapp) -> None:
    graph, _blur, transform = _graph()
    panel = KeyframesPanel()
    panel.resize(800, 300)
    panel.set_node_graph(graph)
    panel.set_value_provider(lambda _node_id, _param_name: 0.0)
    changed = []
    panel.parameter_changed.connect(
        lambda node_id, param_name, value:
        changed.append((node_id, param_name, value)))

    tree = panel._tree_view
    x0, x1 = tree._column_geometry()["value"]
    row_index = next(i for i, row in enumerate(tree._model.visible_rows())
                     if row.node_id == transform.id
                     and row.param_name == "scale_x")
    y = 30 + row_index * 24 + 12
    x = (x0 + x1) / 2
    _mouse(tree, QMouseEvent.Type.MouseButtonPress, x, y,
           Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
    _mouse(tree, QMouseEvent.Type.MouseMove, x + 10, y,
           Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
    _mouse(tree, QMouseEvent.Type.MouseButtonRelease, x + 10, y,
           Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)
    assert changed[-1] == (transform.id, "scale_x", 1.1)


@pytest.mark.usefixtures("qapp")
def test_value_column_drag_updates_keyframe_under_playhead(qapp) -> None:
    graph, _blur, transform = _graph()
    track = KeyframeTrack("scale_x", min_val=0.01, max_val=5.0)
    track.add_keyframe(Keyframe(frame=0, value=1.0))
    transform.set_keyframe_track("scale_x", track)

    panel = KeyframesPanel()
    panel.resize(800, 300)
    panel.set_node_graph(graph)
    panel.set_current_frame(0)
    panel.set_value_provider(
        lambda _node_id, param_name: "1" if param_name == "scale_x" else "0")
    tree = panel._tree_view
    value_x = sum(tree._column_geometry()["value"]) / 2
    row_index = next(i for i, row in enumerate(tree._model.visible_rows())
                     if row.node_id == transform.id
                     and row.param_name == "scale_x")
    y = 30 + row_index * 24 + 12
    _mouse(tree, QMouseEvent.Type.MouseButtonPress, value_x, y,
           Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
    _mouse(tree, QMouseEvent.Type.MouseMove, value_x + 10, y,
           Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
    _mouse(tree, QMouseEvent.Type.MouseButtonRelease, value_x + 10, y,
           Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)

    visual = panel._model.track_for(
        f"param:{transform.id}:scale_x")
    assert visual is not None
    assert visual.keyframes[0].value == pytest.approx(1.1)
    assert panel._keyframe_status_for(transform.id, "scale_x") == {
        "animated": True, "at_current": True}


@pytest.mark.usefixtures("qapp")
def test_value_cell_uses_properties_drag_value_control(qapp) -> None:
    graph, blur, _transform = _graph()
    panel = KeyframesPanel()
    panel.resize(1000, 300)
    panel.set_node_graph(graph)
    panel.show()
    qapp.processEvents()
    changed = []
    panel.parameter_changed.connect(
        lambda node_id, param_name, value:
        changed.append((node_id, param_name, value)))

    control = panel._tree_view._value_controls[(blur.id, "radius_x")]
    press = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress, QPointF(30, 9),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier)
    move = QMouseEvent(
        QMouseEvent.Type.MouseMove, QPointF(40, 9),
        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier)
    release = QMouseEvent(
        QMouseEvent.Type.MouseButtonRelease, QPointF(40, 9),
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier)
    control.mousePressEvent(press)
    control.mouseMoveEvent(move)
    control.mouseReleaseEvent(release)

    assert control.objectName() == "KeyframesSharedDragValue"
    assert changed[-1][0:2] == (blur.id, "radius_x")
    assert changed[-1][2] != 5.0


@pytest.mark.usefixtures("qapp")
def test_collapsing_node_hides_value_controls_immediately(qapp) -> None:
    """Collapsed properties must not leave stale DragValue widgets behind."""
    graph, blur, _transform = _graph()
    panel = KeyframesPanel()
    panel.resize(1000, 300)
    panel.set_node_graph(graph)
    panel.show()
    qapp.processEvents()

    control = panel._tree_view._value_controls[(blur.id, "radius_x")]
    assert control.isVisible()

    panel._model.set_expanded(blur.id, False)
    panel._on_expand_changed(blur.id, False)
    qapp.processEvents()

    assert not control.isVisible()


@pytest.mark.usefixtures("qapp")
def test_pick_whip_drag_reports_parameter_source(qapp) -> None:
    graph, blur, transform = _graph()
    panel = KeyframesPanel()
    panel.resize(800, 300)
    panel.set_node_graph(graph)
    received = []
    panel.parameter_binding_changed.connect(
        lambda *args: received.append(args))

    tree = panel._tree_view
    bind_x = tree._column_geometry()["bind"]
    rows = tree._model.visible_rows()
    target_index = next(i for i, row in enumerate(rows)
                        if row.node_id == transform.id
                        and row.param_name == "translate_x")
    source_index = next(i for i, row in enumerate(rows)
                        if row.node_id == blur.id
                        and row.param_name == "radius")
    x = (bind_x[0] + bind_x[1]) / 2
    y0 = 30 + target_index * 24 + 12
    y1 = 30 + source_index * 24 + 12
    _mouse(tree, QMouseEvent.Type.MouseButtonPress, x, y0,
           Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
    _mouse(tree, QMouseEvent.Type.MouseMove, x, y1,
           Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
    _mouse(tree, QMouseEvent.Type.MouseButtonRelease, x, y1,
           Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)
    assert received == [(transform.id, "translate_x", blur.id, "radius")]
