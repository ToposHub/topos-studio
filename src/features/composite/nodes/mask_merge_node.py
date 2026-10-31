"""
MaskMerge Node - Combine multiple mask/matte inputs.

Merges two alpha mattes using various operations:
add, subtract, multiply, max, min, and invert.

Essential for:
  - Combining multiple roto shapes
  - Creating complex masks from simple shapes
  - Matte compositing workflows
"""

from typing import Dict, Optional, Any
from .base_node import Node, Port, TextureResult, ParamDef

if __name__ != "__main__":
    from src.features.composite.core.format import BBox


# Merge operations for combining mattes
MERGE_OPERATIONS = [
    "add",         # A + B (clamped to 1.0)
    "subtract",    # A - B (clamped to 0.0)
    "multiply",    # A * B
    "max",         # max(A, B)
    "min",         # min(A, B)
    "difference",  # |A - B|
    "average",     # (A + B) / 2
    "screen",      # 1 - (1-A) * (1-B)
]


class MaskMergeNode(Node):
    """Combine multiple mask/matte inputs with various operations."""

    node_type = "mask_merge"
    node_title = "MaskMerge"
    header_color = "#9e6a4a"  # Warm orange-brown

    IS_PLACEHOLDER = False

    PARAM_DEFS = [
        ParamDef("operation", "option", "add", labels=MERGE_OPERATIONS),
        ParamDef("invert_a", "bool", False),
        ParamDef("invert_b", "bool", False),
        ParamDef("opacity_a", "float", 1.0, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("opacity_b", "float", 1.0, min_val=0.0, max_val=1.0, step=0.01),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("A", "input", "image"),
            Port("B", "input", "image"),
        ]
        self.outputs = [
            Port("result", "output", "image"),
            # Semantic alias: the merged result is also a clean matte, so it
            # can be wired directly into Merge.mask without an adapter node.
            Port("matte", "output", "image"),
        ]
        self.parameters = {
            "operation": "add",
            "invert_a": False,
            "invert_b": False,
            "opacity_a": 1.0,
            "opacity_b": 1.0,
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        """Declare the data window the dispatcher actually produces.

        AUD-7004: the old declaration returned ``union(A, B)`` while the
        dispatcher (filter_evaluator._apply_mask_merge) renders a
        ``max(A, B)``-sized texture and declares ``A.bbox or B.bbox`` as the
        result data window.  The scheduler consumed the union for ROI
        planning, so with offset/sub-window inputs the planned region could
        exceed the real output coverage.  Keep the node-side contract in
        lockstep with the dispatch side: A's bbox when present, else B's.
        """
        bbox_a = input_bboxes.get("A")
        bbox_b = input_bboxes.get("B")

        if bbox_a is None or bbox_a.is_empty:
            return bbox_b
        if bbox_b is None or bbox_b.is_empty:
            return bbox_a

        return bbox_a

    def compute_input_roi(self, output_roi: "BBox",
                          params: Dict[str, Any],
                          input_bboxes: Optional[Dict[str, "BBox"]] = None) -> Dict[str, "BBox"]:
        """Both inputs need the same ROI as output (per-pixel operation)."""
        return {
            "A": output_roi,
            "B": output_roi,
        }

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                      resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        """Merge two matte inputs."""
        input_a = inputs.get("A")
        input_b = inputs.get("B")

        if input_a is None and input_b is None:
            return {}

        # Pass through if only one valid input
        if input_a is None or not input_a.is_valid:
            return ({"result": input_b, "matte": input_b}
                    if input_b and input_b.is_valid else {})
        if input_b is None or not input_b.is_valid:
            return ({"result": input_a, "matte": input_a}
                    if input_a and input_a.is_valid else {})

        operation = self.parameters.get("operation", "add")
        invert_a = self.parameters.get("invert_a", False)
        invert_b = self.parameters.get("invert_b", False)
        opacity_a = self.parameters.get("opacity_a", 1.0)
        opacity_b = self.parameters.get("opacity_b", 1.0)

        # AUD-7004: mirror the dispatcher's output dimensions exactly —
        # filter_evaluator._apply_mask_merge renders into a
        # max(A.w, B.w) x max(A.h, B.h) texture.  (The old "B if valid else
        # A" was a constant-true dead branch after the early returns above.)
        width = max(1, int(max(input_a.width or 1, input_b.width or 1)))
        height = max(1, int(max(input_a.height or 1, input_b.height or 1)))

        # Return effect marker for CompositeRenderer
        return {
            "result": TextureResult(
                texture_id=-1,
                width=width,
                height=height,
            ),
            "matte": TextureResult(
                texture_id=-1,
                width=width,
                height=height,
            ),
            "_effect_type": "mask_merge",
            "_effect_input_a": input_a,
            "_effect_input_b": input_b,
            "_effect_params": {
                "operation": operation,
                "invert_a": invert_a,
                "invert_b": invert_b,
                "opacity_a": opacity_a,
                "opacity_b": opacity_b,
            },
        }
