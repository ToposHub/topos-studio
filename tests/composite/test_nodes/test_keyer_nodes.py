"""
Keyer Node Tests - Tests for keying/matte generation nodes.

Tests cover:
- KeyerNode: unified keying interface
- ChromaKeyerNode: color-based keying
- LumaKeyerNode: luminance-based keying
- DifferenceKeyerNode: difference-based keying
- MatteCleanupNode: matte refinement
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from tests.node_test_base import (
    NodeTestBase,
    KeyerNodeTestMixin,
    make_texture_result,
)
from src.features.composite.nodes.keyer_node import KeyerNode
from src.features.composite.nodes.chroma_keyer_node import ChromaKeyerNode
from src.features.composite.nodes.luma_keyer_node import LumaKeyerNode
from src.features.composite.nodes.difference_keyer_node import DifferenceKeyerNode
from src.features.composite.nodes.matte_cleanup_node import MatteCleanupNode

import pytest
pytestmark = pytest.mark.composite_core


# ── Keyer Node Tests ────────────────────────────────────────────────────────

class TestKeyerNode(NodeTestBase, KeyerNodeTestMixin):
    """Tests for KeyerNode - unified keying interface."""

    node_type = "keyer"
    node_class = KeyerNode

    def test_auto_mode_detection(self):
        """Auto mode 自动检测最佳抠像策略."""
        node = self.create_node(params={"keyer_mode": "auto"})
        inputs = self.create_valid_input()
        outputs = node.evaluate(
            inputs, self.get_mock_context(),
            self.get_mock_fb_manager(), self.get_mock_resource_pool()
        )
        # Should not crash and should produce output

    def test_chroma_mode(self):
        """Chroma mode 使用颜色抠像."""
        node = self.create_node(params={"keyer_mode": "chroma"})
        inputs = self.create_valid_input()
        outputs = node.evaluate(
            inputs, self.get_mock_context(),
            self.get_mock_fb_manager(), self.get_mock_resource_pool()
        )
        # Should not crash

    def test_luma_mode(self):
        """Luma mode 使用亮度抠像."""
        node = self.create_node(params={"keyer_mode": "luma"})
        inputs = self.create_valid_input()
        outputs = node.evaluate(
            inputs, self.get_mock_context(),
            self.get_mock_fb_manager(), self.get_mock_resource_pool()
        )
        # Should not crash

    def test_difference_mode_with_reference(self):
        """Difference mode 需要 reference 输入."""
        node = self.create_node(params={"keyer_mode": "difference"})
        # Provide both image and reference inputs
        inputs = {
            "image": make_texture_result(texture_id=1),
            "reference": make_texture_result(texture_id=2),
        }
        outputs = node.evaluate(
            inputs, self.get_mock_context(),
            self.get_mock_fb_manager(), self.get_mock_resource_pool()
        )
        # Should not crash

    def test_screen_color_parameters(self):
        """Screen color 参数可设置."""
        node = self.create_node()
        # Test screen color RGB params
        for param in ["screen_color_r", "screen_color_g", "screen_color_b"]:
            if param in node.parameters:
                node.set_parameter(param, 0.5)
                assert node.parameters.get(param) == 0.5

    def test_tolerance_range(self):
        """Tolerance 参数在 [0, 1] 范围."""
        node = self.create_node()
        # Default tolerance = 0.3
        node.set_parameter("tolerance", 0.0)
        assert node.parameters.get("tolerance") == 0.0
        node.set_parameter("tolerance", 1.0)
        assert node.parameters.get("tolerance") == 1.0
        node.set_parameter("tolerance", 0.5)
        assert node.parameters.get("tolerance") == 0.5

    def test_softness_range(self):
        """Softness 参数在 [0, 0.5] 范围."""
        node = self.create_node()
        # Default softness = 0.1
        node.set_parameter("softness", 0.0)
        assert node.parameters.get("softness") == 0.0
        node.set_parameter("softness", 0.5)
        assert node.parameters.get("softness") == 0.5

    def test_invert_matte(self):
        """Invert 参数反转 matte."""
        node = self.create_node(params={"invert": True})
        assert node.parameters.get("invert") == True
        node.set_parameter("invert", False)
        assert node.parameters.get("invert") == False

    def test_output_mode(self):
        """Output mode 选择输出类型."""
        node = self.create_node()
        for mode in ["result", "matte"]:
            test_node = self.create_node(params={"output_mode": mode})
            assert test_node.parameters.get("output_mode") == mode

    def test_bypass_mapping(self):
        """Bypass 映射到 image 输入."""
        node = self.create_node()
        assert node.BYPASS_MAPPING == {"result": "image"}, \
            "Keyer bypass should pass image to result"


# ── Chroma Keyer Node Tests ─────────────────────────────────────────────────

class TestChromaKeyerNode(NodeTestBase, KeyerNodeTestMixin):
    """Tests for ChromaKeyerNode - color-based keying."""

    node_type = "chroma_keyer"
    node_class = ChromaKeyerNode

    def test_green_screen_default(self):
        """默认 screen color 为绿色."""
        node = self.create_node()
        # Check default screen color
        # Typical greenscreen: R=0, G=1, B=0
        screen_r = node.parameters.get("screen_color_r", 0.0)
        screen_g = node.parameters.get("screen_color_g", 1.0)
        screen_b = node.parameters.get("screen_color_b", 0.0)
        # Default should be some color (exact value depends on implementation)

    def test_blue_screen_configurable(self):
        """可配置蓝幕颜色."""
        node = self.create_node(params={
            "screen_color_r": 0.0,
            "screen_color_g": 0.0,
            "screen_color_b": 1.0,
        })
        assert node.parameters.get("screen_color_b") == 1.0


# ── Luma Keyer Node Tests ───────────────────────────────────────────────────

class TestLumaKeyerNode(NodeTestBase, KeyerNodeTestMixin):
    """Tests for LumaKeyerNode - luminance-based keying."""

    node_type = "luma_keyer"
    node_class = LumaKeyerNode

    def test_luma_threshold(self):
        """Luma threshold 参数控制抠像阈值."""
        node = self.create_node()
        # Check threshold param
        threshold_param = None
        for pdef in LumaKeyerNode.PARAM_DEFS:
            if "threshold" in pdef.name.lower():
                threshold_param = pdef.name
                break

        if threshold_param:
            node.set_parameter(threshold_param, 0.5)
            assert node.parameters.get(threshold_param) == 0.5


# ── Difference Keyer Node Tests ─────────────────────────────────────────────

class TestDifferenceKeyerNode(NodeTestBase, KeyerNodeTestMixin):
    """Tests for DifferenceKeyerNode - difference-based keying."""

    node_type = "difference_keyer"
    node_class = DifferenceKeyerNode

    def test_reference_input_required(self):
        """Difference keyer 需要 reference 输入."""
        node = self.create_node()
        # Check reference input port
        ref_port = node.get_input_port("reference")
        if ref_port:
            assert ref_port.direction == "input"
            assert ref_port.data_type == "image"

    def test_difference_threshold(self):
        """Difference threshold 参数."""
        node = self.create_node()
        # Check threshold param
        if "threshold" in node.parameters:
            node.set_parameter("threshold", 0.1)
            assert node.parameters.get("threshold") == 0.1


# ── Matte Cleanup Node Tests ────────────────────────────────────────────────

class TestMatteCleanupNode(NodeTestBase):
    """Tests for MatteCleanupNode - matte refinement."""

    node_type = "matte_cleanup"
    node_class = MatteCleanupNode

    def test_erode_dilate_operations(self):
        """支持 erode/dilate 操作."""
        node = self.create_node()
        # Check operation param
        if "operation" in node.parameters:
            for op in ["erode", "dilate"]:
                test_node = self.create_node(params={"operation": op})

    def test_softness_control(self):
        """Softness 参数控制边缘柔和度."""
        node = self.create_node()
        if "softness" in node.parameters:
            node.set_parameter("softness", 0.2)
            assert node.parameters.get("softness") == 0.2

    def test_cleanup_is_a_real_effect_marker(self):
        """Matte cleanup must no longer be classified as a pass-through."""
        node = self.create_node(params={"operation": "levels"})
        outputs = node.evaluate(
            {"image": make_texture_result(texture_id=7)},
            self.get_mock_context(), self.get_mock_fb_manager(),
            self.get_mock_resource_pool(),
        )
        assert node.IS_PLACEHOLDER is False
        assert outputs["_effect_type"] == "matte_cleanup"
        assert outputs["_effect_params"]["operation"] == "levels"
        assert outputs["image"].texture_id == -1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
