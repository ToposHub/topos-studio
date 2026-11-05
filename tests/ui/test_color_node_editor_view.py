"""Color-node presentation interactions through actual Qt input events.

These tests instantiate no engine/project/GPU objects.  Draft edits and emitted
intents are the observable result until the backend bridge is implemented.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt6.QtGui import QAction, QMouseEvent
from PyQt6.QtWidgets import QApplication, QMenu

from src.features.color.ui.node_editor_items import ConnectionItem
from src.features.color.ui.node_editor_state import ConnectionDraft, NodeKind
from src.features.color.ui.node_editor_view import ColorNodeView


@pytest.fixture
def view(qapp: QApplication, qtbot: Any) -> Iterator[ColorNodeView]:
    canvas = ColorNodeView()
    canvas.resize(960, 480)
    qtbot.addWidget(canvas)
    canvas.show()
    canvas.setFocus()
    qapp.processEvents()
    yield canvas
    canvas.close()


def _point(view: ColorNodeView, node_id: str, port: str = "") -> QPoint:
    card = view.cards[node_id]
    position = card.ports[port].scenePos() if port else card.mapToScene(card.body_rect().center())
    return view.mapFromScene(position)


def _mouse(
    view: ColorNodeView,
    kind: QEvent.Type,
    position: QPoint,
    button: Qt.MouseButton,
    buttons: Qt.MouseButton,
    modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
) -> None:
    event = QMouseEvent(
        kind, QPointF(position), QPointF(view.viewport().mapToGlobal(position)),
        button, buttons, modifiers,
    )
    QApplication.sendEvent(view.viewport(), event)


def _drag(view: ColorNodeView, start: QPoint, end: QPoint) -> None:
    _mouse(view, QEvent.Type.MouseButtonPress, start,
           Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
    # Several move samples exercise the QGraphicsScene mouse grabber.
    for step in range(1, 5):
        point = start + (end - start) * step / 4
        _mouse(view, QEvent.Type.MouseMove, point,
               Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
    _mouse(view, QEvent.Type.MouseButtonRelease, end,
           Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)


def _action(menu: QMenu, name: str) -> QAction:
    action = menu.findChild(QAction, "node_action_" + name)
    assert action is not None, name
    return action


def test_serial_shortcut_and_undo_restore_topology(view: ColorNodeView, qtbot: Any) -> None:
    original = view.state.snapshot()
    intents: list[str] = []
    view.edit_requested.connect(lambda action, payload: intents.append(action))
    qtbot.keyClick(view, Qt.Key.Key_S, modifier=Qt.KeyboardModifier.AltModifier)
    assert len(view.state.graph.nodes) == len(original.nodes) + 1
    assert intents[-1] == "add_serial"
    qtbot.keyClick(view, Qt.Key.Key_Z, modifier=Qt.KeyboardModifier.ControlModifier)
    assert view.state.graph.connections == original.connections
    assert set(view.state.graph.nodes) == set(original.nodes)


def test_drag_commits_one_move_and_undo_restores_position(view: ColorNodeView, qtbot: Any) -> None:
    node_id = view.state.graph.active_id
    original = (view.state.graph.nodes[node_id].x, view.state.graph.nodes[node_id].y)
    intents: list[str] = []
    view.edit_requested.connect(lambda action, payload: intents.append(action))
    start = _point(view, node_id)
    _drag(view, start, start + QPoint(25, 65))
    moved = view.state.graph.nodes[node_id]
    assert (moved.x, moved.y) != original
    assert intents == ["move_nodes"]
    qtbot.keyClick(view, Qt.Key.Key_Z, modifier=Qt.KeyboardModifier.ControlModifier)
    restored = view.state.graph.nodes[node_id]
    assert (restored.x, restored.y) == original
    assert not view.state.can_undo


def test_drag_key_ports_creates_key_link(view: ColorNodeView) -> None:
    first = view.state.graph.active_id
    second = view.state.add_serial()
    view.rebuild()
    connection = ConnectionDraft(first, "key_out", second, "key_in")
    _drag(view, _point(view, first, "key_out"), _point(view, second, "key_in"))
    assert connection in view.state.graph.connections
    assert any(link.connection == connection and link.data_type == "key" for link in view.links)


def test_drag_incompatible_port_preserves_existing_image_input(view: ColorNodeView) -> None:
    first = view.state.graph.active_id
    second = view.state.add_serial()
    view.rebuild()
    original = list(view.state.graph.connections)
    _drag(view, _point(view, first, "key_out"), _point(view, second, "image_in"))
    assert view.state.graph.connections == original


def test_drag_cycle_preserves_original_input(view: ColorNodeView) -> None:
    first = view.state.graph.active_id
    second = view.state.add_serial()
    view.rebuild()
    original = list(view.state.graph.connections)
    _drag(view, _point(view, second, "image_out"), _point(view, first, "image_in"))
    assert view.state.graph.connections == original


def test_input_drag_to_blank_disconnects_and_undo_restores(view: ColorNodeView, qtbot: Any) -> None:
    node_id = view.state.graph.active_id
    incoming = next(edge for edge in view.state.graph.connections if edge.dst_node == node_id)
    start = _point(view, node_id, "image_in")
    _drag(view, start, start + QPoint(-15, -80))
    assert incoming not in view.state.graph.connections
    qtbot.keyClick(view, Qt.Key.Key_Z, modifier=Qt.KeyboardModifier.ControlModifier)
    assert incoming in view.state.graph.connections


def test_label_enter_commits_and_returns_focus(view: ColorNodeView, qtbot: Any) -> None:
    node_id = view.state.graph.active_id
    qtbot.keyClick(view, Qt.Key.Key_F2)
    assert view._label is not None
    qtbot.keyClicks(view, "Skin Tone")
    qtbot.keyClick(view, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: view._label is None)
    assert view.state.graph.nodes[node_id].label == "Skin Tone"
    assert view._label is None
    qtbot.keyClick(view, Qt.Key.Key_S, modifier=Qt.KeyboardModifier.AltModifier)
    assert view.state.graph.active_id != node_id


def test_label_escape_cancels_without_history(view: ColorNodeView, qtbot: Any) -> None:
    node_id = view.state.graph.active_id
    qtbot.keyClick(view, Qt.Key.Key_F2)
    qtbot.keyClicks(view, "Discard")
    qtbot.keyClick(view, Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: view._label is None)
    assert view.state.graph.nodes[node_id].label == ""
    assert view._label is None
    assert not view.state.can_undo


def test_label_focus_out_commits_safely(view: ColorNodeView, qtbot: Any) -> None:
    node_id = view.state.graph.active_id
    qtbot.keyClick(view, Qt.Key.Key_F2)
    qtbot.keyClicks(view, "Background")
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
    qtbot.waitUntil(lambda: view._label is None)
    assert view.state.graph.nodes[node_id].label == "Background"
    assert view._label is None


def test_context_parallel_action_builds_branch(view: ColorNodeView) -> None:
    first = view.state.graph.active_id
    menu = view.build_context_menu()
    _action(menu, "add_parallel").trigger()
    mixers = [node for node in view.state.graph.nodes.values() if node.kind == NodeKind.PARALLEL_MIXER]
    assert len(mixers) == 1
    assert view.state.graph.active_id != first
    assert sum(edge.dst_node == mixers[0].id for edge in view.state.graph.connections) == 2


def test_context_morph_preserves_ports_and_connections(view: ColorNodeView) -> None:
    view.state.add_branch(NodeKind.PARALLEL_MIXER)
    mixer = next(node for node in view.state.graph.nodes.values() if node.kind == NodeKind.PARALLEL_MIXER)
    view.state.select({mixer.id}, mixer.id)
    view.rebuild()
    connections = list(view.state.graph.connections)
    menu = view.build_context_menu()
    _action(menu, "morph").trigger()
    assert view.state.graph.nodes[mixer.id].kind == NodeKind.LAYER_MIXER
    assert view.state.graph.connections == connections
    assert set(view.cards[mixer.id].ports) == {"image_in_0", "image_in_1", "image_out"}


def test_delete_selected_connection_does_not_delete_corrector(view: ColorNodeView, qtbot: Any) -> None:
    link = view.links[0]
    connection = link.connection
    node_ids = set(view.state.graph.nodes)
    view.scene().clearSelection()
    link.setSelected(True)
    qtbot.keyClick(view, Qt.Key.Key_Backspace)
    assert connection not in view.state.graph.connections
    assert set(view.state.graph.nodes) == node_ids


def test_escape_during_wire_drag_removes_transient_item(view: ColorNodeView, qtbot: Any) -> None:
    node_id = view.state.graph.active_id
    initial_connections = list(view.state.graph.connections)
    point = _point(view, node_id, "key_out")
    _mouse(view, QEvent.Type.MouseButtonPress, point,
           Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
    assert view._wire is not None
    qtbot.keyClick(view, Qt.Key.Key_Escape)
    assert view._wire is None
    assert view._wire_origin is None
    assert view.state.graph.connections == initial_connections


def test_rebuild_clears_deleted_insertion_highlight(view: ColorNodeView, qtbot: Any) -> None:
    # This is the transient state while a card is dragged over a connection.
    old_link: ConnectionItem = view.links[0]
    view._insert_target = old_link
    old_link.set_highlighted(True)
    qtbot.keyClick(view, Qt.Key.Key_Escape)
    assert view._insert_target is None
    assert not view._move_start


def test_locked_card_cannot_be_dragged(view: ColorNodeView) -> None:
    node_id = view.state.graph.active_id
    view.state.set_node_fields({node_id}, locked=True)
    view.rebuild()
    start = _point(view, node_id)
    original = (view.state.graph.nodes[node_id].x, view.state.graph.nodes[node_id].y)
    _drag(view, start, start + QPoint(35, 70))
    node = view.state.graph.nodes[node_id]
    assert (node.x, node.y) == original


def test_alt_drag_moves_the_duplicate_and_preserves_original(view: ColorNodeView, qtbot: Any) -> None:
    original_id = view.state.graph.active_id
    original = (view.state.graph.nodes[original_id].x, view.state.graph.nodes[original_id].y)
    original_ids = set(view.state.graph.nodes)
    start = _point(view, original_id)
    end = start + QPoint(45, 100)
    _mouse(view, QEvent.Type.MouseButtonPress, start,
           Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.AltModifier)
    _mouse(view, QEvent.Type.MouseMove, end,
           Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.AltModifier)
    _mouse(view, QEvent.Type.MouseButtonRelease, end,
           Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.AltModifier)
    duplicate_ids = set(view.state.graph.nodes) - original_ids
    assert len(duplicate_ids) == 1
    duplicate_id = duplicate_ids.pop()
    assert (view.state.graph.nodes[original_id].x, view.state.graph.nodes[original_id].y) == original
    duplicate = view.state.graph.nodes[duplicate_id]
    assert (duplicate.x, duplicate.y) == pytest.approx((original[0] + 45.0, original[1] + 100.0), abs=1.0)
    assert view.state.graph.selected_ids == {duplicate_id}
    qtbot.keyClick(view, Qt.Key.Key_Z, modifier=Qt.KeyboardModifier.ControlModifier)
    assert set(view.state.graph.nodes) == original_ids
    assert not view.state.can_undo


def test_insert_by_drag_is_one_undo_gesture(view: ColorNodeView, qtbot: Any) -> None:
    node_id = view.state.add_node(NodeKind.CORRECTOR, 220.0, 210.0)
    # Load a clean baseline so only the drag should be undoable.
    view.load_graph(view.state.snapshot())
    before = view.state.snapshot()
    link = view.links[0]
    midpoint = view.mapFromScene(link.path().pointAtPercent(0.5))
    _drag(view, _point(view, node_id), midpoint)
    assert any(edge.dst_node == node_id for edge in view.state.graph.connections)
    assert any(edge.src_node == node_id for edge in view.state.graph.connections)
    qtbot.keyClick(view, Qt.Key.Key_Z, modifier=Qt.KeyboardModifier.ControlModifier)
    assert view.state.graph.connections == before.connections
    node, old_node = view.state.graph.nodes[node_id], before.nodes[node_id]
    assert (node.x, node.y) == (old_node.x, old_node.y)
    assert not view.state.can_undo


def test_array_backed_grade_does_not_break_edit_detection(view: ColorNodeView) -> None:
    # Real Layer snapshots can contain curve LUT / warper arrays.  Comparing
    # whole dataclasses with != raises on those otherwise valid draft values.
    import numpy as np

    node_id = view.state.graph.active_id
    view.state.set_node_fields({node_id}, grade={"curves_lut": np.zeros((4, 3))})
    view.rebuild()
    intents: list[str] = []
    view.edit_requested.connect(lambda action, payload: intents.append(action))
    view.apply_edit("move_nodes", lambda: view.state.move_nodes({node_id: (20.0, 30.0)}))
    assert intents == ["move_nodes"]
    assert view.state.graph.nodes[node_id].x == 20.0


def test_loading_graph_cancels_pending_label_commit(view: ColorNodeView, qtbot: Any) -> None:
    node_id = view.state.graph.active_id
    qtbot.keyClick(view, Qt.Key.Key_F2)
    qtbot.keyClicks(view, "Old context")
    qtbot.keyClick(view, Qt.Key.Key_Return)
    replacement = view.state.snapshot()
    replacement.nodes[node_id].label = "New context"
    view.load_graph(replacement)
    # Drain the deferred label callback after the externally hydrated graph.
    QApplication.processEvents()
    assert view.state.graph.nodes[node_id].label == "New context"
    assert not view.state.can_undo


def test_disconnect_reconnect_image_ports_is_two_reversible_gestures(view: ColorNodeView) -> None:
    node_id = view.state.graph.active_id
    edge = next(edge for edge in view.state.graph.connections if edge.dst_node == node_id)
    intents: list[str] = []
    view.edit_requested.connect(lambda action, _: intents.append(action))
    start = _point(view, node_id, edge.dst_port)
    _drag(view, start, start + QPoint(0, -80))
    assert edge not in view.state.graph.connections
    _drag(view, _point(view, edge.src_node, edge.src_port), _point(view, node_id, edge.dst_port))
    assert edge in view.state.graph.connections
    assert intents == ["disconnect", "connect"]
    view.state.undo()
    assert edge not in view.state.graph.connections
    view.state.undo()
    assert edge in view.state.graph.connections


def test_cancel_input_disconnect_restores_wire_without_edit(view: ColorNodeView, qtbot: Any) -> None:
    node_id = view.state.graph.active_id
    edge = next(edge for edge in view.state.graph.connections if edge.dst_node == node_id)
    original = view.state.snapshot()
    start = _point(view, node_id, edge.dst_port)
    _mouse(view, QEvent.Type.MouseButtonPress, start, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
    _mouse(view, QEvent.Type.MouseMove, start + QPoint(0, -80), Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
    assert not next(link for link in view.links if link.connection == edge).isVisible()
    qtbot.keyClick(view, Qt.Key.Key_Escape)
    assert view.state.graph.connections == original.connections
    assert next(link for link in view.links if link.connection == edge).isVisible()
    assert not view.state.can_undo


def test_previous_next_selection_does_not_edit_or_create_history(view: ColorNodeView) -> None:
    first = view.state.graph.active_id
    second = view.state.add_serial()
    view.rebuild()
    edits = []
    view.edit_requested.connect(lambda *args: edits.append(args))
    view.navigate_node(-1)
    assert view.state.graph.active_id == first
    view.navigate_node(1)
    assert view.state.graph.active_id == second
    assert edits == []


def test_append_inserts_at_output_even_when_earlier_node_selected(view: ColorNodeView) -> None:
    first = view.state.graph.active_id
    second = view.state.add_serial()
    view.state.select({first}, first)
    view.rebuild()
    view.append_node()
    appended = view.state.graph.active_id
    assert any(edge.src_node == second and edge.dst_node == appended for edge in view.state.graph.connections)


def test_paste_domains_carries_full_arguments(qtbot) -> None:
    """N12-B：paste_domains 手势透传完整三参——权威重放缺参会被归一化
    拒绝并回滚（此前 UI 手势实际提交失败）。"""
    view = ColorNodeView()
    qtbot.addWidget(view)
    view.apply_edit("add_serial", view.state.add_serial)
    node_id = view.state.graph.active_id
    view.state.select({node_id}, node_id)
    view._copied_domains = {"grade": {"exposure": 3.0},
                            "curves": {"points": [[0, 0], [1, 1]]}}
    captured: list[dict] = []
    view.edit_requested.connect(
        lambda action, payload: captured.append(payload))
    view.paste_domains(("grade",))
    assert captured, "paste_domains 未发出 edit_requested"
    args = captured[-1].get("arguments")
    assert isinstance(args, dict) and args.get("node_ids") == [node_id]
    assert args.get("domains") == ["grade"]
    assert isinstance(args.get("payload"), dict) and "grade" in args["payload"]
