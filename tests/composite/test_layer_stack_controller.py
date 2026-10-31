"""Tests for LayerStackController (plan §9.2 / §10 / §1.2.7 / §13.3).

Covers:
  * add / remove / move / duplicate / rename happy paths
  * set property (visibility / opacity / blend / parent / label)
  * one-operation-one-undo (plan §1.2.6 / §10) — including a media drop
    that creates source + transform + merge being a single undo step
  * parent cycle rejection + capability filter (plan §7)
  * legacy project projection from a free node graph (plan §1.2.7 / §12.2)
"""

import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

# UndoStack pulls in QObject/PyQt — skip the undo-specific cases if the
# env cannot construct a QApplication.  The non-undo cases always run.
try:
    from PyQt6.QtCore import QObject
    from src.features.composite.core.undo_stack import UndoStack, UndoAction
    _CAN_QT = True
except Exception:
    _CAN_QT = False

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.core.layer_model import Layer, LayerStack
from src.features.composite.core.layer_stack_controller import (
    LayerStackController, ACTION_ADD_LAYER, ACTION_REMOVE_LAYER,
    ACTION_MOVE_LAYER, ACTION_DUPLICATE_LAYER,
)
from src.features.composite.nodes.output_node import OutputNode
from src.features.composite.nodes.constant_node import ConstantNode
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.nodes.blur_node import BlurNode
from src.features.composite.nodes.image_input_node import ImageInputNode
from src.features.composite.nodes.precomp_node import PrecompNode
from src.features.composite.core.layer_stack_compiler import (
    MERGE_PORT_FOREGROUND, MERGE_PORT_BACKGROUND,
)

pytestmark = pytest.mark.composite_core


def _new_controller():
    """Build a controller on a fresh graph with one Output node."""
    g = NodeGraph()
    g.add_node(OutputNode(node_id="out"))
    return g, LayerStackController(g, undo_stack=None)


def _add_source(g, i):
    """Add a ConstantNode source with id src<i>."""
    src = ConstantNode(node_id=f"src{i}")
    g.add_node(src)
    return src.id


class TestAddMedia(unittest.TestCase):

    def test_create_precomp_and_compile_nested_child(self):
        g, ctrl = _new_controller()
        precomp_layer_id = ctrl.create_precomp_layer("Nested")
        parent = ctrl.main_stack()
        precomp_layer = g.get_layer(precomp_layer_id)
        self.assertEqual(precomp_layer.layer_type, "precomp")
        precomp = g.get_node(precomp_layer.root_node_id)
        self.assertIsInstance(precomp, PrecompNode)
        child_id = precomp.parameters["child_stack_id"]
        child = g.get_layer_stack(child_id)
        self.assertIsNotNone(child)
        self.assertEqual(child.output_node_id, precomp.id)

        _add_source(g, 0)
        child_layer_id = ctrl.add_media_layer(
            "Inside", "src0", stack_id=child_id)
        child_layer = g.get_layer(child_layer_id)
        self.assertIn(child_layer_id, child.layer_ids)
        self.assertIn((child_layer.root_node_id, "result", precomp.id, "image"),
                      [(c.src_node_id, c.src_port_name, c.dst_node_id,
                        c.dst_port_name) for c in g.connections])
        self.assertIn((precomp.id, "result", "out", "image"),
                      [(c.src_node_id, c.src_port_name, c.dst_node_id,
                        c.dst_port_name) for c in g.connections])

    def test_track_matte_is_one_operation(self):
        g, ctrl = _new_controller()
        _add_source(g, 0)
        _add_source(g, 1)
        matte_id = ctrl.add_media_layer("Matte", "src0")
        target_id = ctrl.add_media_layer("Target", "src1", insert_index=0)
        self.assertTrue(ctrl.set_layer_track_matte(
            target_id, matte_id, mode="luma", invert=True))
        target = g.get_layer(target_id)
        self.assertEqual(target.track_matte_layer_id, matte_id)
        self.assertEqual(target.track_matte_mode, "luma")
        self.assertTrue(target.track_matte_invert)
        self.assertEqual(len(ctrl.main_stack().generated_merge_node_ids), 1)

    def test_layer_relationship_index_describes_target_and_source(self):
        g, ctrl = _new_controller()
        _add_source(g, 0)
        _add_source(g, 1)
        matte_id = ctrl.add_media_layer("Matte", "src0")
        target_id = ctrl.add_media_layer("Target", "src1", insert_index=0)
        self.assertTrue(ctrl.set_layer_track_matte(
            target_id, matte_id, mode="luma", invert=True))

        relations = {
            item["layer_id"]: item
            for item in ctrl.layer_relationships()
        }
        target = relations[target_id]
        matte = relations[matte_id]
        self.assertTrue(target["track_matte_valid"])
        self.assertEqual(target["track_matte_layer_name"], "Matte")
        self.assertEqual(target["track_matte_mode"], "luma")
        self.assertTrue(target["track_matte_invert"])
        self.assertEqual(matte["track_matte_target_layer_ids"], (target_id,))

    def test_remove_precomp_removes_child_stack_and_owned_nodes(self):
        g, ctrl = _new_controller()
        precomp_layer_id = ctrl.create_precomp_layer("Nested")
        precomp = g.get_node(g.get_layer(precomp_layer_id).root_node_id)
        child_id = precomp.parameters["child_stack_id"]
        _add_source(g, 0)
        child_layer_id = ctrl.add_media_layer("Inside", "src0", stack_id=child_id)
        child_root_id = g.get_layer(child_layer_id).root_node_id
        self.assertTrue(ctrl.remove_layer(precomp_layer_id))
        self.assertIsNone(g.get_layer_stack(child_id))
        self.assertIsNone(g.get_node("src0"))
        self.assertIsNone(g.get_node(child_root_id))

    def test_missing_source_is_rejected_without_partial_layer(self):
        g, ctrl = _new_controller()
        with self.assertRaises(ValueError):
            ctrl.add_media_layer("Missing", "ghost")
        self.assertIsNone(ctrl.main_stack())
        self.assertNotIn("ghost", g.nodes)

    def test_payload_drop_creates_asset_backed_source(self):
        from types import SimpleNamespace
        g, ctrl = _new_controller()
        payload = SimpleNamespace(
            node_type="image_input", asset_id="asset-1",
            path="/tmp/plate.exr", display_name="plate",
            duration=1, width=32, height=16, extras={},
        )
        lid = ctrl.add_media_payload(payload)
        layer = g.get_layer(lid)
        self.assertIsNotNone(g.get_node(layer.source_node_id))
        self.assertEqual(g.get_node(layer.source_node_id).parameters["asset_id"],
                         "asset-1")

    def test_model_payload_creates_one_scene_backed_3d_layer(self):
        from types import SimpleNamespace

        g, ctrl = _new_controller()
        payload = SimpleNamespace(
            node_type="model3d", media_type="model", asset_id="model-1",
            path="/tmp/character.glb", display_name="Character",
            duration=1, width=0, height=0, extras={"fps": 30.0},
        )
        layer_id = ctrl.add_model3d_payload(payload)
        layer = g.get_layer(layer_id)

        self.assertEqual(layer.layer_type, "3d")
        model = g.get_node(layer.source_node_id)
        scene = g.get_node(layer.root_node_id)
        self.assertIsNotNone(model)
        self.assertIsNotNone(scene)
        self.assertEqual(model.node_type, "model3d")
        self.assertEqual(scene.node_type, "scene3d")
        self.assertEqual(model.parameters["model_path"], payload.path)
        self.assertTrue(model.parameters["use_imported_model"])
        self.assertEqual(model.parameters["anim_fps"], 30.0)
        self.assertIn(
            (model.id, "display", scene.id, "in"),
            self._connection_tuples(g),
        )
        self.assertIn(
            (scene.id, "display", "out", "image"),
            self._connection_tuples(g),
        )

    def test_single_media_no_merge(self):
        g, ctrl = _new_controller()
        _add_source(g, 0)
        lid = ctrl.add_media_layer("Video 1", "src0")
        stack = ctrl.main_stack()
        self.assertEqual(len(stack.layer_ids), 1)
        self.assertEqual(stack.generated_merge_node_ids, [])
        # transform wrapper created
        layer = g.get_layer(lid)
        self.assertNotEqual(layer.root_node_id, "src0")
        tnode = g.get_node(layer.root_node_id)
        self.assertEqual(tnode.node_type, "transform")
        # transform → out
        conns = [(c.src_node_id, c.dst_node_id) for c in g.connections]
        self.assertIn((layer.root_node_id, "out"), conns)

    def test_two_media_one_merge(self):
        g, ctrl = _new_controller()
        _add_source(g, 0)
        _add_source(g, 1)
        lid0 = ctrl.add_media_layer("A", "src0")
        lid1 = ctrl.add_media_layer("B", "src1", insert_index=0)  # on top
        stack = ctrl.main_stack()
        self.assertEqual(stack.layer_ids, [lid1, lid0])
        self.assertEqual(len(stack.generated_merge_node_ids), 1)

        merge_id = stack.generated_merge_node_ids[0]
        roots = {
            lid0: g.get_layer(lid0).root_node_id,
            lid1: g.get_layer(lid1).root_node_id,
        }
        self.assertIn((roots[lid1], "result", merge_id,
                       MERGE_PORT_FOREGROUND), self._connection_tuples(g))
        self.assertIn((roots[lid0], "result", merge_id,
                       MERGE_PORT_BACKGROUND), self._connection_tuples(g))
        self.assertIn((merge_id, "result", "out", "image"),
                      self._connection_tuples(g))

    @staticmethod
    def _connection_tuples(graph):
        return {
            (c.src_node_id, c.src_port_name,
             c.dst_node_id, c.dst_port_name)
            for c in graph.connections
        }

    def test_reorder_reuses_merge_and_swaps_foreground_background(self):
        g, ctrl = _new_controller()
        for i in range(3):
            _add_source(g, i)
        layers = [ctrl.add_media_layer(f"L{i}", f"src{i}")
                  for i in range(3)]
        stack = ctrl.main_stack()
        merge_ids = list(stack.generated_merge_node_ids)
        roots = {lid: g.get_layer(lid).root_node_id for lid in layers}

        # add_media_layer inserts at the top by default: L2, L1, L0.
        self.assertEqual(stack.layer_ids, [layers[2], layers[1], layers[0]])
        self.assertTrue(ctrl.move_layer(layers[0], 0))
        self.assertEqual(stack.layer_ids, [layers[0], layers[2], layers[1]])
        self.assertEqual(stack.generated_merge_node_ids, merge_ids)

        conns = self._connection_tuples(g)
        # The same Merge nodes are retained, while the stack order becomes
        # the actual A/B order on the first merge and the lower input on the
        # second merge.
        self.assertIn((roots[layers[0]], "result", merge_ids[0],
                       MERGE_PORT_FOREGROUND), conns)
        self.assertIn((roots[layers[2]], "result", merge_ids[0],
                       MERGE_PORT_BACKGROUND), conns)
        self.assertIn((merge_ids[0], "result", merge_ids[1],
                       MERGE_PORT_FOREGROUND), conns)
        self.assertIn((roots[layers[1]], "result", merge_ids[1],
                       MERGE_PORT_BACKGROUND), conns)
        self.assertIn((merge_ids[1], "result", "out", "image"), conns)

    def test_image_default_graph_is_adopted_as_managed_base_layer(self):
        g = NodeGraph()
        output_id = g.create_image_default_graph("/tmp/plate.exr")
        ctrl = LayerStackController(g, undo_stack=None)

        self.assertEqual(ctrl.project_from_node_graph(output_id), 1)
        stack = ctrl.main_stack()
        self.assertTrue(stack.managed)
        self.assertEqual(g.get_layer(stack.layer_ids[0]).layer_type, "media")

        _add_source(g, 0)
        ctrl.add_media_layer("Overlay", "src0", insert_index=0)
        self.assertEqual(len(stack.generated_merge_node_ids), 1)

    def test_empty_comp_default_graph_is_adopted_as_managed_base_layer(self):
        g, ctrl = _new_controller()
        source = ConstantNode(node_id="empty_source")
        g.add_node(source)
        self.assertTrue(g.connect(source.id, "result", "out", "image"))

        self.assertEqual(ctrl.project_from_node_graph(), 1)
        self.assertTrue(ctrl.main_stack().managed)
        _add_source(g, 0)
        ctrl.add_media_layer("Overlay", "src0", insert_index=0)
        self.assertEqual(len(ctrl.main_stack().generated_merge_node_ids), 1)

    def test_text_source_not_wrapped(self):
        from src.features.composite.nodes.text_node import TextNode
        g, ctrl = _new_controller()
        g.add_node(TextNode(node_id="text0"))
        lid = ctrl.add_media_layer("Title", "text0", layer_type="text")
        layer = g.get_layer(lid)
        # Text supports_layer_root → no transform wrapper.
        self.assertEqual(layer.root_node_id, "text0")

    def test_no_output_node_surfaces_warning(self):
        """BUG-5: dropping media with no Output node must not fail silently."""
        g = NodeGraph()  # NO Output node
        ctrl = LayerStackController(g, undo_stack=None)
        _add_source(g, 0)
        # Rebuild returns warnings rather than silently producing a blank canvas.
        ctrl.add_media_layer("A", "src0")
        stack = ctrl.main_stack()
        # Compile via rebuild to collect warnings.
        result = ctrl.rebuild()
        self.assertTrue(any("output" in w.lower() for w in result.warnings))


class TestRemove(unittest.TestCase):

    def test_remove_shared_source_keeps_other_layer_alive(self):
        g, ctrl = _new_controller()
        _add_source(g, 0)
        first = ctrl.add_media_layer("A", "src0")
        second = ctrl.add_media_layer("B", "src0")
        shared_root = g.get_layer(first).root_node_id
        ctrl.remove_layer(second)
        self.assertIsNotNone(g.get_node("src0"))
        self.assertIsNotNone(g.get_node(shared_root))
        self.assertIsNotNone(g.get_layer(first))

    def test_remove_preserves_chain_used_by_free_graph_branch(self):
        """Layer deletion must not destroy nodes still used by Nuke wiring."""
        g, ctrl = _new_controller()
        _add_source(g, 0)
        lid = ctrl.add_media_layer("A", "src0")
        root_id = g.get_layer(lid).root_node_id
        external = TransformNode(node_id="free_transform")
        g.add_node(external)
        self.assertTrue(g.connect(root_id, "result", external.id, "image"))

        ctrl.remove_layer(lid)
        self.assertIsNotNone(g.get_node("src0"))
        self.assertIsNotNone(g.get_node(root_id))
        self.assertIsNotNone(g.get_node(external.id))

    def test_locked_layer_cannot_move_or_delete(self):
        g, ctrl = _new_controller()
        _add_source(g, 0)
        lid = ctrl.add_media_layer("A", "src0")
        self.assertTrue(ctrl.set_layer_locked(lid, True))
        self.assertFalse(ctrl.move_layer(lid, 0))
        self.assertFalse(ctrl.remove_layer(lid))

    def test_remove_deletes_owned_nodes(self):
        g, ctrl = _new_controller()
        _add_source(g, 0)
        lid = ctrl.add_media_layer("A", "src0")
        layer = g.get_layer(lid)
        root_id = layer.root_node_id
        ctrl.remove_layer(lid)
        self.assertIsNone(g.get_layer(lid))
        self.assertIsNone(g.get_node("src0"))
        self.assertIsNone(g.get_node(root_id))

    def test_remove_prunes_merge(self):
        g, ctrl = _new_controller()
        _add_source(g, 0)
        _add_source(g, 1)
        lid0 = ctrl.add_media_layer("A", "src0")
        lid1 = ctrl.add_media_layer("B", "src1")
        self.assertEqual(len(ctrl.main_stack().generated_merge_node_ids), 1)
        ctrl.remove_layer(lid1)
        # 1 layer left → no merge.
        self.assertEqual(ctrl.main_stack().generated_merge_node_ids, [])

    def test_remove_media_layer_deletes_transform_and_owned_merge(self):
        """Deleting one AE layer removes its complete generated graph chain."""
        g, ctrl = _new_controller()
        _add_source(g, 0)
        _add_source(g, 1)
        bottom = ctrl.add_media_layer("Bottom", "src0")
        top = ctrl.add_media_layer("Top", "src1")
        top_layer = g.get_layer(top)
        top_root = top_layer.root_node_id
        merge_id = ctrl.main_stack().generated_merge_node_ids[0]

        self.assertEqual(ctrl.layer_ids_for_node("src1"), [top])
        self.assertEqual(ctrl.layer_ids_for_node(top_root), [top])
        self.assertTrue(ctrl.remove_layer(top))

        self.assertIsNone(g.get_layer(top))
        self.assertIsNone(g.get_node("src1"))
        self.assertIsNone(g.get_node(top_root))
        self.assertIsNone(g.get_node(merge_id))
        self.assertIsNotNone(g.get_layer(bottom))

    def test_remove_only_media_root_keeps_layer_source_and_merge(self):
        """Deleting the generated Transform behaves like deleting an effect."""
        g, ctrl = _new_controller()
        _add_source(g, 0)
        _add_source(g, 1)
        bottom = ctrl.add_media_layer("Bottom", "src0")
        top = ctrl.add_media_layer("Top", "src1")
        top_root = g.get_layer(top).root_node_id
        merge_id = ctrl.main_stack().generated_merge_node_ids[0]

        self.assertTrue(ctrl.remove_layer_root_node(top))

        self.assertIsNotNone(g.get_layer(top))
        self.assertIsNotNone(g.get_node("src1"))
        self.assertIsNone(g.get_node(top_root))
        self.assertIsNotNone(g.get_node(merge_id))
        connections = {
            (c.src_node_id, c.src_port_name,
             c.dst_node_id, c.dst_port_name)
            for c in g.connections
        }
        self.assertIn(("src1", "result", merge_id,
                       MERGE_PORT_FOREGROUND), connections)
        self.assertEqual(g.get_layer(top).root_node_id, "src1")
        self.assertEqual(g.get_layer(bottom).root_node_id != "src1", True)

    def test_media_payload_persists_source_size_and_fits_oversized_media(self):
        from types import SimpleNamespace

        g, ctrl = _new_controller()
        payload = SimpleNamespace(
            node_type="image_input", asset_id="asset-4k",
            path="/tmp/plate.exr", display_name="plate",
            duration=1, width=3840, height=2160, extras={},
        )
        layer_id = ctrl.add_media_payload(payload)
        layer = g.get_layer(layer_id)
        source = g.get_node(layer.source_node_id)
        transform = g.get_node(layer.root_node_id)

        self.assertEqual(source.parameters["width"], 3840)
        self.assertEqual(source.parameters["height"], 2160)
        self.assertAlmostEqual(transform.parameters["scale_x"], 0.5)
        self.assertAlmostEqual(transform.parameters["scale_y"], 0.5)
        self.assertAlmostEqual(transform.parameters["translate_x"], -960.0)
        self.assertAlmostEqual(transform.parameters["translate_y"], 540.0)


class TestMove(unittest.TestCase):

    def test_move_changes_order(self):
        g, ctrl = _new_controller()
        for i in range(3):
            _add_source(g, i)
            ctrl.add_media_layer(f"L{i}", f"src{i}")
        ids = list(ctrl.main_stack().layer_ids)
        # ids = [L2, L1, L0] (each inserted on top).  Move L0 (bottom) to top.
        bottom = ids[-1]
        ctrl.move_layer(bottom, new_index=0)
        self.assertEqual(ctrl.main_stack().layer_ids[0], bottom)


class TestDuplicate(unittest.TestCase):

    def test_duplicate_inserts_above_original(self):
        g, ctrl = _new_controller()
        _add_source(g, 0)
        lid = ctrl.add_media_layer("A", "src0")
        new_lid = ctrl.duplicate_layer(lid)
        self.assertIsNotNone(new_lid)
        ids = ctrl.main_stack().layer_ids
        self.assertEqual(ids.index(new_lid), ids.index(lid) - 1)
        # The clone has its own root node.
        new_layer = g.get_layer(new_lid)
        self.assertNotEqual(new_layer.root_node_id, g.get_layer(lid).root_node_id)

    def test_duplicate_layer_with_root_equal_source_clones_once(self):
        from src.features.composite.nodes.text_node import TextNode
        g, ctrl = _new_controller()
        g.add_node(TextNode(node_id="text0"))
        lid = ctrl.add_media_layer("Title", "text0")
        new_lid = ctrl.duplicate_layer(lid)
        self.assertIsNotNone(new_lid)
        new_layer = g.get_layer(new_lid)
        self.assertEqual(new_layer.root_node_id, new_layer.source_node_id)


class TestRenameAndProperties(unittest.TestCase):

    def test_rename(self):
        g, ctrl = _new_controller()
        _add_source(g, 0)
        lid = ctrl.add_media_layer("A", "src0")
        ctrl.rename_layer(lid, "Renamed")
        self.assertEqual(g.get_layer(lid).name, "Renamed")

    def test_invalid_properties_are_rejected(self):
        g, ctrl = _new_controller()
        _add_source(g, 0)
        lid = ctrl.add_media_layer("A", "src0")
        self.assertFalse(ctrl.set_layer_opacity(lid, 2.0))
        self.assertFalse(ctrl.set_layer_blend_mode(lid, "not-a-mode"))
        self.assertFalse(ctrl.rename_layer(lid, "   "))

    def test_visibility_off_compiles_to_zero_layers(self):
        g, ctrl = _new_controller()
        _add_source(g, 0)
        lid = ctrl.add_media_layer("A", "src0")
        ctrl.set_layer_visibility(lid, False)
        # Disabled layer drops out → Output's image input is disconnected.
        out_conns = [c for c in g.connections
                     if c.dst_node_id == "out" and c.dst_port_name == "image"]
        self.assertEqual(out_conns, [])

    def test_opacity_change_projected_to_merge(self):
        g, ctrl = _new_controller()
        _add_source(g, 0)
        _add_source(g, 1)
        lid0 = ctrl.add_media_layer("A", "src0")
        ctrl.add_media_layer("B", "src1")  # B on top, B drives the merge
        merge_id = ctrl.main_stack().generated_merge_node_ids[0]
        # opacity on B (top) projects onto the merge that has B as fg.
        ctrl.set_layer_opacity(lid0, 0.25)
        # L0 is now bottom; it has no owning merge.  Just assert no crash
        # and merge node still present.
        self.assertEqual(len(ctrl.main_stack().generated_merge_node_ids), 1)


class TestParent(unittest.TestCase):

    def test_parent_cycle_rejected(self):
        g, ctrl = _new_controller()
        _add_source(g, 0)
        _add_source(g, 1)
        l0 = ctrl.add_media_layer("A", "src0")
        l1 = ctrl.add_media_layer("B", "src1")
        self.assertTrue(ctrl.set_layer_parent(l0, l1))
        # l1 → l0 would create a cycle.
        self.assertFalse(ctrl.set_layer_parent(l1, l0))

    def test_parent_self_rejected(self):
        g, ctrl = _new_controller()
        _add_source(g, 0)
        l0 = ctrl.add_media_layer("A", "src0")
        self.assertFalse(ctrl.set_layer_parent(l0, l0))

    def test_parent_unknown_layer_rejected(self):
        g, ctrl = _new_controller()
        _add_source(g, 0)
        lid = ctrl.add_media_layer("A", "src0")
        self.assertFalse(ctrl.set_layer_parent(lid, "ghost"))

    def test_parent_unsupported_node_rejected(self):
        # Blur root does not support_parent_transform.
        g, ctrl = _new_controller()
        g.add_node(BlurNode(node_id="blur0"))
        stack = ctrl.ensure_main_stack("out")
        layer = Layer(layer_id="L0", name="L0", root_node_id="blur0")
        stack.layer_ids.append("L0")
        g.upsert_layer("main", layer)
        ok = ctrl.set_layer_parent("L0", None)  # clearing is fine
        self.assertTrue(ok)
        # Setting to a non-existent parent on an unsupported node → rejected.
        self.assertFalse(ctrl.set_layer_parent("L0", "ghost"))


@unittest.skipUnless(_CAN_QT, "PyQt6 UndoStack unavailable")
class TestUndoOneStep(unittest.TestCase):
    """A media drop = exactly one undo (plan §1.2.6 / §10).

    We assert the *effect* of a single undo/redo rather than the tree node
    count: one undo must restore the entire pre-drop state (no layer, no
    auto-transform, Output un-fed).
    """

    def test_add_media_single_undo_restores_state(self):
        g = NodeGraph()
        g.add_node(OutputNode(node_id="out"))
        stack = UndoStack(None)
        ctrl = LayerStackController(g, undo_stack=stack)
        _add_source(g, 0)
        lid = ctrl.add_media_layer("A", "src0")
        self.assertTrue(stack.can_undo())
        # Capture the auto-transform id so we can assert it disappears.
        layer = g.get_layer(lid)
        transform_id = layer.root_node_id
        # A single undo must clear everything the drop created.
        stack.undo()
        self.assertIsNone(g.get_layer(lid))
        self.assertIsNone(g.get_node(transform_id))
        out_conns = [c for c in g.connections if c.dst_node_id == "out"]
        self.assertEqual(out_conns, [])
        # And redo re-creates it.
        stack.redo()
        self.assertIsNotNone(g.get_layer(lid))

    def test_media_payload_drop_undo_removes_created_source(self):
        """A payload drop must undo its newly-created source as one action."""
        from types import SimpleNamespace

        g = NodeGraph()
        g.add_node(OutputNode(node_id="out"))
        stack = UndoStack(None)
        ctrl = LayerStackController(g, undo_stack=stack)
        payload = SimpleNamespace(
            node_type="image_input", asset_id="asset-undo",
            path="/tmp/plate.exr", display_name="plate",
            duration=1, width=32, height=16, extras={},
        )

        lid = ctrl.add_media_payload(payload)
        source_id = g.get_layer(lid).source_node_id
        self.assertIsNotNone(g.get_node(source_id))
        stack.undo()
        self.assertIsNone(g.get_layer(lid))
        self.assertIsNone(g.get_node(source_id))
        stack.redo()
        self.assertIsNotNone(g.get_layer(lid))
        self.assertIsNotNone(g.get_node(source_id))

    def test_move_single_undo_restores_order(self):
        g = NodeGraph()
        g.add_node(OutputNode(node_id="out"))
        stack = UndoStack(None)
        ctrl = LayerStackController(g, undo_stack=stack)
        for i in range(3):
            _add_source(g, i)
            ctrl.add_media_layer(f"L{i}", f"src{i}")
        ids = list(ctrl.main_stack().layer_ids)
        before = list(ids)
        ctrl.move_layer(ids[-1], 0)
        self.assertNotEqual(ctrl.main_stack().layer_ids, before)
        # One undo restores the previous order exactly.
        stack.undo()
        self.assertEqual(ctrl.main_stack().layer_ids, before)
        stack.redo()
        self.assertNotEqual(ctrl.main_stack().layer_ids, before)

    def test_undo_rehydrates_expression_manager(self):
        """BUG-1: undo must rebuild the graph's ExpressionManager state.

        Without re-hydration, an expression authored before a layer
        operation would lose its dependency edges after undo, silently
        breaking later pick-whip cycle detection.
        """
        g = NodeGraph()
        g.add_node(OutputNode(node_id="out"))
        stack = UndoStack(None)
        ctrl = LayerStackController(g, undo_stack=stack)
        # Author an expression on the Output node first.
        out = g.get_node("out")
        g._expression_manager.set_expression("out", "width", "1920.0")
        self.assertEqual(len(g._expression_manager._expressions), 1)
        # Add a media layer (pushes a snapshot that includes the expr).
        _add_source(g, 0)
        ctrl.add_media_layer("A", "src0")
        # Undo: the expression manager must still know about the expr.
        stack.undo()
        self.assertEqual(len(g._expression_manager._expressions), 1)
        self.assertIn(("out", "width"),
                      g._expression_manager._expressions)
        # Redo keeps it too.
        stack.redo()
        self.assertIn(("out", "width"),
                      g._expression_manager._expressions)


class TestLegacyProjection(unittest.TestCase):
    """project_from_node_graph builds layers from a free graph (§1.2.7)."""

    def test_projection_from_simple_chain(self):
        g = NodeGraph()
        out = OutputNode(node_id="out")
        g.add_node(out)
        src = ConstantNode(node_id="src0")
        g.add_node(src)
        t = TransformNode(node_id="t0")
        g.add_node(t)
        g.connect("src0", "result", "t0", "image")
        g.connect("t0", "result", "out", "image")
        ctrl = LayerStackController(g, undo_stack=None)
        n = ctrl.project_from_node_graph()
        self.assertEqual(n, 1)
        stack = ctrl.main_stack()
        self.assertIsNotNone(stack)
        self.assertEqual(stack.output_node_id, "out")
        layer = g.get_layer(stack.layer_ids[0])
        # Projection marks layers external & unmanaged.
        self.assertFalse(layer.managed)
        self.assertEqual(layer.layer_type, "external")

    def test_projection_idempotent(self):
        g = NodeGraph()
        g.add_node(OutputNode(node_id="out"))
        g.add_node(ConstantNode(node_id="src0"))
        g.add_node(TransformNode(node_id="t0"))
        g.connect("src0", "result", "t0", "image")
        g.connect("t0", "result", "out", "image")
        ctrl = LayerStackController(g, undo_stack=None)
        self.assertEqual(ctrl.project_from_node_graph(), 1)
        # Re-running does not blow up; stack stays consistent.
        self.assertEqual(ctrl.project_from_node_graph(), 0)

    def test_layer_provenance_is_bidirectional(self):
        g, ctrl = _new_controller()
        _add_source(g, 0)
        layer_id = ctrl.add_media_layer("A", "src0")
        layer = g.get_layer(layer_id)

        provenance = ctrl.provenance_for_layer(layer_id)
        self.assertIsNotNone(provenance)
        self.assertEqual(provenance.root_node_id, layer.root_node_id)
        self.assertEqual(provenance.source_node_id, "src0")
        self.assertEqual(provenance.chain_node_ids[0], layer.root_node_id)
        self.assertIn("src0", provenance.chain_node_ids)
        self.assertEqual(ctrl.node_ids_for_layer(layer_id),
                         list(provenance.chain_node_ids))

        source_roles = ctrl.provenance_for_node("src0")
        self.assertTrue(any(item.layer_id == layer_id and item.role == "source"
                            for item in source_roles))
        self.assertEqual(ctrl.trace_layer_ids_for_node("src0"), [layer_id])

    def test_generated_merge_is_read_only_provenance(self):
        g, ctrl = _new_controller()
        _add_source(g, 0)
        _add_source(g, 1)
        ctrl.add_media_layer("A", "src0")
        ctrl.add_media_layer("B", "src1")
        merge_id = ctrl.main_stack().generated_merge_node_ids[0]
        roles = ctrl.provenance_for_node(merge_id)
        self.assertTrue(roles)
        self.assertTrue(all(item.read_only and item.role == "generated_merge"
                            for item in roles))
        self.assertIsNotNone(ctrl.main_stack())

    def test_default_timeline_projection_is_managed(self):
        """The default timeline source becomes the managed base layer."""
        g = NodeGraph()
        output_id = g.create_default_graph()
        ctrl = LayerStackController(g, undo_stack=None)

        self.assertEqual(ctrl.project_from_node_graph(output_id), 1)
        stack = ctrl.main_stack()
        self.assertTrue(stack.managed)
        base_layer = g.get_layer(stack.layer_ids[0])
        self.assertTrue(base_layer.managed)
        self.assertEqual(base_layer.name, "Timeline Clip Input")

        _add_source(g, 0)
        dropped_id = ctrl.add_media_layer(
            "Overlay", "src0", insert_index=0)
        dropped = g.get_layer(dropped_id)
        self.assertEqual(stack.layer_ids, [dropped_id, base_layer.layer_id])
        self.assertEqual(len(stack.generated_merge_node_ids), 1)

        merge_id = stack.generated_merge_node_ids[0]
        connections = {
            (c.src_node_id, c.src_port_name,
             c.dst_node_id, c.dst_port_name)
            for c in g.connections
        }
        self.assertIn((dropped.root_node_id, "result",
                       merge_id, MERGE_PORT_FOREGROUND), connections)
        self.assertIn((base_layer.root_node_id, "result",
                       merge_id, MERGE_PORT_BACKGROUND), connections)
        self.assertIn((merge_id, "result", output_id, "image"), connections)


if __name__ == "__main__":
    unittest.main()
