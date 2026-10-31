#!/usr/bin/env python3
"""
Minimal test for dependency level computation without GPU dependencies.
Tests the core algorithm logic directly.
"""

import sys


def compute_dependency_levels(node_plans, eval_order):
    """
    Compute dependency level for each node.
    Simplified version without GPU dependencies.
    """
    GPU_STATEFUL_TYPES = {"color_input", "constant", "image_input"}

    for node_id in eval_order:
        np = node_plans.get(node_id)
        if np is None:
            continue

        # Source nodes always have level 0
        if np.get('node_type') in GPU_STATEFUL_TYPES:
            np['dependency_level'] = 0
            continue

        # Compute level from upstream dependencies
        inputs = np.get('inputs', {})
        if inputs:
            # Get max level from all upstream nodes
            max_upstream_level = 0
            for port_name, upstream_id in inputs.items():
                upstream_np = node_plans.get(upstream_id)
                if upstream_np:
                    max_upstream_level = max(max_upstream_level, upstream_np.get('dependency_level', 0))
            np['dependency_level'] = max_upstream_level + 1
        else:
            # No inputs: treat as source (level 0)
            np['dependency_level'] = 0


def build_parallel_groups(node_plans):
    """Build parallel execution groups from dependency levels."""
    level_groups = {}
    for node_id, np in node_plans.items():
        level = np.get('dependency_level', 0)
        if level not in level_groups:
            level_groups[level] = []
        level_groups[level].append(node_id)

    # Convert to sorted list (level 0 first)
    max_level = max(level_groups.keys()) if level_groups else 0
    parallel_groups = []
    for level in range(max_level + 1):
        group = level_groups.get(level, [])
        parallel_groups.append(group)

    # Compute max parallelism
    max_parallelism = max(len(group) for group in parallel_groups) if parallel_groups else 1

    return parallel_groups, max_parallelism


def test_source_nodes_level_0():
    """Source nodes have level 0."""
    node_plans = {
        "c1": {"node_id": "c1", "node_type": "color_input", "inputs": {}},
        "c2": {"node_id": "c2", "node_type": "constant", "inputs": {}},
    }
    eval_order = ["c1", "c2"]

    compute_dependency_levels(node_plans, eval_order)

    assert node_plans["c1"]["dependency_level"] == 0, "c1 should be level 0"
    assert node_plans["c2"]["dependency_level"] == 0, "c2 should be level 0"
    print("✓ Source nodes level 0 test passed")


def test_single_chain():
    """Nodes in a single chain have increasing levels."""
    node_plans = {
        "c1": {"node_id": "c1", "node_type": "color_input", "inputs": {}},
        "b1": {"node_id": "b1", "node_type": "blur", "inputs": {"image": "c1"}},
        "b2": {"node_id": "b2", "node_type": "blur", "inputs": {"image": "b1"}},
    }
    eval_order = ["c1", "b1", "b2"]

    compute_dependency_levels(node_plans, eval_order)

    assert node_plans["c1"]["dependency_level"] == 0, "c1 should be level 0"
    assert node_plans["b1"]["dependency_level"] == 1, "b1 should be level 1"
    assert node_plans["b2"]["dependency_level"] == 2, "b2 should be level 2"
    print("✓ Single chain test passed")


def test_parallel_branches():
    """Nodes in independent branches have the same level."""
    node_plans = {
        "c1": {"node_id": "c1", "node_type": "color_input", "inputs": {}},
        "b1": {"node_id": "b1", "node_type": "blur", "inputs": {"image": "c1"}},
        "b2": {"node_id": "b2", "node_type": "blur", "inputs": {"image": "c1"}},
    }
    eval_order = ["c1", "b1", "b2"]

    compute_dependency_levels(node_plans, eval_order)

    assert node_plans["c1"]["dependency_level"] == 0, "c1 should be level 0"
    assert node_plans["b1"]["dependency_level"] == 1, "b1 should be level 1"
    assert node_plans["b2"]["dependency_level"] == 1, "b2 should be level 1"

    # Check parallel groups
    parallel_groups, max_parallelism = build_parallel_groups(node_plans)
    assert len(parallel_groups) == 2, "Should have 2 groups"
    assert parallel_groups[0] == ["c1"], "Group 0 should be c1"
    assert set(parallel_groups[1]) == {"b1", "b2"}, "Group 1 should be b1 and b2"
    assert max_parallelism == 2, "Max parallelism should be 2"
    print("✓ Parallel branches test passed")


def test_complex_graph():
    """Complex graph with multiple merge points."""
    node_plans = {
        "c1": {"node_id": "c1", "node_type": "color_input", "inputs": {}},
        "c2": {"node_id": "c2", "node_type": "color_input", "inputs": {}},
        "b1": {"node_id": "b1", "node_type": "blur", "inputs": {"image": "c1"}},
        "b2": {"node_id": "b2", "node_type": "blur", "inputs": {"image": "c2"}},
        "b3": {"node_id": "b3", "node_type": "blur", "inputs": {"image": "b1"}},
        "b4": {"node_id": "b4", "node_type": "blur", "inputs": {"image": "b2"}},
        "m1": {"node_id": "m1", "node_type": "merge", "inputs": {"image_a": "b3", "image_b": "b4"}},
        "out": {"node_id": "out", "node_type": "output", "inputs": {"image": "m1"}},
    }
    eval_order = ["c1", "c2", "b1", "b2", "b3", "b4", "m1", "out"]

    compute_dependency_levels(node_plans, eval_order)

    # Check dependency levels
    assert node_plans["c1"]["dependency_level"] == 0, "c1 level 0"
    assert node_plans["c2"]["dependency_level"] == 0, "c2 level 0"
    assert node_plans["b1"]["dependency_level"] == 1, "b1 level 1"
    assert node_plans["b2"]["dependency_level"] == 1, "b2 level 1"
    assert node_plans["b3"]["dependency_level"] == 2, "b3 level 2"
    assert node_plans["b4"]["dependency_level"] == 2, "b4 level 2"
    assert node_plans["m1"]["dependency_level"] == 3, "m1 level 3"
    assert node_plans["out"]["dependency_level"] == 4, "out level 4"

    # Check parallel groups
    parallel_groups, max_parallelism = build_parallel_groups(node_plans)
    assert len(parallel_groups) == 5, "Should have 5 groups"
    assert set(parallel_groups[0]) == {"c1", "c2"}, "Group 0: c1, c2"
    assert set(parallel_groups[1]) == {"b1", "b2"}, "Group 1: b1, b2"
    assert set(parallel_groups[2]) == {"b3", "b4"}, "Group 2: b3, b4"
    assert parallel_groups[3] == ["m1"], "Group 3: m1"
    assert parallel_groups[4] == ["out"], "Group 4: out"
    assert max_parallelism == 2, "Max parallelism should be 2"

    print("✓ Complex graph test passed")


def test_merge_node_max_level():
    """Merge node level = max(upstream_levels) + 1."""
    node_plans = {
        "c1": {"node_id": "c1", "node_type": "color_input", "inputs": {}},
        "b1": {"node_id": "b1", "node_type": "blur", "inputs": {"image": "c1"}},
        "b2": {"node_id": "b2", "node_type": "blur", "inputs": {"image": "b1"}},
        "m1": {"node_id": "m1", "node_type": "merge", "inputs": {"image_a": "c1", "image_b": "b2"}},
    }
    eval_order = ["c1", "b1", "b2", "m1"]

    compute_dependency_levels(node_plans, eval_order)

    # c1 level 0, b1 level 1, b2 level 2
    # m1 depends on c1 (level 0) and b2 (level 2) → level 3
    assert node_plans["m1"]["dependency_level"] == 3, "m1 should be level 3 (max of 0 and 2) + 1"
    print("✓ Merge node max level test passed")


def main():
    print("=" * 60)
    print("Dependency Level Computation Verification")
    print("=" * 60)

    try:
        test_source_nodes_level_0()
        test_single_chain()
        test_parallel_branches()
        test_complex_graph()
        test_merge_node_max_level()
        print("=" * 60)
        print("All tests passed!")
        print("=" * 60)
        return 0
    except AssertionError as e:
        print(f"\n✗ Test failed: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())