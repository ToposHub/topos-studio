"""P1-15 persistence tests for forward-compatible node payloads."""

import pytest

from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.nodes.image_input_node import ImageInputNode
from src.features.composite.nodes.unknown_placeholder_node import (
    UnknownPlaceholderNode,
)


pytestmark = pytest.mark.composite_core


def test_known_node_roundtrip_preserves_unknown_top_level_fields():
    data = TransformNode(node_id="transform-1").to_dict()
    data["plugin_extension"] = {
        "schema": 7,
        "custom_knobs": {"quality": "film"},
    }
    data["future_flags"] = ["subframe", "vector-track"]

    restored = TransformNode.from_dict(data)
    saved = restored.to_dict()

    assert saved["plugin_extension"] == data["plugin_extension"]
    assert saved["future_flags"] == data["future_flags"]


def test_unknown_node_roundtrip_preserves_payload_and_animation():
    data = {
        "id": "plugin-node-1",
        "type": "future_blend_v2",
        "position": [10, 20],
        "parameters": {"mix": 0.5},
        "disabled": True,
        "expressions": {"mix": "frame * 0.01"},
        "keyframe_tracks": {
            "mix": {"keyframes": [{"frame": 0, "value": 0.0}]},
        },
        "time_range": {"comp_start": 2, "comp_end": 20},
        "node_color": "#123456",
        "plugin_extension": {"version": 2, "opaque": [1, 2, 3]},
    }

    restored = UnknownPlaceholderNode.from_dict(data)
    saved = restored.to_dict()

    assert saved == data


def test_source_metadata_keeps_unknown_nested_extension_fields():
    data = ImageInputNode(node_id="image-1").to_dict()
    data["_meta"]["vendor_metadata"] = {"camera": "A001"}

    saved = ImageInputNode.from_dict(data).to_dict()

    assert saved["_meta"]["vendor_metadata"] == {"camera": "A001"}
