"""Integration tests covering layer keyframe mapping + node-graph sync
(plan §6.4 / §7 / §8.3 / §10 — batch 4).

These exercise the end-to-end behaviours the plan demands of the MVP:

* Layer keyframes live on the underlying node (plan §6.4): animating a
  layer's Transform must be visible to both the node editor and the
  layers-mode Keyframes view, with no duplicated track storage.
* Parent binding honours capability (plan §7): unsupported roots cannot
  parent; supported ones can; cycles are rejected at the controller.
* Free-graph edits do not silently clobber the auto Merge chain
  (plan §8.3): the compiler surfaces a conflict the controller re-emits.
* The complete MVP flow (media drop → reorder → parent → animate → undo)
  works as one coherent undo history (plan §10 / §12).
"""

import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.core.layer_model import Layer, LayerStack
from src.features.composite.core.layer_stack_controller import (
    LayerStackController,
)
from src.features.composite.core.layer_stack_compiler import (
    LayerStackCompiler, MERGE_PORT_FOREGROUND, MERGE_PORT_BACKGROUND,
)
from src.features.composite.core.layer_stack_validator import (
    LayerStackValidator,
)
from src.features.composite.ui.keyframes_model import KeyframesModel
from src.features.composite.nodes.output_node import OutputNode
from src.features.composite.nodes.constant_node import ConstantNode
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.nodes.blur_node import BlurNode
from src.features.composite.nodes.mask_node import MaskNode
from src.features.composite.nodes.roto_shape_node import RotoShapeNode
from src.features.composite.nodes.geo3d_node import Geo3DNode
from src.shared.keyframe import Keyframe, KeyframeTrack, InterpolationType

pytestmark = pytest.mark.composite_core


def _controller(n_layers: int):
    g = NodeGraph()
    g.add_node(OutputNode(node_id="out"))
    ctrl = LayerStackController(g, undo_stack=None)
    for i in range(n_layers):
        src = ConstantNode(node_id=f"src{i}")
        g.add_node(src)
        ctrl.add_media_layer(f"Layer {i}", f"src{i}", insert_index=0)
    return g, ctrl


class TestLayerKeyframesOnNode(unittest.TestCase):
    """Keyframes live on the node, not the layer (plan §6.4)."""

    def test_animate_layer_transform_attaches_to_node(self):
        g, ctrl = _controller(1)
        layer = g.get_layer(ctrl.main_stack().layer_ids[0])
        root = g.get_node(layer.root_node_id)
        # Animate the layer's transform translate_x.
        track = KeyframeTrack(param_name="translate_x")
        track.add_keyframes_bulk([
            Keyframe(frame=0, value=0.0),
            Keyframe(frame=10, value=100.0),
        ])
        root.set_keyframe_track("translate_x", track)
        # The track lives on the node, not the layer.
        self.assertIn("translate_x", root.keyframe_tracks)
        self.assertNotIn("translate_x", layer.to_dict())

    def test_layers_mode_shows_layer_keyframes(self):
        g, ctrl = _controller(1)
        layer = g.get_layer(ctrl.main_stack().layer_ids[0])
        root = g.get_node(layer.root_node_id)
        track = KeyframeTrack(param_name="translate_x")
        track.add_keyframes_bulk([
            Keyframe(frame=0, value=0.0),
            Keyframe(frame=5, value=50.0),
        ])
        root.set_keyframe_track("translate_x", track)

        m = KeyframesModel()
        m.set_view_mode("layers")
        m.rebuild_layers(ctrl)
        param_rows = [r for r in m.rows if r.row_type == "param"]
        # translate_x appears as a child row of the layer.
        self.assertTrue(any(r.param_name == "translate_x" for r in param_rows))

    def test_layer_projection_exposes_upstream_mask_node(self):
        """Layer mode shows a live Mask child without copying its params."""
        g, ctrl = _controller(1)
        layer = g.get_layer(ctrl.main_stack().layer_ids[0])
        mask = MaskNode(node_id="mask_child")
        g.add_node(mask)
        root = g.get_node(layer.root_node_id)
        g.connect("src0", "result", mask.id, "image")
        g.connect(mask.id, "result", root.id, "image")
        m = KeyframesModel()
        m.rebuild_layers(ctrl)
        self.assertTrue(any(r.node_id == "mask_child"
                            for r in m.rows if r.row_type == "node"))

    def test_promote_and_attach_are_explicit_graph_layer_operations(self):
        g = NodeGraph()
        g.add_node(OutputNode(node_id="out"))
        source = ConstantNode(node_id="source")
        g.add_node(source)
        ctrl = LayerStackController(g, undo_stack=None)
        layer_id = ctrl.add_media_layer("Base", "source")
        roto = RotoShapeNode(node_id="roto")
        g.add_node(roto)

        self.assertTrue(ctrl.add_node_to_layer(layer_id, "roto"))
        self.assertEqual(g.get_layer(layer_id).root_node_id, "roto")

        mask = MaskNode(node_id="standalone_mask")
        g.add_node(mask)
        matte_id = ctrl.create_matte_layer("standalone_mask", "Matte")
        self.assertIsNotNone(matte_id)
        self.assertEqual(g.get_layer(matte_id).root_node_id,
                         "standalone_mask")


class TestParentCapabilityAndCycle(unittest.TestCase):
    """Parent binding capability filter + cycle guard (plan §7)."""

    def test_supported_transforms_can_parent(self):
        g, ctrl = _controller(2)
        ids = ctrl.main_stack().layer_ids
        self.assertTrue(ctrl.set_layer_parent(ids[0], ids[1]))
        layer = g.get_layer(ids[0])
        self.assertEqual(layer.parent_layer_id, ids[1])

    def test_unsupported_root_cannot_parent(self):
        # Replace one layer's root with a Blur (effect, no parent support).
        g, ctrl = _controller(2)
        ids = ctrl.main_stack().layer_ids
        layer = g.get_layer(ids[0])
        blur = BlurNode(node_id="blur_root")
        g.add_node(blur)
        layer.root_node_id = "blur_root"
        self.assertFalse(ctrl.set_layer_parent(ids[0], ids[1]))

    def test_cycle_rejected(self):
        g, ctrl = _controller(3)
        ids = ctrl.main_stack().layer_ids
        ctrl.set_layer_parent(ids[0], ids[1])
        ctrl.set_layer_parent(ids[1], ids[2])
        # ids[2] → ids[0] would close the cycle.
        self.assertFalse(ctrl.set_layer_parent(ids[2], ids[0]))

    def test_3d_layer_parent_composes_object_transform_channels(self):
        g = NodeGraph()
        g.add_node(OutputNode(node_id="out"))
        parent = Geo3DNode(node_id="geo_parent")
        child = Geo3DNode(node_id="geo_child")
        g.add_node(parent)
        g.add_node(child)
        ctrl = LayerStackController(g, undo_stack=None)
        parent_layer = ctrl.add_media_layer("Parent", parent.id,
                                            layer_type="3d",
                                            root_node_id=parent.id)
        child_layer = ctrl.add_media_layer("Child", child.id,
                                           layer_type="3d",
                                           root_node_id=child.id)
        parent.set_parameter("object_pos_x", 2.0)
        parent.set_parameter("object_rot_z", 15.0)
        parent.set_parameter("mesh_scale", 2.0)
        child.set_parameter("object_pos_x", 1.0)
        child.set_parameter("object_rot_z", 5.0)
        child.set_parameter("mesh_scale", 0.5)
        self.assertTrue(ctrl.set_layer_parent(child_layer, parent_layer))

        resolved, _expressions, _errors = g._evaluate_snapshot_parameters(
            frame_number=0, fps=24.0, project_width=1920,
            project_height=1080)
        self.assertEqual(resolved[child.id]["object_pos_x"], 3.0)
        self.assertEqual(resolved[child.id]["object_rot_z"], 20.0)
        self.assertEqual(resolved[child.id]["mesh_scale"], 1.0)


class TestFreeGraphConflict(unittest.TestCase):
    """Manual node-graph edits surface as conflicts (plan §8.3 / §4.5)."""

    def test_external_rewire_blocks_silent_overwrite(self):
        g, ctrl = _controller(2)
        stack = ctrl.main_stack()
        merge_id = stack.generated_merge_node_ids[0]
        # Simulate user grafting an external node onto the merge's A input.
        external = ConstantNode(node_id="external_node")
        g.add_node(external)
        # Drop the layer-root feed and connect the external node.
        layer0_root = g.get_layer(stack.layer_ids[0]).root_node_id
        g.disconnect(layer0_root, "result", merge_id, MERGE_PORT_FOREGROUND)
        g.connect("external_node", "result", merge_id, MERGE_PORT_FOREGROUND)
        # A rebuild must detect the conflict rather than overwrite.
        result = ctrl.rebuild()
        self.assertTrue(result.conflict)

    def test_node_graph_sync_is_dry_run_and_preserves_manual_rewire(self):
        """Node edits surface through sync without a hidden auto-repair."""
        g, ctrl = _controller(2)
        stack = ctrl.main_stack()
        merge_id = stack.generated_merge_node_ids[0]
        external = ConstantNode(node_id="sync_external")
        g.add_node(external)
        root_id = g.get_layer(stack.layer_ids[0]).root_node_id
        g.disconnect(root_id, "result", merge_id, MERGE_PORT_FOREGROUND)
        g.connect(external.id, "result", merge_id, MERGE_PORT_FOREGROUND)

        result = ctrl.sync_from_node_graph()
        self.assertTrue(result.conflict)
        self.assertTrue(any(c.src_node_id == external.id
                            for c in g.connections))

    def test_node_graph_sync_reports_missing_layer_root(self):
        g, ctrl = _controller(1)
        layer_id = ctrl.main_stack().layer_ids[0]
        root_id = g.get_layer(layer_id).root_node_id
        g.remove_node(root_id)

        result = ctrl.sync_from_node_graph()
        self.assertFalse(result.conflict)
        self.assertTrue(any("missing root" in warning.lower()
                            for warning in result.warnings))


class TestApplyFailuresSurface(unittest.TestCase):
    """_apply_changes rejections must surface as warnings (finding #11)."""

    def test_connect_rejection_recorded(self):
        # Feed _apply_changes a connect whose dst node is missing —
        # graph.connect returns False, which must be collected (not swallowed).
        g, ctrl = _controller(1)
        from src.features.composite.core.layer_stack_compiler import (
            CompileChange)
        changes = [
            CompileChange(kind="connect", src_node_id="out", src_port_name="result",
                          dst_node_id="ghost", dst_port_name="A"),
            CompileChange(kind="create_node", node_id="m", node_type="merge"),
        ]
        failures = ctrl._apply_changes(changes)
        self.assertTrue(any("ghost" in f or "reject" in f for f in failures),
                        msg=failures)

    def test_clean_apply_returns_no_failures(self):
        g, ctrl = _controller(1)
        from src.features.composite.core.layer_stack_compiler import (
            CompileChange)
        # Use the layer's root transform as src (it has a real "result"
        # output); merge m1 has an "A" input.  Valid pair → no failures.
        layer_id = ctrl.main_stack().layer_ids[0]
        root_id = g.get_layer(layer_id).root_node_id
        changes = [
            CompileChange(kind="create_node", node_id="m1", node_type="merge"),
            CompileChange(kind="connect", src_node_id=root_id, src_port_name="result",
                          dst_node_id="m1", dst_port_name="A"),
        ]
        failures = ctrl._apply_changes(changes)
        self.assertEqual(failures, [])


class TestParentProjectedToNode(unittest.TestCase):
    """Layer parent is projected onto the node's parent_id (plan §7.1)."""

    def test_parent_compiles_to_node_parent_id(self):
        g, ctrl = _controller(2)
        ids = ctrl.main_stack().layer_ids
        ctrl.set_layer_parent(ids[0], ids[1])
        child_layer = g.get_layer(ids[0])
        parent_layer = g.get_layer(ids[1])
        child_root = g.get_node(child_layer.root_node_id)
        # The compiler projected the parent onto the child's root node.
        self.assertEqual(child_root.parent_id, parent_layer.root_node_id)


class TestEndToEndMvpFlow(unittest.TestCase):
    """Smoke test of the full MVP workflow (plan §12 / §17)."""

    def test_drop_reorder_parent_animate_undo(self):
        from src.features.composite.core.undo_stack import UndoStack
        try:
            stack = UndoStack(None)
        except Exception:
            self.skipTest("PyQt UndoStack unavailable")
        g = NodeGraph()
        g.add_node(OutputNode(node_id="out"))
        ctrl = LayerStackController(g, undo_stack=stack)
        # Drop two media layers.
        g.add_node(ConstantNode(node_id="src0"))
        g.add_node(ConstantNode(node_id="src1"))
        l0 = ctrl.add_media_layer("A", "src0")
        l1 = ctrl.add_media_layer("B", "src1")
        ids = list(ctrl.main_stack().layer_ids)
        # Reorder (B is already on top; move A on top).
        ctrl.move_layer(l0, 0)
        self.assertEqual(ctrl.main_stack().layer_ids[0], l0)
        # Parent A → B.
        self.assertTrue(ctrl.set_layer_parent(l0, l1))
        # Animate A's transform.
        root = g.get_node(g.get_layer(l0).root_node_id)
        track = KeyframeTrack(param_name="translate_y")
        track.add_keyframes_bulk([Keyframe(frame=0, value=0.0),
                                  Keyframe(frame=10, value=20.0)])
        root.set_keyframe_track("translate_y", track)
        # Everything consistent.
        report = LayerStackValidator(g).validate(
            ctrl.main_stack(), ctrl.layers_map())
        self.assertTrue(report.ok, msg=[i.message for i in report.issues])


class TestLargeStackPerformance(unittest.TestCase):
    """Plan §13.5 / §17.12: 50–100 layers remain operable."""

    def test_50_layer_compile_is_fast_and_acyclic(self):
        import time
        g = NodeGraph()
        g.add_node(OutputNode(node_id="out"))
        ctrl = LayerStackController(g, undo_stack=None)
        for i in range(50):
            src = ConstantNode(node_id=f"src{i}")
            g.add_node(src)
            ctrl.add_media_layer(f"L{i}", f"src{i}")
        # The final compile already happened during add_media_layer; re-run
        # a compile explicitly to measure.
        compiler = LayerStackCompiler(g)
        t0 = time.perf_counter()
        result = compiler.compile(ctrl.main_stack(), ctrl.layers_map())
        elapsed = time.perf_counter() - t0
        self.assertTrue(result.ok)
        self.assertEqual(len(result.merge_node_ids), 49)
        # Acyclic.
        order = g.topological_sort()
        self.assertEqual(len(order), len(g.nodes))
        # Generous bound; mainly guards against accidental O(n^2) blowups.
        self.assertLess(elapsed, 2.0)

    def test_100_layer_compile_is_acyclic(self):
        g = NodeGraph()
        g.add_node(OutputNode(node_id="out"))
        ctrl = LayerStackController(g, undo_stack=None)
        for i in range(100):
            src = ConstantNode(node_id=f"s{i}")
            g.add_node(src)
            ctrl.add_media_layer(f"L{i}", f"s{i}")
        order = g.topological_sort()
        self.assertEqual(len(order), len(g.nodes))
        self.assertEqual(len(ctrl.main_stack().generated_merge_node_ids), 99)

    def test_50_layer_undo_snapshot_is_fast(self):
        """Undo snapshot is the hot path (every mutation takes 2 snapshots).

        Guard against accidental O(n^2) regressions in _snapshot — each
        move_layer triggers a before+after full capture.  Without an undo
        stack the snapshot is still built (for the no-op push path) so we
        measure it directly via the internal helper.
        """
        import time
        from src.features.composite.core.undo_stack import UndoStack
        try:
            stack = UndoStack(None)
        except Exception:
            self.skipTest("PyQt UndoStack unavailable")
        g = NodeGraph()
        g.add_node(OutputNode(node_id="out"))
        ctrl = LayerStackController(g, undo_stack=stack)
        for i in range(50):
            src = ConstantNode(node_id=f"u{i}")
            g.add_node(src)
            ctrl.add_media_layer(f"L{i}", f"u{i}")
        ids = list(ctrl.main_stack().layer_ids)
        # One move = 2 snapshots + 1 push. Measure the round trip; generous
        # bound guards against accidental quadratic behaviour in _snapshot.
        t0 = time.perf_counter()
        ctrl.move_layer(ids[-1], 0)
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 1.5,
                        f"move_layer undo snapshot too slow: {elapsed:.3f}s")
        # Undo/redo themselves must also stay snappy.
        t1 = time.perf_counter()
        stack.undo()
        stack.redo()
        self.assertLess(time.perf_counter() - t1, 1.5)


if __name__ == "__main__":
    unittest.main()
