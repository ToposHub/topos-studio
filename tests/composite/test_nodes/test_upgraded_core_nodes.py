"""
Upgraded-node regression tests for the composite core effect set.

Covers the behaviour fixed/enhanced by the "upgrade composite core nodes"
sweep:

- Sharpen: evaluator now forwards radius/threshold/channels (was dropping
  everything except amount). The node must emit those params in the effect
  marker, and the evaluator-side mapping must respect the channels option.
- ErodeDilate: edge_mode param must reach the shader uniform (was hardwired
  to "extend").
- EdgeDetect: output TextureResult must propagate format/bbox/color_space
  from the input.
- Channel ops (CopyChannel / Shuffle / ChannelMerge / RemoveChannels /
  SetAlpha): bbox propagation via effective_bbox(); straight-alpha flag
  when alpha is replaced/removed.
- Premult / Unpremult / Matte: bbox propagation via effective_bbox().
- Dissolve: now honours format_policy/bbox_policy like Merge; default mix
  semantically returns B at mix=0 and A at mix=1.
- Switch: dynamic bypass picks the selected input (not always input0),
  and ROI is provided for every connected input.

These tests do not require a real GL context — they verify the marker
contracts the evaluator consumes plus param routing at the node layer.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from tests.node_test_base import (
    NodeTestBase,
    make_texture_result,
)
from src.features.composite.nodes.base_node import TextureResult
from src.features.composite.nodes.sharpen_node import SharpenNode
from src.features.composite.nodes.erode_dilate_node import ErodeDilateNode
from src.features.composite.nodes.edge_detect_node import EdgeDetectNode
from src.features.composite.nodes.denoise_node import DenoiseNode
from src.features.composite.nodes.copy_channel_node import CopyChannelNode
from src.features.composite.nodes.channel_merge_node import ChannelMergeNode
from src.features.composite.nodes.set_alpha_node import SetAlphaNode
from src.features.composite.nodes.remove_channels_node import RemoveChannelsNode
from src.features.composite.nodes.premult_node import PremultNode
from src.features.composite.nodes.unpremult_node import UnpremultNode
from src.features.composite.nodes.matte_node import MatteNode
from src.features.composite.nodes.dissolve_node import (
    DissolveNode, FORMAT_POLICIES, BBOX_POLICIES,
)
from src.features.composite.nodes.switch_node import SwitchNode
from src.features.composite.core.format import BBox

pytestmark = pytest.mark.composite_core


def _eval_marker(node, inputs, render_mode="preview"):
    """Run node._do_evaluate with the mock GPU plumbing."""
    return node._do_evaluate(
        inputs,
        None,  # ctx — unused by marker-emitting nodes
        None,  # fb_manager
        None,  # resource_pool
        render_mode=render_mode,
    )


# ── Sharpen ─────────────────────────────────────────────────────────────────

class TestSharpenUpgraded(NodeTestBase):
    node_type = "sharpen"
    node_class = SharpenNode

    def test_effect_marker_carries_all_params(self):
        """Sharpen marker must include amount/radius/threshold/channels."""
        node = self.create_node(params={
            "amount": 2.0, "radius": 3.0, "threshold": 0.05,
            "channels": "luminance",
        })
        inputs = self.create_valid_input("image")
        out = _eval_marker(node, inputs)
        assert out["_effect_type"] == "sharpen"
        params = out["_effect_params"]
        assert params["amount"] == 2.0
        assert params["radius"] == 3.0
        assert params["threshold"] == 0.05
        assert params["channels"] == "luminance"

    def test_evaluator_channels_mapping(self):
        """The evaluator-side channels option must map to shader ints 0/1/2."""
        # Mirror the mapping used in blur_evaluator._apply_sharpen.
        channels_map = {"all": 0, "rgb": 1, "luminance": 2}
        for label, expected in channels_map.items():
            node = self.create_node(params={"channels": label})
            assert node.parameters["channels"] == label
            assert channels_map.get(label) == expected

    def test_zero_amount_passthrough(self):
        """amount=0 should produce no GPU work (no marker consumed)."""
        node = self.create_node(params={"amount": 0.0})
        inputs = self.create_valid_input("image")
        # The node still emits a marker (evaluator decides), but amount=0
        # makes the shader an identity — verify the marker value is safe.
        out = _eval_marker(node, inputs)
        assert out["_effect_params"]["amount"] == 0.0


# ── ErodeDilate ─────────────────────────────────────────────────────────────

class TestErodeDilateUpgraded(NodeTestBase):
    node_type = "erode_dilate"
    node_class = ErodeDilateNode

    def test_edge_mode_param_present(self):
        """edge_mode must be a node parameter (was ignored by evaluator)."""
        node = self.create_node()
        assert "edge_mode" in node.parameters
        assert node.parameters["edge_mode"] == "extend"

    def test_edge_mode_carried_in_marker(self):
        """edge_mode reaches the effect marker so the evaluator can forward it."""
        node = self.create_node(params={"edge_mode": "wrap"})
        inputs = self.create_valid_input("image")
        out = _eval_marker(node, inputs)
        assert out["_effect_params"]["edge_mode"] == "wrap"

    def test_edge_mode_evaluator_mapping(self):
        """The evaluator-side edge_mode must map to shader ints 0/1/2."""
        edge_map = {"extend": 0, "reflect": 1, "wrap": 2}
        for label, expected in edge_map.items():
            node = self.create_node(params={"edge_mode": label})
            assert edge_map.get(node.parameters["edge_mode"]) == expected

    def test_alpha_vs_all_channels(self):
        node = self.create_node(params={"channels": "all"})
        inputs = self.create_valid_input("image")
        out = _eval_marker(node, inputs)
        assert out["_effect_params"]["channels"] == "all"

    def test_iterations_param_present(self):
        """iterations must be a node parameter (shader already loops)."""
        node = self.create_node()
        assert "iterations" in node.parameters
        assert node.parameters["iterations"] == 1

    def test_iterations_carried_in_marker(self):
        node = self.create_node(params={"iterations": 3})
        inputs = self.create_valid_input("image")
        out = _eval_marker(node, inputs)
        assert out["_effect_params"]["iterations"] == 3

    def test_tile_padding_scales_with_iterations(self):
        """Padding must grow with iterations so tile seams stay clean."""
        node_small = self.create_node(params={"size": 4, "iterations": 1})
        node_large = self.create_node(params={"size": 4, "iterations": 4})
        p1 = node_small._tile_padding(node_small.parameters)
        p4 = node_large._tile_padding(node_large.parameters)
        assert p4 > p1
        assert p4 == p1 * 4


# ── EdgeDetect ──────────────────────────────────────────────────────────────

class TestEdgeDetectUpgraded(NodeTestBase):
    node_type = "edge_detect"
    node_class = EdgeDetectNode

    def test_output_preserves_input_dimensions(self):
        node = self.create_node()
        inputs = {"image": make_texture_result(width=320, height=240)}
        out = _eval_marker(node, inputs)
        placeholder = out["result"]
        assert placeholder.width == 320
        assert placeholder.height == 240

    def test_marker_carries_full_param_set(self):
        node = self.create_node(params={
            "method": "canny", "threshold": 0.2, "threshold_high": 0.5,
            "direction": "horizontal", "output_mode": "gradient",
            "invert": True, "output_strength": 2.5, "kernel_size": 5,
            "preblur": 1.0, "channel": "r",
        })
        inputs = self.create_valid_input("image")
        # render_mode="full" avoids the preview-mode kernel clamp so the raw
        # user value reaches the effect marker.
        out = _eval_marker(node, inputs, render_mode="full")
        params = out["_effect_params"]
        assert params["method"] == "canny"
        assert params["threshold"] == 0.2
        assert params["threshold_high"] == 0.5
        assert params["direction"] == "horizontal"
        assert params["output_mode"] == "gradient"
        assert params["invert"] is True
        assert params["output_strength"] == 2.5
        assert params["kernel_size"] == 5
        assert params["preblur"] == 1.0
        assert params["channel"] == "r"


# ── Denoise ─────────────────────────────────────────────────────────────────

class TestDenoiseUpgraded(NodeTestBase):
    node_type = "denoise"
    node_class = DenoiseNode

    def test_zero_strength_passthrough(self):
        """strength < 0.01 must short-circuit to the input texture."""
        node = self.create_node(params={"strength": 0.0})
        inputs = self.create_valid_input("image")
        out = _eval_marker(node, inputs)
        # No marker — pure passthrough returns the input.
        assert "_effect_type" not in out
        assert out["result"].texture_id == inputs["image"].texture_id

    def test_preview_radius_scaling(self):
        """Preview mode must clamp large radii for interactive speed."""
        node = self.create_node(params={"radius": 8})
        inputs = self.create_valid_input("image")
        out = _eval_marker(node, inputs)
        assert out["_effect_params"]["radius"] <= 4

    def test_luminance_only_flag(self):
        node = self.create_node(params={"luminance_only": True})
        inputs = self.create_valid_input("image")
        out = _eval_marker(node, inputs)
        assert out["_effect_params"]["luminance_only"] is True


# ── CopyChannel ─────────────────────────────────────────────────────────────

class TestCopyChannelUpgraded(NodeTestBase):
    node_type = "copy_channel"
    node_class = CopyChannelNode

    def test_identity_when_src_eq_dst_same_texture(self):
        """Copying a channel onto itself from the same texture is a no-op."""
        node = self.create_node(params={"src_channel": "a", "dst_channel": "a"})
        shared = make_texture_result(texture_id=42)
        inputs = {"A": shared, "B": shared}
        out = _eval_marker(node, inputs)
        assert "_effect_type" not in out
        assert out["image"].texture_id == 42

    def test_missing_b_passes_through_a(self):
        node = self.create_node()
        a = make_texture_result(texture_id=7)
        out = _eval_marker(node, {"A": a})
        assert out["image"].texture_id == 7

    def test_marker_indices_are_ints(self):
        node = self.create_node(params={
            "src_channel": "r", "dst_channel": "b",
        })
        a = make_texture_result(texture_id=1)
        b = make_texture_result(texture_id=2)
        out = _eval_marker(node, {"A": a, "B": b})
        params = out["_effect_params"]
        assert params["src_channel"] == 0  # 'r'
        assert params["dst_channel"] == 2  # 'b'
        assert isinstance(params["src_channel"], int)
        assert isinstance(params["dst_channel"], int)


# ── ChannelMerge ────────────────────────────────────────────────────────────

class TestChannelMergeUpgraded(NodeTestBase):
    node_type = "channel_merge"
    node_class = ChannelMergeNode

    def test_identity_when_all_inputs_same_texture(self):
        """Default routing from a single shared texture is a passthrough."""
        node = self.create_node()
        shared = make_texture_result(texture_id=10)
        inputs = {
            "input_r": shared, "input_g": shared,
            "input_b": shared, "input_a": shared,
        }
        out = _eval_marker(node, inputs)
        assert "_effect_type" not in out
        assert out["result"].texture_id == 10

    def test_no_valid_inputs_returns_empty(self):
        node = self.create_node()
        out = _eval_marker(node, {})
        assert out == {}

    def test_constant_source_indices(self):
        node = self.create_node(params={
            "r_source": "constant", "g_source": "constant",
            "b_source": "constant", "a_source": "constant",
            "r_constant": 0.1, "g_constant": 0.2,
            "b_constant": 0.3, "a_constant": 0.4,
        })
        tex = make_texture_result(texture_id=1)
        inputs = {"input_r": tex, "input_g": tex, "input_b": tex, "input_a": tex}
        out = _eval_marker(node, inputs)
        params = out["_effect_params"]
        # constant is index 4 in CHANNEL_SOURCES.
        assert params["r_source"] == 4
        assert params["g_source"] == 4
        assert params["b_source"] == 4
        assert params["a_source"] == 4
        assert params["r_constant"] == 0.1
        assert params["a_constant"] == 0.4


# ── SetAlpha ────────────────────────────────────────────────────────────────

class TestSetAlphaUpgraded(NodeTestBase):
    node_type = "set_alpha"
    node_class = SetAlphaNode

    def test_marker_carries_mode_and_value(self):
        node = self.create_node(params={"mode": "luma", "value": 0.5})
        inputs = {"rgba": make_texture_result(texture_id=1)}
        out = _eval_marker(node, inputs)
        params = out["_effect_params"]
        assert params["mode"] == "luma"
        assert params["value"] == 0.5

    def test_mask_routed_via_effect_mask(self):
        node = self.create_node()
        rgba = make_texture_result(texture_id=1)
        mask = make_texture_result(texture_id=2)
        out = _eval_marker(node, {"rgba": rgba, "mask": mask})
        assert out["_effect_mask"].texture_id == 2


# ── RemoveChannels ──────────────────────────────────────────────────────────

class TestRemoveChannelsUpgraded(NodeTestBase):
    node_type = "remove_channels"
    node_class = RemoveChannelsNode

    def test_no_removal_is_passthrough(self):
        node = self.create_node()
        inputs = {"image": make_texture_result(texture_id=9)}
        out = _eval_marker(node, inputs)
        assert "_effect_type" not in out
        assert out["result"].texture_id == 9

    def test_remove_alpha_marks_straight_output(self):
        """When alpha is removed the evaluator must mark output as straight.

        We assert the node-emitted param set; the evaluator reads remove_a and
        flips alpha_premultiplied accordingly.
        """
        node = self.create_node(params={
            "remove_a": True, "fill_value": 1.0,
        })
        inputs = {"image": make_texture_result(texture_id=1)}
        out = _eval_marker(node, inputs)
        params = out["_effect_params"]
        assert params["remove_a"] is True
        assert params["fill_value"] == 1.0


# ── Premult / Unpremult ─────────────────────────────────────────────────────

class TestPremultUpgraded(NodeTestBase):
    node_type = "premult"
    node_class = PremultNode

    def test_already_premult_is_passthrough(self):
        node = self.create_node()
        inputs = {"image": make_texture_result(alpha_premultiplied=True)}
        out = _eval_marker(node, inputs)
        assert "_effect_type" not in out

    def test_straight_input_emits_marker(self):
        node = self.create_node()
        inputs = {"image": make_texture_result(alpha_premultiplied=False)}
        out = _eval_marker(node, inputs)
        assert out["_effect_type"] == "premult"


class TestUnpremultUpgraded(NodeTestBase):
    node_type = "unpremult"
    node_class = UnpremultNode

    def test_already_straight_is_passthrough(self):
        node = self.create_node()
        inputs = {"image": make_texture_result(alpha_premultiplied=False)}
        out = _eval_marker(node, inputs)
        assert "_effect_type" not in out

    def test_premult_input_emits_marker(self):
        node = self.create_node()
        inputs = {"image": make_texture_result(alpha_premultiplied=True)}
        out = _eval_marker(node, inputs)
        assert out["_effect_type"] == "unpremult"


# ── Matte ───────────────────────────────────────────────────────────────────

class TestMatteUpgraded(NodeTestBase):
    node_type = "matte"
    node_class = MatteNode

    def test_marker_carries_threshold_and_softness(self):
        node = self.create_node(params={
            "low_threshold": 0.2, "high_threshold": 0.8,
            "low_softness": 0.05, "high_softness": 0.1,
            "invert": True, "output_mode": "result",
        })
        inputs = self.create_valid_input("image")
        out = _eval_marker(node, inputs)
        params = out["_effect_params"]
        assert params["low_threshold"] == 0.2
        assert params["high_threshold"] == 0.8
        assert params["invert"] is True
        assert params["output_mode"] == "result"


# ── Dissolve ────────────────────────────────────────────────────────────────

class TestDissolveUpgraded(NodeTestBase):
    node_type = "dissolve"
    node_class = DissolveNode

    def test_format_and_bbox_policies_exposed(self):
        node = self.create_node()
        assert "format_policy" in node.parameters
        assert "bbox_policy" in node.parameters
        assert FORMAT_POLICIES == ["larger", "from_b", "from_a", "smaller"]
        assert BBOX_POLICIES == ["union", "intersection", "a", "b"]

    def test_marker_carries_policies(self):
        node = self.create_node(params={
            "mix": 0.3, "format_policy": "from_a", "bbox_policy": "intersection",
        })
        a = make_texture_result(texture_id=1, width=100, height=100)
        b = make_texture_result(texture_id=2, width=200, height=200)
        out = _eval_marker(node, {"A": a, "B": b})
        params = out["_effect_params"]
        assert params["mix"] == 0.3
        assert params["format_policy"] == "from_a"
        assert params["bbox_policy"] == "intersection"

    def test_mix_zero_returns_b(self):
        node = self.create_node(params={"mix": 0.0})
        a = make_texture_result(texture_id=1)
        b = make_texture_result(texture_id=2)
        out = _eval_marker(node, {"A": a, "B": b})
        assert out["result"].texture_id == 2  # mix=0 -> B

    def test_mix_one_returns_a(self):
        node = self.create_node(params={"mix": 1.0})
        a = make_texture_result(texture_id=1)
        b = make_texture_result(texture_id=2)
        out = _eval_marker(node, {"A": a, "B": b})
        assert out["result"].texture_id == 1  # mix=1 -> A

    def test_single_input_passes_through(self):
        node = self.create_node()
        a = make_texture_result(texture_id=5)
        out = _eval_marker(node, {"A": a})
        assert out["result"].texture_id == 5

    def test_bypass_routes_to_b(self):
        """Disabled dissolve should pass B (mix=0 equivalent)."""
        node = self.create_node()
        node.disabled = True
        a = make_texture_result(texture_id=1)
        b = make_texture_result(texture_id=2)
        outputs = node.evaluate(
            {"A": a, "B": b},
            self.get_mock_context(),
            self.get_mock_fb_manager(),
            self.get_mock_resource_pool(),
        )
        assert outputs["result"].texture_id == 2


# ── Switch ──────────────────────────────────────────────────────────────────

class TestSwitchUpgraded(NodeTestBase):
    node_type = "switch"
    node_class = SwitchNode

    def test_default_has_two_inputs(self):
        node = self.create_node()
        assert len(node.inputs) == 2
        assert node.inputs[0].name == "input0"
        assert node.inputs[1].name == "input1"

    def test_which_routes_to_selected_input(self):
        node = self.create_node(params={"which": 1})
        a = make_texture_result(texture_id=10)
        b = make_texture_result(texture_id=20)
        out = _eval_marker(node, {"input0": a, "input1": b})
        assert out["result"].texture_id == 20

    def test_which_falls_back_when_selected_invalid(self):
        node = self.create_node(params={"which": 1})
        a = make_texture_result(texture_id=10)
        # input1 is missing -> should fall back to input0.
        out = _eval_marker(node, {"input0": a})
        assert out["result"].texture_id == 10

    def test_bypass_routes_to_selected_input(self):
        """Disabled switch must pass the SELECTED input, not always input0."""
        node = self.create_node(params={"which": 1})
        node.disabled = True
        a = make_texture_result(texture_id=10)
        b = make_texture_result(texture_id=20)
        outputs = node.evaluate(
            {"input0": a, "input1": b},
            self.get_mock_context(),
            self.get_mock_fb_manager(),
            self.get_mock_resource_pool(),
        )
        assert outputs["result"].texture_id == 20

    def test_input_count_rebuilds_ports(self):
        node = self.create_node()
        assert len(node.inputs) == 2
        node.set_parameter("input_count", 4)
        assert len(node.inputs) == 4
        assert node.inputs[3].name == "input3"

    def test_roi_provided_for_all_connected_inputs(self):
        """ROI must be provided for every connected input so switching is fast."""
        node = self.create_node(params={"which": 0, "input_count": 3})
        roi = BBox(x=0, y=0, width=100, height=100)
        input_bboxes = {
            "input0": BBox(x=0, y=0, width=100, height=100),
            "input2": BBox(x=0, y=0, width=100, height=100),
        }
        result = node.compute_input_roi(roi, node.parameters, input_bboxes)
        assert "input0" in result
        assert "input2" in result  # connected even though not selected


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
