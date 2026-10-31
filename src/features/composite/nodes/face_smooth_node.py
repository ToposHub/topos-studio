"""Face Smooth node (C3) — skin-only bilateral smoothing.

Takes the image plus a skin mask (from the face workflow's
:func:`build_skin_mask`, or any matte) and runs edge-preserving bilateral
smoothing restricted to the mask.  The GPU bilateral path is the existing
validated ``denoise`` shader (luminance-preserving mode); outside the mask
every pixel is untouched.  ``strength=0`` is an exact passthrough.
"""

import logging
from typing import Any, Dict, Optional

from .base_node import Node, ParamDef, Port, TextureResult

if __name__ != "__main__":
    from src.features.composite.core.format import BBox

logger = logging.getLogger(__name__)


class FaceSmoothNode(Node):
    """Mask-restricted skin smoothing (bilateral, edge-preserving)."""

    node_type = "face_smooth"
    node_title = "Face Smooth"
    i18n_title_key = "nodes.face_smooth.title"
    header_color = "#5a8a6a"
    IS_PLACEHOLDER = False

    SEARCH_ALIASES = [
        "face smooth", "skin smoothing", "beauty", "retouch",
        "磨皮", "皮肤平滑", "美颜",
    ]
    DESCRIPTION = "皮肤区域内双边磨皮（mask 外逐位不变），复用已验证的双边 shader"
    TAGS = ["filter", "face", "beauty"]

    PARAM_DEFS = [
        ParamDef("strength", "float", 0.6, min_val=0.0, max_val=1.0,
                 step=0.01, group="Smooth", animated=True),
        ParamDef("radius", "int", 4, min_val=1, max_val=16, step=1,
                 group="Smooth"),
        ParamDef("luminance_only", "bool", True, group="Smooth"),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("image", "input", "image"),
            Port("mask", "input", "image"),
        ]
        self.outputs = [Port("result", "output", "image")]
        self.parameters = {
            "strength": 0.6,
            "radius": 4,
            "luminance_only": True,
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        return input_bboxes.get("image")

    def compute_input_roi(self, output_roi: "BBox", params: Dict[str, Any],
                          input_bboxes: Optional[Dict[str, "BBox"]] = None
                          ) -> Dict[str, "BBox"]:
        # Bilateral gathers within radius px.
        pad = int(self.parameters.get("radius", 4))
        expanded = output_roi.expanded(pad) if pad > 0 else output_roi
        return {"image": expanded, "mask": output_roi}

    def requires_roi_expansion(self, params: Dict[str, Any]) -> int:
        return int(self.parameters.get("radius", 4))

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        return int(params.get("radius", 4))

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any,
                     fb_manager: Any, resource_pool: Any = None,
                     render_mode: str = "preview") -> Dict[str, TextureResult]:
        image = inputs.get("image")
        if image is None or not image.is_valid:
            return {}
        mask = inputs.get("mask")
        if mask is None or not mask.is_valid:
            self.error_message = "Face Smooth: no skin mask input — " \
                                 "connect build_skin_mask output"
            return {"result": image}

        strength = min(1.0, max(0.0,
                       float(self.parameters.get("strength", 0.6))))
        if strength < 0.01:
            return {"result": image}

        # Reuse the validated bilateral denoise shader via the standard
        # marker protocol (mask rides along as _effect_mask).
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
            "_effect_type": "denoise",
            "_effect_input": image,
            "_effect_mask": mask,
            "_effect_params": {
                "strength": strength,
                "radius": int(self.parameters.get("radius", 4)),
                "luminance_only": bool(
                    self.parameters.get("luminance_only", True)),
            },
        }
