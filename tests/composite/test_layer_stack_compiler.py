"""Tests for LayerStackCompiler — layer ordering → Merge/Output chain.

Covers the full plan §13.2 matrix:
  * 0 layers / 1 layer / 2 layers / 3 layers / 10 layers
  * top / middle / bottom insertion
  * top / middle / bottom deletion
  * repeated reorders do not create duplicate merges
  * no cycles ever produced
  * Output always receives the final result
  * Merge port order locked to A=foreground, B=background
"""

import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from src.features.composite.core.node_engine import NodeGraph, Connection
from src.features.composite.core.layer_model import (
    BOTTOM_UP_LAYER_STACK_VERSION, LEGACY_LAYER_STACK_VERSION,
    LAYER_MERGE_MATTE_PARAM, LAYER_MERGE_OWNER_PARAM, Layer, LayerStack,
)
from src.features.composite.core.layer_stack_compiler import (
    LayerStackCompiler, CompileChange, CompileResult,
    MERGE_PORT_FOREGROUND, MERGE_PORT_BACKGROUND, MERGE_OUTPUT_PORT,
    OUTPUT_INPUT_PORT, MERGE_NODE_TYPE,
)
from src.features.composite.nodes.output_node import OutputNode
from src.features.composite.nodes.constant_node import ConstantNode
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.nodes.blend_node import MergeNode

pytestmark = pytest.mark.composite_core


def _apply_changes(graph: NodeGraph, changes):
    """Apply a list of CompileChange objects to a graph (test helper).

    The controller would do this inside an undo group; tests just want the
    end state.
    """
    for ch in changes:
        if ch.kind == "create_node":
            if ch.node_type == MERGE_NODE_TYPE:
                node = MergeNode(node_id=ch.node_id)
            else:
                node = ConstantNode(node_id=ch.node_id)
            graph.add_node(node)
        elif ch.kind == "remove_node":
            graph.remove_node(ch.node_id)
        elif ch.kind == "connect":
            if ch.src_port_name == "__parent__":
                # parent projection — set node.parent_id
                node = graph.get_node(ch.src_node_id)
                if node is not None:
                    node.parent_id = ch.dst_port_name or None
            else:
                graph.connect(ch.src_node_id, ch.src_port_name,
                              ch.dst_node_id, ch.dst_port_name)
        elif ch.kind == "set_parameter":
            graph.set_node_parameter(ch.node_id, ch.param_name, ch.value)
        elif ch.kind == "disconnect":
            graph.disconnect(ch.src_node_id, ch.src_port_name,
                             ch.dst_node_id, ch.dst_port_name)


def _build_graph(num_layers: int, version: int = LEGACY_LAYER_STACK_VERSION) -> tuple:
    """Build a graph with an Output and ``num_layers`` source+transform pairs.

    Returns ``(graph, stack, layers_map)`` where layers_map is
    ``layer_id -> Layer``.  Layers are named L0 (top) .. L{N-1} (bottom).
    The stack and its layers are registered on the graph (via
    ``set_layer_stack`` / ``upsert_layer``) exactly as the controller would,
    so the compiler's ``graph.get_all_layers()`` reflect the real state.
    """
    graph = NodeGraph()
    out = OutputNode(node_id="out")
    graph.add_node(out)

    stack = LayerStack(stack_id="main", output_node_id="out", version=version)
    layers = {}
    for i in range(num_layers):
        src = ConstantNode(node_id=f"src{i}")
        graph.add_node(src)
        t = TransformNode(node_id=f"t{i}")
        graph.add_node(t)
        graph.connect(f"src{i}", "result", f"t{i}", "image")
        lid = f"L{i}"
        layer = Layer(layer_id=lid, name=f"Layer {i}", root_node_id=f"t{i}",
                      source_node_id=f"src{i}")
        layers[lid] = layer
        stack.layer_ids.append(lid)
    # Register on the graph so compiler conflict-detection sees the real
    # layer-root set (matches controller behaviour).
    graph.set_layer_stack(stack)
    for layer in layers.values():
        graph.upsert_layer(stack.stack_id, layer)
    return graph, stack, layers


def _conn_tuples(graph: NodeGraph) -> set:
    return {(c.src_node_id, c.src_port_name, c.dst_node_id, c.dst_port_name)
            for c in graph.connections}


class TestCompilerBasics(unittest.TestCase):
    """0/1/2/N layer compile shapes (plan §4.3)."""

    def test_zero_layers_disconnects_output(self):
        graph, stack, layers = _build_graph(0)
        # Seed an existing connection into Output so we observe the disconnect.
        src = ConstantNode(node_id="phantom")
        graph.add_node(src)
        graph.connect("phantom", "result", "out", "image")
        compiler = LayerStackCompiler(graph)
        result = compiler.compile(stack, layers)
        self.assertTrue(result.ok)
        self.assertEqual(result.merge_node_ids, [])
        kinds = {c.kind for c in result.changes}
        self.assertIn("disconnect", kinds)

    def test_one_layer_no_merge(self):
        graph, stack, layers = _build_graph(1)
        compiler = LayerStackCompiler(graph)
        result = compiler.compile(stack, layers)
        self.assertTrue(result.ok)
        self.assertEqual(result.merge_node_ids, [])
        _apply_changes(graph, result.changes)
        # t0 → out:image
        self.assertIn(("t0", "result", "out", "image"), _conn_tuples(graph))

    def test_track_matte_source_is_hidden_and_luma_uses_mask_port(self):
        graph, stack, layers = _build_graph(2)
        # L0 is the visible layer; L1 supplies its matte and must not be
        # composited a second time as a regular background layer.
        layers["L0"].track_matte_layer_id = "L1"
        layers["L0"].track_matte_mode = "luma"
        layers["L0"].track_matte_invert = True
        compiler = LayerStackCompiler(graph)
        result = compiler.compile(stack, layers)
        self.assertTrue(result.ok)
        self.assertEqual(len(result.merge_node_ids), 1)
        _apply_changes(graph, result.changes)
        mid = result.merge_node_ids[0]
        conns = _conn_tuples(graph)
        self.assertIn(("t0", "result", mid, MERGE_PORT_FOREGROUND), conns)
        self.assertIn(("t1", "result", mid, MERGE_PORT_BACKGROUND), conns)
        self.assertIn(("t1", "result", mid, "mask"), conns)
        self.assertIn((mid, MERGE_OUTPUT_PORT, "out", OUTPUT_INPUT_PORT), conns)
        self.assertEqual(graph.get_node(mid).parameters["blend_mode"], "copy")
        self.assertEqual(graph.get_node(mid).parameters["mask_channel"], "luma")
        self.assertTrue(graph.get_node(mid).parameters["mask_invert"])

    def test_two_layers_one_merge(self):
        graph, stack, layers = _build_graph(2)
        compiler = LayerStackCompiler(graph)
        result = compiler.compile(stack, layers)
        self.assertTrue(result.ok)
        self.assertEqual(len(result.merge_node_ids), 1)
        _apply_changes(graph, result.changes)
        m0 = result.merge_node_ids[0]
        # A=t0 (fg), B=t1 (bg), m0 → out
        conns = _conn_tuples(graph)
        self.assertIn(("t0", "result", m0, MERGE_PORT_FOREGROUND),
                      conns)
        self.assertIn(("t1", "result", m0, MERGE_PORT_BACKGROUND),
                      conns)
        self.assertIn((m0, MERGE_OUTPUT_PORT, "out", OUTPUT_INPUT_PORT),
                      conns)

    def test_three_layers_two_merges_correct_order(self):
        graph, stack, layers = _build_graph(3)
        compiler = LayerStackCompiler(graph)
        result = compiler.compile(stack, layers)
        self.assertTrue(result.ok)
        self.assertEqual(len(result.merge_node_ids), 2)
        _apply_changes(graph, result.changes)
        m0, m1 = result.merge_node_ids
        conns = _conn_tuples(graph)
        # Merge0: A=t0, B=t1
        self.assertIn(("t0", "result", m0, MERGE_PORT_FOREGROUND), conns)
        self.assertIn(("t1", "result", m0, MERGE_PORT_BACKGROUND), conns)
        # Merge1: A=m0, B=t2
        self.assertIn((m0, MERGE_OUTPUT_PORT, m1, MERGE_PORT_FOREGROUND), conns)
        self.assertIn(("t2", "result", m1, MERGE_PORT_BACKGROUND), conns)
        # Merge1 → out
        self.assertIn((m1, MERGE_OUTPUT_PORT, "out", OUTPUT_INPUT_PORT), conns)

    def test_ten_layers_nine_merges(self):
        graph, stack, layers = _build_graph(10)
        compiler = LayerStackCompiler(graph)
        result = compiler.compile(stack, layers)
        self.assertTrue(result.ok)
        self.assertEqual(len(result.merge_node_ids), 9)
        _apply_changes(graph, result.changes)
        # No cycles: topological sort must still succeed.
        order = graph.topological_sort()
        self.assertEqual(len(order), len(graph.nodes))


class TestCompilerReuse(unittest.TestCase):
    """Re-compiling must recycle merges, never duplicate (plan §4.4)."""

    def test_recompile_does_not_create_duplicates(self):
        graph, stack, layers = _build_graph(3)
        compiler = LayerStackCompiler(graph)
        first = compiler.compile(stack, layers)
        self.assertEqual(len(first.merge_node_ids), 2)
        _apply_changes(graph, first.changes)
        stack.generated_merge_node_ids = list(first.merge_node_ids)

        # Recompile with the same ordering.
        second = compiler.compile(stack, layers)
        self.assertEqual(second.merge_node_ids, first.merge_node_ids)
        create_changes = [c for c in second.changes if c.kind == "create_node"]
        self.assertEqual(create_changes, [], "no new merges on recompile")

    def test_recompile_after_reorder_reuses_nodes(self):
        graph, stack, layers = _build_graph(4)
        compiler = LayerStackCompiler(graph)
        first = compiler.compile(stack, layers)
        _apply_changes(graph, first.changes)
        stack.generated_merge_node_ids = list(first.merge_node_ids)
        self.assertEqual(len(stack.generated_merge_node_ids), 3)

        # Reverse the order.
        stack.layer_ids.reverse()
        second = compiler.compile(stack, layers)
        self.assertEqual(len(second.merge_node_ids), 3)
        # Same node ids recycled.
        self.assertEqual(set(second.merge_node_ids),
                         set(first.merge_node_ids))
        _apply_changes(graph, second.changes)
        # Still acyclic.
        order = graph.topological_sort()
        self.assertEqual(len(order), len(graph.nodes))

    def test_removing_layer_prunes_surplus_merge(self):
        graph, stack, layers = _build_graph(3)
        compiler = LayerStackCompiler(graph)
        first = compiler.compile(stack, layers)
        _apply_changes(graph, first.changes)
        stack.generated_merge_node_ids = list(first.merge_node_ids)
        self.assertEqual(len(stack.generated_merge_node_ids), 2)

        # Remove L0 (top): the controller detaches the layer AND deletes its
        # source+transform nodes.  2 layers left → 1 merge.  Note:
        # ``graph.remove_layer`` already drops the id from ``stack.layer_ids``.
        graph.remove_layer("L0")
        for nid in ("t0", "src0"):
            graph.remove_node(nid)
        layers.pop("L0", None)
        second = compiler.compile(stack, layers)
        self.assertEqual(len(second.merge_node_ids), 1)
        removes = [c for c in second.changes if c.kind == "remove_node"]
        self.assertEqual(len(removes), 1)
        _apply_changes(graph, second.changes)
        order = graph.topological_sort()
        self.assertEqual(len(order), len(graph.nodes))


class TestBottomUpLayerStack:
    def test_middle_layer_blends_and_masks_only_its_own_image(self):
        graph, stack, layers = _build_graph(4, BOTTOM_UP_LAYER_STACK_VERSION)
        middle = layers["L1"]
        middle.blend_mode = "multiply"
        middle.opacity = 0.3
        middle.track_matte_layer_id = "L3"
        result = LayerStackCompiler(graph).compile(stack, layers)
        assert result.ok
        _apply_changes(graph, result.changes)
        top_merge, middle_merge, bottom_merge = result.merge_node_ids
        conns = _conn_tuples(graph)
        assert ("t2", "result", bottom_merge, "A") in conns
        assert not any(dst == bottom_merge and port == "B" for _, _, dst, port in conns)
        assert ("t1", "result", middle_merge, "A") in conns
        assert (bottom_merge, "result", middle_merge, "B") in conns
        assert ("t3", "result", middle_merge, "mask") in conns
        assert ("t0", "result", top_merge, "A") in conns
        assert (middle_merge, "result", top_merge, "B") in conns
        assert (top_merge, "result", "out", "image") in conns
        params = graph.get_node(middle_merge).parameters
        assert params[LAYER_MERGE_OWNER_PARAM] == "L1"
        assert params[LAYER_MERGE_MATTE_PARAM] is True
        assert params["blend_mode"] == "multiply"
        assert params["opacity"] == pytest.approx(0.3)

    def test_merge_animation_stays_with_layer_through_reorder_and_undo(self):
        from src.features.composite.core.layer_stack_controller import LayerStackController
        from src.features.composite.core.undo_stack import UndoStack
        from src.shared.keyframe import Keyframe, KeyframeTrack

        graph, stack, layers = _build_graph(3, BOTTOM_UP_LAYER_STACK_VERSION)
        undo = UndoStack()
        controller = LayerStackController(graph, undo_stack=undo)
        first = controller.rebuild()
        top_id, middle_id, bottom_id = first.merge_node_ids
        middle_merge = graph.get_node(middle_id)
        middle_merge.set_keyframe_track("opacity", KeyframeTrack("opacity", [Keyframe(5, 0.25)]))
        track = middle_merge.keyframe_tracks["opacity"]

        assert controller.move_layer("L1", 2)
        assert stack.generated_merge_node_ids == [top_id, bottom_id, middle_id]
        assert graph.get_node(middle_id).keyframe_tracks["opacity"] == track
        assert controller.set_layer_visibility("L1", False)
        assert graph.get_node(middle_id).keyframe_tracks["opacity"] == track
        assert undo.undo()
        assert undo.undo()
        assert controller.main_stack().generated_merge_node_ids == [top_id, middle_id, bottom_id]
        assert graph.get_node(middle_id).keyframe_tracks["opacity"] == track
        assert undo.redo()
        assert controller.main_stack().generated_merge_node_ids == [top_id, bottom_id, middle_id]
        reloaded = NodeGraph.from_dict(graph.to_dict())
        assert reloaded.get_main_layer_stack().version == BOTTOM_UP_LAYER_STACK_VERSION
        assert reloaded.get_node(middle_id).parameters[LAYER_MERGE_OWNER_PARAM] == "L1"
        assert reloaded.get_node(middle_id).keyframe_tracks["opacity"] == track

    def test_owner_survives_appending_effect_and_preserves_side_branch(self):
        from src.features.composite.core.layer_stack_controller import LayerStackController

        graph, stack, layers = _build_graph(2, BOTTOM_UP_LAYER_STACK_VERSION)
        controller = LayerStackController(graph)
        top_merge, _ = controller.rebuild().merge_node_ids
        side_branch = TransformNode(node_id="side")
        effect = TransformNode(node_id="effect")
        graph.add_node(side_branch)
        graph.add_node(effect)
        assert graph.connect(top_merge, "result", "side", "image")

        assert controller.add_node_to_layer("L0", "effect")
        conns = _conn_tuples(graph)
        assert ("effect", "result", top_merge, "A") in conns
        assert (top_merge, "result", "side", "image") in conns
        assert controller.move_layer("L0", 1)
        assert (top_merge, "result", "side", "image") in _conn_tuples(graph)
        assert not controller.remove_layer("L0")
        assert graph.get_layer("L0") is not None
        assert (top_merge, "result", "side", "image") in _conn_tuples(graph)

    def test_removing_a_shared_matte_keeps_its_free_node_branch(self):
        from src.features.composite.core.layer_stack_controller import LayerStackController

        graph, stack, layers = _build_graph(2, BOTTOM_UP_LAYER_STACK_VERSION)
        controller = LayerStackController(graph)
        layers["L0"].track_matte_layer_id = "L1"
        target_merge, = controller.rebuild().merge_node_ids
        graph.add_node(TransformNode(node_id="side"))
        assert graph.connect("t1", "result", "side", "image")

        assert controller.remove_layer("L1")
        assert graph.get_layer("L0").track_matte_layer_id is None
        assert ("t1", "result", "side", "image") in _conn_tuples(graph)
        assert ("t1", "result", target_merge, "mask") not in _conn_tuples(graph)

    def test_manual_input_conflict_preserves_graph_and_layer_order(self):
        from src.features.composite.core.layer_stack_controller import LayerStackController

        graph, stack, layers = _build_graph(2, BOTTOM_UP_LAYER_STACK_VERSION)
        controller = LayerStackController(graph)
        top_merge, _ = controller.rebuild().merge_node_ids
        graph.add_node(ConstantNode(node_id="manual"))
        graph.disconnect("t0", "result", top_merge, "A")
        assert graph.connect("manual", "result", top_merge, "A")

        assert not controller.move_layer("L0", 1)
        assert stack.layer_ids == ["L0", "L1"]
        assert ("manual", "result", top_merge, "A") in _conn_tuples(graph)

    def test_hiding_the_only_solo_layer_restores_ordinary_visible_layers(self):
        from src.features.composite.core.layer_stack_controller import LayerStackController

        graph, stack, layers = _build_graph(2, BOTTOM_UP_LAYER_STACK_VERSION)
        controller = LayerStackController(graph)
        top_merge, bottom_merge = controller.rebuild().merge_node_ids
        assert controller.set_layer_solo("L0", True)
        assert (top_merge, "result", "out", "image") in _conn_tuples(graph)

        assert controller.set_layer_visibility("L0", False)
        assert (bottom_merge, "result", "out", "image") in _conn_tuples(graph)
        assert stack.generated_merge_node_ids == [top_merge, bottom_merge]

    def test_promoting_an_output_dependent_layer_below_its_source_rejects_cycle(self):
        from src.features.composite.core.layer_stack_controller import LayerStackController

        graph, stack, layers = _build_graph(2, BOTTOM_UP_LAYER_STACK_VERSION)
        controller = LayerStackController(graph)
        top_merge, _ = controller.rebuild().merge_node_ids
        graph.add_node(TransformNode(node_id="dependent"))
        assert graph.connect(top_merge, "result", "dependent", "image")
        connections = _conn_tuples(graph)

        assert controller.promote_node_to_layer("dependent", insert_index=1) is None
        assert controller.main_stack().layer_ids == ["L0", "L1"]
        assert _conn_tuples(graph) == connections

    def test_legacy_project_without_version_retains_top_down_chain(self):
        graph, stack, layers = _build_graph(3)
        data = stack.to_dict()
        del data["version"]
        loaded = LayerStack.from_dict(data)
        assert loaded.version == LEGACY_LAYER_STACK_VERSION
        result = LayerStackCompiler(graph).compile(loaded, layers)
        _apply_changes(graph, result.changes)
        first, second = result.merge_node_ids
        assert (first, "result", second, "A") in _conn_tuples(graph)


class TestCompilerPortOrderLock(unittest.TestCase):
    """Foreground/Background port order lock (plan §4.2 / §13.2).

    This is the regression test explicitly demanded by the plan doc: the
    actual MergeNode ports are ``A`` (foreground) and ``B`` (background),
    not the "Foreground"/"Background" labels used illustratively in the doc.
    """

    def test_foreground_is_A_background_is_B(self):
        graph, stack, layers = _build_graph(2)
        compiler = LayerStackCompiler(graph)
        result = compiler.compile(stack, layers)
        _apply_changes(graph, result.changes)
        m0 = result.merge_node_ids[0]
        merge_node = graph.get_node(m0)
        # Sanity: the MergeNode class itself still uses A/B.
        self.assertEqual([p.name for p in merge_node.inputs],
                         ["A", "B", "mask"])
        # And the compiler connected fg→A, bg→B.
        conns = _conn_tuples(graph)
        self.assertIn(("t0", "result", m0, "A"), conns)
        self.assertIn(("t1", "result", m0, "B"), conns)


class TestCompilerCycles(unittest.TestCase):
    """No reorder may ever produce a cycle (plan §13.2)."""

    def test_many_reorders_never_cycle(self):
        graph, stack, layers = _build_graph(6)
        compiler = LayerStackCompiler(graph)
        first = compiler.compile(stack, layers)
        _apply_changes(graph, first.changes)
        stack.generated_merge_node_ids = list(first.merge_node_ids)

        import random
        rng = random.Random(1234)
        for _ in range(30):
            rng.shuffle(stack.layer_ids)
            result = compiler.compile(stack, layers)
            self.assertTrue(result.ok, msg=result.conflict_reason)
            _apply_changes(graph, result.changes)
            stack.generated_merge_node_ids = list(result.merge_node_ids)
            order = graph.topological_sort()
            self.assertEqual(len(order), len(graph.nodes),
                             "topological sort lost nodes after reorder")


class TestCompilerVisibility(unittest.TestCase):
    """disabled / solo layers drop out of the chain (plan §6.3 / §13.1)."""

    def test_disabled_layer_drops_out(self):
        graph, stack, layers = _build_graph(3)
        layers["L1"].enabled = False
        compiler = LayerStackCompiler(graph)
        result = compiler.compile(stack, layers)
        self.assertTrue(result.ok)
        # L0 and L2 only → 1 merge.
        self.assertEqual(len(result.merge_node_ids), 1)
        _apply_changes(graph, result.changes)
        m0 = result.merge_node_ids[0]
        conns = _conn_tuples(graph)
        self.assertIn(("t0", "result", m0, "A"), conns)
        self.assertIn(("t2", "result", m0, "B"), conns)

    def test_solo_hides_non_solo(self):
        graph, stack, layers = _build_graph(3)
        layers["L2"].solo = True
        compiler = LayerStackCompiler(graph)
        result = compiler.compile(stack, layers)
        self.assertTrue(result.ok)
        # Only L2 → 0 merges, direct to output.
        self.assertEqual(result.merge_node_ids, [])
        _apply_changes(graph, result.changes)
        self.assertIn(("t2", "result", "out", "image"), _conn_tuples(graph))

    def test_disabled_layer_does_not_shift_merge_properties(self):
        graph, stack, layers = _build_graph(3)
        layers["L0"].enabled = False
        layers["L1"].opacity = 0.25
        layers["L1"].blend_mode = "screen"
        result = LayerStackCompiler(graph).compile(stack, layers)
        _apply_changes(graph, result.changes)
        merge = graph.get_node(result.merge_node_ids[0])
        self.assertEqual(merge.parameters["opacity"], 0.25)
        self.assertEqual(merge.parameters["blend_mode"], "screen")

    def test_friendly_blend_mode_maps_to_shader_token(self):
        graph, stack, layers = _build_graph(2)
        layers["L0"].blend_mode = "color_dodge"
        result = LayerStackCompiler(graph).compile(stack, layers)
        _apply_changes(graph, result.changes)
        merge = graph.get_node(result.merge_node_ids[0])
        self.assertEqual(merge.parameters["blend_mode"], "color-dodge")


class TestCompilerConflict(unittest.TestCase):
    """Manual rewire of an auto-merge is flagged, not overwritten (§4.5)."""

    def test_manual_rewire_raises_conflict(self):
        graph, stack, layers = _build_graph(3)
        compiler = LayerStackCompiler(graph)
        first = compiler.compile(stack, layers)
        _apply_changes(graph, first.changes)
        stack.generated_merge_node_ids = list(first.merge_node_ids)

        # Simulate the user grafting an EXTERNAL node (one the layer system
        # never authors) onto an auto-Merge's foreground input.  Rewiring
        # between layer roots is treated as a normal reorder; wiring an
        # outside node is the conflict.
        external = ConstantNode(node_id="external_blur_proxy")
        graph.add_node(external)
        m0 = stack.generated_merge_node_ids[0]
        graph.disconnect("t0", "result", m0, "A")
        graph.connect("external_blur_proxy", "result", m0, "A")

        result = compiler.compile(stack, layers)
        self.assertFalse(result.ok)
        self.assertTrue(result.conflict)
        self.assertIsNotNone(result.conflict_reason)


class TestCompilerFreeGraph(unittest.TestCase):
    """managed=False → compiler is a no-op (plan §8.2)."""

    def test_free_graph_emits_no_changes(self):
        graph, stack, layers = _build_graph(3)
        stack.managed = False
        compiler = LayerStackCompiler(graph)
        result = compiler.compile(stack, layers)
        self.assertTrue(result.ok)
        self.assertEqual(result.changes, [])


if __name__ == "__main__":
    unittest.main()
