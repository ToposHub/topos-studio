"""
Ramp Node - Generates gradient textures.

Creates linear, radial, angular, box, and diamond gradients with
configurable colors, positions, and edge behavior.

Supported ramp types (RAMP_TYPES):
  - linear: Straight gradient between two points
  - radial: Circular gradient from center outward
  - angular: Conical/angle gradient rotating around center
  - box: Four-corner gradient with bilinear interpolation
  - diamond: Diamond-shaped gradient (Manhattan distance from center)

Supported extend modes (EXTEND_MODES):
  - clamp: Clamp coordinates outside the start/end range
  - repeat: Tile the gradient outside the range
  - mirror: Mirror the gradient outside the range

Uses GPU shader for real-time gradient generation.
"""

import logging
from typing import Any, Dict, Optional
from .base_node import Node, Port, TextureResult, ParamDef

logger = logging.getLogger(__name__)


# Ramp types
RAMP_TYPES = ["linear", "radial", "angular", "box", "diamond"]

# Extend modes
EXTEND_MODES = ["clamp", "repeat", "mirror"]


class RampNode(Node):
    """Generates gradient textures using GPU shader."""

    node_type = "ramp"
    node_title = "Ramp"
    header_color = "#8a6a5a"  # Orange-ish

    # Metadata
    SEARCH_ALIASES = ["ramp", "gradient", "渐变", "linear", "radial", "angular", "渐变器"]
    DESCRIPTION = "生成渐变纹理（线性/径向/角度/菱形渐变）"
    TAGS = ["generator", "texture", "color"]

    PARAM_DEFS = [
        ParamDef("ramp_type", "option", "linear", labels=RAMP_TYPES),
        # Start point (normalized 0-1)
        ParamDef("start_x", "float", 0.0, min_val=-2.0, max_val=2.0, step=0.01),
        ParamDef("start_y", "float", 0.0, min_val=-2.0, max_val=2.0, step=0.01),
        # End point (normalized 0-1)
        ParamDef("end_x", "float", 1.0, min_val=-2.0, max_val=2.0, step=0.01),
        ParamDef("end_y", "float", 1.0, min_val=-2.0, max_val=2.0, step=0.01),
        # Start color (HDR values supported)
        ParamDef("start_r", "float", 0.0, min_val=0.0, max_val=65535.0, step=0.01),
        ParamDef("start_g", "float", 0.0, min_val=0.0, max_val=65535.0, step=0.01),
        ParamDef("start_b", "float", 0.0, min_val=0.0, max_val=65535.0, step=0.01),
        ParamDef("start_a", "float", 1.0, min_val=0.0, max_val=65535.0, step=0.01),
        # End color (HDR values supported)
        ParamDef("end_r", "float", 1.0, min_val=0.0, max_val=65535.0, step=0.01),
        ParamDef("end_g", "float", 1.0, min_val=0.0, max_val=65535.0, step=0.01),
        ParamDef("end_b", "float", 1.0, min_val=0.0, max_val=65535.0, step=0.01),
        ParamDef("end_a", "float", 1.0, min_val=0.0, max_val=65535.0, step=0.01),
        # Extend mode
        ParamDef("extend_mode", "option", "clamp", labels=EXTEND_MODES),
        # Output dimensions
        ParamDef("width", "int", 1920, min_val=1, max_val=16384, step=1),
        ParamDef("height", "int", 1080, min_val=1, max_val=16384, step=1),
        # Radial/Angular specific
        ParamDef("center_x", "float", 0.5, min_val=-2.0, max_val=2.0, step=0.01),
        ParamDef("center_y", "float", 0.5, min_val=-2.0, max_val=2.0, step=0.01),
        ParamDef("radius", "float", 0.5, min_val=0.001, max_val=2.0, step=0.01),
        ParamDef("angle_start", "float", 0.0, min_val=0.0, max_val=360.0, step=1.0),
        ParamDef("angle_end", "float", 360.0, min_val=0.0, max_val=360.0, step=1.0),
        # Box specific
        ParamDef("corner0_r", "float", 0.0, min_val=0.0, max_val=65535.0, step=0.01),
        ParamDef("corner0_g", "float", 0.0, min_val=0.0, max_val=65535.0, step=0.01),
        ParamDef("corner0_b", "float", 0.0, min_val=0.0, max_val=65535.0, step=0.01),
        ParamDef("corner0_a", "float", 1.0, min_val=0.0, max_val=65535.0, step=0.01),
        ParamDef("corner1_r", "float", 1.0, min_val=0.0, max_val=65535.0, step=0.01),
        ParamDef("corner1_g", "float", 0.0, min_val=0.0, max_val=65535.0, step=0.01),
        ParamDef("corner1_b", "float", 0.0, min_val=0.0, max_val=65535.0, step=0.01),
        ParamDef("corner1_a", "float", 1.0, min_val=0.0, max_val=65535.0, step=0.01),
        ParamDef("corner2_r", "float", 0.0, min_val=0.0, max_val=65535.0, step=0.01),
        ParamDef("corner2_g", "float", 1.0, min_val=0.0, max_val=65535.0, step=0.01),
        ParamDef("corner2_b", "float", 0.0, min_val=0.0, max_val=65535.0, step=0.01),
        ParamDef("corner2_a", "float", 1.0, min_val=0.0, max_val=65535.0, step=0.01),
        ParamDef("corner3_r", "float", 0.0, min_val=0.0, max_val=65535.0, step=0.01),
        ParamDef("corner3_g", "float", 0.0, min_val=0.0, max_val=65535.0, step=0.01),
        ParamDef("corner3_b", "float", 1.0, min_val=0.0, max_val=65535.0, step=0.01),
        ParamDef("corner3_a", "float", 1.0, min_val=0.0, max_val=65535.0, step=0.01),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.outputs = [
            Port("result", "output", "image")
        ]
        self.parameters = {
            "ramp_type": "linear",
            "start_x": 0.0,
            "start_y": 0.0,
            "end_x": 1.0,
            "end_y": 1.0,
            "start_r": 0.0,
            "start_g": 0.0,
            "start_b": 0.0,
            "start_a": 1.0,
            "end_r": 1.0,
            "end_g": 1.0,
            "end_b": 1.0,
            "end_a": 1.0,
            "extend_mode": "clamp",
            "width": 1920,
            "height": 1080,
            "center_x": 0.5,
            "center_y": 0.5,
            "radius": 0.5,
            "angle_start": 0.0,
            "angle_end": 360.0,
            # Box corners (4-corner gradient)
            "corner0_r": 0.0,
            "corner0_g": 0.0,
            "corner0_b": 0.0,
            "corner0_a": 1.0,
            "corner1_r": 1.0,
            "corner1_g": 0.0,
            "corner1_b": 0.0,
            "corner1_a": 1.0,
            "corner2_r": 0.0,
            "corner2_g": 1.0,
            "corner2_b": 0.0,
            "corner2_a": 1.0,
            "corner3_r": 0.0,
            "corner3_g": 0.0,
            "corner3_b": 1.0,
            "corner3_a": 1.0,
        }

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                     resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        """Generate gradient texture using GPU shader.

        Returns a marker dict for the renderer to execute the shader.
        """
        # Get parameters
        # AUD-7200: whitelist validation — deserialized legacy/corrupt
        # projects may carry arbitrary strings.  Without this check the
        # dispatcher silently fell back to index 0 (linear/clamp) while the
        # properties panel still showed the stored value; now the invalid
        # value is normalised to the documented default and logged.
        ramp_type = str(self.parameters.get("ramp_type", "linear"))
        if ramp_type not in RAMP_TYPES:
            logger.warning(
                "Ramp: invalid ramp_type %r; falling back to 'linear'",
                ramp_type)
            ramp_type = "linear"
        width = max(1, int(self.parameters.get("width", 1920)))
        height = max(1, int(self.parameters.get("height", 1080)))
        extend_mode = str(self.parameters.get("extend_mode", "clamp"))
        if extend_mode not in EXTEND_MODES:
            logger.warning(
                "Ramp: invalid extend_mode %r; falling back to 'clamp'",
                extend_mode)
            extend_mode = "clamp"

        # Start and end points
        start_x = float(self.parameters.get("start_x", 0.0))
        start_y = float(self.parameters.get("start_y", 0.0))
        end_x = float(self.parameters.get("end_x", 1.0))
        end_y = float(self.parameters.get("end_y", 1.0))

        # Start color
        start_r = float(self.parameters.get("start_r", 0.0))
        start_g = float(self.parameters.get("start_g", 0.0))
        start_b = float(self.parameters.get("start_b", 0.0))
        start_a = float(self.parameters.get("start_a", 1.0))

        # End color
        end_r = float(self.parameters.get("end_r", 1.0))
        end_g = float(self.parameters.get("end_g", 1.0))
        end_b = float(self.parameters.get("end_b", 1.0))
        end_a = float(self.parameters.get("end_a", 1.0))

        # Radial/Angular specific
        center_x = float(self.parameters.get("center_x", 0.5))
        center_y = float(self.parameters.get("center_y", 0.5))
        radius = max(0.001, float(self.parameters.get("radius", 0.5)))
        angle_start = float(self.parameters.get("angle_start", 0.0))
        angle_end = float(self.parameters.get("angle_end", 360.0))

        # Box corners
        corner0_r = float(self.parameters.get("corner0_r", 0.0))
        corner0_g = float(self.parameters.get("corner0_g", 0.0))
        corner0_b = float(self.parameters.get("corner0_b", 0.0))
        corner0_a = float(self.parameters.get("corner0_a", 1.0))
        corner1_r = float(self.parameters.get("corner1_r", 1.0))
        corner1_g = float(self.parameters.get("corner1_g", 0.0))
        corner1_b = float(self.parameters.get("corner1_b", 0.0))
        corner1_a = float(self.parameters.get("corner1_a", 1.0))
        corner2_r = float(self.parameters.get("corner2_r", 0.0))
        corner2_g = float(self.parameters.get("corner2_g", 1.0))
        corner2_b = float(self.parameters.get("corner2_b", 0.0))
        corner2_a = float(self.parameters.get("corner2_a", 1.0))
        corner3_r = float(self.parameters.get("corner3_r", 0.0))
        corner3_g = float(self.parameters.get("corner3_g", 0.0))
        corner3_b = float(self.parameters.get("corner3_b", 1.0))
        corner3_a = float(self.parameters.get("corner3_a", 1.0))

        # Return effect marker for GPU renderer
        return {
            "result": TextureResult(
                texture_id=-1,
                width=width,
                height=height,
                alpha_premultiplied=False,
                color_space="linear",
            ),
            "_effect_type": "ramp",
            "_effect_params": {
                "ramp_type": ramp_type,
                "width": width,
                "height": height,
                "start_x": start_x,
                "start_y": start_y,
                "end_x": end_x,
                "end_y": end_y,
                "start_r": start_r,
                "start_g": start_g,
                "start_b": start_b,
                "start_a": start_a,
                "end_r": end_r,
                "end_g": end_g,
                "end_b": end_b,
                "end_a": end_a,
                "extend_mode": extend_mode,
                "center_x": center_x,
                "center_y": center_y,
                "radius": radius,
                "angle_start": angle_start,
                "angle_end": angle_end,
                "corner0_r": corner0_r,
                "corner0_g": corner0_g,
                "corner0_b": corner0_b,
                "corner0_a": corner0_a,
                "corner1_r": corner1_r,
                "corner1_g": corner1_g,
                "corner1_b": corner1_b,
                "corner1_a": corner1_a,
                "corner2_r": corner2_r,
                "corner2_g": corner2_g,
                "corner2_b": corner2_b,
                "corner2_a": corner2_a,
                "corner3_r": corner3_r,
                "corner3_g": corner3_g,
                "corner3_b": corner3_b,
                "corner3_a": corner3_a,
            },
        }
