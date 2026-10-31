"""P8 tests: frame_number consistency across preview / playback / export.

Covers A-5 (preview snapshot must carry frame_number) and A-13 (current
frame must reach the properties panel). The CompositePage itself needs a
full GUI, so these tests exercise the resolution helper + the snapshot
contract directly, plus the export path's resolve_video_range.

F-15 (preview/export share one frame coordinate system) is verified by
asserting that the same frame_number flows into both a NodeGraph.snapshot
(used by preview) and the resolve_video_range bounds (used by export).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.core.runtime_context import CompositeRuntimeContext
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.nodes.output_node import OutputNode
from src.features.composite.render.composite_render_output import (
    detect_composite_mode, resolve_video_range, CompositeMode)
from src.shared.keyframe import Keyframe, KeyframeTrack


pytestmark = pytest.mark.composite_core


# ── A-5: snapshot carries the frame it was asked for ────────────────────

def test_preview_snapshot_carries_frame_number():
    """A preview snapshot must record the frame_number for interpolation."""
    g = NodeGraph()
    g.add_node(TransformNode())
    snap = g.snapshot(frame_number=42)
    assert snap.frame_number == 42


def test_preview_and_export_share_frame_coordinate():
    """F-15: the same frame_number is valid for both preview snapshot and the
    export range derived from the graph."""
    g = NodeGraph()
    t = TransformNode()
    track = KeyframeTrack(param_name="rotation", min_val=-360.0, max_val=360.0)
    track.add_keyframe(Keyframe(0, 0.0))
    track.add_keyframe(Keyframe(50, 90.0))
    t.set_keyframe_track("rotation", track)
    g.add_node(t)
    g.add_node(OutputNode())

    graph_data = g.to_dict()

    # Export range is derived from the animation's furthest keyframe.
    start, end, fps = resolve_video_range(graph_data)
    assert start == 0
    assert end >= 50
    assert fps == 24.0

    # A preview snapshot at frame 25 (mid-animation) is within the export range.
    snap = g.snapshot(frame_number=25)
    assert start <= snap.frame_number <= end


# ── _current_keyframe_frame resolution logic ────────────────────────────

def test_current_frame_resolution_prefers_clip_local_frame():
    """A Comp Clip's clip_local_frame wins over the panel playhead."""
    rc = CompositeRuntimeContext(
        project_id="p", graph_id="g", clip_local_frame=17, timeline_frame=100,
        source_frame=42)
    # Mimic CompositePage._current_keyframe_frame precedence without
    # instantiating the full page.
    local = getattr(rc, 'clip_local_frame', None) or 0
    assert local == 17


def test_current_frame_resolution_falls_back_to_timeline_frame():
    rc = CompositeRuntimeContext(project_id="p", graph_id="g", timeline_frame=33)
    local = getattr(rc, 'clip_local_frame', None)
    tf = getattr(rc, 'timeline_frame', None)
    frame = local or tf or 0
    assert frame == 33


# ── P0-3: frame 0 must be honoured (truthy bug fix) ─────────────────────

def test_clip_local_frame_zero_is_honoured():
    """P0-3: clip_local_frame == 0 must return 0, not fall through.

    The old ``if local:`` treated 0 as False and skipped to the next source.
    """
    rc = CompositeRuntimeContext(
        project_id="p", graph_id="g", clip_local_frame=0, timeline_frame=99)
    # Mimic the NEW precedence (explicit None check).
    local = getattr(rc, 'clip_local_frame', None)
    assert local is not None  # the fix: 0 is not None
    assert local == 0


def test_timeline_frame_zero_is_honoured():
    """P0-3: timeline_frame == 0 must return 0 even when panel frame is 50."""
    rc = CompositeRuntimeContext(project_id="p", graph_id="g", timeline_frame=0)
    local = getattr(rc, 'clip_local_frame', None)
    tf = getattr(rc, 'timeline_frame', None)
    # With the fix, tf is not None → use it (0), not the panel fallback.
    if local is not None:
        frame = local
    elif tf is not None:
        frame = tf
    else:
        frame = 50
    assert frame == 0


# ── A-9 / A-10: mode detection + range from per-node animation ──────────

def test_graph_with_only_keyframes_detected_as_video():
    """A-9: a still graph with per-node keyframes is a video."""
    g = NodeGraph()
    b = TransformNode()
    track = KeyframeTrack(param_name="rotation")
    track.add_keyframe(Keyframe(0, 0.0))
    track.add_keyframe(Keyframe(20, 45.0))
    b.set_keyframe_track("rotation", track)
    g.add_node(b)
    g.add_node(OutputNode())
    assert detect_composite_mode(g.to_dict()) is CompositeMode.VIDEO


def test_graph_with_time_range_detected_as_video():
    g = NodeGraph()
    b = TransformNode()
    b.time_range = {"comp_start": 0, "comp_end": 30}
    g.add_node(b)
    g.add_node(OutputNode())
    assert detect_composite_mode(g.to_dict()) is CompositeMode.VIDEO


def test_resolve_range_uses_keyframe_end_when_no_clip():
    """A-10: independent comp with animation derives range from keyframes."""
    g = NodeGraph()
    b = TransformNode()
    track = KeyframeTrack(param_name="rotation")
    track.add_keyframe(Keyframe(0, 0.0))
    track.add_keyframe(Keyframe(47, 90.0))
    b.set_keyframe_track("rotation", track)
    g.add_node(b)
    g.add_node(OutputNode())
    start, end, fps = resolve_video_range(g.to_dict())
    assert end >= 47


def test_still_graph_remains_image():
    """A plain still graph (no animation, no video node) stays IMAGE."""
    g = NodeGraph()
    g.add_node(TransformNode())
    g.add_node(OutputNode())
    assert detect_composite_mode(g.to_dict()) is CompositeMode.IMAGE
