"""
MatteCleanup Node - Clean up and refine alpha mattes.

Provides essential matte cleanup operations:
  - Erode/Dilate: Shrink or expand matte boundaries
  - Blur: Soften hard edges
  - Clamp: Restrict alpha range (black/white point)
  - Invert: Flip matte polarity
  - Levels: Adjust contrast with black/white point and gamma

Essential for:
  - Cleaning up noisy mattes
  - Removing matte halos
  - Adjusting matte contrast
  - Preparing mattes for final compositing
"""

from typing import Dict, Optional, Any
from .base_node import Node, Port, TextureResult, ParamDef

if __name__ != "__main__":
    from src.features.composite.core.format import BBox


# Operation modes
OPERATION_MODES = [
    "erode",       # Shrink bright areas
    "dilate",      # Expand bright areas
    "blur",        # Soften edges
    "clamp",       # Restrict to range
    "invert",      # Flip matte
    "levels",      # Contrast adjustment
]

# Channel modes
CHANNEL_MODES = ["alpha", "all"]


class MatteCleanupNode(Node):
    """Clean up and refine alpha mattes."""

    node_type = "matte_cleanup"
    node_title = "MatteCleanup"
    header_color = "#6a9e7e"  # Green-teal
    # The node dispatches to the existing blur/morphology/color utility
    # shaders through the common evaluator.  It is a real effect rather than
    # a pass-through placeholder.
    IS_PLACEHOLDER = False

    PARAM_DEFS = [
        ParamDef("operation", "option", "blur", labels=OPERATION_MODES),
        ParamDef("size", "int", 3, min_val=1, max_val=50),
        ParamDef("blur_amount", "float", 2.0, min_val=0.0, max_val=50.0, step=0.5),
        ParamDef("black_point", "float", 0.0, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("white_point", "float", 1.0, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("gamma", "float", 1.0, min_val=0.1, max_val=5.0, step=0.1),
        ParamDef("invert", "bool", False),
        ParamDef("channels", "option", "alpha", labels=CHANNEL_MODES),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("image", "input", "image"),
        ]
        self.outputs = [
            Port("image", "output", "image"),
        ]
        self.parameters = {
            "operation": "blur",
            "size": 3,
            "blur_amount": 2.0,
            "black_point": 0.0,
            "white_point": 1.0,
            "gamma": 1.0,
            "invert": False,
            "channels": "alpha",
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        """Output bbox matches the input matte."""
        return input_bboxes.get("image")

    def requires_roi_expansion(self, params: Dict[str, Any]) -> int:
        """Return ROI expansion margin for blur/erode/dilate.

        AUD-7006: the dispatcher (keyer_evaluator._apply_matte_cleanup) feeds
        ``blur_amount`` verbatim into the gaussian ``radius_x``/``radius_y``,
        and BlurNode's padding convention for the same semantic is the FULL
        radius (blur_node._tile_padding).  The old ``amount // 2 + 1``
        under-padded by ~half the kernel, so ROI/tiled renders darkened or
        smeared the request window's edge.
        """
        operation = params.get("operation", "blur")

        if operation == "blur":
            try:
                amount = float(params.get("blur_amount", 2.0))
            except (TypeError, ValueError):
                amount = 2.0
            return int(max(0.0, amount))
        elif operation in ("erode", "dilate"):
            try:
                size = int(params.get("size", 3))
            except (TypeError, ValueError):
                size = 3
            return size // 2 + 1

        return 0

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                      resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        """Apply matte cleanup operation."""
        image_input = inputs.get("image")

        if image_input is None or not image_input.is_valid:
            return {}

        operation = self.parameters.get("operation", "blur")
        size = self.parameters.get("size", 3)
        blur_amount = self.parameters.get("blur_amount", 2.0)
        black_point = self.parameters.get("black_point", 0.0)
        white_point = self.parameters.get("white_point", 1.0)
        gamma = self.parameters.get("gamma", 1.0)
        invert = self.parameters.get("invert", False)
        channels = self.parameters.get("channels", "alpha")

        # Return effect marker for CompositeRenderer
        return {
            "image": TextureResult(
                texture_id=-1,
                width=image_input.width,
                height=image_input.height,
            ),
            "_effect_type": "matte_cleanup",
            "_effect_input": image_input,
            "_effect_params": {
                "operation": operation,
                "size": size,
                "blur_amount": blur_amount,
                "black_point": black_point,
                "white_point": white_point,
                "gamma": gamma,
                "invert": invert,
                "channels": channels,
            },
        }
