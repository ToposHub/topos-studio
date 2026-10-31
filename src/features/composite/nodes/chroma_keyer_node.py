"""
ChromaKeyer Node - Greenscreen/Bluescreen keying.

Uses screen color (typically green or blue) to generate a matte.
Supports edge refinement and despill.

Commonly used for:
  - Removing colored backgrounds (greenscreen, bluescreen)
  - Preserving edge detail with edge refinement
  - Removing color spill from reflected surfaces
"""

from typing import Dict, Optional, Any
from .base_node import Node, Port, TextureResult, ParamDef

if __name__ != "__main__":
    from src.features.composite.core.format import BBox


# Output modes
OUTPUT_MODES = ["result", "matte"]


class ChromaKeyerNode(Node):
    """Chroma keying based on screen color."""

    HELP_TEXT = """色度抠像（绿幕/蓝幕）。基于颜色容差生成 alpha matte。
输入：image（图像）
输出：抠像结果（RGBA）
参数：
  - key_color: 关键色（RGB）
  - tolerance: 容差（0.0-1.0）
  - softness: 软边程度（0.0-1.0）
  - despill: 去溢色强度
  - edge_softness: 边缘柔化
注意：
  - 适合绿幕/蓝幕抠像
  - tolerance 控制抠像范围
  - despill 减少关键色在主体上的反射"""
    DISPLAY_NAME = "Chroma Keyer"

    node_type = "chroma_keyer"
    node_title = "ChromaKeyer"
    header_color = "#4a8e4a"  # Green

    # Metadata
    SEARCH_ALIASES = ["chromakey", "chroma", "green", "blue", "greenscreen", "bluescreen", "绿幕", "抠像", "色度键"]
    DESCRIPTION = "基于屏幕颜色的色度抠像"
    TAGS = ["keyer", "matte"]

    PARAM_DEFS = [
        ParamDef("screen_color_r", "float", 0.0, min_val=0.0, max_val=1.0),
        ParamDef("screen_color_g", "float", 1.0, min_val=0.0, max_val=1.0),
        ParamDef("screen_color_b", "float", 0.0, min_val=0.0, max_val=1.0),
        ParamDef("tolerance", "float", 0.3, min_val=0.0, max_val=1.0),
        ParamDef("softness", "float", 0.1, min_val=0.0, max_val=1.0),
        ParamDef("despill", "bool", True),
        ParamDef("edge_width", "float", 2.0, min_val=0.0, max_val=10.0),
        ParamDef("output_mode", "option", "result", labels=OUTPUT_MODES),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("image", "input", "image"),
            Port("screen", "input", "image"),
        ]
        self.outputs = [
            Port("result", "output", "image")
        ]
        self.parameters = {
            "screen_color_r": 0.0,
            "screen_color_g": 1.0,
            "screen_color_b": 0.0,
            "tolerance": 0.3,
            "softness": 0.1,
            "despill": True,
            "edge_width": 2.0,
            "output_mode": "result",
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        """Output bbox matches the image input."""
        return input_bboxes.get("image")

    def compute_input_roi(self, output_roi: "BBox",
                          params: Dict[str, Any],
                          input_bboxes: Optional[Dict[str, "BBox"]] = None) -> Dict[str, "BBox"]:
        """Both inputs need the same ROI."""
        return {"image": output_roi, "screen": output_roi}

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                      resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        image_input = inputs.get("image")

        if image_input is None or not image_input.is_valid:
            return {}

        # 确保颜色值在 0.0-1.0 范围内，防止 GPU shader 异常
        screen_color_r = self._get_param_clamped("screen_color_r", 0.0, 1.0)
        screen_color_g = self._get_param_clamped("screen_color_g", 0.0, 1.0)
        screen_color_b = self._get_param_clamped("screen_color_b", 0.0, 1.0)
        tolerance = self.parameters.get("tolerance", 0.3)
        softness = self.parameters.get("softness", 0.1)
        despill = self.parameters.get("despill", True)
        edge_width = self.parameters.get("edge_width", 2.0)
        output_mode = self.parameters.get("output_mode", "result")

        return {
            "result": TextureResult(
                texture_id=-1,
                width=image_input.width,
                height=image_input.height,
            ),
            "_effect_type": "chroma_keyer",
            "_effect_input": image_input,
            "_effect_params": {
                "screen_color": (screen_color_r, screen_color_g, screen_color_b),
                "tolerance": tolerance,
                "softness": softness,
                "despill": despill,
                "edge_width": edge_width,
                "output_mode": output_mode,
            },
        }
