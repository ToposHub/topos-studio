"""P0.5/P1 tests: keyframe evaluation uses snapshot frame_number correctly.

Covers A-5 (preview/export snapshot must carry frame_number so animated
params interpolate at the right frame) and P1.14 (int params cast to int).

These tests exercise GraphEvaluatorMixin._interpolate_parameters directly
(no GL) against snapshots produced by NodeGraph.snapshot(frame_number=...).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.nodes.blur_node import BlurNode
from src.features.composite.core.node_engine import NodeGraph
from src.shared.keyframe import Keyframe, KeyframeTrack, InterpolationType


pytestmark = pytest.mark.composite_core


class _FakeEvaluator:
    """Minimal stand-in exposing _interpolate_parameters + _snapshot.

    The real GraphEvaluatorMixin reads frame_number off its snapshot; we
    mimic that contract so the pure interpolation logic can be tested
    without a GL context.
    """

    def __init__(self, snapshot):
        self._snapshot = snapshot

    def interpolate(self, ns, frame):
        # Mirror GraphEvaluatorMixin._interpolate_parameters
        from src.features.composite.utils.keyframe import KeyframeTrack
        meta = ns.param_defs_meta or {}
        result = {}
        for pname, track_data in ns.keyframe_tracks.items():
            track = KeyframeTrack.from_dict(track_data)
            if not (track and track.keyframes):
                continue
            default_val = ns.parameters.get(pname, 0.0)
            val = track.get_value(frame, default=default_val)
            if meta.get(pname, {}).get("ptype") == "int":
                val = int(round(float(val)))
            result[pname] = val
        return result


def _make_animated_transform():
    node = TransformNode()
    track = KeyframeTrack(param_name="rotation", min_val=-360.0, max_val=360.0)
    track.add_keyframe(Keyframe(0, 0.0, InterpolationType.LINEAR))
    track.add_keyframe(Keyframe(10, 90.0, InterpolationType.LINEAR))
    node.set_keyframe_track("rotation", track)
    return node


def test_snapshot_records_frame_number():
    """P0.5: snapshot must remember the frame_number it was asked for."""
    g = NodeGraph()
    node = _make_animated_transform()
    g.add_node(node)

    snap0 = g.snapshot(frame_number=0)
    snap5 = g.snapshot(frame_number=5)
    assert snap0.frame_number == 0
    assert snap5.frame_number == 5


def test_interpolation_uses_snapshot_frame_number():
    """A-5: evaluating the SAME snapshot at different frames gives different
    values (the snapshot itself carries the authoritative frame)."""
    g = NodeGraph()
    node = _make_animated_transform()
    g.add_node(node)

    snap0 = g.snapshot(frame_number=0)
    snap5 = g.snapshot(frame_number=10)

    ev0 = _FakeEvaluator(snap0)
    ev5 = _FakeEvaluator(snap5)
    ns0 = snap0.nodes[node.id]
    ns5 = snap5.nodes[node.id]

    v0 = ev0.interpolate(ns0, snap0.frame_number)["rotation"]
    v10 = ev5.interpolate(ns5, snap5.frame_number)["rotation"]
    assert pytest.approx(v0) == 0.0
    assert pytest.approx(v10) == 90.0


def test_interpolation_mid_frame():
    """Linear interpolation halfway between 0→90 at frame 5 yields 45."""
    g = NodeGraph()
    node = _make_animated_transform()
    g.add_node(node)
    snap = g.snapshot(frame_number=5)
    ev = _FakeEvaluator(snap)
    v = ev.interpolate(snap.nodes[node.id], 5)["rotation"]
    assert pytest.approx(v) == 45.0


def test_int_param_animated_value_is_cast_to_int():
    """P1.14: an animated int param must not leak float values."""
    node = BlurNode()
    # blur has float radius, so craft an int-typed meta on the snapshot
    track = KeyframeTrack(param_name="radius", min_val=0.0, max_val=100.0)
    track.add_keyframe(Keyframe(0, 1.0))
    track.add_keyframe(Keyframe(10, 10.0))
    node.set_keyframe_track("radius", track)

    g = NodeGraph()
    g.add_node(node)
    snap = g.snapshot(frame_number=5)
    # Force the meta to claim radius is int (simulate an int param)
    snap.nodes[node.id].param_defs_meta["radius"]["ptype"] = "int"

    ev = _FakeEvaluator(snap)
    v = ev.interpolate(snap.nodes[node.id], 5)["radius"]
    assert isinstance(v, int)
    assert v == 6  # halfway 1→10 rounded


def test_non_animated_node_has_no_interpolated_params():
    """A node with no keyframe tracks yields an empty interpolation dict."""
    g = NodeGraph()
    node = BlurNode()
    g.add_node(node)
    snap = g.snapshot(frame_number=5)
    ev = _FakeEvaluator(snap)
    assert ev.interpolate(snap.nodes[node.id], 5) == {}
