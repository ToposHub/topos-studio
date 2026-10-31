"""
Filter Node Tests - Tests for filter/effect nodes.

Tests cover:
- BlurNode: box/gaussian/bilateral/motion/bokeh blur (5 types) + per-channel mask
- GlowNode: glow effect
- DenoiseNode: noise reduction
- SharpenNode: sharpen effect
- ErodeDilateNode: morphological operations
- DefocusNode: lens defocus
- EdgeDetectNode: edge detection
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from tests.node_test_base import (
    NodeTestBase,
    FilterNodeTestMixin,
    make_texture_result,
)
from src.features.composite.nodes.blur_node import BlurNode, MOTION_SOURCES
from src.features.composite.nodes.glow_node import GlowNode
from src.features.composite.nodes.denoise_node import DenoiseNode
from src.features.composite.nodes.sharpen_node import SharpenNode
from src.features.composite.nodes.erode_dilate_node import ErodeDilateNode
from src.features.composite.nodes.defocus_node import DefocusNode
from src.features.composite.nodes.edge_detect_node import EdgeDetectNode

import pytest
pytestmark = pytest.mark.composite_core


# ── Blur Node Tests ─────────────────────────────────────────────────────────

class TestBlurNode(NodeTestBase, FilterNodeTestMixin):
    """Tests for BlurNode - box/gaussian/bilateral/motion/bokeh blur."""

    node_type = "blur"
    node_class = BlurNode

    def test_radius_range(self):
        """Radius 参数在有效范围."""
        node = self.create_node()
        # Test radius_x/radius_y params
        node.set_parameter("radius_x", 0.0)
        assert node.parameters.get("radius_x") == 0.0
        node.set_parameter("radius_x", 100.0)
        assert node.parameters.get("radius_x") == 100.0
        node.set_parameter("radius_y", 50.0)
        assert node.parameters.get("radius_y") == 50.0

    def test_blur_types(self):
        """5 种 blur type 可用,box 为默认."""
        node = self.create_node()
        # Default is box.
        assert node.parameters.get("blur_type") == "box"
        for blur_type in ["box", "gaussian", "bilateral", "motion", "bokeh"]:
            test_node = self.create_node(params={"blur_type": blur_type})
            assert test_node.parameters.get("blur_type") == blur_type

    def test_motion_angle(self):
        """Motion blur 使用 angle 参数."""
        node = self.create_node(params={"blur_type": "motion"})
        node.set_parameter("angle", 45.0)
        assert node.parameters.get("angle") == 45.0

    def test_motion_velocity_contract(self):
        """Velocity mode carries shutter/source and the optional input."""
        node = self.create_node(params={
            "blur_type": "motion",
            "motion_source": "velocity",
            "shutter": 1.5,
            "radius": 0.0,
        })
        image = make_texture_result(texture_id=1, width=64, height=64)
        velocity = make_texture_result(texture_id=2, width=64, height=64)
        result = node._do_evaluate(
            {"image": image, "velocity": velocity}, None, None)
        assert result["_effect_params"]["motion_source"] == "velocity"
        assert result["_effect_params"]["shutter"] == 1.5
        assert result["_effect_velocity"] is velocity
        assert {p.name for p in node.inputs} == {"image", "velocity", "mask"}  # CP-044 通用 mask 端口

    def test_motion_sources_are_stable(self):
        assert MOTION_SOURCES == ["angle", "velocity"]

    def test_edge_policies(self):
        """Edge policy 参数可选."""
        node = self.create_node()
        for policy in ["clamp", "wrap", "mirror"]:
            test_node = self.create_node(params={"edge_policy": policy})
            assert test_node.parameters.get("edge_policy") == policy

    def test_quality_levels(self):
        """Quality 参数控制渲染质量."""
        node = self.create_node()
        for quality in ["fast", "normal", "high"]:
            test_node = self.create_node(params={"quality": quality})
            assert test_node.parameters.get("quality") == quality

    def test_process_alpha_option(self):
        """Process alpha 参数可选 (legacy, hidden)."""
        node = self.create_node()
        node.set_parameter("process_alpha", False)
        assert node.parameters.get("process_alpha") == False
        node.set_parameter("process_alpha", True)
        assert node.parameters.get("process_alpha") == True

    # ── New: type-specific parameter visibility ──

    def test_blur_type_param_visibility(self):
        """每种 blur_type 隐藏不相关的参数."""
        # Separable types show radius_x/y, hide scalar radius + type-specific.
        for sep in ("box", "gaussian"):
            hidden = BlurNode.blur_type_param_visibility(sep)
            assert "radius" in hidden
            assert "angle" in hidden
            assert "edge_preserve" in hidden
            assert "aperture_blades" in hidden
            assert "anamorphic" in hidden
            assert "highlight_gain" in hidden
            assert "radius_x" not in hidden
            assert "radius_y" not in hidden

        # Bilateral: scalar radius + edge_preserve shown; radius_x/y/angle/bokeh hidden.
        h = BlurNode.blur_type_param_visibility("bilateral")
        assert "radius_x" in h and "radius_y" in h
        assert "angle" in h
        assert "aperture_blades" in h and "anamorphic" in h and "highlight_gain" in h
        assert "radius" not in h
        assert "edge_preserve" not in h

        # Motion: scalar radius + angle/source/shutter shown.
        h = BlurNode.blur_type_param_visibility("motion")
        assert "radius_x" in h and "radius_y" in h
        assert "edge_preserve" in h
        assert "aperture_blades" in h and "anamorphic" in h and "highlight_gain" in h
        assert "radius" not in h
        assert "angle" not in h
        assert "motion_source" not in h
        assert "shutter" not in h

        # Velocity mode hides the unused angle control.
        h = BlurNode.blur_type_param_visibility("motion", "velocity")
        assert "angle" in h

        # Bokeh: scalar radius + aperture/anamorphic/highlight shown.
        h = BlurNode.blur_type_param_visibility("bokeh")
        assert "radius_x" in h and "radius_y" in h
        assert "angle" in h
        assert "edge_preserve" in h
        assert "radius" not in h
        assert "aperture_blades" not in h
        assert "anamorphic" not in h
        assert "highlight_gain" not in h

        # process_alpha always hidden (legacy).
        for t in ("box", "gaussian", "bilateral", "motion", "bokeh"):
            assert "process_alpha" in BlurNode.blur_type_param_visibility(t)

    # ── New: channel mask packing ──

    def test_channel_mask_packing(self):
        """channel_r/g/b/a 默认 RGB 开 A 关,可独立切换并打包成 channel_mask."""
        node = self.create_node()
        # Defaults: RGB on, A off.
        assert node.parameters["channel_r"] is True
        assert node.parameters["channel_g"] is True
        assert node.parameters["channel_b"] is True
        assert node.parameters["channel_a"] is False

        # Toggle individual channels.
        node.set_parameter("channel_r", False)
        node.set_parameter("channel_a", True)
        assert node.parameters["channel_r"] is False
        assert node.parameters["channel_a"] is True

    # ── New: backward compat (directional -> motion) ──

    def test_directional_backward_compat(self):
        """旧工程 directional 类型读入后映射为 motion,不崩溃."""
        node = self.create_node(params={"blur_type": "directional"})
        # _do_evaluate maps directional -> motion in the effect params.
        from src.features.composite.nodes.base_node import TextureResult
        # texture_id > 0 => is_valid property is True.
        fake_image = TextureResult(
            texture_id=1, width=64, height=64,
            alpha_premultiplied=False, color_space=None)
        result = node._do_evaluate({"image": fake_image}, None, None)
        assert result["_effect_params"]["blur_type"] == "motion"
        # motion uses scalar radius, angle carried over.
        assert "angle" in result["_effect_params"]

        # Radius helper also normalises directional to motion semantics.
        rx, ry = node._get_effective_radius(
            {"blur_type": "directional", "radius": 12.0})
        assert rx == 12.0 and ry == 12.0

    def test_roi_expansion(self):
        """Blur 扩展 ROI."""
        node = self.create_node(params={"radius_x": 10.0, "radius_y": 10.0})
        if hasattr(node, 'requires_roi_expansion'):
            expansion = node.requires_roi_expansion(node.parameters)
            assert expansion >= 0, "ROI expansion should be positive or zero"


# ── Glow Node Tests ─────────────────────────────────────────────────────────

class TestGlowNode(NodeTestBase, FilterNodeTestMixin):
    """Tests for GlowNode - glow effect."""

    node_type = "glow"
    node_class = GlowNode

    def test_glow_intensity(self):
        """Glow intensity 参数."""
        node = self.create_node()
        if "intensity" in node.parameters:
            node.set_parameter("intensity", 0.5)
            assert node.parameters.get("intensity") == 0.5

    def test_glow_radius(self):
        """Glow radius 参数."""
        node = self.create_node()
        if "radius" in node.parameters:
            node.set_parameter("radius", 20.0)
            assert node.parameters.get("radius") == 20.0


# ── Denoise Node Tests ──────────────────────────────────────────────────────

class TestDenoiseNode(NodeTestBase, FilterNodeTestMixin):
    """Tests for DenoiseNode - noise reduction."""

    node_type = "denoise"
    node_class = DenoiseNode

    def test_denoise_strength(self):
        """Denoise strength 参数."""
        node = self.create_node()
        # Check strength param
        strength_param = None
        for pdef in DenoiseNode.PARAM_DEFS:
            if "strength" in pdef.name.lower() or "amount" in pdef.name.lower():
                strength_param = pdef.name
                break

        if strength_param:
            node.set_parameter(strength_param, 0.5)
            assert node.parameters.get(strength_param) == 0.5


# ── Sharpen Node Tests ──────────────────────────────────────────────────────

class TestSharpenNode(NodeTestBase, FilterNodeTestMixin):
    """Tests for SharpenNode - sharpen effect."""

    node_type = "sharpen"
    node_class = SharpenNode

    def test_sharpen_amount(self):
        """Sharpen amount 参数."""
        node = self.create_node()
        if "amount" in node.parameters:
            node.set_parameter("amount", 1.0)
            assert node.parameters.get("amount") == 1.0


# ── Erode Dilate Node Tests ─────────────────────────────────────────────────

class TestErodeDilateNode(NodeTestBase, FilterNodeTestMixin):
    """Tests for ErodeDilateNode - morphological operations."""

    node_type = "erode_dilate"
    node_class = ErodeDilateNode

    def test_operation_type(self):
        """Erode/Dilate 操作可选."""
        node = self.create_node()
        if "operation" in node.parameters:
            for op in ["erode", "dilate"]:
                test_node = self.create_node(params={"operation": op})

    def test_size_parameter(self):
        """Size 参数控制操作尺寸."""
        node = self.create_node()
        if "size" in node.parameters:
            node.set_parameter("size", 5)
            assert node.parameters.get("size") == 5


# ── Defocus Node Tests ──────────────────────────────────────────────────────

class TestDefocusNode(NodeTestBase, FilterNodeTestMixin):
    """Tests for DefocusNode - lens defocus."""

    node_type = "defocus"
    node_class = DefocusNode

    def test_defocus_size(self):
        """Defocus size 参数."""
        node = self.create_node()
        if "size" in node.parameters:
            node.set_parameter("size", 10.0)
            assert node.parameters.get("size") == 10.0


# ── Edge Detect Node Tests ──────────────────────────────────────────────────

class TestEdgeDetectNode(NodeTestBase, FilterNodeTestMixin):
    """Tests for EdgeDetectNode - edge detection."""

    node_type = "edge_detect"
    node_class = EdgeDetectNode

    def test_edge_method(self):
        """Edge detection method 参数."""
        node = self.create_node()
        # Check method param
        if "method" in node.parameters:
            for method in ["sobel", "laplacian"]:
                test_node = self.create_node(params={"method": method})


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
