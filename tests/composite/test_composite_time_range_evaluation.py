"""P3-1 tests: time_range gates node activation in the evaluator (plan §5).

Validates that when the current frame is outside a node's [comp_start,
comp_end] range, the node is bypassed (effect) / produces nothing (source),
and inside the range it behaves normally.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.blur_node import BlurNode
from src.features.composite.nodes.image_input_node import ImageInputNode
from src.features.composite.nodes.output_node import OutputNode
from src.features.composite.nodes.video_frame_node import VideoFrameNode
from src.features.composite.evaluators.base_evaluator import (
    BaseEvaluatorMixin, _node_time_range_active,
)
from src.shared.keyframe import Keyframe, KeyframeTrack


pytestmark = pytest.mark.composite_core


def _eval_instantiate(snapshot, node_id):
    """Drive _create_ephemeral_node through a minimal evaluator."""
    from src.features.composite.evaluators.base_evaluator import BaseEvaluatorMixin

    class _Ev(BaseEvaluatorMixin):
        def __init__(self, snap):
            self._snapshot = snap
    ev = _Ev(snapshot)
    ns = snapshot.nodes[node_id]
    return ev._create_ephemeral_node(ns)


def test_node_inside_range_is_active():
    g = NodeGraph()
    b = BlurNode()
    b.time_range = {"enabled": True, "comp_start": 0, "comp_end": 50}
    g.add_node(b)
    snap = g.snapshot(frame_number=25)
    node = _eval_instantiate(snap, b.id)
    assert node.disabled is False


def test_effect_node_outside_range_is_bypassed():
    """A frame past comp_end → effect node disabled (bypasses input)."""
    g = NodeGraph()
    b = BlurNode()
    b.time_range = {"enabled": True, "comp_start": 0, "comp_end": 50}
    g.add_node(b)
    snap = g.snapshot(frame_number=60)  # past end
    node = _eval_instantiate(snap, b.id)
    assert node.disabled is True


def test_node_before_range_is_bypassed():
    g = NodeGraph()
    b = BlurNode()
    b.time_range = {"enabled": True, "comp_start": 10, "comp_end": 50}
    g.add_node(b)
    snap = g.snapshot(frame_number=5)  # before start
    node = _eval_instantiate(snap, b.id)
    assert node.disabled is True


def test_node_at_range_boundaries_is_active():
    """Inclusive boundaries: comp_start and comp_end frames are active."""
    g = NodeGraph()
    b = BlurNode()
    b.time_range = {"enabled": True, "comp_start": 10, "comp_end": 50}
    g.add_node(b)
    snap_start = g.snapshot(frame_number=10)
    assert _eval_instantiate(snap_start, b.id).disabled is False
    snap_end = g.snapshot(frame_number=50)
    assert _eval_instantiate(snap_end, b.id).disabled is False


def test_disabled_range_does_not_gate():
    """enabled=False → node always active regardless of frame."""
    g = NodeGraph()
    b = BlurNode()
    b.time_range = {"enabled": False, "comp_start": 0, "comp_end": 10}
    g.add_node(b)
    snap = g.snapshot(frame_number=999)
    node = _eval_instantiate(snap, b.id)
    assert node.disabled is False


def test_output_node_never_gated():
    """The Output node must never be bypassed by a time_range."""
    g = NodeGraph()
    out = OutputNode()
    out.time_range = {"enabled": True, "comp_start": 0, "comp_end": 5}
    g.add_node(out)
    snap = g.snapshot(frame_number=999)
    node = _eval_instantiate(snap, out.id)
    assert node.disabled is False


def test_node_without_range_is_active():
    """No time_range → always active (default behaviour)."""
    g = NodeGraph()
    b = BlurNode()
    g.add_node(b)
    snap = g.snapshot(frame_number=999)
    node = _eval_instantiate(snap, b.id)
    assert node.disabled is False


def test_range_round_trips_with_full_fields():
    """resolve_video_range and the evaluator gate use the same comp_start/end."""
    from src.features.composite.render.composite_render_output import resolve_video_range
    g = NodeGraph()
    b = BlurNode()
    b.time_range = {"enabled": True, "comp_start": 5, "comp_end": 47,
                    "source_in": 0, "source_out": 42, "offset": 0}
    g.add_node(b)
    g.add_node(OutputNode())
    start, end, fps = resolve_video_range(g.to_dict())
    # The export range should at least cover the node's range.
    assert start <= 5
    assert end >= 47


def test_video_source_range_is_inactive_before_trimmed_comp_start():
    g = NodeGraph()
    video = VideoFrameNode(node_id="video")
    video.time_range = {"enabled": True, "comp_start": 16,
                        "comp_end": 120}
    g.add_node(video)

    before = g.snapshot(frame_number=0).nodes[video.id]
    inside = g.snapshot(frame_number=16).nodes[video.id]
    assert _node_time_range_active(before, 0) is False
    assert _node_time_range_active(inside, 16) is True


def test_render_source_instance_receives_trimmed_time_range():
    g = NodeGraph()
    video = VideoFrameNode(node_id="video")
    video.time_range = {"enabled": True, "comp_start": 16,
                        "comp_end": 120}
    g.add_node(video)
    snapshot = g.snapshot(frame_number=16)

    class _Evaluator(BaseEvaluatorMixin):
        def __init__(self):
            self._render_nodes = {}

        def _invalidate_cache_for_node(self, _node_id):
            pass

    evaluator = _Evaluator()
    evaluator._sync_render_nodes(snapshot)
    assert evaluator._render_nodes[video.id].time_range == video.time_range
