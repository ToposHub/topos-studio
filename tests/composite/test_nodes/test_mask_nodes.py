"""
Mask Node Tests - Tests for mask/matte nodes.

Tests cover:
- MaskNode: apply mask to image
- MaskMergeNode: merge masks
- RotoShapeNode: roto/shape mask
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from tests.node_test_base import (
    NodeTestBase,
    make_texture_result,
)
from src.features.composite.nodes.mask_node import MaskNode
from src.features.composite.nodes.mask_merge_node import MaskMergeNode
from src.features.composite.nodes.roto_shape_node import RotoShapeNode

import pytest
pytestmark = pytest.mark.composite_core


# ── Mask Node Tests ─────────────────────────────────────────────────────────

class TestMaskNode(NodeTestBase):
    """Tests for MaskNode - geometric mask generator.

    MaskNode can be a standalone geometric generator or an image-masking
    effect.  ``result`` is the visible workflow output and ``matte`` is the
    explicit mask output used by Merge/Grade nodes.
    """

    node_type = "mask"
    node_class = MaskNode

    def test_optional_image_input(self):
        """MaskNode accepts an optional image for inline masking."""
        node = self.create_node()
        assert [port.name for port in node.inputs] == ["image", "mask"]  # CP-044 通用 mask 端口

    def test_matte_output(self):
        """MaskNode exposes both visible result and explicit matte outputs."""
        node = self.create_node()
        assert [port.name for port in node.outputs] == ["result", "matte"]
        assert all(port.direction == "output" for port in node.outputs)

    def test_mask_type_parameter(self):
        """Mask type parameter supports rectangle/ellipse/polygon."""
        node = self.create_node()
        assert "mask_type" in node.parameters
        # Test all valid mask types
        for mask_type in ["rectangle", "ellipse", "polygon"]:
            node.set_parameter("mask_type", mask_type)
            assert node.parameters.get("mask_type") == mask_type

    def test_position_parameters(self):
        """Center position parameters (normalized 0-1)."""
        node = self.create_node()
        assert "center_x" in node.parameters
        assert "center_y" in node.parameters
        node.set_parameter("center_x", 0.3)
        node.set_parameter("center_y", 0.7)
        assert node.parameters.get("center_x") == 0.3
        assert node.parameters.get("center_y") == 0.7

    def test_size_parameters(self):
        """Width/height parameters (normalized 0-2, can extend beyond frame)."""
        node = self.create_node()
        assert "width" in node.parameters
        assert "height" in node.parameters
        node.set_parameter("width", 0.8)
        node.set_parameter("height", 1.2)
        assert node.parameters.get("width") == 0.8
        assert node.parameters.get("height") == 1.2

    def test_rotation_parameter(self):
        """Rotation parameter (degrees, -180 to 180)."""
        node = self.create_node()
        assert "rotation" in node.parameters
        node.set_parameter("rotation", 45.0)
        assert node.parameters.get("rotation") == 45.0

    def test_feather_parameter(self):
        """Feather parameter for edge softness (0-0.5)."""
        node = self.create_node()
        assert "feather" in node.parameters
        node.set_parameter("feather", 0.1)
        assert node.parameters.get("feather") == 0.1

    def test_invert_parameter(self):
        """Invert mask parameter."""
        node = self.create_node()
        assert "invert" in node.parameters
        node.set_parameter("invert", True)
        assert node.parameters.get("invert") == True

    def test_output_mode_parameter(self):
        """Output mode: matte (alpha only) or result (white matte)."""
        node = self.create_node()
        assert "output_mode" in node.parameters
        for mode in ["matte", "result"]:
            node.set_parameter("output_mode", mode)
            assert node.parameters.get("output_mode") == mode

    def test_color_page_circle_alias_uses_ellipse_shader_path(self):
        """The Color page's ``circle`` name is accepted by Composite Mask."""
        node = self.create_node(params={"mask_type": "circle"})
        marker = node._do_evaluate({}, None, None)
        assert marker["_effect_params"]["mask_type"] == "ellipse"

    def test_polygon_sides_parameter(self):
        """Polygon sides parameter (3-32 sides)."""
        node = self.create_node()
        assert "sides" in node.parameters
        node.set_parameter("sides", 6)
        assert node.parameters.get("sides") == 6

    def test_output_dimensions_parameters(self):
        """Output width/height parameters for standalone use."""
        node = self.create_node()
        assert "output_width" in node.parameters
        assert "output_height" in node.parameters
        node.set_parameter("output_width", 3840)
        node.set_parameter("output_height", 2160)
        assert node.parameters.get("output_width") == 3840
        assert node.parameters.get("output_height") == 2160

    def test_evaluate_no_inputs_required(self):
        """MaskNode evaluates without any inputs (generator mode)."""
        node = self.create_node()
        # Generator takes no inputs
        outputs = node.evaluate(
            {},  # No inputs needed
            self.get_mock_context(),
            self.get_mock_fb_manager(),
            self.get_mock_resource_pool()
        )
        # Should return matte output with effect marker
        assert "matte" in outputs
        assert outputs["matte"].texture_id == -1  # Marker texture
        assert outputs["matte"].width > 0
        assert outputs["matte"].height > 0
        # Should have effect metadata for GPU renderer
        assert "_effect_type" in outputs
        assert outputs["_effect_type"] == "mask"
        assert "_effect_params" in outputs


# ── Mask Merge Node Tests ───────────────────────────────────────────────────

class TestMaskMergeNode(NodeTestBase):
    """Tests for MaskMergeNode - merge masks."""

    node_type = "mask_merge"
    node_class = MaskMergeNode

    def test_two_mask_inputs(self):
        """MaskMerge 需要两个 mask 输入."""
        node = self.create_node()
        # Should have at least 2 input ports
        assert len(node.inputs) >= 2, "MaskMerge should have multiple inputs"

    def test_merge_operation(self):
        """Merge operation 参数."""
        node = self.create_node()
        if "operation" in node.parameters:
            for op in ["add", "subtract", "intersect", "replace"]:
                try:
                    test_node = self.create_node(params={"operation": op})
                except:
                    pass

    def test_mask_merge_output(self):
        """Mask merge output 正确."""
        node = self.create_node()
        inputs = {
            "A": make_texture_result(texture_id=1),
            "B": make_texture_result(texture_id=2),
        }
        outputs = node.evaluate(
            inputs, self.get_mock_context(),
            self.get_mock_fb_manager(), self.get_mock_resource_pool()
        )
        # Should not crash

    def test_mask_merge_is_renderable(self):
        """Boolean mask merge is an implemented GPU effect, not a placeholder."""
        node = self.create_node()
        assert node.IS_PLACEHOLDER is False
        result = node.evaluate(
            {"A": make_texture_result(texture_id=1),
             "B": make_texture_result(texture_id=2)},
            self.get_mock_context(), self.get_mock_fb_manager(),
            self.get_mock_resource_pool())
        assert result["_effect_type"] == "mask_merge"
        assert {port.name for port in node.outputs} == {"result", "matte"}


# ── Roto Shape Node Tests ───────────────────────────────────────────────────

class TestRotoShapeNode(NodeTestBase):
    """Tests for RotoShapeNode - roto/shape mask."""

    node_type = "roto_shape"
    node_class = RotoShapeNode

    def test_shape_type_parameter(self):
        """Shape type 参数."""
        node = self.create_node()
        if "shape_type" in node.parameters:
            for shape in ["rectangle", "ellipse", "polygon", "bezier"]:
                try:
                    test_node = self.create_node(params={"shape_type": shape})
                except:
                    pass

    def test_shape_points_parameter(self):
        """Shape points 参数."""
        node = self.create_node()
        # Check points param (for polygon/bezier)
        if "points" in node.parameters:
            # Points would be a list of coordinates
            pass

    def test_bezier_node_contract(self):
        """Roto is a dual-mode node with a real Bezier path contract."""
        node = self.create_node()
        assert node.IS_PLACEHOLDER is False
        assert [port.name for port in node.inputs] == ["image", "mask"]  # CP-044 通用 mask 端口
        assert [port.name for port in node.outputs] == ["result", "matte"]
        assert len(node.parse_points(node.parameters["points"])) >= 3
        outputs = node.evaluate(
            {}, self.get_mock_context(), self.get_mock_fb_manager(),
            self.get_mock_resource_pool())
        assert outputs["_effect_type"] == "roto"

    def test_point_channels_are_animatable_and_drive_path(self):
        """Each point/tangent channel is a real animated scalar property."""
        node = self.create_node()
        names = node._point_param_names(0)
        assert names["x"] in node.parameters
        original = node.parse_points(node.parameters["points"])[0]["pos"][0]
        node.set_parameter(names["x"], original + 0.1)
        points = node.parse_points(node.parameters["points"])
        assert points[0]["pos"][0] == pytest.approx(original + 0.1)
        overridden = node.apply_point_parameter_overrides(
            node.parse_points(node.parameters["points"]),
            {names["x"]: 0.9})
        assert overridden[0]["pos"][0] == pytest.approx(0.9)

    def test_point_feather_is_animatable_and_keeps_legacy_fallback(self):
        """Point feather overrides are explicit; old points inherit global feather."""
        node = self.create_node(params={"feather": 0.2})
        feather_name = node._point_param_names(0)["feather"]
        assert feather_name in node.get_param_defs()
        assert node.get_param_defs()[feather_name].animated is True

        points = node.parse_points(node.parameters["points"])
        assert "feather" not in points[0]
        overridden = node.apply_point_parameter_overrides(
            points, {feather_name: 0.05})
        assert overridden[0]["feather"] == pytest.approx(0.05)

        node.set_parameter(feather_name, 0.05)
        stored = node.parse_points(node.parameters["points"])
        assert stored[0]["feather"] == pytest.approx(0.05)

    def test_feather_parameter(self):
        """Feather 参数."""
        node = self.create_node()
        if "feather" in node.parameters:
            node.set_parameter("feather", 5.0)
            assert node.parameters.get("feather") == 5.0

    def test_no_input_expected(self):
        """RotoShape 不需要上游输入."""
        node = self.create_node()
        # RotoShape generates mask, not modifies input
        # Should have minimal or no inputs


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
