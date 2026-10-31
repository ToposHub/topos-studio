"""
COLOR Node - DEPRECATED: Bridge to the Color page's rendering result.

DEPRECATED: This node is kept for backward compatibility with old projects.
New graphs should use ToposColorNode instead, which reads ColorStack data
directly instead of depending on the Color page's current output texture.

See topos_color_node.py for the replacement.

This node has no inputs and one output ("result").
It outputs the texture ID from the Color page's AsyncRenderer.
"""

from typing import Any, Dict, Optional
from .base_node import Node, Port, TextureResult
from src.features.composite.core.format import ImageFormat, BBox
from src.features.composite.core.color_space import ColorSpace


class COLORNode(Node):
    """Outputs the Color page's rendered result as a texture."""

    HELP_TEXT = """输入彩色画布。将 Color 页面的渲染结果作为节点图的输入源。适用于从颜色校正页面导入已处理的图像。
输入：无
输出：彩色图像（RGBA）
参数：无
注意：此节点桥接 Color 页面与 Composite 节点图，格式由 Color 页面设置决定。"""
    DISPLAY_NAME = "Color Input"

    node_type = "color_input"
    node_title = "COLOR"
    header_color = "#cc6600"  # Orange to match app accent

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.outputs = [
            Port("result", "output", "image")
        ]
        self._cached_texture_id: Optional[int] = None
        self._cached_width: int = 0
        self._cached_height: int = 0
        self._revision: int = 0

    def set_color_result(self, texture_id: Optional[int], width: int = 0, height: int = 0,
                         revision: int = 0):
        """Called when entering Composite page with the Color page's output texture."""
        changed = (self._cached_texture_id != texture_id
                   or self._cached_width != width
                   or self._cached_height != height
                   or self._revision != revision)
        if changed:
            self._cached_texture_id = texture_id
            self._cached_width = width
            self._cached_height = height
            self._revision = revision
            self._dirty = True

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                      resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        # Invalid texture (None, 0, or negative) should return empty dict
        if self._cached_texture_id is None or self._cached_texture_id <= 0:
            return {}

        # Invalid dimensions should also return empty dict
        if self._cached_width <= 0 or self._cached_height <= 0:
            return {}

        fmt = ImageFormat(
            width=self._cached_width,
            height=self._cached_height,
            name="Color Output",
        )
        bbox = BBox.full_frame(self._cached_width, self._cached_height)

        return {
            "result": TextureResult(
                texture_id=self._cached_texture_id,
                width=self._cached_width,
                height=self._cached_height,
                revision=self._revision,
                format=fmt,
                bbox=bbox,
                alpha_premultiplied=False,  # Color page outputs straight alpha
                # The Color page's ColorOutput texture holds sRGB-encoded
                # (gamma-domain) values, NOT scene-linear.
                #
                # For a normal sRGB image (JPEG/PNG) with the RAW module off,
                # the page neither decodes (u_is_srgb_source=False) nor encodes
                # (u_raw_gamma=0) — it is a pure bypass. So the texture carries
                # the same sRGB encoding the pixels had on disk.
                #
                # Tagging this SRGB (the truth) is what makes the composite
                # engine's colour management work: the unified IDT hook
                # (_apply_idt_to_source_outputs) decodes sRGB→linear once at the
                # graph boundary, the graph runs in linear light, and the
                # viewer's ODT encodes linear→sRGB once for display — a single
                # round trip that reproduces the original appearance.
                #
                # A previous LINEAR tag skipped the IDT decode, so the sRGB
                # values reached the viewer still sRGB-encoded and the ODT then
                # encoded them a SECOND time → double gamma → the composite
                # page looked noticeably brighter than the Color page.
                #
                # The merge-darkening concern that motivated the LINEAR tag is
                # already handled by _auto_align_color_spaces(), which decodes
                # every sRGB merge input to linear (including the both-sRGB
                # case), so an SRGB-tagged color_input merging with a LINEAR
                # Constant now aligns correctly in linear light.
                color_space=ColorSpace.SRGB,
            )
        }

    def to_dict(self) -> dict:
        # Don't serialize texture ID - it's runtime state
        return super().to_dict()
