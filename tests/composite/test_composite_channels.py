"""
Tests for Channel system nodes and Color Space utilities.

Covers:
- ShuffleNode: identity passthrough, marker generation, index mapping, serialization
- CopyChannelNode: A/B inputs, marker, identity detection, serialization
- CSTransformNode: no-op detection, marker, direction index, serialization
- ColorSpace constants and utilities
- Channel source index mapping
"""
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.nodes.base_node import TextureResult
from src.features.composite.nodes.shuffle_node import ShuffleNode, CHANNEL_SOURCES
from src.features.composite.nodes.copy_channel_node import CopyChannelNode, CHANNELS
from src.features.composite.nodes.cs_transform_node import CSTransformNode, CS_TRANSFORM_MODES
from src.features.composite.core.color_space import ColorSpace, srgb_to_linear_value, linear_to_srgb_value

import pytest
pytestmark = pytest.mark.composite_core


# ── Helpers ──

def _make_tr(w=100, h=100, tex_id=1, color_space=None, alpha_premultiplied=None):
    return TextureResult(
        texture_id=tex_id, width=w, height=h,
        color_space=color_space,
        alpha_premultiplied=alpha_premultiplied,
    )


# ── ShuffleNode ──

class TestShuffleNodeStructure:
    def test_type(self):
        assert ShuffleNode.node_type == "shuffle"

    def test_inputs(self):
        assert [p.name for p in ShuffleNode().inputs] == ["image", "mask"]  # CP-044 通用 mask

    def test_outputs(self):
        assert [p.name for p in ShuffleNode().outputs] == ["image"]

    def test_defaults(self):
        node = ShuffleNode()
        assert node.parameters["r_from"] == "r"
        assert node.parameters["g_from"] == "g"
        assert node.parameters["b_from"] == "b"
        assert node.parameters["a_from"] == "a"


class TestShuffleNodeIdentity:
    def test_identity_passthrough(self):
        """Default shuffle (r→r, g→g, b→b, a→a) is a no-op."""
        node = ShuffleNode()
        inp = _make_tr(tex_id=42)
        result = node._do_evaluate({"image": inp}, None, None)
        assert result["image"] is inp

    def test_non_identity_returns_marker(self):
        node = ShuffleNode()
        node.parameters["a_from"] = "luma"
        inp = _make_tr(tex_id=42)
        result = node._do_evaluate({"image": inp}, None, None)
        assert result["_effect_type"] == "shuffle"
        assert result["_effect_input"] is inp

    def test_no_input_empty(self):
        node = ShuffleNode()
        result = node._do_evaluate({}, None, None)
        assert result == {}


class TestShuffleNodeIndices:
    def test_source_list(self):
        assert CHANNEL_SOURCES == ["r", "g", "b", "a", "zero", "one", "luma"]

    def test_r_index(self):
        assert CHANNEL_SOURCES.index("r") == 0

    def test_a_index(self):
        assert CHANNEL_SOURCES.index("a") == 3

    def test_zero_index(self):
        assert CHANNEL_SOURCES.index("zero") == 4

    def test_one_index(self):
        assert CHANNEL_SOURCES.index("one") == 5

    def test_luma_index(self):
        assert CHANNEL_SOURCES.index("luma") == 6

    def test_alpha_from_luma_marker(self):
        node = ShuffleNode()
        node.parameters["a_from"] = "luma"
        inp = _make_tr()
        result = node._do_evaluate({"image": inp}, None, None)
        params = result["_effect_params"]
        assert params["r_from"] == 0  # r
        assert params["g_from"] == 1  # g
        assert params["b_from"] == 2  # b
        assert params["a_from"] == 6  # luma

    def test_all_zero_marker(self):
        node = ShuffleNode()
        node.parameters["r_from"] = "zero"
        node.parameters["g_from"] = "zero"
        node.parameters["b_from"] = "zero"
        node.parameters["a_from"] = "zero"
        inp = _make_tr()
        result = node._do_evaluate({"image": inp}, None, None)
        params = result["_effect_params"]
        assert params["r_from"] == 4
        assert params["g_from"] == 4
        assert params["b_from"] == 4
        assert params["a_from"] == 4


class TestShuffleNodeSerialization:
    def test_roundtrip(self):
        node = ShuffleNode()
        node.set_parameter("a_from", "luma")
        node.set_parameter("r_from", "zero")
        d = node.to_dict()
        restored = ShuffleNode.from_dict(d)
        assert restored.parameters["a_from"] == "luma"
        assert restored.parameters["r_from"] == "zero"
        assert restored.parameters["g_from"] == "g"  # preserved default

    def test_old_data(self):
        d = {"id": "s1", "type": "shuffle", "parameters": {"r_from": "b"}}
        node = ShuffleNode.from_dict(d)
        assert node.parameters["r_from"] == "b"
        assert node.parameters["g_from"] == "g"  # default


# ── CopyChannelNode ──

class TestCopyChannelNodeStructure:
    def test_type(self):
        assert CopyChannelNode.node_type == "copy_channel"

    def test_inputs(self):
        assert [p.name for p in CopyChannelNode().inputs] == ["A", "B", "mask"]  # CP-044 通用 mask

    def test_outputs(self):
        assert [p.name for p in CopyChannelNode().outputs] == ["image"]

    def test_defaults(self):
        node = CopyChannelNode()
        assert node.parameters["src_channel"] == "a"
        assert node.parameters["dst_channel"] == "a"


class TestCopyChannelNodeMarkers:
    def test_both_inputs_marker(self):
        node = CopyChannelNode()
        node.parameters["src_channel"] = "r"
        node.parameters["dst_channel"] = "a"
        a = _make_tr(tex_id=1)
        b = _make_tr(tex_id=2)
        result = node._do_evaluate({"A": a, "B": b}, None, None)
        assert result["_effect_type"] == "copy_channel"
        assert result["_effect_input"] is a

    def test_no_b_passthrough_a(self):
        node = CopyChannelNode()
        a = _make_tr(tex_id=1)
        result = node._do_evaluate({"A": a}, None, None)
        assert result["image"] is a

    def test_no_a_empty(self):
        node = CopyChannelNode()
        b = _make_tr(tex_id=2)
        result = node._do_evaluate({"B": b}, None, None)
        assert result == {}

    def test_identity_same_texture(self):
        """Same texture + same channel = identity."""
        node = CopyChannelNode()
        tex = _make_tr(tex_id=1)
        result = node._do_evaluate({"A": tex, "B": tex}, None, None)
        assert result["image"] is tex

    def test_no_input_empty(self):
        node = CopyChannelNode()
        assert node._do_evaluate({}, None, None) == {}


class TestCopyChannelNodeIndices:
    def test_channels_list(self):
        assert CHANNELS == ["r", "g", "b", "a"]

    def test_r_index(self):
        assert CHANNELS.index("r") == 0

    def test_a_index(self):
        assert CHANNELS.index("a") == 3

    def test_marker_params_indices(self):
        node = CopyChannelNode()
        node.parameters["src_channel"] = "r"
        node.parameters["dst_channel"] = "a"
        a = _make_tr(tex_id=1)
        b = _make_tr(tex_id=2)
        result = node._do_evaluate({"A": a, "B": b}, None, None)
        params = result["_effect_params"]
        assert params["src_channel"] == 0  # r
        assert params["dst_channel"] == 3  # a


class TestCopyChannelNodeSerialization:
    def test_roundtrip(self):
        node = CopyChannelNode()
        node.set_parameter("src_channel", "r")
        node.set_parameter("dst_channel", "a")
        d = node.to_dict()
        restored = CopyChannelNode.from_dict(d)
        assert restored.parameters["src_channel"] == "r"
        assert restored.parameters["dst_channel"] == "a"


# ── CSTransformNode ──

class TestCSTransformNodeStructure:
    def test_type(self):
        assert CSTransformNode.node_type == "cs_transform"

    def test_inputs(self):
        assert [p.name for p in CSTransformNode().inputs] == ["image", "mask"]  # CP-044 通用 mask

    def test_outputs(self):
        assert [p.name for p in CSTransformNode().outputs] == ["image"]

    def test_defaults(self):
        node = CSTransformNode()
        assert node.parameters["direction"] == "srgb_to_linear"

    def test_modes(self):
        assert CS_TRANSFORM_MODES == ["srgb_to_linear", "linear_to_srgb"]


class TestCSTransformNodeNoop:
    def test_srgb_to_linear_already_linear(self):
        node = CSTransformNode()
        inp = _make_tr(color_space=ColorSpace.LINEAR)
        result = node._do_evaluate({"image": inp}, None, None)
        assert result["image"] is inp

    def test_linear_to_srgb_already_srgb(self):
        node = CSTransformNode()
        node.parameters["direction"] = "linear_to_srgb"
        inp = _make_tr(color_space=ColorSpace.SRGB)
        result = node._do_evaluate({"image": inp}, None, None)
        assert result["image"] is inp

    def test_srgb_to_linear_needs_transform(self):
        node = CSTransformNode()
        inp = _make_tr(color_space=ColorSpace.SRGB)
        result = node._do_evaluate({"image": inp}, None, None)
        assert result["_effect_type"] == "cs_transform"
        assert result["_effect_params"]["direction"] == 0
        assert result["_effect_params"]["target_color_space"] == ColorSpace.LINEAR

    def test_linear_to_srgb_needs_transform(self):
        node = CSTransformNode()
        node.parameters["direction"] = "linear_to_srgb"
        inp = _make_tr(color_space=ColorSpace.LINEAR)
        result = node._do_evaluate({"image": inp}, None, None)
        assert result["_effect_type"] == "cs_transform"
        assert result["_effect_params"]["direction"] == 1
        assert result["_effect_params"]["target_color_space"] == ColorSpace.SRGB

    def test_unknown_color_space_triggers_transform(self):
        node = CSTransformNode()
        inp = _make_tr(color_space=ColorSpace.UNKNOWN)
        result = node._do_evaluate({"image": inp}, None, None)
        assert result["_effect_type"] == "cs_transform"

    def test_no_input_empty(self):
        node = CSTransformNode()
        assert node._do_evaluate({}, None, None) == {}


class TestCSTransformNodeSerialization:
    def test_roundtrip(self):
        node = CSTransformNode()
        node.set_parameter("direction", "linear_to_srgb")
        d = node.to_dict()
        restored = CSTransformNode.from_dict(d)
        assert restored.parameters["direction"] == "linear_to_srgb"


# ── ColorSpace utilities ──

class TestColorSpaceConstants:
    def test_linear(self):
        assert ColorSpace.LINEAR == "linear"

    def test_srgb(self):
        assert ColorSpace.SRGB == "srgb"

    def test_display(self):
        assert ColorSpace.DISPLAY == "display"

    def test_unknown(self):
        assert ColorSpace.UNKNOWN == "unknown"

    def test_all(self):
        assert ColorSpace.ALL == ("linear", "srgb", "display", "unknown")


class TestSRGBConversion:
    def test_srgb_to_linear_zero(self):
        assert srgb_to_linear_value(0.0) == 0.0

    def test_srgb_to_linear_one(self):
        assert abs(srgb_to_linear_value(1.0) - 1.0) < 1e-6

    def test_linear_to_srgb_zero(self):
        assert linear_to_srgb_value(0.0) == 0.0

    def test_linear_to_srgb_one(self):
        assert abs(linear_to_srgb_value(1.0) - 1.0) < 1e-6

    def test_roundtrip(self):
        """srgb_to_linear → linear_to_srgb should round-trip."""
        for v in [0.0, 0.18, 0.5, 0.9, 1.0]:
            linear = srgb_to_linear_value(v)
            back = linear_to_srgb_value(linear)
            assert abs(back - v) < 1e-6, f"Round-trip failed for {v}"

    def test_srgb_to_linear_is_monotonic(self):
        prev = -1.0
        for i in range(101):
            v = i / 100.0
            linear = srgb_to_linear_value(v)
            assert linear > prev
            prev = linear
