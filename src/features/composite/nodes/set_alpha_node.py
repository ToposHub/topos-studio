"""
SetAlpha Node - Sets or replaces the alpha channel.

Modes:
  - constant: Set alpha to a constant value
  - luma: Set alpha based on luminance of input
  - r/g/b/a: Copy specific channel to alpha

Commonly used for:
  - Generating alpha from grayscale
  - Quick alpha adjustments
  - Channel manipulation
"""

from typing import Dict, Optional, Any
from .base_node import Node, Port, TextureResult, ParamDef

if __name__ != "__main__":
    from src.features.composite.core.format import BBox


# Alpha source modes
ALPHA_MODES = ["constant", "luma", "r", "g", "b", "a"]


class SetAlphaNode(Node):
    """Sets or replaces the alpha channel."""

    # AUD-7401: 参数名与 PARAM_DEFS 一致（此前写作 constant_value，实际
    # 参数名是 value——按旧文档引用会恒取默认值）。
    HELP_TEXT = """设置/替换 alpha 通道。支持常量、亮度或 RGB 通道作为 alpha 源。
输入：image（图像）、mask（可选 alpha 源）
输出：设置 alpha 后的图像（RGBA）
参数：
  - mode: 模式（constant/luma/r/g/b/a）
  - value: 常量 alpha 值（当 mode=constant）
注意：
  - mode=luma: 使用图像亮度作为 alpha（灰度 → 透明）
  - mode=r/g/b/a: 使用指定通道作为 alpha
  - 适合从亮度图创建透明蒙版"""
    DISPLAY_NAME = "Set Alpha"

    node_type = "set_alpha"
    node_title = "SetAlpha"
    header_color = "#7e9e5a"  # Olive green

    # Metadata
    SEARCH_ALIASES = ["setalpha", "alpha", "透明", "透明度", "opacity", "透明通道"]
    DESCRIPTION = "设置或替换alpha通道"
    TAGS = ["channel", "alpha"]

    PARAM_DEFS = [
        ParamDef("mode", "option", "constant", labels=ALPHA_MODES),
        ParamDef("value", "float", 1.0, min_val=0.0, max_val=1.0),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("rgba", "input", "image"),
            Port("mask", "input", "image"),
        ]
        self.outputs = [
            Port("rgba", "output", "image")
        ]
        self.parameters = {
            "mode": "constant",
            "value": 1.0,
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        """Output bbox matches the rgba input."""
        return input_bboxes.get("rgba")

    def compute_input_roi(self, output_roi: "BBox",
                          params: Dict[str, Any],
                          input_bboxes: Optional[Dict[str, "BBox"]] = None) -> Dict[str, "BBox"]:
        """Both inputs need the same ROI."""
        return {"rgba": output_roi, "mask": output_roi}

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                      resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        rgba_input = inputs.get("rgba")
        mask_input = inputs.get("mask")

        if rgba_input is None or not rgba_input.is_valid:
            return {}

        mode = self.parameters.get("mode", "constant")
        value = self.parameters.get("value", 1.0)

        return {
            "rgba": TextureResult(
                texture_id=-1,
                width=rgba_input.width,
                height=rgba_input.height,
            ),
            "_effect_type": "set_alpha",
            "_effect_input": rgba_input,
            "_effect_mask": mask_input,
            "_effect_params": {
                "mode": mode,
                "value": value,
            },
        }
