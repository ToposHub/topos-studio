"""DepthMatte node (A3) — alpha matte from a depth range.

Outputs a white-with-coverage matte for ``z0 <= Z01 <= z1`` with ``feather``
smooth edges, ready for any downstream mask input (Keyer, merge mask,
grade mask...).  Purely per-pixel (padding 0).
"""

from typing import Any, Dict, Optional

from .base_node import Node, ParamDef, Port, TextureResult
from .depth_common import DEPTH_UNITS, depth_marker_inputs, depth_params

if __name__ != "__main__":
    from src.features.composite.core.format import BBox


class DepthMatteNode(Node):
    """Depth-range matte with feathered boundaries."""

    node_type = "depth_matte"
    node_title = "Depth Matte"
    i18n_title_key = "nodes.depth_matte.title"
    header_color = "#6a6a8a"
    IS_PLACEHOLDER = False

    SEARCH_ALIASES = [
        "depth matte", "z matte", "depth key", "深度遮罩", "Z 抠像",
        "深度 matte",
    ]
    DESCRIPTION = "按 Z 区间输出羽化 matte，供任意下游 mask 使用"
    TAGS = ["mask", "depth", "matte", "3d"]

    PARAM_DEFS = [
        ParamDef("range_start", "float", 0.3, min_val=0.0, max_val=1.0,
                 step=0.005, group="Range", animated=True),
        ParamDef("range_end", "float", 0.7, min_val=0.0, max_val=1.0,
                 step=0.005, group="Range", animated=True),
        ParamDef("feather", "float", 0.05, min_val=0.0, max_val=0.5,
                 step=0.005, group="Range", animated=True),
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
            "range_start": 0.3,
            "range_end": 0.7,
            "feather": 0.05,
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
        z0 = min(1.0, max(0.0,
                 float(self.parameters.get("range_start", 0.3))))
        z1 = min(1.0, max(0.0,
                 float(self.parameters.get("range_end", 0.7))))
        feather = min(0.5, max(0.0,
                      float(self.parameters.get("feather", 0.05))))

        return {
            "result": TextureResult(
                texture_id=-1,
                width=image.width,
                height=image.height,
                format=image.format,
                bbox=image.bbox,
                alpha_premultiplied=True,
                color_space=image.color_space,
            ),
            "_effect_type": "depth_matte",
            "_effect_input": image,
            "_effect_depth": pair["depth"],
            "_effect_params": {
                **params,
                "range_start": z0,
                "range_end": z1,
                "feather": feather,
            },
        }
