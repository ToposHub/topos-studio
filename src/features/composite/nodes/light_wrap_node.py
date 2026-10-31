"""Light Wrap node for integrating keyed foreground edges with a background.

The node samples the background around the foreground edge and adds a
premultiplied, alpha-aware wrap contribution.  It is deliberately a two-input
effect so the background used for the wrap is explicit and can be cached,
previewed, and exported through the normal composite graph.
"""

from typing import Any, Dict, Optional

from .base_node import Node, ParamDef, Port, TextureResult

if __name__ != "__main__":
    from src.features.composite.core.format import BBox


class LightWrapNode(Node):
    """Add a controllable background light wrap around a foreground matte."""

    node_type = "light_wrap"
    node_title = "Light Wrap"
    i18n_title_key = "nodes.light_wrap.title"
    header_color = "#b47a4c"
    IS_PLACEHOLDER = False

    SEARCH_ALIASES = [
        "light wrap", "lightwrap", "wrap", "edge light", "边缘光",
        "光包裹", "边缘融合",
    ]
    DESCRIPTION = "使用背景颜色在前景边缘生成可控的光包裹"
    TAGS = ["keyer", "matte", "composite", "edge"]

    PARAM_DEFS = [
        ParamDef("width", "float", 12.0, min_val=0.0, max_val=100.0,
                 step=0.5, group="Light Wrap"),
        ParamDef("intensity", "float", 0.35, min_val=0.0, max_val=2.0,
                 step=0.01, group="Light Wrap"),
        ParamDef("brightness", "float", 1.0, min_val=0.0, max_val=4.0,
                 step=0.01, group="Light Wrap"),
        ParamDef("falloff", "float", 1.0, min_val=0.1, max_val=4.0,
                 step=0.05, group="Light Wrap"),
        ParamDef("source_alpha", "bool", True,
                 group="Light Wrap"),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("background", "input", "image"),
            Port("foreground", "input", "image"),
        ]
        self.outputs = [
            Port("result", "output", "image"),
        ]
        self.parameters = {
            "width": 12.0,
            "intensity": 0.35,
            "brightness": 1.0,
            "falloff": 1.0,
            "source_alpha": True,
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        """The result covers both the background canvas and foreground data."""
        background = input_bboxes.get("background")
        foreground = input_bboxes.get("foreground")
        if background is None or background.is_empty:
            return foreground
        if foreground is None or foreground.is_empty:
            return background
        return background.union(foreground)

    def compute_input_roi(self, output_roi: "BBox", params: Dict[str, Any],
                          input_bboxes: Optional[Dict[str, "BBox"]] = None
                          ) -> Dict[str, "BBox"]:
        """Expand both inputs because the wrap samples outside the edge."""
        width = max(0, int(float(params.get("width", 12.0))))
        expanded = output_roi.expanded(width) if width else output_roi
        return {"background": expanded, "foreground": expanded}

    def requires_roi_expansion(self, params: Dict[str, Any]) -> int:
        """Return the sampling radius needed for seamless tiled evaluation."""
        return max(0, int(float(params.get("width", 12.0))))

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any,
                     fb_manager: Any, resource_pool: Any = None,
                     render_mode: str = "preview") -> Dict[str, TextureResult]:
        """Emit the renderer-neutral Light Wrap effect marker."""
        background = inputs.get("background")
        foreground = inputs.get("foreground")
        if (background is None or not background.is_valid or
                foreground is None or not foreground.is_valid):
            return {}

        width = max(0.0, float(self.parameters.get("width", 12.0)))
        intensity = max(0.0, float(self.parameters.get("intensity", 0.35)))
        brightness = max(0.0, float(self.parameters.get("brightness", 1.0)))
        falloff = max(0.1, float(self.parameters.get("falloff", 1.0)))
        if render_mode == "preview":
            width = min(width, 48.0)

        output_width = max(int(background.width), int(foreground.width), 1)
        output_height = max(int(background.height), int(foreground.height), 1)
        return {
            "result": TextureResult(
                texture_id=-1,
                width=output_width,
                height=output_height,
                format=foreground.format or background.format,
                bbox=foreground.bbox or background.bbox,
                color_space=foreground.color_space or background.color_space,
                alpha_premultiplied=True,
            ),
            "_effect_type": "light_wrap",
            "_effect_input_a": background,
            "_effect_input_b": foreground,
            "_effect_params": {
                "width": width,
                "intensity": intensity,
                "brightness": brightness,
                "falloff": falloff,
                "source_alpha": bool(self.parameters.get("source_alpha", True)),
            },
        }

