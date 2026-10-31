"""
Premult Node - Multiplies RGB channels by alpha (straight → premultiplied).

In NUKE, this is the "Premult" node. It converts unpremultiplied (straight)
alpha images to premultiplied, which is required before most compositing
operations (blur, transform, merge/over, etc.).
"""

from typing import Any, Dict, Optional
from .base_node import Node, Port, TextureResult


class PremultNode(Node):
    """Convert straight alpha to premultiplied alpha."""

    HELP_TEXT = """RGB × Alpha：将直 alpha 转换为预乘 alpha。
输入：image（图像）
输出：预乘结果（RGBA）
参数：无
注意：NUKE/Fusion 风格的合成操作需要预乘 alpha。此节点用于确保输入格式正确。"""
    DISPLAY_NAME = "Premult"

    node_type = "premult"
    node_title = "Premult"
    header_color = "#5a9e6f"  # Green

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

        # Already premultiplied — no-op passthrough
        if image_input.alpha_premultiplied is True:
            return {"result": image_input}

        # Return effect marker for the renderer to dispatch GPU premult
        return {
            "result": TextureResult(
                texture_id=-1,
                width=image_input.width,
                height=image_input.height,
            ),
            "_effect_type": "premult",
            "_effect_input": image_input,
            "_effect_params": {},
        }
