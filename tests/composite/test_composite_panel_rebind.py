"""Regression: properties panel must rebind to a live node after a graph switch.

Root cause of the drag-break bug: ``switch_to_color_output_graph`` /
``switch_to_timeline_input_graph`` clear+rebuild the node graph but left the
panel's ``_current_node_id`` pointing at a now-deleted node. DragValue drags
then emitted ``parameter_changed`` against a dead id and did nothing — so
dragging a value box stopped working once a 2nd image had been opened.

The fix is ``CompositePage._rebind_properties_panel_to_live_node`` (called at
the end of both switch methods). These tests exercise it via the lightweight
unbound-method + SimpleNamespace-page pattern used in
tests/app/test_composite_entry_source_selection.py (avoids GL/heavy setup).
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace

import pytest

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.ui.composite_page import CompositePage
from src.features.composite.ui.node_properties_panel import NodePropertiesPanel
from src.features.composite.nodes.transform_node import TransformNode


@pytest.fixture
def page(qapp):
    """A CompositePage stand-in: a real graph with one Transform node + a real
    properties panel, wired so the unbound CompositePage selection methods run
    against it (``_on_node_selected`` is rebound onto the namespace)."""
    graph = NodeGraph()
    node = TransformNode()
    graph.add_node(node)
    panel = NodePropertiesPanel()
    page = SimpleNamespace(
        _node_graph=graph,
        _properties_panel=panel,
        _viewer=None,
        _thumbnail_manager=None,
        _last_selected_node_id=None,
    )
    # The rebind helper calls self._on_node_selected(...); delegate to the real
    # CompositePage method bound to this namespace.
    page._on_node_selected = lambda nid: CompositePage._on_node_selected(page, nid)
    page.live_node_id = node.id
    return page


def test_rebind_moves_panel_off_a_dead_node(page):
    """After a graph switch the panel can be bound to a node that no longer
    exists. The rebind must repoint it at a live node."""
    page._properties_panel._current_node_id = "DEAD_NODE_GONE"

    CompositePage._rebind_properties_panel_to_live_node(page)

    live_id = page._properties_panel._current_node_id
    assert live_id is not None
    assert live_id != "DEAD_NODE_GONE"
    assert page._node_graph.get_node(live_id) is not None


def test_rebind_prefers_last_selected_when_still_present(page):
    """If the previously-selected node still exists in the new graph, keep it."""
    page._properties_panel._current_node_id = "DEAD_NODE_GONE"
    page._last_selected_node_id = page.live_node_id

    CompositePage._rebind_properties_panel_to_live_node(page)

    assert page._properties_panel._current_node_id == page.live_node_id


def test_drag_after_rebind_emits_against_live_node(page):
    """End-to-end of the reported bug: after the rebind, a parameter change is
    emitted against a node that exists in the graph (not a dead id), so the
    drag actually takes effect."""
    page._properties_panel._current_node_id = "DEAD_NODE_GONE"
    CompositePage._rebind_properties_panel_to_live_node(page)

    emitted = []
    page._properties_panel.parameter_changed.connect(
        lambda nid, key, value: emitted.append((nid, key, value)))

    # Simulate a DragValue valueChanged → _on_param_changed path.
    page._properties_panel._on_param_changed("translate_x", 5.0)

    assert emitted, "parameter_changed was not emitted"
    emitted_id = emitted[0][0]
    assert page._node_graph.get_node(emitted_id) is not None, (
        "drag emitted against a dead node id")


def test_tracker_action_signals_are_relayed(qapp):
    """Tracker actions from a node section reach the panel-level API."""
    panel = NodePropertiesPanel()
    section = panel._create_section({
        "node_id": "tracker-1",
        "node_type": "tracker",
        "node_title": "Tracker",
        "parameters": {},
    })
    emitted = []
    panel.tracker_analyze_requested.connect(emitted.append)

    section.tracker_analyze_requested.emit("tracker-1")

    assert emitted == ["tracker-1"]
