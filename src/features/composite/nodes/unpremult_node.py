"""
Unpremult Node - Divides RGB channels by alpha (premultiplied → straight).

In NUKE, this is the "Unpremult" node. It converts premultiplied alpha images
to straight (unpremultiplied), typically used before color corrections that
should not affect alpha-premultiplied RGB (e.g. Grade, ColorCorrect).
"""

from typing import Any, Dict, Optional
from .base_node import Node, Port, TextureResult


class UnpremultNode(Node):
    """Convert premultiplied alpha to straight alpha."""

    HELP_TEXT = """RGB ÷ Alpha：将预乘 alpha 转换为直 alpha。
输入：image（图像）
输出：直 alpha 结果（RGBA）
参数：无
注意：某些滤镜操作（如模糊）在直 alpha 空间效果更好。此节点用于格式转换。"""
    DISPLAY_NAME = "Unpremult"

    node_type = "unpremult"
    node_title = "Unpremult"
    header_color = "#6fa85a"  # Green variant

    PARAM_DEFS = []

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("image", "input", "image")
        ]
        self.outputs = [
            Port("result", "output", "image")
        ]

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                      resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        image_input = inputs.get("image")
        if not image_input or not image_input.is_valid:
            return {}

        # Already straight — no-op passthrough
        if image_input.alpha_premultiplied is False:
            return {"result": image_input}

        # Return effect marker for the renderer to dispatch GPU unpremult
        return {
            "result": TextureResult(
                texture_id=-1,
                width=image_input.width,
                height=image_input.height,
            ),
            "_effect_type": "unpremult",
            "_effect_input": image_input,
            "_effect_params": {},
        }
