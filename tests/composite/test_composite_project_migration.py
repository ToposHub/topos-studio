"""
Tests for composite graph schema migration.

Covers:
- v1 → v2 migration: "blend" type alias to "merge"
- Missing parameters filled with safe defaults
- image_input gets color_space parameter
- disabled field default
- v2 graph passes through unchanged
- Round-trip: to_dict → from_dict preserves all data
"""

import sys
import os
import pytest
import copy

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.core.graph_migration import (
    COMPOSITE_SCHEMA_VERSION,
    migrate_graph,
    migrate_v1_to_v2,
)
from src.features.composite.core.node_engine import NodeGraph, NODE_REGISTRY
from src.features.composite.nodes.blend_node import MergeNode
from src.features.composite.nodes.image_input_node import ImageInputNode
from src.features.composite.nodes.output_node import OutputNode
from src.features.composite.nodes.color_node import COLORNode
from src.features.composite.nodes.blur_node import BlurNode
from src.features.composite.nodes.constant_node import ConstantNode
from src.features.composite.nodes.premult_node import PremultNode
from src.features.composite.nodes.shuffle_node import ShuffleNode
from src.features.composite.nodes.reformat_node import ReformatNode
from src.features.composite.nodes.crop_node import CropNode


# ── Helper ──

def _make_v1_graph():
    """Build a dict that mimics a v1-era saved graph."""
    return {
        "nodes": [
            {
                "id": "color01",
                "type": "color_input",
                "position": [100, 200],
                "parameters": {},
            },
            {
                "id": "img01",
                "type": "image_input",
                "position": [100, 400],
                "parameters": {
                    "file_path": "/some/image.png",
                },
            },
            {
                "id": "blend01",
                "type": "blend",
                "position": [300, 200],
                "parameters": {
                    "blend_mode": "over",
                },
            },
            {
                "id": "blur01",
                "type": "blur",
                "position": [300, 400],
                "parameters": {
                    "radius": 5.0,
                },
            },
            {
                "id": "out01",
                "type": "output",
                "position": [500, 200],
                "parameters": {
                    "resolution_mode": "auto",
                    "width": 1920,
                    "height": 1080,
                },
            },
        ],
        "connections": [
            {
                "src_node_id": "color01",
                "src_port_name": "result",
                "dst_node_id": "blend01",
                "dst_port_name": "A",
            },
            {
                "src_node_id": "img01",
                "src_port_name": "image",
                "dst_node_id": "blend01",
                "dst_port_name": "B",
            },
            {
                "src_node_id": "blend01",
                "src_port_name": "result",
                "dst_node_id": "out01",
                "dst_port_name": "image",
            },
        ],
    }


# ── Tests ──

class TestV1toV2Migration:
    """Test the v1 → v2 migration function directly."""

    def test_blend_type_becomes_merge(self):
        data = _make_v1_graph()
        migrated = migrate_v1_to_v2(data)

        types = [n["type"] for n in migrated["nodes"]]
        assert "blend" not in types
        assert "merge" in types

        # The blend node should now be type "merge"
        blend_node = next(n for n in migrated["nodes"] if n["id"] == "blend01")
        assert blend_node["type"] == "merge"

    def test_merge_gets_new_parameters(self):
        data = _make_v1_graph()
        migrated = migrate_v1_to_v2(data)

        merge_node = next(n for n in migrated["nodes"] if n["id"] == "blend01")
        params = merge_node["parameters"]

        # Original parameter preserved
        assert params["blend_mode"] == "over"
        # New v2 parameters filled with defaults
        assert "opacity" in params
        assert params["opacity"] == 1.0
        assert "format_policy" in params
        assert params["format_policy"] == "from_b"
        assert "bbox_policy" in params
        assert params["bbox_policy"] == "union"

    def test_image_input_gets_color_space(self):
        data = _make_v1_graph()
        migrated = migrate_v1_to_v2(data)

        img_node = next(n for n in migrated["nodes"] if n["id"] == "img01")
        assert img_node["parameters"]["color_space"] == "auto"
        assert img_node["parameters"]["file_path"] == "/some/image.png"

    def test_disabled_field_added(self):
        data = _make_v1_graph()
        migrated = migrate_v1_to_v2(data)

        for node in migrated["nodes"]:
            assert "disabled" in node
            assert node["disabled"] is False

    def test_existing_parameters_not_overwritten(self):
        data = _make_v1_graph()
        # Set a custom opacity on the blend node (v1 didn't have this, but test safety)
        blend = next(n for n in data["nodes"] if n["id"] == "blend01")
        blend["parameters"]["opacity"] = 0.5
        blend["parameters"]["format_policy"] = "from_a"

        migrated = migrate_v1_to_v2(data)
        merge_node = next(n for n in migrated["nodes"] if n["id"] == "blend01")
        assert merge_node["parameters"]["opacity"] == 0.5
        assert merge_node["parameters"]["format_policy"] == "from_a"

    def test_does_not_mutate_input(self):
        data = _make_v1_graph()
        original = copy.deepcopy(data)
        migrate_v1_to_v2(data)
        assert data == original

    def test_connections_preserved(self):
        data = _make_v1_graph()
        migrated = migrate_v1_to_v2(data)

        assert len(migrated["connections"]) == 3
        # Connection from blend01 should still work (type changed but id preserved)
        conn = migrated["connections"][0]
        assert conn["src_node_id"] == "color01"
        assert conn["dst_node_id"] == "blend01"

    def test_empty_connections_get_default(self):
        data = {"nodes": []}
        migrated = migrate_v1_to_v2(data)
        assert "connections" in migrated
        assert migrated["connections"] == []


class TestMigrateGraph:
    """Test the high-level migrate_graph() entry point."""

    def test_v1_detected_and_migrated(self):
        data = _make_v1_graph()
        assert "composite_schema_version" not in data

        migrated = migrate_graph(data)
        assert migrated["composite_schema_version"] == COMPOSITE_SCHEMA_VERSION
        assert migrated["nodes"][0]["disabled"] is False  # v2 field present

    def test_v2_migrates_to_v3(self):
        data = {
            "composite_schema_version": 2,
            "nodes": [
                {"id": "a", "type": "merge", "parameters": {"blend_mode": "over"},
                 "disabled": True, "position": [0, 0]},
            ],
            "connections": [],
        }
        migrated = migrate_graph(data)
        # v2 should be upgraded to current version (v3)
        assert migrated["composite_schema_version"] == COMPOSITE_SCHEMA_VERSION
        assert migrated["nodes"][0]["disabled"] is True

    def test_empty_graph_passes_through(self):
        assert migrate_graph({}) == {}
        assert migrate_graph(None) is None

    def test_does_not_mutate_input(self):
        data = _make_v1_graph()
        original = copy.deepcopy(data)
        migrate_graph(data)
        assert data == original

    def test_v2_transform_uv_to_pixel_space(self):
        """v2 transform nodes with UV-space pivot/translate convert to pixel-space."""
        data = {
            "composite_schema_version": 2,
            "nodes": [
                {"id": "t1", "type": "transform", "parameters": {
                    "translate_x": 0.1, "translate_y": -0.05,
                    "pivot_x": 0.5, "pivot_y": 0.5,
                    "scale_x": 1.0, "scale_y": 1.0,
                    "rotation": 45.0, "filter": "linear",
                }, "disabled": False, "position": [0, 0]},
            ],
            "connections": [],
        }
        migrated = migrate_graph(data)
        params = migrated["nodes"][0]["parameters"]
        # pivot (0.5,0.5) = center → pivot_mode="center"
        assert params["pivot_mode"] == "center"
        # translate should be converted from UV to pixels
        assert params["translate_x"] == 192.0   # 0.1 * 1920
        assert params["translate_y"] == -54.0    # -0.05 * 1080

    def test_v2_transform_non_center_pivot(self):
        """v2 transform with non-center UV pivot converts to pixel-space custom."""
        data = {
            "composite_schema_version": 2,
            "nodes": [
                {"id": "t1", "type": "transform", "parameters": {
                    "pivot_x": 0.25, "pivot_y": 0.75,
                    "scale_x": 1.0, "scale_y": 1.0,
                    "rotation": 30.0, "filter": "linear",
                }, "disabled": False, "position": [0, 0]},
            ],
            "connections": [],
        }
        migrated = migrate_graph(data)
        params = migrated["nodes"][0]["parameters"]
        assert params["pivot_mode"] == "custom"
        assert params["pivot_x"] == 480.0   # 0.25 * 1920
        assert params["pivot_y"] == 810.0   # 0.75 * 1080

    def test_v2_transform_old_param_names(self):
        """v2 transform with old param names (rotate, scale, anchor) gets renamed."""
        data = {
            "composite_schema_version": 2,
            "nodes": [
                {"id": "t1", "type": "transform", "parameters": {
                    "rotate": 30.0,
                    "scale": 1.5,
                    "anchor_x": 0.5,
                    "anchor_y": 0.5,
                    "filter": "linear",
                }, "disabled": False, "position": [0, 0]},
            ],
            "connections": [],
        }
        migrated = migrate_graph(data)
        params = migrated["nodes"][0]["parameters"]
        assert "rotation" in params
        assert params["rotation"] == 30.0
        assert "scale_x" in params and "scale_y" in params
        assert params["scale_x"] == 1.5
        # anchor (0.5, 0.5) = center → pivot_mode="center"
        assert params["pivot_mode"] == "center"


class TestNodeGraphRoundTrip:
    """Test that NodeGraph.to_dict / from_dict round-trips correctly."""

    def test_saves_schema_version(self):
        g = NodeGraph()
        g.create_default_graph()
        data = g.to_dict()
        assert data["composite_schema_version"] == COMPOSITE_SCHEMA_VERSION

    def test_roundtrip_v2_graph(self):
        g = NodeGraph()
        g.create_default_graph()
        data = g.to_dict()

        g2 = NodeGraph.from_dict(data)
        assert len(g2.nodes) == 3  # TimelineClipInput → Topos Color → Output
        assert len(g2.connections) == 2

    def test_load_v1_graph_through_from_dict(self):
        """Loading a v1 dict via NodeGraph.from_dict should auto-migrate."""
        v1_data = _make_v1_graph()

        g = NodeGraph.from_dict(v1_data)
        # This v1 graph has color_input but NO timeline_clip_input, so it is a
        # standalone-image graph and migration is skipped (color_input is the
        # valid Color-page bridge for single images). Node count stays at 5.
        assert len(g.nodes) == 5

        # blend01 should be a MergeNode (type alias resolved)
        blend = g.get_node("blend01")
        assert blend is not None
        assert isinstance(blend, MergeNode)
        assert blend.node_type == "merge"

        # image_input should have color_space
        img = g.get_node("img01")
        assert img is not None
        assert img.parameters.get("color_space") == "auto"

        # color_input is preserved (not migrated) — standalone-image path
        assert g.get_node("color01") is not None
        types = {n.node_type for n in g.nodes.values()}
        assert "color_input" in types
        assert "timeline_clip_input" not in types

        # Connections unchanged: 3 original
        assert len(g.connections) == 3

    def test_v1_blend_node_gets_v2_defaults(self):
        v1_data = _make_v1_graph()
        g = NodeGraph.from_dict(v1_data)

        merge = g.get_node("blend01")
        assert merge is not None
        assert merge.parameters.get("format_policy") == "from_b"
        assert merge.parameters.get("bbox_policy") == "union"
        assert merge.parameters.get("opacity") == 1.0


class TestNewNodesDeserialization:
    """Verify that new phase-2 nodes can be loaded from saved data."""

    def test_premult_roundtrip(self):
        g = NodeGraph()
        node = PremultNode()
        g.add_node(node)
        data = g.to_dict()

        g2 = NodeGraph.from_dict(data)
        loaded = list(g2.nodes.values())[0]
        assert isinstance(loaded, PremultNode)
        assert loaded.node_type == "premult"

    def test_shuffle_roundtrip(self):
        g = NodeGraph()
        node = ShuffleNode()
        node.set_parameter("a_from", "luma")
        g.add_node(node)
        data = g.to_dict()

        g2 = NodeGraph.from_dict(data)
        loaded = list(g2.nodes.values())[0]
        assert isinstance(loaded, ShuffleNode)
        assert loaded.parameters["a_from"] == "luma"
        assert loaded.parameters["r_from"] == "r"  # Default preserved

    def test_reformat_roundtrip(self):
        g = NodeGraph()
        node = ReformatNode()
        node.set_parameter("resize_mode", "fill")
        g.add_node(node)
        data = g.to_dict()

        g2 = NodeGraph.from_dict(data)
        loaded = list(g2.nodes.values())[0]
        assert isinstance(loaded, ReformatNode)
        assert loaded.parameters["resize_mode"] == "fill"
        assert loaded.parameters["filter"] == "linear"

    def test_crop_roundtrip(self):
        g = NodeGraph()
        node = CropNode()
        g.add_node(node)
        data = g.to_dict()

        g2 = NodeGraph.from_dict(data)
        loaded = list(g2.nodes.values())[0]
        assert isinstance(loaded, CropNode)
        assert loaded.parameters["outside"] == "transparent"

    def test_old_crop_data_without_new_fields(self):
        """Simulate loading a v1-era crop node that only has partial data."""
        data = {
            "nodes": [
                {
                    "id": "crop1",
                    "type": "crop",
                    "position": [0, 0],
                    "parameters": {"x": 10, "y": 20},
                }
            ],
            "connections": [],
        }
        # This should still work — migration fills missing defaults
        g = NodeGraph.from_dict(data)
        crop = g.get_node("crop1")
        assert crop is not None
        assert crop.parameters["x"] == 10
        assert crop.parameters["y"] == 20
        assert crop.parameters["width"] == 512  # Default from PARAM_DEFS


class TestFormatSerialization:
    """Verify ImageFormat and BBox round-trip through serialization."""

    def test_image_format_roundtrip(self):
        from src.features.composite.core.format import ImageFormat
        fmt = ImageFormat(width=3840, height=2160, pixel_aspect=1.5, name="UHD Wide")
        data = fmt.to_dict()
        fmt2 = ImageFormat.from_dict(data)
        assert fmt2.width == 3840
        assert fmt2.height == 2160
        assert fmt2.pixel_aspect == 1.5
        assert fmt2.name == "UHD Wide"

    def test_bbox_roundtrip(self):
        from src.features.composite.core.format import BBox
        bbox = BBox(x=10, y=20, width=100, height=200)
        data = bbox.to_dict()
        bbox2 = BBox.from_dict(data)
        assert bbox2.x == 10
        assert bbox2.y == 20
        assert bbox2.width == 100
        assert bbox2.height == 200

    def test_bbox_union_intersection(self):
        from src.features.composite.core.format import BBox
        a = BBox(x=0, y=0, width=100, height=100)
        b = BBox(x=50, y=50, width=100, height=100)
        u = a.union(b)
        assert u.x == 0 and u.y == 0
        assert u.width == 150 and u.height == 150

        i = a.intersection(b)
        assert i.x == 50 and i.y == 50
        assert i.width == 50 and i.height == 50
