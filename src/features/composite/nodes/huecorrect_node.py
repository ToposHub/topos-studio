"""
Hue Correct Node - Selective hue-based color adjustments.

Provides per-hue-range adjustments for hue shift, saturation, and brightness.
Essential for professional color grading workflows, allowing targeted
adjustments to specific color ranges (e.g., skin tones, skies, foliage).

Designed for HDR workflows (float16/32) — no clamping in shader.

Mathematical operations:
- RGB → HSV conversion
- Hue range selection with soft falloff
- Per-range adjustments: hue_shift, saturation_mult, brightness_mult
- HSV → RGB conversion

Hue range is specified in degrees (0-360):
- 0 = Red
- 60 = Yellow
- 120 = Green
- 180 = Cyan
- 240 = Blue
- 300 = Magenta
"""

from typing import Any, Dict, Optional
from .base_node import Node, Port, TextureResult, ParamDef


class HueCorrectNode(Node):
    """Selective hue-based color correction node."""

    node_type = "huecorrect"
    node_title = "Hue Correct"
    header_color = "#4a90d9"  # Same blue as ColorCorrect (color grading family)

    # Metadata
    SEARCH_ALIASES = ["huecorrect", "hue", "色相", "色调", "color grading", "调色"]
    DESCRIPTION = "基于色相范围的调整节点"
    TAGS = ["color", "grading"]

    PARAM_DEFS = [
        # Global hue shift (applied to all hues when range is full)
        ParamDef("hue_shift", "float", 0.0, min_val=-180.0, max_val=180.0, step=0.1),
        # Saturation multiplier
        ParamDef("saturation_mult", "float", 1.0, min_val=0.0, max_val=4.0, step=0.01),
        # Brightness multiplier (value in HSV)
        ParamDef("brightness_mult", "float", 1.0, min_val=0.0, max_val=4.0, step=0.01),
        # Hue range selection (in degrees, 0-360)
        ParamDef("range_low", "float", 0.0, min_val=0.0, max_val=360.0, step=1.0),
        ParamDef("range_high", "float", 360.0, min_val=0.0, max_val=360.0, step=1.0),
        # Softness (transition width at range edges)
        ParamDef("range_softness", "float", 10.0, min_val=0.0, max_val=180.0, step=1.0),
        # Mix control (blend between original and corrected)
        ParamDef("mix", "float", 1.0, min_val=0.0, max_val=1.0, step=0.01),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("image", "input", "image"),
            Port("mask", "input", "image"),  # Optional mask input
        ]
        self.outputs = [
            Port("result", "output", "image")
        ]
        self.parameters = {
            "hue_shift": 0.0,
            "saturation_mult": 1.0,
            "brightness_mult": 1.0,
            "range_low": 0.0,
            "range_high": 360.0,
            "range_softness": 10.0,
            "mix": 1.0,
        }

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                      resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        image = inputs.get("image")
        if not image or not image.is_valid:
            return {}

        mask_image = inputs.get("mask")  # Optional mask input

        # Check if any parameter differs from default
        defaults = {
            "hue_shift": 0.0,
            "saturation_mult": 1.0,
            "brightness_mult": 1.0,
            "range_low": 0.0,
            "range_high": 360.0,
            "range_softness": 10.0,
            "mix": 1.0,
        }
        has_changes = any(
            self.parameters.get(key, default) != default
            for key, default in defaults.items()
        )

        if not has_changes:
            return {"result": image}

        # Clamp parameters to safe ranges
        hue_shift = self._get_param_clamped("hue_shift", -180.0, 180.0)
        saturation_mult = self._get_param_clamped("saturation_mult", 0.0, 4.0)
        brightness_mult = self._get_param_clamped("brightness_mult", 0.0, 4.0)
        range_low = self._get_param_clamped("range_low", 0.0, 360.0)
        range_high = self._get_param_clamped("range_high", 0.0, 360.0)
        range_softness = self._get_param_clamped("range_softness", 0.0, 180.0)
        mix = self._get_param_clamped("mix", 0.0, 1.0)

        return {
            "result": TextureResult(
                texture_id=-1,
                width=image.width,
                height=image.height,
            ),
            "_effect_type": "huecorrect",
            "_effect_input": image,
            "_effect_mask": mask_image,  # Optional mask
            "_effect_params": {
                "hue_shift": hue_shift,
                "saturation_mult": saturation_mult,
                "brightness_mult": brightness_mult,
                "range_low": range_low,
                "range_high": range_high,
                "range_softness": range_softness,
                "mix": mix,
            },
        }