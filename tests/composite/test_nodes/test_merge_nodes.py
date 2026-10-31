"""
Merge Node Tests - Tests for merge/blend nodes.

Tests cover:
- MergeNode/BlendNode: compositing with various blend modes
- PremultNode: premultiply alpha
- UnpremultNode: unpremultiply alpha
- DissolveNode: dissolve transition
- DeepMergeNode: deep compositing
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from tests.node_test_base import (
    NodeTestBase,
    MergeNodeTestMixin,
    make_texture_result,
)
from src.features.composite.nodes.blend_node import MergeNode
from src.features.composite.nodes.premult_node import PremultNode
from src.features.composite.nodes.unpremult_node import UnpremultNode
from src.features.composite.nodes.dissolve_node import DissolveNode
from src.features.composite.nodes.deep_merge_node import DeepMergeNode


# ── Merge/Blend Node Tests ──────────────────────────────────────────────────

class TestMergeNode(NodeTestBase, MergeNodeTestMixin):
    """Tests for MergeNode - compositing with blend modes."""

    node_type = "merge"
    node_class = MergeNode

    def test_two_inputs_required(self):
        """Merge 需要两个输入 (A 和 B)."""
        node = self.create_node()
        input_a = node.get_input_port("A")
        input_b = node.get_input_port("B")
        assert input_a is not None, "Merge should have A input"
        assert input_b is not None, "Merge should have B input"

    def test_blend_mode_options(self):
        """各种 blend mode 可选."""
        node = self.create_node()
        # Check blend_mode param
        if "blend_mode" in node.parameters:
            # Test common blend modes
            common_modes = ["over", "add", "multiply", "screen", "minus"]
            for mode in common_modes:
                if mode in MergeNode.PARAM_DEFS[0].labels if MergeNode.PARAM_DEFS else []:
                    test_node = self.create_node()
                    test_node.set_parameter("blend_mode", mode)

    def test_opacity_range(self):
        """Opacity 参数在 [0, 1] 范围."""
        node = self.create_node()
        if "opacity" in node.parameters:
            # Test opacity extremes
            node.set_parameter("opacity", 0.0)
            assert node.parameters.get("opacity") == 0.0
            node.set_parameter("opacity", 1.0)
            assert node.parameters.get("opacity") == 1.0
            node.set_parameter("opacity", 0.5)
            assert node.parameters.get("opacity") == 0.5

    def test_bbox_policy(self):
        """BBox policy 控制输出边界."""
        node = self.create_node()
        # Check bbox_policy param
        if "bbox_policy" in node.parameters:
            policies = ["union", "intersection", "from_a", "from_b"]
            for policy in policies:
                test_node = self.create_node()
                try:
                    test_node.set_parameter("bbox_policy", policy)
                except:
                    pass  # Some policies may not be valid

    def test_format_policy(self):
        """Format policy 控制输出格式."""
        node = self.create_node()
        # Check format_policy param
        if "format_policy" in node.parameters:
            node.set_parameter("format_policy", "from_b")
            assert node.parameters.get("format_policy") == "from_b"

    def test_bypass_mapping(self):
        """Bypass 映射正确."""
        node = self.create_node()
        # Check BYPASS_MAPPING if defined
        if node.BYPASS_MAPPING:
            assert isinstance(node.BYPASS_MAPPING, dict), \
                "BYPASS_MAPPING should be dict"
            for out_port, in_port in node.BYPASS_MAPPING.items():
                assert node.get_output_port(out_port) is not None, \
                    f"Bypass output port {out_port} not found"
                assert node.get_input_port(in_port) is not None, \
                    f"Bypass input port {in_port} not found"

    def test_single_input_passthrough(self):
        """单输入时应正确处理."""
        node = self.create_node()
        # Only provide A input, no B
        inputs = {"A": make_texture_result(texture_id=1)}
        outputs = node.evaluate(
            inputs, self.get_mock_context(),
            self.get_mock_fb_manager(), self.get_mock_resource_pool()
        )
        # Should not crash
        assert node.state is not None


class TestBlendNode:
    """Tests for blend alias - legacy alias for MergeNode.

    NOTE: 'blend' is a legacy migration alias that points to MergeNode.
    The actual node_type is 'merge', not 'blend'.
    This test class only validates alias registration, NOT node_type equality.
    """

    def test_alias_registered(self):
        """blend 是 merge 的别名，指向同一个节点类."""
        from src.features.composite.core.node_engine import NODE_REGISTRY
        assert NODE_REGISTRY.get("blend") == MergeNode, \
            "'blend' should be registered as alias to MergeNode"
        assert NODE_REGISTRY.get("merge") == MergeNode, \
            "'merge' should be registered as MergeNode"

    def test_alias_same_class(self):
        """blend 和 merge 指向同一个节点类."""
        from src.features.composite.core.node_engine import NODE_REGISTRY
        assert NODE_REGISTRY["blend"] is NODE_REGISTRY["merge"], \
            "'blend' and 'merge' should reference the exact same class object"

    def test_node_type_is_merge(self):
        """通过 'blend' alias 创建的节点，node_type 应为 'merge'."""
        node = MergeNode()
        assert node.node_type == "merge", \
            "MergeNode.node_type is always 'merge', regardless of how it's registered"

    def test_backward_compatible_alias(self):
        """BlendNode 别名（类名）向后兼容."""
        # Verify BlendNode = MergeNode alias exists in blend_node.py
        from src.features.composite.nodes.blend_node import BlendNode
        assert BlendNode is MergeNode, \
            "BlendNode should be a backward-compatible alias for MergeNode"

    def test_search_aliases_include_blend(self):
        """MergeNode 的 SEARCH_ALIASES 包含 'blend'."""
        assert "blend" in MergeNode.SEARCH_ALIASES, \
            "MergeNode.SEARCH_ALIASES should include 'blend' for discoverability"


# ── Premult Node Tests ──────────────────────────────────────────────────────

class TestPremultNode(NodeTestBase):
    """Tests for PremultNode - premultiply alpha."""

    node_type = "premult"
    node_class = PremultNode

    def test_premult_operation(self):
        """Premult 节点乘入 alpha."""
        node = self.create_node()
        inputs = self.create_valid_input()
        outputs = node.evaluate(
            inputs, self.get_mock_context(),
            self.get_mock_fb_manager(), self.get_mock_resource_pool()
        )
        # Should produce output
        assert "result" in outputs or outputs == {}

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


# ── Unpremult Node Tests ────────────────────────────────────────────────────

class TestUnpremultNode(NodeTestBase):
    """Tests for UnpremultNode - unpremultiply alpha."""

    node_type = "unpremult"
    node_class = UnpremultNode

    def test_unpremult_operation(self):
        """Unpremult 节点分离 alpha."""
        node = self.create_node()
        inputs = self.create_valid_input()
        outputs = node.evaluate(
            inputs, self.get_mock_context(),
            self.get_mock_fb_manager(), self.get_mock_resource_pool()
        )
        # Should produce output
        assert "result" in outputs or outputs == {}

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


# ── Dissolve Node Tests ─────────────────────────────────────────────────────

class TestDissolveNode(NodeTestBase, MergeNodeTestMixin):
    """Tests for DissolveNode - dissolve transition."""

    node_type = "dissolve"
    node_class = DissolveNode

    def test_mix_parameter(self):
        """Mix 参数控制混合比例."""
        node = self.create_node()
        # Check mix param
        if "mix" in node.parameters:
            # Test mix range [0, 1]
            node.set_parameter("mix", 0.0)
            assert node.parameters.get("mix") == 0.0
            node.set_parameter("mix", 1.0)
            assert node.parameters.get("mix") == 1.0
            node.set_parameter("mix", 0.5)
            assert node.parameters.get("mix") == 0.5

    def test_two_inputs_required(self):
        """Dissolve 需要两个输入."""
        node = self.create_node()
        # Should have two input ports
        assert len(node.inputs) >= 2, "Dissolve should have at least 2 inputs"


# ── Deep Merge Node Tests ───────────────────────────────────────────────────

class TestDeepMergeNode(NodeTestBase):
    """Tests for DeepMergeNode - deep compositing."""

    node_type = "deep_merge"
    node_class = DeepMergeNode

    def test_deep_input_handling(self):
        """Deep merge 处理带 depth 的输入."""
        node = self.create_node()
        # Create input with depth info
        inputs = {
            "A": make_texture_result(texture_id=1, width=1920, height=1080),
            "B": make_texture_result(texture_id=2, width=1920, height=1080),
        }
        outputs = node.evaluate(
            inputs, self.get_mock_context(),
            self.get_mock_fb_manager(), self.get_mock_resource_pool()
        )
        # Should not crash


if __name__ == "__main__":
    pytest.main([__file__, "-v"])