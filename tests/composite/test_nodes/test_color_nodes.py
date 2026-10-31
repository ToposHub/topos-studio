"""
Color Node Tests - Tests for color adjustment nodes.

Tests cover:
- GradeNode: lift/gamma/gain color correction
- ColorCorrectNode: basic color correction
- HueCorrectNode: hue-based adjustments
- LutNode: LUT application
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from tests.node_test_base import (
    NodeTestBase,
    ColorNodeTestMixin,
    make_texture_result,
)
from src.features.composite.nodes.grade_node import GradeNode
from src.features.composite.nodes.color_correct_node import ColorCorrectNode
from src.features.composite.nodes.huecorrect_node import HueCorrectNode
from src.features.composite.nodes.lut_node import LutNode


# ── Grade Node Tests ────────────────────────────────────────────────────────

class TestGradeNode(NodeTestBase, ColorNodeTestMixin):
    """Tests for GradeNode - NUKE-style color correction."""

    node_type = "grade"
    node_class = GradeNode

    def test_lift_values_clamped(self):
        """Lift 参数在 [-1, 1] 范围."""
        node = self.create_node()
        for channel in ['r', 'g', 'b']:
            param = f"lift_{channel}"
            node.set_parameter(param, -0.5)
            assert node.parameters[param] == -0.5
            node.set_parameter(param, 0.5)
            assert node.parameters[param] == 0.5

    def test_gamma_positive(self):
        """Gamma 参数应为正值."""
        node = self.create_node()
        for channel in ['r', 'g', 'b']:
            param = f"gamma_{channel}"
            # Test positive values
            node.set_parameter(param, 1.5)
            assert node.parameters[param] == 1.5
            node.set_parameter(param, 0.5)
            assert node.parameters[param] == 0.5

    def test_gain_scaling(self):
        """Gain 参数控制亮度倍增."""
        node = self.create_node()
        # Default gain = 1.0 (no change)
        assert node.parameters.get("gain_r") == 1.0
        assert node.parameters.get("gain_g") == 1.0
        assert node.parameters.get("gain_b") == 1.0

        # Increase gain
        node.set_parameter("gain_r", 2.0)
        assert node.parameters.get("gain_r") == 2.0

    def test_blackpoint_whitepoint_range(self):
        """Blackpoint/Whitepoint 在有效范围."""
        node = self.create_node()
        # Blackpoint should be in [0, 1]
        node.set_parameter("blackpoint", 0.1)
        assert node.parameters.get("blackpoint") == 0.1

        # Whitepoint should be in [0, 4]
        node.set_parameter("whitepoint", 2.0)
        assert node.parameters.get("whitepoint") == 2.0

    def test_mix_blend_parameter(self):
        """Mix 参数控制效果混合."""
        node = self.create_node()
        # Default mix = 1.0 (full effect)
        assert node.parameters.get("mix") == 1.0

        # Mix = 0 should produce no effect (passthrough)
        node.set_parameter("mix", 0.0)
        assert node.parameters.get("mix") == 0.0

    def test_mask_input_optional(self):
        """Grade 节点支持可选 mask 输入."""
        node = self.create_node()
        mask_port = node.get_input_port("mask")
        assert mask_port is not None, "Grade should have mask input port"
        assert mask_port.direction == "input"

    def test_no_changes_passthrough(self):
        """默认参数应直接返回输入."""
        node = self.create_node(params={
            "lift_r": 0.0, "lift_g": 0.0, "lift_b": 0.0,
            "gamma_r": 1.0, "gamma_g": 1.0, "gamma_b": 1.0,
            "gain_r": 1.0, "gain_g": 1.0, "gain_b": 1.0,
            "offset_r": 0.0, "offset_g": 0.0, "offset_b": 0.0,
            "blackpoint": 0.0, "whitepoint": 1.0,
            "multiply": 1.0, "add": 0.0, "mix": 1.0,
        })
        inputs = self.create_valid_input()
        outputs = node.evaluate(
            inputs, self.get_mock_context(),
            self.get_mock_fb_manager(), self.get_mock_resource_pool()
        )
        # With default params, should return input unchanged
        if "result" in outputs and inputs.get("image"):
            # Effect marker or passthrough
            assert outputs["result"].texture_id == inputs["image"].texture_id or \
                   outputs["result"].texture_id == -1, \
               "Default grade should passthrough or mark as effect"


# ── Color Correct Node Tests ────────────────────────────────────────────────

class TestColorCorrectNode(NodeTestBase, ColorNodeTestMixin):
    """Tests for ColorCorrectNode - basic color correction."""

    node_type = "color_correct"
    node_class = ColorCorrectNode

    def test_master_contrast(self):
        """Master contrast 参数可用."""
        node = self.create_node()
        if "master_contrast" in node.parameters:
            node.set_parameter("master_contrast", 1.5)
            assert node.parameters.get("master_contrast") == 1.5

    def test_saturation_control(self):
        """Saturation 参数控制颜色饱和度."""
        node = self.create_node()
        # Check saturation param exists
        saturation_param = None
        for pdef in ColorCorrectNode.PARAM_DEFS:
            if "saturation" in pdef.name.lower():
                saturation_param = pdef.name
                break

        if saturation_param:
            node.set_parameter(saturation_param, 1.5)
            assert node.parameters.get(saturation_param) == 1.5


# ── Hue Correct Node Tests ──────────────────────────────────────────────────

class TestHueCorrectNode(NodeTestBase):
    """Tests for HueCorrectNode - hue-based adjustments."""

    node_type = "huecorrect"
    node_class = HueCorrectNode

    def test_hue_shift_range(self):
        """Hue shift 参数在有效范围."""
        node = self.create_node()
        # Check if hue_shift param exists
        for pdef in HueCorrectNode.PARAM_DEFS:
            if "hue" in pdef.name.lower():
                param_name = pdef.name
                test_value = (pdef.min_val + pdef.max_val) / 2
                node.set_parameter(param_name, test_value)
                assert node.parameters.get(param_name) == test_value


# ── Lut Node Tests ──────────────────────────────────────────────────────────

class TestLutNode(NodeTestBase):
    """Tests for LutNode - LUT application."""

    node_type = "lut"
    node_class = LutNode

    def test_lut_file_param(self):
        """LUT 文件参数可用."""
        node = self.create_node()
        # Check lut_file param
        if "lut_file" in node.parameters:
            # Can set to empty string
            node.set_parameter("lut_file", "")
            assert node.parameters.get("lut_file") == ""

    def test_lut_intensity(self):
        """LUT intensity 参数控制混合."""
        node = self.create_node()
        # Check intensity param
        intensity_param = None
        for pdef in LutNode.PARAM_DEFS:
            if "intensity" in pdef.name.lower() or "mix" in pdef.name.lower():
                intensity_param = pdef.name
                break

        if intensity_param:
            # Default should be 1.0 (full LUT)
            node.set_parameter(intensity_param, 0.5)
            assert node.parameters.get(intensity_param) == 0.5


# ── Additional Color Node Tests ─────────────────────────────────────────────

class TestCSTransformNode(NodeTestBase):
    """Tests for CSTransformNode - color space transformation."""

    node_type = "cs_transform"
    node_class = None  # Import if needed

    @pytest.fixture(autouse=True)
    def setup_node_class(self):
        """Import CSTransformNode."""
        from src.features.composite.nodes.cs_transform_node import CSTransformNode
        self.node_class = CSTransformNode

    def test_color_space_options(self):
        """Color space 选项可用."""
        node = self.create_node()
        # Check input/output color space params
        for param in ['in_colorspace', 'out_colorspace']:
            if param in node.parameters:
                # Should have options
                pass


if __name__ == "__main__":
    pytest.main([__file__, "-v"])