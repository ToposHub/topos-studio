"""VectorBlur Node — motion-vector driven 2D blur (CP-020/021).

Nuke VectorBlur counterpart: consumes a per-pixel motion-vector image
(signed normalized UV displacement per frame — the same convention as the
blur node's velocity input and Scene3D's MOTION_VECTOR AOV) and smears
the image along each pixel's vector.

Shutter (CP-021): the blur interval. With ``use_project_shutter`` on
(default), the composition-level shutter from the Output node's
``motion_blur_shutter`` parameter is used when the evaluation context
carries one (CompositeEvalContext.motion_blur_shutter); otherwise the
node's own ``shutter`` value applies.
"""

from typing import Any, Dict, Optional

from .base_node import Node, ParamDef, Port, TextureResult


class VectorBlurNode(Node):
    """Motion-vector driven directional blur per pixel."""

    node_type = "vectorblur"
    node_title = "VectorBlur"
    i18n_title_key = "nodes.vectorblur.title"
    header_color = "#5c7d8a"

    SEARCH_ALIASES = [
        "vector blur", "vectorblur", "motion vector blur", "velocity blur",
        "运动矢量模糊", "矢量模糊", "速度模糊",
    ]
    DESCRIPTION = "按运动矢量图逐像素方向模糊（消费 MOTION_VECTOR AOV / velocity 图）"
    TAGS = ["filter", "motion", "blur"]

    PARAM_DEFS = [
        ParamDef("shutter", "float", 0.5, min_val=0.0, max_val=2.0,
                 step=0.05, group="Motion"),
        ParamDef("use_project_shutter", "bool", True, group="Motion"),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("image", "input", "image"),
            Port("velocity", "input", "image"),
        ]
        self.outputs = [
            Port("result", "output", "image"),
        ]
        self.parameters = {
            "shutter": 0.5,
            "use_project_shutter": True,
        }
        self._init_port_revisions()

    def effective_shutter(self, ctx: Any) -> float:
        """Project shutter wins when available (CP-021)."""
        if self._get_param_bool("use_project_shutter", True):
            project_shutter = getattr(ctx, "motion_blur_shutter", None)
            try:
                if project_shutter is not None:
                    return min(2.0, max(0.0, float(project_shutter)))
            except (TypeError, ValueError):
                pass
        return self._get_param_clamped("shutter", 0.0, 2.0)

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any,
                     fb_manager: Any, resource_pool: Any = None,
                     render_mode: str = "preview") -> Dict[str, TextureResult]:
        image = inputs.get("image")
        has_image = image is not None and image.is_valid
        if not has_image:
            return {}
        velocity = inputs.get("velocity")
        velocity_valid = velocity is not None and velocity.is_valid
        width = max(1, int(image.width))
        height = max(1, int(image.height))
        return {
            "result": TextureResult(
                texture_id=-1, width=width, height=height,
                alpha_premultiplied=image.alpha_premultiplied,
                color_space=image.color_space),
            "_effect_type": "vectorblur",
            "_effect_input": image,
            "_effect_params": {
                "_velocity_tex": velocity if velocity_valid else None,
                "shutter": self.effective_shutter(ctx),
            },
        }
