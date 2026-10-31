"""
Glow Node - Bloom/glow effect with HDR support.

Adds a soft luminous glow around bright areas of the image.
HDR-aware with threshold, intensity, tint, and mix controls.
"""

from typing import Dict, Optional, Any
from .base_node import Node, Port, TextureResult, ParamDef

if __name__ != "__main__":
    from src.features.composite.core.format import BBox


class GlowNode(Node):
    """Applies a glow/bloom effect to bright areas with HDR support."""

    node_type = "glow"
    node_title = "Glow"
    header_color = "#d4a017"  # Golden

    # i18n title key
    i18n_title_key = "nodes.glow.title"

    # Metadata
    SEARCH_ALIASES = ["glow", "bloom", "发光", "光晕", "辉光", "泛光"]
    DESCRIPTION = "在图像亮部添加发光效果"
    TAGS = ["filter", "image", "hdr"]

    PARAM_DEFS = [
        ParamDef("threshold", "float", 0.7, min_val=0.0, max_val=2.0, step=0.01),
        ParamDef("radius", "float", 15.0, min_val=0.0, max_val=100.0, step=0.5),
        ParamDef("intensity", "float", 1.5, min_val=0.0, max_val=10.0, step=0.05),
        ParamDef("tint_r", "float", 1.0, min_val=0.0, max_val=2.0, step=0.01),
        ParamDef("tint_g", "float", 0.85, min_val=0.0, max_val=2.0, step=0.01),
        ParamDef("tint_b", "float", 0.7, min_val=0.0, max_val=2.0, step=0.01),
        ParamDef("mix", "float", 1.0, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("quality", "option", "normal", labels=["fast", "normal", "high"]),
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
            "threshold": 0.7,
            "radius": 15.0,
            "intensity": 1.5,
            "tint_r": 1.0,
            "tint_g": 0.85,
            "tint_b": 0.7,
            "mix": 1.0,
            "quality": "normal",
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        """Glow expands bbox by radius."""
        if not input_bboxes:
            return None
        input_bbox = input_bboxes.get("image")
        if input_bbox is None or input_bbox.is_empty:
            return None
        radius = float(params.get("radius", 15.0))
        return input_bbox.expanded(int(radius))

    def compute_input_roi(self, output_roi: "BBox",
                          params: Dict[str, Any],
                          input_bboxes: Optional[Dict[str, "BBox"]] = None) -> Dict[str, "BBox"]:
        """To glow output_roi, we need input expanded by radius."""
        radius = float(params.get("radius", 15.0))
        expanded_roi = output_roi.expanded(int(radius))
        return {"image": expanded_roi}

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        """Declare tile padding requirement for seamless boundaries.

        Glow effect involves a blur operation on the bright areas.
        Requires radius pixels on all sides for seamless tile boundaries.

        Args:
            params: Node parameters dictionary.

        Returns:
            Padding in pixels (radius).
        """
        return int(float(params.get("radius", 15.0)))

    def requires_roi_expansion(self, params: Dict[str, Any]) -> int:
        """Legacy API - delegates to _tile_padding()."""
        return self._tile_padding(params)

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                      resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        image = inputs.get("image")
        if not image or not image.is_valid:
            return {}

        threshold = float(self.parameters.get("threshold", 0.7))
        radius = float(self.parameters.get("radius", 15.0))
        intensity = float(self.parameters.get("intensity", 1.5))
        tint_r = float(self.parameters.get("tint_r", 1.0))
        tint_g = float(self.parameters.get("tint_g", 0.85))
        tint_b = float(self.parameters.get("tint_b", 0.7))
        mix = float(self.parameters.get("mix", 1.0))
        quality = self.parameters.get("quality", "normal")

        # Skip if radius is negligible or intensity is zero
        if radius < 0.1 or intensity < 0.01:
            return {"result": image}

        # Preview mode: reduce radius for faster response
        if render_mode == "preview" and radius > 20.0:
            radius = max(5.0, radius * 0.35)

        # Quality-based sample count
        quality_samples = {
            "fast": 8,
            "normal": 16,
            "high": 32,
        }
        sample_count = quality_samples.get(quality, 16)

        return {
            "result": TextureResult(
                texture_id=-1,  # Will be replaced by actual GPU result
                width=image.width,
                height=image.height,
                alpha_premultiplied=image.alpha_premultiplied,
                color_space=image.color_space,
            ),
            "_effect_type": "glow",
            "_effect_input": image,
            "_effect_params": {
                "threshold": threshold,
                "radius": radius,
                "intensity": intensity,
                "tint_r": tint_r,
                "tint_g": tint_g,
                "tint_b": tint_b,
                "mix": mix,
                "sample_count": sample_count,
            },
        }
