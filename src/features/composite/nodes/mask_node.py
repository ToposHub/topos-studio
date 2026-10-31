"""
Mask Node - Generate geometric masks (rectangle, ellipse, polygon).

Creates alpha mattes from geometric shapes using GPU shader.
Supports feathering, rotation, and inversion.

Commonly used for:
  - Quick geometric masks
  - Vignette effects
  - Isolating regions
  - Complementary to RotoShape (which handles Bezier curves)

The node has two workflows:
  - With ``image`` connected: ``result`` is the input image multiplied by the
    generated shape, while ``matte`` remains available as a clean mask for a
    Merge/Grade mask input.
  - Without ``image`` connected: ``result`` is a standalone shape texture and
    ``matte`` is its canonical grayscale counterpart for Merge.mask.

Note: This node is distinct from roto_shape_node:
  - MaskNode: Fast geometric masks (rect/ellipse/polygon)
  - RotoShapeNode: Bezier curve shapes for precise rotoscoping
"""

from typing import Dict, Optional, Any, List
from .base_node import Node, Port, TextureResult, ParamDef


# Mask types.  ``circle`` is kept as a user-facing alias used by the Color
# page; the renderer treats it as an ellipse with equal width/height.
MASK_TYPES = ["rectangle", "ellipse", "circle", "polygon"]

# Output modes
OUTPUT_MODES = ["matte", "result"]


class MaskNode(Node):
    """Generate geometric masks using GPU shader.

    Supports:
      - Rectangle: SDF-based rectangular mask
      - Ellipse: SDF-based elliptical mask
      - Polygon: Regular polygon mask (n-sided)

    All shapes support rotation, feathering, and inversion.
    """

    node_type = "mask"
    node_title = "Mask"
    header_color = "#7a8a6a"  # Sage green

    # Metadata for UI search
    SEARCH_ALIASES = ["mask", "蒙版", "rectangle", "ellipse", "polygon", "几何蒙版"]
    DESCRIPTION = "生成几何蒙版（矩形/椭圆/多边形），支持羽化和旋转"
    TAGS = ["generator", "mask", "matte"]

    # Not a placeholder anymore
    IS_PLACEHOLDER = False

    PARAM_DEFS = [
        # Mask type
        ParamDef("mask_type", "option", "rectangle", labels=MASK_TYPES,
                 group="Shape"),
        # Center position (normalized 0-1)
        ParamDef("center_x", "float", 0.5, min_val=0.0, max_val=1.0, step=0.01,
                 group="Position", animated=True),
        ParamDef("center_y", "float", 0.5, min_val=0.0, max_val=1.0, step=0.01,
                 group="Position", animated=True),
        # Size (normalized 0-2, can extend beyond frame)
        ParamDef("width", "float", 0.5, min_val=0.0, max_val=2.0, step=0.01,
                 group="Size", animated=True),
        ParamDef("height", "float", 0.5, min_val=0.0, max_val=2.0, step=0.01,
                 group="Size", animated=True),
        # Rotation (degrees)
        ParamDef("rotation", "float", 0.0, min_val=-180.0, max_val=180.0, step=1.0,
                 group="Transform", animated=True),
        # Feather (normalized 0-0.5)
        ParamDef("feather", "float", 0.0, min_val=0.0, max_val=0.5, step=0.01,
                 group="Edge"),
        # Invert mask
        ParamDef("invert", "bool", False, group="Edge"),
        # Output mode: matte = alpha only, result = white matte
        ParamDef("output_mode", "option", "matte", labels=OUTPUT_MODES,
                 group="Output"),
        # Polygon specific: number of sides
        ParamDef("sides", "int", 5, min_val=3, max_val=32, group="Polygon"),
        # Output dimensions (for standalone use)
        ParamDef("output_width", "int", 1920, min_val=1, max_val=16384, hidden=True),
        ParamDef("output_height", "int", 1080, min_val=1, max_val=16384, hidden=True),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        # Optional image input.  The node remains a generator when this port
        # is empty, but becomes a mask effect when an upstream image is wired.
        self.inputs: List[Port] = [
            Port("image", "input", "image"),
        ]
        self.outputs: List[Port] = [
            # Keep result first so normal node insertion/preview follows the
            # visible workflow output.  ``matte`` is the explicit mask output.
            Port("result", "output", "image"),
            Port("matte", "output", "image"),
        ]
        # Default parameters
        self.parameters: Dict[str, Any] = {
            "mask_type": "rectangle",
            "center_x": 0.5,
            "center_y": 0.5,
            "width": 0.5,
            "height": 0.5,
            "rotation": 0.0,
            "feather": 0.0,
            "invert": False,
            # A standalone shape is most useful as a visible white matte.
            "output_mode": "result",
            "sides": 5,
            "output_width": 1920,
            "output_height": 1080,
        }

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                     resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        """Generate mask texture using GPU shader.

        Returns a marker dict for the renderer to execute the shader.
        """
        image_input = inputs.get("image")
        has_image_input = image_input is not None and image_input.is_valid

        # Follow the connected image's canvas.  When disconnected, retain the
        # explicit standalone output dimensions for shape generation.
        width = max(1, int(image_input.width)) if has_image_input else max(
            1, int(self.parameters.get("output_width", 1920)))
        height = max(1, int(image_input.height)) if has_image_input else max(
            1, int(self.parameters.get("output_height", 1080)))

        # Get parameters
        mask_type = self.parameters.get("mask_type", "rectangle")
        if mask_type == "circle":
            mask_type = "ellipse"
        center_x = self._get_param_clamped("center_x", min_val=0.0, max_val=1.0)
        center_y = self._get_param_clamped("center_y", min_val=0.0, max_val=1.0)
        width_norm = self._get_param_clamped("width", min_val=0.0, max_val=2.0)
        height_norm = self._get_param_clamped("height", min_val=0.0, max_val=2.0)
        rotation = self._get_param_clamped("rotation", min_val=-180.0, max_val=180.0)
        feather = self._get_param_clamped("feather", min_val=0.0, max_val=0.5)
        invert = self._get_param_bool("invert", default=False)
        output_mode = self.parameters.get("output_mode", "matte")
        sides = self._get_param_int("sides", default=5)
        sides = max(3, min(32, sides))  # Clamp to valid range

        # Return effect marker for GPU renderer.  The dispatcher renders the
        # alpha matte and, when an image is connected, a masked result.
        return {
            "result": TextureResult(
                texture_id=-1,
                width=width,
                height=height,
                alpha_premultiplied=True if has_image_input else False,
                color_space="linear",
            ),
            "matte": TextureResult(
                texture_id=-1,
                width=width,
                height=height,
                alpha_premultiplied=False,
                color_space="linear",
            ),
            "_effect_type": "mask",
            "_effect_input": image_input if has_image_input else None,
            "_effect_params": {
                "mask_type": mask_type,
                "center_x": center_x,
                "center_y": center_y,
                "width": width_norm,
                "height": height_norm,
                "rotation": rotation,
                "feather": feather,
                "invert": invert,
                "output_mode": output_mode,
                "sides": sides,
                "output_width": width,
                "output_height": height,
            },
        }
