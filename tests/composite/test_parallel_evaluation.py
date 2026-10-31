"""
Tests for parallel node evaluation — dependency levels and parallel groups.

Covers:
- Dependency level computation for source nodes (level 0)
- Dependency level for single-chain nodes (1, 2, 3, ...)
- Dependency level for merge nodes (multiple inputs)
- Parallel group construction
- Max parallelism calculation
- Complex graphs with multiple branches
"""

import sys
import os

# Try to import pytest, fall back to simple test runner if not available
try:
    import pytest
    HAS_PYTEST = True
except ImportError:
    HAS_PYTEST = False

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.render.render_scheduler import (
    RenderScheduler, RenderPlan, NodePlan,
    _GPU_STATEFUL_TYPES,
)
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
        color_width=kwargs.get('color_width', 1920),
        color_height=kwargs.get('color_height', 1080),
        color_revision=kwargs.get('color_revision', 0),
    )


def _node(node_id="n1", node_type="blur", params=None, disabled=False, **kwargs):
    """Shorthand to create a NodeSnapshot dict."""
    return {
        "node_id": node_id,
        "node_type": node_type,
        "parameters": params or {"radius": 5.0},
        "disabled": disabled,
        "input_ports": kwargs.get("input_ports", [("image", "image")]),
        "output_ports": kwargs.get("output_ports", [("result", "image")]),
    }


# ── Dependency Level Tests ──

class TestDependencyLevel:
    """Verify dependency level computation."""

    def test_source_nodes_level_0(self):
        """Source nodes (color_input, constant, image_input) have level 0."""
        scheduler = RenderScheduler()
        snapshot = _make_snapshot(
            nodes=[
                _node("c1", "color_input", input_ports=[], output_ports=[("result", "image")]),
                _node("c2", "constant", input_ports=[], output_ports=[("result", "image")]),
                _node("img1", "image_input", input_ports=[], output_ports=[("result", "image")]),
            ],
        )
        plan = scheduler.plan(snapshot, "preview")
        assert plan.node_plans["c1"].dependency_level == 0
        assert plan.node_plans["c2"].dependency_level == 0
        assert plan.node_plans["img1"].dependency_level == 0

    def test_single_chain_levels(self):
        """Nodes in a single chain have increasing levels."""
        scheduler = RenderScheduler()
        snapshot = _make_snapshot(
            nodes=[
                _node("c1", "color_input", input_ports=[], output_ports=[("result", "image")]),
                _node("b1", "blur"),
                _node("b2", "blur"),
                _node("b3", "blur"),
            ],
            connections=[
                ConnectionSnapshot("c1", "result", "b1", "image"),
                ConnectionSnapshot("b1", "result", "b2", "image"),
                ConnectionSnapshot("b2", "result", "b3", "image"),
            ],
            eval_order=["c1", "b1", "b2", "b3"],
        )
        plan = scheduler.plan(snapshot, "preview")
        assert plan.node_plans["c1"].dependency_level == 0
        assert plan.node_plans["b1"].dependency_level == 1
        assert plan.node_plans["b2"].dependency_level == 2
        assert plan.node_plans["b3"].dependency_level == 3

    def test_parallel_branches_same_level(self):
        """Nodes in independent branches have the same level."""
        scheduler = RenderScheduler()
        snapshot = _make_snapshot(
            nodes=[
                _node("c1", "color_input", input_ports=[], output_ports=[("result", "image")]),
                _node("b1", "blur"),
                _node("b2", "blur"),
                _node("m1", "merge", input_ports=[("image_a", "image"), ("image_b", "image")]),
            ],
            connections=[
                ConnectionSnapshot("c1", "result", "b1", "image"),
                ConnectionSnapshot("c1", "result", "b2", "image"),
                ConnectionSnapshot("b1", "result", "m1", "image_a"),
                ConnectionSnapshot("b2", "result", "m1", "image_b"),
            ],
            eval_order=["c1", "b1", "b2", "m1"],
        )
        plan = scheduler.plan(snapshot, "preview")
        assert plan.node_plans["c1"].dependency_level == 0
        # Both b1 and b2 depend only on c1 (level 0) → level 1
        assert plan.node_plans["b1"].dependency_level == 1
        assert plan.node_plans["b2"].dependency_level == 1
        # m1 depends on b1 and b2 (level 1) → level 2
        assert plan.node_plans["m1"].dependency_level == 2

    def test_merge_node_level_from_max_input(self):
        """Merge node level = max(upstream_levels) + 1."""
        scheduler = RenderScheduler()
        snapshot = _make_snapshot(
            nodes=[
                _node("c1", "color_input", input_ports=[], output_ports=[("result", "image")]),
                _node("b1", "blur"),  # level 1
                _node("b2", "blur"),  # level 1
                _node("b3", "blur"),  # level 2 (after b1)
                _node("m1", "merge", input_ports=[("image_a", "image"), ("image_b", "image")]),
            ],
            connections=[
                ConnectionSnapshot("c1", "result", "b1", "image"),
                ConnectionSnapshot("c1", "result", "b2", "image"),
                ConnectionSnapshot("b1", "result", "b3", "image"),
                ConnectionSnapshot("b2", "result", "m1", "image_a"),
                ConnectionSnapshot("b3", "result", "m1", "image_b"),
            ],
            eval_order=["c1", "b1", "b2", "b3", "m1"],
        )
        plan = scheduler.plan(snapshot, "preview")
        assert plan.node_plans["c1"].dependency_level == 0
        assert plan.node_plans["b1"].dependency_level == 1
        assert plan.node_plans["b2"].dependency_level == 1
        assert plan.node_plans["b3"].dependency_level == 2
        # m1 depends on b2 (level 1) and b3 (level 2) → level 3
        assert plan.node_plans["m1"].dependency_level == 3

    def test_no_inputs_level_0(self):
        """Nodes with no inputs (orphan nodes) have level 0."""
        scheduler = RenderScheduler()
        snapshot = _make_snapshot(
            nodes=[
                _node("b1", "blur"),  # No connections
            ],
        )
        plan = scheduler.plan(snapshot, "preview")
        assert plan.node_plans["b1"].dependency_level == 0


# ── Parallel Group Tests ──

class TestParallelGroups:
    """Verify parallel group construction."""

    def test_empty_graph_no_groups(self):
        """Empty graph produces empty parallel groups."""
        scheduler = RenderScheduler()
        snapshot = _make_snapshot(nodes=[])
        plan = scheduler.plan(snapshot, "preview")
        assert plan.parallel_groups == []
        assert plan.max_parallelism == 0  # No nodes, no parallelism

    def test_single_node_one_group(self):
        """Single node produces one group with one node."""
        scheduler = RenderScheduler()
        snapshot = _make_snapshot(nodes=[_node("c1", "color_input")])
        plan = scheduler.plan(snapshot, "preview")
        assert plan.parallel_groups == [["c1"]]
        assert plan.max_parallelism == 1

    def test_parallel_sources_in_same_group(self):
        """Multiple source nodes are in the same group (level 0)."""
        scheduler = RenderScheduler()
        snapshot = _make_snapshot(
            nodes=[
                _node("c1", "color_input"),
                _node("c2", "color_input"),
                _node("c3", "color_input"),
            ],
        )
        plan = scheduler.plan(snapshot, "preview")
        assert len(plan.parallel_groups) == 1
        assert plan.parallel_groups[0] == ["c1", "c2", "c3"]
        assert plan.max_parallelism == 3

    def test_chain_creates_sequential_groups(self):
        """Single chain creates sequential groups (one node per group)."""
        scheduler = RenderScheduler()
        snapshot = _make_snapshot(
            nodes=[
                _node("c1", "color_input"),
                _node("b1", "blur"),
                _node("b2", "blur"),
            ],
            connections=[
                ConnectionSnapshot("c1", "result", "b1", "image"),
                ConnectionSnapshot("b1", "result", "b2", "image"),
            ],
            eval_order=["c1", "b1", "b2"],
        )
        plan = scheduler.plan(snapshot, "preview")
        assert len(plan.parallel_groups) == 3
        assert plan.parallel_groups[0] == ["c1"]
        assert plan.parallel_groups[1] == ["b1"]
        assert plan.parallel_groups[2] == ["b2"]
        assert plan.max_parallelism == 1

    def test_parallel_branches_create_parallel_groups(self):
        """Independent branches create groups with multiple nodes."""
        scheduler = RenderScheduler()
        snapshot = _make_snapshot(
            nodes=[
                _node("c1", "color_input"),
                _node("b1", "blur"),
                _node("b2", "blur"),
                _node("b3", "blur"),
                _node("b4", "blur"),
            ],
            connections=[
                ConnectionSnapshot("c1", "result", "b1", "image"),
                ConnectionSnapshot("c1", "result", "b2", "image"),
                ConnectionSnapshot("b1", "result", "b3", "image"),
                ConnectionSnapshot("b2", "result", "b4", "image"),
            ],
            eval_order=["c1", "b1", "b2", "b3", "b4"],
        )
        plan = scheduler.plan(snapshot, "preview")
        # Level 0: c1
        # Level 1: b1, b2 (parallel)
        # Level 2: b3, b4 (parallel)
        assert len(plan.parallel_groups) == 3
        assert plan.parallel_groups[0] == ["c1"]
        assert set(plan.parallel_groups[1]) == {"b1", "b2"}
        assert set(plan.parallel_groups[2]) == {"b3", "b4"}
        assert plan.max_parallelism == 2

    def test_complex_graph_parallelism(self):
        """Complex graph with multiple merge points."""
        scheduler = RenderScheduler()
        snapshot = _make_snapshot(
            nodes=[
                _node("c1", "color_input"),
                _node("c2", "color_input"),
                _node("b1", "blur"),
                _node("b2", "blur"),
                _node("b3", "blur"),
                _node("b4", "blur"),
                _node("m1", "merge", input_ports=[("image_a", "image"), ("image_b", "image")]),
                _node("m2", "merge", input_ports=[("image_a", "image"), ("image_b", "image")]),
                _node("out", "output", input_ports=[("image", "image")]),
            ],
            connections=[
                ConnectionSnapshot("c1", "result", "b1", "image"),
                ConnectionSnapshot("c2", "result", "b2", "image"),
                ConnectionSnapshot("b1", "result", "b3", "image"),
                ConnectionSnapshot("b2", "result", "b4", "image"),
                ConnectionSnapshot("b3", "result", "m1", "image_a"),
                ConnectionSnapshot("b4", "result", "m1", "image_b"),
                ConnectionSnapshot("c1", "result", "m2", "image_a"),
                ConnectionSnapshot("m1", "result", "m2", "image_b"),
                ConnectionSnapshot("m2", "result", "out", "image"),
            ],
            eval_order=["c1", "c2", "b1", "b2", "b3", "b4", "m1", "m2", "out"],
        )
        plan = scheduler.plan(snapshot, "preview")
        # Expected levels:
        # Level 0: c1, c2 (source nodes)
        # Level 1: b1, b2 (depends on level 0)
        # Level 2: b3, b4 (depends on level 1)
        # Level 3: m1 (depends on b3 and b4 at level 2)
        # Level 4: m2 (depends on c1 level 0 and m1 level 3 → level 4)
        # Level 5: out (depends on m2 level 4)
        assert len(plan.parallel_groups) == 6
        assert set(plan.parallel_groups[0]) == {"c1", "c2"}  # max parallelism = 2
        assert set(plan.parallel_groups[1]) == {"b1", "b2"}
        assert set(plan.parallel_groups[2]) == {"b3", "b4"}
        assert plan.parallel_groups[3] == ["m1"]
        assert plan.parallel_groups[4] == ["m2"]
        assert plan.parallel_groups[5] == ["out"]
        assert plan.max_parallelism == 2

    def test_output_node_in_correct_group(self):
        """Output node is placed in the correct group based on its dependencies."""
        scheduler = RenderScheduler()
        snapshot = _make_snapshot(
            nodes=[
                _node("c1", "color_input"),
                _node("b1", "blur"),
                _node("out", "output", input_ports=[("image", "image")]),
            ],
            connections=[
                ConnectionSnapshot("c1", "result", "b1", "image"),
                ConnectionSnapshot("b1", "result", "out", "image"),
            ],
            eval_order=["c1", "b1", "out"],
        )
        plan = scheduler.plan(snapshot, "preview")
        assert plan.parallel_groups[0] == ["c1"]
        assert plan.parallel_groups[1] == ["b1"]
        assert plan.parallel_groups[2] == ["out"]
        assert plan.node_plans["out"].dependency_level == 2


# ── Max Parallelism Tests ──

class TestMaxParallelism:
    """Verify max parallelism calculation."""

    def test_single_branch_parallelism_1(self):
        """Single branch has max parallelism = 1."""
        scheduler = RenderScheduler()
        snapshot = _make_snapshot(
            nodes=[
                _node("c1", "color_input"),
                _node("b1", "blur"),
                _node("b2", "blur"),
            ],
            connections=[
                ConnectionSnapshot("c1", "result", "b1", "image"),
                ConnectionSnapshot("b1", "result", "b2", "image"),
            ],
            eval_order=["c1", "b1", "b2"],
        )
        plan = scheduler.plan(snapshot, "preview")
        assert plan.max_parallelism == 1

    def test_wide_graph_high_parallelism(self):
        """Wide graph with many parallel branches has high parallelism."""
        scheduler = RenderScheduler()
        nodes = [_node("c1", "color_input")]
        for i in range(10):
            nodes.append(_node(f"b{i}", "blur"))
        connections = [ConnectionSnapshot("c1", "result", f"b{i}", "image") for i in range(10)]
        eval_order = ["c1"] + [f"b{i}" for i in range(10)]
        snapshot = _make_snapshot(nodes=nodes, connections=connections, eval_order=eval_order)
        plan = scheduler.plan(snapshot, "preview")
        # All 10 blur nodes are at level 1 (depend on c1)
        assert plan.max_parallelism == 10
        assert len(plan.parallel_groups[1]) == 10


# ── Integration Tests ──

class TestParallelEvaluationIntegration:
    """Integration tests for parallel evaluation."""

    def test_parallel_groups_preserve_eval_order(self):
        """Parallel groups should be ordered consistently with eval_order."""
        scheduler = RenderScheduler()
        snapshot = _make_snapshot(
            nodes=[
                _node("c1", "color_input"),
                _node("b1", "blur"),
                _node("b2", "blur"),
            ],
            connections=[
                ConnectionSnapshot("c1", "result", "b1", "image"),
                ConnectionSnapshot("c1", "result", "b2", "image"),
            ],
            eval_order=["c1", "b1", "b2"],
        )
        plan = scheduler.plan(snapshot, "preview")
        # Flatten parallel groups should be a superset of eval_order
        all_nodes = []
        for group in plan.parallel_groups:
            all_nodes.extend(group)
        # All nodes should be present
        assert set(all_nodes) == set(plan.eval_order)

    def test_plan_has_parallel_groups_field(self):
        """RenderPlan should have parallel_groups field."""
        scheduler = RenderScheduler()
        snapshot = _make_snapshot(nodes=[_node("c1", "color_input")])
        plan = scheduler.plan(snapshot, "preview")
        assert hasattr(plan, 'parallel_groups')
        assert hasattr(plan, 'max_parallelism')
        assert isinstance(plan.parallel_groups, list)
        assert isinstance(plan.max_parallelism, int)

    def test_node_plan_has_dependency_level_field(self):
        """NodePlan should have dependency_level field."""
        scheduler = RenderScheduler()
        snapshot = _make_snapshot(nodes=[_node("c1", "color_input")])
        plan = scheduler.plan(snapshot, "preview")
        np = plan.node_plans["c1"]
        assert hasattr(np, 'dependency_level')
        assert isinstance(np.dependency_level, int)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])