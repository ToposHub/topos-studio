"""Chromatic Aberration node (B4) — radial RGB channel separation.

Samples the red and blue channels along the radial direction with
independent pixel offsets (green stays put).  ``edge_boost`` ramps the
offset up with distance from the centre so the centre stays clean; with
``edge_boost=0`` the offsets are constant and analytically testable.
Tile padding equals the largest absolute offset.
"""

from typing import Any, Dict, Optional

from .base_node import Node, ParamDef, Port, TextureResult

if __name__ != "__main__":
    from src.features.composite.core.format import BBox


class ChromaticAberrationNode(Node):
    """Radial lens chromatic aberration (R/B split, G fixed)."""

    node_type = "chromatic_aberration"
    node_title = "Chromatic Aberration"
    i18n_title_key = "nodes.chromatic_aberration.title"
    header_color = "#5a8a6a"
    IS_PLACEHOLDER = False

    SEARCH_ALIASES = [
        "chromatic aberration", "ca", "color fringe", "rgb split",
        "色差", "色散", "RGB 错位",
    ]
    DESCRIPTION = "径向 RGB 通道错位模拟镜头色差（R/B 独立偏移 + 边缘增强）"
    TAGS = ["filter", "lens", "camera"]

    PARAM_DEFS = [
        ParamDef("offset_r", "float", 2.0, min_val=-20.0, max_val=20.0,
                 step=0.25, group="Aberration", animated=True),
        ParamDef("offset_b", "float", -2.0, min_val=-20.0, max_val=20.0,
                 step=0.25, group="Aberration", animated=True),
        ParamDef("center_x", "float", 0.5, min_val=0.0, max_val=1.0,
                 step=0.005, group="Aberration", animated=True),
        ParamDef("center_y", "float", 0.5, min_val=0.0, max_val=1.0,
                 step=0.005, group="Aberration", animated=True),
        ParamDef("edge_boost", "float", 0.0, min_val=0.0, max_val=1.0,
                 step=0.01, group="Aberration", animated=True,
                 expression_hint="edge_boost"),
        ParamDef("cache_to_disk", "bool", False, group="Cache"),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [Port("image", "input", "image")]
        self.outputs = [Port("result", "output", "image")]
        self.parameters = {
            "offset_r": 2.0,
            "offset_b": -2.0,
            "center_x": 0.5,
            "center_y": 0.5,
            "edge_boost": 0.0,
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
        return {"image": expanded}

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        return int(max(abs(float(params.get("offset_r", 2.0))),
                       abs(float(params.get("offset_b", -2.0)))))

    def requires_roi_expansion(self, params: Dict[str, Any]) -> int:
        return self._tile_padding(params)

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any,
                     fb_manager: Any, resource_pool: Any = None,
                     render_mode: str = "preview") -> Dict[str, TextureResult]:
        image = inputs.get("image")
        if image is None or not image.is_valid:
            return {}

        offset_r = max(-20.0, min(20.0,
                       float(self.parameters.get("offset_r", 2.0))))
        offset_b = max(-20.0, min(20.0,
                       float(self.parameters.get("offset_b", -2.0))))
        edge_boost = min(1.0, max(0.0,
                         float(self.parameters.get("edge_boost", 0.0))))
        if render_mode == "preview":
            # Halve the displacement for fast interactive feedback.
            offset_r *= 0.5
            offset_b *= 0.5

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
            "_effect_type": "chromatic_aberration",
            "_effect_input": image,
            "_effect_params": {
                "offset_r": offset_r,
                "offset_b": offset_b,
                "center": (
                    min(1.0, max(0.0,
                        float(self.parameters.get("center_x", 0.5)))),
                    min(1.0, max(0.0,
                        float(self.parameters.get("center_y", 0.5)))),
                ),
                "edge_boost": edge_boost,
            },
        }
