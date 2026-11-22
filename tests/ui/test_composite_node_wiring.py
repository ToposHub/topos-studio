"""Exercise cable gestures through the composite canvas and page undo stack."""

from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QMimeData, QPoint, QPointF, Qt
from PyQt6.QtGui import QDragEnterEvent, QDropEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QDockWidget, QMainWindow

from src.features.composite.core.graph_edit import EDIT_LAYER_CONFLICT, EDIT_STALE_REVISION
from src.features.composite.core.layer_stack_compiler import CompileResult
from src.features.composite.nodes.blur_node import BlurNode
from src.features.composite.nodes.constant_node import ConstantNode
from src.features.composite.nodes.output_node import OutputNode
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.ui.composite_page import CompositePage
from src.features.composite.ui.keyframes_panel import KeyframesPanel
from src.features.composite.ui.node_item import NodeItem
from src.features.composite.ui.node_search_popup import NODE_PALETTE_MIME, NodeSearchPopup


@pytest.fixture
def page(qapp):
    page = CompositePage()
    page._evaluate_graph = MagicMock()
    page.node_graph.clear()
    page.set_composition_active(True)
    yield page
    page.close()


def _node(page, cls, name, x, y):
    node = cls(node_id=name)
    node.position = (x, y)
    page.node_graph.add_node(node)
    return node


def _wiring_page(page, qapp):
    for nid, x, y in (("a", 0, 0), ("b", 0, 155)):
        _node(page, ConstantNode, nid, x, y)
    for nid, x, y in (("c", 340, 0), ("d", 340, 155), ("e", 340, 305)):
        _node(page, TransformNode, nid, x, y)
    page.node_graph.connect("a", "result", "c", "image")
    page.node_graph.connect("b", "result", "d", "image")
    page._node_editor.build_from_graph(page.node_graph)
    editor = page._node_editor
    page.resize(860, 560)
    page.show()
    qapp.processEvents()
    editor.centerOn(240, 178)
    qapp.processEvents()
    return editor


def _drag(editor, source, target, modifiers=Qt.KeyboardModifier.NoModifier):
    view = editor.viewport()
    button = Qt.MouseButton.LeftButton
    start = editor.mapFromScene(source)
    end = editor.mapFromScene(target)
    QTest.mousePress(view, button, modifiers, start)
    QTest.mouseMove(view, end)
    QTest.mouseRelease(view, button, modifiers, end)


def _end(editor, node_id, input_=False):
    item = editor.get_node_item(node_id)
    return (item.input_ports["image"] if input_ else item.output_ports["result"]).scenePos()


def test_drag_existing_input_to_new_source_body_is_one_undo(page, qapp):
    editor = _wiring_page(page, qapp)
    before = page._graph_cable_keys()
    c_port = _end(editor, "c", input_=True)
    b_body = editor.get_node_item("b").sceneBoundingRect().center()
    view = editor.viewport()
    QTest.mousePress(view, Qt.MouseButton.LeftButton, pos=editor.mapFromScene(c_port))
    QTest.mouseMove(view, editor.mapFromScene(b_body))
    assert page._graph_cable_keys() == before
    assert editor._snap_port is editor.get_node_item("b").output_ports["result"]
    QTest.mouseRelease(view, Qt.MouseButton.LeftButton, pos=editor.mapFromScene(b_body))

    result = {("b", "result", "c", "image"), ("b", "result", "d", "image")}
    assert page._graph_cable_keys() == result
    page.undo()
    assert page._graph_cable_keys() == before
    page.redo()
    assert page._graph_cable_keys() == result


def test_shift_drag_cable_duplicates_branch(page, qapp):
    editor = _wiring_page(page, qapp)
    start = _end(editor, "c", input_=True) - QPointF(13, 0)
    before = page._graph_cable_keys()
    _drag(editor, start, _end(editor, "e", input_=True), Qt.KeyboardModifier.ShiftModifier)
    assert page._graph_cable_keys() == before | {("a", "result", "e", "image")}
    page.undo()
    assert page._graph_cable_keys() == before


def test_replace_occupied_input_highlights_old_cable(page, qapp):
    editor = _wiring_page(page, qapp)
    before = page._graph_cable_keys()
    view = editor.viewport()
    c_end = _end(editor, "c", input_=True)
    start = c_end - QPointF(13, 0)
    end = _end(editor, "d", input_=True)
    QTest.mousePress(view, Qt.MouseButton.LeftButton, pos=editor.mapFromScene(start))
    QTest.mouseMove(view, editor.mapFromScene(end))
    assert len(editor._replaced_cables) == 1
    assert editor._replaced_cables[0].dst_port.parentItem().node_id == "d"
    assert page._graph_cable_keys() == before
    QTest.mouseRelease(view, Qt.MouseButton.LeftButton, pos=editor.mapFromScene(end))
    assert page._graph_cable_keys() == {("a", "result", "d", "image")}
    page.undo()
    assert page._graph_cable_keys() == before


def test_escape_and_invalid_drop_restore_cable(page, qapp):
    editor = _wiring_page(page, qapp)
    before = page._graph_cable_keys()
    c_port = _end(editor, "c", input_=True)
    _drag(editor, c_port, _end(editor, "d", input_=True))
    assert page._graph_cable_keys() == before
    view = editor.viewport()
    QTest.mousePress(view, Qt.MouseButton.LeftButton, pos=editor.mapFromScene(c_port))
    QTest.mouseMove(view, editor.mapFromScene(_end(editor, "b")))
    QTest.keyClick(editor, Qt.Key.Key_Escape)
    QTest.mouseRelease(view, Qt.MouseButton.LeftButton, pos=editor.mapFromScene(_end(editor, "b")))
    assert page._graph_cable_keys() == before
    assert not editor._dragging_connection


def test_drag_end_to_empty_space_disconnects_one_edge(page, qapp):
    editor = _wiring_page(page, qapp)
    before = page._graph_cable_keys()
    _drag(editor, _end(editor, "c", input_=True), QPointF(240, 130))
    assert page._graph_cable_keys() == {("b", "result", "d", "image")}
    page.undo()
    assert page._graph_cable_keys() == before


def test_releasing_cable_outside_viewport_cancels(page, qapp):
    editor = _wiring_page(page, qapp)
    before = page._graph_cable_keys()
    outside = editor.mapToScene(editor.viewport().rect().topRight() + QPoint(40, 90))
    _drag(editor, _end(editor, "c", input_=True), outside)
    assert not editor._dragging_connection
    assert page._graph_cable_keys() == before


def test_releasing_cable_on_another_wire_cancels(page, qapp):
    editor = _wiring_page(page, qapp)
    before = page._graph_cable_keys()
    other_wire = next(cable for cable, _src, _port, dst, _input
                      in editor._connection_items if dst == "d")
    _drag(editor, _end(editor, "c", input_=True), other_wire.get_midpoint_scene())
    assert page._graph_cable_keys() == before


@pytest.mark.parametrize("routing", ["curved", "straight", "orthogonal"])
def test_cable_routing_hit_test_matches_painted_path(page, qapp, routing):
    editor = _wiring_page(page, qapp)
    before = page._graph_cable_keys()
    editor.get_node_item("c").setPos(QPointF(340, 90))
    edge = next(cable for cable, _src, _port, dst, _input in editor._connection_items if dst == "c")
    editor._set_cable_routing(routing)
    end = edge.path().pointAtPercent(1)
    assert end == _end(editor, "c", input_=True)
    if routing == "orthogonal":
        assert edge.path().elementCount() == 4
    elif routing == "straight":
        assert edge.path().elementCount() == 2
    else:
        assert edge.path().elementAt(1).isCurveTo()
    midpoint = edge.get_midpoint_scene()
    _, distance = edge.get_nearest_point_on_path(midpoint)
    assert distance < 1.0
    edge.set_show_insertion_dot(True)
    assert edge.hit_test_insertion_dot(midpoint)
    assert page._graph_cable_keys() == before


def test_double_click_empty_graph_creates_at_click_position(page, qapp, monkeypatch):
    editor = _wiring_page(page, qapp)
    editor.select_node("a")
    point = editor.mapFromScene(QPointF(175, 125))
    click_scene = editor.mapToScene(point)
    assert editor.itemAt(point) is None
    monkeypatch.setattr(NodeSearchPopup, "_save_recent_nodes", lambda _popup: None)
    monkeypatch.setattr(NodeSearchPopup, "exec", lambda popup: popup._confirm_selection("blur"))
    before = page._graph_cable_keys()
    QTest.mouseDClick(editor.viewport(), Qt.MouseButton.LeftButton, pos=point)
    blur = next(node for node in page.node_graph.nodes.values() if node.node_type == "blur")
    assert blur.position == (click_scene.x(), click_scene.y())
    assert page._graph_cable_keys() == before


def test_node_library_accepts_node_drag_on_graph(page, qapp):
    editor = _wiring_page(page, qapp)
    page._node_palette_button.click()
    assert page._node_palette.isVisible()
    assert page._node_palette._all_list.dragEnabled()
    point = editor.mapFromScene(QPointF(175, 150))
    mime = QMimeData()
    mime.setData(NODE_PALETTE_MIME, b"blur")
    copy = Qt.DropAction.CopyAction
    button = Qt.MouseButton.LeftButton
    modifiers = Qt.KeyboardModifier.NoModifier
    enter = QDragEnterEvent(point, copy, mime, button, modifiers)
    editor.dragEnterEvent(enter)
    assert enter.isAccepted()
    drop = QDropEvent(QPointF(point), copy, mime, button, modifiers)
    editor.dropEvent(drop)
    assert drop.isAccepted()
    assert sum(node.node_type == "blur" for node in page.node_graph.nodes.values()) == 1
    page.undo()
    assert all(node.node_type != "blur" for node in page.node_graph.nodes.values())

    page.set_composition_active(False)
    assert not page._node_palette.isVisible()
    editor.dragEnterEvent(enter)
    assert not enter.isAccepted()


def test_node_library_and_tab_chooser_share_favorites(qapp, monkeypatch):
    favorites = []
    monkeypatch.setattr(NodeSearchPopup, "_load_favorites", lambda _popup: favorites.copy())
    monkeypatch.setattr(NodeSearchPopup, "_save_favorites",
                        lambda popup: favorites.__setitem__(slice(None), popup._favorites))
    categories = {"Filter": [("blur", BlurNode)], "Transform": [("transform", TransformNode)]}
    modal = NodeSearchPopup(categories)
    library = NodeSearchPopup(categories, embedded=True)
    modal._toggle_favorite("blur")
    library._toggle_favorite("transform")
    modal._tab_widget.setCurrentWidget(modal._favorites_tab)
    assert modal._favorites == ["blur", "transform"]
    assert modal._favorites_list.count() == 2


def test_reveal_layer_and_node_opens_the_other_dock(page, qapp):
    controller = page._layer_stack_controller
    _node(page, OutputNode, "out", 540, 0)
    source = _node(page, ConstantNode, "plate", 10, 0)
    layer_id = controller.add_media_layer("Plate", source.id)
    other = _node(page, ConstantNode, "other", 10, 150)
    controller.add_media_layer("Other", other.id)
    layer = page.node_graph.get_layer(layer_id)
    merge = controller.layer_merge_node(layer_id)
    page._node_editor.build_from_graph(page.node_graph)

    window = QMainWindow()
    timeline, graph = QDockWidget(window), QDockWidget(window)
    panel = KeyframesPanel()
    timeline.setWidget(panel)
    graph.setWidget(page)
    window.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, timeline)
    window.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, graph)
    page.set_keyframes_panel(panel)
    window.resize(1000, 720)
    window.show()
    qapp.processEvents()
    timeline.hide()
    assert page._timeline_layers_for_node(merge.id) == [(layer_id, "Plate")]
    page._node_editor.reveal_timeline_layer_requested.emit(layer_id)
    assert timeline.isVisible()
    assert panel._selected_layer_id == layer_id
    graph.hide()
    group_id = "other_group"
    assert page.node_graph.create_group(group_id, [other.id], {"title": "Other"})
    page._on_group_enter_requested(group_id)
    before = page.node_graph.to_dict()
    panel.reveal_layer_node_requested.emit(layer.root_node_id)
    assert graph.isVisible()
    assert page._node_editor.get_selected_node_id() == layer.root_node_id
    assert not page._node_editor.is_in_group_view()
    assert page.node_graph.to_dict() == before
    page.undo()
    assert not page.node_graph.get_group(group_id)["is_expanded"]
    page.setParent(None)
    window.close()


def test_add_after_fork_keeps_existing_branches(page, qapp):
    editor = _wiring_page(page, qapp)
    assert page.node_graph.connect("a", "result", "e", "image")
    editor.build_from_graph(page.node_graph)
    before = page._graph_cable_keys()
    editor._add_new_node("blur", QPointF(), "a")
    blur = next(node for node in page.node_graph.nodes.values() if node.node_type == "blur")
    assert page._graph_cable_keys() == before | {("a", "result", blur.id, "image")}
    page.undo()
    assert page.node_graph.get_node(blur.id) is None
    assert page._graph_cable_keys() == before
    page.redo()
    assert page.node_graph.get_node(blur.id) is not None
    assert ("a", "result", blur.id, "image") in page._graph_cable_keys()


def test_compact_merge_and_explicit_effect_drop_preserve_layer_and_other_branch(page):
    controller = page._layer_stack_controller
    graph = page.node_graph
    root = _node(page, ConstantNode, "plate", 0, 0)
    side = _node(page, TransformNode, "side", 400, 180)
    _node(page, OutputNode, "out", 550, 0)
    blur = _node(page, BlurNode, "blur", 150, 60)
    layer_id = controller.add_media_layer("Footage", root.id)
    layer_root = graph.get_layer(layer_id).root_node_id
    merge = controller.layer_merge_node(layer_id)
    graph.connect(layer_root, "result", side.id, "image")
    editor = page._node_editor
    editor.build_from_graph(graph)
    item = editor.get_node_item(merge.id)
    assert item.rect().width() == NodeItem.NODE_WIDTH == 148
    assert item._layer_managed
    assert item.input_ports["A"].display_label.startswith("A ")
    assert item.input_ports["B"].display_label.startswith("B ")
    assert item.input_ports["mask"].display_label.startswith("Mask")
    old = page._graph_cable_keys()
    editor._move_start_positions = {blur.id: QPointF(*blur.position)}
    editor._drop_insert_connection = next(edge for edge in editor._connection_items
                                         if edge[3:] == (merge.id, "A"))
    editor._drop_insert_nearest_point = QPointF(275, 0)

    editor._execute_connection_insertion()
    assert graph.get_layer(layer_id).root_node_id == blur.id
    assert (layer_root, "result", side.id, "image") in page._graph_cable_keys()
    assert (blur.id, "result", merge.id, "A") in page._graph_cable_keys()
    page.undo()
    assert graph.get_layer(layer_id).root_node_id == layer_root
    assert page._graph_cable_keys() == old
    assert graph.get_node(blur.id).position == (150, 60)
    page.redo()
    assert graph.get_layer(layer_id).root_node_id == blur.id


def test_managed_foreground_rejects_foreign_cable_and_accepts_new_transform(page, qapp):
    controller = page._layer_stack_controller
    _node(page, OutputNode, "out", 590, 0)
    source = _node(page, ConstantNode, "plate", 0, 0)
    foreign = _node(page, ConstantNode, "foreign", 0, 170)
    transform = _node(page, TransformNode, "new", 240, 170)
    layer_id = controller.add_media_layer("Footage", source.id)
    merge = controller.layer_merge_node(layer_id)
    merge.position = (420, 0)
    editor = page._node_editor
    editor.build_from_graph(page.node_graph)
    page.resize(900, 590)
    page.show()
    qapp.processEvents()
    editor.centerOn(275, 120)

    old = page._graph_cable_keys()
    merge_input = editor.get_node_item(merge.id).input_ports["A"].scenePos()
    _drag(editor, _end(editor, foreign.id), merge_input)
    assert page._graph_cable_keys() == old

    cable = next(data[0] for data in editor._connection_items if data[3:] == (merge.id, "A"))
    cable.setSelected(True)
    editor._delete_selected()
    assert page._graph_cable_keys() == old

    # Transform supports wiring either end first. A non-owned generator does not.
    previous_root = page.node_graph.get_layer(layer_id).root_node_id
    start = editor.mapFromScene(_end(editor, transform.id))
    end = editor.mapFromScene(merge_input)
    viewport = editor.viewport()
    button = Qt.MouseButton.LeftButton
    QTest.mousePress(viewport, button, pos=start)
    QTest.mouseMove(viewport, end)
    page.node_graph.bump_generation()
    latest = page.node_graph.generation
    history = page._undo_stack.undo_description()
    QTest.mouseRelease(viewport, button, pos=end)
    assert page._graph_cable_keys() == old
    assert page.node_graph.generation == latest
    assert page._undo_stack.undo_description() == history
    assert page._last_graph_edit_issues[0].code == EDIT_STALE_REVISION

    _drag(editor, _end(editor, transform.id), merge_input)
    assert editor.pending_layer_transform() == (transform.id, "result", merge.id, "A")
    assert page._graph_cable_keys() == old
    assert page.node_graph.generation == latest
    assert page._undo_stack.undo_description() == history
    _drag(editor, _end(editor, previous_root), _end(editor, transform.id, input_=True))
    assert page.node_graph.get_layer(layer_id).root_node_id == transform.id
    assert editor.pending_layer_transform() is None
    page.undo()
    assert page._graph_cable_keys() == old
    assert page.node_graph.get_layer(layer_id).root_node_id == previous_root


def test_refused_wired_layer_effect_keeps_graph_and_revisions(page, monkeypatch) -> None:
    graph = page.node_graph
    controller = page._layer_stack_controller
    _node(page, OutputNode, "out", 590, 0)
    source = _node(page, ConstantNode, "source", 0, 0)
    blur = _node(page, BlurNode, "effect", 240, 170)
    layer_id = controller.add_media_layer("Footage", source.id)
    root_id = graph.get_layer(layer_id).root_node_id
    merge_id = controller.layer_merge_node(layer_id).id
    original = (root_id, "result", merge_id, "A")
    incoming = (root_id, "result", blur.id, "image")
    outgoing = (blur.id, "result", merge_id, "A")
    ordered_edges = graph.connections
    edges = page._graph_cable_keys()
    data = graph.to_dict()
    revisions = (graph.generation, graph._topology_revision, controller.generation)
    history = page._undo_stack.undo_description()

    def refuse_candidate(*_args: object) -> CompileResult:
        assert page._graph_cable_keys() == edges - {original} | {incoming, outgoing}
        assert graph.generation == revisions[0]
        return CompileResult(ok=False)

    monkeypatch.setattr(controller.compiler, "compile", refuse_candidate)
    assert not page._edit_graph_cables((original,), (incoming, outgoing))
    assert page._last_graph_edit_issues[0].code == EDIT_LAYER_CONFLICT
    assert graph.to_dict() == data
    assert graph.connections is ordered_edges
    assert page._graph_cable_keys() == edges
    assert (graph.generation, graph._topology_revision, controller.generation) == revisions
    assert page._undo_stack.undo_description() == history


def test_failed_layer_projection_rolls_back_connections_and_history(page, monkeypatch) -> None:
    graph = page.node_graph
    controller = page._layer_stack_controller
    _node(page, OutputNode, "out", 590, 0)
    source = _node(page, ConstantNode, "source", 0, 0)
    blur = _node(page, BlurNode, "effect", 240, 170)
    layer_id = controller.add_media_layer("Footage", source.id)
    root_id = graph.get_layer(layer_id).root_node_id
    merge_id = controller.layer_merge_node(layer_id).id
    ordered_edges = graph.connections
    state = graph.to_dict()
    revisions = (graph.generation, graph._topology_revision, controller.generation)
    history = page._undo_stack.undo_description()

    monkeypatch.setattr(controller, "_apply_changes", lambda _changes: ["projection failed"])
    assert not page._edit_graph_cables(
        ((root_id, "result", merge_id, "A"),),
        ((root_id, "result", blur.id, "image"), (blur.id, "result", merge_id, "A")),
    )
    assert page._last_graph_edit_issues[0].code == EDIT_LAYER_CONFLICT
    assert graph.to_dict() == state
    assert graph.connections is ordered_edges
    assert (graph.generation, graph._topology_revision, controller.generation) == revisions
    assert page._undo_stack.undo_description() == history


def test_drop_node_from_its_title_highlights_only_intended_wire(page, qapp):
    editor = _wiring_page(page, qapp)
    blur = _node(page, BlurNode, "blur", 230, 235)
    graph = page.node_graph
    assert graph.connect("a", "result", "e", "image")
    editor.build_from_graph(graph)
    editor.centerOn(240, 178)
    original = page._graph_cable_keys()
    cable = next(data[0] for data in editor._connection_items if data[3] == "c")
    hit = cable.get_midpoint_scene()
    start = editor.get_node_item(blur.id).scenePos() + QPointF(65, 10)
    view = editor.viewport()
    QTest.mousePress(view, Qt.MouseButton.LeftButton, pos=editor.mapFromScene(start))
    QTest.mouseMove(view, editor.mapFromScene(hit))
    assert editor._drop_insert_connection is not None
    assert editor._drop_insert_connection[0] is cable
    QTest.mouseRelease(view, Qt.MouseButton.LeftButton, pos=editor.mapFromScene(hit))
    assert page._graph_cable_keys() == (
        original - {("a", "result", "c", "image")}
        | {("a", "result", blur.id, "image"), (blur.id, "result", "c", "image")}
    )
    page.undo()
    assert page._graph_cable_keys() == original
    assert graph.get_node(blur.id).position == (230, 235)


def test_dot_on_wire_is_one_undo(page, qapp):
    editor = _wiring_page(page, qapp)
    old = page._graph_cable_keys()
    cable = next(data[0] for data in editor._connection_items if data[3] == "c")
    editor._ctrl_insertion_active = True
    editor._show_connection_insertion_dots(True)
    QTest.mouseClick(editor.viewport(), Qt.MouseButton.LeftButton,
                     pos=editor.mapFromScene(cable.get_midpoint_scene()))
    dot = next(node for node in page.node_graph.nodes.values() if node.node_type == "dot")
    assert page._graph_cable_keys() == {
        ("a", "result", dot.id, "input"), (dot.id, "output", "c", "image"),
        ("b", "result", "d", "image"),
    }
    page.undo()
    assert page.node_graph.get_node(dot.id) is None
    assert page._graph_cable_keys() == old
    page.redo()
    assert page.node_graph.get_node(dot.id) is not None
    assert (dot.id, "output", "c", "image") in page._graph_cable_keys()


def test_rejected_cable_edit_reports_reason_code_without_mutation(page, qapp):
    """B02: a refused gesture exposes a reason code and keeps cables intact."""
    from src.features.composite.core.graph_edit import EDIT_TYPE_MISMATCH
    from src.features.composite.nodes.scene3d_node import Scene3DNode

    editor = _wiring_page(page, qapp)
    _node(page, Scene3DNode, "s3d", 600, 0)
    editor.build_from_graph(page.node_graph)
    before = page._graph_cable_keys()

    # Image output dragged into a scene-typed input: type mismatch.
    assert page._edit_graph_cables(
        (), (("a", "result", "s3d", "in"),), allow_pending_transform=False) is False
    assert page._graph_cable_keys() == before
    assert page._last_graph_edit_issues
    assert page._last_graph_edit_issues[0].code == EDIT_TYPE_MISMATCH


def test_stale_gesture_revision_cancels_edit(page, qapp):
    """B02: an edit proposed against an outdated revision is rejected."""
    editor = _wiring_page(page, qapp)
    before = page._graph_cable_keys()

    stale = page.node_graph.generation + 3
    assert page._edit_graph_cables(
        (("a", "result", "c", "image"),),
        (("a", "result", "e", "image"),),
        expected_revision=stale) is False
    assert page._graph_cable_keys() == before
    assert page._last_graph_edit_issues[0].code == "edit.stale_revision"

    # The same edit against the current revision applies ("e" is free).
    assert page._edit_graph_cables(
        (("a", "result", "c", "image"),),
        (("a", "result", "e", "image"),)) is True
    assert ("a", "result", "e", "image") in page._graph_cable_keys()
