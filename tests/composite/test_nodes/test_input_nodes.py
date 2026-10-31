"""
Input Node Tests - Tests for input/source nodes.

Tests cover:
- ColorNode (color_input): solid color generator
- ConstantNode: constant value generator
- ImageInputNode: external image input
- VideoFrameNode: video frame input
- TextNode: text generator
- NoiseNode: noise generator
- RampNode: ramp/gradient generator
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from tests.node_test_base import (
    NodeTestBase,
    make_texture_result,
)
from src.features.composite.nodes.color_node import COLORNode
from src.features.composite.nodes.constant_node import ConstantNode
from src.features.composite.nodes.image_input_node import ImageInputNode
from src.features.composite.nodes.video_frame_node import VideoFrameNode
from src.features.composite.nodes.text_node import TextNode
from src.features.composite.nodes.noise_node import NoiseNode
from src.features.composite.nodes.ramp_node import RampNode
from src.shared.color.color_conversion import srgb_to_linear

import pytest
pytestmark = pytest.mark.composite_core


# ── Color Input Node Tests ──────────────────────────────────────────────────

class TestColorNode(NodeTestBase):
    """Tests for COLORNode - solid color generator."""

    node_type = "color_input"
    node_class = COLORNode

    def test_color_rgb_parameters(self):
        """Color RGB 参数."""
        node = self.create_node()
        # Check color params
        if "color_r" in node.parameters:
            node.set_parameter("color_r", 1.0)
            assert node.parameters.get("color_r") == 1.0
        if "color_g" in node.parameters:
            node.set_parameter("color_g", 0.5)
        if "color_b" in node.parameters:
            node.set_parameter("color_b", 0.0)

    def test_color_hex_input(self):
        """Color hex 输入."""
        node = self.create_node()
        if "color" in node.parameters:
            node.set_parameter("color", "#FF0000")

    def test_alpha_parameter(self):
        """Alpha 参数."""
        node = self.create_node()
        if "alpha" in node.parameters:
            node.set_parameter("alpha", 1.0)
            assert node.parameters.get("alpha") == 1.0

    def test_no_input_expected(self):
        """Color input 不需要输入."""
        node = self.create_node()
        # Color node has no inputs
        assert len(node.inputs) == 0 or \
               all(p.name == "mask" for p in node.inputs), \
               "Color node should have no required inputs"

    def test_output_port(self):
        """Color output端口正确."""
        node = self.create_node()
        # Should have output port
        assert len(node.outputs) > 0, "Color node should have output"


# ── Constant Node Tests ─────────────────────────────────────────────────────

class TestConstantNode(NodeTestBase):
    """Tests for ConstantNode - constant value generator."""

    node_type = "constant"
    node_class = ConstantNode

    def test_width_height_parameters(self):
        """Width/Height 参数."""
        node = self.create_node()
        if "width" in node.parameters:
            node.set_parameter("width", 1920)
            assert node.parameters.get("width") == 1920
        if "height" in node.parameters:
            node.set_parameter("height", 1080)
            assert node.parameters.get("height") == 1080

    def test_constant_color(self):
        """Constant color 参数."""
        node = self.create_node()
        # Check color params
        if "color_r" in node.parameters:
            node.set_parameter("color_r", 0.5)

    def test_color_helpers_use_srgb_display_values(self):
        """Constant hex helpers round-trip display sRGB and linear storage."""
        linear = tuple(srgb_to_linear(value) for value in (0.2, 0.4, 0.6))
        assert ConstantNode.rgba_to_hex(*linear) == "#336699"
        assert ConstantNode.hex_to_rgba("#336699")[:3] == linear

    def test_legacy_constant_data_is_migrated_on_load(self):
        """Old graphs without the marker are converted once at load time."""
        node = ConstantNode.from_dict({
            "id": "legacy_constant",
            "type": "constant",
            "parameters": {
                "color_r": 0.5,
                "color_g": 0.25,
                "color_b": 0.75,
            },
        })
        assert node.parameters["color_r"] == srgb_to_linear(0.5)
        assert node.parameters["color_g"] == srgb_to_linear(0.25)
        assert node.parameters["color_b"] == srgb_to_linear(0.75)
        assert node.parameters["_topos_color_space"] == "linear"


# ── Image Input Node Tests ──────────────────────────────────────────────────

class TestImageInputNode(NodeTestBase):
    """Tests for ImageInputNode - external image input."""

    node_type = "image_input"
    node_class = ImageInputNode

    def test_file_path_parameter(self):
        """File path 参数."""
        node = self.create_node()
        if "file_path" in node.parameters:
            node.set_parameter("file_path", "/tmp/test.png")
            assert node.parameters.get("file_path") == "/tmp/test.png"

    def test_no_input_expected(self):
        """Image input 不需要上游输入."""
        node = self.create_node()
        # Image input reads from file, not upstream node
        assert len(node.inputs) == 0 or \
               all(p.name == "mask" for p in node.inputs), \
               "Image input should have no upstream inputs"

    def test_output_port(self):
        """Image output端口正确."""
        node = self.create_node()
        assert len(node.outputs) > 0, "Image input should have output"


# ── Video Frame Node Tests ──────────────────────────────────────────────────

class TestVideoFrameNode(NodeTestBase):
    """Tests for VideoFrameNode - video frame input."""

    node_type = "video_frame"
    node_class = VideoFrameNode

    def test_video_path_parameter(self):
        """Video path 参数."""
        node = self.create_node()
        if "video_path" in node.parameters:
            node.set_parameter("video_path", "/tmp/test.mp4")

    def test_frame_number_parameter(self):
        """Frame number 参数."""
        node = self.create_node()
        if "frame_number" in node.parameters:
            node.set_parameter("frame_number", 0)
            assert node.parameters.get("frame_number") == 0
            node.set_parameter("frame_number", 100)


# ── Text Node Tests ─────────────────────────────────────────────────────────

class TestTextNode(NodeTestBase):
    """Tests for TextNode - text generator (CPU QPainter rasterization)."""

    node_type = "text"
    node_class = TextNode

    def test_text_content_parameter(self):
        """Text content 参数."""
        node = self.create_node()
        if "text" in node.parameters:
            node.set_parameter("text", "Hello World")
            assert node.parameters.get("text") == "Hello World"

    def test_font_size_parameter(self):
        """Font size 参数."""
        node = self.create_node()
        if "font_size" in node.parameters:
            node.set_parameter("font_size", 24)

    def test_text_transform_translate(self):
        """Text transform translate (pixel x/y) 参数."""
        node = self.create_node()
        if "translate_x" in node.parameters:
            node.set_parameter("translate_x", 100.0)
            assert node.parameters.get("translate_x") == 100.0
        if "translate_y" in node.parameters:
            node.set_parameter("translate_y", -50.0)
            assert node.parameters.get("translate_y") == -50.0

    def test_text_transform_params_present(self):
        """Text node exposes Transform-compatible params for the gizmo."""
        node = self.create_node()
        for k in ("translate_x", "translate_y", "scale_x", "scale_y",
                  "rotation", "pivot_mode", "pivot_x", "pivot_y", "filter"):
            assert k in node.parameters, f"missing transform param {k}"
        # Old anchor/scale params must be gone (replaced by the transform set).
        assert "anchor_x" not in node.parameters
        assert "anchor_y" not in node.parameters
        assert "scale" not in node.parameters  # now scale_x/scale_y

    def test_text_is_not_placeholder(self):
        """Text node should NOT be a placeholder (it now rasterizes text)."""
        assert TextNode.IS_PLACEHOLDER is False
        node = self.create_node()
        from src.features.composite.nodes.placeholder_node import PlaceholderNode
        assert not isinstance(node, PlaceholderNode)

    def test_text_metadata(self):
        """Text node has the expected search aliases and i18n key."""
        assert "text" in TextNode.SEARCH_ALIASES
        assert "文字" in TextNode.SEARCH_ALIASES
        assert TextNode.i18n_title_key == "nodes.text.title"
        assert TextNode.DESCRIPTION != ""
        assert "input" in TextNode.TAGS

    def test_text_output_port(self):
        """Text node exposes a single 'result' image output port."""
        node = self.create_node()
        assert len(node.outputs) == 1
        assert node.outputs[0].name == "result"
        assert node.outputs[0].data_type == "image"

    def test_text_param_visibility(self):
        """param_visibility hides outline/shadow groups when disabled."""
        # Both disabled: outline/shadow params hidden
        hidden = TextNode.param_visibility({
            "outline_enable": False, "shadow_enable": False,
        })
        assert "outline_width" in hidden
        assert "shadow_blur" in hidden
        assert "fill_hex" in hidden  # always-hidden UI params

        # Both enabled: outline/shadow params visible
        hidden2 = TextNode.param_visibility({
            "outline_enable": True, "shadow_enable": True,
        })
        assert "outline_width" not in hidden2
        assert "shadow_blur" not in hidden2

    def test_text_color_helpers(self):
        """HDR-safe color helpers convert correctly."""
        assert TextNode.rgba_to_hex(1.0, 0.0, 0.0) == "#ff0000"
        rgba = TextNode.hex_to_rgba("#00ff00")
        assert rgba == (0.0, 1.0, 0.0, 1.0)
        # HDR >1.0 input is accepted by the helper contract
        assert TextNode.rgba_to_hex(1.0, 1.0, 1.0) == "#ffffff"

    def test_text_content_hash_changes_with_text(self):
        """_content_hash must change when text content changes (cache key)."""
        node = self.create_node()
        node.set_parameter("text", "AAA")
        h1 = node._content_hash()
        node.set_parameter("text", "BBB")
        h2 = node._content_hash()
        assert h1 != h2

    def test_text_content_hash_stable_for_same_params(self):
        """_content_hash must be stable when params are unchanged."""
        node = self.create_node()
        node.set_parameter("text", "Same")
        h1 = node._content_hash()
        h2 = node._content_hash()
        assert h1 == h2

    def test_text_content_hash_changes_with_color(self):
        """_content_hash must change when fill color changes."""
        node = self.create_node()
        node.set_parameter("fill_r", 1.0)
        h1 = node._content_hash()
        node.set_parameter("fill_r", 2.0)
        h2 = node._content_hash()
        assert h1 != h2

    def test_text_render_pixels_basic(self, qapp):
        """_render_text_pixels produces a valid RGBA float32 buffer."""
        node = self.create_node()
        node.set_parameter("width", 256)
        node.set_parameter("height", 128)
        node.set_parameter("text", "Hello")
        node.set_parameter("font_size", 48)
        data = node._render_text_pixels(256, 128)
        import numpy as np
        assert data.shape == (128, 256, 4)
        assert data.dtype == np.float32
        # Text must actually render opaque pixels.
        assert (data[..., 3] > 0.5).sum() > 100

    def test_text_render_pixels_hdr(self, qapp):
        """HDR fill colors (>1.0) survive rasterization (not clamped)."""
        node = self.create_node()
        node.set_parameter("width", 128)
        node.set_parameter("height", 128)
        node.set_parameter("text", "HDR")
        node.set_parameter("font_size", 48)
        node.set_parameter("fill_r", 4.0)
        node.set_parameter("fill_g", 2.0)
        node.set_parameter("fill_b", 0.5)
        data = node._render_text_pixels(128, 128)
        # HDR red value should be preserved at ~4.0 on opaque text pixels.
        assert float(data[..., 0].max()) > 1.5

    def test_text_render_pixels_empty(self, qapp):
        """Empty / whitespace text renders a fully transparent canvas."""
        node = self.create_node()
        node.set_parameter("text", "   ")
        data = node._render_text_pixels(64, 64)
        assert float(data[..., 3].max()) < 0.01

    def test_text_render_pixels_outline_expands_coverage(self, qapp):
        """Outline mode strokes glyphs and expands coverage vs fill alone."""
        import numpy as np
        base = self.create_node()
        base.set_parameter("width", 256)
        base.set_parameter("height", 128)
        base.set_parameter("text", "Test")
        base.set_parameter("font_size", 48)
        fill = base._render_text_pixels(256, 128)

        outlined = self.create_node()
        outlined.set_parameter("width", 256)
        outlined.set_parameter("height", 128)
        outlined.set_parameter("text", "Test")
        outlined.set_parameter("font_size", 48)
        outlined.set_parameter("outline_enable", True)
        outlined.set_parameter("outline_width", 12.0)
        data = outlined._render_text_pixels(256, 128)

        fill_coverage = int((fill[..., 3] > 0.01).sum())
        outline_coverage = int((data[..., 3] > 0.01).sum())
        assert outline_coverage > fill_coverage

    def test_text_render_pixels_shadow(self, qapp):
        """Shadow + blur produces extra coverage around the glyphs."""
        import numpy as np
        base = self.create_node()
        base.set_parameter("width", 256)
        base.set_parameter("height", 128)
        base.set_parameter("text", "Shadow")
        base.set_parameter("font_size", 48)
        fill = base._render_text_pixels(256, 128)

        shadowed = self.create_node()
        shadowed.set_parameter("width", 256)
        shadowed.set_parameter("height", 128)
        shadowed.set_parameter("text", "Shadow")
        shadowed.set_parameter("font_size", 48)
        shadowed.set_parameter("shadow_enable", True)
        shadowed.set_parameter("shadow_blur", 8.0)
        shadowed.set_parameter("shadow_offset_x", 15.0)
        shadowed.set_parameter("shadow_offset_y", 15.0)
        data = shadowed._render_text_pixels(256, 128)

        fill_coverage = int((fill[..., 3] > 0.01).sum())
        shadow_coverage = int((data[..., 3] > 0.01).sum())
        assert shadow_coverage > fill_coverage

    def test_text_render_pixels_multiline_rotation(self, qapp):
        """Multi-line text with rotation renders correctly."""
        node = self.create_node()
        node.set_parameter("width", 200)
        node.set_parameter("height", 200)
        node.set_parameter("text", "Line1\nLine2")
        node.set_parameter("font_size", 32)
        node.set_parameter("rotation", 45.0)
        node.set_parameter("line_spacing", 1.5)
        data = node._render_text_pixels(200, 200)
        assert (data[..., 3] > 0.5).sum() > 50

    def test_text_render_pixels_letter_spacing(self, qapp):
        """Letter spacing widens the text's horizontal bounding box."""
        import numpy as np
        def _bbox_w(spacing):
            n = self.create_node()
            n.set_parameter("width", 400)
            n.set_parameter("height", 100)
            n.set_parameter("text", "WIDE")
            n.set_parameter("font_size", 48)
            n.set_parameter("letter_spacing", spacing)
            d = n._render_text_pixels(400, 100)
            xs = np.where(d[..., 3] > 0.3)[1]
            return xs.max() - xs.min() if len(xs) else 0

        w_wide = _bbox_w(20.0)
        w_normal = _bbox_w(0.0)
        assert w_wide > w_normal

    def test_text_cleanup_safe_when_no_texture(self):
        """cleanup() is safe to call before any texture exists."""
        node = self.create_node()
        node.cleanup()  # should not raise
        assert node._gpu_texture is None


# ── Noise Node Tests ────────────────────────────────────────────────────────

class TestNoiseNode(NodeTestBase):
    """Tests for NoiseNode - noise generator."""

    node_type = "noise"
    node_class = NoiseNode

    def test_noise_type_parameter(self):
        """Noise type 参数."""
        node = self.create_node()
        if "noise_type" in node.parameters:
            for noise_type in ["gaussian", "uniform", "perlin"]:
                try:
                    test_node = self.create_node(params={"noise_type": noise_type})
                except:
                    pass

    def test_intensity_parameter(self):
        """Intensity 参数."""
        node = self.create_node()
        if "intensity" in node.parameters:
            node.set_parameter("intensity", 0.5)


# ── Ramp Node Tests ─────────────────────────────────────────────────────────

class TestRampNode(NodeTestBase):
    """Tests for RampNode - ramp/gradient generator."""

    node_type = "ramp"
    node_class = RampNode

    def test_ramp_type_parameter(self):
        """Ramp type 参数."""
        node = self.create_node()
        if "ramp_type" in node.parameters:
            for ramp_type in ["linear", "radial"]:
                try:
                    test_node = self.create_node(params={"ramp_type": ramp_type})
                except:
                    pass

    def test_start_end_colors(self):
        """Start/End color 参数."""
        node = self.create_node()
        if "start_color" in node.parameters:
            node.set_parameter("start_color", "#000000")
        if "end_color" in node.parameters:
            node.set_parameter("end_color", "#FFFFFF")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
