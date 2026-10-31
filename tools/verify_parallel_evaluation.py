#!/usr/bin/env python3
"""
Simple verification script for parallel node evaluation.
Tests dependency level computation and parallel group construction without pytest.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from src.composite.render_scheduler import RenderScheduler
from src.composite.graph_snapshot import GraphSnapshot, NodeSnapshot, ConnectionSnapshot


def create_node(node_id, node_type, input_ports=None, output_ports=None):
    """Create a NodeSnapshot."""
    return NodeSnapshot(
        node_id=node_id,
        node_type=node_type,
        parameters={},
        disabled=False,
        input_ports=input_ports or [("image", "image")],
        output_ports=output_ports or [("result", "image")],
    )


def test_source_nodes_level_0():
    """Source nodes have level 0."""
    scheduler = RenderScheduler()
    nodes = {
        "c1": create_node("c1", "color_input", [], [("result", "image")]),
        "c2": create_node("c2", "constant", [], [("result", "image")]),
    }
    snapshot = GraphSnapshot(
        generation=1,
        nodes=nodes,
        connections=[],
        eval_order=["c1", "c2"],
        color_width=1920,
        color_height=1080,
    )
    plan = scheduler.plan(snapshot, "preview")

    assert plan.node_plans["c1"].dependency_level == 0, "c1 should be level 0"
    assert plan.node_plans["c2"].dependency_level == 0, "c2 should be level 0"
    print("✓ Source nodes level 0 test passed")


def test_single_chain():
    """Nodes in a single chain have increasing levels."""
    scheduler = RenderScheduler()
    nodes = {
        "c1": create_node("c1", "color_input", [], [("result", "image")]),
        "b1": create_node("b1", "blur"),
        "b2": create_node("b2", "blur"),
    }
    connections = [
        ConnectionSnapshot("c1", "result", "b1", "image"),
        ConnectionSnapshot("b1", "result", "b2", "image"),
    ]
    snapshot = GraphSnapshot(
        generation=1,
        nodes=nodes,
        connections=connections,
        eval_order=["c1", "b1", "b2"],
        color_width=1920,
        color_height=1080,
    )
    plan = scheduler.plan(snapshot, "preview")

    assert plan.node_plans["c1"].dependency_level == 0, "c1 should be level 0"
    assert plan.node_plans["b1"].dependency_level == 1, "b1 should be level 1"
    assert plan.node_plans["b2"].dependency_level == 2, "b2 should be level 2"
    print("✓ Single chain test passed")


def test_parallel_branches():
    """Nodes in independent branches have the same level."""
    scheduler = RenderScheduler()
    nodes = {
        "c1": create_node("c1", "color_input", [], [("result", "image")]),
        "b1": create_node("b1", "blur"),
        "b2": create_node("b2", "blur"),
    }
    connections = [
        ConnectionSnapshot("c1", "result", "b1", "image"),
        ConnectionSnapshot("c1", "result", "b2", "image"),
    ]
    snapshot = GraphSnapshot(
        generation=1,
        nodes=nodes,
        connections=connections,
        eval_order=["c1", "b1", "b2"],
        color_width=1920,
        color_height=1080,
    )
    plan = scheduler.plan(snapshot, "preview")

    assert plan.node_plans["c1"].dependency_level == 0, "c1 should be level 0"
    assert plan.node_plans["b1"].dependency_level == 1, "b1 should be level 1"
    assert plan.node_plans["b2"].dependency_level == 1, "b2 should be level 1"

    # Check parallel groups
    assert len(plan.parallel_groups) == 2, "Should have 2 groups"
    assert plan.parallel_groups[0] == ["c1"], "Group 0 should be c1"
    assert set(plan.parallel_groups[1]) == {"b1", "b2"}, "Group 1 should be b1 and b2"
    assert plan.max_parallelism == 2, "Max parallelism should be 2"
    print("✓ Parallel branches test passed")


def test_complex_graph():
    """Complex graph with multiple merge points."""
    scheduler = RenderScheduler()
    nodes = {
        "c1": create_node("c1", "color_input", [], [("result", "image")]),
        "c2": create_node("c2", "color_input", [], [("result", "image")]),
        "b1": create_node("b1", "blur"),
        "b2": create_node("b2", "blur"),
        "b3": create_node("b3", "blur"),
        "b4": create_node("b4", "blur"),
        "m1": create_node("m1", "merge", [("image_a", "image"), ("image_b", "image")]),
        "out": create_node("out", "output", [("image", "image")], [("display", "image")]),
    }
    connections = [
        ConnectionSnapshot("c1", "result", "b1", "image"),
        ConnectionSnapshot("c2", "result", "b2", "image"),
        ConnectionSnapshot("b1", "result", "b3", "image"),
        ConnectionSnapshot("b2", "result", "b4", "image"),
        ConnectionSnapshot("b3", "result", "m1", "image_a"),
        ConnectionSnapshot("b4", "result", "m1", "image_b"),
        ConnectionSnapshot("m1", "result", "out", "image"),
    ]
    snapshot = GraphSnapshot(
        generation=1,
        nodes=nodes,
        connections=connections,
        eval_order=["c1", "c2", "b1", "b2", "b3", "b4", "m1", "out"],
        color_width=1920,
        color_height=1080,
    )
    plan = scheduler.plan(snapshot, "preview")

    # Check dependency levels
    assert plan.node_plans["c1"].dependency_level == 0, "c1 level 0"
    assert plan.node_plans["c2"].dependency_level == 0, "c2 level 0"
    assert plan.node_plans["b1"].dependency_level == 1, "b1 level 1"
    assert plan.node_plans["b2"].dependency_level == 1, "b2 level 1"
    assert plan.node_plans["b3"].dependency_level == 2, "b3 level 2"
    assert plan.node_plans["b4"].dependency_level == 2, "b4 level 2"
    assert plan.node_plans["m1"].dependency_level == 3, "m1 level 3"
    assert plan.node_plans["out"].dependency_level == 4, "out level 4"

    # Check parallel groups
    assert len(plan.parallel_groups) == 5, "Should have 5 groups"
    assert set(plan.parallel_groups[0]) == {"c1", "c2"}, "Group 0: c1, c2"
    assert set(plan.parallel_groups[1]) == {"b1", "b2"}, "Group 1: b1, b2"
    assert set(plan.parallel_groups[2]) == {"b3", "b4"}, "Group 2: b3, b4"
    assert plan.parallel_groups[3] == ["m1"], "Group 3: m1"
    assert plan.parallel_groups[4] == ["out"], "Group 4: out"
    assert plan.max_parallelism == 2, "Max parallelism should be 2"

    print("✓ Complex graph test passed")


def main():
    print("=" * 60)
    print("Parallel Node Evaluation Verification")
    print("=" * 60)

    try:
        test_source_nodes_level_0()
        test_single_chain()
        test_parallel_branches()
        test_complex_graph()
        print("=" * 60)
        print("All tests passed!")
        print("=" * 60)
        return 0
    except AssertionError as e:
        print(f"\n✗ Test failed: {e}")
        return 1
    except Exception as e:
        print(f"\n✗ Error: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())