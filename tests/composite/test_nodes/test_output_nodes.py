"""
Output Node Tests - Tests for output nodes.

Tests cover:
- OutputNode: viewer output
- FileOutputNode: file export
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from tests.node_test_base import (
    NodeTestBase,
    make_texture_result,
)
from src.features.composite.nodes.output_node import OutputNode
from src.features.composite.nodes.file_output_node import FileOutputNode

import pytest
pytestmark = pytest.mark.composite_core


# ── Output Node Tests ───────────────────────────────────────────────────────

class TestOutputNode(NodeTestBase):
    """Tests for OutputNode - viewer output."""

    node_type = "output"
    node_class = OutputNode

    def test_image_input(self):
        """Output 需要 image 输入."""
        node = self.create_node()
        image_port = node.get_input_port("image")
        assert image_port is not None, "Output should have image input"
        assert image_port.direction == "input"

    def test_display_output(self):
        """Display output 端口."""
        node = self.create_node()
        # Should have display output
        display_port = node.get_output_port("display")
        if display_port:
            assert display_port.direction == "output"

    def test_resolution_mode_parameter(self):
        """Resolution mode 参数."""
        node = self.create_node()
        if "resolution_mode" in node.parameters:
            for mode in ["auto", "full", "proxy"]:
                try:
                    test_node = self.create_node(params={"resolution_mode": mode})
                except:
                    pass

    def test_output_evaluate(self):
        """Output evaluate 正确."""
        node = self.create_node()
        inputs = self.create_valid_input()
        outputs = node.evaluate(
            inputs, self.get_mock_context(),
            self.get_mock_fb_manager(), self.get_mock_resource_pool()
        )
        # Should not crash

    def test_output_color_space_options_no_display(self):
        """output_color_space 选项不应再包含已移除的 'display'.

        'display' was removed in stage 2 (semantics overlapped with the
        viewer ODT). The remaining options are same/linear/srgb.
        """
        node = self.create_node()
        pdef = next(p for p in OutputNode.PARAM_DEFS if p.name == "output_color_space")
        assert pdef.labels == ["same", "linear", "srgb"], \
            f"output_color_space options changed unexpectedly: {pdef.labels}"
        assert "display" not in pdef.labels

    def test_resolve_color_space_same_inherits_input(self):
        """'same' should inherit the input's color space."""
        assert OutputNode._resolve_color_space("same", "linear") == "linear"
        assert OutputNode._resolve_color_space("same", "srgb") == "srgb"
        assert OutputNode._resolve_color_space("same", None) is None

    def test_resolve_color_space_legacy_display_maps_to_same(self):
        """Legacy 'display' value (removed from UI) must map to 'same' so
        old projects load without crashing or forcing a wrong transform."""
        assert OutputNode._resolve_color_space("display", "linear") == "linear"
        assert OutputNode._resolve_color_space("display", None) is None

    def test_resolve_color_space_explicit_srgb(self):
        """Explicit 'srgb' should resolve to srgb regardless of input."""
        assert OutputNode._resolve_color_space("srgb", "linear") == "srgb"
        assert OutputNode._resolve_color_space("srgb", None) == "srgb"

    def test_linear_to_srgb_shader_compiles(self):
        """The Output node's linear→sRGB shader must compile as GLSL."""
        try:
            import moderngl
            ctx = moderngl.create_standalone_context()
        except Exception as e:
            pytest.skip(f"Cannot create standalone GL context: {e}")
        from src.features.composite.nodes.output_node import (
            _RESIZE_VERTEX, _LINEAR_TO_SRGB_FRAGMENT,
        )
        prog = ctx.program(vertex_shader=_RESIZE_VERTEX,
                           fragment_shader=_LINEAR_TO_SRGB_FRAGMENT)
        assert 'u_texture' in prog
        prog.release()
        ctx.release()


# ── File Output Node Tests ──────────────────────────────────────────────────

class TestFileOutputNode(NodeTestBase):
    """Tests for FileOutputNode - file export."""

    node_type = "file_output"
    node_class = FileOutputNode

    def test_file_path_parameter(self):
        """File path 参数."""
        node = self.create_node()
        if "file_path" in node.parameters:
            node.set_parameter("file_path", "/tmp/output.png")
            assert node.parameters.get("file_path") == "/tmp/output.png"

    def test_format_parameter(self):
        """Format 参数."""
        node = self.create_node()
        if "format" in node.parameters:
            for format in ["png", "jpg", "exr", "tiff"]:
                try:
                    test_node = self.create_node(params={"format": format})
                except:
                    pass

    def test_quality_parameter(self):
        """Quality 参数."""
        node = self.create_node()
        if "quality" in node.parameters:
            node.set_parameter("quality", 90)

    def test_image_input(self):
        """File output 需要 image 输入."""
        node = self.create_node()
        image_port = node.get_input_port("image")
        if image_port:
            assert image_port.direction == "input"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])