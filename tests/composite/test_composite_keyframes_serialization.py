"""P0/P1 tests: per-node keyframe track isolation + serialization.

These tests were written first as failing tests (P0) to prove the old
PARAM_DEFS-based keyframe store leaked state between same-type nodes. After
P1 they must pass: each Node instance owns its own keyframe_tracks,
time_range and custom_header_color, and the data round-trips through
to_dict()/from_dict().
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


# ── A-1: two same-type nodes must NOT share keyframe state ──────────────

def test_two_transform_nodes_do_not_share_keyframes():
    """P0.1: animating one Transform must not animate another Transform."""
    a = TransformNode()
    b = TransformNode()

    track = KeyframeTrack(param_name="rotation", min_val=-360.0, max_val=360.0)
    track.add_keyframe(Keyframe(0, 0.0))
    track.add_keyframe(Keyframe(10, 45.0))
    a.set_keyframe_track("rotation", track)

    assert a.is_param_animated("rotation") is True
    assert b.is_param_animated("rotation") is False
    assert "rotation" in a.keyframe_tracks
    assert "rotation" not in b.keyframe_tracks


def test_two_blur_nodes_isolated_keyframes():
    """Same isolation check for a different node type."""
    a = BlurNode()
    b = BlurNode()

    track = KeyframeTrack(param_name="radius", min_val=0.0, max_val=100.0)
    track.add_keyframe(Keyframe(0, 1.0))
    track.add_keyframe(Keyframe(20, 10.0))
    a.set_keyframe_track("radius", track)

    assert a.is_param_animated("radius")
    assert not b.is_param_animated("radius")


# ── A-2: serialization round-trip ───────────────────────────────────────

def test_keyframe_tracks_round_trip_through_to_dict_from_dict():
    """P0.6/P1.2/P1.3: keyframes survive save → reload."""
    node = TransformNode()
    track = KeyframeTrack(param_name="rotation", min_val=-360.0, max_val=360.0)
    track.add_keyframe(Keyframe(0, 0.0, InterpolationType.LINEAR))
    track.add_keyframe(Keyframe(72, 90.0, InterpolationType.EASE_IN_OUT))
    node.set_keyframe_track("rotation", track)

    data = node.to_dict()
    assert "keyframe_tracks" in data
    assert "rotation" in data["keyframe_tracks"]
    assert len(data["keyframe_tracks"]["rotation"]["keyframes"]) == 2

    restored = TransformNode.from_dict(data)
    assert restored.is_param_animated("rotation")
    kfs = restored.keyframe_tracks["rotation"]["keyframes"]
    assert kfs[0]["frame"] == 0
    assert kfs[1]["frame"] == 72
    assert pytest.approx(kfs[1]["value"]) == 90.0


def test_node_without_keyframes_omits_field():
    """A node with no animation should not emit an empty keyframe_tracks key."""
    node = BlurNode()
    data = node.to_dict()
    assert "keyframe_tracks" not in data


def test_removed_track_not_serialized():
    """remove_keyframe_track drops the entry so it isn't saved."""
    node = TransformNode()
    track = KeyframeTrack(param_name="rotation")
    track.add_keyframe(Keyframe(0, 0.0))
    node.set_keyframe_track("rotation", track)
    node.remove_keyframe_track("rotation")
    data = node.to_dict()
    assert "keyframe_tracks" not in data


# ── A-11: node color persistence ────────────────────────────────────────

def test_custom_node_color_round_trips():
    """A user-picked node color must survive save/reload."""
    node = BlurNode()
    node.custom_header_color = "#7a6a9a"
    data = node.to_dict()
    assert data.get("node_color") == "#7a6a9a"

    restored = BlurNode.from_dict(data)
    assert restored.custom_header_color == "#7a6a9a"
    # header_color is mirrored so legacy readers see the override
    assert restored.header_color == "#7a6a9a"


def test_default_color_not_serialized():
    """No custom color → no node_color key (keep serialization clean)."""
    node = BlurNode()
    data = node.to_dict()
    assert "node_color" not in data


def test_effective_header_color_respects_override():
    node = BlurNode()
    default_color = node.header_color
    assert node.effective_header_color() == default_color
    node.custom_header_color = "#123456"
    assert node.effective_header_color() == "#123456"


# ── A-3: NodeGraph.snapshot reads node-instance tracks ──────────────────

def test_snapshot_reads_per_node_keyframe_tracks():
    """P1.6: snapshot must pull keyframes from node.keyframe_tracks, not
    from the class-level PARAM_DEFS."""
    g = NodeGraph()
    a = TransformNode()
    b = TransformNode()
    g.add_node(a)
    g.add_node(b)

    track = KeyframeTrack(param_name="rotation", min_val=-360.0, max_val=360.0)
    track.add_keyframe(Keyframe(0, 0.0))
    track.add_keyframe(Keyframe(10, 30.0))
    a.set_keyframe_track("rotation", track)

    snap = g.snapshot()
    a_snap = snap.nodes[a.id]
    b_snap = snap.nodes[b.id]

    assert "rotation" in a_snap.keyframe_tracks
    assert "rotation" not in b_snap.keyframe_tracks


def test_snapshot_carries_node_color():
    g = NodeGraph()
    node = BlurNode()
    node.custom_header_color = "#abcdef"
    g.add_node(node)
    snap = g.snapshot()
    assert snap.nodes[node.id].node_color == "#abcdef"


def test_snapshot_carries_time_range():
    g = NodeGraph()
    node = BlurNode()
    node.time_range = {"comp_start": 0, "comp_end": 100}
    g.add_node(node)
    snap = g.snapshot()
    assert snap.nodes[node.id].time_range == {"comp_start": 0, "comp_end": 100}


# ── A-8: duplicate frame policy ─────────────────────────────────────────

def test_add_keyframe_replaces_duplicate_frame():
    """P1.8: adding a keyframe at an existing frame replaces it (no dups)."""
    track = KeyframeTrack(param_name="x")
    track.add_keyframe(Keyframe(10, 1.0))
    track.add_keyframe(Keyframe(10, 5.0))  # same frame → replace
    assert len(track.keyframes) == 1
    assert track.keyframes[0].value == 5.0


# ── N-04（2026-10-14 审计）：expression / keyframe 共存（base + 覆盖）───

def test_setting_expression_keeps_keyframe_track():
    """N-04：设置表达式不再删除轨道——轨道构成 base，启用的表达式在
    求值时覆盖 base（loopOut/valueAtTime 采样 pre-expression 轨道）。"""
    node = TransformNode()
    track = KeyframeTrack(param_name="rotation")
    track.add_keyframe(Keyframe(0, 0.0))
    node.set_keyframe_track("rotation", track)
    assert node.is_param_animated("rotation")

    # Setting an expression on the same param keeps the track.
    assert node.set_expression("rotation", "frame * 0.1")
    assert node.is_param_animated("rotation")
    assert node.has_expression("rotation")


def test_setting_keyframe_track_keeps_expression():
    """N-04：加轨道不再清除表达式（对称）。"""
    node = TransformNode()
    assert node.set_expression("rotation", "frame * 0.1")
    assert node.has_expression("rotation")

    track = KeyframeTrack(param_name="rotation")
    track.add_keyframe(Keyframe(0, 0.0))
    node.set_keyframe_track("rotation", track)

    assert node.is_param_animated("rotation")
    assert node.has_expression("rotation")


# ── P5-3: load-path expression/keyframe mutual exclusion ────────────────

def test_load_with_expression_and_keyframe_keeps_both():
    """N-04：同时携带表达式与关键帧的载荷按共存模型载入（表达式在求值
    时覆盖 base），不再丢表达式。"""
    node = TransformNode()
    # Manually craft a dict where rotation has both.
    data = {
        "id": node.id,
        "type": "transform",
        "parameters": {"rotation": 0.0},
        "expressions": {"rotation": "frame * 0.1"},
        "keyframe_tracks": {
            "rotation": {
                "param_name": "rotation",
                "keyframes": [{"frame": 0, "value": 0.0}],
                "min_val": -360.0, "max_val": 360.0,
            }
        },
    }
    restored = TransformNode.from_dict(data)
    assert restored.is_param_animated("rotation")
    # N-04：表达式保留（求值时覆盖轨道 base）。
    assert restored.has_expression("rotation")


def test_load_expression_without_keyframe_keeps_expression():
    """An expression alone (no keyframes) survives load."""
    node = TransformNode()
    data = {
        "id": node.id, "type": "transform",
        "parameters": {"rotation": 0.0},
        "expressions": {"rotation": "frame * 0.1"},
    }
    restored = TransformNode.from_dict(data)
    assert restored.has_expression("rotation")
    assert not restored.is_param_animated("rotation")
