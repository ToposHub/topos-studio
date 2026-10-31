"""
LumaKeyer Node - Keying based on luminance/brightness.

Generates a matte based on the luminance values of the input image.
Useful for separating bright or dark elements from a background.

Commonly used for:
  - Separating bright objects from dark backgrounds
  - Creating matte from contrast
  - Simple keying without color information
"""

from typing import Dict, Optional, Any
from .base_node import Node, Port, TextureResult, ParamDef

if __name__ != "__main__":
    from src.features.composite.core.format import BBox


# Output modes
OUTPUT_MODES = ["result", "matte"]


class LumaKeyerNode(Node):
    """Keying based on luminance values."""

    # AUD-7000: HELP_TEXT 参数名与 PARAM_DEFS 一一对应（此前引用了不存在的
    # low_threshold/high_threshold，误导用户与二次开发者）。
    HELP_TEXT = """亮度抠像。基于亮度值生成 alpha matte。
输入：image（图像）
输出：抠像结果（RGBA）
参数：
  - black_point: 黑点（亮度低于此值完全透明）
  - white_point: 白点（亮度高于此值完全不透明）
  - softness: 过渡软度
  - invert: 反转 matte（亮区透明、暗区不透明）
  - output_mode: 输出 result（抠像结果）或 matte（纯 alpha matte）
注意：适合从灰度图创建透明区域。例如黑色背景 → 完全透明。"""
    DISPLAY_NAME = "Luma Keyer"

    node_type = "luma_keyer"
    node_title = "LumaKeyer"
    header_color = "#8e8e4a"  # Yellow-green

    # Metadata
    SEARCH_ALIASES = ["lumakey", "luma", "亮度", "亮度键", "抠像", "brightness"]
    DESCRIPTION = "基于亮度值的抠像"
    TAGS = ["keyer", "matte"]

    PARAM_DEFS = [
        ParamDef("black_point", "float", 0.0, min_val=0.0, max_val=1.0),
        ParamDef("white_point", "float", 1.0, min_val=0.0, max_val=1.0),
        ParamDef("softness", "float", 0.0, min_val=0.0, max_val=1.0),
        ParamDef("invert", "bool", False),
        ParamDef("output_mode", "option", "result", labels=OUTPUT_MODES),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("image", "input", "image"),
        ]
        self.outputs = [
            Port("result", "output", "image")
        ]
        self.parameters = {
            "black_point": 0.0,
            "white_point": 1.0,
            "softness": 0.0,
            "invert": False,
            "output_mode": "result",
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        """Output bbox matches the image input."""
        return input_bboxes.get("image")

    def compute_input_roi(self, output_roi: "BBox",
                          params: Dict[str, Any],
                          input_bboxes: Optional[Dict[str, "BBox"]] = None) -> Dict[str, "BBox"]:
        """Image input needs the full ROI."""
        return {"image": output_roi}

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                      resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        image_input = inputs.get("image")

        if image_input is None or not image_input.is_valid:
            return {}

        black_point = self.parameters.get("black_point", 0.0)
        white_point = self.parameters.get("white_point", 1.0)
        softness = self.parameters.get("softness", 0.0)
        invert = self.parameters.get("invert", False)
        output_mode = self.parameters.get("output_mode", "result")

        return {
            "result": TextureResult(
                texture_id=-1,
                width=image_input.width,
                height=image_input.height,
            ),
            "_effect_type": "luma_keyer",
            "_effect_input": image_input,
            "_effect_params": {
                "black_point": black_point,
                "white_point": white_point,
                "softness": softness,
                "invert": invert,
                "output_mode": output_mode,
            },
        }
