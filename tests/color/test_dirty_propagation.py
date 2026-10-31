"""
Tests for fine-grained dirty propagation in RenderScheduler.plan().

Covers:
- First plan marks all nodes dirty
- Same snapshot twice → no dirty nodes
- Parameter change → only that node + downstream dirty
- Disabled toggle → that node + downstream dirty
- Topology change (connect/disconnect) → affected node + downstream dirty
- Color revision change → color_input nodes + downstream dirty
- Dirty propagation stops at disconnected branches
- New node added → new node dirty
- Node removed → tracking cleaned up
- reset_dirty_tracking() resets to "all dirty"
- Downstream propagation chains (A → B → C → D)
"""

import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.render.render_scheduler import RenderScheduler, RenderPlan, NodePlan
from src.features.composite.core.graph_snapshot import GraphSnapshot, NodeSnapshot, ConnectionSnapshot

import pytest
pytestmark = pytest.mark.composite_core


def _make_snapshot(nodes=None, connections=None, eval_order=None, **kwargs):
    """Helper to create a GraphSnapshot with sensible defaults."""
    node_dict = {}
    if nodes:
        for n in nodes:
            if isinstance(n, dict):
                ns = NodeSnapshot(**n)
            else:
                ns = n
            node_dict[ns.node_id] = ns

    return GraphSnapshot(
        generation=kwargs.get('generation', 1),
        nodes=node_dict,
        connections=connections or [],
        eval_order=eval_order or list(node_dict.keys()),
        color_texture_id=kwargs.get('color_texture_id', 0),
        color_width=kwargs.get('color_width', 0),
        color_height=kwargs.get('color_height', 0),
        color_revision=kwargs.get('color_revision', 0),
        topology_revision=kwargs.get('topology_revision', 0),
    )


def _node(node_id="n1", node_type="blur", params=None, disabled=False,
          param_revision=1, **kwargs):
    """Shorthand to create a NodeSnapshot dict."""
    return {
        "node_id": node_id,
        "node_type": node_type,
        "parameters": params or {"radius": 5.0},
        "disabled": disabled,
        "input_ports": kwargs.get("input_ports", [("image", "image")]),
        "output_ports": kwargs.get("output_ports", [("result", "image")]),
        "param_revision": param_revision,
    }


def _conn(src, src_port="result", dst="n1", dst_port="image"):
    return ConnectionSnapshot(src, src_port, dst, dst_port)


# ── First Plan ──

class TestFirstPlan:
    """First plan generation: all nodes dirty."""

    def test_first_plan_all_dirty(self):
        scheduler = RenderScheduler()
        snapshot = _make_snapshot(
            nodes=[_node("a"), _node("b"), _node("c")],
        )
        plan = scheduler.plan(snapshot, "preview")
        assert sorted(plan.dirty_node_ids) == ["a", "b", "c"]

    def test_empty_graph_no_dirty(self):
        scheduler = RenderScheduler()
        snapshot = _make_snapshot(nodes=[])
        plan = scheduler.plan(snapshot, "preview")
        assert plan.dirty_node_ids == []


# ── No Change ──

class TestNoChange:
    """Same snapshot twice → no nodes dirty."""

    def test_same_snapshot_no_dirty(self):
        scheduler = RenderScheduler()
        nodes = [_node("a"), _node("b")]
        snapshot = _make_snapshot(nodes=nodes)

        plan1 = scheduler.plan(snapshot, "preview")
        assert len(plan1.dirty_node_ids) == 2  # first plan

        plan2 = scheduler.plan(snapshot, "preview")
        assert plan2.dirty_node_ids == []

    def test_same_snapshot_three_times(self):
        scheduler = RenderScheduler()
        nodes = [_node("a"), _node("b"), _node("c")]
        snapshot = _make_snapshot(nodes=nodes)

        scheduler.plan(snapshot, "preview")
        plan2 = scheduler.plan(snapshot, "preview")
        assert plan2.dirty_node_ids == []

        plan3 = scheduler.plan(snapshot, "preview")
        assert plan3.dirty_node_ids == []


# ── Parameter Change ──

class TestParamChange:
    """Parameter revision change → that node + downstream dirty."""

    def test_single_node_param_change(self):
        scheduler = RenderScheduler()
        snap1 = _make_snapshot(nodes=[_node("a", "blur", param_revision=1)])
        scheduler.plan(snap1, "preview")

        snap2 = _make_snapshot(nodes=[_node("a", "blur", param_revision=2)])
        plan = scheduler.plan(snap2, "preview")
        assert plan.dirty_node_ids == ["a"]

    def test_param_change_propagates_downstream(self):
        """A → B → C. Changing A's params dirties A, B, C."""
        scheduler = RenderScheduler()
        conns = [_conn("a", "result", "b"), _conn("b", "result", "c")]
        snap1 = _make_snapshot(
            nodes=[
                _node("a", "color_input", input_ports=[], param_revision=1),
                _node("b", "blur", param_revision=1),
                _node("c", "grade", param_revision=1),
            ],
            connections=conns,
            eval_order=["a", "b", "c"],
        )
        scheduler.plan(snap1, "preview")

        # Change A's params
        snap2 = _make_snapshot(
            nodes=[
                _node("a", "color_input", input_ports=[], param_revision=2),
                _node("b", "blur", param_revision=1),
                _node("c", "grade", param_revision=1),
            ],
            connections=conns,
            eval_order=["a", "b", "c"],
        )
        plan = scheduler.plan(snap2, "preview")
        assert sorted(plan.dirty_node_ids) == ["a", "b", "c"]

    def test_param_change_only_mid_chain(self):
        """A → B → C. Changing B's params dirties B, C only."""
        scheduler = RenderScheduler()
        conns = [_conn("a", "result", "b"), _conn("b", "result", "c")]
        snap1 = _make_snapshot(
            nodes=[
                _node("a", "color_input", input_ports=[], param_revision=1),
                _node("b", "blur", param_revision=1),
                _node("c", "grade", param_revision=1),
            ],
            connections=conns,
            eval_order=["a", "b", "c"],
        )
        scheduler.plan(snap1, "preview")

        # Change B's params only
        snap2 = _make_snapshot(
            nodes=[
                _node("a", "color_input", input_ports=[], param_revision=1),
                _node("b", "blur", param_revision=2),
                _node("c", "grade", param_revision=1),
            ],
            connections=conns,
            eval_order=["a", "b", "c"],
        )
        plan = scheduler.plan(snap2, "preview")
        assert sorted(plan.dirty_node_ids) == ["b", "c"]

    def test_param_change_does_not_affect_parallel_branch(self):
        """A → B (dirty), C (parallel, not dirty). Only B is dirty."""
        scheduler = RenderScheduler()
        snap1 = _make_snapshot(
            nodes=[
                _node("a", "blur", param_revision=1),
                _node("b", "grade", param_revision=1),
            ],
        )
        scheduler.plan(snap1, "preview")

        snap2 = _make_snapshot(
            nodes=[
                _node("a", "blur", param_revision=2),
                _node("b", "grade", param_revision=1),
            ],
        )
        plan = scheduler.plan(snap2, "preview")
        assert plan.dirty_node_ids == ["a"]


# ── Disabled Toggle ──

class TestDisabledToggle:
    """Disabled flag change → that node + downstream dirty."""

    def test_enable_node(self):
        scheduler = RenderScheduler()
        snap1 = _make_snapshot(nodes=[_node("a", disabled=True)])
        scheduler.plan(snap1, "preview")

        snap2 = _make_snapshot(nodes=[_node("a", disabled=False)])
        plan = scheduler.plan(snap2, "preview")
        assert plan.dirty_node_ids == ["a"]

    def test_disable_node_propagates_downstream(self):
        """A → B. Disabling A dirties A and B."""
        scheduler = RenderScheduler()
        conns = [_conn("a", "result", "b")]
        snap1 = _make_snapshot(
            nodes=[
                _node("a", "blur", disabled=False, param_revision=1),
                _node("b", "grade", param_revision=1),
            ],
            connections=conns,
            eval_order=["a", "b"],
        )
        scheduler.plan(snap1, "preview")

        snap2 = _make_snapshot(
            nodes=[
                _node("a", "blur", disabled=True, param_revision=1),
                _node("b", "grade", param_revision=1),
            ],
            connections=conns,
            eval_order=["a", "b"],
        )
        plan = scheduler.plan(snap2, "preview")
        assert sorted(plan.dirty_node_ids) == ["a", "b"]


# ── Topology Change ──

class TestTopologyChange:
    """Connection changes → affected node + downstream dirty."""

    def test_connect_new_input(self):
        """A → C (new connection). C gets dirty."""
        scheduler = RenderScheduler()
        # Initial: A and C exist, no connection
        snap1 = _make_snapshot(
            nodes=[
                _node("a", "color_input", input_ports=[], param_revision=1),
                _node("c", "blur", param_revision=1),
            ],
            eval_order=["a", "c"],
            topology_revision=0,
        )
        scheduler.plan(snap1, "preview")

        # Connect A → C
        snap2 = _make_snapshot(
            nodes=[
                _node("a", "color_input", input_ports=[], param_revision=1),
                _node("c", "blur", param_revision=1),
            ],
            connections=[_conn("a", "result", "c")],
            eval_order=["a", "c"],
            topology_revision=1,
        )
        plan = scheduler.plan(snap2, "preview")
        assert "c" in plan.dirty_node_ids

    def test_disconnect_input(self):
        """A → B (disconnect). B gets dirty."""
        scheduler = RenderScheduler()
        conns = [_conn("a", "result", "b")]
        snap1 = _make_snapshot(
            nodes=[
                _node("a", "color_input", input_ports=[], param_revision=1),
                _node("b", "blur", param_revision=1),
            ],
            connections=conns,
            eval_order=["a", "b"],
            topology_revision=0,
        )
        scheduler.plan(snap1, "preview")

        # Disconnect A → B
        snap2 = _make_snapshot(
            nodes=[
                _node("a", "color_input", input_ports=[], param_revision=1),
                _node("b", "blur", param_revision=1),
            ],
            connections=[],
            eval_order=["a", "b"],
            topology_revision=1,
        )
        plan = scheduler.plan(snap2, "preview")
        assert "b" in plan.dirty_node_ids

    def test_topology_change_propagates_downstream(self):
        """A → B → C. Disconnect A→B. B and C get dirty."""
        scheduler = RenderScheduler()
        conns = [_conn("a", "result", "b"), _conn("b", "result", "c")]
        snap1 = _make_snapshot(
            nodes=[
                _node("a", "color_input", input_ports=[], param_revision=1),
                _node("b", "blur", param_revision=1),
                _node("c", "grade", param_revision=1),
            ],
            connections=conns,
            eval_order=["a", "b", "c"],
            topology_revision=0,
        )
        scheduler.plan(snap1, "preview")

        # Disconnect A → B
        snap2 = _make_snapshot(
            nodes=[
                _node("a", "color_input", input_ports=[], param_revision=1),
                _node("b", "blur", param_revision=1),
                _node("c", "grade", param_revision=1),
            ],
            connections=[_conn("b", "result", "c")],
            eval_order=["a", "b", "c"],
            topology_revision=1,
        )
        plan = scheduler.plan(snap2, "preview")
        assert sorted(plan.dirty_node_ids) == ["b", "c"]

    def test_same_topology_no_dirty(self):
        """topology_revision unchanged → no topology-driven dirty."""
        scheduler = RenderScheduler()
        conns = [_conn("a", "result", "b")]
        snap1 = _make_snapshot(
            nodes=[
                _node("a", "color_input", input_ports=[], param_revision=1),
                _node("b", "blur", param_revision=1),
            ],
            connections=conns,
            eval_order=["a", "b"],
            topology_revision=0,
        )
        scheduler.plan(snap1, "preview")

        # Same topology, same params
        snap2 = _make_snapshot(
            nodes=[
                _node("a", "color_input", input_ports=[], param_revision=1),
                _node("b", "blur", param_revision=1),
            ],
            connections=conns,
            eval_order=["a", "b"],
            topology_revision=0,
        )
        plan = scheduler.plan(snap2, "preview")
        assert plan.dirty_node_ids == []


# ── Color Revision Change ──

class TestColorRevisionChange:
    """Color texture revision → color_input nodes + downstream dirty."""

    def test_color_revision_dirties_color_input(self):
        scheduler = RenderScheduler()
        snap1 = _make_snapshot(
            nodes=[
                _node("c1", "color_input", input_ports=[], param_revision=1),
                _node("b1", "blur", param_revision=1),
            ],
            connections=[_conn("c1", "result", "b1")],
            eval_order=["c1", "b1"],
            color_revision=0,
        )
        scheduler.plan(snap1, "preview")

        snap2 = _make_snapshot(
            nodes=[
                _node("c1", "color_input", input_ports=[], param_revision=1),
                _node("b1", "blur", param_revision=1),
            ],
            connections=[_conn("c1", "result", "b1")],
            eval_order=["c1", "b1"],
            color_revision=1,
        )
        plan = scheduler.plan(snap2, "preview")
        assert sorted(plan.dirty_node_ids) == ["b1", "c1"]

    def test_color_revision_does_not_affect_non_color_nodes(self):
        """Non-color nodes shouldn't be directly affected by color_revision."""
        scheduler = RenderScheduler()
        snap1 = _make_snapshot(
            nodes=[
                _node("b1", "blur", param_revision=1),
            ],
            color_revision=0,
        )
        scheduler.plan(snap1, "preview")

        snap2 = _make_snapshot(
            nodes=[
                _node("b1", "blur", param_revision=1),
            ],
            color_revision=1,
        )
        plan = scheduler.plan(snap2, "preview")
        assert plan.dirty_node_ids == []


# ── New / Removed Nodes ──

class TestNodeAddRemove:
    """Adding/removing nodes affects dirty set."""

    def test_new_node_is_dirty(self):
        scheduler = RenderScheduler()
        snap1 = _make_snapshot(nodes=[_node("a", param_revision=1)])
        scheduler.plan(snap1, "preview")

        snap2 = _make_snapshot(
            nodes=[_node("a", param_revision=1), _node("b", param_revision=1)],
            eval_order=["a", "b"],
        )
        plan = scheduler.plan(snap2, "preview")
        assert plan.dirty_node_ids == ["b"]

    def test_removed_node_tracking_cleaned(self):
        """After removing a node, a re-add should mark it dirty (new)."""
        scheduler = RenderScheduler()
        snap1 = _make_snapshot(
            nodes=[_node("a", param_revision=1), _node("b", param_revision=1)],
        )
        scheduler.plan(snap1, "preview")

        # Remove b
        snap2 = _make_snapshot(nodes=[_node("a", param_revision=1)])
        plan2 = scheduler.plan(snap2, "preview")
        assert plan2.dirty_node_ids == []

        # Re-add b
        snap3 = _make_snapshot(
            nodes=[_node("a", param_revision=1), _node("b", param_revision=1)],
        )
        plan3 = scheduler.plan(snap3, "preview")
        assert plan3.dirty_node_ids == ["b"]


# ── Downstream Propagation ──

class TestDownstreamPropagation:
    """Dirty propagation chains and branch behavior."""

    def test_long_chain(self):
        """A → B → C → D. Change A → all four dirty."""
        scheduler = RenderScheduler()
        conns = [
            _conn("a", "result", "b"),
            _conn("b", "result", "c"),
            _conn("c", "result", "d"),
        ]
        snap1 = _make_snapshot(
            nodes=[
                _node("a", "color_input", input_ports=[], param_revision=1),
                _node("b", "blur", param_revision=1),
                _node("c", "grade", param_revision=1),
                _node("d", "invert", param_revision=1),
            ],
            connections=conns,
            eval_order=["a", "b", "c", "d"],
        )
        scheduler.plan(snap1, "preview")

        snap2 = _make_snapshot(
            nodes=[
                _node("a", "color_input", input_ports=[], param_revision=2),
                _node("b", "blur", param_revision=1),
                _node("c", "grade", param_revision=1),
                _node("d", "invert", param_revision=1),
            ],
            connections=conns,
            eval_order=["a", "b", "c", "d"],
        )
        plan = scheduler.plan(snap2, "preview")
        assert sorted(plan.dirty_node_ids) == ["a", "b", "c", "d"]

    def test_branch_propagation(self):
        """A → B, A → C. Change A → A, B, C dirty."""
        scheduler = RenderScheduler()
        conns = [
            _conn("a", "result", "b"),
            _conn("a", "result", "c"),
        ]
        snap1 = _make_snapshot(
            nodes=[
                _node("a", "color_input", input_ports=[], param_revision=1),
                _node("b", "blur", param_revision=1),
                _node("c", "grade", param_revision=1),
            ],
            connections=conns,
            eval_order=["a", "b", "c"],
        )
        scheduler.plan(snap1, "preview")

        snap2 = _make_snapshot(
            nodes=[
                _node("a", "color_input", input_ports=[], param_revision=2),
                _node("b", "blur", param_revision=1),
                _node("c", "grade", param_revision=1),
            ],
            connections=conns,
            eval_order=["a", "b", "c"],
        )
        plan = scheduler.plan(snap2, "preview")
        assert sorted(plan.dirty_node_ids) == ["a", "b", "c"]

    def test_disconnected_branch_not_affected(self):
        """A → B, C (disconnected). Change A → only A, B dirty."""
        scheduler = RenderScheduler()
        conns = [_conn("a", "result", "b")]
        snap1 = _make_snapshot(
            nodes=[
                _node("a", "color_input", input_ports=[], param_revision=1),
                _node("b", "blur", param_revision=1),
                _node("c", "grade", param_revision=1),
            ],
            connections=conns,
            eval_order=["a", "b", "c"],
        )
        scheduler.plan(snap1, "preview")

        snap2 = _make_snapshot(
            nodes=[
                _node("a", "color_input", input_ports=[], param_revision=2),
                _node("b", "blur", param_revision=1),
                _node("c", "grade", param_revision=1),
            ],
            connections=conns,
            eval_order=["a", "b", "c"],
        )
        plan = scheduler.plan(snap2, "preview")
        assert sorted(plan.dirty_node_ids) == ["a", "b"]


# ── Reset ──

class TestResetDirtyTracking:
    """reset_dirty_tracking() forces all nodes dirty on next plan."""

    def test_reset_forces_all_dirty(self):
        scheduler = RenderScheduler()
        snap = _make_snapshot(nodes=[_node("a"), _node("b")])
        scheduler.plan(snap, "preview")

        # Same snapshot → no dirty
        plan = scheduler.plan(snap, "preview")
        assert plan.dirty_node_ids == []

        # Reset → all dirty
        scheduler.reset_dirty_tracking()
        plan = scheduler.plan(snap, "preview")
        assert sorted(plan.dirty_node_ids) == ["a", "b"]
