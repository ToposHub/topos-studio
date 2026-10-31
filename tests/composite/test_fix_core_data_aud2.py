"""Regression tests for core-data audit fixes (batch A, Critical).

Covers:
  * AUD-4905 — ``_recompile_stack_recursive`` must aggregate nested
    (Precomp child stack) compile changes instead of dropping them while
    still overwriting ``generated_merge_node_ids``.
  * AUD-5408 — ``UndoTree.compress`` must re-attach the children of a
    merged-away node so the current history chain (and any branch below
    it) stays connected; redo keeps working after compression.
  * AUD-11600 — incremental delta must not silently drop unknown/new
    top-level keys (fps, duration_frames, layers, layer_stacks, ...):
    no whitelist gap and no false "no_changes".
"""

import copy
import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.core.layer_stack_controller import (
    LayerStackController,
)
from src.features.composite.core.layer_stack_compiler import (
    MERGE_PORT_FOREGROUND, MERGE_PORT_BACKGROUND,
)
from src.features.composite.core.undo_stack import (
    UndoTree, UndoAction, create_param_change_action,
)
from src.features.composite.utils.serialization_formats import (
    IncrementalSaver, save_incremental, load_incremental,
)
from src.features.composite.nodes.output_node import OutputNode
from src.features.composite.nodes.constant_node import ConstantNode

pytestmark = pytest.mark.composite_core


def _new_controller() -> tuple:
    """Build a controller on a fresh graph with one Output node."""
    g = NodeGraph()
    g.add_node(OutputNode(node_id="out"))
    return g, LayerStackController(g, undo_stack=None)


def _add_source(g: NodeGraph, i: int) -> str:
    """Add a ConstantNode source with id src<i>."""
    src = ConstantNode(node_id=f"src{i}")
    g.add_node(src)
    return src.id


def _conn_tuples(g: NodeGraph) -> set:
    return {
        (c.src_node_id, c.src_port_name, c.dst_node_id, c.dst_port_name)
        for c in g.connections
    }


# ── AUD-4905 ─────────────────────────────────────────────────────────────────


class TestNestedRecompileAppliesChildChanges(unittest.TestCase):
    """Main-stack operations must repair nested Precomp stacks (AUD-4905)."""

    def _build_nested(self) -> tuple:
        g, ctrl = _new_controller()
        precomp_layer_id = ctrl.create_precomp_layer("Nested")
        precomp = g.get_node(g.get_layer(precomp_layer_id).root_node_id)
        child_id = precomp.parameters["child_stack_id"]
        child = g.get_layer_stack(child_id)
        _add_source(g, 0)
        _add_source(g, 1)
        ctrl.add_media_layer("A", "src0", stack_id=child_id)
        ctrl.add_media_layer("B", "src1", stack_id=child_id)
        self.assertEqual(len(child.generated_merge_node_ids), 1)
        return g, ctrl, precomp, child

    def test_main_rebuild_repairs_deleted_child_merge(self):
        g, ctrl, precomp, child = self._build_nested()
        merge_id = child.generated_merge_node_ids[0]
        self.assertIsNotNone(g.get_node(merge_id))

        # Simulate the user deleting the child stack's auto-Merge in node
        # mode (removes the node plus its connections).
        g.remove_node(merge_id)
        self.assertIsNone(g.get_node(merge_id))

        # Rebuilding the MAIN stack walks into the Precomp child; the
        # child's repair changes must be applied, not dropped.
        result = ctrl.rebuild()
        self.assertTrue(result.ok)

        new_merge_id = child.generated_merge_node_ids[0]
        self.assertIsNotNone(
            g.get_node(new_merge_id),
            "AUD-4905: child-stack merge change was dropped; "
            "generated_merge_node_ids points at a node that was never "
            "created.")
        conns = _conn_tuples(g)
        self.assertIn((new_merge_id, "result", precomp.id, "image"), conns)
        # The child chain feeds the repaired merge.  add_media_layer
        # inserts on top, so layer order is [B, A] (B foreground).
        roots = [g.get_layer(lid).root_node_id for lid in child.layer_ids]
        self.assertIn((roots[0], "result", new_merge_id,
                       MERGE_PORT_FOREGROUND), conns)
        self.assertIn((roots[1], "result", new_merge_id,
                       MERGE_PORT_BACKGROUND), conns)

    def test_child_generated_ids_all_exist_after_main_rebuild(self):
        g, ctrl, precomp, child = self._build_nested()
        for mid in child.generated_merge_node_ids:
            g.remove_node(mid)
        ctrl.rebuild()
        for mid in child.generated_merge_node_ids:
            self.assertIsNotNone(
                g.get_node(mid),
                "generated_merge_node_ids must reference live nodes after a "
                "recompile (otherwise the corruption persists on save).")


# ── AUD-5408 ─────────────────────────────────────────────────────────────────


def _push_param(tree: UndoTree, data: dict, old: float, new: float,
                param: str = "radius") -> None:
    action = create_param_change_action(
        node_id="node_001",
        param_name=param,
        old_value=old,
        new_value=new,
        undo_fn=lambda d: data.update({"value": d["old"]}),
        redo_fn=lambda d: data.update({"value": d["new"]}),
        merge_window_ms=60_000,
    )
    tree.push(action)
    data["value"] = new


class TestCompressChainIntegrity(unittest.TestCase):
    """compress() must keep the history chain connected (AUD-5408)."""

    def test_current_deeper_than_merged_nodes_stays_connected(self):
        tree = UndoTree()
        data = {"value": 0.0}
        for i in range(3):
            _push_param(tree, data, float(i), float(i + 1))
        # current == last node; compress merges the two earlier nodes away.
        tree.compress()

        # Every surviving node's parent must resolve (no dangling chain).
        for node in tree._nodes.values():
            if node.parent_id is not None:
                self.assertIn(node.parent_id, tree._nodes,
                              "AUD-5408: dangling parent_id after compress")
        # Path from the cursor must reach the root.
        path = tree._get_path_to_root(tree._current_node_id)
        self.assertEqual(path[-1], tree._root_node_id)

        # Undo all the way to the root and redo all the way back.
        while tree.get_current_node() and not tree.get_current_node().is_root():
            self.assertIsNotNone(tree.undo())
        self.assertEqual(data["value"], 0.0)  # merged action reachable
        while tree.redo() is not None:
            pass
        self.assertEqual(data["value"], 3.0)

    def test_merged_action_values_span_all_merged_steps(self):
        tree = UndoTree()
        data = {"value": 0.0}
        for i in range(4):
            _push_param(tree, data, float(i), float(i + 1))
        tree.compress()
        # One merged undo must rewind across every merged step (0.0 ← 4.0).
        self.assertIsNotNone(tree.undo())
        self.assertEqual(data["value"], 0.0)
        # And redo returns to the newest value in a single step.
        self.assertIsNotNone(tree.redo())
        self.assertEqual(data["value"], 4.0)

    def test_compress_preserves_branch_children_of_merged_node(self):
        tree = UndoTree()
        data = {"value": 0.0}
        _push_param(tree, data, 0.0, 1.0)   # A
        _push_param(tree, data, 1.0, 2.0)   # B (same merge key)
        _push_param(tree, data, 2.0, 3.0)   # C (same merge key)
        b_node = tree.get_current_node()    # current == C, parent == B
        b_id = b_node.parent_id
        c_id = tree._current_node_id
        # Undo to B and push a divergent (different merge key) child D.
        tree.undo()
        self.assertEqual(tree._current_node_id, b_id)
        _push_param(tree, data, 2.0, 42.0, param="gain")  # D, not mergeable
        d_id = tree._current_node_id

        tree.compress()  # merges B into A along root→A→B→D

        a_id = tree._nodes[d_id].parent_id
        # The off-path sibling C must have been re-attached to A.
        self.assertIn(c_id, tree._nodes)
        self.assertEqual(tree._nodes[c_id].parent_id, a_id)
        self.assertEqual(tree._nodes[d_id].parent_id, a_id)
        self.assertIn(c_id, tree._nodes[a_id].children)
        # C is still reachable: undo D, then switch onto the C branch.
        self.assertTrue(tree.switch_to_node(c_id))
        self.assertEqual(data["value"], 3.0)


# ── AUD-11600 ─────────────────────────────────────────────────────────────────


def _base_snapshot() -> dict:
    return {
        "nodes": [{"id": "n1", "parameters": {"gain": 1.0},
                   "position": [0.0, 0.0]}],
        "connections": [],
        "fps": 24.0,
        "duration_frames": 100,
        "layers": {"L0": {"name": "A", "opacity": 1.0}},
        "layer_stacks": {"main": {"layer_ids": ["L0"]}},
        "backdrops": {"bd1": {"title": "Group A"}},
    }


class TestIncrementalDeltaTopLevelFields(unittest.TestCase):
    """Unknown/new top-level keys must survive an incremental round-trip."""

    def test_new_top_level_key_survives_delta_roundtrip(self):
        saver = IncrementalSaver()
        base = _base_snapshot()
        saver.set_base_snapshot(base, generation=3)
        new = copy.deepcopy(base)
        new["render_settings"] = {"quality": "high"}  # future/unknown key

        delta_data = saver.compute_delta(new)
        self.assertEqual(delta_data["delta_type"], "incremental")
        self.assertIsNone(delta_data["delta"].get("no_changes"),
                          "AUD-11600: a new top-level key must not be "
                          "reported as no_changes")

        restored = saver.apply_delta(copy.deepcopy(base), delta_data)
        self.assertEqual(restored["render_settings"], {"quality": "high"})

    def test_fps_layers_layer_stacks_changes_survive(self):
        saver = IncrementalSaver()
        base = _base_snapshot()
        saver.set_base_snapshot(base)
        new = copy.deepcopy(base)
        new["fps"] = 60.0
        new["duration_frames"] = 250
        new["layers"]["L0"]["opacity"] = 0.5
        new["layer_stacks"]["main"]["layer_ids"] = ["L0", "L1"]
        new["composite_schema_version"] = 7

        delta_data = saver.compute_delta(new)
        self.assertIsNone(delta_data["delta"].get("no_changes"))
        restored = saver.apply_delta(copy.deepcopy(base), delta_data)
        self.assertEqual(restored["fps"], 60.0)
        self.assertEqual(restored["duration_frames"], 250)
        self.assertEqual(restored["layers"]["L0"]["opacity"], 0.5)
        self.assertEqual(restored["layer_stacks"]["main"]["layer_ids"],
                         ["L0", "L1"])
        self.assertEqual(restored["composite_schema_version"], 7)

    def test_unchanged_data_is_still_no_changes(self):
        saver = IncrementalSaver()
        base = _base_snapshot()
        saver.set_base_snapshot(base)
        delta_data = saver.compute_delta(copy.deepcopy(base))
        self.assertEqual(delta_data["delta_type"], "incremental")
        self.assertTrue(delta_data["delta"].get("no_changes"))
        restored = saver.apply_delta(base, delta_data)
        self.assertEqual(restored, base)

    def test_known_replacement_keys_still_roundtrip(self):
        saver = IncrementalSaver()
        base = _base_snapshot()
        saver.set_base_snapshot(base)
        new = copy.deepcopy(base)
        new["backdrops"] = {"bd2": {"title": "Renamed"}}
        delta_data = saver.compute_delta(new)
        restored = saver.apply_delta(copy.deepcopy(base), delta_data)
        self.assertEqual(restored["backdrops"], {"bd2": {"title": "Renamed"}})

    def test_bytes_roundtrip_preserves_new_top_level_key(self):
        saver = IncrementalSaver()
        base = _base_snapshot()
        saver.set_base_snapshot(base, generation=1)
        new = copy.deepcopy(base)
        new["fps"] = 30.0
        new["render_settings"] = {"quality": "high"}

        raw = save_incremental(new, saver)
        loaded = load_incremental(raw, base_snapshot=base, saver=saver)
        self.assertEqual(loaded["fps"], 30.0)
        self.assertEqual(loaded["render_settings"], {"quality": "high"})


if __name__ == "__main__":
    unittest.main()
