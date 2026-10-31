"""P7 tests: graph-level save/load round-trip + schema migration v6.

Verifies that keyframe_tracks, time_range and node_color survive a full
NodeGraph.to_dict() → from_dict() cycle, and that legacy v5 graphs (with
animation embedded inside parameters or at graph-level keyframe_tracks)
migrate cleanly to the per-node v6 store.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.core.graph_migration import (
    COMPOSITE_SCHEMA_VERSION, migrate_v5_to_v6, migrate_graph)
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.nodes.blur_node import BlurNode
from src.features.composite.nodes.output_node import OutputNode
from src.shared.keyframe import Keyframe, KeyframeTrack, InterpolationType


pytestmark = pytest.mark.composite_core


# ── Graph-level round-trip ──────────────────────────────────────────────

def test_graph_round_trip_preserves_keyframe_tracks():
    """P7: a graph with per-node animation reloads identically."""
    g = NodeGraph()
    t = TransformNode()
    track = KeyframeTrack(param_name="rotation", min_val=-360.0, max_val=360.0)
    track.add_keyframe(Keyframe(0, 0.0, InterpolationType.LINEAR))
    track.add_keyframe(Keyframe(72, 90.0, InterpolationType.EASE_IN_OUT))
    t.set_keyframe_track("rotation", track)
    out = OutputNode()
    g.add_node(t)
    g.add_node(out)
    g.connect(t.id, "result", out.id, "image")

    data = g.to_dict()
    assert data["composite_schema_version"] == COMPOSITE_SCHEMA_VERSION

    g2 = NodeGraph.from_dict(data)
    t2 = g2.get_node(t.id)
    assert t2 is not None
    assert t2.is_param_animated("rotation")
    kfs = t2.keyframe_tracks["rotation"]["keyframes"]
    assert len(kfs) == 2
    assert kfs[1]["frame"] == 72
    assert kfs[1]["interpolation"] == "ease_in_out"


def test_graph_round_trip_preserves_time_range():
    g = NodeGraph()
    b = BlurNode()
    b.time_range = {"comp_start": 5, "comp_end": 50}
    g.add_node(b)

    data = g.to_dict()
    g2 = NodeGraph.from_dict(data)
    b2 = g2.get_node(b.id)
    assert b2.time_range == {"comp_start": 5, "comp_end": 50}


def test_graph_round_trip_preserves_node_color():
    g = NodeGraph()
    b = BlurNode()
    b.custom_header_color = "#ff8800"
    g.add_node(b)

    data = g.to_dict()
    g2 = NodeGraph.from_dict(data)
    b2 = g2.get_node(b.id)
    assert b2.custom_header_color == "#ff8800"
    assert b2.header_color == "#ff8800"


def test_graph_round_trip_no_animation_stays_clean():
    """A graph with no animation must not invent empty keyframe_tracks."""
    g = NodeGraph()
    g.add_node(BlurNode())
    g.add_node(OutputNode())
    data = g.to_dict()
    for node in data["nodes"]:
        assert "keyframe_tracks" not in node


# ── Migration v5 → v6 ───────────────────────────────────────────────────

def test_migration_v5_to_v6_hoists_legacy_parameter_dict():
    """Legacy animation embedded in parameters dict is hoisted to
    per-node keyframe_tracks (P1.10)."""
    legacy = {
        "composite_schema_version": 5,
        "nodes": [
            {
                "id": "blur1",
                "type": "blur",
                "parameters": {
                    "radius": {"animated": True,
                               "keyframes": [
                                   {"frame": 0, "value": 1.0},
                                   {"frame": 10, "value": 5.0}],
                               "min_val": 0.0, "max_val": 100.0},
                },
            }
        ],
        "connections": [],
    }
    migrated = migrate_v5_to_v6(legacy)
    node = migrated["nodes"][0]
    assert "keyframe_tracks" in node
    assert "radius" in node["keyframe_tracks"]
    kfs = node["keyframe_tracks"]["radius"]["keyframes"]
    assert len(kfs) == 2
    # The parameter value is restored to a plain scalar (not a dict).
    assert not isinstance(node["parameters"]["radius"], dict)


def test_migration_v5_to_v6_distributes_graph_level_tracks():
    """Graph-level keyframe_tracks keyed by node_id.param are distributed
    onto the corresponding node."""
    legacy = {
        "composite_schema_version": 5,
        "nodes": [
            {"id": "t1", "type": "transform", "parameters": {"rotation": 0.0}},
        ],
        "connections": [],
        "keyframe_tracks": {
            "t1.rotation": {
                "keyframes": [{"frame": 0, "value": 0.0},
                              {"frame": 20, "value": 45.0}],
                "min_val": -360.0, "max_val": 360.0,
            },
        },
    }
    migrated = migrate_v5_to_v6(legacy)
    # Graph-level store is removed.
    assert "keyframe_tracks" not in migrated
    node = migrated["nodes"][0]
    assert "rotation" in node["keyframe_tracks"]
    kfs = node["keyframe_tracks"]["rotation"]["keyframes"]
    assert len(kfs) == 2


def test_migrate_graph_full_chain_bumps_version():
    """migrate_graph on a v5 dict produces a v6 dict."""
    legacy = {
        "composite_schema_version": 5,
        "nodes": [
            {"id": "b1", "type": "blur",
             "parameters": {"radius": {"animated": True,
                                       "keyframes": [{"frame": 0, "value": 1.0}]}}},
        ],
        "connections": [],
    }
    migrated = migrate_graph(legacy)
    assert migrated["composite_schema_version"] == COMPOSITE_SCHEMA_VERSION


def test_v1_project_loads_through_full_chain():
    """A very old v1 graph with no animation loads without error."""
    legacy = {
        "composite_schema_version": 1,
        "nodes": [{"id": "b1", "type": "blur", "parameters": {"radius": 5.0}}],
        "connections": [],
    }
    g = NodeGraph.from_dict(legacy)
    assert g.get_node("b1") is not None


def test_old_project_with_legacy_animation_loads_and_animates():
    """End-to-end: a v5 graph with legacy parameter-dict animation loads
    into a NodeGraph and the resulting node reports an animated track."""
    legacy = {
        "composite_schema_version": 5,
        "nodes": [
            {"id": "b1", "type": "blur",
             "parameters": {"radius": {"animated": True,
                                       "keyframes": [
                                           {"frame": 0, "value": 1.0},
                                           {"frame": 10, "value": 8.0}],
                                       "min_val": 0.0, "max_val": 100.0}}},
        ],
        "connections": [],
    }
    g = NodeGraph.from_dict(legacy)
    b = g.get_node("b1")
    assert b is not None
    assert b.is_param_animated("radius")
    # Snapshot sees the migrated per-node track.
    snap = g.snapshot(frame_number=5)
    assert "radius" in snap.nodes["b1"].keyframe_tracks
