"""P1 view tests: KeyframesPanel shared selection + expression provider (T11/T13/T49 host).

Verifies the panel now owns a shared AnimationSelectionModel, mirrors its
legacy per-point selection into it on every rebuild/gesture, installs the
expression-status provider on the tree, and exposes hooks for the Spline
panel / Graph Editor / AI to read+write the same selection.
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.ui.keyframes_panel import KeyframesPanel
from src.features.composite.ui.timeline_view_model import (
    AnimationSelectionModel,
    make_node_row_id,
    make_param_row_id,
)
from src.shared.keyframe import Keyframe, KeyframeTrack


CLIP_FRAMES = 100
CLIP_FPS = 24.0


@pytest.fixture
def graph():
    g = NodeGraph()
    g.add_node(TransformNode())
    return g


@pytest.fixture
def panel(qapp, graph):
    p = KeyframesPanel()
    p.resize(1000, 300)

    def on_track_changed(node_id, param_name, keyframes):
        node = graph.get_node(node_id)
        if node is None or not keyframes:
            return
        track = KeyframeTrack(param_name=param_name,
                              min_val=-360.0, max_val=360.0)
        for kf_data in keyframes:
            track.add_keyframe(Keyframe.from_dict(kf_data))
        node.set_keyframe_track(param_name, track)
    p.track_changed.connect(on_track_changed)

    p.set_node_graph(graph)
    p.set_frame_range(0, CLIP_FRAMES - 1, fps=CLIP_FPS)
    return p


def _seed_rotation(graph, frames=(0, 25, 50)):
    node = graph.nodes[next(iter(graph.nodes))]
    track = KeyframeTrack(param_name="rotation",
                          min_val=-360.0, max_val=360.0)
    for f in frames:
        track.add_keyframe(Keyframe(f, float(f)))
    node.set_keyframe_track("rotation", track)
    return node


# ── T11/T13: shared selection model ────────────────────────────────────


class TestSharedSelectionModel:
    def test_panel_exposes_shared_selection_model(self, panel):
        sm = panel.selection_model
        assert isinstance(sm, AnimationSelectionModel)

    def test_rebuild_mirrors_node_row_selection(self, panel, graph):
        """set_node_graph with a selected node mirrors the row flag into the
        shared model so subscribers see it without re-querying."""
        node = graph.nodes[next(iter(graph.nodes))]
        panel.set_node_graph(graph, selected_node_id=node.id)
        sm = panel.selection_model
        assert make_node_row_id(node.id) in sm.selection.rows

    def test_param_selection_updates_shared_model(self, panel, graph):
        node = _seed_rotation(graph)
        panel.set_node_graph(graph)  # rebuild picks up the track
        panel._on_param_selected(node.id, "rotation")
        sm = panel.selection_model
        assert make_param_row_id(node.id, "rotation") in sm.selection.rows

    def test_subscribers_receive_selection_updates(self, panel, graph):
        """A subscribed Spline panel / Graph Editor gets notified when the
        Keyframes selection changes (T13 bidirectional sync prep)."""
        node = graph.nodes[next(iter(graph.nodes))]
        events = []
        panel.selection_model.subscribe(events.append)
        panel.set_node_graph(graph, selected_node_id=node.id)
        # at least one notification fired
        assert len(events) >= 1
        assert any(make_node_row_id(node.id) in ev.rows for ev in events)

    def test_apply_shared_selection_pushes_back_to_views(self, panel, graph):
        """Another surface changes the shared selection; the panel reflects
        it on its tree + timeline."""
        node = _seed_rotation(graph)
        panel.set_node_graph(graph)
        sm = panel.selection_model
        # Simulate the Spline panel selecting the rotation row.
        sm.clear()
        sm.select_row(make_param_row_id(node.id, "rotation"))
        panel.apply_shared_selection_to_views()
        # The model's row flag now matches the shared selection.
        param_row = next(r for r in panel._model.rows
                         if r.row_type == "param"
                         and r.node_id == node.id
                         and r.param_name == "rotation")
        assert param_row.selected is True


# ── T49 host: expression-status provider ───────────────────────────────


class TestExpressionStatusProvider:
    def test_provider_returns_none_without_graph(self, panel):
        panel._node_graph = None
        assert panel._expression_status_for("x", "y") is None

    def test_provider_returns_none_for_unknown_node(self, panel, graph):
        panel.set_node_graph(graph)
        assert panel._expression_status_for("no_such_node", "rotation") is None

    def test_provider_returns_status_for_real_expression(self, panel, graph):
        from src.features.composite.ui.timeline_expression import (
            EXPRESSION_VALID)
        node = graph.nodes[next(iter(graph.nodes))]
        node._expressions = {"rotation": "value + 10"}
        panel.set_node_graph(graph)
        status = panel._expression_status_for(node.id, "rotation")
        assert status is not None
        assert status.state == EXPRESSION_VALID

    def test_provider_surfaces_runtime_expression_error(self, panel, graph):
        from src.features.composite.ui.timeline_expression import (
            EXPRESSION_ERROR)
        node = graph.nodes[next(iter(graph.nodes))]
        assert node.set_expression("rotation", "Missing.radius")
        panel.set_node_graph(graph)
        status = panel._expression_status_for(node.id, "rotation")
        assert status is not None
        assert status.state == EXPRESSION_ERROR
        assert "Missing" in status.error_message

    def test_provider_returns_none_for_param_without_expression(
            self, panel, graph):
        node = graph.nodes[next(iter(graph.nodes))]
        assert not node.has_expression("rotation")
        panel.set_node_graph(graph)
        status = panel._expression_status_for(node.id, "rotation")
        # No expression → derive returns a NONE status object; the tree
        # provider treats that as "no marker".
        assert status is not None
        assert status.state == "none"

    def test_tree_view_has_provider_installed(self, panel):
        """The tree view's provider must be wired so the marker actually
        renders (not just the panel exposing the helper)."""
        assert panel._tree_view._expression_status_provider is not None
        # And it's bound to the panel's helper.
        assert (panel._tree_view._expression_status_provider
                == panel._expression_status_for)

    def test_buggy_node_getter_does_not_crash_provider(self, panel):
        class _BrokenGraph:
            def get_node(self, node_id):
                raise RuntimeError("boom")
        panel._node_graph = _BrokenGraph()
        assert panel._expression_status_for("x", "y") is None


# ── T11/T13: bidirectional contract ────────────────────────────────────


class TestBidirectionalContract:
    def test_sync_then_apply_round_trips(self, panel, graph):
        """The mirror is reversible: sync reads flags into the shared model,
        apply writes them back. A full round-trip preserves the selection."""
        node = _seed_rotation(graph)
        panel.set_node_graph(graph)
        panel._on_param_selected(node.id, "rotation")
        sm = panel.selection_model
        # Snapshot the shared selection.
        rows_before = set(sm.selection.rows)
        # Mutate the model flags out from under us, then re-apply.
        for r in panel._model.rows:
            r.selected = False
        panel.apply_shared_selection_to_views()
        # The apply restored the selected rows.
        selected_after = {r.row_id for r in panel._model.rows if r.selected}
        assert rows_before <= selected_after
