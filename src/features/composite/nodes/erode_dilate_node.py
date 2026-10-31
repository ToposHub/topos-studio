"""
ErodeDilate Node - Morphological operations for matte cleanup.

Erode shrinks bright areas, dilate expands them.
Essential for cleaning up noisy mattes and expanding/contracting shapes.

Commonly used for:
  - Matte cleanup
  - Expanding or shrinking masks
  - Removing small specks
  - Filling small holes in mattes
"""

from typing import Dict, Optional, Any
from .base_node import Node, Port, TextureResult, ParamDef

if __name__ != "__main__":
    from src.features.composite.core.format import BBox


# Operation modes
OPERATION_MODES = ["erode", "dilate"]

# Edge modes
EDGE_MODES = ["extend", "reflect", "wrap"]

# Channel modes
CHANNEL_MODES = ["alpha", "all"]


class ErodeDilateNode(Node):
    """Morphological erode/dilate operations for matte cleanup."""

    HELP_TEXT = """形态学腐蚀/膨胀操作。用于 matte 清理和边缘处理。
输入：image（图像）
输出：处理后的图像（RGBA）
参数：
  - mode: 模式（erode/dilate）
  - radius: 操作半径（1-20）
注意：
  - erode: 缩小亮区域（收缩 matte 边缘）
  - dilate: 扩大亮区域（扩展 matte 边缘）
  - 适合清理抠像 matte、去除小噪点"""
    DISPLAY_NAME = "Erode Dilate"

    node_type = "erode_dilate"
    node_title = "ErodeDilate"
    header_color = "#7e5a9e"  # Purple

    # Metadata
    SEARCH_ALIASES = ["erode", "dilate", "侵蚀", "膨胀", "morphology", "形态学", "matte", "edge"]
    DESCRIPTION = "形态学操作（侵蚀/膨胀）用于遮罩清理"
    TAGS = ["filter", "matte"]

    PARAM_DEFS = [
        ParamDef("operation", "option", "erode", labels=OPERATION_MODES),
        ParamDef("size", "int", 3, min_val=1, max_val=50),
        ParamDef("iterations", "int", 1, min_val=1, max_val=10),
        ParamDef("edge_mode", "option", "extend", labels=EDGE_MODES),
        ParamDef("channels", "option", "alpha", labels=CHANNEL_MODES),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("image", "input", "image"),
        ]
        self.outputs = [
            Port("image", "output", "image")
        ]
        self.parameters = {
            "operation": "erode",
            "size": 3,
            "iterations": 1,
            "edge_mode": "extend",
            "channels": "alpha",
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        """Output bbox matches the image input."""
        return input_bboxes.get("image")

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        """Declare tile padding requirement for seamless boundaries.

        Erode/dilate uses a square kernel of size `size` applied `iterations`
        times. Padding needed per pass = size // 2 + 1; across all passes
        the total border footprint scales with iterations.
        """
        size = int(params.get("size", 3))
        iterations = max(1, int(params.get("iterations", 1)))
        return (size // 2 + 1) * iterations

    def requires_roi_expansion(self, params: Dict[str, Any]) -> int:
        """Legacy API - delegates to _tile_padding()."""
        return self._tile_padding(params)

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                      resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        image_input = inputs.get("image")

        if image_input is None or not image_input.is_valid:
            return {}

        operation = self.parameters.get("operation", "erode")
        size = max(1, int(self.parameters.get("size", 3)))
        iterations = max(1, int(self.parameters.get("iterations", 1)))
        edge_mode = self.parameters.get("edge_mode", "extend")
        channels = self.parameters.get("channels", "alpha")

        # Preview mode: cap total work for interactive response.
        if render_mode == "preview":
            if size > 8:
                size = max(2, size // 2)
            if iterations > 3:
                iterations = 3

        return {
            "image": TextureResult(
                texture_id=-1,
                width=image_input.width,
                height=image_input.height,
            ),
            "_effect_type": "erode_dilate",
            "_effect_input": image_input,
            "_effect_params": {
                "operation": operation,
                "size": size,
                "iterations": iterations,
                "edge_mode": edge_mode,
                "channels": channels,
            },
        }
