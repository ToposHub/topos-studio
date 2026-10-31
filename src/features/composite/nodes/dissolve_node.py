"""
Dissolve Node - Linear interpolation between two inputs.

Mixes two inputs A and B based on a mix parameter where:
  - mix = 0.0 returns B (background)
  - mix = 1.0 returns A (foreground)
  - Values in between are linearly interpolated

This is commonly used for:
  - Version comparisons
  - Transitions
  - Mask blending
"""

from typing import Dict, Optional, Any
from .base_node import Node, Port, TextureResult, ParamDef

if __name__ != "__main__":
    from src.features.composite.core.format import BBox


# Output format/bbox policies (shared with MergeNode for consistent semantics).
FORMAT_POLICIES = ["larger", "from_b", "from_a", "smaller"]
BBOX_POLICIES = ["union", "intersection", "a", "b"]


class DissolveNode(Node):
    """Dissolves between two inputs based on mix parameter.

    Parameters:
      - mix: blend amount 0=B .. 1=A.
      - format_policy: how to pick the output canvas size from A/B.
      - bbox_policy: how to pick the output data window from A/B.
    """

    node_type = "dissolve"
    node_title = "Dissolve"
    header_color = "#8b5e9e"  # Purple

    # Metadata
    SEARCH_ALIASES = ["dissolve", "mix", "blend", "fade", "过渡", "混合", "淡入淡出"]
    DESCRIPTION = "在两个输入之间线性插值混合"
    TAGS = ["merge", "transition"]

    # Bypass mapping: when disabled, pass through B (mix=0 equivalent).
    BYPASS_MAPPING = {"result": "B"}

    PARAM_DEFS = [
        ParamDef("mix", "float", 0.5, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("format_policy", "option", "larger", labels=FORMAT_POLICIES),
        ParamDef("bbox_policy", "option", "union", labels=BBOX_POLICIES),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("A", "input", "image"),
            Port("B", "input", "image"),
        ]
        self.outputs = [
            Port("result", "output", "image")
        ]
        self.parameters = {
            "mix": 0.5,
            "format_policy": "larger",
            "bbox_policy": "union",
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        """Dissolve output bbox depends on bbox_policy (same as MergeNode)."""
        bbox_a = input_bboxes.get("A")
        bbox_b = input_bboxes.get("B")

        if bbox_a is None or bbox_a.is_empty:
            return bbox_b
        if bbox_b is None or bbox_b.is_empty:
            return bbox_a

        bbox_policy = params.get("bbox_policy", "union")
        if bbox_policy == "intersection":
            return bbox_a.intersection(bbox_b)
        elif bbox_policy == "a":
            return bbox_a
        elif bbox_policy == "b":
            return bbox_b
        else:  # "union" (default)
            return bbox_a.union(bbox_b)

    def compute_input_roi(self, output_roi: "BBox",
                          params: Dict[str, Any],
                          input_bboxes: Optional[Dict[str, "BBox"]] = None) -> Dict[str, "BBox"]:
        """Both inputs need the same ROI for dissolve."""
        return {"A": output_roi, "B": output_roi}

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                      resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        input_a = inputs.get("A")
        input_b = inputs.get("B")

        if input_a is None and input_b is None:
            return {}

        # Pass through single input
        if input_a is None or not input_a.is_valid:
            return {"result": input_b} if input_b else {}
        if input_b is None or not input_b.is_valid:
            return {"result": input_a}

        mix = self._get_param_clamped("mix", min_val=0.0, max_val=1.0)

        # Boundary short-circuits: emit the fully-resolved input directly so
        # no GPU dispatch is needed (the evaluator path also short-circuits,
        # but skipping the marker here lets the cache reuse upstream textures).
        if mix <= 0.0:
            return {"result": input_b}
        if mix >= 1.0:
            return {"result": input_a}

        format_policy = self.parameters.get("format_policy", "larger")
        bbox_policy = self.parameters.get("bbox_policy", "union")

        # Compute output canvas for the placeholder TextureResult.
        wa, ha = max(1, input_a.width), max(1, input_a.height)
        wb, hb = max(1, input_b.width), max(1, input_b.height)
        if format_policy == "from_a":
            out_w, out_h = wa, ha
        elif format_policy == "from_b":
            out_w, out_h = wb, hb
        elif format_policy == "smaller":
            out_w, out_h = min(wa, wb), min(ha, hb)
        else:  # "larger"
            out_w, out_h = max(wa, wb), max(ha, hb)

        # Return dissolve marker for GPU renderer.
        # Uses _effect_input_a/_effect_input_b (new protocol) so the renderer
        # can route both textures and the per-input params uniformly.
        return {
            "result": TextureResult(
                texture_id=-1,
                width=out_w,
                height=out_h,
            ),
            "_effect_type": "dissolve",
            "_input_a": input_a,
            "_input_b": input_b,
            "_effect_input_a": input_a,
            "_effect_input_b": input_b,
            "_effect_params": {
                "mix": mix,
                "format_policy": format_policy,
                "bbox_policy": bbox_policy,
            },
        }
