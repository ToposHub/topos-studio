"""God Rays node (B3) — volumetric light shafts (screen-space radial march).

Classic GPU radial-blur godrays: every pixel marches toward the light
position, accumulating the occlusion it crosses with ``decay^i`` falloff:

    rays    = Σ occlusion(uv_i) · decay^i   (normalized by sample count)
    result  = image + rays · weight · density · light_color

The occluder is the input alpha, or the optional ``mask`` input when it is
connected.  The march is global, so the node is full-frame-only: it opts
out of ROI clipping (capability ``roi_policy="full_frame"`` + the input ROI
expands to the whole input), and tile padding is 0.
"""

from typing import Any, Dict, Optional

from .base_node import Node, ParamDef, Port, TextureResult

if __name__ != "__main__":
    from src.features.composite.core.format import BBox


class GodRaysNode(Node):
    """Screen-space volumetric light shafts (Tyndall effect)."""

    node_type = "godrays"
    node_title = "God Rays"
    i18n_title_key = "nodes.godrays.title"
    header_color = "#8a8a5a"
    IS_PLACEHOLDER = False

    SEARCH_ALIASES = [
        "godrays", "god rays", "volumetric light", "light shafts",
        "tyndall", "体积光", "丁达尔", "光束", "耶稣光",
    ]
    DESCRIPTION = "屏幕空间径向采样的体积光束（遮挡越少光越强，随距离衰减）"
    TAGS = ["filter", "lighting", "volumetric"]

    PARAM_DEFS = [
        ParamDef("light_x", "float", 0.5, min_val=-0.5, max_val=1.5,
                 step=0.005, group="Light", animated=True,
                 expression_hint="light_x"),
        ParamDef("light_y", "float", 0.5, min_val=-0.5, max_val=1.5,
                 step=0.005, group="Light", animated=True),
        ParamDef("color", "color", (1.0, 0.95, 0.85, 1.0), group="Light",
                 animated=True),
        ParamDef("density", "float", 0.35, min_val=0.0, max_val=2.0,
                 step=0.01, group="Rays", animated=True),
        ParamDef("weight", "float", 0.5, min_val=0.0, max_val=2.0,
                 step=0.01, group="Rays", animated=True),
        ParamDef("decay", "float", 0.95, min_val=0.5, max_val=1.0,
                 step=0.005, group="Rays", animated=True),
        ParamDef("samples", "int", 32, min_val=16, max_val=64, step=16,
                 group="Rays", animated=True),
        ParamDef("cache_to_disk", "bool", False, group="Cache"),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("image", "input", "image"),
            Port("mask", "input", "image"),
        ]
        self.outputs = [
            Port("result", "output", "image"),
        ]
        self.parameters = {
            "light_x": 0.5,
            "light_y": 0.5,
            "color": (1.0, 0.95, 0.85, 1.0),
            "density": 0.35,
            "weight": 0.5,
            "decay": 0.95,
            "samples": 32,
            "cache_to_disk": False,
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        return input_bboxes.get("image")

    def compute_input_roi(self, output_roi: "BBox", params: Dict[str, Any],
                          input_bboxes: Optional[Dict[str, "BBox"]] = None
                          ) -> Dict[str, "BBox"]:
        """Full-frame-only: the radial march reaches the whole input, so
        the requested inputs are the full input bboxes (never the clipped
        output ROI)."""
        if input_bboxes:
            image_bbox = input_bboxes.get("image")
            if image_bbox is not None and not image_bbox.is_empty:
                full = image_bbox.union(output_roi)
                result = {"image": full}
                if "mask" in input_bboxes:
                    result["mask"] = full
                return result
        return {"image": output_roi, "mask": output_roi}

    def requires_roi_expansion(self, params: Dict[str, Any]) -> int:
        # Radial sampling is bounded by the input frame, not by a kernel.
        return 0

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any,
                     fb_manager: Any, resource_pool: Any = None,
                     render_mode: str = "preview") -> Dict[str, TextureResult]:
        image = inputs.get("image")
        if image is None or not image.is_valid:
            return {}

        mask = inputs.get("mask")
        use_mask = mask is not None and mask.is_valid

        light_x = min(1.5, max(-0.5,
                      float(self.parameters.get("light_x", 0.5))))
        light_y = min(1.5, max(-0.5,
                      float(self.parameters.get("light_y", 0.5))))
        density = max(0.0, float(self.parameters.get("density", 0.35)))
        weight = max(0.0, float(self.parameters.get("weight", 0.5)))
        decay = min(1.0, max(0.5, float(self.parameters.get("decay", 0.95))))
        sample_count = int(self.parameters.get("samples", 32))
        sample_count = max(16, min(64, sample_count))
        if render_mode == "preview":
            # Plan §2-14: draft/preview halves the march resolution.
            sample_count = max(8, sample_count // 2)

        color = self.parameters.get("color", (1.0, 0.95, 0.85, 1.0))
        light_color = (float(color[0]), float(color[1]), float(color[2]))

        marker: Dict[str, Any] = {
            "result": TextureResult(
                texture_id=-1,
                width=image.width,
                height=image.height,
                format=image.format,
                bbox=image.bbox,
                alpha_premultiplied=image.alpha_premultiplied,
                color_space=image.color_space,
            ),
            "_effect_type": "godrays",
            "_effect_input": image,
            "_effect_params": {
                "light_pos": (light_x, light_y),
                "density": density,
                "weight": weight,
                "decay": decay,
                "sample_count": int(sample_count),
                "light_color": light_color,
            },
        }
        if use_mask:
            marker["_effect_mask"] = mask
        return marker
