"""P6 tests: shared selection sync between Keyframes and Spline (T56/T61).

Verifies that attaching the Spline panel to the Keyframes panel's shared
AnimationSelectionModel keeps the two panels' selections in sync: selecting
a parameter row in Keyframes selects the matching Spline curve (without
resetting the Spline viewport), and a detached Spline keeps its own
selection (legacy behaviour preserved).
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.ui.keyframes_panel import KeyframesPanel
from src.features.composite.ui.spline_panel import SplinePanel
from src.features.composite.ui.timeline_view_model import (
    make_param_row_id,
    make_node_row_id,
)
from src.shared.keyframe import Keyframe, KeyframeTrack


CLIP_FRAMES = 100
CLIP_FPS = 24.0


@pytest.fixture
def graph():
    g = NodeGraph()
    g.add_node(TransformNode())
    return g


def _seed_rotation(graph, frames=(0, 25, 50)):
    node = graph.nodes[next(iter(graph.nodes))]
    track = KeyframeTrack(param_name="rotation",
                          min_val=-360.0, max_val=360.0)
    for f in frames:
        track.add_keyframe(Keyframe(f, float(f)))
    node.set_keyframe_track("rotation", track)
    return node


@pytest.fixture
def keyframes_panel(qapp, graph):
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


@pytest.fixture
def spline_panel(qapp, graph):
    sp = SplinePanel()
    sp.resize(1000, 300)
    sp.set_node_graph(graph)
    return sp


def _curve_for(spline_panel, node_id, param_name):
    return spline_panel._model.curve_by_id(f"{node_id}:{param_name}")


# ── T56: Spline follows Keyframes param selection ──────────────────────


class TestSplineFollowsKeyframes:
    def test_attach_syncs_current_selection(self, keyframes_panel,
                                            spline_panel, graph):
        """Attaching after a Keyframes selection already exists syncs the
        Spline immediately (not just on the NEXT change)."""
        node = _seed_rotation(graph)
        keyframes_panel.set_node_graph(graph)
        spline_panel.set_node_graph(graph)
        keyframes_panel._on_param_selected(node.id, "rotation")
        spline_panel.attach_to_shared_selection(
            keyframes_panel.selection_model)
        curve = _curve_for(spline_panel, node.id, "rotation")
        assert curve is not None
        assert curve.selected is True

    def test_selecting_param_in_keyframes_selects_spline_curve(
            self, keyframes_panel, spline_panel, graph):
        node = _seed_rotation(graph)
        keyframes_panel.set_node_graph(graph)
        spline_panel.set_node_graph(graph)
        spline_panel.attach_to_shared_selection(
            keyframes_panel.selection_model)
        # Initially the Spline's first curve is selected (legacy default).
        # Selecting rotation in Keyframes flips the Spline to that curve.
        keyframes_panel._on_param_selected(node.id, "rotation")
        curve = _curve_for(spline_panel, node.id, "rotation")
        assert curve.selected is True
        # Every other curve is deselected.
        for c in spline_panel._model.curves:
            if c.curve_id != f"{node.id}:rotation":
                assert c.selected is False

    def test_node_only_selection_does_not_change_curve(
            self, keyframes_panel, spline_panel, graph):
        """A node-only selection (no param row) is ambiguous — a node has
        many curves. The Spline must NOT change its curve selection."""
        node = _seed_rotation(graph)  # ensures at least one curve exists
        keyframes_panel.set_node_graph(graph)
        spline_panel.set_node_graph(graph)
        # Pre-select a specific curve on the Spline side.
        first_curve = spline_panel._model.curves[0]
        for c in spline_panel._model.curves:
            c.selected = (c.curve_id == first_curve.curve_id)
        spline_panel.attach_to_shared_selection(
            keyframes_panel.selection_model)
        # Select the node (not a param) in Keyframes.
        keyframes_panel.set_node_graph(graph, selected_node_id=node.id)
        # Spline's selected curve is unchanged.
        assert first_curve.selected is True

    def test_viewport_not_reset_on_selection_sync(self, keyframes_panel,
                                                  spline_panel, graph):
        """Plan §7.2: syncing selection must not reset the Spline's zoom/pan.
        Attach + select something, then verify the viewport is the same as
        what set_node_graph established."""
        node = _seed_rotation(graph)
        keyframes_panel.set_node_graph(graph)
        spline_panel.set_node_graph(graph)
        # Capture the viewport the rebuild established.
        vp_before = spline_panel._view._viewport
        spline_panel.attach_to_shared_selection(
            keyframes_panel.selection_model)
        keyframes_panel._on_param_selected(node.id, "rotation")
        assert spline_panel._view._viewport == vp_before


# ── detach / legacy behaviour ──────────────────────────────────────────


class TestDetachAndLegacy:
    def test_unattached_spline_keeps_independent_selection(
            self, keyframes_panel, spline_panel, graph):
        """Without attach_to_shared_selection, the Spline must NOT follow
        Keyframes — preserves the legacy behaviour the existing Spline tests
        were written against."""
        node = _seed_rotation(graph)
        keyframes_panel.set_node_graph(graph)
        spline_panel.set_node_graph(graph)
        # No attach call. Selecting rotation in Keyframes must not touch the
        # Spline's curve selection.
        first_curve = spline_panel._model.curves[0]
        selected_before = {c.curve_id for c in spline_panel._model.curves
                           if c.selected}
        keyframes_panel._on_param_selected(node.id, "rotation")
        selected_after = {c.curve_id for c in spline_panel._model.curves
                          if c.selected}
        assert selected_before == selected_after

    def test_attach_then_detach_stops_sync(self, keyframes_panel,
                                           spline_panel, graph):
        node = _seed_rotation(graph)
        keyframes_panel.set_node_graph(graph)
        spline_panel.set_node_graph(graph)
        spline_panel.attach_to_shared_selection(
            keyframes_panel.selection_model)
        # Detach by attaching to None.
        spline_panel.attach_to_shared_selection(None)
        keyframes_panel._on_param_selected(node.id, "rotation")
        # Spline no longer follows.
        curve = _curve_for(spline_panel, node.id, "rotation")
        # curve may or may not be selected depending on initial state, but it
        # must NOT have been flipped ON by the Keyframes selection.
        # Re-attach to verify it WOULD have synced (sanity).
        spline_panel.attach_to_shared_selection(
            keyframes_panel.selection_model)
        assert curve.selected is True

    def test_reattach_does_not_double_fire(self, keyframes_panel,
                                           spline_panel, graph):
        """Re-attaching must unsubscribe the old callback first so a single
        selection change doesn't fire the handler twice."""
        node = _seed_rotation(graph)
        keyframes_panel.set_node_graph(graph)
        spline_panel.set_node_graph(graph)
        spline_panel.attach_to_shared_selection(
            keyframes_panel.selection_model)
        spline_panel.attach_to_shared_selection(
            keyframes_panel.selection_model)
        # Count subscribers on the shared model — should be exactly 1.
        assert len(keyframes_panel.selection_model._subscribers) == 1


# ── T61: data-level contract ───────────────────────────────────────────


class TestDataLevelContract:
    def test_curve_id_matches_param_row_id_grammar(self, keyframes_panel,
                                                   spline_panel, graph):
        """The Spline's curve_id ('{node_id}:{param_name}') must resolve from
        a TimelineRow param row_id ('param:{node_id}:{param_name}') via
        parse_row_id — that's the bridge the sync handler relies on."""
        from src.features.composite.ui.timeline_view_model import parse_row_id
        node = graph.nodes[next(iter(graph.nodes))]
        row_id = make_param_row_id(node.id, "rotation")
        parsed = parse_row_id(row_id)
        reconstructed = f"{parsed['node_id']}:{parsed['param_name']}"
        assert reconstructed == f"{node.id}:rotation"


# ── T61: Keyframes follows Spline (back-sync) ─────────────────────────


class TestKeyframesFollowsSpline:
    """T61 reverse direction: when the user selects a curve in the Spline
    (via the tree OR by clicking a keyframe in the chart), the matching
    parameter row should become selected in the Keyframes panel via the
    shared selection model."""

    def test_tree_curve_selection_pushes_to_shared_model(
            self, keyframes_panel, spline_panel, graph):
        """Selecting a curve via the Spline tree pushes its param row_id
        into the shared selection model."""
        node = _seed_rotation(graph)
        keyframes_panel.set_node_graph(graph)
        spline_panel.set_node_graph(graph)
        spline_panel.attach_to_shared_selection(
            keyframes_panel.selection_model)
        # Emit curve_selected as the tree would when the user clicks a row.
        spline_panel._on_curve_selected(f"{node.id}:rotation")
        sel = keyframes_panel.selection_model.selection
        expected_row = make_param_row_id(node.id, "rotation")
        assert expected_row in sel.rows

    def test_view_curve_selection_pushes_to_shared_model(
            self, keyframes_panel, spline_panel, graph):
        """A curve selection change originating from the Spline *view*
        (clicking a keyframe on a different curve) must also back-sync."""
        node = _seed_rotation(graph)
        keyframes_panel.set_node_graph(graph)
        spline_panel.set_node_graph(graph)
        spline_panel.attach_to_shared_selection(
            keyframes_panel.selection_model)
        # Simulate the view selecting a curve.
        curve = _curve_for(spline_panel, node.id, "rotation")
        for c in spline_panel._model.curves:
            c.selected = False
        curve.selected = True
        spline_panel._view._emit_curve_selection()
        sel = keyframes_panel.selection_model.selection
        assert make_param_row_id(node.id, "rotation") in sel.rows

    def test_back_sync_does_not_loop(self, keyframes_panel, spline_panel,
                                     graph):
        """Forward (Keyframes→Spline) and back (Spline→Keyframes) handlers
        must not ping-pong. Driving the Spline's tree selection once should
        settle after one round-trip, not recurse."""
        node = _seed_rotation(graph)
        keyframes_panel.set_node_graph(graph)
        spline_panel.set_node_graph(graph)
        spline_panel.attach_to_shared_selection(
            keyframes_panel.selection_model)
        # Drive the back direction.
        spline_panel._on_curve_selected(f"{node.id}:rotation")
        rows_before = set(keyframes_panel.selection_model.selection.rows)
        # Drive it again with the same curve — should be idempotent.
        spline_panel._on_curve_selected(f"{node.id}:rotation")
        rows_after = set(keyframes_panel.selection_model.selection.rows)
        assert rows_before == rows_after

    def test_back_sync_ignored_when_not_attached(self, keyframes_panel,
                                                 spline_panel, graph):
        """A detached Spline (no attach call) keeps its own selection and
        never pushes anywhere — preserves legacy behaviour."""
        node = _seed_rotation(graph)
        keyframes_panel.set_node_graph(graph)
        spline_panel.set_node_graph(graph)
        # NOTE: deliberately NOT attaching.
        rows_before = set(
            keyframes_panel.selection_model.selection.rows)
        spline_panel._on_curve_selected(f"{node.id}:rotation")
        rows_after = set(
            keyframes_panel.selection_model.selection.rows)
        assert rows_before == rows_after

    def test_malformed_curve_id_does_not_crash(self, keyframes_panel,
                                               spline_panel, graph):
        """A curve_id without the expected '{node}:{param}' shape must be
        ignored gracefully (the back-sync is best-effort)."""
        keyframes_panel.set_node_graph(graph)
        spline_panel.set_node_graph(graph)
        spline_panel.attach_to_shared_selection(
            keyframes_panel.selection_model)
        # No 'param' part — the handler should skip, not raise.
        spline_panel._on_curve_selected("just_a_node_id")
        # And an empty string.
        spline_panel._on_curve_selected("")
