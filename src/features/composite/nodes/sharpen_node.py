"""
Sharpen Node - Image sharpening using unsharp mask.

Enhances edge detail by subtracting a blurred version from the original.
Classic technique for making images appear sharper.

Commonly used for:
  - Enhancing image detail
  - Pre-processing for print or display
  - Counteracting blur from downsampling
"""

from typing import Dict, Optional, Any
from .base_node import Node, Port, TextureResult, ParamDef

if __name__ != "__main__":
    from src.features.composite.core.format import BBox


# Channel modes
CHANNEL_MODES = ["all", "rgb", "luminance"]


class SharpenNode(Node):
    """Image sharpening using unsharp mask."""

    HELP_TEXT = """非锐化掩模锐化。增强图像细节和边缘。
输入：image（图像）
输出：锐化后的图像（RGBA）
参数：
  - amount: 锐化强度（0.0-2.0）
  - radius: 锐化半径（0.5-10.0）
  - threshold: 阈值（避免放大噪声）
注意：过度锐化会产生光晕和噪声。建议小幅度多次应用。"""
    DISPLAY_NAME = "Sharpen"

    node_type = "sharpen"
    node_title = "Sharpen"
    header_color = "#9e7e4a"  # Orange-brown

    # Metadata
    SEARCH_ALIASES = ["sharpen", "锐化", "锐度", "unsharp", "detail", "细节"]
    DESCRIPTION = "使用反锐化掩模增强图像边缘细节"
    TAGS = ["filter", "image"]

    PARAM_DEFS = [
        ParamDef("amount", "float", 1.0, min_val=0.0, max_val=10.0),
        ParamDef("radius", "float", 1.0, min_val=0.0, max_val=50.0),
        ParamDef("threshold", "float", 0.0, min_val=0.0, max_val=10.0),
        ParamDef("channels", "option", "all", labels=CHANNEL_MODES),
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
            "amount": 1.0,
            "radius": 1.0,
            "threshold": 0.0,
            "channels": "all",
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        """Output bbox matches the image input."""
        return input_bboxes.get("image")

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        """Declare tile padding requirement for seamless boundaries.

        Sharpen uses unsharp-mask technique which involves a blur.
        The blur radius determines the padding needed.
        Typically radius * 2 for the kernel footprint.

        Args:
            params: Node parameters dictionary.

        Returns:
            Padding in pixels (radius * 2, minimum 1).
        """
        radius = params.get("radius", 1.0)
        return max(1, int(radius * 2))

    def requires_roi_expansion(self, params: Dict[str, Any]) -> int:
        """Legacy API - delegates to _tile_padding()."""
        return self._tile_padding(params)

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                      resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        image_input = inputs.get("image")

        if image_input is None or not image_input.is_valid:
            return {}

        amount = self.parameters.get("amount", 1.0)
        radius = self.parameters.get("radius", 1.0)
        threshold = self.parameters.get("threshold", 0.0)
        channels = self.parameters.get("channels", "all")

        return {
            "image": TextureResult(
                texture_id=-1,
                width=image_input.width,
                height=image_input.height,
            ),
            "_effect_type": "sharpen",
            "_effect_input": image_input,
            "_effect_params": {
                "amount": amount,
                "radius": radius,
                "threshold": threshold,
                "channels": channels,
            },
        }
