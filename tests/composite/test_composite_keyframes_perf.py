"""P10B performance tests: Keyframes panel scales to many nodes/keyframes.

Validates F-12 (1000 keyframes, playhead drag not laggy) at the model +
mapping layer. Full GPU paint benchmarking needs a live GL context and is
out of scope for headless CI; these tests bound the pure-data cost.
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.ui.keyframes_model import (
    KeyframesModel, FrameMapper)
from src.shared.keyframe import Keyframe, KeyframeTrack


pytestmark = pytest.mark.composite_core


def _graph_with_many_keyframes(node_count=20, keyframes_per_node=50):
    """Build a graph with node_count * keyframes_per_node keyframes."""
    g = NodeGraph()
    for _ in range(node_count):
        t = TransformNode()
        track = KeyframeTrack(param_name="rotation")
        for i in range(keyframes_per_node):
            track.add_keyframe(Keyframe(i * 2, float(i)))
        t.set_keyframe_track("rotation", track)
        g.add_node(t)
    return g


def test_model_rebuild_under_1000_keyframes():
    """F-12: rebuilding the model for ~1000 keyframes is fast (< 200ms)."""
    g = _graph_with_many_keyframes(node_count=20, keyframes_per_node=50)
    assert sum(len(n.keyframe_tracks.get("rotation", {}).get("keyframes", []))
               for n in g.nodes.values()) == 1000

    model = KeyframesModel()
    start = time.monotonic()
    model.rebuild(g)
    elapsed = time.monotonic() - start
    assert elapsed < 0.2, f"model rebuild took {elapsed:.3f}s for 1000 keyframes"

    # All keyframes are reflected in the model.
    total_kfs = sum(len(tr.keyframes) for tr in model.tracks().values())
    assert total_kfs == 1000


def test_visible_rows_under_many_nodes():
    """visible_rows() stays linear with the node count."""
    g = _graph_with_many_keyframes(node_count=50, keyframes_per_node=5)
    model = KeyframesModel()
    model.rebuild(g)
    start = time.monotonic()
    rows = model.visible_rows()
    elapsed = time.monotonic() - start
    assert elapsed < 0.05
    # 50 nodes + their param rows
    assert len(rows) >= 50


def test_frame_mapper_is_constant_time():
    """Frame mapping must not depend on the number of keyframes."""
    fm = FrameMapper(pixels_per_frame=4.0, scroll_offset=100.0)
    # 10000 lookups should be near-instant.
    start = time.monotonic()
    for f in range(0, 10000, 7):
        fm.frame_to_x(f)
        fm.x_to_frame(f * 4.0 + 100.0)
    elapsed = time.monotonic() - start
    assert elapsed < 0.05
