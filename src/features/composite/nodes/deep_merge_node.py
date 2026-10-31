"""
Deep Merge Node - Deep compositing merge operations.

Composites two inputs (A=foreground, B=background) with depth information.
Supports depth-based sorting and compositing with optional mask.

Deep compositing allows per-pixel depth information for proper occlusion
and order-independent transparency. Each pixel has RGBA + Z (depth) values.

In NUKE's convention:
  A = foreground (top layer)
  B = background (bottom layer)
  Mask = optional per-pixel modulation of the merge effect
  Depth = per-pixel Z value (smaller = closer to camera)
"""

from typing import Dict, Optional, Any
from .base_node import Node, Port, TextureResult, ParamDef

if __name__ != "__main__":
    from src.features.composite.core.format import BBox


# Depth mode options
DEPTH_MODES = [
    "z_front",  # Front-most pixel occludes (smaller Z wins)
    "z_back",   # Back-most pixel shows through (larger Z wins)
    "z_merge",  # Depth-weighted merge for similar depths
]

# Format policy options (same as MergeNode)
FORMAT_POLICIES = ["from_b", "from_a", "larger", "smaller"]

# Bbox policy options (same as MergeNode)
BBOX_POLICIES = ["union", "intersection", "a", "b"]


class DeepMergeNode(Node):
    """Merges two deep inputs with optional mask.

    This node performs depth-aware compositing, using per-pixel Z values
    to determine proper occlusion. It supports:
    - z_front: Front-most (smallest Z) pixel occludes background
    - z_back: Back-most (largest Z) pixel shows through foreground
    - z_merge: Depth-weighted merge for similar depths (anti-aliasing)

    The output preserves depth information for downstream deep nodes.
    """

    node_type = "deep_merge"
    node_title = "Deep Merge"
    header_color = "#8b5e3c"  # Brown (same as Merge)

    # Metadata
    SEARCH_ALIASES = ["deep", "depth", "merge", "z", "deepmerge", "深度", "合并"]
    DESCRIPTION = "深度合并节点，基于 Z 值排序像素，支持深度信息合并"
    TAGS = ["merge", "composite", "deep"]

    # Bypass mapping: result comes from first available input
    BYPASS_MAPPING: Optional[Dict[str, str]] = {"result": "A"}

    PARAM_DEFS = [
        ParamDef("depth_mode", "option", "z_front", labels=DEPTH_MODES),
        ParamDef("depth_tolerance", "float", 0.01, min_val=0.0, max_val=1.0, step=0.001),
        ParamDef("opacity", "float", 1.0, min_val=0.0, max_val=1.0, step=0.01),
        ParamDef("format_policy", "option", "from_b", labels=FORMAT_POLICIES),
        ParamDef("bbox_policy", "option", "union", labels=BBOX_POLICIES),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("A", "input", "image"),      # Foreground (deep)
            Port("B", "input", "image"),      # Background (deep)
            Port("mask", "input", "image"),   # Optional mask
        ]
        self.outputs = [
            Port("result", "output", "image"),  # Output (deep)
        ]
        self.parameters = {
            "depth_mode": "z_front",
            "depth_tolerance": 0.01,
            "opacity": 1.0,
            "format_policy": "from_b",
            "bbox_policy": "union",
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        """Deep merge output bbox depends on bbox_policy (same as MergeNode)."""
        bbox_a = input_bboxes.get("A")
        bbox_b = input_bboxes.get("B")

        # Handle missing inputs
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
        """For deep merge, we need the output ROI from both A and B inputs.

        Mask input also needs the same ROI if present.
        """
        result = {}
        # A and B both need the same output ROI
        result["A"] = output_roi
        result["B"] = output_roi

        # Mask also needs the same ROI (if present)
        for port in self.inputs:
            if port.name == "mask":
                result["mask"] = output_roi

        return result

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                      resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        """Return deep merge marker for GPU dispatch.

        The actual GPU rendering is handled by GraphEvaluatorMixin._dispatch_deep_merge().
        """
        input_a = inputs.get("A")
        input_b = inputs.get("B")
        input_mask = inputs.get("mask")

        if input_a is None and input_b is None:
            return {}

        # Pass through if only one input
        if input_a is None or not input_a.is_valid:
            return {"result": input_b} if input_b else {}
        if input_b is None or not input_b.is_valid:
            return {"result": input_a}

        # Determine if inputs have depth info
        has_depth_a = input_a.has_depth() if input_a else False
        has_depth_b = input_b.has_depth() if input_b else False

        # If neither input has depth, fall back to regular merge behavior
        # S1-C: Use new `_effect_type: "merge"` protocol (same as MergeNode)
        if not has_depth_a and not has_depth_b:
            # Use regular blend shader (no deep compositing needed)
            return {
                "result": TextureResult(
                    texture_id=-1,
                    width=input_b.width if input_b.is_valid else input_a.width,
                    height=input_b.height if input_b.is_valid else input_a.height,
                ),
                "_effect_type": "merge",  # S1-C: unified marker protocol
                "_effect_input": input_b,
                "_effect_input_a": input_a,
                "_effect_input_b": input_b,
                "_effect_input_mask": input_mask,
                "_effect_params": {
                    "blend_mode": "over",  # Deep merge fallback uses standard over
                    "opacity": self._get_param_clamped("opacity", min_val=0.0, max_val=1.0),
                    "format_policy": self.parameters.get("format_policy", "from_b"),
                    "bbox_policy": self.parameters.get("bbox_policy", "union"),
                },
            }

        # Return deep merge marker for GPU dispatch
        return {
            "result": TextureResult(
                texture_id=-1,
                width=input_b.width if input_b.is_valid else input_a.width,
                height=input_b.height if input_b.is_valid else input_a.height,
                # Output will have depth if either input has depth
                has_deep_data=True,
            ),
            "_effect_type": "deep_merge",
            "_effect_params": {
                "depth_mode": self.parameters.get("depth_mode", "z_front"),
                "depth_tolerance": self._get_param_clamped("depth_tolerance", min_val=0.0, max_val=1.0),
                "opacity": self._get_param_clamped("opacity", min_val=0.0, max_val=1.0),
                "format_policy": self.parameters.get("format_policy", "from_b"),
                "bbox_policy": self.parameters.get("bbox_policy", "union"),
                "has_depth_a": has_depth_a,
                "has_depth_b": has_depth_b,
            },
            "_effect_input_a": input_a,
            "_effect_input_b": input_b,
            "_effect_mask": input_mask,
        }

    def _bypass_evaluate(self, inputs: Dict[str, TextureResult]) -> Dict[str, TextureResult]:
        """When disabled, pass through the first available deep input.

        Prefers A over B if both are available.
        """
        input_a = inputs.get("A")
        input_b = inputs.get("B")

        # Use BYPASS_MAPPING if defined
        if self.BYPASS_MAPPING:
            result: Dict[str, TextureResult] = {}
            for out_port, in_port in self.BYPASS_MAPPING.items():
                tex = inputs.get(in_port)
                if tex and tex.is_valid:
                    result[out_port] = tex
            return result

        # Default: pass through first available input
        if input_a and input_a.is_valid:
            return {"result": input_a}
        if input_b and input_b.is_valid:
            return {"result": input_b}

        return {}