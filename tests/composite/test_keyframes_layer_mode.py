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
from src.features.composite.nodes.mask_node import MaskNode
from src.shared.keyframe import Keyframe, KeyframeTrack

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


def test_layer_visibility_and_solo_keep_timeline_rows_editable():
    controller = _controller_with_layers(3)
    top, middle, bottom = controller.main_stack().layer_ids
    assert controller.set_layer_visibility(top, False)
    assert controller.set_layer_solo(middle, True)

    model = KeyframesModel()
    model.rebuild_layers(controller)
    layers = [row for row in model.visible_rows() if row.row_type == "layer"]
    assert [row.layer_id for row in layers] == [top, middle, bottom]
    assert not layers[0].layer_enabled
    assert layers[1].layer_solo


def test_new_layers_hide_all_child_rows_until_expanded():
    graph = NodeGraph()
    graph.add_node(OutputNode(node_id="out"))
    controller = LayerStackController(graph, layer_stack_version=2)
    layer_id = controller.create_generated_layer("text", "Title", {"text": "Title"})

    model = KeyframesModel()
    model.rebuild_layers(controller)
    [layer_row] = model.visible_rows()
    assert layer_row.layer_id == layer_id
    model.set_expanded(layer_row.node_id, True)
    assert any(row.param_name == "opacity" for row in model.visible_rows())
    model.set_expanded(layer_row.node_id, False)
    assert model.visible_rows() == [layer_row]


def test_groups_follow_render_nodes_and_collapse_by_view_identity():
    graph = NodeGraph()
    graph.add_node(OutputNode(node_id="out"))
    controller = LayerStackController(graph, layer_stack_version=2)
    layer_id = controller.create_generated_layer("text", "Title", {"text": "Title"})
    effect_id = controller.add_layer_effect(layer_id, "blur")
    assert effect_id is not None
    mask = MaskNode(node_id="soft_shape")
    graph.add_node(mask)
    assert graph.connect(mask.id, "matte", effect_id, "mask")

    model = KeyframesModel()
    model.rebuild_layers(controller, selected_layer_id=layer_id)
    layer_row_id = f"layer:{layer_id}"
    assert model.set_row_expanded(layer_row_id, True)
    assert [r.row_id for r in model.visible_rows() if r.parent_row_id == layer_row_id] == [
        f"{layer_row_id}:group:{category}"
        for category in ("masks", "effects", "transform", "content", "advanced")
    ]
    layer = graph.get_layer(layer_id)
    source = next(r for r in model.rows if r.row_type == "node" and r.node_id == layer.source_node_id)
    assert source.parent_row_id == f"{layer_row_id}:group:content"
    mask_row = next(r for r in model.rows if r.row_type == "node" and r.node_id == mask.id)
    effect_row = next(r for r in model.rows if r.row_type == "node" and r.node_id == effect_id)
    assert mask_row.parent_row_id == f"{layer_row_id}:group:masks"
    assert effect_row.parent_row_id == f"{layer_row_id}:group:effects"
    mask_param = next(r for r in model.rows if r.parent_row_id == mask_row.row_id)
    assert model.reveal_row(mask_param.row_id)
    assert mask_param in model.visible_rows()
    assert model.set_row_expanded(mask_row.row_id, False)
    assert mask_param not in model.visible_rows()
    assert mask_row in model.visible_rows()
    assert effect_row not in model.visible_rows()
    assert controller.set_layer_locked(layer_id, True)
    model.rebuild_layers(controller, selected_layer_id=layer_id)
    assert all(row.locked for row in model.rows)


def test_filter_reveals_component_ancestors_without_changing_expansion():
    graph = NodeGraph()
    graph.add_node(OutputNode(node_id="out"))
    controller = LayerStackController(graph, layer_stack_version=2)
    layer_id = controller.create_generated_layer("solid", "Card", {})
    layer = graph.get_layer(layer_id)
    root = graph.get_node(layer.root_node_id)
    root.set_keyframe_track("translate_x", KeyframeTrack("translate_x", [Keyframe(3, 40.0)]))
    model = KeyframesModel()
    model.set_filter("animated")
    model.rebuild_layers(controller, selected_layer_id=layer_id)
    layer_row, transform_row, position_row, x_row = model.visible_rows()
    assert layer_row.layer_id == layer_id
    assert transform_row.row_id == f"layer:{layer_id}:group:transform"
    assert position_row.component_params == ("translate_x", "translate_y")
    assert position_row.node_id == root.id
    assert x_row.row_id == f"param:{root.id}:translate_x"
    assert model.track_for(x_row.row_id).keyframes[0].value == 40.0

    model.set_filter("all")
    model.set_text_filter("translate_y")
    model.rebuild_layers(controller, selected_layer_id=layer_id)
    assert model.visible_rows()[-1].row_id == f"param:{root.id}:translate_y"
    model.set_text_filter("")
    model.rebuild_layers(controller, selected_layer_id=layer_id)
    assert [r.row_type for r in model.visible_rows()] == ["layer"]


def test_legacy_layer_gains_groups_without_migrating_tracks_or_topology():
    original = _controller_with_layers(1)
    old_layer_id = original.main_stack().layer_ids[0]
    old_layer = original.graph.get_layer(old_layer_id)
    original.graph.get_node(old_layer.root_node_id).set_keyframe_track(
        "translate_y", KeyframeTrack("translate_y", [Keyframe(7, 21.0)]))
    payload = original.graph.to_dict()
    del payload["layer_stacks"]["main"]["version"]
    graph = NodeGraph.from_dict(payload)
    controller = LayerStackController(graph, layer_stack_version=2)
    before = graph.to_dict()

    model = KeyframesModel()
    model.rebuild_layers(controller, selected_layer_id=old_layer_id)
    assert controller.main_stack().version == 1
    transform_group = next(row for row in model.rows if row.row_id.endswith(":group:transform"))
    pairs = [row.component_params for row in model.rows if row.parent_row_id == transform_group.row_id
             and row.component_params]
    assert pairs == [("pivot_x", "pivot_y"), ("translate_x", "translate_y"), ("scale_x", "scale_y")]
    y_row_id = f"param:{old_layer.root_node_id}:translate_y"
    assert model.reveal_row(y_row_id)
    assert model.track_for(y_row_id).keyframes[0].frame == 7
    assert graph.to_dict() == before

    model.rebuild(graph)
    assert not any(row.row_type == "group" for row in model.rows)
    assert model.track_for(y_row_id).keyframes[0].frame == 7


def test_layer_options_apply_an_effect_without_opening_the_node_graph(qapp):
    from PyQt6.QtWidgets import QMenu
    from src.features.composite.ui.keyframes_panel import KeyframesPanel

    controller = _controller_with_layers(1)
    layer_id = controller.main_stack().layer_ids[0]
    panel = KeyframesPanel()
    panel.set_layer_stack_controller(controller)
    panel.set_layer_view_mode("layers")
    panel.set_node_graph(controller.graph, selected_layer_id=layer_id)
    menu = QMenu(panel)
    layer = controller.graph.get_layer(layer_id)
    panel._populate_layer_menu(menu, layer, controller.layers_map(), [layer_id], True)
    effects = [a.menu() for a in menu.actions() if a.menu()][0]
    assert effects.menuAction().isEnabled()
    committed = []
    panel.layer_edit_committed.connect(lambda *args: committed.append(args))
    effects.actions()[0].trigger()

    [(selected_layer, effect_id)] = committed
    assert selected_layer == layer_id
    assert effect_id in controller.node_ids_for_layer(layer_id)
    assert layer.root_node_id == controller.graph.get_layer(layer_id).root_node_id
    panel.close()


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
        # The layer heads a property tree; parameters retain the root node ID.
        layer_rows = [r for r in m.rows if r.row_type == "layer"]
        param_rows = [r for r in m.rows if r.row_type == "param"]
        self.assertTrue(all(r.depth == 0 for r in layer_rows))
        self.assertTrue(all(r.depth >= 2 for r in param_rows))
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
        self.assertEqual(model_row.parent_row_id, f"{layer_row.row_id}:group:content")
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
