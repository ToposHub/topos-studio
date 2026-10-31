"""ZBlur node (A3) — depth-of-field from a depth input.

Per-pixel circle of confusion ``r = max_radius * smoothstep(0, 1,
|Z01 - focus| / focal_range)``; the variable-radius blur is a Vogel-spiral
disc gather (correct-first MVP, no pyramid), sample count driven by the
quality param and halved in preview mode.  Tile padding equals
``max_blur_radius``.
"""

from typing import Any, Dict, Optional

from .base_node import Node, ParamDef, Port, TextureResult
from .depth_common import DEPTH_UNITS, depth_marker_inputs, depth_params

if __name__ != "__main__":
    from src.features.composite.core.format import BBox

QUALITY_LEVELS = ["fast", "normal", "high"]
_QUALITY_SAMPLES = {"fast": 8, "normal": 16, "high": 32}


class ZBlurNode(Node):
    """Depth-of-field blur driven by a depth texture."""

    node_type = "zblur"
    node_title = "ZBlur"
    i18n_title_key = "nodes.zblur.title"
    header_color = "#5a8a6a"
    IS_PLACEHOLDER = False

    SEARCH_ALIASES = [
        "zblur", "z blur", "depth of field", "dof", "focus",
        "景深", "深度模糊", "焦点模糊",
    ]
    DESCRIPTION = "按深度图做景深模糊（焦点平面清晰、远近视距按半径虚化）"
    TAGS = ["filter", "depth", "blur", "3d"]

    PARAM_DEFS = [
        ParamDef("focus_distance", "float", 0.5, min_val=0.0, max_val=1.0,
                 step=0.005, group="Focus", animated=True,
                 expression_hint="focus_distance"),
        ParamDef("focal_range", "float", 0.15, min_val=0.001, max_val=1.0,
                 step=0.005, group="Focus", animated=True),
        ParamDef("max_blur_radius", "float", 12.0, min_val=0.0,
                 max_val=100.0, step=0.5, group="Focus", animated=True),
        ParamDef("bokeh_toggle", "bool", False, group="Focus"),
        ParamDef("depth_units", "option", "normalized", labels=DEPTH_UNITS,
                 group="Depth"),
        ParamDef("near", "float", 0.0, min_val=-1e6, max_val=1e6, step=1.0,
                 group="Depth", animated=True),
        ParamDef("far", "float", 100.0, min_val=-1e6, max_val=1e6,
                 step=1.0, group="Depth", animated=True),
        ParamDef("quality", "option", "normal", labels=QUALITY_LEVELS,
                 group="Focus"),
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
            "focus_distance": 0.5,
            "focal_range": 0.15,
            "max_blur_radius": 12.0,
            "bokeh_toggle": False,
            "depth_units": "normalized",
            "near": 0.0,
            "far": 100.0,
            "quality": "normal",
            "cache_to_disk": False,
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        return input_bboxes.get("image")

    def compute_input_roi(self, output_roi: "BBox", params: Dict[str, Any],
                          input_bboxes: Optional[Dict[str, "BBox"]] = None
                          ) -> Dict[str, "BBox"]:
        pad = self._tile_padding(params)
        expanded = output_roi.expanded(pad) if pad > 0 else output_roi
        return {"image": expanded, "depth": output_roi}

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        return int(max(0.0, float(params.get("max_blur_radius", 12.0))))

    def requires_roi_expansion(self, params: Dict[str, Any]) -> int:
        return self._tile_padding(params)

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any,
                     fb_manager: Any, resource_pool: Any = None,
                     render_mode: str = "preview") -> Dict[str, TextureResult]:
        pair = depth_marker_inputs(inputs)
        if not pair:
            return {}
        image = pair["image"]

        params = depth_params(self.parameters)
        focus = min(1.0, max(0.0,
                    float(self.parameters.get("focus_distance", 0.5))))
        focal_range = max(0.001,
                          float(self.parameters.get("focal_range", 0.15)))
        max_radius = max(0.0,
                         float(self.parameters.get("max_blur_radius", 12.0)))
        quality = self.parameters.get("quality", "normal")
        sample_count = _QUALITY_SAMPLES.get(quality, 16)
        if render_mode == "preview":
            # Interactive degradation: fewer gather samples (plan §2-14).
            sample_count = max(4, sample_count // 2)
            if max_radius > 40.0:
                max_radius = max(40.0, max_radius * 0.5)

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
            "_effect_type": "zblur",
            "_effect_input": image,
            "_effect_depth": pair["depth"],
            "_effect_params": {
                **params,
                "focus_distance": focus,
                "focal_range": focal_range,
                "max_blur_radius": max_radius,
                "sample_count": int(sample_count),
                "bokeh_toggle": bool(
                    self.parameters.get("bokeh_toggle", False)),
            },
        }
