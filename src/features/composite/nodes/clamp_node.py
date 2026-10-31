"""
Clamp Node - Clamps pixel values to a specified range.

This node explicitly clamps values to prevent overflow in HDR workflows.
Unlike most nodes, Clamp is explicit about its clamping behavior.

Note: Other nodes should NOT silently clamp HDR values.
This node is for cases where clamping is explicitly desired.

Modes:
  - min_max: Clamp to [min, max] range
  - histogram: Clamp based on histogram analysis
  - legal: Clamp to legal broadcast range (e.g., 0.0-1.0 for some formats)
"""

from typing import Dict, Optional, Any
from .base_node import Node, Port, TextureResult, ParamDef

if __name__ != "__main__":
    from src.features.composite.core.format import BBox


# Clamp modes
CLAMP_MODES = ["min_max", "histogram", "legal"]


class ClampNode(Node):
    """Explicitly clamps pixel values to a specified range."""

    HELP_TEXT = """显式像素值钳制。防止 HDR 值溢出。
输入：image（图像）
输出：钳制后的图像（RGBA）
参数：
  - mode: 钳制模式（min_max/histogram/legal）
  - min_val, max_val: 钳制范围
注意：
  - mode=min_max: 简单钳制到 [min_val, max_val]
  - mode=legal: 钳制到法律范围（0.0-1.0 for video）
  - HDR 工作流中用于防止溢出到显示器范围"""
    DISPLAY_NAME = "Clamp"

    node_type = "clamp"
    node_title = "Clamp"
    header_color = "#5a8e7e"  # Teal

    PARAM_DEFS = [
        ParamDef("mode", "option", "min_max", labels=CLAMP_MODES),
        ParamDef("min", "float", 0.0, min_val=-10.0, max_val=100.0),
        ParamDef("max", "float", 1.0, min_val=-10.0, max_val=100.0),
        # AUD-64019: ptype 用规范类型表的 "str"（"string" 未注册）
        ParamDef("channels", "str", "rgba"),
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
            "mode": "min_max",
            "min": 0.0,
            "max": 1.0,
            "channels": "rgba",
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        """Output bbox matches input."""
        return input_bboxes.get("image")

    def compute_input_roi(self, output_roi: "BBox",
                          params: Dict[str, Any],
                          input_bboxes: Optional[Dict[str, "BBox"]] = None) -> Dict[str, "BBox"]:
        """ROI passes through directly."""
        return {"image": output_roi}

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                      resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        image_input = inputs.get("image")
        if image_input is None or not image_input.is_valid:
            return {}

        mode = self.parameters.get("mode", "min_max")
        min_val = self.parameters.get("min", 0.0)
        max_val = self.parameters.get("max", 1.0)
        channels = self.parameters.get("channels", "rgba")

        return {
            "result": TextureResult(
                texture_id=-1,
                width=image_input.width,
                height=image_input.height,
            ),
            "_effect_type": "clamp",
            "_effect_input": image_input,
            "_effect_params": {
                "mode": mode,
                "min": min_val,
                "max": max_val,
                "channels": channels,
            },
        }
