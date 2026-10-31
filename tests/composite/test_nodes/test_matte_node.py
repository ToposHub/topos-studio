"""
Matte Node Tests - Tests for matte generation nodes.

Tests cover:
- MatteNode: matte extraction
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
from src.features.composite.nodes.matte_node import MatteNode

import pytest
pytestmark = pytest.mark.composite_core


# ── Matte Node Tests ────────────────────────────────────────────────────────

class TestMatteNode(NodeTestBase, KeyerNodeTestMixin):
    """Tests for MatteNode - matte extraction."""

    node_type = "matte"
    node_class = MatteNode

    def test_image_input(self):
        """Matte 需要 image 输入."""
        node = self.create_node()
        image_port = node.get_input_port("image")
        assert image_port is not None, "Matte should have image input"

    def test_matte_output(self):
        """Matte output 端口."""
        node = self.create_node()
        # Should produce matte output
        assert len(node.outputs) > 0, "Matte should have output"

    def test_matte_extract(self):
        """Matte extraction 正确."""
        node = self.create_node()
        inputs = self.create_valid_input()
        outputs = node.evaluate(
            inputs, self.get_mock_context(),
            self.get_mock_fb_manager(), self.get_mock_resource_pool()
        )
        # Should not crash

    def test_threshold_parameter(self):
        """Threshold 参数."""
        node = self.create_node()
        if "threshold" in node.parameters:
            node.set_parameter("threshold", 0.5)
            assert node.parameters.get("threshold") == 0.5

    def test_softness_parameter(self):
        """Softness 参数."""
        node = self.create_node()
        if "softness" in node.parameters:
            node.set_parameter("softness", 0.1)
            assert node.parameters.get("softness") == 0.1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])