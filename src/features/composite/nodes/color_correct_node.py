"""
Color Correct Node - Professional color correction (DAVINCI-style).

Provides shadows/midtones/highlights controls, saturation, contrast, hue shift,
exposure, temperature/tint, and channel selector.

Designed for HDR workflows (float16/32) — no clamping in shader.

Mathematical operations:
- Exposure: color * pow(2.0, exposure)
- Contrast: (color - 0.5) * contrast + 0.5
- Saturation: mix(luma, color, saturation)
- Hue shift: rotate RGB in hue space
- Temperature/Tint: shift white point in yellow-blue and green-magenta axes
- Shadows/Midtones/Highlights: per-tone-region lift/gamma/gain
"""

from typing import Any, Dict, Optional
from .base_node import Node, Port, TextureResult, ParamDef


class ColorCorrectNode(Node):
    """Professional color correction node with tonal range controls."""

    node_type = "color_correct"
    node_title = "Color Correct"
    header_color = "#4a90d9"  # Professional blue

    # Metadata
    SEARCH_ALIASES = ["colorcorrect", "cc", "correct", "校正", "阴影", "高光", "色调", "saturation"]
    DESCRIPTION = "专业色彩校正（阴影/中间调/高光）"
    TAGS = ["color", "correction"]

    PARAM_DEFS = [
        # Exposure / Contrast / Saturation
        ParamDef("exposure", "float", 0.0, min_val=-4.0, max_val=4.0, step=0.01),
        ParamDef("contrast", "float", 1.0, min_val=0.0, max_val=4.0, step=0.01),
        ParamDef("saturation", "float", 1.0, min_val=0.0, max_val=4.0, step=0.01),
        # Hue shift
        ParamDef("hue_shift", "float", 0.0, min_val=-180.0, max_val=180.0, step=0.1),
        # Temperature / Tint
        ParamDef("temperature", "float", 0.0, min_val=-1.0, max_val=1.0, step=0.01),
        ParamDef("tint", "float", 0.0, min_val=-1.0, max_val=1.0, step=0.01),
        # Shadows controls (lift-style)
        ParamDef("shadows_lifting_r", "float", 0.0, min_val=-1.0, max_val=1.0, step=0.01),
        ParamDef("shadows_lifting_g", "float", 0.0, min_val=-1.0, max_val=1.0, step=0.01),
        ParamDef("shadows_lifting_b", "float", 0.0, min_val=-1.0, max_val=1.0, step=0.01),
        # Midtones controls (gamma-style)
        ParamDef("midtones_gamma_r", "float", 1.0, min_val=0.01, max_val=4.0, step=0.01),
        ParamDef("midtones_gamma_g", "float", 1.0, min_val=0.01, max_val=4.0, step=0.01),
        ParamDef("midtones_gamma_b", "float", 1.0, min_val=0.01, max_val=4.0, step=0.01),
        # Highlights controls (gain-style)
        ParamDef("highlights_gain_r", "float", 1.0, min_val=0.0, max_val=4.0, step=0.01),
        ParamDef("highlights_gain_g", "float", 1.0, min_val=0.0, max_val=4.0, step=0.01),
        ParamDef("highlights_gain_b", "float", 1.0, min_val=0.0, max_val=4.0, step=0.01),
        # Channel selector
        ParamDef("channel_r", "bool", True),
        ParamDef("channel_g", "bool", True),
        ParamDef("channel_b", "bool", True),
        ParamDef("channel_a", "bool", False),
        # Mix control
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
            # Exposure / Contrast / Saturation
            "exposure": 0.0,
            "contrast": 1.0,
            "saturation": 1.0,
            # Hue shift
            "hue_shift": 0.0,
            # Temperature / Tint
            "temperature": 0.0,
            "tint": 0.0,
            # Shadows
            "shadows_lifting_r": 0.0,
            "shadows_lifting_g": 0.0,
            "shadows_lifting_b": 0.0,
            # Midtones
            "midtones_gamma_r": 1.0,
            "midtones_gamma_g": 1.0,
            "midtones_gamma_b": 1.0,
            # Highlights
            "highlights_gain_r": 1.0,
            "highlights_gain_g": 1.0,
            "highlights_gain_b": 1.0,
            # Channel selector
            "channel_r": True,
            "channel_g": True,
            "channel_b": True,
            "channel_a": False,
            # Mix
            "mix": 1.0,
        }

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                      resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        image = inputs.get("image")
        if not image or not image.is_valid:
            return {}

        mask_image = inputs.get("mask")  # Optional mask input

        # Check if any parameter actually differs from default
        # Use PARAM_DEFS for dynamic default value lookup (not hardcoded)
        has_changes = any(
            self.parameters.get(param_def.name, param_def.default) != param_def.default
            for param_def in self.PARAM_DEFS
        )

        if not has_changes:
            return {"result": image}

        # Build channel selector
        channel_selector = [
            1.0 if self.parameters.get("channel_r", True) else 0.0,
            1.0 if self.parameters.get("channel_g", True) else 0.0,
            1.0 if self.parameters.get("channel_b", True) else 0.0,
            1.0 if self.parameters.get("channel_a", False) else 0.0,
        ]

        return {
            "result": TextureResult(
                texture_id=-1,
                width=image.width,
                height=image.height,
            ),
            "_effect_type": "color_correct",
            "_effect_input": image,
            "_effect_mask": mask_image,  # Optional mask
            "_effect_params": {
                "exposure": self.parameters.get("exposure", 0.0),
                "contrast": self.parameters.get("contrast", 1.0),
                "saturation": self.parameters.get("saturation", 1.0),
                "hue_shift": self.parameters.get("hue_shift", 0.0),
                "temperature": self.parameters.get("temperature", 0.0),
                "tint": self.parameters.get("tint", 0.0),
                "shadows_lifting": [
                    self.parameters.get("shadows_lifting_r", 0.0),
                    self.parameters.get("shadows_lifting_g", 0.0),
                    self.parameters.get("shadows_lifting_b", 0.0),
                ],
                "midtones_gamma": [
                    self.parameters.get("midtones_gamma_r", 1.0),
                    self.parameters.get("midtones_gamma_g", 1.0),
                    self.parameters.get("midtones_gamma_b", 1.0),
                ],
                "highlights_gain": [
                    self.parameters.get("highlights_gain_r", 1.0),
                    self.parameters.get("highlights_gain_g", 1.0),
                    self.parameters.get("highlights_gain_b", 1.0),
                ],
                "channel_selector": channel_selector,
                "mix": self.parameters.get("mix", 1.0),
            },
        }
