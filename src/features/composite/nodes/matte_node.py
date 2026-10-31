"""
Matte Node - Generate luminance-based mattes.

Creates a matte (mask) based on the luminance values of the input image.
Useful for separating bright or dark elements, or for isolating specific
tonal ranges.

Features:
  - Luminance matte based on brightness thresholds
  - Soft edge transitions (linear interpolation)
  - Upper matte (bright regions) and lower matte (dark regions)
  - Invert option for reversed matte
  - Output mode: matte (grayscale) or result (premultiplied)
"""

from typing import Dict, Optional, Any
from .base_node import Node, Port, TextureResult, ParamDef

if __name__ != "__main__":
    from src.features.composite.core.format import BBox


# Output modes
OUTPUT_MODES = ["matte", "result"]


class MatteNode(Node):
    """Generate a luminance-based matte for masking operations.

    This node creates a matte based on the luminance (brightness) values
    of the input image. Pixels below low_threshold become transparent,
    pixels above high_threshold become white (opaque), and pixels in
    between smoothly transition based on the softness parameters.

    The matte can be used downstream as a mask input for Merge nodes,
    or as an alpha channel via Shuffle/CopyChannel.
    """

    node_type = "matte"
    node_title = "Matte"
    header_color = "#6a7a8a"  # Blue-gray

    # Metadata
    SEARCH_ALIASES = ["matte", "mask", "遮罩", "蒙版", "遮板", "luminance", "brightness"]
    DESCRIPTION = "生成基于亮度范围的遮罩"
    TAGS = ["mask", "generator", "matte"]
    IS_PLACEHOLDER = False

    PARAM_DEFS = [
        ParamDef("low_threshold", "float", 0.0, min_val=0.0, max_val=1.0, step=0.01,
                 group="Threshold"),
        ParamDef("high_threshold", "float", 1.0, min_val=0.0, max_val=1.0, step=0.01,
                 group="Threshold"),
        ParamDef("low_softness", "float", 0.1, min_val=0.0, max_val=0.5, step=0.01,
                 group="Softness"),
        ParamDef("high_softness", "float", 0.1, min_val=0.0, max_val=0.5, step=0.01,
                 group="Softness"),
        ParamDef("invert", "bool", False, group="Options"),
        ParamDef("output_mode", "option", "matte", labels=OUTPUT_MODES, group="Output"),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("image", "input", "image"),
        ]
        self.outputs = [
            Port("result", "output", "image"),
        ]
        self.parameters = {
            "low_threshold": 0.0,
            "high_threshold": 1.0,
            "low_softness": 0.1,
            "high_softness": 0.1,
            "invert": False,
            "output_mode": "matte",
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
        """Evaluate the matte node.

        Returns an effect marker dict that will be resolved by
        GraphEvaluatorMixin._resolve_output() and dispatched to
        _apply_matte() for GPU rendering.

        Args:
            inputs: Input texture results (must have "image" port).
            ctx: ModernGL context (unused in marker mode).
            fb_manager: FramebufferManager (unused in marker mode).
            resource_pool: GPU resource pool (unused in marker mode).
            render_mode: "preview", "full", or "export" (unused).

        Returns:
            Dict with output TextureResult placeholder and effect marker keys.
        """
        image_input = inputs.get("image")

        if image_input is None or not image_input.is_valid:
            return {}

        # Get parameters (validated in shader)
        low_threshold = self.parameters.get("low_threshold", 0.0)
        high_threshold = self.parameters.get("high_threshold", 1.0)
        low_softness = self.parameters.get("low_softness", 0.1)
        high_softness = self.parameters.get("high_softness", 0.1)
        invert = self.parameters.get("invert", False)
        output_mode = self.parameters.get("output_mode", "matte")

        # Return effect marker for deferred GPU rendering
        # The marker protocol is defined in base_node.py header comment
        return {
            "result": TextureResult(
                texture_id=-1,  # Placeholder, will be replaced by _resolve_output()
                width=image_input.width,
                height=image_input.height,
            ),
            "_effect_type": "matte",
            "_effect_input": image_input,
            "_effect_params": {
                "low_threshold": low_threshold,
                "high_threshold": high_threshold,
                "low_softness": low_softness,
                "high_softness": high_softness,
                "invert": invert,
                "output_mode": output_mode,
            },
        }