"""DeGrain Node — grain-gated smoothing (CP-019).

Static (single-frame) degrain: a bilateral-smoothed estimate of the image
is blended back only where the original deviates from it by LESS than a
grain threshold — film grain (small, high-frequency deviation) is replaced
by the smooth estimate while real detail edges (large deviation) pass
through untouched.

Honest boundary: this is NOT temporal degraining (Nuke's DeGrain /
Furnace-style motion-compensated multi-frame estimate). It removes
per-frame grain at the cost of slightly flattening grain-scale real
texture. The temporal version needs the motion-vector infrastructure
tracked by CP-033.
"""

from typing import Any, Dict, Optional

from .base_node import Node, ParamDef, Port, TextureResult


class DeGrainNode(Node):
    """Grain-gated bilateral smoothing (static single-frame degrain)."""

    node_type = "degrain"
    node_title = "DeGrain"
    i18n_title_key = "nodes.degrain.title"
    header_color = "#6c7a8a"

    SEARCH_ALIASES = [
        "degrain", "remove grain", "去颗粒", "降噪点", "grain removal",
    ]
    DESCRIPTION = "静态去颗粒：颗粒门控双边平滑（阈值内替换为平滑估计，边缘细节保留）"
    TAGS = ["filter", "film", "cleanup"]

    PARAM_DEFS = [
        ParamDef("grain_threshold", "float", 0.02, min_val=0.001,
                 max_val=0.5, step=0.001, group="DeGrain"),
        ParamDef("spatial_sigma", "float", 1.5, min_val=0.5, max_val=8.0,
                 step=0.5, group="DeGrain"),
        ParamDef("range_sigma", "float", 0.05, min_val=0.005, max_val=0.5,
                 step=0.005, group="DeGrain"),
        ParamDef("amount", "float", 1.0, min_val=0.0, max_val=1.0,
                 step=0.05, group="DeGrain"),
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
            "grain_threshold": 0.02,
            "spatial_sigma": 1.5,
            "range_sigma": 0.05,
            "amount": 1.0,
        }
        self._init_port_revisions()

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any,
                     fb_manager: Any, resource_pool: Any = None,
                     render_mode: str = "preview") -> Dict[str, TextureResult]:
        image = inputs.get("image")
        has_image = image is not None and image.is_valid
        if not has_image:
            return {}
        width = max(1, int(image.width))
        height = max(1, int(image.height))
        return {
            "result": TextureResult(
                texture_id=-1, width=width, height=height,
                alpha_premultiplied=image.alpha_premultiplied,
                color_space=image.color_space),
            "_effect_type": "degrain",
            "_effect_input": image,
            "_effect_params": {
                "grain_threshold": self._get_param_clamped(
                    "grain_threshold", 0.001, 0.5),
                "spatial_sigma": self._get_param_clamped(
                    "spatial_sigma", 0.5, 8.0),
                "range_sigma": self._get_param_clamped(
                    "range_sigma", 0.005, 0.5),
                "amount": self._get_param_clamped("amount", 0.0, 1.0),
            },
        }
