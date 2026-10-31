"""P1-12 tests for Composite graph bookmarks, history and node locks."""

from __future__ import annotations

from PyQt6.QtWidgets import QGraphicsItem

from src.features.composite.ui.graph_navigation import GraphNavigationState
from src.features.composite.ui.node_item import NodeItem


def _view(x: float, y: float, zoom: float = 1.0) -> dict:
    return {"center_x": x, "center_y": y, "zoom": zoom}


def test_bookmarks_round_trip_and_replace_in_place() -> None:
    state = GraphNavigationState()
    state.add_bookmark("Input", _view(10.0, 20.0, 0.75), "source")
    state.add_bookmark("Output", _view(100.0, 200.0), "output")
    state.add_bookmark("Input", _view(30.0, 40.0, 1.25), "source-2")

    restored = GraphNavigationState.from_dict(state.to_dict())
    assert [bookmark.name for bookmark in restored.bookmarks()] == [
        "Input", "Output"
    ]
    bookmark = restored.get_bookmark("Input")
    assert bookmark is not None
    assert bookmark.viewport == {"zoom": 1.25, "center_x": 30.0,
                                 "center_y": 40.0}
    assert bookmark.selected_node_id == "source-2"


def test_history_deduplicates_bounds_and_truncates_forward_branch() -> None:
    state = GraphNavigationState(history_limit=3)
    assert state.record_view(_view(0.0, 0.0))
    assert not state.record_view(_view(0.0, 0.0))
    assert state.record_view(_view(1.0, 0.0))
    assert state.record_view(_view(2.0, 0.0))
    assert state.record_view(_view(3.0, 0.0))
    assert state.record_view(_view(4.0, 0.0))

    # The bounded history keeps the last three locations.
    assert state.can_go_back
    assert state.back()["viewport"]["center_x"] == 3.0
    assert state.back()["viewport"]["center_x"] == 2.0
    assert state.back() is None
    assert state.can_go_forward
    assert state.forward()["viewport"]["center_x"] == 3.0

    # A new movement after going back discards the forward branch.
    assert state.record_view(_view(9.0, 0.0))
    assert not state.can_go_forward


def test_malformed_bookmarks_are_ignored() -> None:
    state = GraphNavigationState.from_dict({
        "bookmarks": {
            "version": 1,
            "bookmarks": [
                {"name": "Valid", "center_x": 4, "center_y": 5},
                {"name": ""},
                "not-a-bookmark",
            ],
        }
    })
    assert [bookmark.name for bookmark in state.bookmarks()] == ["Valid"]


def test_node_item_lock_disables_movement_but_keeps_selection(qapp) -> None:
    item = NodeItem("node", node_title="Blur")
    assert item.flags() & QGraphicsItem.GraphicsItemFlag.ItemIsMovable
    assert item.flags() & QGraphicsItem.GraphicsItemFlag.ItemIsSelectable

    item.set_locked(True)
    assert item.is_locked
    assert not item.flags() & QGraphicsItem.GraphicsItemFlag.ItemIsMovable
    assert item.flags() & QGraphicsItem.GraphicsItemFlag.ItemIsSelectable

    item.set_locked(False)
    assert not item.is_locked
    assert item.flags() & QGraphicsItem.GraphicsItemFlag.ItemIsMovable


def test_node_editor_persists_bookmarks_and_locks(qapp) -> None:
    from src.features.composite.core.node_engine import NodeGraph
    from src.features.composite.nodes.constant_node import ConstantNode
    from src.features.composite.nodes.output_node import OutputNode
    from src.features.composite.ui.node_editor_widget import NodeEditorWidget

    graph = NodeGraph()
    source = ConstantNode(node_id="source")
    output = OutputNode(node_id="output")
    graph.add_node(source)
    graph.add_node(output)
    assert graph.connect("source", "result", "output", "image")

    editor = NodeEditorWidget()
    editor.build_from_graph(graph)
    assert editor.set_node_locked("source", True)
    assert editor.add_view_bookmark("Source")

    saved = graph._viewport_state
    assert saved["locked_node_ids"] == ["source"]
    assert saved["bookmarks"]["bookmarks"][0]["name"] == "Source"

    editor.set_node_locked("source", False)
    editor.set_viewport_state(saved)
    assert editor.is_node_locked("source")
    assert editor._navigation.get_bookmark("Source") is not None
    editor.close()


def test_node_editor_focuses_branch_without_mutating_graph(qapp) -> None:
    from src.features.composite.core.node_engine import NodeGraph
    from src.features.composite.nodes.blur_node import BlurNode
    from src.features.composite.nodes.constant_node import ConstantNode
    from src.features.composite.nodes.output_node import OutputNode
    from src.features.composite.ui.node_editor_widget import NodeEditorWidget

    graph = NodeGraph()
    source = ConstantNode(node_id="source")
    blur = BlurNode(node_id="blur")
    output = OutputNode(node_id="output")
    unrelated = ConstantNode(node_id="unrelated")
    for node in (source, blur, output, unrelated):
        graph.add_node(node)
    assert graph.connect("source", "result", "blur", "image")
    assert graph.connect("blur", "result", "output", "image")
    original_connections = list(graph.connections)

    editor = NodeEditorWidget()
    editor.build_from_graph(graph)
    assert editor.select_node("blur")
    focused = editor.isolate_selected_branch()
    assert focused == {"source", "blur", "output"}
    assert editor.graph_focus_ids == focused
    assert editor._node_items["unrelated"].opacity() == 0.12
    assert editor._node_items["blur"].opacity() == 1.0

    editor.clear_graph_focus()
    assert editor.graph_focus_ids is None
    assert all(item.opacity() == 1.0 for item in editor._node_items.values())
    assert graph.connections == original_connections
    editor.close()


def test_node_editor_displays_grouped_preflight_diagnostics(qapp) -> None:
    from src.features.composite.core.graph_preflight import PreflightIssue
    from src.features.composite.core.node_engine import NodeGraph
    from src.features.composite.nodes.constant_node import ConstantNode
    from src.features.composite.nodes.output_node import OutputNode
    from src.features.composite.ui.node_editor_widget import NodeEditorWidget

    graph = NodeGraph()
    source = ConstantNode(node_id="source")
    output = OutputNode(node_id="output")
    graph.add_node(source)
    graph.add_node(output)
    assert graph.connect("source", "result", "output", "image")

    editor = NodeEditorWidget()
    editor.build_from_graph(graph)
    editor.set_node_diagnostics([
        PreflightIssue("source.missing", "warning", "素材不存在", "source",
                       "重新指定素材"),
        PreflightIssue("source.contract", "error", "端口契约无效", "source"),
    ])
    item = editor.get_node_item("source")
    assert item is not None
    assert item.diagnostic_level == "error"
    assert "source.missing" in item.diagnostic_message
    assert "重新指定素材" in item.toolTip()

    editor.clear_node_diagnostics()
    assert item.diagnostic_level == ""
    editor.close()
