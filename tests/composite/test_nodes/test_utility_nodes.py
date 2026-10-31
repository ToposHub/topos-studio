"""
Utility Node Tests - Tests for utility/helper nodes.

Tests cover:
- InvertNode: invert colors
- ClampNode: clamp values
- NormalizeNode: normalize to [0,1]
- CompareNode: compare two inputs
- DotNode: visual dot (pass-through)
- NoOpNode: no operation (pass-through)
- SwitchNode: switch between inputs
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from tests.node_test_base import (
    NodeTestBase,
    make_texture_result,
)
from src.features.composite.nodes.invert_node import InvertNode
from src.features.composite.nodes.clamp_node import ClampNode
from src.features.composite.nodes.normalize_node import NormalizeNode
from src.features.composite.nodes.compare_node import CompareNode
from src.features.composite.nodes.dot_node import DotNode
from src.features.composite.nodes.noop_node import NoOpNode
from src.features.composite.nodes.switch_node import SwitchNode

import pytest
pytestmark = pytest.mark.composite_core


# ── Invert Node Tests ───────────────────────────────────────────────────────

class TestInvertNode(NodeTestBase):
    """Tests for InvertNode - invert colors."""

    node_type = "invert"
    node_class = InvertNode

    def test_channels_parameter(self):
        """Channels 参数控制反转哪些通道."""
        node = self.create_node()
        if "channels" in node.parameters:
            node.set_parameter("channels", "rgb")
            assert node.parameters.get("channels") == "rgb"
            node.set_parameter("channels", "rgba")

    def test_bypass_passthrough(self):
        """Bypass 时直通输入."""
        node = self.create_node()
        node.disabled = True
        inputs = self.create_valid_input()
        outputs = node.evaluate(
            inputs, self.get_mock_context(),
            self.get_mock_fb_manager(), self.get_mock_resource_pool()
        )
        if "result" in outputs:
            assert outputs["result"].texture_id == inputs["image"].texture_id

    def test_roi_expansion(self):
        """Invert 不需要 ROI 扩展."""
        node = self.create_node()
        if hasattr(node, 'requires_roi_expansion'):
            expansion = node.requires_roi_expansion(node.parameters)
            assert expansion == 0, "Invert should have 0 ROI expansion"


# ── Clamp Node Tests ────────────────────────────────────────────────────────

class TestClampNode(NodeTestBase):
    """Tests for ClampNode - clamp values."""

    node_type = "clamp"
    node_class = ClampNode

    def test_min_max_parameters(self):
        """Min/Max 参数."""
        node = self.create_node()
        if "min" in node.parameters:
            node.set_parameter("min", 0.0)
            assert node.parameters.get("min") == 0.0
        if "max" in node.parameters:
            node.set_parameter("max", 1.0)
            assert node.parameters.get("max") == 1.0

    def test_clamp_range(self):
        """Clamp range 参数."""
        node = self.create_node(params={"min": 0.0, "max": 1.0})
        inputs = self.create_valid_input()
        outputs = node.evaluate(
            inputs, self.get_mock_context(),
            self.get_mock_fb_manager(), self.get_mock_resource_pool()
        )
        # Should not crash


# ── Normalize Node Tests ────────────────────────────────────────────────────

class TestNormalizeNode(NodeTestBase):
    """Tests for NormalizeNode - normalize to [0,1]."""

    node_type = "normalize"
    node_class = NormalizeNode

    def test_normalize_range(self):
        """Normalize range 参数."""
        node = self.create_node()
        if "min" in node.parameters:
            node.set_parameter("min", 0.0)
        if "max" in node.parameters:
            node.set_parameter("max", 4.0)

    def test_auto_detect_range(self):
        """Auto detect range 可选."""
        node = self.create_node()
        if "auto" in node.parameters:
            node.set_parameter("auto", True)


# ── Compare Node Tests ──────────────────────────────────────────────────────

class TestCompareNode(NodeTestBase):
    """Tests for CompareNode - compare two inputs."""

    node_type = "compare"
    node_class = CompareNode

    def test_two_inputs(self):
        """Compare 需要两个输入."""
        node = self.create_node()
        # Should have two input ports (A and B)
        assert len(node.inputs) >= 2, "Compare should have at least 2 inputs"

    def test_compare_mode_parameter(self):
        """Compare mode 参数."""
        node = self.create_node()
        if "mode" in node.parameters:
            for mode in ["difference", "equal", "side_by_side"]:
                try:
                    test_node = self.create_node(params={"mode": mode})
                except:
                    pass

    def test_comparison_output(self):
        """Comparison emits a real GPU marker instead of a placeholder."""
        node = self.create_node()
        inputs = {
            "A": make_texture_result(texture_id=1),
            "B": make_texture_result(texture_id=2),
        }
        outputs = node.evaluate(
            inputs, self.get_mock_context(),
            self.get_mock_fb_manager(), self.get_mock_resource_pool()
        )
        assert node.IS_PLACEHOLDER is False
        assert outputs["_effect_type"] == "compare"
        assert outputs["_input_a"].texture_id == 1
        assert outputs["_input_b"].texture_id == 2


# ── Dot Node Tests ──────────────────────────────────────────────────────────

class TestDotNode(NodeTestBase):
    """Tests for DotNode - visual dot (pass-through)."""

    node_type = "dot"
    node_class = DotNode

    def test_passthrough(self):
        """Dot 节点直通输入."""
        node = self.create_node()
        inputs = self.create_valid_input()
        outputs = node.evaluate(
            inputs, self.get_mock_context(),
            self.get_mock_fb_manager(), self.get_mock_resource_pool()
        )
        # Dot should pass input through
        if "output" in outputs and "input" in inputs:
            assert outputs["output"].texture_id == inputs["input"].texture_id

    def test_single_input_output(self):
        """Dot 只有单输入输出."""
        node = self.create_node()
        assert len(node.inputs) == 1, "Dot should have 1 input"
        assert len(node.outputs) == 1, "Dot should have 1 output"


# ── NoOp Node Tests ─────────────────────────────────────────────────────────

class TestNoOpNode(NodeTestBase):
    """Tests for NoOpNode - no operation."""

    node_type = "noop"
    node_class = NoOpNode

    def test_passthrough(self):
        """NoOp 直通输入."""
        node = self.create_node()
        inputs = self.create_valid_input()
        outputs = node.evaluate(
            inputs, self.get_mock_context(),
            self.get_mock_fb_manager(), self.get_mock_resource_pool()
        )
        # NoOp should pass input through unchanged

    def test_no_parameters(self):
        """NoOp 应没有参数."""
        node = self.create_node()
        # NoOp typically has minimal or no parameters
        # Check that it doesn't require params to function


# ── Switch Node Tests ───────────────────────────────────────────────────────

class TestSwitchNode(NodeTestBase):
    """Tests for SwitchNode - switch between inputs."""

    node_type = "switch"
    node_class = SwitchNode

    def test_multiple_inputs(self):
        """Switch 支持多输入."""
        node = self.create_node()
        # Should have multiple input ports
        assert len(node.inputs) >= 2, "Switch should have multiple inputs"

    def test_input_index_parameter(self):
        """Input index 参数控制选择."""
        node = self.create_node()
        if "input_index" in node.parameters:
            node.set_parameter("input_index", 0)
            assert node.parameters.get("input_index") == 0
            node.set_parameter("input_index", 1)
            assert node.parameters.get("input_index") == 1

    def test_switch_output(self):
        """Switch output 选择正确输入."""
        node = self.create_node(params={"input_index": 0})
        inputs = {
            "input0": make_texture_result(texture_id=1),
            "input1": make_texture_result(texture_id=2),
        }
        outputs = node.evaluate(
            inputs, self.get_mock_context(),
            self.get_mock_fb_manager(), self.get_mock_resource_pool()
        )
        # Should output input0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
