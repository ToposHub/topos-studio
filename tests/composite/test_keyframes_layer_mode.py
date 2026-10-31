"""Tests for KeyframesModel layer mode (plan §6.1 / §6.2).

Covers:
  * view_mode state (nodes / layers)
  * rebuild_layers produces one layer row per layer, foreground-first
  * layer_index numbering (top = 1)
  * child parameter rows indented under each layer
  * visible_rows respects layer expansion
  * detached layers are hidden while their graph/layer metadata is retained
"""

import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from types import SimpleNamespace

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.core.layer_stack_controller import (
    LayerStackController,
)
from src.features.composite.ui.keyframes_model import KeyframesModel
from src.features.composite.nodes.output_node import OutputNode
from src.features.composite.nodes.constant_node import ConstantNode

pytestmark = pytest.mark.composite_core


def _controller_with_layers(n: int):
    g = NodeGraph()
    g.add_node(OutputNode(node_id="out"))
    ctrl = LayerStackController(g, undo_stack=None)
    for i in range(n):
        src = ConstantNode(node_id=f"src{i}")
        g.add_node(src)
        ctrl.add_media_layer(f"Layer {i}", f"src{i}", insert_index=0)
    return ctrl


class TestViewMode(unittest.TestCase):

    def test_default_is_nodes(self):
        m = KeyframesModel()
        self.assertEqual(m.view_mode, "nodes")

    def test_set_view_mode(self):
        m = KeyframesModel()
        m.set_view_mode("layers")
        self.assertEqual(m.view_mode, "layers")
        m.set_view_mode("nodes")
        self.assertEqual(m.view_mode, "nodes")

    def test_invalid_mode_ignored(self):
        m = KeyframesModel()
        m.set_view_mode("bogus")
        self.assertEqual(m.view_mode, "nodes")


class TestRebuildLayers(unittest.TestCase):

    def test_layer_rows_foreground_first(self):
        ctrl = _controller_with_layers(3)
        # add_media_layer insert_index=0 → last added is on top.
        # Layers added: L0 (bottom), L1, L2 (top).
        m = KeyframesModel()
        m.rebuild_layers(ctrl)
        layer_rows = [r for r in m.rows if r.row_type == "layer"]
        self.assertEqual(len(layer_rows), 3)
        # Top layer (added last, "Layer 2") gets index 1.
        self.assertEqual(layer_rows[0].layer_index, 1)
        self.assertEqual(layer_rows[0].label, "Layer 2")
        self.assertEqual(layer_rows[-1].layer_index, 3)
        self.assertEqual(layer_rows[-1].label, "Layer 0")

    def test_child_param_rows_indented(self):
        ctrl = _controller_with_layers(1)
        m = KeyframesModel()
        m.rebuild_layers(ctrl)
        # The layer row is depth 0; any animatable params are depth 1.
        layer_rows = [r for r in m.rows if r.row_type == "layer"]
        param_rows = [r for r in m.rows if r.row_type == "param"]
        self.assertTrue(all(r.depth == 0 for r in layer_rows))
        self.assertTrue(all(r.depth == 1 for r in param_rows))
        # Param rows belong to the layer's root node.
        layer = layer_rows[0]
        for pr in param_rows:
            self.assertEqual(pr.node_id, layer.node_id)

    def test_detached_layer_is_hidden_until_reconnected(self):
        ctrl = _controller_with_layers(1)
        # Simulate a Nodes-mode disconnect. The source and Layer metadata stay
        # alive, but the root no longer contributes to Output.
        layer = ctrl.main_stack()
        lid = layer.layer_ids[0]
        root_id = ctrl.graph.get_layer(lid).root_node_id
        ctrl.graph.disconnect(root_id, "result", "out", "image")
        m = KeyframesModel()
        m.rebuild_layers(ctrl)
        layer_rows = [r for r in m.rows if r.row_type == "layer"]
        self.assertEqual(layer_rows, [])
        self.assertIsNotNone(ctrl.graph.get_node(root_id))

        # Reconnecting the same root to Output makes the existing Layer row
        # visible again without recreating the layer or its animation state.
        ctrl.graph.connect(root_id, "result", "out", "image")
        m.rebuild_layers(ctrl)
        layer_rows = [r for r in m.rows if r.row_type == "layer"]
        self.assertEqual(len(layer_rows), 1)
        self.assertEqual(layer_rows[0].layer_id, lid)

    def test_layers_follow_merge_to_output_path(self):
        """Both material layers disappear when their Merge leaves Output."""
        ctrl = _controller_with_layers(2)
        stack = ctrl.main_stack()
        merge_id = stack.generated_merge_node_ids[0]
        m = KeyframesModel()

        m.rebuild_layers(ctrl)
        self.assertEqual(
            {row.layer_id for row in m.rows if row.row_type == "layer"},
            set(stack.layer_ids),
        )

        ctrl.graph.disconnect(merge_id, "result", "out", "image")
        m.rebuild_layers(ctrl)
        self.assertEqual([], [r for r in m.rows if r.row_type == "layer"])

        ctrl.graph.connect(merge_id, "result", "out", "image")
        m.rebuild_layers(ctrl)
        self.assertEqual(
            {row.layer_id for row in m.rows if row.row_type == "layer"},
            set(stack.layer_ids),
        )

    def test_visible_rows_respects_layer_collapse(self):
        ctrl = _controller_with_layers(1)
        m = KeyframesModel()
        m.rebuild_layers(ctrl)
        # Initially expanded → params visible.
        before = len(m.visible_rows())
        layer_row = next(r for r in m.rows if r.row_type == "layer")
        layer_row.expanded = False
        after = len(m.visible_rows())
        self.assertLess(after, before)
        # The layer row itself stays visible.
        self.assertIn(layer_row, m.visible_rows())

    def test_empty_stack(self):
        g = NodeGraph()
        g.add_node(OutputNode(node_id="out"))
        ctrl = LayerStackController(g, undo_stack=None)
        m = KeyframesModel()
        m.rebuild_layers(ctrl)
        self.assertEqual(m.rows, [])

    def test_3d_layer_projects_model_animation_properties(self):
        g = NodeGraph()
        g.add_node(OutputNode(node_id="out"))
        ctrl = LayerStackController(g, undo_stack=None)
        layer_id = ctrl.add_model3d_payload(SimpleNamespace(
            node_type="model3d", media_type="model", asset_id="model-1",
            path="/tmp/character.glb", display_name="Character",
            duration=1, width=0, height=0, extras={},
        ))

        m = KeyframesModel()
        m.rebuild_layers(ctrl, selected_layer_id=layer_id)
        layer_row = next(r for r in m.rows if r.row_type == "layer")
        model_row = next(r for r in m.rows
                         if r.row_type == "node"
                         and r.node_id.startswith("model3d_"))
        animation_params = {
            r.param_name for r in m.rows
            if r.row_type == "param" and r.node_id == model_row.node_id
        }
        self.assertEqual(layer_row.layer_type, "3d")
        self.assertEqual(model_row.parent_row_id, layer_row.row_id)
        self.assertIn("anim_speed", animation_params)
        self.assertIn("anim_play_mode", animation_params)
        self.assertIn("anim_fps", animation_params)

    def test_layer_rows_expose_track_matte_relation_badges(self):
        ctrl = _controller_with_layers(2)
        stack = ctrl.main_stack()
        target_id, matte_id = stack.layer_ids[0], stack.layer_ids[1]
        self.assertTrue(ctrl.set_layer_track_matte(
            target_id, matte_id, mode="luma", invert=True))
        m = KeyframesModel()
        m.rebuild_layers(ctrl)
        rows = {row.layer_id: row for row in m.rows if row.row_type == "layer"}
        self.assertIn("ML!", rows[target_id].relation_badge)
        self.assertEqual(rows[target_id].relation_tooltip.split(":", 1)[0],
                         "Track matte")
        self.assertIn("S1", rows[matte_id].relation_badge)


if __name__ == "__main__":
    unittest.main()
