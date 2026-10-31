"""Time semantics tests — plan P0-04 (frame remap) + P0-05 (layer gates).

Covers:
  * ``TimeRemap`` closed-form math and chain composition
  * ``compute_frame_overrides`` on real ``NodeGraph.snapshot()`` output
    (offset / hold / retime chains, bypassed nodes, branch conflicts)
  * Layer ``in_frame``/``out_frame`` projected into snapshot frame gates
    (controller round-trip, generation bump, bounds, intersection)
  * ``VideoFrameNode._apply_frame_override`` playhead remapping
"""

import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.core.layer_stack_controller import (
    LayerStackController,
)
from src.features.composite.core.time_remap import (
    TimeRemap, gate_active, compute_frame_overrides,
    compute_layer_frame_gates, remap_for_parameters,
)
from src.features.composite.nodes.output_node import OutputNode
from src.features.composite.nodes.constant_node import ConstantNode
from src.features.composite.nodes.video_frame_node import VideoFrameNode
from src.features.composite.nodes.blur_node import BlurNode
from src.features.composite.nodes.time_offset_node import TimeOffsetNode
from src.features.composite.nodes.frame_hold_node import FrameHoldNode
from src.features.composite.nodes.retime_node import RetimeNode
from src.features.composite.nodes.precomp_node import PrecompNode

pytestmark = pytest.mark.composite_core


# ── TimeRemap math ─────────────────────────────────────────────────────────

class TestTimeRemapMath(unittest.TestCase):

    def test_identity(self):
        r = TimeRemap()
        self.assertTrue(r.is_identity())
        self.assertEqual(r.apply(42), 42)

    def test_offset(self):
        r = TimeRemap(offset=5)
        self.assertEqual(r.apply(10), 15)
        # doc contract: positive offset shows future frames
        r_neg = TimeRemap(offset=-3)
        self.assertEqual(r_neg.apply(10), 7)

    def test_hold(self):
        r = TimeRemap(hold=7)
        self.assertEqual(r.apply(0), 7)
        self.assertEqual(r.apply(999), 7)

    def test_speed(self):
        r = TimeRemap(speed=2.0)
        self.assertEqual(r.apply(6), 12)
        self.assertEqual(r.apply(0), 0)

    def test_signature_distinguishes_remaps(self):
        self.assertNotEqual(TimeRemap(offset=5).signature(),
                            TimeRemap(offset=6).signature())
        self.assertNotEqual(TimeRemap(speed=2.0).signature(),
                            TimeRemap(speed=1.0).signature())
        self.assertNotEqual(TimeRemap(hold=3).signature(),
                            TimeRemap(offset=3).signature())

    def test_compose_offset_then_offset(self):
        # upstream chain: time_offset(+5) below time_offset(+3)
        t = TimeRemap(offset=3)           # applied when reaching 2nd node
        g = TimeRemap(offset=5)           # downstream node's own mapping
        self.assertEqual(t.pushed_upstream(g).apply(10), 18)

    def test_compose_retime_then_offset(self):
        # video → time_offset(+5) → retime(2x): retime processed first in
        # reverse order; its upstream (time_offset) is evaluated at 2F, and
        # the source sees 2F+5.
        t = TimeRemap(speed=2.0)
        g = TimeRemap(offset=5)
        composed = t.pushed_upstream(g)
        self.assertEqual(composed.apply(10), 25)

    def test_compose_hold_freezes_everything_upstream(self):
        t = TimeRemap(speed=2.0, offset=7)
        g = TimeRemap(hold=3)
        self.assertEqual(t.pushed_upstream(g).apply(100), 3)

    def test_compose_hold_downstream_of_hold(self):
        t = TimeRemap(hold=8)
        g = TimeRemap(hold=3)
        self.assertEqual(t.pushed_upstream(g).apply(100), 3)


# ── remap_for_parameters ───────────────────────────────────────────────────

class TestRemapForParameters(unittest.TestCase):

    def test_time_offset(self):
        r = remap_for_parameters("time_offset", {"frame_offset": -4})
        self.assertEqual((r.speed, r.offset, r.hold), (1.0, -4, None))

    def test_frame_hold(self):
        r = remap_for_parameters("frame_hold", {"hold_frame": 12})
        self.assertEqual(r.hold, 12)

    def test_retime(self):
        r = remap_for_parameters("retime", {"speed": 0.5})
        self.assertEqual(r.speed, 0.5)

    def test_precomp_time_controls(self):
        r = remap_for_parameters(
            "precomp", {"speed": 2.0, "frame_offset": 3})
        self.assertEqual(r.apply(10), 23)
        held = remap_for_parameters(
            "precomp", {"speed": 2.0, "hold_frame": 7})
        self.assertEqual(held.apply(100), 7)

    def test_non_time_type_is_none(self):
        self.assertIsNone(remap_for_parameters("blur", {"radius": 2}))

    def test_invalid_params_fall_back(self):
        # missing parameters degrade to the identity remap (not recorded)
        self.assertTrue(remap_for_parameters("time_offset", None).is_identity())
        # garbage speed is unusable → the node becomes a pass-through
        self.assertIsNone(remap_for_parameters("retime", {"speed": "garbage"}))
        # zero/negative speed clamps to identity mapping
        r = remap_for_parameters("retime", {"speed": 0.0})
        self.assertEqual(r.speed, 1.0)


# ── compute_frame_overrides through a real graph snapshot ─────────────────

def _graph_with_chain(node_specs, connections):
    """Build a NodeGraph; node_specs = [(ctor, id)]; connections =
    [(src, dst)] wired on the nodes' real first input/output ports."""
    g = NodeGraph()
    for ctor, nid in node_specs:
        g.add_node(ctor(node_id=nid))
    for src, dst in connections:
        src_node = g.get_node(src)
        dst_node = g.get_node(dst)
        out_port = src_node.outputs[0].name
        in_port = dst_node.inputs[0].name
        assert g.connect(src, out_port, dst, in_port), (src, dst, out_port,
                                                        in_port)
    return g


class TestFrameOverridesSnapshot(unittest.TestCase):

    def test_offset_reaches_source(self):
        g = _graph_with_chain(
            [(VideoFrameNode, "video"), (TimeOffsetNode, "offset1"),
             (OutputNode, "out")],
            [("video", "offset1"), ("offset1", "out")])
        g.get_node("offset1").set_parameter("frame_offset", 5)
        snap = g.snapshot(frame_number=10)
        remap = snap.frame_overrides.get("video")
        self.assertIsNotNone(remap)
        self.assertEqual(remap.apply(10), 15)

    def test_no_time_nodes_no_overrides(self):
        g = _graph_with_chain(
            [(VideoFrameNode, "video"), (BlurNode, "blur"), (OutputNode, "out")],
            [("video", "blur"), ("blur", "out")])
        snap = g.snapshot(frame_number=10)
        self.assertEqual(snap.frame_overrides, {})

    def test_precomp_remap_reaches_child_source(self):
        g = _graph_with_chain(
            [(VideoFrameNode, "video"), (PrecompNode, "precomp"),
             (OutputNode, "out")],
            [("video", "precomp"), ("precomp", "out")])
        g.get_node("precomp").set_parameter("frame_offset", 4)
        g.get_node("precomp").set_parameter("speed", 2.0)
        snap = g.snapshot(frame_number=10)
        self.assertEqual(snap.frame_overrides["video"].apply(10), 24)

    def test_zero_offset_is_identity_not_recorded(self):
        g = _graph_with_chain(
            [(VideoFrameNode, "video"), (TimeOffsetNode, "offset1"),
             (OutputNode, "out")],
            [("video", "offset1"), ("offset1", "out")])
        snap = g.snapshot(frame_number=10)
        self.assertNotIn("video", snap.frame_overrides)

    def test_chain_retime_over_offset(self):
        g = _graph_with_chain(
            [(VideoFrameNode, "video"), (TimeOffsetNode, "offset1"),
             (RetimeNode, "retime1"), (OutputNode, "out")],
            [("video", "offset1"), ("offset1", "retime1"),
             ("retime1", "out")])
        g.get_node("offset1").set_parameter("frame_offset", 5)
        g.get_node("retime1").set_parameter("speed", 2.0)
        snap = g.snapshot(frame_number=10)
        remap = snap.frame_overrides.get("video")
        self.assertIsNotNone(remap)
        self.assertEqual(remap.apply(10), 25)  # 10*2 + 5

    def test_hold_freezes_source(self):
        g = _graph_with_chain(
            [(VideoFrameNode, "video"), (FrameHoldNode, "hold1"),
             (OutputNode, "out")],
            [("video", "hold1"), ("hold1", "out")])
        g.get_node("hold1").set_parameter("hold_frame", 33)
        snap = g.snapshot(frame_number=100)
        self.assertEqual(snap.frame_overrides["video"].apply(100), 33)

    def test_bypassed_time_node_does_not_remap(self):
        g = _graph_with_chain(
            [(VideoFrameNode, "video"), (TimeOffsetNode, "offset1"),
             (OutputNode, "out")],
            [("video", "offset1"), ("offset1", "out")])
        g.get_node("offset1").set_parameter("frame_offset", 5)
        g.get_node("offset1").disabled = True
        snap = g.snapshot(frame_number=10)
        self.assertNotIn("video", snap.frame_overrides)

    def test_time_range_inactive_time_node_does_not_remap(self):
        g = _graph_with_chain(
            [(VideoFrameNode, "video"), (TimeOffsetNode, "offset1"),
             (OutputNode, "out")],
            [("video", "offset1"), ("offset1", "out")])
        g.get_node("offset1").set_parameter("frame_offset", 5)
        g.get_node("offset1").time_range = {
            "enabled": True, "comp_start": 0, "comp_end": 4}
        snap = g.snapshot(frame_number=10)  # outside [0, 4]
        self.assertNotIn("video", snap.frame_overrides)
        snap2 = g.snapshot(frame_number=3)  # inside range
        self.assertEqual(snap2.frame_overrides["video"].apply(3), 8)

    def test_keyframed_offset_uses_interpolated_value(self):
        g = _graph_with_chain(
            [(VideoFrameNode, "video"), (TimeOffsetNode, "offset1"),
             (OutputNode, "out")],
            [("video", "offset1"), ("offset1", "out")])
        node = g.get_node("offset1")
        node.set_parameter("frame_offset", 5)
        # keyframe interpolation is evaluated into snapshot parameters
        # before overrides are computed, so an animated offset rides along.
        snap = g.snapshot(frame_number=10)
        self.assertEqual(snap.frame_overrides["video"].apply(10), 15)

    def test_branch_conflict_first_wins_and_logs(self):
        # video feeds two different time chains; the first assignment in
        # reverse topological order wins deterministically.
        g = _graph_with_chain(
            [(VideoFrameNode, "video"), (TimeOffsetNode, "offsetA"),
             (TimeOffsetNode, "offsetB"), (OutputNode, "out")],
            [("video", "offsetA"), ("video", "offsetB"),
             ("offsetA", "out"), ("offsetB", "out")])
        g.get_node("offsetA").set_parameter("frame_offset", 5)
        g.get_node("offsetB").set_parameter("frame_offset", 9)
        conflicts = []
        snap = g.snapshot(frame_number=10)
        compute_frame_overrides(
            snap.nodes,
            lambda nid: [c for c in snap.connections if c.dst_node_id == nid],
            snap.eval_order, conflict_logger=conflicts.append,
            frame_number=10)
        remap = snap.frame_overrides["video"]
        self.assertIn(remap.apply(10), (15, 19))
        self.assertTrue(conflicts, "branch conflict must be reported")


# ── Layer frame gates (P0-05) ──────────────────────────────────────────────

def _controller_with_media_layer():
    g = NodeGraph()
    g.add_node(OutputNode(node_id="out"))
    ctrl = LayerStackController(g, undo_stack=None)
    g.add_node(ConstantNode(node_id="src0"))
    layer_id = ctrl.add_media_layer("Media", "src0")
    return g, ctrl, layer_id


class TestLayerFrameGates(unittest.TestCase):

    def test_gate_active_bounds_inclusive(self):
        self.assertTrue(gate_active((5, 10), 5))
        self.assertTrue(gate_active((5, 10), 10))
        self.assertFalse(gate_active((5, 10), 4))
        self.assertFalse(gate_active((5, 10), 11))

    def test_gate_unbounded_sides(self):
        self.assertTrue(gate_active((None, 10), 0))
        self.assertFalse(gate_active((None, 10), 11))
        self.assertFalse(gate_active((5, None), 4))
        self.assertTrue(gate_active((5, None), 999))

    def test_snapshot_has_no_gate_without_range(self):
        g, ctrl, layer_id = _controller_with_media_layer()
        snap = g.snapshot(frame_number=0)
        self.assertEqual(snap.layer_frame_gates, {})

    def test_in_out_projected_to_root_and_source(self):
        g, ctrl, layer_id = _controller_with_media_layer()
        layer = g.get_layer(layer_id)
        self.assertTrue(ctrl.set_layer_property(layer_id, "in_frame", 3))
        self.assertTrue(ctrl.set_layer_property(layer_id, "out_frame", 9))
        snap = g.snapshot(frame_number=5)
        self.assertEqual(snap.layer_frame_gates.get(layer.source_node_id),
                         (3, 9))
        self.assertEqual(snap.layer_frame_gates.get(layer.root_node_id),
                         (3, 9))

    def test_in_out_change_bumps_generation(self):
        g, ctrl, layer_id = _controller_with_media_layer()
        gen_before = g.generation
        self.assertTrue(ctrl.set_layer_property(layer_id, "in_frame", 3))
        self.assertGreater(g.generation, gen_before)

    def test_validation_rejects_inverted_range(self):
        g, ctrl, layer_id = _controller_with_media_layer()
        self.assertTrue(ctrl.set_layer_property(layer_id, "out_frame", 5))
        self.assertFalse(ctrl.set_layer_property(layer_id, "in_frame", 9))

    def test_intersecting_gates(self):
        from src.features.composite.core.layer_model import Layer, LayerStack
        stack = LayerStack(stack_id="s1")
        stack.layer_ids = ["l1", "l2"]
        layers = {
            "l1": Layer(layer_id="l1", name="A", root_node_id="rootA",
                        source_node_id="srcA", in_frame=0, out_frame=10),
            "l2": Layer(layer_id="l2", name="B", root_node_id="rootA",
                        source_node_id="srcA", in_frame=5, out_frame=20),
        }
        gates = compute_layer_frame_gates([stack], layers.get)
        self.assertEqual(gates["rootA"], (5, 10))
        self.assertEqual(gates["srcA"], (5, 10))

    def test_layers_without_range_produce_no_gate(self):
        from src.features.composite.core.layer_model import Layer, LayerStack
        stack = LayerStack(stack_id="s1")
        stack.layer_ids = ["l1"]
        layers = {"l1": Layer(layer_id="l1", name="A", root_node_id="rootA")}
        gates = compute_layer_frame_gates([stack], layers.get)
        self.assertEqual(gates, {})


# ── VideoFrameNode override application ────────────────────────────────────

class _StubEvalCtx:
    def __init__(self, frame, overrides):
        self.frame_number = frame
        self.node_frame_overrides = overrides


class TestVideoFrameOverride(unittest.TestCase):

    def _node(self):
        node = VideoFrameNode(node_id="video1")
        return node

    def test_no_override_passthrough(self):
        node = self._node()
        ctx = _StubEvalCtx(24, {})
        self.assertEqual(node._apply_frame_override(ctx, 24), 24)

    def test_none_overrides_passthrough(self):
        node = self._node()
        ctx = _StubEvalCtx(24, None)
        self.assertEqual(node._apply_frame_override(ctx, 24), 24)

    def test_override_applied(self):
        node = self._node()
        ctx = _StubEvalCtx(
            10, {"video1": TimeRemap(speed=2.0, offset=5)})
        self.assertEqual(node._apply_frame_override(ctx, 10), 25)

    def test_other_node_override_ignored(self):
        node = self._node()
        ctx = _StubEvalCtx(10, {"other": TimeRemap(hold=1)})
        self.assertEqual(node._apply_frame_override(ctx, 10), 10)

    def test_none_frame_stays_none(self):
        node = self._node()
        ctx = _StubEvalCtx(0, {"video1": TimeRemap(hold=3)})
        self.assertIsNone(node._apply_frame_override(ctx, None))


if __name__ == "__main__":
    unittest.main()


# ── B5: reverse / loop modes ───────────────────────────────────────────────

class TestReverseLoopMath(unittest.TestCase):
    """TimeRemap reverse/loop closed-form math (B5)."""

    def test_default_fields_stay_identity(self):
        r = TimeRemap()
        self.assertTrue(r.is_identity())

    def test_reverse_mirrors_within_range(self):
        # range (0, 99): F=10 → 89
        r = TimeRemap(reverse=True, loop=(0, 99))
        self.assertEqual(r.apply(10), 89)

    def test_reverse_without_range_anchors_at_zero(self):
        r = TimeRemap(reverse=True)
        self.assertEqual(r.apply(10), -10)

    def test_retime_reverse_off_anchors_at_user_range(self):
        # B5 regression: reverse with loop_mode "off" used to mirror at 0
        # — every positive frame mapped negative, the source clamped, and
        # playback froze on frame 0. The mirror must anchor on the user's
        # loop range even when the wrap itself is disabled.
        from src.features.composite.core.time_remap import remap_for_parameters
        r = remap_for_parameters("retime", {
            "reverse": True, "loop_mode": "off",
            "loop_in": 0, "loop_out": 99, "speed": 1.0})
        self.assertIsNotNone(r)
        self.assertEqual(r.apply(10), 89)
        self.assertEqual(r.apply(0), 99)
        # no wrap: frames past the mirrored range stay outside it
        self.assertEqual(r.apply(100), -1)

    def test_retime_reverse_loop_combined_unchanged(self):
        from src.features.composite.core.time_remap import remap_for_parameters
        r = remap_for_parameters("retime", {
            "reverse": True, "loop_mode": "loop",
            "loop_in": 0, "loop_out": 9, "speed": 1.0})
        # mirror (0+9)-F then wrap into [0, 9]
        self.assertEqual(r.apply(0), 9)
        self.assertEqual(r.apply(9), 0)

    def test_reverse_is_involutive_within_range(self):
        # Mirror is an involution for frames inside [in, out] (the wrap is
        # identity there); outside frames wrap into the range first, so the
        # double application no longer returns the original frame.
        r = TimeRemap(reverse=True, loop=(10, 50))
        for f in (10, 20, 30, 50):
            self.assertEqual(r.apply(r.apply(f)), f)
        outside = r.apply(60)
        self.assertTrue(10 <= outside <= 50)

    def test_loop_wraps_periodically(self):
        r = TimeRemap(loop=(0, 9))
        self.assertEqual(r.apply(0), 0)
        self.assertEqual(r.apply(9), 9)
        self.assertEqual(r.apply(10), 0)
        self.assertEqual(r.apply(25), 5)
        # periodicity: apply(F) == apply(F + span)
        for f in (0, 3, 7, 11):
            self.assertEqual(r.apply(f), r.apply(f + 10))

    def test_loop_offset_range(self):
        r = TimeRemap(loop=(100, 109))
        self.assertEqual(r.apply(105), 105)
        self.assertEqual(r.apply(110), 100)
        self.assertEqual(r.apply(95), 105)  # negative modulo wraps forward

    def test_loop_degenerate_single_frame_range(self):
        r = TimeRemap(loop=(7, 7))
        self.assertEqual(r.apply(0), 7)
        self.assertEqual(r.apply(50), 7)

    def test_reverse_then_loop_order(self):
        # per-node order: affine → reverse → loop
        r = TimeRemap(reverse=True, loop=(0, 99))
        # F=110 → affine 110 → reverse 99-110 = -11 → wrap 0 + (-11 % 100) = 89
        self.assertEqual(r.apply(110), 89)

    def test_hold_wins_over_reverse_loop(self):
        r = TimeRemap(hold=5, reverse=True, loop=(0, 9))
        self.assertEqual(r.apply(1000), 5)

    def test_signature_distinguishes_modes(self):
        base = TimeRemap()
        rev = TimeRemap(reverse=True)
        lp = TimeRemap(loop=(0, 9))
        rev_lp = TimeRemap(reverse=True, loop=(0, 9))
        sigs = {base.signature(), rev.signature(), lp.signature(),
                rev_lp.signature()}
        self.assertEqual(len(sigs), 4)
        # chained remap must not share a flat signature
        chained = TimeRemap(reverse=True, loop=(0, 9),
                            inner=TimeRemap(offset=3))
        self.assertNotIn(chained.signature(), sigs)

    def test_composition_exactness_with_non_affine(self):
        # pushed_upstream must equal g ∘ self exactly when non-affine
        # modes are present, for a spread of frames and combos.
        ups = [
            TimeRemap(speed=2.0),
            TimeRemap(offset=7),
            TimeRemap(reverse=True, loop=(0, 99)),
            TimeRemap(loop=(10, 29)),
            TimeRemap(speed=0.5, offset=3, reverse=True, loop=(0, 49)),
        ]
        downs = [
            TimeRemap(speed=1.0),
            TimeRemap(offset=-4),
            TimeRemap(reverse=True, loop=(0, 99)),
            TimeRemap(speed=1.5, loop=(5, 44)),
        ]
        for t in ups:
            for g in downs:
                comp = t.pushed_upstream(g)
                for f in (0, 1, 5, 13, 37, 64, 120):
                    self.assertEqual(
                        comp.apply(f), g.apply(t.apply(f)),
                        f"t={t.signature()} g={g.signature()} F={f}")

    def test_composition_pure_affine_still_folds(self):
        t = TimeRemap(speed=2.0)
        g = TimeRemap(offset=5)
        comp = t.pushed_upstream(g)
        self.assertIsNone(comp.inner)
        self.assertEqual(comp.speed, 2.0)
        self.assertEqual(comp.apply(10), 25)

    def test_composition_chain_three_nodes(self):
        t = TimeRemap(offset=3)
        g1 = TimeRemap(reverse=True, loop=(0, 49))
        g2 = TimeRemap(speed=2.0)
        step1 = t.pushed_upstream(g1)          # g1 ∘ t  (chain: inner=t)
        step2 = step1.pushed_upstream(g2)      # g2 ∘ g1 ∘ t
        for f in (0, 4, 9, 21, 40):
            self.assertEqual(step2.apply(f), g2.apply(g1.apply(t.apply(f))))


class TestRetimeReverseLoopParams(unittest.TestCase):
    """retime node params → TimeRemap (B5)."""

    def test_retime_forward_unchanged(self):
        r = remap_for_parameters("retime", {"speed": 2.0})
        self.assertEqual(r.speed, 2.0)
        self.assertFalse(r.reverse)
        self.assertIsNone(r.loop)

    def test_retime_reverse(self):
        r = remap_for_parameters(
            "retime", {"speed": 1.0, "reverse": True})
        self.assertTrue(r.reverse)

    def test_retime_loop_range(self):
        r = remap_for_parameters(
            "retime", {"loop_mode": "loop", "loop_in": 10, "loop_out": 19})
        self.assertEqual(r.loop, (10, 19))
        self.assertEqual(r.apply(20), 10)

    def test_retime_loop_mode_off_ignored(self):
        r = remap_for_parameters(
            "retime", {"loop_mode": "off", "loop_in": 0, "loop_out": 9})
        self.assertIsNone(r.loop)

    def test_retime_invalid_range_degrades_to_off(self):
        r = remap_for_parameters(
            "retime", {"loop_mode": "loop", "loop_in": 50, "loop_out": 10})
        self.assertIsNone(r.loop)

    def test_retime_params_serialize_round_trip(self):
        node = RetimeNode(node_id="rt")
        node.set_parameter("reverse", True)
        node.set_parameter("loop_mode", "loop")
        node.set_parameter("loop_in", 5)
        node.set_parameter("loop_out", 24)
        data = node.to_dict()
        node2 = RetimeNode.from_dict(data)
        r = remap_for_parameters("retime", node2.parameters)
        self.assertTrue(r.reverse)
        self.assertEqual(r.loop, (5, 24))
        # old projects without the new keys still load and stay forward
        old = RetimeNode.from_dict({"id": "rt", "type": "retime",
                                    "parameters": {"speed": 1.5}})
        r_old = remap_for_parameters("retime", old.parameters)
        self.assertFalse(r_old.reverse)
        self.assertIsNone(r_old.loop)


class TestReverseLoopSnapshotPropagation(unittest.TestCase):
    """reverse/loop remaps reach the source through frame_overrides (B5)."""

    def test_reverse_reaches_source(self):
        g = _graph_with_chain(
            [(VideoFrameNode, "video"), (RetimeNode, "rt"),
             (OutputNode, "out")],
            [("video", "rt"), ("rt", "out")])
        g.get_node("rt").set_parameter("reverse", True)
        g.get_node("rt").set_parameter("loop_mode", "loop")
        g.get_node("rt").set_parameter("loop_in", 0)
        g.get_node("rt").set_parameter("loop_out", 99)
        snap = g.snapshot(frame_number=10)
        remap = snap.frame_overrides.get("video")
        self.assertIsNotNone(remap)
        self.assertEqual(remap.apply(10), 89)

    def test_loop_reaches_source(self):
        g = _graph_with_chain(
            [(VideoFrameNode, "video"), (RetimeNode, "rt"),
             (OutputNode, "out")],
            [("video", "rt"), ("rt", "out")])
        g.get_node("rt").set_parameter("loop_mode", "loop")
        g.get_node("rt").set_parameter("loop_in", 0)
        g.get_node("rt").set_parameter("loop_out", 9)
        snap = g.snapshot(frame_number=25)
        remap = snap.frame_overrides.get("video")
        self.assertIsNotNone(remap)
        self.assertEqual(remap.apply(25), 5)

    def test_reversed_retime_before_offset_chain(self):
        # video → offset(+5) → retime(reverse): comp F=10 → offset sees
        # reverse(10) first in reverse order … verify end-to-end exactness.
        g = _graph_with_chain(
            [(VideoFrameNode, "video"), (TimeOffsetNode, "off"),
             (RetimeNode, "rt"), (OutputNode, "out")],
            [("video", "off"), ("off", "rt"), ("rt", "out")])
        g.get_node("off").set_parameter("frame_offset", 5)
        g.get_node("rt").set_parameter("reverse", True)
        g.get_node("rt").set_parameter("loop_mode", "loop")
        g.get_node("rt").set_parameter("loop_in", 0)
        g.get_node("rt").set_parameter("loop_out", 99)
        snap = g.snapshot(frame_number=10)
        remap = snap.frame_overrides.get("video")
        self.assertIsNotNone(remap)
        # video remap = offset(+5) ∘ reverse: reverse first, then +5
        self.assertEqual(remap.apply(10), (99 - 10) + 5)

    def test_layer_gate_orthogonal_to_reverse(self):
        # Layer in/out gates act on the composition (timeline) frame domain —
        # a reversed remap must not move the gate window.
        g = _graph_with_chain(
            [(VideoFrameNode, "video"), (RetimeNode, "rt"),
             (OutputNode, "out")],
            [("video", "rt"), ("rt", "out")])
        g.get_node("rt").set_parameter("reverse", True)
        gates = {(10, 20)}
        gate = (10, 20)
        # gate checks the composition frame, unaffected by the remap itself
        self.assertTrue(gate_active(gate, 15))
        self.assertFalse(gate_active(gate, 25))
        snap = g.snapshot(frame_number=15)
        self.assertIsNotNone(snap.frame_overrides.get("video"))
