"""Vignette node (B4) — multiplicative lens darkening.

Factor ``v = 1 - amount · smoothstep(radius - softness, radius, d)`` where
``d`` is a superellipse distance from the frame centre controlled by
``roundness`` (0 = circle, 1 = rectangle-ish).  Purely per-pixel, linear
domain, padding 0.
"""

import math
from typing import Any, Dict, Optional

from .base_node import Node, ParamDef, Port, TextureResult

if __name__ != "__main__":
    from src.features.composite.core.format import BBox


def vignette_factor(dx: float, dy: float, radius: float, softness: float,
                    amount: float, roundness: float) -> float:
    """Python mirror of the shader math (aspect ratio 1)."""
    n = 2.0 + 6.0 * min(1.0, max(0.0, roundness))
    d = math.pow(abs(dx) ** n + abs(dy) ** n, 1.0 / n)
    radius = min(1.0, max(0.05, radius))
    softness = min(radius, max(0.001, softness))
    t = min(1.0, max(0.0, (d - (radius - softness)) / softness))
    smooth = t * t * (3.0 - 2.0 * t)
    return 1.0 - min(1.0, max(0.0, amount)) * smooth


class VignetteNode(Node):
    """Lens vignette (exposure-style multiplicative darkening)."""

    node_type = "vignette"
    node_title = "Vignette"
    i18n_title_key = "nodes.vignette.title"
    header_color = "#5a8a6a"
    IS_PLACEHOLDER = False

    SEARCH_ALIASES = [
        "vignette", "lens darkening", "暗角", "晕影", "压四角",
    ]
    DESCRIPTION = "乘性镜头暗角（圆度可调，linear 域 exposure 语义）"
    TAGS = ["filter", "lens", "camera"]

    PARAM_DEFS = [
        ParamDef("amount", "float", 0.5, min_val=0.0, max_val=1.0,
                 step=0.01, group="Vignette", animated=True),
        ParamDef("cache_to_disk", "bool", False, group="Cache"),
        ParamDef("radius", "float", 0.7, min_val=0.05, max_val=1.0,
                 step=0.01, group="Vignette", animated=True),
        ParamDef("softness", "float", 0.4, min_val=0.001, max_val=1.0,
                 step=0.01, group="Vignette", animated=True),
        ParamDef("roundness", "float", 0.0, min_val=0.0, max_val=1.0,
                 step=0.01, group="Vignette", animated=True),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [Port("image", "input", "image")]
        self.outputs = [Port("result", "output", "image")]
        self.parameters = {
            "amount": 0.5,
            "radius": 0.7,
            "softness": 0.4,
            "roundness": 0.0,
            "cache_to_disk": False,
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        return input_bboxes.get("image")

    def compute_input_roi(self, output_roi: "BBox", params: Dict[str, Any],
                          input_bboxes: Optional[Dict[str, "BBox"]] = None
                          ) -> Dict[str, "BBox"]:
        return {"image": output_roi}

    def requires_roi_expansion(self, params: Dict[str, Any]) -> int:
        return 0

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any,
                     fb_manager: Any, resource_pool: Any = None,
                     render_mode: str = "preview") -> Dict[str, TextureResult]:
        image = inputs.get("image")
        if image is None or not image.is_valid:
            return {}

        return {
            "result": TextureResult(
                texture_id=-1,
                width=image.width,
                height=image.height,
                format=image.format,
                bbox=image.bbox,
                alpha_premultiplied=image.alpha_premultiplied,
                color_space=image.color_space,
            ),
            "_effect_type": "vignette",
            "_effect_input": image,
            "_effect_params": {
                "amount": min(1.0, max(0.0,
                              float(self.parameters.get("amount", 0.5)))),
                "radius": min(1.0, max(0.05,
                              float(self.parameters.get("radius", 0.7)))),
                "softness": min(1.0, max(0.001, float(
                    self.parameters.get("softness", 0.4)))),
                "roundness": min(1.0, max(0.0, float(
                    self.parameters.get("roundness", 0.0)))),
            },
        }
