"""
ColorSpace Transform Node - Convert between color spaces (sRGB ↔ Linear).

In a professional linear workflow:
  - All compositing operations (Merge, Blur, Grade, etc.) should happen in
    scene-linear space for physically correct results.
  - Source images (JPEG, PNG) are typically sRGB-encoded and need to be
    decoded to linear before compositing.
  - The final output is encoded back to sRGB for display.

This node provides explicit sRGB ↔ Linear conversion via GPU shader.
The viewer handles its own display transform independently.
"""

from typing import Any, Dict, Optional
from .base_node import Node, Port, TextureResult, ParamDef
from src.features.composite.core.color_space import ColorSpace

# Direction options — index must match shader u_direction
CS_TRANSFORM_MODES = ["srgb_to_linear", "linear_to_srgb"]


class CSTransformNode(Node):
    """Convert between sRGB and scene-linear color spaces."""

    HELP_TEXT = """色彩空间转换。sRGB ↔ Linear 空间转换。
输入：image（图像）
输出：转换后的图像（RGBA）
参数：
  - direction: 转换方向（srgb_to_linear/linear_to_srgb）
注意：
  - srgb_to_linear: 伽马解码（显示 → 工作空间）
  - linear_to_srgb: 伽马编码（工作空间 → 显示）
  - HDR 工作流中应在 linear 空间进行合成操作"""
    DISPLAY_NAME = "Cs Transform"

    node_type = "cs_transform"
    node_title = "CS Transform"
    header_color = "#b85c5c"  # Reddish

    # Metadata
    SEARCH_ALIASES = ["cstransform", "colorspace", "线性", "linear", "srgb", "色彩空间", "转换"]
    DESCRIPTION = "sRGB与线性色彩空间转换"
    TAGS = ["color", "transform"]

    PARAM_DEFS = [
        ParamDef("direction", "option", "srgb_to_linear", labels=CS_TRANSFORM_MODES),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("image", "input", "image")
        ]
        self.outputs = [
            Port("image", "output", "image")
        ]
        self.parameters = {
            "direction": "srgb_to_linear",
        }

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                      resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        image_input = inputs.get("image")
        if not image_input or not image_input.is_valid:
            return {}

        direction = self.parameters.get("direction", "srgb_to_linear")

        # No-op if already in target space
        if direction == "srgb_to_linear" and image_input.color_space == ColorSpace.LINEAR:
            return {"image": image_input}
        if direction == "linear_to_srgb" and image_input.color_space == ColorSpace.SRGB:
            return {"image": image_input}

        # Determine the target color_space for metadata
        target_cs = ColorSpace.LINEAR if direction == "srgb_to_linear" else ColorSpace.SRGB

        return {
            "image": TextureResult(
                texture_id=-1,
                width=image_input.width,
                height=image_input.height,
            ),
            "_effect_type": "cs_transform",
            "_effect_input": image_input,
            "_effect_params": {
                "direction": 0 if direction == "srgb_to_linear" else 1,
                "target_color_space": target_cs,
            },
        }
