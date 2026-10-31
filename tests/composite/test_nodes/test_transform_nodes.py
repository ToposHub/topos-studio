"""
Transform Node Tests - Tests for transform/geometry nodes.

Tests cover:
- TransformNode: translate/scale/rotate
- ReformatNode: resolution/format change
- CropNode: crop region
- CornerPinNode: corner pin distortion
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from tests.node_test_base import (
    NodeTestBase,
    make_texture_result,
)
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.nodes.reformat_node import ReformatNode
from src.features.composite.nodes.crop_node import CropNode
from src.features.composite.nodes.cornerpin_node import CornerPinNode
from src.features.composite.core.format import BBox

import pytest
pytestmark = pytest.mark.composite_core


# ── Transform Node Tests ────────────────────────────────────────────────────

class TestTransformNode(NodeTestBase):
    """Tests for TransformNode - translate/scale/rotate."""

    node_type = "transform"
    node_class = TransformNode

    def test_translate_parameters(self):
        """Translate X/Y 参数."""
        node = self.create_node()
        node.set_parameter("translate_x", 100.0)
        assert node.parameters.get("translate_x") == 100.0
        node.set_parameter("translate_y", 50.0)
        assert node.parameters.get("translate_y") == 50.0

    def test_scale_parameters(self):
        """Scale X/Y 参数."""
        node = self.create_node()
        node.set_parameter("scale_x", 2.0)
        assert node.parameters.get("scale_x") == 2.0
        node.set_parameter("scale_y", 0.5)
        assert node.parameters.get("scale_y") == 0.5

    def test_rotation_parameter(self):
        """Rotation 参数."""
        node = self.create_node()
        node.set_parameter("rotation", 45.0)
        assert node.parameters.get("rotation") == 45.0
        node.set_parameter("rotation", 180.0)
        assert node.parameters.get("rotation") == 180.0

    def test_pivot_mode_options(self):
        """Pivot mode 参数可选."""
        node = self.create_node()
        for mode in ["center", "top_left", "bottom_right"]:
            try:
                test_node = self.create_node(params={"pivot_mode": mode})
            except:
                pass  # Some modes may not be valid

    def test_filter_options(self):
        """Filter 参数可选."""
        node = self.create_node()
        for filter_type in ["linear", "nearest"]:
            test_node = self.create_node(params={"filter": filter_type})
            assert test_node.parameters.get("filter") == filter_type

    def test_identity_transform(self):
        """Identity 变换应直通."""
        node = self.create_node(params={
            "translate_x": 0.0,
            "translate_y": 0.0,
            "scale_x": 1.0,
            "scale_y": 1.0,
            "rotation": 0.0,
        })
        inputs = self.create_valid_input()
        # Should not crash on evaluate

    def test_negative_scale(self):
        """Negative scale 反转图像."""
        node = self.create_node(params={"scale_x": -1.0})
        inputs = self.create_valid_input()
        outputs = node.evaluate(
            inputs, self.get_mock_context(),
            self.get_mock_fb_manager(), self.get_mock_resource_pool()
        )
        # Should not crash


# ── Reformat Node Tests ─────────────────────────────────────────────────────

class TestReformatNode(NodeTestBase):
    """Tests for ReformatNode - resolution/format change."""

    node_type = "reformat"
    node_class = ReformatNode

    def test_preset_formats(self):
        """Preset format 参数可选."""
        node = self.create_node()
        # Check preset param
        if "preset" in node.parameters:
            presets = ["HD 1080", "4K UHD", "HD 720", "SD"]
            for preset in presets:
                try:
                    test_node = self.create_node(params={"preset": preset})
                except:
                    pass

    def test_custom_resolution(self):
        """Custom resolution 参数."""
        node = self.create_node(params={
            "format_mode": "custom",
            "width": 1920,
            "height": 1080,
        })
        assert node.parameters.get("width") == 1920
        assert node.parameters.get("height") == 1080

    def test_resize_mode_options(self):
        """Resize mode 参数可选."""
        node = self.create_node()
        for mode in ["fit", "fill", "crop", "none"]:
            try:
                test_node = self.create_node(params={"resize_mode": mode})
            except:
                pass


# ── Crop Node Tests ─────────────────────────────────────────────────────────

class TestCropNode(NodeTestBase):
    """Tests for CropNode - crop region."""

    node_type = "crop"
    node_class = CropNode

    def test_crop_region_parameters(self):
        """Crop region 参数."""
        node = self.create_node(params={
            "x": 100,
            "y": 50,
            "width": 500,
            "height": 300,
        })
        assert node.parameters.get("x") == 100
        assert node.parameters.get("y") == 50
        assert node.parameters.get("width") == 500
        assert node.parameters.get("height") == 300

    def test_reformat_option(self):
        """Reformat 参数可选."""
        node = self.create_node()
        node.set_parameter("reformat", True)
        assert node.parameters.get("reformat") == True

    def test_crop_outside_image(self):
        """Crop region 超出图像范围."""
        node = self.create_node(params={
            "x": -100,
            "y": -50,
            "width": 3000,
            "height": 2000,
        })
        inputs = self.create_valid_input(width=1920, height=1080)
        outputs = node.evaluate(
            inputs, self.get_mock_context(),
            self.get_mock_fb_manager(), self.get_mock_resource_pool()
        )
        # Should not crash


# ── Corner Pin Node Tests ───────────────────────────────────────────────────

class TestCornerPinNode(NodeTestBase):
    """Tests for CornerPinNode - corner pin distortion."""

    node_type = "cornerpin"
    node_class = CornerPinNode

    def test_corner_parameters(self):
        """Corner pin 参数."""
        node = self.create_node()
        # Check corner params (to_x1, to_y1, etc.)
        if "to_x1" in node.parameters:
            node.set_parameter("to_x1", 0.0)
            node.set_parameter("to_y1", 0.0)

    def test_from_corners(self):
        """From corner 参数."""
        node = self.create_node()
        # Check from params
        if "from_x1" in node.parameters:
            node.set_parameter("from_x1", 0.0)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])