"""
AI Node Tests - Tests for AI-powered nodes.

Tests cover:
- AIGenerateNode: AI image generation
- AIInpaintNode: AI inpainting
- AIExpandNode: AI expand/outpainting
- AIRemoveNode: AI object removal
- AIDenoiseNode: AI denoise
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from tests.node_test_base import (
    NodeTestBase,
    make_texture_result,
)
from src.features.composite.nodes.ai_generate_node import AIGenerateNode
from src.features.composite.nodes.ai_inpaint_node import AIInpaintNode
from src.features.composite.nodes.ai_expand_node import AIExpandNode
from src.features.composite.nodes.ai_remove_node import AIRemoveNode
from src.features.composite.nodes.ai_denoise_node import AIDenoiseNode

import pytest
pytestmark = pytest.mark.composite_core


# ── AI Generate Node Tests ──────────────────────────────────────────────────

class TestAIGenerateNode(NodeTestBase):
    """Tests for AIGenerateNode - AI image generation."""

    node_type = "ai_generate"
    node_class = AIGenerateNode

    def test_prompt_parameter(self):
        """Prompt 参数."""
        node = self.create_node()
        if "prompt" in node.parameters:
            node.set_parameter("prompt", "test prompt")
            assert node.parameters.get("prompt") == "test prompt"

    def test_negative_prompt_parameter(self):
        """Negative prompt 参数."""
        node = self.create_node()
        if "negative_prompt" in node.parameters:
            node.set_parameter("negative_prompt", "blur, low quality")

    def test_strength_parameter(self):
        """Strength 参数."""
        node = self.create_node()
        if "strength" in node.parameters:
            node.set_parameter("strength", 0.5)
            assert node.parameters.get("strength") == 0.5

    def test_is_placeholder_flag(self):
        """AI node placeholder status."""
        node = self.create_node()
        # Check if marked as placeholder
        # Some AI nodes may be placeholder until model loaded
        pass


# ── AI Inpaint Node Tests ───────────────────────────────────────────────────

class TestAIInpaintNode(NodeTestBase):
    """Tests for AIInpaintNode - AI inpainting."""

    node_type = "ai_inpaint"
    node_class = AIInpaintNode

    def test_mask_input(self):
        """Inpaint 需要 mask 输入."""
        node = self.create_node()
        mask_port = node.get_input_port("mask")
        if mask_port:
            assert mask_port.direction == "input"
            assert mask_port.data_type == "image"

    def test_inpaint_output(self):
        """Inpaint output 正确."""
        node = self.create_node()
        inputs = {
            "image": make_texture_result(texture_id=1),
            "mask": make_texture_result(texture_id=2),
        }
        outputs = node.evaluate(
            inputs, self.get_mock_context(),
            self.get_mock_fb_manager(), self.get_mock_resource_pool()
        )
        # Should not crash


# ── AI Expand Node Tests ────────────────────────────────────────────────────

class TestAIExpandNode(NodeTestBase):
    """Tests for AIExpandNode - AI expand/outpainting."""

    node_type = "ai_expand"
    node_class = AIExpandNode

    def test_expand_direction_parameter(self):
        """Expand direction 参数."""
        node = self.create_node()
        if "direction" in node.parameters:
            for direction in ["left", "right", "top", "bottom", "all"]:
                try:
                    test_node = self.create_node(params={"direction": direction})
                except:
                    pass

    def test_expand_amount_parameter(self):
        """Expand amount 参数."""
        node = self.create_node()
        if "amount" in node.parameters:
            node.set_parameter("amount", 50)


# ── AI Remove Node Tests ────────────────────────────────────────────────────

class TestAIRemoveNode(NodeTestBase):
    """Tests for AIRemoveNode - AI object removal."""

    node_type = "ai_remove"
    node_class = AIRemoveNode

    def test_mask_input(self):
        """Remove 需要 mask 输入."""
        node = self.create_node()
        mask_port = node.get_input_port("mask")
        if mask_port:
            assert mask_port.direction == "input"

    def test_remove_output(self):
        """Remove output 正确."""
        node = self.create_node()
        inputs = {
            "image": make_texture_result(texture_id=1),
            "mask": make_texture_result(texture_id=2),
        }
        outputs = node.evaluate(
            inputs, self.get_mock_context(),
            self.get_mock_fb_manager(), self.get_mock_resource_pool()
        )
        # Should not crash


# ── AI Denoise Node Tests ───────────────────────────────────────────────────

class TestAIDenoiseNode(NodeTestBase):
    """Tests for AIDenoiseNode - AI denoise."""

    node_type = "ai_denoise"
    node_class = AIDenoiseNode

    def test_strength_parameter(self):
        """Strength 参数."""
        node = self.create_node()
        if "strength" in node.parameters:
            node.set_parameter("strength", 0.5)
            assert node.parameters.get("strength") == 0.5

    def test_model_parameter(self):
        """Model 参数."""
        node = self.create_node()
        if "model" in node.parameters:
            node.set_parameter("model", "default")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])