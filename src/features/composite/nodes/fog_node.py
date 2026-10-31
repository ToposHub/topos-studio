"""Fog node (A3) — exponential depth fog.

    out = mix(image, fog_color, 1 - exp(-density * t))
    t   = clamp((Z01 - start) / max(end - start, eps), 0, 1)

Purely per-pixel (padding 0); the fog amount follows theBeer–Lambert law
against the normalized depth.
"""

from typing import Any, Dict, Optional

from .base_node import Node, ParamDef, Port, TextureResult
from .depth_common import DEPTH_UNITS, depth_marker_inputs, depth_params

if __name__ != "__main__":
    from src.features.composite.core.format import BBox


class FogNode(Node):
    """Exponential depth fog in the linear domain."""

    node_type = "fog"
    node_title = "Fog"
    i18n_title_key = "nodes.fog.title"
    header_color = "#5a8a6a"
    IS_PLACEHOLDER = False

    SEARCH_ALIASES = [
        "fog", "depth fog", "atmosphere", "haze", "雾", "大气", "深度雾",
    ]
    DESCRIPTION = "按深度做指数大气雾（Beer–Lambert，start 前无雾、end 处饱和）"
    TAGS = ["filter", "depth", "atmosphere", "3d"]

    PARAM_DEFS = [
        ParamDef("color", "color", (0.5, 0.6, 0.7, 1.0), group="Fog",
                 animated=True),
        ParamDef("density", "float", 1.0, min_val=0.0, max_val=10.0,
                 step=0.01, group="Fog", animated=True),
        ParamDef("start", "float", 0.0, min_val=0.0, max_val=1.0,
                 step=0.005, group="Fog", animated=True),
        ParamDef("end", "float", 1.0, min_val=0.0, max_val=1.0,
                 step=0.005, group="Fog", animated=True),
        ParamDef("depth_units", "option", "normalized", labels=DEPTH_UNITS,
                 group="Depth"),
        ParamDef("near", "float", 0.0, min_val=-1e6, max_val=1e6, step=1.0,
                 group="Depth", animated=True),
        ParamDef("far", "float", 100.0, min_val=-1e6, max_val=1e6,
                 step=1.0, group="Depth", animated=True),
        ParamDef("cache_to_disk", "bool", False, group="Cache"),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("image", "input", "image"),
            Port("depth", "input", "image"),
        ]
        self.outputs = [
            Port("result", "output", "image"),
        ]
        self.parameters = {
            "color": (0.5, 0.6, 0.7, 1.0),
            "density": 1.0,
            "start": 0.0,
            "end": 1.0,
            "depth_units": "normalized",
            "near": 0.0,
            "far": 100.0,
            "cache_to_disk": False,
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        return input_bboxes.get("image")

    def compute_input_roi(self, output_roi: "BBox", params: Dict[str, Any],
                          input_bboxes: Optional[Dict[str, "BBox"]] = None
                          ) -> Dict[str, "BBox"]:
        return {"image": output_roi, "depth": output_roi}

    def requires_roi_expansion(self, params: Dict[str, Any]) -> int:
        return 0

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any,
                     fb_manager: Any, resource_pool: Any = None,
                     render_mode: str = "preview") -> Dict[str, TextureResult]:
        pair = depth_marker_inputs(inputs)
        if not pair:
            return {}
        image = pair["image"]

        params = depth_params(self.parameters)
        density = max(0.0, float(self.parameters.get("density", 1.0)))
        start = min(1.0, max(0.0, float(self.parameters.get("start", 0.0))))
        end = min(1.0, max(0.0, float(self.parameters.get("end", 1.0))))
        color = self.parameters.get("color", (0.5, 0.6, 0.7, 1.0))
        fog_color = (float(color[0]), float(color[1]), float(color[2]))

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
            "_effect_type": "fog",
            "_effect_input": image,
            "_effect_depth": pair["depth"],
            "_effect_params": {
                **params,
                "density": density,
                "start": start,
                "end": max(start, end),
                "fog_color": fog_color,
            },
        }
