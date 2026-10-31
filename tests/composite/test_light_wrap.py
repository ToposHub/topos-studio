"""Contract tests for the P1-09 Light Wrap node."""

from tests.node_test_base import NodeTestBase, make_texture_result
from src.features.composite.core.node_capabilities import get_node_capability
from src.features.composite.core.node_engine import NODE_CATEGORIES, NODE_REGISTRY
from src.features.composite.nodes.light_wrap_node import LightWrapNode


class TestLightWrapNode(NodeTestBase):
    node_type = "light_wrap"
    node_class = LightWrapNode

    def test_registry_and_capability_are_real_effect(self):
        assert NODE_REGISTRY["light_wrap"] is LightWrapNode
        assert any(
            node_type == "light_wrap"
            for node_type, _node_class in NODE_CATEGORIES["Keyer"]
        )
        capability = get_node_capability("light_wrap")
        assert capability.effect_type == "light_wrap"
        assert capability.is_placeholder is False

    def test_emits_explicit_background_foreground_marker(self):
        node = self.create_node()
        background = make_texture_result(texture_id=11, width=1280, height=720)
        foreground = make_texture_result(texture_id=12, width=1280, height=720)

        outputs = node.evaluate(
            {"background": background, "foreground": foreground},
            self.get_mock_context(), self.get_mock_fb_manager(),
            self.get_mock_resource_pool(), render_mode="full",
        )

        assert outputs["_effect_type"] == "light_wrap"
        assert outputs["_effect_input_a"] is background
        assert outputs["_effect_input_b"] is foreground
        assert outputs["result"].alpha_premultiplied is True

    def test_preview_limits_sampling_width(self):
        node = self.create_node(params={"width": 100.0})
        inputs = {
            "background": make_texture_result(texture_id=21),
            "foreground": make_texture_result(texture_id=22),
        }

        outputs = node.evaluate(
            inputs, self.get_mock_context(), self.get_mock_fb_manager(),
            self.get_mock_resource_pool(), render_mode="preview",
        )

        assert outputs["_effect_params"]["width"] == 48.0

    def test_missing_input_does_not_create_effect(self):
        node = self.create_node()
        outputs = node.evaluate(
            {"foreground": make_texture_result()},
            self.get_mock_context(), self.get_mock_fb_manager(),
            self.get_mock_resource_pool(),
        )
        assert outputs == {}

    def test_roi_expands_by_wrap_width(self):
        node = self.create_node(params={"width": 20.0})
        assert node.requires_roi_expansion(node.parameters) == 20

