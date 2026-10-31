"""
Compare Node - AB comparison display for QC workflow.

Provides side-by-side, wipe, and difference comparison modes for
quality control and visual comparison of two image inputs.

Modes:
  - split: Left half shows A, right half shows B (vertical split)
  - wipe: Draggable split line with angle support
  - difference: Display |A - B| to highlight differences
"""

from typing import Dict, Optional, Any
from .base_node import Node, Port, TextureResult, ParamDef

if __name__ != "__main__":
    from src.features.composite.core.format import BBox


# All supported compare modes
COMPARE_MODES = ["split", "wipe", "difference"]


class CompareNode(Node):
    """AB comparison node for QC workflow."""

    node_type = "compare"
    node_title = "Compare"
    header_color = "#5e8b6e"  # Green

    IS_PLACEHOLDER = False

    # Metadata
    SEARCH_ALIASES = ["compare", "ab", "split", "wipe", "difference", "对比", "比较", "QC"]
    DESCRIPTION = "AB对比节点，支持split/wipe/difference模式"
    TAGS = ["utility", "qc"]

    # Bypass mapping: when disabled, pass through input A
    BYPASS_MAPPING = {"result": "A"}

    PARAM_DEFS = [
        ParamDef("mode", "option", "split", labels=COMPARE_MODES),
        ParamDef("split_position", "float", 0.5, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("split_angle", "float", 0.0, min_val=0.0, max_val=360.0, step=1.0),
        ParamDef("wipe_position", "float", 0.5, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("show_difference_value", "bool", False),
        ParamDef("difference_scale", "float", 1.0, min_val=0.0, max_val=10.0, step=0.1),
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
            "mode": "split",
            "split_position": 0.5,
            "split_angle": 0.0,
            "wipe_position": 0.5,
            "show_difference_value": False,
            "difference_scale": 1.0,
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        """Compare output bbox is the union of both inputs."""
        bbox_a = input_bboxes.get("A")
        bbox_b = input_bboxes.get("B")

        if bbox_a is None or bbox_a.is_empty:
            return bbox_b
        if bbox_b is None or bbox_b.is_empty:
            return bbox_a

        return bbox_a.union(bbox_b)

    def compute_input_roi(self, output_roi: "BBox",
                          params: Dict[str, Any],
                          input_bboxes: Optional[Dict[str, "BBox"]] = None) -> Dict[str, "BBox"]:
        """Both inputs need the same ROI for comparison."""
        return {"A": output_roi, "B": output_roi}

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                      resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        input_a = inputs.get("A")
        input_b = inputs.get("B")

        # Get parameters
        mode = self.parameters.get("mode", "split")
        split_position = self._get_param_clamped("split_position", 0.0, 1.0)
        split_angle = self._get_param_clamped("split_angle", 0.0, 360.0)
        wipe_position = self._get_param_clamped("wipe_position", 0.0, 1.0)
        show_difference_value = self._get_param_bool("show_difference_value", False)
        difference_scale = self._get_param_clamped("difference_scale", 0.0, 10.0)

        # Handle missing inputs
        if input_a is None and input_b is None:
            return {}

        # Pass through single input
        if input_a is None or not input_a.is_valid:
            return {"result": input_b} if input_b else {}
        if input_b is None or not input_b.is_valid:
            return {"result": input_a}

        # Determine output dimensions
        output_width = max(input_a.width, input_b.width)
        output_height = max(input_a.height, input_b.height)

        # Return compare marker for the shared GPU evaluator.  Keeping A/B in
        # the marker avoids coupling this node to a particular renderer.
        return {
            "result": TextureResult(
                texture_id=-1,
                width=output_width,
                height=output_height,
            ),
            "_effect_type": "compare",
            "_input_a": input_a,
            "_input_b": input_b,
            "_effect_params": {
                "mode": mode,
                "split_position": split_position,
                "split_angle": split_angle,
                "wipe_position": wipe_position,
                "show_difference_value": show_difference_value,
                "difference_scale": difference_scale,
            },
        }
